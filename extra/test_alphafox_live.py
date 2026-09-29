"""alphafox_live 跟单实盘镜像层单元测试（mock 币安 exchange）。

覆盖：OPEN/ADD/CLOSE/REDUCE/LEV 信号镜像、开关关闭语义（不新开、
退出跟随）、与主流趋势互斥、对账补开/收敛/数量对齐、手动平仓。
"""
import io
import os
import sys
import tempfile
import time
import json

# 控制台是 GBK：断言名里的 ⚡ / − / ≈ 之类字符会让 print 直接抛 UnicodeEncodeError
# （曾把"断言失败"变成"脚本崩"，看不出真实结果）
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

_TMP = tempfile.mkdtemp(prefix="aflive_")
config.DB_PATH = os.path.join(_TMP, "main.db")
config.SUPERTREND_DB = os.path.join(_TMP, "supertrend.db")
config.LIVE_DB_PATH = os.path.join(_TMP, "live.db")
config.ALPHAFOX_DB = os.path.join(_TMP, "alphafox.db")
config.BINANCE_HEDGE_MODE = True

import storage
import alphafox_trade
import alphafox_live as afl

with storage.get_conn() as conn:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(storage.SCHEMA)
    # 生产由 storage._migrate 加的列，这里手动补（测试只跑 SCHEMA）
    conn.execute("ALTER TABLE trade_positions ADD COLUMN managed INTEGER DEFAULT 1")
    conn.executescript("""
    DROP TABLE IF EXISTS trade_signal_locks;
    CREATE TABLE trade_signal_locks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT NOT NULL,
        signal_key TEXT NOT NULL,
        strategy TEXT DEFAULT 'agent',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(token, signal_key, strategy)
    );
    """)
storage.init_onchain_live_db()
with storage.get_conn(config.SUPERTREND_DB) as conn:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS supertrend_tokens (
        token TEXT PRIMARY KEY, status TEXT DEFAULT 'ACTIVE',
        live_mode INTEGER DEFAULT 0
    );
    """)

PRICE = {}
alphafox_trade.get_mark_price = lambda token: PRICE.get(token)

# 比例模型（2026-09-17）：k =（我的可用余额 ÷ 交易员保证金余额），开仓冻结。
# 默认让 k = 1（余额 == 保证金）→ **目标量 == 交易员量 == 纸盘量**，
# 老用例里"交易所数量 == 纸盘数量"的断言语义不变，读起来也直观。
BAL = 100000.0

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


class FakeExchange:
    def __init__(self):
        self.positions = []
        self.calls = []
        self.last_error = ""
        self._last_error_msg = ""
        self.fail_positions = False
        self.fail_close_once = False
        self.zero_fill_once = False    # 平仓单零成交（被拒/EXPIRED 但返回 200）
        self.partial_fill_once = 0.0   # 非 0 → 平仓只成交这个量
        self.step = None      # 非 None 时按 stepSize 量化（模拟币安 LOT_SIZE）
        self.min_qty = 0.0
        self._lev = {}        # symbol -> 当前杠杆（币安按交易对生效）
        self._next_id = 9000
        # 比例模型要读"我的可用余额"（60s 缓存）；0 = 读不到（老用例不受影响）
        self.balance = BAL
        # 账户权益（判"像不像我们的仓"用）。默认与可用余额相同 = 空账户口径，
        # 老用例行为不变；要模拟"有仓压着可用余额"时单独改它
        self.equity = BAL
        self.fail_account = False
        # 开仓扣保证金 / 平仓退回（币安口径：可用余额 = 余额 − 已用保证金）。
        # 默认关 —— 老用例的 k = 余额÷保证金 依赖余额恒定，开成默认会全线漂移
        self.deduct_margin = False

    def get_account_info(self):
        if self.fail_account:
            raise RuntimeError("api down")
        return {"available_balance": self.balance, "equity": self.equity,
                "margin_used": 0.0}

    def get_balance(self):
        return self.balance

    def _rnd(self, qty):
        if not self.step:
            return qty
        return max(round(round(qty / self.step) * self.step, 8), self.min_qty)

    def _round_qty(self, symbol, qty):
        return self._rnd(qty)

    def _fetch_symbol_filters(self, symbol):
        # 生产里 exchange 会缓存 1h；桩里直接回 step（未设 step → 0 = 不额外设下限，
        # 这样不用量化的老用例行为完全不变）
        return {"stepSize": self.step or 0.0, "minQty": self.min_qty,
                "priceStep": 0.01}

    def get_positions(self):
        if self.fail_positions:
            raise RuntimeError("api down")
        return [dict(p) for p in self.positions if abs(p["quantity"]) > 1e-9]

    def get_last_error(self):
        return self.last_error

    def set_leverage(self, symbol, leverage):
        self.calls.append(("set_lev", symbol, leverage))
        self._lev[symbol] = leverage          # 币安杠杆按交易对生效
        for p in self.positions:
            if p["symbol"] == symbol:
                p["leverage"] = leverage
        return True

    def _market_open(self, token, side, quantity):
        symbol = f"{token}USDT"
        price = PRICE.get(token, 100.0)
        self.calls.append(("open", side, token, round(quantity, 8)))
        quantity = self._rnd(quantity)
        if self.deduct_margin:      # 币安：开仓吃掉可用余额（保证金）
            lev = max(self._lev.get(symbol, 3), 1)
            self.balance = max(self.balance - quantity * price / lev, 0.0)
        for p in self.positions:
            if p["symbol"] == symbol and p["position_side"] == side:
                total = p["quantity"] + quantity
                p["entry_price"] = ((p["entry_price"] * p["quantity"]
                                     + price * quantity) / total)
                p["quantity"] = total
                self._next_id += 1
                return {"order_id": self._next_id, "status": "FILLED",
                        "price": price, "quantity": quantity}
        # 新仓继承该交易对当前杠杆（币安语义：杠杆按 symbol 生效，先 set_leverage
        # 再开仓 → 开出来的仓就是这个杠杆）。原来写死 3，导致记录杠杆≠交易所杠杆
        # → 对账的 lev_bad 归属校验永远判"不是我们的仓" → 减仓路径实际没被测试覆盖
        self.positions.append({"symbol": symbol, "quantity": quantity,
                               "entry_price": price, "mark_price": price,
                               "unrealized_pnl": 0.0,
                               "leverage": self._lev.get(symbol, 3),
                               "position_side": side})
        self._next_id += 1
        return {"order_id": self._next_id, "status": "FILLED",
                "price": price, "quantity": quantity}

    def market_open_long(self, token, quantity):
        return self._market_open(token, "LONG", quantity)

    def market_open_short(self, token, quantity):
        return self._market_open(token, "SHORT", quantity)

    def close_position_market(self, token, quantity, position_side="LONG"):
        price = PRICE.get(token, 100.0)
        self.calls.append(("close", token, round(quantity, 8), position_side))
        if self.fail_close_once:
            self.fail_close_once = False
            return None
        if self.zero_fill_once:
            self.zero_fill_once = False
            return {"order_id": 9999, "price": price, "quantity": 0.0,
                    "status": "EXPIRED"}
        if self.partial_fill_once:
            quantity = self.partial_fill_once
            self.partial_fill_once = 0.0
        quantity = self._rnd(quantity)
        if self.deduct_margin:      # 平仓把那一份保证金退回可用余额
            lev = max(self._lev.get(f"{token}USDT", 3), 1)
            self.balance += quantity * price / lev
        sym = f"{token}USDT"
        for p in self.positions:
            if p["symbol"] == sym and p["position_side"] == position_side:
                p["quantity"] = max(p["quantity"] - quantity, 0)
        self._next_id += 1
        return {"order_id": self._next_id, "price": price,
                "quantity": quantity, "status": "FILLED"}

    def cancel_all_orders(self, token):
        return True

    def pos_qty(self, token, side="LONG"):
        for p in self.positions:
            if p["symbol"] == f"{token}USDT" and p["position_side"] == side:
                return p["quantity"]
        return 0.0


FAKE = FakeExchange()
afl.exchange = FAKE


def set_bal(v):
    """改"我的可用余额"并清 60s 缓存（下一读立刻生效）。

    权益一起设成同值 = 空账户口径（老用例的默认假设）；要模拟"有仓压着可用余额"
    用 set_equity()。"""
    FAKE.balance = float(v)
    FAKE.equity = float(v)
    afl._BAL_CACHE.update({"ts": 0.0, "v": 0.0, "eq": 0.0})


def set_equity(v):
    """只改账户权益（不动可用余额）→ 模拟"有仓占着保证金"的账户状态"""
    FAKE.equity = float(v)
    afl._BAL_CACHE.update({"ts": 0.0, "v": 0.0, "eq": 0.0})


def set_bal_av(v):
    """只改可用余额（权益保持不动）"""
    FAKE.balance = float(v)
    afl._BAL_CACHE.update({"ts": 0.0, "v": 0.0, "eq": 0.0})


def insert_paper(token, side="LONG", qty=1.0, entry=100.0, lev=3.0,
                 margin=50.0, closed_qty=0.0, age=None,
                 k_qty=1.0, trader_margin=None):
    """插一条活跃纸盘仓。

    k_qty / trader_margin 是比例模型的两个输入（写进 signal_snapshot）：
    - `_trader_qty_of` = quantity ÷ k_qty → 默认 k_qty=1 即"纸盘量 == 交易员量"
    - k = 我的可用余额 ÷ trader_margin → 默认 trader_margin=BAL（余额同值）→ k=1
    两者合起来：默认目标量 == 纸盘量（老用例的"1 纸盘单位 = 1 交易所单位"）。
    """
    snap = json.dumps({"k_qty": k_qty,
                       "trader_margin": BAL if trader_margin is None
                       else trader_margin})
    with storage.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO trade_positions (token, symbol, side, status, mode, "
            "margin_amount, leverage, notional, quantity, entry_price, "
            "current_price, strategy, closed_qty, signal_snapshot) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (token, f"{token}USDT", side, "OPEN", "paper", margin, lev,
             margin * lev, qty, entry, entry, "alphafox", closed_qty, snap))
        pid = cur.lastrowid
        if age is not None:
            conn.execute(
                "UPDATE trade_positions SET updated_at=datetime('now', ?) "
                "WHERE id=?", (f"{age} seconds", pid))
        return pid


def set_supertrend_occupy(token, occupy):
    with storage.get_conn(config.SUPERTREND_DB) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO supertrend_tokens (token, status, live_mode) "
            "VALUES (?, 'ACTIVE', ?)", (token, 1 if occupy else 0))
    if occupy:
        # 占用语义：超势 live_mode=1 且有活跃纸盘仓
        with storage.get_conn() as conn:
            conn.execute(
                "INSERT INTO trade_positions (token, symbol, side, status, mode, "
                "margin_amount, leverage, notional, quantity, entry_price, "
                "current_price, strategy) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (token, f"{token}USDT", "LONG", "OPEN", "paper", 50, 3, 150,
                 1, 100, 100, "supertrend"))


def calls_for(token):
    """与某 token 相关的交易所调用（sync 是全局的，会顺带处理别的测试仓）"""
    return [c for c in FAKE.calls if any(token in str(x) for x in c)]


def live_rows_of(token):
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        return [dict(r) for r in lc.execute(
            "SELECT * FROM live_positions WHERE token=? ORDER BY id DESC",
            (token,)).fetchall()]


def sig(action, token, side="LONG", executed=True, symbol=None, leverage=3,
        trader_margin=BAL):
    """纸盘信号（照 alphafox_trade 真实载荷造：trader_margin 是 k 的分母）"""
    d = {"action": action, "symbol": symbol or f"{token}USDT",
         "side": side, "executed": executed, "leverage": leverage}
    if trader_margin is not None:
        d["trader_margin"] = trader_margin
    return d


def main():
    PRICE["BTC"] = 60000.0

    print("== 1. OPEN 信号镜像（比例模型：目标 = 交易员量 × k） ==")
    # 纸盘量 0.001、k_qty 0.05 → 交易员持仓 0.02 枚
    # 余额 100U、交易员保证金 1000U → k = 0.1 → 我们的目标量 = 0.02 × 0.1 = 0.002
    insert_paper("BTC", qty=0.001, entry=60000, k_qty=0.05, trader_margin=1000.0)
    set_bal(100.0)
    afl.apply_signal(sig("OPEN", "BTC", trader_margin=1000.0), True, 0.0, 3.0)
    check("按比例开仓 0.002（交易员 0.02 × k=0.1）",
          abs(FAKE.pos_qty("BTC") - 0.002) < 1e-12, str(FAKE.pos_qty("BTC")))
    lr = live_rows_of("BTC")
    check("live 记录 OPEN strategy=alphafox",
          len(lr) == 1 and lr[0]["status"] == "OPEN"
          and lr[0]["strategy"] == "alphafox", str(lr))
    _ord = json.loads(lr[0]["exchange_orders_json"])
    check("记录冻结比例 k 与两个参考值",
          abs(float(_ord.get("k") or 0) - 0.1) < 1e-12
          and abs(float(_ord.get("balance_ref") or 0) - 100.0) < 1e-9
          and abs(float(_ord.get("trader_margin_ref") or 0) - 1000.0) < 1e-9,
          str(_ord))

    print("== 2. ADD 信号 → 按比例补齐到新目标 ==")
    # 交易员加到 0.04（纸盘 0.002）→ 目标 0.004 → 再买 0.002
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET quantity=0.002, "
            "updated_at=CURRENT_TIMESTAMP WHERE token='BTC' "
            "AND strategy='alphafox' AND status IN ('OPEN','PARTIAL')")
    afl.apply_signal(sig("ADD", "BTC"), True, 0.0, 3.0)
    check("加仓到 0.004（0.04 × k=0.1）",
          abs(FAKE.pos_qty("BTC") - 0.004) < 1e-12, str(FAKE.pos_qty("BTC")))
    check("记录数量与交易所一致",
          abs(float(live_rows_of("BTC")[0]["quantity"]) - 0.004) < 1e-12,
          str(live_rows_of("BTC")[0]["quantity"]))

    print("== 3. REDUCE 信号 → 按比例减（交易员减半，我们减半） ==")
    # 系统口径：quantity 是**累计总量**，减仓只累加 closed_qty
    # → 交易员当前量 = (quantity − closed_qty) ÷ k_qty = 0.001 ÷ 0.05 = 0.02
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET quantity=0.002, closed_qty=0.001, "
            "updated_at=CURRENT_TIMESTAMP WHERE token='BTC' "
            "AND strategy='alphafox' AND status IN ('OPEN','PARTIAL')")
    afl.apply_signal(sig("REDUCE", "BTC"), True, 0.0, 3.0)
    check("减到 0.002（交易员 0.02 × k=0.1）",
          abs(FAKE.pos_qty("BTC") - 0.002) < 1e-12, str(FAKE.pos_qty("BTC")))

    print("== 4. REDUCE 到零 → 全平 ==")
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET closed_qty=quantity, "
            "status='CLOSED', updated_at=CURRENT_TIMESTAMP WHERE token='BTC' "
            "AND strategy='alphafox' AND status IN ('OPEN','PARTIAL')")
    afl.apply_signal(sig("REDUCE", "BTC"), True, 0.0, 3.0)
    check("币安已平", FAKE.pos_qty("BTC") == 0.0)
    lr = live_rows_of("BTC")
    check("live 记录 CLOSED", lr and lr[0]["status"] == "CLOSED",
          str(lr[0] if lr else None))
    set_bal(BAL)      # 还原：后面的用例按 k=1 设计

    print("== 5. CLOSE 信号（开关关闭也跟随平仓） ==")
    insert_paper("ETH", qty=0.01, entry=3000)
    PRICE["ETH"] = 3000.0
    afl.apply_signal(sig("OPEN", "ETH"), True, 0.0, 3.0)
    check("前置：ETH 实盘已开", FAKE.pos_qty("ETH") > 0)
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
            "WHERE token='ETH' AND strategy='alphafox' "
            "AND status IN ('OPEN','PARTIAL')")
    afl.apply_signal(sig("CLOSE", "ETH"), False, 0.0, 3.0)
    check("开关关：CLOSE 仍平实盘", FAKE.pos_qty("ETH") == 0.0)
    lr = live_rows_of("ETH")
    check("开关关：live 记录 CLOSED", lr and lr[0]["status"] == "CLOSED")

    print("== 6. 开关关闭：OPEN/ADD 不镜像 ==")
    insert_paper("SOL", qty=1.0, entry=200)
    PRICE["SOL"] = 200.0
    afl.apply_signal(sig("OPEN", "SOL"), False, 0.0, 3.0)
    check("开关关：未开实盘", FAKE.pos_qty("SOL") == 0.0)
    check("开关关：无 live 记录", len(live_rows_of("SOL")) == 0)

    print("== 7. 互斥：supertrend 占用 → 跳过镜像 ==")
    set_supertrend_occupy("SOL", True)
    afl.apply_signal(sig("OPEN", "SOL"), True, 0.0, 3.0)
    check("被占用：未开实盘", FAKE.pos_qty("SOL") == 0.0)
    set_supertrend_occupy("SOL", False)
    afl.apply_signal(sig("OPEN", "SOL"), True, 0.0, 3.0)
    check("释放后：正常镜像", FAKE.pos_qty("SOL") > 0)

    print("== 8. 对账补开（纸盘有、live 无） ==")
    insert_paper("XRP", qty=10.0, entry=2.0)
    PRICE["XRP"] = 2.0
    afl._rounds_done = 99  # 跳过冷窗
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("对账补开：币安已开", FAKE.pos_qty("XRP") > 0)
    lr = live_rows_of("XRP")
    check("对账补开：live 记录存在", len(lr) == 1)

    print("== 9. 对账收敛（纸盘无、live 有） ==")
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
            "WHERE token='XRP' AND strategy='alphafox' "
            "AND status IN ('OPEN','PARTIAL')")
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("对账收敛：币安已平", FAKE.pos_qty("XRP") == 0.0)
    lr = live_rows_of("XRP")
    check("对账收敛：live 记录 CLOSED", lr and lr[0]["status"] == "CLOSED")

    print("== 10. 比例模型：价格漂移不再调仓（目标只跟交易员量走） ==")
    insert_paper("ADA", qty=5.0, entry=1.0)
    PRICE["ADA"] = 1.0
    afl.apply_signal(sig("OPEN", "ADA"), True, 0.0, 3.0)
    check("前置：开 5.0（交易员 5.0 × k=1）",
          abs(FAKE.pos_qty("ADA") - 5.0) < 1e-9, str(FAKE.pos_qty("ADA")))
    PRICE["ADA"] = 1.25   # 价格涨 25%：目标与现价无关 → 一个动作都不该有
    next(p for p in FAKE.positions
         if p["symbol"] == "ADAUSDT")["mark_price"] = 1.25
    _c0 = len(FAKE.calls)
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("价格 +25% 不再调仓（目标 = 交易员量 × 冻结 k，与现价无关）",
          abs(FAKE.pos_qty("ADA") - 5.0) < 1e-9
          and all(c[0] not in ("open", "close") for c in FAKE.calls[_c0:]),
          f"{FAKE.pos_qty('ADA')} {FAKE.calls[_c0:]}")

    print("== 11. 手动平仓 close_live ==")
    ok = afl.close_live("ADA")
    check("返回已处理", ok is True, str(ok))
    check("纸盘已平", True)
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM trade_positions WHERE token='ADA' "
            "AND strategy='alphafox' AND status IN ('OPEN','PARTIAL')").fetchone()
    check("纸盘无活跃仓", row is None)
    check("币安已平", FAKE.pos_qty("ADA") == 0.0)
    lr = live_rows_of("ADA")
    check("live 记录 CLOSED", lr and lr[0]["status"] == "CLOSED")

    print("== 15. live 记录丢失 + 交易所残留 → ADD 补齐到新目标，不重复开 ==")
    insert_paper("AVAX", qty=2.0, entry=20.0)
    PRICE["AVAX"] = 20.0
    afl.apply_signal(sig("OPEN", "AVAX"), True, 0.0, 3.0)
    check("前置：实盘 2.0（交易员 2.0 × k=1）",
          abs(FAKE.pos_qty("AVAX") - 2.0) < 1e-9, str(FAKE.pos_qty("AVAX")))
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        lc.execute("DELETE FROM live_positions WHERE token='AVAX'")  # 模拟 DB 清理
    with storage.get_conn() as conn:   # 交易员加仓 → 纸盘 4.0 → 目标 4.0
        conn.execute(
            "UPDATE trade_positions SET quantity=4.0, "
            "updated_at=CURRENT_TIMESTAMP WHERE token='AVAX' "
            "AND strategy='alphafox' AND status IN ('OPEN','PARTIAL')")
    afl.apply_signal(sig("ADD", "AVAX"), True, 0.0, 3.0)
    check("ADD 补到新目标（残留 2 + 补 2 = 4）",
          abs(FAKE.pos_qty("AVAX") - 4.0) < 1e-9, str(FAKE.pos_qty("AVAX")))
    lr = live_rows_of("AVAX")
    check("补建 live 记录（OPEN，指向当前纸盘仓）",
          len(lr) == 1 and lr[0]["status"] == "OPEN", str(lr))
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("对账：已在目标 → 一笔都不成交",
          abs(FAKE.pos_qty("AVAX") - 4.0) < 1e-9, str(FAKE.pos_qty("AVAX")))

    print("== 16. 存量仓豁免：开关开启前的仓不补开，新变化正常 ==")
    pid = insert_paper("DOT", qty=10.0, entry=5.0)
    PRICE["DOT"] = 5.0
    # 模拟开关 0→1 转变：当前活跃纸盘仓记录为豁免（token|side#纸盘仓id 键）
    with storage.get_conn() as conn:
        storage.trading_settings_update(
            conn, {"alphafox_live_exempt_paper_ids":
                   json.dumps([f"DOT|LONG#{pid}"])})
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("豁免仓：不补开", FAKE.pos_qty("DOT") == 0.0)
    check("豁免仓：无 live 记录", len(live_rows_of("DOT")) == 0)
    # 豁免仓平掉 → 清单维护清空
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
            "WHERE token='DOT' AND strategy='alphafox' "
            "AND status IN ('OPEN','PARTIAL')")
    afl.sync_alphafox_live(True, 0.0, 3.0)
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM trading_settings "
            "WHERE key='alphafox_live_exempt_paper_ids'").fetchone()
    check("清单已清空", row is None or row["value"] in ("", "[]"), str(row))
    # 新仓（非豁免）→ 正常补开
    insert_paper("DOT", qty=10.0, entry=5.0)
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("新仓：正常补开", FAKE.pos_qty("DOT") > 0)

    print("== 17. 豁免仓全部动作不镜像；平仓解除豁免后重开正常 ==")
    _wpid = insert_paper("WIF", qty=2.0, entry=2.5)
    PRICE["WIF"] = 2.5
    with storage.get_conn() as conn:
        storage.trading_settings_update(
            conn, {"alphafox_live_exempt_paper_ids":
                   json.dumps([f"WIF|LONG#{_wpid}"])})
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("前置：豁免仓未补开", FAKE.pos_qty("WIF") == 0.0)
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET quantity=3.0, notional=7.5, "
            "updated_at=CURRENT_TIMESTAMP WHERE token='WIF' "
            "AND strategy='alphafox' AND status IN ('OPEN','PARTIAL')")
    afl.apply_signal(sig("ADD", "WIF"), True, 0.0, 3.0)
    check("豁免仓 ADD：不镜像（仍 0）", FAKE.pos_qty("WIF") == 0.0)
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
            "WHERE token='WIF' AND strategy='alphafox' "
            "AND status IN ('OPEN','PARTIAL')")
    afl.apply_signal(sig("CLOSE", "WIF"), True, 0.0, 3.0)
    check("豁免仓 CLOSE：不兜底平（仍 0）", FAKE.pos_qty("WIF") == 0.0)
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM trading_settings "
            "WHERE key='alphafox_live_exempt_paper_ids'").fetchone()
    check("豁免已解除", row is None or row["value"] in ("", "[]"), str(row))
    # 交易员重开 → 视为新仓，正常镜像
    insert_paper("WIF", qty=2.0, entry=2.5)
    afl.apply_signal(sig("OPEN", "WIF"), True, 0.0, 3.0)
    check("重开：正常镜像", FAKE.pos_qty("WIF") > 0)

    print("== 18. 豁免+live 并存（关开关再开）→ CLOSE 照常平实盘 ==")
    insert_paper("SUI", qty=3.0, entry=1.0)
    PRICE["SUI"] = 1.0
    afl.apply_signal(sig("OPEN", "SUI"), True, 0.0, 3.0)
    check("前置：实盘 3.0", abs(FAKE.pos_qty("SUI") - 3.0) < 1e-9)
    # 模拟关开关再开：SUI key 进豁免清单（live 记录仍在）
    with storage.get_conn() as conn:
        _spid = conn.execute("SELECT id FROM trade_positions WHERE token='SUI' "
                             "AND strategy='alphafox' ORDER BY id DESC LIMIT 1"
                             ).fetchone()["id"]
        storage.trading_settings_update(
            conn, {"alphafox_live_exempt_paper_ids":
                   json.dumps([f"SUI|LONG#{_spid}"])})
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
            "WHERE token='SUI' AND strategy='alphafox' "
            "AND status IN ('OPEN','PARTIAL')")
    afl.apply_signal(sig("CLOSE", "SUI"), True, 0.0, 3.0)
    check("CLOSE：实盘已平（豁免不阻退出跟随）", FAKE.pos_qty("SUI") == 0.0)
    lr = live_rows_of("SUI")
    check("CLOSE：live 记录 CLOSED", lr and lr[0]["status"] == "CLOSED",
          str(lr[0] if lr else None))
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM trading_settings "
            "WHERE key='alphafox_live_exempt_paper_ids'").fetchone()
    check("豁免已解除", row is None or row["value"] in ("", "[]"), str(row))

    print("== 19. 换向场景：OPEN 新方向不挂旧方向 live（防僵尸） ==")
    insert_paper("TON", side="LONG", qty=2.0, entry=5.0)
    PRICE["TON"] = 5.0
    afl.apply_signal(sig("OPEN", "TON", side="LONG"), True, 0.0, 3.0)
    check("前置：LONG 实盘 2.0", abs(FAKE.pos_qty("TON", "LONG") - 2.0) < 1e-9)
    # 模拟换向：纸盘 LONG 平 + SHORT 开（CLOSE 镜像失败 → LONG live 残留）
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
            "WHERE token='TON' AND strategy='alphafox' "
            "AND status IN ('OPEN','PARTIAL')")
    insert_paper("TON", side="SHORT", qty=2.0, entry=5.0)
    afl.apply_signal(sig("OPEN", "TON", side="SHORT"), True, 0.0, 3.0)
    check("SHORT 实盘已开 2.0", abs(FAKE.pos_qty("TON", "SHORT") - 2.0) < 1e-9,
          str(FAKE.pos_qty("TON", "SHORT")))
    lr = [r for r in live_rows_of("TON") if r["status"] == "OPEN"]
    check("两条 live 按 side 分开（LONG 残留 + SHORT 新开）",
          len(lr) == 2 and {r["side"] for r in lr} == {"LONG", "SHORT"}, str(lr))
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("对账收敛：LONG 残留已平", FAKE.pos_qty("TON", "LONG") == 0.0)
    check("SHORT 持仓保留", abs(FAKE.pos_qty("TON", "SHORT") - 2.0) < 1e-9)

    print("== 20. 手动平仓：多方向 live 全平 ==")
    insert_paper("NEAR", side="LONG", qty=1.0, entry=5.0)
    PRICE["NEAR"] = 5.0
    afl.apply_signal(sig("OPEN", "NEAR", side="LONG"), True, 0.0, 3.0)
    # 交易员换向：先平 LONG 再开 SHORT（符合真实时序）
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
            "WHERE token='NEAR' AND strategy='alphafox' "
            "AND status IN ('OPEN','PARTIAL')")
    insert_paper("NEAR", side="SHORT", qty=1.0, entry=5.0)
    afl.apply_signal(sig("OPEN", "NEAR", side="SHORT"), True, 0.0, 3.0)
    check("前置：LONG 1.0 + SHORT 1.0",
          abs(FAKE.pos_qty("NEAR", "LONG") - 1.0) < 1e-9
          and abs(FAKE.pos_qty("NEAR", "SHORT") - 1.0) < 1e-9)
    ok = afl.close_live("NEAR")
    check("返回已处理", ok is True, str(ok))
    check("交易所两边全平",
          FAKE.pos_qty("NEAR", "LONG") == 0.0 and FAKE.pos_qty("NEAR", "SHORT") == 0.0)
    lr = live_rows_of("NEAR")
    check("live 全部 CLOSED",
          len(lr) == 2 and all(r["status"] == "CLOSED" for r in lr), str(lr))

    print("== 12. 无纸盘仓的 close_live 返回 False ==")
    ok = afl.close_live("NONEXIST")
    check("返回 False", ok is False, str(ok))

    print("== 13. CLOSE 时 live 记录丢失 → 交易所兜底平仓 ==")
    insert_paper("DOGE", qty=100.0, entry=0.1)
    PRICE["DOGE"] = 0.1
    afl.apply_signal(sig("OPEN", "DOGE"), True, 0.0, 3.0)
    check("前置：实盘已开", FAKE.pos_qty("DOGE") > 0)
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        lc.execute("DELETE FROM live_positions WHERE token='DOGE'")  # 模拟 DB 清理
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
            "WHERE token='DOGE' AND strategy='alphafox' "
            "AND status IN ('OPEN','PARTIAL')")
    afl.apply_signal(sig("CLOSE", "DOGE"), True, 0.0, 3.0)
    check("CLOSE 兜底：交易所已平", FAKE.pos_qty("DOGE") == 0.0)

    print("== 14. 开关关闭：对账补仓跳过（减仓照常） ==")
    insert_paper("LINK", qty=0.5, entry=15.0)
    PRICE["LINK"] = 15.0
    afl.apply_signal(sig("OPEN", "LINK"), True, 0.0, 3.0)
    q15 = 0.5      # 交易员 0.5 × k=1
    check("前置：实盘 0.5", abs(FAKE.pos_qty("LINK") - q15) < 1e-9)
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET quantity=1.0, "
            "updated_at=CURRENT_TIMESTAMP WHERE token='LINK' "
            "AND strategy='alphafox' AND status IN ('OPEN','PARTIAL')")
    afl.sync_alphafox_live(False, 0.0, 3.0)
    check("开关关：对账不补仓（交易员加到 1.0 也不动）",
          abs(FAKE.pos_qty("LINK") - q15) < 1e-9,
          str(FAKE.pos_qty("LINK")))
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET quantity=0.5, closed_qty=0.7, "
            "updated_at=CURRENT_TIMESTAMP WHERE token='LINK' "
            "AND strategy='alphafox' AND status IN ('OPEN','PARTIAL')")
    afl.sync_alphafox_live(False, 0.0, 3.0)
    check("开关关：对账也不减仓（交易员减到 0.5 仍不动）",
          abs(FAKE.pos_qty("LINK") - q15) < 1e-9,
          str(FAKE.pos_qty("LINK")))

    print("== 21. 手动仓防护：同币同向手动仓 → 不镜像、兜底不误平 ==")
    FAKE.positions.append({"symbol": "LTCUSDT", "quantity": 5.0,
                           "entry_price": 100.0, "mark_price": 100.0,
                           "unrealized_pnl": 0.0, "leverage": 75,
                           "position_side": "LONG"})
    insert_paper("LTC", qty=2.0, entry=100.0)
    PRICE["LTC"] = 100.0
    afl.apply_signal(sig("OPEN", "LTC"), True, 0.0, 3.0)
    check("手动仓在：OPEN 不镜像（未加仓）",
          abs(FAKE.pos_qty("LTC") - 5.0) < 1e-9, str(FAKE.pos_qty("LTC")))
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("手动仓在：对账补开也不镜像",
          abs(FAKE.pos_qty("LTC") - 5.0) < 1e-9, str(FAKE.pos_qty("LTC")))
    check("手动仓在：无 live 记录", len(live_rows_of("LTC")) == 0)
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
            "WHERE token='LTC' AND strategy='alphafox' "
            "AND status IN ('OPEN','PARTIAL')")
    afl.apply_signal(sig("CLOSE", "LTC"), True, 0.0, 3.0)
    check("手动仓在：CLOSE 兜底不误平",
          abs(FAKE.pos_qty("LTC") - 5.0) < 1e-9, str(FAKE.pos_qty("LTC")))

    print("== 22. 反方向手动仓：首次开仓跳过（不改该币杠杆） ==")
    FAKE.positions.append({"symbol": "XMRUSDT", "quantity": -3.0,
                           "entry_price": 50.0, "mark_price": 50.0,
                           "unrealized_pnl": 0.0, "leverage": 20,
                           "position_side": "SHORT"})
    insert_paper("XMR", qty=2.0, entry=50.0)
    PRICE["XMR"] = 50.0
    before = len(FAKE.calls)
    afl.apply_signal(sig("OPEN", "XMR"), True, 0.0, 3.0)
    check("反方向手动仓：OPEN 不镜像（无新开仓动作）",
          all(c[0] != "open" for c in FAKE.calls[before:]), str(FAKE.calls[before:]))
    check("反方向手动仓：其持仓未被动",
          abs(FAKE.pos_qty("XMR", "SHORT") + 3.0) < 1e-9,
          str(FAKE.pos_qty("XMR", "SHORT")))

    print("== 23. 对账减仓：交易所混入手动仓 → 不越界减仓 ==")
    insert_paper("ICP", qty=1.0, entry=100.0)
    PRICE["ICP"] = 100.0
    afl.apply_signal(sig("OPEN", "ICP"), True, 0.0, 3.0)
    check("前置：实盘一份 1.0", abs(FAKE.pos_qty("ICP") - 1.0) < 1e-9,
          str(FAKE.pos_qty("ICP")))
    # 手动加仓把交易所持仓抬到 1.5（名义 150 → 与 1 份 100 / 2 份 200 都对不上）
    for p in FAKE.positions:
        if p["symbol"] == "ICPUSDT":
            p["quantity"] = 1.5
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("对账不越界减仓（仍 1.5）", abs(FAKE.pos_qty("ICP") - 1.5) < 1e-9,
          str(FAKE.pos_qty("ICP")))

    print("== 24. 补建路径：live 记录丢失 + 交易所残留 → 补建记录不重复开 ==")
    insert_paper("FIL", qty=2.0, entry=100.0)
    PRICE["FIL"] = 100.0
    afl.apply_signal(sig("OPEN", "FIL"), True, 0.0, 3.0)
    qty_after_open = FAKE.pos_qty("FIL")
    check("前置：实盘 2.0（交易员 2.0 × k=1）",
          abs(qty_after_open - 2.0) < 1e-9, str(qty_after_open))
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        lc.execute("DELETE FROM live_positions WHERE token='FIL'")  # 模拟 DB 清理
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("补建记录：live 行已建", len(live_rows_of("FIL")) == 1,
          str(live_rows_of("FIL")))
    check("补建记录：交易所未重复开（仍 2.0）",
          abs(FAKE.pos_qty("FIL") - 2.0) < 1e-9, str(FAKE.pos_qty("FIL")))
    # 再跑两轮：不得因记录状态问题反复开仓
    afl.sync_alphafox_live(True, 0.0, 3.0)
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("连续对账不膨胀（仍 2.0）", abs(FAKE.pos_qty("FIL") - 2.0) < 1e-9,
          str(FAKE.pos_qty("FIL")))

    print("== 25. 记录已 CLOSED 残留 + 纸盘仍开 → 不自动重开（防无限膨胀） ==")
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        lc.execute("UPDATE live_positions SET status='CLOSED', closed_at="
                   "CURRENT_TIMESTAMP WHERE token='FIL'")
    before = len(FAKE.calls)
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("不自动重开（无新开仓动作）",
          all(c[0] != "open" for c in FAKE.calls[before:]), str(FAKE.calls[before:]))
    # 信号驱动仍可重建（复用 CLOSED 行，不新增行）
    with storage.get_conn() as conn:   # 交易员加到 4.0 → 目标 4.0（交易所已有 2.0）
        conn.execute(
            "UPDATE trade_positions SET quantity=4.0, "
            "updated_at=CURRENT_TIMESTAMP WHERE token='FIL' "
            "AND strategy='alphafox' AND status IN ('OPEN','PARTIAL')")
    afl.apply_signal(sig("ADD", "FIL"), True, 0.0, 3.0)
    check("信号驱动重开：交易所补到新目标 4.0",
          abs(FAKE.pos_qty("FIL") - 4.0) < 1e-9, str(FAKE.pos_qty("FIL")))
    lr = live_rows_of("FIL")
    check("复用原行（仍 1 行）且 OPEN", len(lr) == 1 and lr[0]["status"] == "OPEN",
          str([(r["id"], r["status"]) for r in lr]))

    print("== 26. 平仓 API 无返回 → 不标 CLOSED，下轮重试 ==")
    insert_paper("APT", qty=1.0, entry=100.0)
    PRICE["APT"] = 100.0
    afl.apply_signal(sig("OPEN", "APT"), True, 0.0, 3.0)
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
            "WHERE token='APT' AND strategy='alphafox' "
            "AND status IN ('OPEN','PARTIAL')")
    FAKE.fail_close_once = True
    afl.apply_signal(sig("CLOSE", "APT"), True, 0.0, 3.0)
    check("平仓失败：交易所仍持仓", FAKE.pos_qty("APT") > 0,
          str(FAKE.pos_qty("APT")))
    check("平仓失败：记录不标 CLOSED",
          live_rows_of("APT")[0]["status"] == "OPEN",
          live_rows_of("APT")[0]["status"])
    afl.apply_signal(sig("CLOSE", "APT"), True, 0.0, 3.0)
    check("重试后平干净", FAKE.pos_qty("APT") == 0.0, str(FAKE.pos_qty("APT")))
    check("重试后记录 CLOSED", live_rows_of("APT")[0]["status"] == "CLOSED")

    print("== 27. 交易所持仓消失（纸盘仍在）→ 记录收敛，不自动重开 ==")
    insert_paper("OP", qty=1.0, entry=100.0)
    PRICE["OP"] = 100.0
    afl.apply_signal(sig("OPEN", "OP"), True, 0.0, 3.0)
    check("前置：实盘 1.0", abs(FAKE.pos_qty("OP") - 1.0) < 1e-9)
    for p in FAKE.positions:  # 模拟手动平仓/强平：交易所持仓消失
        if p["symbol"] == "OPUSDT":
            p["quantity"] = 0.0
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        lc.execute("UPDATE live_positions SET created_at=datetime('now','-200 seconds') "
                   "WHERE token='OP'")
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("记录收敛为 CLOSED", live_rows_of("OP")[0]["status"] == "CLOSED",
          live_rows_of("OP")[0]["status"])
    before = len(FAKE.calls)
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("不自动重开（无新开仓动作）",
          all(c[0] != "open" for c in FAKE.calls[before:]), str(FAKE.calls[before:]))
    # 用户语义（2026-09-14 确认）：手动平掉这一笔 = **这笔不再跟单** → 纸盘同笔收尾
    with storage.get_conn() as conn:
        _op = conn.execute(
            "SELECT status, advice FROM trade_positions WHERE token='OP' "
            "AND strategy='alphafox' ORDER BY id DESC LIMIT 1").fetchone()
    check("纸盘这一笔也一起收尾（CLOSED + 留痕）",
          _op is not None and _op["status"] == "CLOSED"
          and "不再跟这一笔" in (_op["advice"] or ""),
          str(dict(_op) if _op else None))
    # 之后交易员再加仓 → 不该复活（不下单、不新建记录）
    before = len(FAKE.calls)
    afl.apply_signal(sig("ADD", "OP"), True, 0.0, 3.0)
    _act = [c for c in FAKE.calls[before:] if "OP" in str(c)]
    check("交易员再加仓：实盘无任何动作（这笔已收尾）", not _act, str(_act))
    check("交易员再加仓：不会新建 live 记录",
          len(live_rows_of("OP")) == 1,
          str([(r["id"], r["status"]) for r in live_rows_of("OP")]))

    print("== 28. 豁免仓 + live 并存 → 对账不动交易所 ==")
    _apid = insert_paper("ATOM", qty=1.0, entry=100.0)
    PRICE["ATOM"] = 100.0
    afl.apply_signal(sig("OPEN", "ATOM"), True, 0.0, 3.0)
    with storage.get_conn() as conn:
        storage.trading_settings_update(
            conn, {"alphafox_live_exempt_paper_ids":
                   json.dumps([f"ATOM|LONG#{_apid}"])})
    # 把交易所数量改小（低于目标一份）→ 非豁免时对账会补仓，豁免时不应有任何动作
    for p in FAKE.positions:
        if p["symbol"] == "ATOMUSDT":
            p["quantity"] = 0.5
    before = len(FAKE.calls)
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("豁免仓对账：交易所动作 = 0",
          all(c[0] != "open" for c in FAKE.calls[before:]), str(FAKE.calls[before:]))
    check("豁免仓对账：交易所数量不变（不被补到目标）",
          abs(FAKE.pos_qty("ATOM") - 0.5) < 1e-9, str(FAKE.pos_qty("ATOM")))
    with storage.get_conn() as conn:
        storage.trading_settings_update(
            conn, {"alphafox_live_exempt_paper_ids": json.dumps([])})
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("解除豁免后对账恢复（补回一份）",
          abs(FAKE.pos_qty("ATOM") - 1.0) < 1e-9, str(FAKE.pos_qty("ATOM")))

    print("== 29. 数量量化：目标非 step 整数倍 → 不反复补/减（振荡） ==")
    FAKE.step = 0.001
    FAKE.min_qty = 0.001
    # 交易员 0.001282 枚（纸盘 1.0 ÷ k_qty 780）；目标 = 交易员量 × k=1
    # = 0.001282 → 量化到 step 0.001（非整数倍才咬得住振荡）
    insert_paper("BTCX", qty=1.0, entry=78000.0, k_qty=780.0)
    PRICE["BTCX"] = 78000.0
    afl.apply_signal(sig("OPEN", "BTCX"), True, 0.0, 3.0)
    q0 = FAKE.pos_qty("BTCX")
    check("开仓按 step 量化到 0.001", abs(q0 - 0.001) < 1e-12, str(q0))
    # 记录里的目标量也必须是**量化值**（对账以它为基准；不量化的话每轮都差半个
    # step，靠容忍带兜着 —— 带子一收紧就变成反复反向成交）
    check("记录里的数量同样是量化值 0.001",
          abs(float(live_rows_of("BTCX")[0]["quantity"]) - 0.001) < 1e-12,
          str(live_rows_of("BTCX")[0]["quantity"]))
    for _ in range(4):
        afl.sync_alphafox_live(True, 0.0, 3.0)
    check("连续 4 轮对账不振荡（仍 0.001）",
          abs(FAKE.pos_qty("BTCX") - 0.001) < 1e-12, str(FAKE.pos_qty("BTCX")))
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        lv = dict(lc.execute("SELECT * FROM live_positions "
                             "WHERE token='BTCX'").fetchone())
    check("记录数量与交易所一致（量化后 0.001，不再漂移）",
          abs(float(lv["quantity"]) - 0.001) < 1e-12, str(lv["quantity"]))
    FAKE.step = None
    FAKE.min_qty = 0.0

    print("== 30. REDUCE 平仓无返回 → 记账不减（下轮重试） ==")
    insert_paper("LDO", qty=1.0, entry=100.0)
    PRICE["LDO"] = 100.0
    afl.apply_signal(sig("OPEN", "LDO"), True, 0.0, 3.0)
    with storage.get_conn() as conn:   # 交易员加到 2.0
        conn.execute(
            "UPDATE trade_positions SET quantity=2.0, "
            "updated_at=CURRENT_TIMESTAMP WHERE token='LDO' "
            "AND strategy='alphafox' AND status IN ('OPEN','PARTIAL')")
    afl.apply_signal(sig("ADD", "LDO"), True, 0.0, 3.0)
    lv_before = live_rows_of("LDO")[0]
    check("前置：实盘 2.0", abs(FAKE.pos_qty("LDO") - 2.0) < 1e-9,
          str(FAKE.pos_qty("LDO")))
    FAKE.fail_close_once = True
    with storage.get_conn() as conn:   # 交易员减回 1.0 → 目标 1.0（净量 = 2.0−1.0）
        conn.execute(
            "UPDATE trade_positions SET quantity=2.0, closed_qty=1.0, "
            "updated_at=CURRENT_TIMESTAMP WHERE token='LDO' "
            "AND strategy='alphafox' AND status IN ('OPEN','PARTIAL')")
    afl.apply_signal(sig("REDUCE", "LDO"), True, 0.0, 3.0)
    lv_after = live_rows_of("LDO")[0]
    check("减仓失败：交易所仍 2.0", abs(FAKE.pos_qty("LDO") - 2.0) < 1e-9,
          str(FAKE.pos_qty("LDO")))
    check("减仓失败：记录数量不变",
          abs(float(lv_after["quantity"]) - float(lv_before["quantity"])) < 1e-12,
          f'{lv_before["quantity"]} -> {lv_after["quantity"]}')
    afl.apply_signal(sig("REDUCE", "LDO"), True, 0.0, 3.0)
    check("重试后减到目标 1.0",
          abs(FAKE.pos_qty("LDO") - 1.0) < 1e-9, str(FAKE.pos_qty("LDO")))

    print("== 31. 手动平仓：行情不可用 → 用纸盘价兜底（不卡死） ==")
    insert_paper("ARB", qty=1.0, entry=100.0)
    PRICE["ARB"] = 100.0
    afl.apply_signal(sig("OPEN", "ARB"), True, 0.0, 3.0)
    PRICE["ARB"] = None  # 行情不可用
    ok = afl.close_live("ARB")
    check("兜底价可用：平仓成功", ok is True, str(ok))
    check("交易所已平", FAKE.pos_qty("ARB") == 0.0, str(FAKE.pos_qty("ARB")))

    print("== 32. 手动平仓解除豁免键（防永久挡住重开） ==")
    _gpid = insert_paper("GMX", qty=1.0, entry=100.0)
    PRICE["GMX"] = 100.0
    with storage.get_conn() as conn:
        storage.trading_settings_update(
            conn, {"alphafox_live_exempt_paper_ids":
                   json.dumps([f"GMX|LONG#{_gpid}"])})
    afl._note_skip("GMX", "LONG", "补开跳过：实盘开关关闭")    # 平仓前先留个原因
    afl.close_live("GMX")
    with storage.get_conn() as conn:
        row = conn.execute("SELECT value FROM trading_settings "
                           "WHERE key='alphafox_live_exempt_paper_ids'").fetchone()
    val = json.loads(row["value"]) if row and row["value"] else []
    check("豁免键已解除", not any(str(v).startswith("GMX|LONG#") for v in val), str(val))
    check("实盘页手动平仓 → 「没跟上」的原因也清掉",
          afl.live_skip_reason("GMX", "LONG") == "",
          repr(afl.live_skip_reason("GMX", "LONG")))

    print("== 33. LEV 信号：杠杆写回 live 记录（防归属校验误判成非镜像仓） ==")
    insert_paper("LEV1", qty=1.0, entry=100.0)
    PRICE["LEV1"] = 100.0
    afl.apply_signal(sig("OPEN", "LEV1"), True, 0.0, 3.0)
    lr = live_rows_of("LEV1")[0]
    check("前置：记录杠杆 3", abs(float(lr["leverage"]) - 3) < 1e-9,
          str(lr["leverage"]))
    afl.apply_signal(sig("LEV", "LEV1"), True, 0.0, 8.0)
    lr = live_rows_of("LEV1")[0]
    # 2026-09-14 用户语义：名义/杠杆在开仓那一刻**冻结**，改设置只影响之后新开的仓
    # → LEV 信号只把"记录里的杠杆"再设一遍（交易所与记录始终一致，归属校验不误判）
    check("LEV 后：杠杆保持开仓时冻结的 3（改设置不动已有仓）",
          abs(float(lr["leverage"]) - 3) < 1e-9, str(lr["leverage"]))

    print("== 34. 存量仓闸门：早于本次进程启动的仓不镜像（防发版/重启补开旧仓） ==")
    insert_paper("OLD1", qty=1.0, entry=100.0)
    with storage.get_conn() as conn:   # 造"上次进程就在"的仓：created_at 回拨一小时
        conn.execute(
            "UPDATE trade_positions SET created_at=datetime('now','-3600 seconds') "
            "WHERE token='OLD1' AND strategy='alphafox'")
    PRICE["OLD1"] = 100.0
    afl._rounds_done = 99
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("存量仓：对账不补开", FAKE.pos_qty("OLD1") == 0.0, str(FAKE.pos_qty("OLD1")))
    check("存量仓：无 live 记录", len(live_rows_of("OLD1")) == 0,
          str(live_rows_of("OLD1")))
    with storage.get_conn() as conn:   # 交易员加仓（信号驱动）也不镜像
        conn.execute(
            "UPDATE trade_positions SET quantity=2.0, updated_at=CURRENT_TIMESTAMP "
            "WHERE token='OLD1' AND strategy='alphafox' "
            "AND status IN ('OPEN','PARTIAL')")
    afl.apply_signal(sig("ADD", "OLD1"), True, 0.0, 3.0)
    check("存量仓：ADD 信号不镜像", FAKE.pos_qty("OLD1") == 0.0,
          str(FAKE.pos_qty("OLD1")))
    insert_paper("NEW1", qty=1.0, entry=100.0)   # 启动后新开的仓 → 正常镜像
    PRICE["NEW1"] = 100.0
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("启动后新开仓：正常补开", FAKE.pos_qty("NEW1") > 0, str(FAKE.pos_qty("NEW1")))

    print("== 35. 存量仓 + 交易所已有我们的持仓 → 仍要补建记录（防孤儿仓无人对账） ==")
    insert_paper("ORP1", qty=1.0, entry=100.0)
    with storage.get_conn() as conn:   # 存量仓（早于启动）
        conn.execute(
            "UPDATE trade_positions SET created_at=datetime('now','-3600 seconds') "
            "WHERE token='ORP1' AND strategy='alphafox'")
    PRICE["ORP1"] = 100.0
    FAKE.positions.append({"symbol": "ORP1USDT", "quantity": 1.0,
                           "entry_price": 100.0, "mark_price": 100.0,
                           "unrealized_pnl": 0.0, "leverage": 3,
                           "position_side": "LONG"})   # 模拟 live 记录丢失后的残留
    afl._rounds_done = 99
    afl.sync_alphafox_live(True, 0.0, 3.0)
    lr = live_rows_of("ORP1")
    check("存量仓+交易所已有持仓：仍补建 live 记录",
          len(lr) == 1 and lr[0]["status"] == "OPEN", str(lr))
    check("不重复开仓（交易所仍 1.0）",
          abs(FAKE.pos_qty("ORP1") - 1.0) < 1e-9, str(FAKE.pos_qty("ORP1")))
    check("补建后互斥占用恢复", afl.alphafox_occupied("ORP1") is True)

    print("== 36. 存量仓判定：5 秒容差边界 ==")
    from datetime import timedelta as _td

    def _row(delta_s):
        return {"created_at": (afl._STARTED_AT + _td(seconds=delta_s))
                .strftime("%Y-%m-%d %H:%M:%S")}

    check("启动前 3 秒（容差内）→ 不算存量", afl._pre_startup(_row(-3)) is False)
    check("启动前 60 秒 → 算存量", afl._pre_startup(_row(-60)) is True)
    check("启动后创建 → 不算存量", afl._pre_startup(_row(60)) is False)

    print("== 37. 改设置不影响已有仓（比例 k 在开仓时冻结） ==")
    insert_paper("CUT1", qty=1.0, entry=100.0)
    PRICE["CUT1"] = 100.0
    afl._rounds_done = 99
    afl.apply_signal(sig("OPEN", "CUT1"), True, 0.0, 3.0)
    check("前置：实盘 1.0", abs(FAKE.pos_qty("CUT1") - 1.0) < 1e-9,
          str(FAKE.pos_qty("CUT1")))
    # 用户语义（2026-09-14 起，比例模型沿用）：改"实盘单笔上限"**不影响已有仓**
    # → 目标仍 = 交易员量 × 冻结 k，对账一个动作都不做
    _c0 = len(FAKE.calls)
    afl.sync_alphafox_live(True, 30.0, 3.0)
    check("改上限后：已有仓不动（冻结语义）",
          abs(FAKE.pos_qty("CUT1") - 1.0) < 1e-9, str(FAKE.pos_qty("CUT1")))
    check("改上限后：对账没动这个币（只算 CUT1 的动作）",
          not [c for c in FAKE.calls[_c0:] if "CUT1" in str(c)],
          str([c for c in FAKE.calls[_c0:] if "CUT1" in str(c)]))
    lr = live_rows_of("CUT1")[0]
    check("记录数量保持 1.0 且冻结比例 k 写进了记录",
          abs(float(lr["quantity"]) - 1.0) < 1e-9
          and abs(float((json.loads(lr["exchange_orders_json"] or "{}")
                         ).get("k") or 0) - 1.0) < 1e-12,
          f"{lr['quantity']} / {lr['exchange_orders_json']}")
    # 对照：混入手动仓 → 仍不越界。量取 2.5（远超记录量 1.0 × 1.25）→ 判"不像镜像仓"
    for p in FAKE.positions:
        if p["symbol"] == "CUT1USDT":
            p["quantity"] = 2.5
    afl.sync_alphafox_live(True, 30.0, 3.0)
    check("混入手动仓（2.5 > 1.0×1.25，不像镜像仓）：不越界减仓",
          abs(FAKE.pos_qty("CUT1") - 2.5) < 1e-9, str(FAKE.pos_qty("CUT1")))

    print("== 38. 加仓：杠杆与比例 k 都用开仓时冻结的值 ==")
    insert_paper("LEV2", qty=1.0, entry=100.0)
    PRICE["LEV2"] = 100.0
    afl._rounds_done = 99
    afl.apply_signal(sig("OPEN", "LEV2"), True, 0.0, 3.0)
    check("前置：记录杠杆 3",
          abs(float(live_rows_of("LEV2")[0]["leverage"]) - 3) < 1e-9,
          str(live_rows_of("LEV2")[0]["leverage"]))
    with storage.get_conn() as conn:   # 交易员加到 2.0 → 目标 2.0
        conn.execute("UPDATE trade_positions SET quantity=2.0 "
                     "WHERE token='LEV2' AND strategy='alphafox' "
                     "AND status IN ('OPEN','PARTIAL')")
    afl.apply_signal(sig("ADD", "LEV2"), True, 0.0, 8.0)   # 设置改 8x 后加仓
    lr = live_rows_of("LEV2")[0]
    check("加仓后：杠杆仍保持开仓时冻结的 3",
          abs(float(lr["leverage"]) - 3) < 1e-9, str(lr["leverage"]))
    check("加仓后：数量到新目标 2.0",
          abs(float(lr["quantity"]) - 2.0) < 1e-9, str(lr["quantity"]))
    n0 = FAKE.pos_qty("LEV2")
    afl.sync_alphafox_live(True, 0.0, 8.0)
    check("热改杠杆后对账仍能对齐（不被归属校验卡住）",
          abs(FAKE.pos_qty("LEV2") - 2.0) < 1e-9,
          f"{n0} -> {FAKE.pos_qty('LEV2')}")

    print("== 39. 残留 live 记录：对应纸盘仓已结束 → 先收敛，不接新仓的加仓语义 ==")
    insert_paper("STL1", qty=1.0, entry=100.0)
    PRICE["STL1"] = 100.0
    afl._rounds_done = 99
    afl.apply_signal(sig("OPEN", "STL1"), True, 0.0, 3.0)
    check("前置：实盘 1.0", abs(FAKE.pos_qty("STL1") - 1.0) < 1e-9,
          str(FAKE.pos_qty("STL1")))
    with storage.get_conn() as conn:   # 纸盘仓结束（模拟 CLOSE 信号丢失）
        conn.execute("UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
                     "WHERE token='STL1' AND strategy='alphafox' "
                     "AND status IN ('OPEN','PARTIAL')")
    insert_paper("STL1", qty=1.0, entry=100.0)   # 交易员重开同币同向 → 新纸盘仓
    afl.apply_signal(sig("OPEN", "STL1"), True, 0.0, 3.0)
    check("残留记录被收敛后新开一份（不是叠加成 2.0）",
          abs(FAKE.pos_qty("STL1") - 1.0) < 1e-9, str(FAKE.pos_qty("STL1")))
    lr = live_rows_of("STL1")[0]
    check("live 记录指向新仓（OPEN，冻结 k）",
          lr["status"] == "OPEN"
          and "k" in (lr["exchange_orders_json"] or ""),
          str((lr["status"], lr["exchange_orders_json"])))

    print("== 40. 取消跟单：仓位保留（纸盘+实盘都不平）→ 转非托管 ==")
    alphafox_trade.init_db()
    _p40 = insert_paper("UNF1", qty=1.0, entry=100.0)
    with storage.get_conn() as conn:   # 归属某交易员
        conn.execute("UPDATE trade_positions SET signal_snapshot=? "
                     "WHERE token='UNF1' AND strategy='alphafox'",
                     (json.dumps({"trader_id": "bn-9999", "k_qty": 0.01}),))
    PRICE["UNF1"] = 100.0
    alphafox_trade.set_follow("bn-9999", "测试跟单员", "", True)
    afl.apply_signal(sig("OPEN", "UNF1"), True, 0.0, 3.0)
    check("前置：实盘已镜像", FAKE.pos_qty("UNF1") > 0, str(FAKE.pos_qty("UNF1")))
    res = alphafox_trade.set_follow("bn-9999", "测试跟单员", "", False)
    check("取消跟单：实盘仓保留（不平）", FAKE.pos_qty("UNF1") > 0,
          str(FAKE.pos_qty("UNF1")))
    with storage.get_conn() as conn:
        row = conn.execute("SELECT status, managed FROM trade_positions "
                           "WHERE id=?", (_p40,)).fetchone()
    check("取消跟单：纸盘仓仍 OPEN 且转非托管",
          row and row["status"] == "OPEN" and int(row["managed"]) == 0,
          str(dict(row)))
    check("取消跟单：返回取消托管笔数", res.get("managed_off") == 1, str(res))

    print("== 41. 豁免按纸盘仓 id 记账：陈旧键不误伤重开的新仓 + 自动剪枝 ==")
    # 造一笔"已平仓的存量仓"的陈旧豁免键（旧 id），并让交易员重开同币同向
    _old_pid = insert_paper("EXH1", qty=1.0, entry=100.0)
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
                     "WHERE id=?", (_old_pid,))
        storage.trading_settings_update(
            conn, {"alphafox_live_exempt_paper_ids":
                   json.dumps([f"EXH1|LONG#{_old_pid}"])})
    PRICE["EXH1"] = 100.0
    afl._rounds_done = 99
    insert_paper("EXH1", qty=1.0, entry=100.0)     # 新仓（新 id）
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("陈旧豁免键不误伤重开的新仓（正常镜像）",
          abs(FAKE.pos_qty("EXH1") - 1.0) < 1e-9, str(FAKE.pos_qty("EXH1")))
    with storage.get_conn() as conn:
        row = conn.execute("SELECT value FROM trading_settings "
                           "WHERE key='alphafox_live_exempt_paper_ids'").fetchone()
    val = json.loads(row["value"]) if row and row["value"] else []
    check("陈旧键被对账剪枝", not any(str(v).startswith("EXH1|LONG#") for v in val),
          str(val))
    # 旧格式（不带 id）的键同样不误伤，且会被剪掉
    with storage.get_conn() as conn:
        storage.trading_settings_update(
            conn, {"alphafox_live_exempt_paper_ids": json.dumps(["EXH1|LONG"])})
    afl.sync_alphafox_live(True, 0.0, 3.0)
    with storage.get_conn() as conn:
        row = conn.execute("SELECT value FROM trading_settings "
                           "WHERE key='alphafox_live_exempt_paper_ids'").fetchone()
    val = json.loads(row["value"]) if row and row["value"] else []
    check("旧格式键被剪枝（不会永久挡住）", "EXH1|LONG" not in val, str(val))

    print("== 42. 取消跟单只清该交易员：不误伤其他交易员的仓 ==")
    alphafox_trade.init_db()
    for t, tid in (("ISO1", "bn-A"), ("ISO2", "bn-B")):
        insert_paper(t, qty=1.0, entry=100.0)
        with storage.get_conn() as conn:
            conn.execute("UPDATE trade_positions SET signal_snapshot=? "
                         "WHERE token=? AND strategy='alphafox'",
                         (json.dumps({"trader_id": tid, "k_qty": 0.01}), t))
        PRICE[t] = 100.0
        afl.apply_signal(sig("OPEN", t), True, 0.0, 3.0)
    check("前置：两币均已镜像",
          FAKE.pos_qty("ISO1") > 0 and FAKE.pos_qty("ISO2") > 0,
          f"ISO1={FAKE.pos_qty('ISO1')} ISO2={FAKE.pos_qty('ISO2')}")
    alphafox_trade.set_follow("bn-A", "A", "", False)
    check("取消 A：A 的实盘仓保留（新语义不平仓）", FAKE.pos_qty("ISO1") > 0,
          str(FAKE.pos_qty("ISO1")))
    check("取消 A：B 的实盘不受影响", FAKE.pos_qty("ISO2") > 0,
          str(FAKE.pos_qty("ISO2")))
    with storage.get_conn() as conn:
        r1 = conn.execute("SELECT status, managed FROM trade_positions "
                          "WHERE token='ISO1' AND strategy='alphafox' "
                          "ORDER BY id DESC LIMIT 1").fetchone()
        r2 = conn.execute("SELECT status, managed FROM trade_positions "
                          "WHERE token='ISO2' AND strategy='alphafox' "
                          "ORDER BY id DESC LIMIT 1").fetchone()
    check("取消 A：A 的仓仍 OPEN 且转非托管",
          r1 and r1["status"] == "OPEN" and int(r1["managed"]) == 0, str(dict(r1)))
    check("取消 A：B 的纸盘仓仍 OPEN 且仍托管",
          r2 and r2["status"] == "OPEN" and int(r2["managed"]) == 1, str(dict(r2)))

    print("== 43. 残留收敛失败 → 本轮不再开仓（防实盘双倍敞口） ==")
    alphafox_trade.init_db()
    _rp = insert_paper("RES1", qty=1.0, entry=100.0)
    PRICE["RES1"] = 100.0
    afl.apply_signal(sig("OPEN", "RES1"), True, 0.0, 3.0)
    check("前置：实盘一份", abs(FAKE.pos_qty("RES1") - 1.0) < 1e-9,
          str(FAKE.pos_qty("RES1")))
    with storage.get_conn() as conn:   # 纸盘仓结束（模拟 CLOSE 信号丢失）
        conn.execute("UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
                     "WHERE id=?", (_rp,))
    insert_paper("RES1", qty=1.0, entry=100.0)   # 交易员重开同币同向 → 新纸盘仓
    FAKE.fail_close_once = True                  # 收敛平仓被拒（返回 None）
    afl.apply_signal(sig("OPEN", "RES1"), True, 0.0, 3.0)
    check("收敛失败：不开新仓（实盘仍一份，未翻倍）",
          abs(FAKE.pos_qty("RES1") - 1.0) < 1e-9, str(FAKE.pos_qty("RES1")))
    _lr = live_rows_of("RES1")[0]
    check("收敛失败：残留记录仍 OPEN（下轮重试）", _lr["status"] == "OPEN",
          str(_lr["status"]))
    afl.apply_signal(sig("OPEN", "RES1"), True, 0.0, 3.0)   # 下轮重试成功
    check("下轮收敛成功后正常开一份",
          abs(FAKE.pos_qty("RES1") - 1.0) < 1e-9, str(FAKE.pos_qty("RES1")))

    print("== 44. 非托管仓：交易所查询不可用 → 不凭猜测收尾（不误平） ==")
    alphafox_trade.init_db()
    _p44 = insert_paper("FAIL1", qty=1.0, entry=100.0)
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET signal_snapshot=? WHERE id=?",
                     (json.dumps({"trader_id": "bn-F", "k_qty": 0.01}), _p44))
    PRICE["FAIL1"] = 100.0
    afl.apply_signal(sig("OPEN", "FAIL1"), True, 0.0, 3.0)
    _q44 = FAKE.pos_qty("FAIL1")
    check("前置：实盘已镜像", _q44 > 0, str(_q44))
    with storage.get_conn() as conn:      # 随后取消托管（镜像已建）
        conn.execute("UPDATE trade_positions SET managed=0 WHERE id=?", (_p44,))
    FAKE.fail_positions = True          # 交易所持仓查询失败
    try:
        afl.sync_alphafox_live(True, 0.0, 3.0)
    finally:
        FAKE.fail_positions = False
    check("查询失败：实盘仓不动", FAKE.pos_qty("FAIL1") == _q44,
          str(FAKE.pos_qty("FAIL1")))
    with storage.get_conn() as conn:
        row = conn.execute("SELECT status FROM trade_positions WHERE id=?",
                           (_p44,)).fetchone()
    check("查询失败：纸盘仓仍 OPEN（不误收尾）", row and row["status"] == "OPEN",
          str(dict(row)))
    check("查询失败：实盘记录仍 OPEN",
          live_rows_of("FAIL1")[0]["status"] == "OPEN",
          str(live_rows_of("FAIL1")[0]["status"]))

    print("== 45. 平仓单零成交 → 不许标已平（假平仓=裸仓） ==")
    alphafox_trade.init_db()
    insert_paper("ZERO1", qty=1.0, entry=100.0)
    PRICE["ZERO1"] = 100.0
    afl.apply_signal(sig("OPEN", "ZERO1"), True, 0.0, 3.0)
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
                     "WHERE token='ZERO1' AND strategy='alphafox'")
    afl._rounds_done = 99
    FAKE.zero_fill_once = True
    afl.sync_alphafox_live(True, 0.0, 3.0)   # 纸盘无活跃仓 → 收敛
    _lr = live_rows_of("ZERO1")[0]
    check("零成交：live 记录仍 OPEN（下轮重试）", _lr["status"] == "OPEN",
          str(_lr["status"]))
    check("零成交：交易所持仓不动", FAKE.pos_qty("ZERO1") > 0,
          str(FAKE.pos_qty("ZERO1")))
    afl.sync_alphafox_live(True, 0.0, 3.0)   # 下轮：正常成交 → 收敛
    check("下轮收敛成功：live CLOSED", live_rows_of("ZERO1")[0]["status"] == "CLOSED",
          str(live_rows_of("ZERO1")[0]["status"]))

    print("== 46. 对账认错记录：残留行改指当前纸盘仓（不是拿错对象算份数） ==")
    alphafox_trade.init_db()
    _p1 = insert_paper("REP1", qty=1.0, entry=100.0)
    PRICE["REP1"] = 100.0
    afl.apply_signal(sig("OPEN", "REP1"), True, 0.0, 3.0)   # 记录挂 #_p1
    with storage.get_conn() as conn:      # 交易员平掉又重开同币同向 → 新纸盘仓
        conn.execute("UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
                     "WHERE id=?", (_p1,))
    _p2 = insert_paper("REP1", qty=1.0, entry=100.0)
    afl._rounds_done = 99
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("残留行已改指当前纸盘仓",
          live_rows_of("REP1")[0]["paper_position_id"] == _p2,
          f"live 指向 {live_rows_of('REP1')[0]['paper_position_id']}，应为 {_p2}")
    check("实盘数量收敛到一份（未被误加倍/减半）",
          abs(FAKE.pos_qty("REP1") - 1.0) < 1e-9, str(FAKE.pos_qty("REP1")))

    print("== 47. 对账补仓：交易所量多于记录 → 不加（防喂大混进来的手动仓） ==")
    alphafox_trade.init_db()
    insert_paper("ADDG", qty=1.0, entry=100.0)
    PRICE["ADDG"] = 100.0
    afl.apply_signal(sig("OPEN", "ADDG"), True, 0.0, 3.0)
    with storage.get_conn() as conn:   # 交易员加到 4.0 → 目标 4.0
        conn.execute("UPDATE trade_positions SET quantity=4.0 "
                     "WHERE token='ADDG' AND strategy='alphafox' "
                     "AND status IN ('OPEN','PARTIAL')")
    afl.apply_signal(sig("ADD", "ADDG"), True, 0.0, 3.0)
    check("前置：实盘 4.0（交易员 4.0 × k=1）",
          abs(FAKE.pos_qty("ADDG") - 4.0) < 1e-9, str(FAKE.pos_qty("ADDG")))
    # 造"记录过期偏小 + 交易所混进手动仓"：记录 1.0、交易所 3.0、目标 4.0
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        lc.execute("UPDATE live_positions SET quantity=1.0 WHERE token='ADDG'")
    for _p in FAKE.positions:
        if _p["symbol"] == "ADDGUSDT":
            _p["quantity"] = 3.0
    afl._rounds_done = 99
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("补仓跳过：没加到混有手动仓的仓上",
          abs(FAKE.pos_qty("ADDG") - 3.0) < 1e-9, str(FAKE.pos_qty("ADDG")))
    # 正向对照：记录补回真实量（健康状态）→ 照常补到目标 4.0
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        lc.execute("UPDATE live_positions SET quantity=3.0 WHERE token='ADDG'")
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("健康状态下补仓正常执行（闸门没有过宽）",
          abs(FAKE.pos_qty("ADDG") - 4.0) < 1e-9, str(FAKE.pos_qty("ADDG")))

    print("== 48. 价格怎么动都不调仓 + 容忍带吞掉 1 个 step 的偏差 + 超带才修 ==")
    alphafox_trade.init_db()
    FAKE.step, FAKE.min_qty = 0.01, 0.01        # 模拟 LOT_SIZE：1 step = 0.01
    insert_paper("BAND1", qty=0.9, entry=100.0)
    PRICE["BAND1"] = 100.0
    afl._rounds_done = 99
    afl.apply_signal(sig("OPEN", "BAND1"), True, 0.0, 5.0)   # 交易员 0.9 → 目标 0.9
    check("前置：实盘 0.9（交易员 0.9 × k=1）",
          abs(FAKE.pos_qty("BAND1") - 0.9) < 1e-9,
          str(live_rows_of("BAND1")[0]["exchange_orders_json"]))

    def _closes(tok):
        return len([c for c in FAKE.calls if c[0] == "close" and c[1] == tok])

    # ① 现价 +1% / +10% / +30% 全程不动：目标 = 交易员量 × 冻结 k，与现价无关
    #   （旧"每份名义"口径在 +10% 就会减仓 —— 那正是反复补仓的来源）
    _c0 = _closes("BAND1")
    for _mk in (101.0, 110.0, 130.0):
        for _p in FAKE.positions:
            if _p["symbol"] == "BAND1USDT":
                _p["mark_price"] = _mk
        afl.sync_alphafox_live(True, 0.0, 5.0)
    check("价格 +1%/+10%/+30% 全程不调仓（与现价无关）",
          _closes("BAND1") == _c0 and abs(FAKE.pos_qty("BAND1") - 0.9) < 1e-9,
          f"closes={_closes('BAND1') - _c0} pos={FAKE.pos_qty('BAND1')}")
    _b1 = live_rows_of("BAND1")
    check("价格怎么动，记录都对齐真实持仓 0.9",
          bool(_b1) and abs(float(_b1[0].get("quantity") or 0) - 0.9) < 1e-9,
          str(_b1[:1]))
    # ② 交易所比目标少 1 个 step（部分成交/手动蹭掉）→ 容忍带内，不补
    next(p for p in FAKE.positions
         if p["symbol"] == "BAND1USDT")["quantity"] = 0.89
    _o0 = len([c for c in FAKE.calls if c[0] == "open" and c[2] == "BAND1"])
    afl.sync_alphafox_live(True, 0.0, 5.0)
    check("差 1 个 step：不补仓（容忍带下限 1.5×step 吞掉取整噪声）",
          len([c for c in FAKE.calls if c[0] == "open" and c[2] == "BAND1"]) == _o0
          and abs(FAKE.pos_qty("BAND1") - 0.89) < 1e-9, str(FAKE.pos_qty("BAND1")))
    # ③ 差 10 个 step（0.09 ≫ 带 0.075 = 5%×目标）→ 补回目标，并写进 live_trade_log
    next(p for p in FAKE.positions
         if p["symbol"] == "BAND1USDT")["quantity"] = 0.8
    afl.sync_alphafox_live(True, 0.0, 5.0)
    check("差 10 个 step：补回目标 0.9（对账本职）",
          abs(FAKE.pos_qty("BAND1") - 0.9) < 1e-9, str(FAKE.pos_qty("BAND1")))
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        _lg = [dict(r) for r in lc.execute(
            "SELECT * FROM live_trade_log WHERE token='BAND1' ORDER BY id DESC LIMIT 1")]
    check("对账成交写进 live_trade_log（action=rebalance_add）",
          _lg and _lg[0]["action"] == "rebalance_add", str(_lg))
    # ④ 目标量小到 5% 容忍带比 1 个 step 还小 → **必须**靠 step 下限兜住：
    #   目标 0.1、step 0.01 → 5% 带只有 0.005 < 1 step；差 1.2 step 时若没有下限
    #   就会把取整噪声当真偏离去成交（来回抖动白付手续费）
    PRICE["BND2"] = 100.0
    set_bal(1000.0)
    insert_paper("BND2", qty=10.0, entry=100.0, k_qty=1.0, trader_margin=100000.0)
    afl.apply_signal(sig("OPEN", "BND2", trader_margin=100000.0), True, 0.0, 5.0)
    check("前置：目标 0.1（5% 带 0.005 < 1 step 0.01）",
          abs(FAKE.pos_qty("BND2") - 0.1) < 1e-9, str(FAKE.pos_qty("BND2")))
    next(p for p in FAKE.positions
         if p["symbol"] == "BND2USDT")["quantity"] = 0.088     # 差 1.2 step
    _o2 = len([c for c in FAKE.calls if c[0] == "open" and c[2] == "BND2"])
    afl.sync_alphafox_live(True, 0.0, 5.0)
    check("差 1.2 个 step 被 step 下限吞掉（不带下限就会真成交）",
          len([c for c in FAKE.calls if c[0] == "open" and c[2] == "BND2"]) == _o2
          and abs(FAKE.pos_qty("BND2") - 0.088) < 1e-9, str(FAKE.pos_qty("BND2")))
    set_bal(BAL)
    FAKE.step, FAKE.min_qty = None, 0.0

    print("== 49. 跟单加/减仓也记账（原来实盘成交但查不到出处） ==")
    alphafox_trade.init_db()
    insert_paper("LOGG1", qty=1.0, entry=100.0)
    PRICE["LOGG1"] = 100.0
    afl._rounds_done = 99
    afl.apply_signal(sig("OPEN", "LOGG1"), True, 0.0, 5.0)
    with storage.get_conn() as conn:   # 交易员 1.0 → 2.0
        conn.execute("UPDATE trade_positions SET quantity=2.0 "
                     "WHERE token='LOGG1' AND strategy='alphafox' "
                     "AND status IN ('OPEN','PARTIAL')")
    afl.apply_signal(sig("ADD", "LOGG1"), True, 0.0, 5.0)
    with storage.get_conn() as conn:   # 交易员 2.0 → 1.0（净量 = 2.0−1.0）
        conn.execute("UPDATE trade_positions SET quantity=2.0, closed_qty=1.0 "
                     "WHERE token='LOGG1' AND strategy='alphafox' "
                     "AND status IN ('OPEN','PARTIAL')")
    afl.apply_signal(sig("REDUCE", "LOGG1"), True, 0.0, 5.0)
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        _acts = [r["action"] for r in lc.execute(
            "SELECT action FROM live_trade_log WHERE token='LOGG1' ORDER BY id")]
    check("加仓记为 add、减仓记为 reduce",
          "add" in _acts and "reduce" in _acts, str(_acts))

    print("== 50. 容忍带按目标**比例**（5%）：带内吞掉、超带必执行 ==")
    alphafox_trade.init_db()
    insert_paper("BIGL", qty=30.0, entry=100.0)
    PRICE["BIGL"] = 100.0
    afl._rounds_done = 99
    afl.apply_signal(sig("OPEN", "BIGL"), True, 0.0, 5.0)   # 交易员 30 → 目标 30
    check("前置：实盘 30.0", abs(FAKE.pos_qty("BIGL") - 30.0) < 1e-9,
          str(FAKE.pos_qty("BIGL")))
    # ① 差 1.0 = 目标的 3.3% < 5% 带 → 吞掉（不值得为它付手续费）
    for _p in FAKE.positions:
        if _p["symbol"] == "BIGLUSDT":
            _p["quantity"] = 29.0
    _c = len([c for c in FAKE.calls if c[0] == "close" and c[1] == "BIGL"])
    afl.sync_alphafox_live(True, 0.0, 5.0)
    check("目标 30 时差 1.0（3.3% < 5% 带）：不动（记录对齐事实）",
          len([c for c in FAKE.calls if c[0] == "close" and c[1] == "BIGL"]) == _c
          and abs(float(live_rows_of("BIGL")[0]["quantity"]) - 29.0) < 1e-9,
          f"{FAKE.pos_qty('BIGL')} / {live_rows_of('BIGL')[0]['quantity']}")
    # ② 差 3.0 = 10% > 5% 带 → 必须补回目标（带子不是"越大越钝"）
    for _p in FAKE.positions:
        if _p["symbol"] == "BIGLUSDT":
            _p["quantity"] = 27.0
    afl.sync_alphafox_live(True, 0.0, 5.0)
    check("差 3.0（10% > 5% 带）：补回目标 30.0",
          abs(FAKE.pos_qty("BIGL") - 30.0) < 1e-9, str(FAKE.pos_qty("BIGL")))

    print("== 51. 非托管仓：不下单、事实同步；交易所空了 → 记录+纸盘一起收尾 ==")
    alphafox_trade.init_db()
    PRICE["UNM1"] = 100.0
    _p51 = insert_paper("UNM1", qty=1.0, entry=100.0)
    afl.apply_signal(sig("OPEN", "UNM1"), True, 0.0, 3.0)
    check("前置：实盘已镜像", FAKE.pos_qty("UNM1") > 0, str(FAKE.pos_qty("UNM1")))
    with storage.get_conn() as conn:   # 取消托管 + 纸盘数量被改（托管中会对齐）
        conn.execute("UPDATE trade_positions SET managed=0, quantity=2.0, "
                     "current_price=100.0, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                     (_p51,))
    _calls0 = len(calls_for("UNM1"))
    _q51 = FAKE.pos_qty("UNM1")
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("非托管：不做数量对齐（交易所数量未变）",
          abs(FAKE.pos_qty("UNM1") - _q51) < 1e-9, str(FAKE.pos_qty("UNM1")))
    check("非托管：不下任何单", len(calls_for("UNM1")) == _calls0,
          str(calls_for("UNM1")[_calls0:]))
    for _p in FAKE.positions:          # 你在币安手动加仓 → 记录按事实同步
        if _p["symbol"] == "UNM1USDT":
            _p["quantity"] = 1.5
    _calls0 = len(calls_for("UNM1"))
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("非托管：交易所手动加仓 → 记录事实同步为 1.5",
          abs(float(live_rows_of("UNM1")[0]["quantity"]) - 1.5) < 1e-9,
          str(live_rows_of("UNM1")[0]["quantity"]))
    check("非托管：事实同步不下单", len(calls_for("UNM1")) == _calls0,
          str(calls_for("UNM1")[_calls0:]))
    check("非托管：实盘记录仍 OPEN（不被误收尾）",
          live_rows_of("UNM1")[0]["status"] == "OPEN",
          str(live_rows_of("UNM1")[0]["status"]))
    # 你在币安手动平掉 → 记录 + 纸盘一起收尾（决策 2）
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        lc.execute("UPDATE live_positions SET "
                   "created_at=datetime('now','-200 seconds') WHERE token='UNM1'")
    FAKE.positions = [p for p in FAKE.positions if p["symbol"] != "UNM1USDT"]
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("交易所空了：实盘记录收尾 CLOSED",
          live_rows_of("UNM1")[0]["status"] == "CLOSED",
          str(live_rows_of("UNM1")[0]["status"]))
    with storage.get_conn() as conn:
        _row51 = dict(conn.execute("SELECT * FROM trade_positions WHERE id=?",
                                   (_p51,)).fetchone())
    check("交易所空了：纸盘仓一起标平（页面不留幽灵）",
          _row51["status"] == "CLOSED", str(_row51["status"]))
    check("交易所空了：advice 标注手动平仓",
          "已取消托管" in (_row51.get("advice") or ""), str(_row51.get("advice")))

    print("== 52. 恢复托管：解豁免 + 比例 k 按现状冻结（零下单，绝不按纸盘量重算） ==")
    alphafox_trade.init_db()
    PRICE["UNM2"] = 100.0
    _p52 = insert_paper("UNM2", qty=1.0, entry=100.0, margin=50, lev=2)
    afl.apply_signal(sig("OPEN", "UNM2"), True, 0.0, 3.0)
    check("前置：实盘 1.0、冻结 k=1",
          abs(FAKE.pos_qty("UNM2") - 1.0) < 1e-9
          and abs(float(json.loads(live_rows_of("UNM2")[0]["exchange_orders_json"]
                                   or "{}").get("k") or 0) - 1.0) < 1e-12,
          str(live_rows_of("UNM2")[0]["exchange_orders_json"]))
    with storage.get_conn() as conn:   # 未托管期间交易员加到 3.0（纸盘量 3.0）
        conn.execute("UPDATE trade_positions SET quantity=3.0, current_price=100.0, "
                     "managed=1, updated_at=CURRENT_TIMESTAMP WHERE id=?", (_p52,))
    afl._exempt_write({f"UNM2|LONG#{_p52}"})
    check("前置：豁免键存在", afl._exempt_has("UNM2", "LONG"))
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("豁免未清时对账不动交易所（这就是要解豁免的原因）",
          abs(FAKE.pos_qty("UNM2") - 1.0) < 1e-9, str(FAKE.pos_qty("UNM2")))
    _c52 = len(calls_for("UNM2"))
    afl.on_reattach("UNM2", "LONG")
    check("恢复托管：解除豁免", not afl._exempt_has("UNM2", "LONG"))
    check("恢复托管：k 按现状冻结 = 交易所量 ÷ 交易员量 = 1/3（不是开仓时的 1）",
          abs(float(json.loads(live_rows_of("UNM2")[0]["exchange_orders_json"]
                               or "{}").get("k") or 0) - 1 / 3) < 1e-9,
          str(live_rows_of("UNM2")[0]["exchange_orders_json"]))
    check("恢复托管：不下单", len(calls_for("UNM2")) == _c52,
          str(calls_for("UNM2")[_c52:]))
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("恢复托管后对账：实盘数量保持 1.0（目标==现状，零下单）",
          abs(FAKE.pos_qty("UNM2") - 1.0) < 1e-9, str(FAKE.pos_qty("UNM2")))
    check("恢复托管后：记录量同步到交易所事实 1.0",
          abs(float(live_rows_of("UNM2")[0]["quantity"]) - 1.0) < 1e-9,
          str(live_rows_of("UNM2")[0]["quantity"]))

    print("== 53. 恢复中(2)的仓：交易所空了也不许当'手动平仓'收尾 ==")
    alphafox_trade.init_db()
    PRICE["UNM3"] = 100.0
    _p53 = insert_paper("UNM3", qty=1.0, entry=100.0)
    afl.apply_signal(sig("OPEN", "UNM3"), True, 0.0, 3.0)
    FAKE.positions = [p for p in FAKE.positions if p["symbol"] != "UNM3USDT"]
    with storage.get_conn() as conn:      # 用户点了"恢复托管"（2），下一轮追平前
        conn.execute("UPDATE trade_positions SET managed=2, current_price=100.0 "
                     "WHERE id=?", (_p53,))
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        lc.execute("UPDATE live_positions SET created_at=datetime('now','-200 seconds')"
                   " WHERE token='UNM3'")
    afl.sync_alphafox_live(True, 0.0, 3.0)
    with storage.get_conn() as conn:
        _r53 = dict(conn.execute("SELECT status, managed FROM trade_positions "
                                 "WHERE id=?", (_p53,)).fetchone())
    check("恢复中：纸盘仓不被收尾（恢复动作不被静默回滚）",
          _r53["status"] == "OPEN" and int(_r53["managed"]) == 2, str(_r53))
    check("恢复中：实盘记录仍 OPEN",
          live_rows_of("UNM3")[0]["status"] == "OPEN",
          str(live_rows_of("UNM3")[0]["status"]))

    print("== 54. 未托管仓走页面平仓：照平（安全出口必须一直可用） ==")
    alphafox_trade.init_db()
    PRICE["UNM4"] = 100.0
    _p54 = insert_paper("UNM4", qty=1.0, entry=100.0)
    afl.apply_signal(sig("OPEN", "UNM4"), True, 0.0, 3.0)
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET managed=0, current_price=100.0 "
                     "WHERE id=?", (_p54,))
    _ok54 = afl.close_live("UNM4")
    check("未托管仓可手动平仓", _ok54, str(_ok54))
    with storage.get_conn() as conn:
        _r54 = dict(conn.execute("SELECT status, advice FROM trade_positions "
                                 "WHERE id=?", (_p54,)).fetchone())
    check("未托管仓平仓：纸盘已平", _r54["status"] == "CLOSED", str(_r54))
    check("未托管仓平仓：实盘已平", FAKE.pos_qty("UNM4") == 0.0,
          str(FAKE.pos_qty("UNM4")))
    check("未托管仓平仓：实盘记录 CLOSED",
          live_rows_of("UNM4")[0]["status"] == "CLOSED",
          str(live_rows_of("UNM4")[0]["status"]))

    print("== 55. 恢复托管：k 按实盘现状冻结（不抹掉用户手动加减） ==")
    alphafox_trade.init_db()
    PRICE["UNM5"] = 100.0
    _p55 = insert_paper("UNM5", qty=1.0, entry=100.0)
    afl.apply_signal(sig("OPEN", "UNM5"), True, 0.0, 3.0)
    with storage.get_conn() as conn:   # 取消托管
        conn.execute("UPDATE trade_positions SET managed=0, current_price=100.0 "
                     "WHERE id=?", (_p55,))
    for _p in FAKE.positions:          # 你在币安手动加到 3.0
        if _p["symbol"] == "UNM5USDT":
            _p["quantity"] = 3.0
    afl.sync_alphafox_live(True, 0.0, 3.0)      # 事实同步 → 记录 3.0
    check("前置：记录已事实同步为 3.0",
          abs(float(live_rows_of("UNM5")[0]["quantity"]) - 3.0) < 1e-9,
          str(live_rows_of("UNM5")[0]["quantity"]))
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET managed=1 WHERE id=?", (_p55,))
    afl.on_reattach("UNM5", "LONG")
    check("恢复托管：k 按现状冻结 = 3.0 ÷ 交易员 1.0 = 3.0",
          abs(float(json.loads(live_rows_of("UNM5")[0]["exchange_orders_json"]
                               or "{}").get("k") or 0) - 3.0) < 1e-9,
          str(live_rows_of("UNM5")[0]["exchange_orders_json"]))
    _c55 = len(calls_for("UNM5"))
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("恢复托管后对账：不下单（目标≈当前数量）",
          len(calls_for("UNM5")) == _c55, str(calls_for("UNM5")[_c55:]))
    check("恢复托管后对账：实盘数量仍 3.0（用户手动加的不被抹掉）",
          abs(FAKE.pos_qty("UNM5") - 3.0) < 1e-9, str(FAKE.pos_qty("UNM5")))
    # 记录还没被事实同步刷新时（同一轮询窗口里手动改仓 + 点恢复）→ 必须按交易所现状
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        lc.execute("UPDATE live_positions SET quantity=1.0, "
                   "exchange_orders_json=? WHERE token='UNM5'",
                   (json.dumps({"model": "ratio", "k": 1.0}),))
    afl.on_reattach("UNM5", "LONG")
    check("55 记录未同步时：k 按交易所现状（3.0）而非旧记录（1.0）",
          abs(float(json.loads(live_rows_of("UNM5")[0]["exchange_orders_json"]
                               or "{}").get("k") or 0) - 3.0) < 1e-9,
          str(live_rows_of("UNM5")[0]["exchange_orders_json"]))
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("55 记录未同步时：对账也不抹掉手动加的量（仍 3.0）",
          abs(FAKE.pos_qty("UNM5") - 3.0) < 1e-9, str(FAKE.pos_qty("UNM5")))

    print("== 56. 取消托管后的在飞信号：实盘不下单（与纸盘守卫对称） ==")
    alphafox_trade.init_db()
    PRICE["UNM6"] = 100.0
    _p56 = insert_paper("UNM6", qty=1.0, entry=100.0)
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET managed=0 WHERE id=?", (_p56,))
    _c56 = len(calls_for("UNM6"))
    afl.apply_signal(sig("OPEN", "UNM6"), True, 0.0, 3.0)
    check("未托管：executed 的信号也不镜像（不下单）",
          len(calls_for("UNM6")) == _c56 and FAKE.pos_qty("UNM6") == 0.0,
          str(calls_for("UNM6")[_c56:]))
    check("未托管：不产生 live 记录", not live_rows_of("UNM6"),
          str(live_rows_of("UNM6")))

    print("== 57. 减仓零成交/部分成交：按实际成交量记账（数量不漂移） ==")
    alphafox_trade.init_db()
    PRICE["UNM7"] = 100.0
    _p57 = insert_paper("UNM7", qty=2.0, entry=100.0)
    afl.apply_signal(sig("OPEN", "UNM7"), True, 0.0, 3.0)
    with storage.get_conn() as conn:   # 交易员加到 4.0
        conn.execute("UPDATE trade_positions SET quantity=4.0, "
                     "updated_at=CURRENT_TIMESTAMP WHERE id=?", (_p57,))
    afl.apply_signal(sig("ADD", "UNM7"), True, 0.0, 3.0)
    check("前置：实盘 4.0（交易员 4.0 × k=1）",
          abs(FAKE.pos_qty("UNM7") - 4.0) < 1e-9, str(FAKE.pos_qty("UNM7")))
    _q57 = float(live_rows_of("UNM7")[0]["quantity"])
    with storage.get_conn() as conn:   # 交易员减到 2.0 → 目标 2.0（净量 = 4.0−2.0）
        conn.execute("UPDATE trade_positions SET quantity=4.0, closed_qty=2.0, "
                     "updated_at=CURRENT_TIMESTAMP WHERE id=?", (_p57,))
    FAKE.zero_fill_once = True
    afl.apply_signal(sig("REDUCE", "UNM7"), True, 0.0, 3.0)
    _lr57 = live_rows_of("UNM7")[0]
    check("减仓零成交：记录数量不变（不按委托量记账）",
          abs(float(_lr57["quantity"]) - _q57) < 1e-9,
          str({k: _lr57[k] for k in ("quantity", "exchange_orders_json")}))
    check("减仓零成交：实盘仍持仓", FAKE.pos_qty("UNM7") > 0,
          str(FAKE.pos_qty("UNM7")))
    FAKE.partial_fill_once = 0.3
    afl.apply_signal(sig("REDUCE", "UNM7"), True, 0.0, 3.0)
    _lr57b = live_rows_of("UNM7")[0]
    check("减仓部分成交：按实际成交量减记账（4.0 − 0.3 = 3.7）",
          abs(float(_lr57b["quantity"]) - (_q57 - 0.3)) < 1e-9,
          str({k: _lr57b[k] for k in ("quantity", "exchange_orders_json")}))

    print("== 58. 补建记录（记录丢失、交易所有我们的仓）：比例按现状反推 ==")
    alphafox_trade.init_db()
    PRICE["ADPT1"] = 100.0
    insert_paper("ADPT1", qty=3.0, entry=100.0)      # 交易员 3.0
    FAKE.positions.append({"symbol": "ADPT1USDT", "quantity": 3.0,
                           "entry_price": 100.0, "mark_price": 100.0,
                           "unrealized_pnl": 0.0, "leverage": 5,
                           "position_side": "LONG"})
    afl._rounds_done = 99
    afl.sync_alphafox_live(True, 0.0, 5.0)
    _lr58 = live_rows_of("ADPT1")
    check("补建记录：比例按现状反推 k = 3.0 ÷ 交易员 3.0 = 1.0",
          bool(_lr58) and abs(float(json.loads(
              _lr58[0]["exchange_orders_json"] or "{}").get("k") or 0)
              - 1.0) < 1e-9,
          str(_lr58[0]["exchange_orders_json"] if _lr58 else None))
    check("补建记录：数量按交易所现状 3.0（不是写死）",
          bool(_lr58) and abs(float(_lr58[0]["quantity"]) - 3.0) < 1e-9,
          str(_lr58[0]["quantity"] if _lr58 else None))
    _c58 = len(calls_for("ADPT1"))
    afl.sync_alphafox_live(True, 0.0, 5.0)
    check("补建后下一轮：不砍仓（实盘仍 3.0、无下单）",
          abs(FAKE.pos_qty("ADPT1") - 3.0) < 1e-9
          and len(calls_for("ADPT1")) == _c58,
          str((FAKE.pos_qty("ADPT1"), calls_for("ADPT1")[_c58:])))

    print("== 59. live 行复用重开：上一段的已实现盈亏不许清零 ==")
    alphafox_trade.init_db()
    PRICE["ACC1"] = 100.0
    insert_paper("ACC1", qty=1.0, entry=100.0)
    afl.apply_signal(sig("OPEN", "ACC1"), True, 0.0, 3.0)
    for _p in FAKE.positions:                 # 手动改标记价 → 平仓有盈亏
        if _p["symbol"] == "ACC1USDT":
            _p["mark_price"] = 110.0
    PRICE["ACC1"] = 110.0
    afl.apply_signal(sig("CLOSE", "ACC1"), True, 0.0, 3.0)
    _r59 = float(live_rows_of("ACC1")[0]["realized_pnl"])
    check("前置：第一次平仓写了 realized", abs(_r59) > 0, str(_r59))
    afl.apply_signal(sig("ADD", "ACC1"), True, 0.0, 3.0)   # 交易员又加回来 → 复用行重开
    _lr59 = live_rows_of("ACC1")[0]
    check("复用重开：realized 不清零（上一段真实盈亏不从统计消失）",
          abs(float(_lr59["realized_pnl"]) - _r59) < 1e-9,
          str(_lr59["realized_pnl"]))

    print("== 60. 交易所持仓消失 → 收敛时按纸盘现价估算 realized ==")
    alphafox_trade.init_db()
    PRICE["VAN1"] = 100.0
    insert_paper("VAN1", qty=1.0, entry=100.0)
    afl.apply_signal(sig("OPEN", "VAN1"), True, 0.0, 3.0)
    with storage.get_conn(config.LIVE_DB_PATH) as lc:   # 老化记录（绕过 90s 保护）
        lc.execute("UPDATE live_positions SET created_at=datetime('now','-200 seconds')"
                   " WHERE token='VAN1'")
    FAKE.positions = [p for p in FAKE.positions if p["symbol"] != "VAN1USDT"]
    with storage.get_conn() as conn:         # 纸盘现价 110 → 多头估算 +10
        conn.execute("UPDATE trade_positions SET current_price=110.0 "
                     "WHERE token='VAN1' AND strategy='alphafox'")
    afl._rounds_done = 99
    afl.sync_alphafox_live(True, 0.0, 3.0)
    _lr60 = live_rows_of("VAN1")[0]
    check("60 收敛时写 realized 估算（不留 0 混进胜率/累计盈亏）",
          _lr60["status"] == "CLOSED" and float(_lr60["realized_pnl"]) > 0,
          str({k: _lr60[k] for k in ("status", "realized_pnl")}))

    print("== 61. 每币杠杆覆盖：只对新仓生效 + 老记录补写冻结 k ==")
    alphafox_trade.init_db()
    with storage.get_conn(config.ALPHAFOX_DB) as c:   # 比例模型：每币只剩杠杆
        c.execute("INSERT OR REPLACE INTO alphafox_token_settings "
                  "(token, live_notional, live_leverage, updated_at) "
                  "VALUES ('OVR1', NULL, 7.0, ?)", (int(time.time()),))
    check("覆盖生效（杠杆 7）",
          abs(afl._eff_live_params("OVR1", 3.0) - 7.0) < 1e-9,
          str(afl._eff_live_params("OVR1", 3.0)))
    check("没覆盖的币回退全局",
          abs(afl._eff_live_params("NOPE9", 3.0) - 3.0) < 1e-9,
          str(afl._eff_live_params("NOPE9", 3.0)))
    insert_paper("OVR1", qty=1.0, entry=100.0)
    PRICE["OVR1"] = 100.0
    afl._rounds_done = 99
    afl.apply_signal(sig("OPEN", "OVR1"), True, 0.0, 3.0)   # 传入的是全局杠杆 3
    lr = live_rows_of("OVR1")[0]
    check("新仓用每币覆盖的杠杆 7（不是全局 3）",
          abs(float(lr["leverage"]) - 7) < 1e-9, str(lr["leverage"]))
    check("规模仍按比例 k（交易员 1.0 × k=1 = 1.0）",
          abs(FAKE.pos_qty("OVR1") - 1.0) < 1e-9, str(FAKE.pos_qty("OVR1")))
    with storage.get_conn(config.ALPHAFOX_DB) as c:
        c.execute("UPDATE alphafox_token_settings SET live_leverage=20 "
                  "WHERE token='OVR1'")
    _c0 = len(FAKE.calls)
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("改覆盖后：已有仓不动、一个单都不下（杠杆冻结在开仓那一刻）",
          abs(FAKE.pos_qty("OVR1") - 1.0) < 1e-9
          and all(c[0] not in ("open", "close") for c in FAKE.calls[_c0:]),
          f"{FAKE.pos_qty('OVR1')} {FAKE.calls[_c0:]}")
    # 老记录（份数模型写的、没有 k）：按"记录量 ÷ 交易员量"补写一次 → 之后冻结
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        lc.execute("UPDATE live_positions SET exchange_orders_json='{\"model\": "
                   "\"lots\"}' WHERE token='OVR1'")
    afl.sync_alphafox_live(True, 0.0, 3.0)
    lr = live_rows_of("OVR1")[0]
    check("老记录补写冻结 k = 记录量 1.0 ÷ 交易员量 1.0 = 1.0",
          abs(float(json.loads(lr["exchange_orders_json"] or "{}").get("k") or 0)
              - 1.0) < 1e-9, lr["exchange_orders_json"])
    check("补写后目标 == 现状 → 不下单",
          abs(FAKE.pos_qty("OVR1") - 1.0) < 1e-9, str(FAKE.pos_qty("OVR1")))
    _c1 = len(FAKE.calls)
    afl.sync_alphafox_live(True, 0.0, 3.0)          # 再跑一轮：冻结后不再动
    check("冻结后再跑一轮：不下单",
          all(c[0] not in ("open", "close") for c in FAKE.calls[_c1:]),
          str(FAKE.calls[_c1:]))

    print("== 62. 交易所已无持仓（别人平的）→ 按纸盘现价估算 realized（不记 0） ==")
    insert_paper("EST1", qty=1.0, entry=100.0)
    PRICE["EST1"] = 100.0
    afl._rounds_done = 99
    afl.apply_signal(sig("OPEN", "EST1"), True, 0.0, 3.0)
    with storage.get_conn() as conn:      # 纸盘现价被 1s 循环刷到 110
        conn.execute("UPDATE trade_positions SET current_price=110.0 "
                     "WHERE token='EST1' AND strategy='alphafox'")
    _lv = live_rows_of("EST1")[0]
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        afl._close_exchange(lc, "EST1", _lv, [], "手动平仓（交易所已无持仓）")
    lr = live_rows_of("EST1")[0]
    check("62-a 记录收敛 CLOSED", lr["status"] == "CLOSED", str(lr["status"]))
    check("62-b realized 按纸盘现价估算 = (110-100)x1.0 = 10（不是 0）",
          abs(float(lr["realized_pnl"] or 0) - 10.0) < 1e-6,
          str(lr["realized_pnl"]))

    print("== 63. 减仓归属：交易所多出一点（≤记录×1.25）仍要对齐回目标 ==")
    # 比例模型下不再有"价格漂移"驱动的减仓，但交易所**多出一点**（部分成交取整/
    # 手动蹭单）时仍要对齐回目标。现价跑掉只是让"名义≈整数倍"这类形态判据失效，
    # 判据要落到"与记录量比"（≤1.25×）才能放行减仓，否则永远多着那 0.2。
    insert_paper("NEAR1", qty=1.0, entry=100.0)
    PRICE["NEAR1"] = 100.0
    afl._rounds_done = 99
    afl.apply_signal(sig("OPEN", "NEAR1"), True, 0.0, 3.0)
    check("前置：实盘 1.0（交易员 1.0 × k=1）",
          abs(FAKE.pos_qty("NEAR1") - 1.0) < 1e-9,
          str(live_rows_of("NEAR1")[0]["exchange_orders_json"]))
    PRICE["NEAR1"] = 130.0
    for p in FAKE.positions:          # 对账读的是**交易所仓的** mark_price，要一起动
        if p["symbol"] == "NEAR1USDT":
            p["mark_price"] = 130.0
            p["quantity"] = 1.2       # 交易所比目标多 0.2
    _lr = live_rows_of("NEAR1")[0]
    check("归属判据：1.2 ≤ 1.0×1.25 → 仍认作我们的仓",
          afl._looks_like_ours({"quantity": 1.2, "mark_price": 130.0,
                                "leverage": 3}, 1.0, 3) is True,
          str(_lr["exchange_orders_json"]))
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("接近记录 → 仍然减仓回到目标 1.0",
          abs(FAKE.pos_qty("NEAR1") - 1.0) < 1e-6, str(FAKE.pos_qty("NEAR1")))
    check("减仓后记录跟上真实量",
          abs(float(live_rows_of("NEAR1")[0]["quantity"]) - 1.0) < 1e-6,
          str(live_rows_of("NEAR1")[0]["quantity"]))
    # 对照：交易所明显多于记录（>1.25×）→ 判"混有手动仓" → 不越界减
    for p in FAKE.positions:
        if p["symbol"] == "NEAR1USDT":
            p["quantity"] = 1.3
    _c0 = len([c for c in FAKE.calls if c[0] == "close" and c[1] == "NEAR1"])
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("交易所 1.3 > 记录×1.25：不越界减仓（混手动仓）",
          len([c for c in FAKE.calls if c[0] == "close" and c[1] == "NEAR1"]) == _c0
          and abs(FAKE.pos_qty("NEAR1") - 1.3) < 1e-9, str(FAKE.pos_qty("NEAR1")))

    print("== 64. 现价怎么抖都不成交（BTC 真样本再现：旧口径在 step 间反复减/补） ==")
    # 真样本 2026-09-16 BTC：旧口径 目标 = 份数 × 名义 ÷ **现价** → 现价微动就在
    # 相邻 step 间跳 ±1 格，而旧容忍带 0.00053 **< 1 step** → 每次跳动都真成交：
    # 日志实证「加一份 → 1 秒后对账减仓 0.001 → 随后又对账补仓 0.001」白付手续费。
    # 比例模型：目标 = 交易员量 × k，与现价无关 → 抖动必须一笔都不成交。
    FAKE.step, FAKE.min_qty = 0.001, 0.001
    insert_paper("BTCQ", qty=1.0, entry=76000.0, k_qty=384.6153846153846)
    PRICE["BTCQ"] = 76000.0
    afl._rounds_done = 99
    afl.apply_signal(sig("OPEN", "BTCQ"), True, 0.0, 5.0)

    def _q_mark(m):
        PRICE["BTCQ"] = m
        for _p in FAKE.positions:
            if _p["symbol"] == "BTCQUSDT":
                _p["mark_price"] = m

    # 交易员 0.0026 枚（纸盘 1.0 ÷ k_qty 384.6）→ 目标 0.0026 **不是 step 整数倍**
    _t0 = afl._round_qty_ex("BTCQ", 0.0026)           # → 0.003
    check("前置：目标 0.0026 落在量化值 0.003 上（非整数倍）",
          abs(FAKE.pos_qty("BTCQ") - _t0) < 1e-9 and abs(_t0 - 0.0026) > 1e-9,
          f"pos={FAKE.pos_qty('BTCQ')} 目标 {_t0}")
    # 关键回归：现价连续**来回抖** 20 轮 → 一笔都不许成交。旧口径下
    # 目标 = 份数×名义÷现价 会随现价在相邻 step 间跳（真样本 09-16 BTC：
    # "减 0.001 → 补 0.001"来回白付手续费）；比例模型目标与现价无关。
    _c0 = len(FAKE.calls)
    for _i in range(20):
        _q_mark(76000.0 * (1 + (0.00005 if _i % 2 else -0.00005)))
        afl.sync_alphafox_live(True, 0.0, 5.0)
    check("现价来回抖 20 轮：一笔成交都没有（目标与现价无关）",
          len(FAKE.calls) == _c0, f"多打 {len(FAKE.calls) - _c0} 笔 "
          f"{[c for c in FAKE.calls[_c0:] if c[0] in ('open', 'close')][:4]}")
    check("抖动期间记录始终对齐真实持仓",
          abs(float(live_rows_of("BTCQ")[0]["quantity"]) - _t0) < 1e-9,
          str(live_rows_of("BTCQ")[0]["quantity"]))
    # 而"交易员加仓"必须照跟，且正好落到新目标（**现价故意抬到远离开仓价的
    # 85000**：加仓量若按现价折算而不是按 k 算，结果会差）
    _c1 = len(FAKE.calls)
    _q_mark(85000.0)
    with storage.get_conn() as conn:   # 交易员 0.0026 → 0.0052（纸盘 1.0 → 2.0）
        conn.execute("UPDATE trade_positions SET quantity=2.0, "
                     "updated_at=CURRENT_TIMESTAMP WHERE token='BTCQ' "
                     "AND strategy='alphafox' AND status IN ('OPEN','PARTIAL')")
    afl.apply_signal(sig("ADD", "BTCQ"), True, 0.0, 5.0)
    _t2 = afl._round_qty_ex("BTCQ", 0.0052)
    _ord = [c for c in FAKE.calls[_c1:] if c[0] in ("open", "close")]
    check("交易员加仓：只下一笔成交单，且正好落到新目标",
          len(_ord) == 1 and abs(FAKE.pos_qty("BTCQ") - _t2) < 1e-9,
          f"成交单 {len(_ord)} 笔 {_ord}，仓位 {FAKE.pos_qty('BTCQ')} 目标 {_t2}")
    _c2 = len(FAKE.calls)
    afl.sync_alphafox_live(True, 0.0, 5.0)
    check("加仓后紧接着的对账不再补一刀（旧行为：再来一次减/补）",
          len(FAKE.calls) == _c2, f"多打 {len(FAKE.calls) - _c2} 笔")
    FAKE.step, FAKE.min_qty = None, 0.0

    print("== 65. ⚡ 快速跟随（实盘侧）：闸门 + 认领已有记录 ==")
    alphafox_trade.init_db()
    with storage.get_conn() as conn:      # 实盘开关打开（否则闸门先一步跳过）
        storage.trading_settings_update(conn, {"alphafox_live_enabled": True})
    PRICE["QF1"] = 100.0
    insert_paper("QF1", qty=2.0, entry=100.0)     # 交易员 2.0（纸盘 2.0 ÷ k_qty 1）
    # 该币的"生效杠杆"（每币覆盖没有 → 取全局设置；桩里是 config 默认值）
    _lev_qf = int(afl._eff_live_params("QF1", afl._af_global_leverage()))
    # 无纸盘仓 → 失败
    r = afl.quick_follow_live("NOPE1", "LONG", 1.0)
    check("无纸盘活跃仓 → 失败", r["ok"] is False and "纸盘" in r["reason"], str(r))
    # 交易所查询不可用 → 失败（不许瞎猜）
    FAKE.fail_positions = True
    try:
        r = afl.quick_follow_live("QF1", "LONG", 2.0)
    finally:
        FAKE.fail_positions = False
    check("交易所查询不可用 → 失败", r["ok"] is False and "交易所" in r["reason"],
          str(r))
    # 交易所已有该币、杠杆 ≠ 设置 → 失败 + 日志留痕（用户需求 d）
    FAKE.positions.append({"symbol": "QF1USDT", "quantity": 2.0,
                           "entry_price": 100.0, "mark_price": 100.0,
                           "unrealized_pnl": 0.0, "leverage": _lev_qf + 7,
                           "position_side": "LONG"})
    r = afl.quick_follow_live("QF1", "LONG", 2.0)
    check("杠杆不一致 → 失败", r["ok"] is False and "杠杆" in r["reason"], str(r))
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        _qf_log = [dict(x) for x in lc.execute(
            "SELECT * FROM live_trade_log WHERE token='QF1' AND "
            "action='quick_follow_blocked' ORDER BY id DESC LIMIT 1")]
    check("被拒写 live_trade_log（action=quick_follow_blocked）",
          bool(_qf_log) and "杠杆" in (json.loads(
              _qf_log[0]["details_json"] or "{}").get("reason") or ""),
          str(_qf_log))
    check("被拒不动交易所持仓", abs(FAKE.pos_qty("QF1") - 2.0) < 1e-9,
          str(FAKE.pos_qty("QF1")))
    # 杠杆对上、数量远超"我们会开的量"→ 判"不像我们的"（手动仓）→ 失败
    next(p for p in FAKE.positions if p["symbol"] == "QF1USDT")["leverage"] = _lev_qf
    for p in FAKE.positions:
        if p["symbol"] == "QF1USDT":
            p["quantity"] = 50.0        # 目标会是 2.0×1 = 2.0 → 50 远超 1.25×
    r = afl.quick_follow_live("QF1", "LONG", 2.0)
    check("交易所持仓远超预期（手动仓）→ 失败不认领",
          r["ok"] is False and "非镜像" in r["reason"], str(r))
    # 形态对得上 → 认领：**不下单**，由随后对账补齐
    for p in FAKE.positions:
        if p["symbol"] == "QF1USDT":
            p["quantity"] = 2.0
    afl._rounds_done = 0                    # 故意压回冷窗
    _c65 = len(calls_for("QF1"))
    r = afl.quick_follow_live("QF1", "LONG", 2.0)
    check("通过闸门：交易所有我们的仓 → 如实回报「认领」（不是「新建」，更不下单）",
          r["ok"] is True and r["action"] == "认领", str(r))
    check("快速跟随本身不下单", len(calls_for("QF1")) == _c65,
          str(calls_for("QF1")[_c65:]))
    check("冷窗被抬过（对账立刻能补开）", afl._rounds_done > afl.MIN_SYNC_ROUNDS,
          str(afl._rounds_done))
    # 对账用**该币生效杠杆**（页面/探测线程传的就是全局设置值）
    afl.sync_alphafox_live(True, 0.0, _lev_qf)
    check("对账把交易所补到目标 2.0", abs(FAKE.pos_qty("QF1") - 2.0) < 1e-9,
          str(FAKE.pos_qty("QF1")))
    check("实盘记录已建且 k=1",
          bool(live_rows_of("QF1")) and abs(float(json.loads(
              live_rows_of("QF1")[0]["exchange_orders_json"] or "{}").get("k") or 0)
              - 1.0) < 1e-9, str(live_rows_of("QF1")[:1]))
    # 已有记录（第二次点）→ "已跟"，且把 k 用记录补写一次
    r = afl.quick_follow_live("QF1", "LONG", 2.0)
    check("已有记录 → 已跟（幂等，不重复建）",
          r["ok"] is True and r["action"] == "已跟", str(r))
    check("幂等：live 记录仍只有 1 条", len(live_rows_of("QF1")) == 1,
          str(len(live_rows_of("QF1"))))

    print("== 66. 资金约束：余额不够 → 只跳过加仓（减仓/平仓照跟） ==")
    alphafox_trade.init_db()
    PRICE["FUND1"] = 100.0
    _p66 = insert_paper("FUND1", qty=1.0, entry=100.0, k_qty=1.0)
    set_bal(100000.0)
    afl._rounds_done = 99
    afl.apply_signal(sig("OPEN", "FUND1"), True, 0.0, 3.0)
    check("前置：实盘 1.0", abs(FAKE.pos_qty("FUND1") - 1.0) < 1e-9,
          str(FAKE.pos_qty("FUND1")))
    with storage.get_conn() as conn:   # 交易员加到 5.0 → 目标 5.0（要新增 4.0）
        conn.execute("UPDATE trade_positions SET quantity=5.0, "
                     "updated_at=CURRENT_TIMESTAMP WHERE id=?", (_p66,))
    set_bal(1.0)                       # 可用余额只剩 1U：新增保证金 4×100/3 要不起
    afl.apply_signal(sig("ADD", "FUND1"), True, 0.0, 3.0)
    check("余额不够：不加仓（交易所仍 1.0）",
          abs(FAKE.pos_qty("FUND1") - 1.0) < 1e-9, str(FAKE.pos_qty("FUND1")))
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        _lg66 = [dict(x) for x in lc.execute(
            "SELECT * FROM live_trade_log WHERE token='FUND1' AND "
            "action='ratio_capped' ORDER BY id")]
    check("被拒写了 ratio_capped 日志（一次）", len(_lg66) == 1, str(_lg66))
    afl.apply_signal(sig("ADD", "FUND1"), True, 0.0, 3.0)
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        _lg66b = [dict(x) for x in lc.execute(
            "SELECT * FROM live_trade_log WHERE token='FUND1' AND "
            "action='ratio_capped' ORDER BY id")]
    check("状态没变不重复记（不刷日志）", len(_lg66b) == 1, str(len(_lg66b)))
    with storage.get_conn() as conn:   # 交易员减到 0.5 → 目标 0.5（净量 = 5.0−4.5）
        conn.execute("UPDATE trade_positions SET quantity=5.0, closed_qty=4.5, "
                     "updated_at=CURRENT_TIMESTAMP WHERE id=?", (_p66,))
    afl.apply_signal(sig("REDUCE", "FUND1"), True, 0.0, 3.0)
    check("余额不够也要跟减仓（0.5）",
          abs(FAKE.pos_qty("FUND1") - 0.5) < 1e-9, str(FAKE.pos_qty("FUND1")))
    set_bal(BAL)

    print("== 67. 单笔上限：超限只跳过加仓（0 = 不限） ==")
    alphafox_trade.init_db()
    PRICE["CAP1"] = 100.0
    _p67 = insert_paper("CAP1", qty=1.0, entry=100.0)
    afl._rounds_done = 99
    afl.apply_signal(sig("OPEN", "CAP1"), True, 0.0, 3.0)     # 不限 → 开 1.0
    check("前置：实盘 1.0", abs(FAKE.pos_qty("CAP1") - 1.0) < 1e-9,
          str(FAKE.pos_qty("CAP1")))
    with storage.get_conn() as conn:   # 交易员加到 3.0 → 名义 300U
        conn.execute("UPDATE trade_positions SET quantity=3.0, "
                     "updated_at=CURRENT_TIMESTAMP WHERE id=?", (_p67,))
    afl.sync_alphafox_live(True, 150.0, 3.0)   # 上限 150U < 300U
    check("超上限：不加仓（交易所仍 1.0）",
          abs(FAKE.pos_qty("CAP1") - 1.0) < 1e-9, str(FAKE.pos_qty("CAP1")))
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        _lg67 = [dict(x) for x in lc.execute(
            "SELECT * FROM live_trade_log WHERE token='CAP1' AND "
            "action='ratio_capped' ORDER BY id DESC LIMIT 1")]
    check("超上限写 ratio_capped 日志（原因含单笔上限）",
          bool(_lg67) and "单笔上限" in (json.loads(
              _lg67[0]["details_json"] or "{}").get("reason") or ""),
          str(_lg67))
    afl.sync_alphafox_live(True, 0.0, 3.0)     # 改回不限 → 补齐
    check("上限放开后：补齐到目标 3.0",
          abs(FAKE.pos_qty("CAP1") - 3.0) < 1e-9, str(FAKE.pos_qty("CAP1")))
    check("加成功后清掉 capped 标记",
          "capped" not in (live_rows_of("CAP1")[0]["exchange_orders_json"] or ""),
          str(live_rows_of("CAP1")[0]["exchange_orders_json"]))

    print("== 69. 资金约束（用户例子）：每次加仓只要求「本次新增保证金」够 ==")
    # 用户 2026-09-18 口径：交易员保证金 10w、我 200U、5x。
    # 交易员每加 10w（名义）→ 我们加 200U（保证金 40U）：
    #   可用余额 200 → 160 → 120 → 80 → 40 → 0，到不够下一次 40U 为止
    alphafox_trade.init_db()
    PRICE["FUNDX"] = 100.0
    FAKE.deduct_margin = True            # 开/平仓按币安口径增减可用余额
    set_bal(200.0)
    # 交易员 10w 保证金、持仓 1000 枚 @100（名义 10w）→ k = 200/100000 = 0.002
    _p69 = insert_paper("FUNDX", qty=1000.0, entry=100.0,
                        k_qty=1.0, trader_margin=100000.0)
    afl._rounds_done = 99
    _sig_tm = {"trader_margin": 100000.0}
    afl.apply_signal(sig("OPEN", "FUNDX", **_sig_tm), True, 0.0, 5.0)
    check("第 1 次：开 1000 × 0.002 = 2.0（名义 200U、占用 40U）",
          abs(FAKE.pos_qty("FUNDX") - 2.0) < 1e-12, str(FAKE.pos_qty("FUNDX")))
    check("可用余额 200 → 160", abs(FAKE.balance - 160.0) < 1e-9,
          str(FAKE.balance))
    for _i, _want in enumerate((120.0, 80.0, 40.0, 0.0), start=2):
        with storage.get_conn() as conn:      # 交易员再加 10w（累计 _i×10w）
            conn.execute("UPDATE trade_positions SET quantity=?, "
                         "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         (1000.0 * _i, _p69))
        set_bal(FAKE.balance)                 # 刷新 60s 缓存（下一轮读到新余额）
        afl.apply_signal(sig("ADD", "FUNDX"), True, 0.0, 5.0)
        check(f"第 {_i} 次：再跟 200U（累计 {_i * 2.0:.1f} 枚）",
              abs(FAKE.pos_qty("FUNDX") - _i * 2.0) < 1e-12,
              str(FAKE.pos_qty("FUNDX")))
        check(f"第 {_i} 次后可用余额 = {_want}", abs(FAKE.balance - _want) < 1e-9,
              str(FAKE.balance))
    # 第 6 次：可用余额 0，本次要 40U → 加不动（但仓位与监控继续）
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET quantity=6000.0, "
                     "updated_at=CURRENT_TIMESTAMP WHERE id=?", (_p69,))
    set_bal(FAKE.balance)
    afl.apply_signal(sig("ADD", "FUNDX"), True, 0.0, 5.0)
    check("第 6 次：可用余额不够 → 不加仓（仍 10.0）",
          abs(FAKE.pos_qty("FUNDX") - 10.0) < 1e-12, str(FAKE.pos_qty("FUNDX")))
    check("被挡下的原因写清楚了（可用余额 / 本次需保证金）",
          "可用余额" in afl._load_orders(live_rows_of("FUNDX")[0]).get("capped", ""),
          str(live_rows_of("FUNDX")[0]["exchange_orders_json"]))
    with storage.get_conn() as conn:   # 交易员减到 3000 → 目标 6.0：没钱也要跟减
        conn.execute("UPDATE trade_positions SET quantity=6000.0, closed_qty=3000.0, "
                     "updated_at=CURRENT_TIMESTAMP WHERE id=?", (_p69,))
    afl.apply_signal(sig("REDUCE", "FUNDX"), True, 0.0, 5.0)
    check("加不动之后：减仓照跟（10.0 → 6.0）",
          abs(FAKE.pos_qty("FUNDX") - 6.0) < 1e-12, str(FAKE.pos_qty("FUNDX")))
    FAKE.deduct_margin = False
    set_bal(BAL)

    print("== 74. 减仓/对账减仓都要记已实现盈亏（少记=战绩失真） ==")
    alphafox_trade.init_db()
    PRICE["RL1"] = 100.0
    _p74 = insert_paper("RL1", qty=2.0, entry=100.0, k_qty=1.0, trader_margin=1000.0)
    set_bal(1000.0)
    afl.apply_signal(sig("OPEN", "RL1", trader_margin=1000.0), True, 0.0, 3.0)
    check("前置：实盘 2.0", abs(FAKE.pos_qty("RL1") - 2.0) < 1e-12,
          str(FAKE.pos_qty("RL1")))
    check("开仓时 realized 仍是 0",
          abs(float(live_rows_of("RL1")[0]["realized_pnl"] or 0)) < 1e-12,
          str(live_rows_of("RL1")[0]["realized_pnl"]))
    # 价格涨到 110（多头赚 10/枚）→ 交易员减半（净量 1.0）→ 平掉 1.0 应记 +10
    PRICE["RL1"] = 110.0
    for _p in FAKE.positions:
        if _p["symbol"] == "RL1USDT":
            _p["mark_price"] = 110.0
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET quantity=2.0, closed_qty=1.0, "
                     "updated_at=CURRENT_TIMESTAMP WHERE id=?", (_p74,))
    afl.apply_signal(sig("REDUCE", "RL1"), True, 0.0, 3.0)
    _lr74 = live_rows_of("RL1")[0]
    check("信号减仓：平掉 1.0 记了 +10（(110−100)×1.0）",
          abs(float(_lr74["realized_pnl"] or 0) - 10.0) < 1e-9
          and abs(float(_lr74["quantity"]) - 1.0) < 1e-12,
          f"{_lr74['realized_pnl']} / {_lr74['quantity']}")
    # 对账减仓路径：交易所被手动加到 1.2（在记录×1.25 之内）→ 对账减回 1.0 → 记 +2
    for _p in FAKE.positions:
        if _p["symbol"] == "RL1USDT":
            _p["quantity"] = 1.2
    afl.sync_alphafox_live(True, 0.0, 3.0)
    _lr74b = live_rows_of("RL1")[0]
    check("对账减仓：减掉 0.2 记了 +2（(110−100)×0.2）",
          abs(float(_lr74b["realized_pnl"] or 0) - 12.0) < 1e-9,
          str(_lr74b["realized_pnl"]))
    with storage.get_conn(config.LIVE_DB_PATH) as _lc:
        _lg74 = [dict(r) for r in _lc.execute(
            "SELECT * FROM live_trade_log WHERE token='RL1' AND "
            "action='rebalance_reduce' ORDER BY id DESC LIMIT 1")]
    check("对账减仓写进 live_trade_log（action=rebalance_reduce，量 0.2）",
          bool(_lg74) and abs(abs(float(_lg74[0]["quantity"] or 0)) - 0.2) < 1e-6,
          str(_lg74))
    # 全平：按剩余 1.0 再记 +10 → 累计 22（减仓那两段不再丢失）
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
                     "WHERE token='RL1' AND strategy='alphafox'")
    afl.apply_signal(sig("CLOSE", "RL1"), True, 0.0, 3.0)
    _lr74c = live_rows_of("RL1")[0]
    check("全平后累计 realized = 12 + 10 = 22（原来只记最后一次的 10）",
          _lr74c["status"] == "CLOSED"
          and abs(float(_lr74c["realized_pnl"] or 0) - 22.0) < 1e-9,
          f"{_lr74c['status']} / {_lr74c['realized_pnl']}")
    # 空头方向也要对（方向感知）
    PRICE["RL2"] = 100.0
    _p75 = insert_paper("RL2", side="SHORT", qty=1.0, entry=100.0,
                        k_qty=1.0, trader_margin=1000.0)
    afl.apply_signal(sig("OPEN", "RL2", side="SHORT", trader_margin=1000.0),
                     True, 0.0, 3.0)
    PRICE["RL2"] = 90.0                     # 空头赚 10/枚
    for _p in FAKE.positions:
        if _p["symbol"] == "RL2USDT":
            _p["mark_price"] = 90.0
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET quantity=1.0, closed_qty=0.5, "
                     "updated_at=CURRENT_TIMESTAMP WHERE id=?", (_p75,))
    afl.apply_signal(sig("REDUCE", "RL2", side="SHORT"), True, 0.0, 3.0)
    check("空头减仓方向也对：平 0.5 记 +5（(100−90)×0.5）",
          abs(float(live_rows_of("RL2")[0]["realized_pnl"] or 0) - 5.0) < 1e-9,
          str(live_rows_of("RL2")[0]["realized_pnl"]))
    set_bal(BAL)

    print("== 72. CLOSE 兜底平仓：同向同杠杆的**用户手动仓**不许被误平 ==")
    # 用户的真实 setup：他在币安手动开了仓，同时跟单一个也在该币做空的交易员
    # （我们的实盘被闸门挡着、从没建过记录）→ 交易员平仓时，兜底平仓的参考量
    # 如果是 0（只按杠杆判），就会把他的手动仓平掉
    alphafox_trade.init_db()
    PRICE["MC1"] = 100.0
    insert_paper("MC1", qty=1.0, entry=100.0, k_qty=1.0, trader_margin=1000.0)
    set_bal(1000.0)                    # 权益=可用=1000 → 参考量 = 1 × 1000/1000 = 1
    # 用户手动仓：同方向、**同杠杆**（3x，与设置一致），但数量 50 ≫ 参考量×1.25
    FAKE.positions.append({"symbol": "MC1USDT", "quantity": 50.0,
                           "entry_price": 100.0, "mark_price": 100.0,
                           "unrealized_pnl": 0.0, "leverage": 3,
                           "position_side": "LONG"})
    with storage.get_conn() as conn:   # 纸盘仓被交易员平掉（CLOSE 信号来了）
        conn.execute("UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
                     "WHERE token='MC1' AND strategy='alphafox'")
    _c72 = len([c for c in FAKE.calls if c[0] == "close"])
    afl.apply_signal(sig("CLOSE", "MC1"), True, 0.0, 3.0)
    check("参考量用「该纸盘仓反推的实盘目标量」→ 50 枚判「不像我们的」→ 不误平",
          len([c for c in FAKE.calls if c[0] == "close"]) == _c72
          and abs(FAKE.pos_qty("MC1") - 50.0) < 1e-9,
          f"平仓调用 {FAKE.calls[_c72:]}，持仓 {FAKE.pos_qty('MC1')}")
    # 对照：真的是我们的残留仓（数量与参考量一致）→ 照常兜底平
    FAKE.positions = [p for p in FAKE.positions if p["symbol"] != "MC1USDT"]
    PRICE["MC2"] = 100.0
    insert_paper("MC2", qty=1.0, entry=100.0, k_qty=1.0, trader_margin=1000.0)
    FAKE.positions.append({"symbol": "MC2USDT", "quantity": 1.0,
                           "entry_price": 100.0, "mark_price": 100.0,
                           "unrealized_pnl": 0.0, "leverage": 3,
                           "position_side": "LONG"})
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
                     "WHERE token='MC2' AND strategy='alphafox'")
    afl.apply_signal(sig("CLOSE", "MC2"), True, 0.0, 3.0)
    check("对照：数量与目标量一致（是我们的残留仓）→ 兜底平掉",
          FAKE.pos_qty("MC2") == 0.0, str(FAKE.pos_qty("MC2")))
    set_bal(BAL)

    print("== 73. 快速跟随：反方向有手动仓 → 如实回报（不报「新建」） ==")
    alphafox_trade.init_db()
    PRICE["MC3"] = 100.0
    insert_paper("MC3", qty=1.0, entry=100.0, k_qty=1.0, trader_margin=1000.0)
    with storage.get_conn() as _c:
        storage.trading_settings_update(_c, {"alphafox_live_enabled": True})
    FAKE.positions.append({"symbol": "MC3USDT", "quantity": -8.0,
                           "entry_price": 100.0, "mark_price": 100.0,
                           "unrealized_pnl": 0.0, "leverage": 3,
                           "position_side": "SHORT"})   # 用户手动开的是空单
    set_bal(1000.0)
    r = afl.quick_follow_live("MC3", "LONG", 1.0)      # 交易员做多
    check("反方向有手动仓 → 跳过并说明（不是「新建」）",
          r["ok"] is False and "反方向" in (r.get("reason") or ""), str(r))
    check("用户的反方向仓没被动", abs(FAKE.pos_qty("MC3", "SHORT") + 8.0) < 1e-9,
          str(FAKE.pos_qty("MC3", "SHORT")))
    set_bal(BAL)

    print("== 71. 认领/快速跟随的参考量用**权益**（不是可用余额） ==")
    # 用户 2026-09-18 的场景：账户里已有一笔仓占着保证金 → 可用余额被压小。
    # 拿可用余额当参考量会"自己把自己判小" → 自家残留仓被判"不像"、认领不了。
    alphafox_trade.init_db()
    PRICE["EQ2"] = 100.0
    set_bal_av(400.0)          # 可用余额只剩 400
    set_equity(1000.0)         # 权益仍是 1000（差额被已有仓占着）
    # 交易员 1000 枚、保证金 10w：权益口径参考量 = 1000 × 1000/100000 = 10 枚
    #                       可用口径参考量 = 1000 ×  400/100000 =  4 枚
    _p71 = insert_paper("EQ2", qty=1000.0, entry=100.0, k_qty=1.0,
                        trader_margin=100000.0)
    FAKE.positions.append({"symbol": "EQ2USDT", "quantity": 6.0,
                           "entry_price": 100.0, "mark_price": 100.0,
                           "unrealized_pnl": 0.0, "leverage": 3,
                           "position_side": "LONG"})
    check("前置：6 枚 > 可用口径上限 4×1.25=5，但 ≤ 权益口径上限 10×1.25=12.5",
          6.0 > 1000.0 * 400 / 100000 * 1.25 and 6.0 <= 1000.0 * 1000 / 100000 * 1.25)
    afl._rounds_done = 99
    _c71 = len([c for c in FAKE.calls if c[0] in ("open", "close")])
    afl.sync_alphafox_live(True, 0.0, 3.0)
    _lr71 = live_rows_of("EQ2")
    check("权益口径判「像我们的」→ 认领（补建记录）",
          bool(_lr71) and _lr71[0]["status"] == "OPEN", str(_lr71[:1]))
    check("认领按现状冻结 k=6÷1000、记录数量=6",
          bool(_lr71) and abs(float(_lr71[0]["quantity"]) - 6.0) < 1e-12
          and abs(float(json.loads(_lr71[0]["exchange_orders_json"] or "{}")
                        .get("k") or 0) - 0.006) < 1e-12,
          str(_lr71[:1]))
    _new71 = [c for c in FAKE.calls if c[0] in ("open", "close")][_c71:]
    check("认领零下单（目标==现状）", not _new71, str(_new71))
    # 对照：真·手动大仓（远超权益口径上限）仍然拒绝
    PRICE["EQ3"] = 100.0
    insert_paper("EQ3", qty=1000.0, entry=100.0, k_qty=1.0, trader_margin=100000.0)
    FAKE.positions.append({"symbol": "EQ3USDT", "quantity": 500.0,
                           "entry_price": 100.0, "mark_price": 100.0,
                           "unrealized_pnl": 0.0, "leverage": 3,
                           "position_side": "LONG"})
    _c73 = len([c for c in FAKE.calls if c[0] in ("open", "close")])
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("真手动大仓（500 枚 ≫ 12.5）：仍然拒绝、不认领",
          not live_rows_of("EQ3")
          and len([c for c in FAKE.calls if c[0] in ("open", "close")]) == _c73,
          str(live_rows_of("EQ3")[:1]))
    check("跳过时把原因记进内存（页面提示用；以前只有日志能看）",
          "非镜像持仓" in afl.live_skip_reason("EQ3", "LONG"),
          repr(afl.live_skip_reason("EQ3", "LONG")))
    # 快速跟随闸门同一口径：同一个币，权益口径放行、可用口径拒绝
    PRICE["EQ4"] = 100.0
    insert_paper("EQ4", qty=1000.0, entry=100.0, k_qty=1.0, trader_margin=100000.0)
    FAKE.positions.append({"symbol": "EQ4USDT", "quantity": 6.0,
                           "entry_price": 100.0, "mark_price": 100.0,
                           "unrealized_pnl": 0.0,
                           "leverage": 5,   # 与全局设置一致（否则先被杠杆闸门拦）
                           "position_side": "LONG"})
    with storage.get_conn() as _c:
        storage.trading_settings_update(_c, {"alphafox_live_enabled": True})
    r = afl.quick_follow_live("EQ4", "LONG", 1000.0)
    check("快速跟随闸门同口径：权益口径 → 不像的判据不再误拦（放行）",
          r["ok"] is True, str(r))
    set_bal(BAL)

    print("== 68. 可用余额 60s 缓存（连开时共用第一次读到的读数） ==")
    set_bal(1000.0)
    check("第一次读到 1000", abs(afl.available_balance_cached() - 1000.0) < 1e-9,
          str(afl.available_balance_cached()))
    FAKE.balance = 50.0                 # 底层读数变了（但缓存还在窗口内）
    check("窗口内仍用第一次的读数 1000",
          abs(afl.available_balance_cached() - 1000.0) < 1e-9,
          str(afl.available_balance_cached()))
    afl._BAL_CACHE.update({"ts": 0.0, "v": 0.0})   # 过了窗口
    check("过窗口后读到新值 50",
          abs(afl.available_balance_cached() - 50.0) < 1e-9,
          str(afl.available_balance_cached()))
    # 读数失败 → -1（与"余额真的是 0"区分开：0 = 加不动了，-1 = 没读到）
    FAKE.fail_account = True
    afl._BAL_CACHE.update({"ts": 0.0, "v": 0.0})
    check("读数失败 → -1（不抛异常，且不误判成余额为 0）",
          afl.available_balance_cached() == -1.0,
          str(afl.available_balance_cached()))
    FAKE.fail_account = False
    set_bal(BAL)

    print("== 70. 全局映射比例 α：只按 α 的资金量跟（控实盘风险） ==")
    alphafox_trade.init_db()
    PRICE["ALP1"] = 100.0
    # 余额 1000、交易员保证金 1000 → 全额 k=1（目标量 == 交易员量），
    # 这样"全额 1.0 枚"和"α 折后"一眼可辨
    _p70 = insert_paper("ALP1", qty=1.0, entry=100.0, k_qty=1.0,
                        trader_margin=1000.0)
    afl._rounds_done = 99

    def _set_alpha(v):
        with storage.get_conn() as conn:
            storage.trading_settings_update(
                conn, {"alphafox_live_ratio": v})

    _set_alpha(1.0)
    set_bal(1000.0)
    afl.apply_signal(sig("OPEN", "ALP1", trader_margin=1000.0), True, 0.0, 3.0)
    check("前置：α=1 时全额 = 交易员量 1.0",
          abs(FAKE.pos_qty("ALP1") - 1.0) < 1e-12, str(FAKE.pos_qty("ALP1")))
    # 重来一次：α=0.5（把上一次的仓清掉）
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        lc.execute("UPDATE live_positions SET status='CLOSED' WHERE token='ALP1'")
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
                     "WHERE token='ALP1' AND strategy='alphafox'")
    FAKE.positions = [p for p in FAKE.positions if p["symbol"] != "ALP1USDT"]
    _p70 = insert_paper("ALP1", qty=1.0, entry=100.0, k_qty=1.0,
                        trader_margin=1000.0)
    afl._QF_EXPLICIT.discard("ALP1|LONG")
    _set_alpha(0.5)                       # 只跟他一半
    afl.apply_signal(sig("OPEN", "ALP1", trader_margin=1000.0), True, 0.0, 3.0)
    check("α=0.5：开仓量是全额的一半（1.0 → 0.5）",
          abs(FAKE.pos_qty("ALP1") - 0.5) < 1e-12, str(FAKE.pos_qty("ALP1")))
    _o70 = json.loads(live_rows_of("ALP1")[0]["exchange_orders_json"])
    check("记录里冻结了 α=0.5（事后可核对这笔是几折跟的）",
          abs(float(_o70.get("alpha") or 0) - 0.5) < 1e-9, str(_o70))
    check("冻结的 k = α × 余额 ÷ 交易员保证金 = 0.5",
          abs(float(_o70.get("k") or 0) - 0.5) < 1e-12, str(_o70))
    # 加仓同样按 α 缩放（目标量已经含 α，加仓量 = 目标 − 现仓）
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET quantity=2.0, "
                     "updated_at=CURRENT_TIMESTAMP WHERE id=?", (_p70,))
    afl.apply_signal(sig("ADD", "ALP1"), True, 0.0, 3.0)
    check("α=0.5：交易员加到 2.0 → 我们加到 1.0（不是 2.0）",
          abs(FAKE.pos_qty("ALP1") - 1.0) < 1e-12, str(FAKE.pos_qty("ALP1")))
    # 改 α 不动已有仓（k 已冻结）
    _set_alpha(0.2)
    _c70 = len(FAKE.calls)
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("改 α 不动已有仓（冻结语义）",
          abs(FAKE.pos_qty("ALP1") - 1.0) < 1e-12
          and all(c[0] not in ("open", "close") for c in FAKE.calls[_c70:]),
          f"{FAKE.pos_qty('ALP1')} {FAKE.calls[_c70:]}")
    check("已有仓的记录 k/α 也没被改写",
          abs(float(json.loads(live_rows_of("ALP1")[0]["exchange_orders_json"]
                               ).get("k") or 0) - 0.5) < 1e-12
          and abs(float(json.loads(live_rows_of("ALP1")[0]["exchange_orders_json"]
                                   ).get("alpha") or 0) - 0.5) < 1e-9,
          str(live_rows_of("ALP1")[0]["exchange_orders_json"]))

    # α 与资金约束的联动：用户 5x 跟高杠杆交易员的卡点，靠 α 化解
    # 交易员保证金 100000、持仓名义 1000000（账户有效杠杆 10x）→ α=1 时
    # 目标名义 = 1000 × 10 = 10000U，5x 需 2000U > 可用 1000U → 开不出；
    # α=0.4 → 需 800U ≤ 1000U → 开得出
    PRICE["ALP2"] = 100.0
    insert_paper("ALP2", qty=10000.0, entry=100.0, k_qty=1.0,
                 trader_margin=100000.0)
    _set_alpha(1.0)
    r = afl.quick_follow_live("ALP2", "LONG", 10000.0)
    check("α=1 + 5x：资金不够 → 如实拒绝",
          r["ok"] is False and "资金不够" in (r.get("reason") or ""), str(r))
    _set_alpha(0.4)
    r = afl.quick_follow_live("ALP2", "LONG", 10000.0)
    check("α=0.4 + 5x：开得出了（映射比例就是低杠杆跟高杠杆的通行证）",
          r["ok"] is True and r["action"] == "新建", str(r))
    _c70b = len([c for c in FAKE.calls
                 if c[0] in ("open", "close") and "ALP2" in str(c)])
    afl.sync_alphafox_live(True, 0.0, 5.0)
    check("按 α 开仓：10000 枚 × (0.4 × 1000/100000) = 40 枚",
          abs(FAKE.pos_qty("ALP2") - 40.0) < 1e-9, str(FAKE.pos_qty("ALP2")))
    check("只下一笔成交单",
          len([c for c in FAKE.calls
               if c[0] in ("open", "close") and "ALP2" in str(c)]) - _c70b == 1,
          str(calls_for("ALP2")[_c70b:]))

    # 老记录反推 k（份数模型写的）**不乘 α**：那是"目标==现状、零下单"
    PRICE["ALP3"] = 100.0
    _p73 = insert_paper("ALP3", qty=1.0, entry=100.0, k_qty=1.0)
    afl.apply_signal(sig("OPEN", "ALP3"), True, 0.0, 3.0)
    _q73 = FAKE.pos_qty("ALP3")
    with storage.get_conn(config.LIVE_DB_PATH) as lc:   # 抹掉 k，模拟老记录
        lc.execute("UPDATE live_positions SET exchange_orders_json=? "
                   "WHERE token='ALP3'", (json.dumps({"model": "lots"}),))
    _set_alpha(0.3)
    _c73 = len(FAKE.calls)
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("老记录反推 k 不乘 α（目标==现状 → 零下单，不会砍成三折）",
          abs(FAKE.pos_qty("ALP3") - _q73) < 1e-12
          and len(FAKE.calls) == _c73,
          f"{FAKE.pos_qty('ALP3')} {FAKE.calls[_c73:]}")
    # 记录**整条丢失** + 交易所有我们的仓 → 认领时也不乘 α：
    # 乘了的话认领那一刻就把手里的仓砍成 α 折（用户的真金白银）
    PRICE["ALP4"] = 100.0
    insert_paper("ALP4", qty=1.0, entry=100.0, k_qty=1.0, trader_margin=1000.0)
    _set_alpha(1.0)
    afl.apply_signal(sig("OPEN", "ALP4", trader_margin=1000.0), True, 0.0, 3.0)
    _q74 = FAKE.pos_qty("ALP4")
    check("前置：ALP4 全额 1.0", abs(_q74 - 1.0) < 1e-12, str(_q74))
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        lc.execute("DELETE FROM live_positions WHERE token='ALP4'")
    # α 取 0.9（不是 0.3）：0.3 时"参考量"只有 0.3×现状 → 先被"不像我们的"闸门
    # 挡下，根本走不到认领那一步，咬不住这条
    _set_alpha(0.9)
    _c74 = len([c for c in FAKE.calls if c[0] in ("open", "close")])
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("认领（记录丢失）：按现状冻结、当轮零下单",
          abs(FAKE.pos_qty("ALP4") - _q74) < 1e-12
          and len([c for c in FAKE.calls if c[0] in ("open", "close")]) == _c74,
          f"{FAKE.pos_qty('ALP4')} {FAKE.calls[_c74:]}")
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("认领后下一轮：目标仍==现状（α=0.9 也不许把它砍成九折）",
          abs(FAKE.pos_qty("ALP4") - _q74) < 1e-12
          and len([c for c in FAKE.calls if c[0] in ("open", "close")]) == _c74,
          f"{FAKE.pos_qty('ALP4')} {FAKE.calls[_c74:]}")

    # α 小到目标量低于交易所最小下单量 → 宁可**不跟**，也不能被抬到 minQty
    # （`exchange._round_qty` 是 max(取整, minQty)——抬上去会开出比 α 口径大几倍的仓）
    FAKE.step, FAKE.min_qty = 0.01, 0.01
    PRICE["ALP5"] = 100.0
    insert_paper("ALP5", qty=10.0, entry=100.0, k_qty=1.0, trader_margin=100000.0)
    _set_alpha(0.0001)          # 目标 10 × 0.0001×1000/100000 = 1e-5 < minQty 0.01
    _c75 = len([c for c in FAKE.calls if c[0] in ("open", "close")])
    r = afl.quick_follow_live("ALP5", "LONG", 10.0)
    check("α 过小（目标 < minQty）：快速跟随如实回报「低于最小下单量」",
          r["ok"] is False and "最小下单量" in (r.get("reason") or ""), str(r))
    afl.sync_alphafox_live(True, 0.0, 5.0)
    check("α 过小：一个单都不下（不被抬到 minQty 开出放大仓）",
          len([c for c in FAKE.calls if c[0] in ("open", "close")]) == _c75
          and FAKE.pos_qty("ALP5") == 0.0,
          f"{FAKE.pos_qty('ALP5')} {FAKE.calls[_c75:]}")
    # 加仓路径同理：本次增量低于 minQty → 跳过（原来会被抬到 minQty 超买）
    FAKE.step, FAKE.min_qty = None, 0.0
    PRICE["ALP6"] = 100.0
    _p76 = insert_paper("ALP6", qty=10.0, entry=100.0, k_qty=1.0)
    _set_alpha(1.0)
    set_bal(1000.0)
    afl.apply_signal(sig("OPEN", "ALP6", trader_margin=1000.0), True, 0.0, 5.0)
    FAKE.step, FAKE.min_qty = 0.01, 0.01
    with storage.get_conn() as conn:   # 交易员只加了一丝（我们增量 0.005 < minQty 0.01）
        conn.execute("UPDATE trade_positions SET quantity=10.005, "
                     "updated_at=CURRENT_TIMESTAMP WHERE id=?", (_p76,))
    _c76 = len([c for c in FAKE.calls if c[0] in ("open", "close")])
    afl.apply_signal(sig("ADD", "ALP6"), True, 0.0, 5.0)
    check("增量低于 minQty：跳过加仓（不被抬到 minQty 超买）",
          len([c for c in FAKE.calls if c[0] in ("open", "close")]) == _c76,
          str(FAKE.calls[_c76:]))
    FAKE.step, FAKE.min_qty = None, 0.0

    # 脏值/越界 α → 回退 1.0（不因为配置表脏了就缩仓）
    _set_alpha(0)
    check("α=0（脏值）→ 回退 1.0", abs(afl._af_live_ratio() - 1.0) < 1e-12,
          str(afl._af_live_ratio()))
    _set_alpha(5)
    check("α=5（越界）→ 回退 1.0（只允许缩小，不允许放大）",
          abs(afl._af_live_ratio() - 1.0) < 1e-12, str(afl._af_live_ratio()))
    _set_alpha(1)
    set_bal(BAL)

    # 交易所有我们的仓 + 可用余额被别的仓压干 → 仍要**认领**（零下单，不需要资金）。
    # 预检是给"真去开新仓"用的，不该拦认领路径（2026-09-29 用户 CL 场景：手动平掉
    # 后又手动开回同向仓，点快速跟随收编时被"实盘资金不够"误拦）
    PRICE["QFC1"] = 100.0
    insert_paper("QFC1", qty=10000.0, entry=100.0, k_qty=1.0,
                 trader_margin=100000.0)
    _set_alpha(1.0)
    set_bal(100.0)          # 可用 100U；交易员有效杠杆 10x（10000×100÷100000）
    _lev_c = int(afl._eff_live_params("QFC1", afl._af_global_leverage()))
    FAKE.positions.append({"symbol": "QFC1USDT", "quantity": 10.0,
                           "entry_price": 100.0, "mark_price": 100.0,
                           "unrealized_pnl": 0.0, "leverage": _lev_c,
                           "position_side": "LONG"})
    _c_qfc = len(calls_for("QFC1"))
    r = afl.quick_follow_live("QFC1", "LONG", 10000.0)
    check("交易所有我们的仓 + 可用余额不足：仍认领（资金预检不拦认领）",
          r["ok"] is True and r["action"] == "认领", str(r))
    check("认领当轮零下单", len(calls_for("QFC1")) == _c_qfc,
          str(calls_for("QFC1")[_c_qfc:]))
    # 同一账户状态下交易所**没仓** → 预检照旧拦住（闸门是收窄，不是拆掉）
    for _p in [x for x in FAKE.positions if x["symbol"] == "QFC1USDT"]:
        FAKE.positions.remove(_p)
    r = afl.quick_follow_live("QFC1", "LONG", 10000.0)
    check("同一余额下交易所没仓 → 仍如实报「资金不够」（预检没被拆）",
          r["ok"] is False and "资金不够" in (r.get("reason") or ""), str(r))
    set_bal(BAL)

    print("== 71. 恢复托管：交易所查询不可用 → 不落定（旧 k 不许继续生效） ==")
    alphafox_trade.init_db()
    PRICE["RAT1"] = 100.0
    _p71 = insert_paper("RAT1", qty=1.0, entry=100.0, k_qty=1.0,
                        trader_margin=1000.0)
    set_bal(1000.0)
    afl.apply_signal(sig("OPEN", "RAT1", trader_margin=1000.0), True, 0.0, 3.0)
    check("71 前置：实盘 1.0、冻结 k=1（余额 1000 ÷ 保证金 1000）",
          abs(FAKE.pos_qty("RAT1") - 1.0) < 1e-12
          and abs(float(json.loads(live_rows_of("RAT1")[0]["exchange_orders_json"]
                                   ).get("k") or 0) - 1.0) < 1e-12,
          str(FAKE.pos_qty("RAT1")))
    check("71 交易所快照为 None/空 → _ex_match 不抛，一律当'查不到'",
          afl._ex_match(None, "RAT1", "LONG") is None
          and afl._ex_match([], "RAT1", "LONG") is None)
    with storage.get_conn() as conn:      # 取消托管 + 你在币安手动加到 3.0
        conn.execute("UPDATE trade_positions SET managed=0, current_price=100.0 "
                     "WHERE id=?", (_p71,))
    for _p in FAKE.positions:
        if _p["symbol"] == "RAT1USDT":
            _p["quantity"] = 3.0
    afl.sync_alphafox_live(True, 0.0, 3.0)          # 事实同步 → 记录 3.0
    check("71 前置：非托管只同步不下单（记录 3.0、交易所仍 3.0）",
          abs(float(live_rows_of("RAT1")[0]["quantity"]) - 3.0) < 1e-9
          and abs(FAKE.pos_qty("RAT1") - 3.0) < 1e-12,
          str(live_rows_of("RAT1")[0]["quantity"]))
    # 点"恢复托管"那一刻交易所查询失败 → 必须**不落定**：旧 k=1 会让下一轮目标
    # 只有 1.0，把自己手动加的量按旧目标市价砍回去（真金白银）
    FAKE.fail_positions = True
    check("71 交易所查询不可用：on_reattach 返回 False（不落定）",
          afl.on_reattach("RAT1", "LONG") is False)
    check("71 不可用时：k 没被改写（仍是开仓时的 1）",
          abs(float(json.loads(live_rows_of("RAT1")[0]["exchange_orders_json"]
                               ).get("k") or 0) - 1.0) < 1e-12,
          str(live_rows_of("RAT1")[0]["exchange_orders_json"]))
    FAKE.fail_positions = False
    _c71 = len(calls_for("RAT1"))
    check("71 接口恢复后重试：k 按交易所**现状**冻结 = 3.0（不是旧 k=1）",
          afl.on_reattach("RAT1", "LONG") is True
          and abs(float(json.loads(live_rows_of("RAT1")[0]["exchange_orders_json"]
                                   ).get("k") or 0) - 3.0) < 1e-9,
          str(live_rows_of("RAT1")[0]["exchange_orders_json"]))
    check("71 冻结过程零下单", len(calls_for("RAT1")) == _c71,
          str(calls_for("RAT1")[_c71:]))
    with storage.get_conn() as conn:      # 落定：托管中
        conn.execute("UPDATE trade_positions SET managed=1 WHERE id=?", (_p71,))
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("71 落定后对账：手动加的仓一分不动（仍 3.0、零下单）",
          abs(FAKE.pos_qty("RAT1") - 3.0) < 1e-12
          and len(calls_for("RAT1")) == _c71,
          f"{FAKE.pos_qty('RAT1')} {calls_for('RAT1')[_c71:]}")
    # 对照（证明上面不是空转）：k 若还是旧的 1.0，对账真会把 3.0 砍回 1.0
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        lc.execute("UPDATE live_positions SET quantity=3.0, exchange_orders_json=? "
                   "WHERE token='RAT1'",
                   (json.dumps({"model": "ratio", "k": 1.0}),))
    _c71b = len(calls_for("RAT1"))
    afl.sync_alphafox_live(True, 0.0, 3.0)
    check("71 对照：旧 k 确实会砍仓（这就是'不落定'要防的事）",
          abs(FAKE.pos_qty("RAT1") - 1.0) < 1e-9
          and len(calls_for("RAT1")) > _c71b,
          f"{FAKE.pos_qty('RAT1')} {calls_for('RAT1')[_c71b:]}")
    set_bal(BAL)

    print("== 75. 实盘「没跟上」的原因登记：写 / 读 / 建起来后清除 ==")
    afl._clear_skip("SKIP1", "LONG")
    check("初始为空（没跳过过就不提示）",
          afl.live_skip_reason("SKIP1", "LONG") == "")
    afl._note_skip("SKIP1", "LONG", "补开跳过：实盘开关关闭")
    check("记下后能读出来",
          afl.live_skip_reason("SKIP1", "LONG") == "补开跳过：实盘开关关闭",
          repr(afl.live_skip_reason("SKIP1", "LONG")))
    check("按 token|side 分键，不串味",
          afl.live_skip_reason("SKIP1", "SHORT") == "")
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        afl._write_live_open(lc, 999999, {
            "token": "SKIP1", "symbol": "SKIP1USDT", "side": "LONG",
            "strategy": "alphafox", "entry_price": 1.0, "quantity": 1.0,
            "leverage": 3, "margin_usd": 0.33, "tp1_done": 0, "tp2_done": 0,
            "status": "OPEN"})
    check("实盘记录建起来 → 原因自动清掉（页面提示随之消失）",
          afl.live_skip_reason("SKIP1", "LONG") == "",
          repr(afl.live_skip_reason("SKIP1", "LONG")))
    # 这一笔结束了也要清：否则交易员重开同币时，新纸盘仓在下一轮对账跑到之前
    # 会把**上一笔**的原因显示出来
    alphafox_trade.init_db()
    PRICE["SKIP2"] = 100.0
    insert_paper("SKIP2", qty=1.0, entry=100.0)
    afl._note_skip("SKIP2", "LONG", "补开跳过：实盘开关关闭")
    check("前置：原因已记下", afl.live_skip_reason("SKIP2", "LONG") != "")
    afl._close_paper_leg("SKIP2", "LONG", "测试收尾")
    check("纸盘那一笔收尾 → 原因一起清掉",
          afl.live_skip_reason("SKIP2", "LONG") == "",
          repr(afl.live_skip_reason("SKIP2", "LONG")))

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
