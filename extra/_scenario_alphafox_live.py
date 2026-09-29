"""边界场景验证（补充单测未覆盖的路径）：
S1 交易员量序列 1→2→3→2→1→0 账本自洽（比例模型：目标 = 交易员量 × k）
S2 对账补仓（交易所数量小于目标 → 补到目标）
S3 交易所持仓查询不可用 → 对账/平仓延迟不动作
S4 REDUCE 时交易所混有手动仓 → 只减**我们自己那一份**（不越界）
S5 CLOSE 时交易所数量大于我们的记录 → 只平我们的部分
"""
import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

_TMP = tempfile.mkdtemp(prefix="scen_alf_")
config.DB_PATH = os.path.join(_TMP, "main.db")
config.SUPERTREND_DB = os.path.join(_TMP, "supertrend.db")
config.LIVE_DB_PATH = os.path.join(_TMP, "live.db")
config.ALPHAFOX_DB = os.path.join(_TMP, "alphafox.db")
config.BINANCE_HEDGE_MODE = True

import storage
import alphafox_trade
import alphafox_live

with storage.get_conn() as conn:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(storage.SCHEMA)
    conn.executescript("""
    DROP TABLE IF EXISTS trade_signal_locks;
    CREATE TABLE trade_signal_locks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL,
        signal_key TEXT NOT NULL, strategy TEXT DEFAULT 'agent',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(token, signal_key, strategy));
    """)
storage.init_onchain_live_db()
alphafox_trade.init_db()
with storage.get_conn(config.SUPERTREND_DB) as conn:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS supertrend_tokens (
        token TEXT PRIMARY KEY, status TEXT DEFAULT 'ACTIVE',
        live_mode INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS supertrend_status (token TEXT PRIMARY KEY);
    """)

# 比例模型：我的可用余额 1000U ÷ 交易员保证金 1000U → k = 1（纸盘量 == 目标量，
# 场景里"1 份"就是 1 个交易单位）
BAL = 1000.0
MARGIN = 1000.0
LEV = 50
PRICE = {}


class FakeExchange:
    def __init__(self):
        self.positions = []
        self.calls = []
        self.fail_positions = False
        self._last_error_msg = ""
        self.last_error = ""
        self.balance = BAL

    def get_account_info(self):
        return {"available_balance": self.balance, "equity": self.balance}

    def get_balance(self):
        return self.balance

    def get_positions(self):
        if self.fail_positions:
            raise RuntimeError("api down")
        return [dict(p) for p in self.positions if abs(p["quantity"]) > 1e-9]

    def get_last_error(self):
        return self.last_error

    def set_leverage(self, symbol, lev):
        self.calls.append(("set_lev", symbol, lev))
        return True

    def _open(self, token, side, qty):
        price = PRICE.get(token, 100.0)
        self.calls.append(("open", side, token, round(qty, 8)))
        for p in self.positions:
            if p["symbol"] == f"{token}USDT" and p["position_side"] == side:
                p["quantity"] += qty
                p["mark_price"] = price
                return {"order_id": 1, "status": "FILLED", "price": price,
                        "quantity": qty}
        self.positions.append({"symbol": f"{token}USDT", "quantity": qty,
                               "entry_price": price, "mark_price": price,
                               "unrealized_pnl": 0.0, "leverage": LEV,
                               "position_side": side})
        return {"order_id": 2, "status": "FILLED", "price": price,
                "quantity": qty}

    def market_open_long(self, token, qty):
        return self._open(token, "LONG", qty)

    def market_open_short(self, token, qty):
        return self._open(token, "SHORT", qty)

    def close_position_market(self, token, qty, position_side="LONG"):
        self.calls.append(("close", token, round(qty, 8), position_side))
        for p in self.positions:
            if p["symbol"] == f"{token}USDT" and p["position_side"] == position_side:
                p["quantity"] = max(p["quantity"] - qty, 0)
        return {"order_id": 3, "price": PRICE.get(token, 100.0),
                "quantity": qty, "status": "FILLED"}

    def pos_qty(self, token, side="LONG"):
        for p in self.positions:
            if p["symbol"] == f"{token}USDT" and p["position_side"] == side:
                return p["quantity"]
        return 0.0

    def bump(self, token, side, delta):
        for p in self.positions:
            if p["symbol"] == f"{token}USDT" and p["position_side"] == side:
                p["quantity"] += delta
                return
        self.positions.append({"symbol": f"{token}USDT", "quantity": delta,
                               "entry_price": PRICE.get(token, 100.0),
                               "mark_price": PRICE.get(token, 100.0),
                               "unrealized_pnl": 0.0, "leverage": 20,
                               "position_side": side})


FAKE = FakeExchange()
alphafox_live.exchange = FAKE

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def insert_paper(token, side="LONG", qty=1.0, entry=100.0):
    import json as _json
    snap = _json.dumps({"k_qty": 1.0, "trader_margin": MARGIN})
    with storage.get_conn() as conn:
        conn.execute(
            "INSERT INTO trade_positions (token, symbol, side, status, mode, "
            "margin_amount, leverage, notional, quantity, entry_price, "
            "current_price, strategy, closed_qty, signal_snapshot) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (token, f"{token}USDT", side, "OPEN", "paper", 50.0, LEV,
             50.0 * LEV, qty, entry, entry, "alphafox", 0.0, snap))


def set_paper_qty(token, qty, closed=0.0):
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET quantity=?, closed_qty=?, "
            "updated_at=CURRENT_TIMESTAMP WHERE token=? AND strategy='alphafox' "
            "AND status IN ('OPEN','PARTIAL')", (qty, closed, token))


def close_paper(token):
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
            "WHERE token=? AND strategy='alphafox' AND status IN ('OPEN','PARTIAL')",
            (token,))


def sig(action, token, side="LONG"):
    return {"action": action, "symbol": f"{token}USDT", "side": side,
            "executed": True, "leverage": LEV, "trader_margin": MARGIN}


def set_bal(v):
    FAKE.balance = float(v)
    alphafox_live._BAL_CACHE.update({"ts": 0.0, "v": 0.0})


def live_row(token):
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        r = lc.execute("SELECT * FROM live_positions WHERE token=? "
                       "ORDER BY id DESC LIMIT 1", (token,)).fetchone()
    return dict(r) if r else {}


print("== S1. 交易员量序列 1→2→3→2→1→0（账本自洽，k=1）==")
T = "AAA"
PRICE[T] = 100.0
insert_paper(T, qty=1.0, entry=100.0)
one = 1.0                      # k=1 → 交易所 1 个交易单位就是纸盘/交易员的 1
for i in (1, 2, 3):
    _act = "OPEN" if i == 1 else "ADD"
    set_paper_qty(T, float(i))
    alphafox_live.apply_signal(sig(_act, T), True, 0.0, LEV)
    check(f"交易员 {i} → 交易所 {i}",
          abs(FAKE.pos_qty(T) - i * one) < 1e-9, str(FAKE.pos_qty(T)))
row = live_row(T)
check("live quantity = 3", abs(float(row["quantity"]) - 3 * one) < 1e-9,
      str(row["quantity"]))
for exp_q in (2, 1):
    set_paper_qty(T, float(exp_q))
    alphafox_live.apply_signal(sig("REDUCE", T), True, 0.0, LEV)
    check(f"交易员减到 {exp_q} → 交易所 {exp_q}",
          abs(FAKE.pos_qty(T) - exp_q * one) < 1e-9, str(FAKE.pos_qty(T)))
row = live_row(T)
check("比例 k 始终冻结在记录里",
      abs(float(json.loads(row.get("exchange_orders_json") or "{}").get("k") or 0)
          - 1.0) < 1e-12, row.get("exchange_orders_json"))
close_paper(T)                 # 交易员减到零 → 纸盘整笔 CLOSED
alphafox_live.apply_signal(sig("REDUCE", T), True, 0.0, LEV)
check("减到 0：交易所清零", FAKE.pos_qty(T) == 0.0, str(FAKE.pos_qty(T)))
check("live 记录 CLOSED", live_row(T).get("status") == "CLOSED",
      live_row(T).get("status"))

print("== S2. 对账补仓（交易所数量小于目标 → 补到目标）==")
T = "BBB"
PRICE[T] = 100.0
insert_paper(T, qty=1.0, entry=100.0)
alphafox_live._rounds_done = 99
alphafox_live.apply_signal(sig("OPEN", T), True, 0.0, LEV)
set_paper_qty(T, 2.0)
alphafox_live.apply_signal(sig("ADD", T), True, 0.0, LEV)
check("前置：交易所 2.0", abs(FAKE.pos_qty(T) - 2 * one) < 1e-9,
      str(FAKE.pos_qty(T)))
FAKE.bump(T, "LONG", -one)  # 模拟被平掉一份（外部原因）
check("前置：交易所只剩 1.0", abs(FAKE.pos_qty(T) - one) < 1e-9,
      str(FAKE.pos_qty(T)))
alphafox_live.sync_alphafox_live(True, 0.0, LEV)
check("对账补回到 2.0", abs(FAKE.pos_qty(T) - 2 * one) < 1e-9,
      str(FAKE.pos_qty(T)))

print("== S3. 交易所持仓查询不可用 → 延迟不动作 ==")
before_status = live_row(T).get("status")
FAKE.fail_positions = True
alphafox_live.sync_alphafox_live(True, 0.0, LEV)
check("对账：API 失败不改 live 记录", live_row(T).get("status") == before_status)
close_paper(T)
alphafox_live.apply_signal(sig("CLOSE", T), True, 0.0, LEV)
check("CLOSE：API 失败不标记 CLOSED（下轮重试）",
      live_row(T).get("status") == before_status, live_row(T).get("status"))
FAKE.fail_positions = False
alphafox_live.apply_signal(sig("CLOSE", T), True, 0.0, LEV)
check("API 恢复后 CLOSE 正常收敛", FAKE.pos_qty(T) == 0.0
      and live_row(T).get("status") == "CLOSED")

print("== S4. REDUCE 时交易所混有手动仓 → 只减我们自己那份 ==")
T = "CCC"
PRICE[T] = 100.0
insert_paper(T, qty=1.0, entry=100.0)
alphafox_live.apply_signal(sig("OPEN", T), True, 0.0, LEV)
set_paper_qty(T, 2.0)
alphafox_live.apply_signal(sig("ADD", T), True, 0.0, LEV)
FAKE.bump(T, "LONG", 5.0)  # 手动仓混入（合并成同一持仓）
merged = FAKE.pos_qty(T)
set_paper_qty(T, 2.0, closed=1.0)          # 净量 1.0 → 交易员减半 → 目标 1.0
alphafox_live.apply_signal(sig("REDUCE", T), True, 0.0, LEV)
check("只减我们自己那份（记录 2.0 → 目标 1.0 = 减 1.0），手动仓不动",
      abs(FAKE.pos_qty(T) - (merged - 1.0)) < 1e-9, str(FAKE.pos_qty(T)))

print("== S5. CLOSE 时交易所数量大于我们的记录 → 只平我们的部分 ==")
T2 = "DDD"
PRICE[T2] = 100.0
insert_paper(T2, qty=1.0, entry=100.0)
alphafox_live.apply_signal(sig("OPEN", T2), True, 0.0, LEV)
FAKE.bump(T2, "LONG", 4.0)  # 手动仓混入
merged2 = FAKE.pos_qty(T2)
close_paper(T2)
alphafox_live.apply_signal(sig("CLOSE", T2), True, 0.0, LEV)
check("只平我们的 1.0，手动部分保留",
      abs(FAKE.pos_qty(T2) - (merged2 - one)) < 1e-9, str(FAKE.pos_qty(T2)))
check("live 记录 CLOSED", live_row(T2).get("status") == "CLOSED")

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
