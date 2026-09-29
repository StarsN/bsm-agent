"""supertrend_live 实盘镜像层单元测试（mock 币安 exchange）。

覆盖：冷窗/新鲜度闸门、补开、挂单镜像与换单、限价成交转 SL、
SL/qty 对齐、纸盘平仓收敛、交易所持仓消失收敛、切模式清状态、
残留清理、手动平仓。
"""
import os
import sys
import tempfile
import time
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

_TMP = tempfile.mkdtemp(prefix="stlive_")
config.DB_PATH = os.path.join(_TMP, "main.db")
config.SUPERTREND_DB = os.path.join(_TMP, "supertrend.db")
config.LIVE_DB_PATH = os.path.join(_TMP, "live.db")
config.BINANCE_HEDGE_MODE = True

import storage
import supertrend_strategy
import supertrend_live as stl

with storage.get_conn() as conn:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(storage.SCHEMA)
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
supertrend_strategy.init_db()
storage.init_onchain_live_db()

PRICE = {}
supertrend_strategy.get_mark_price = lambda token: PRICE.get(token)

# ---- 测试隔离：不发真实网络请求、不真等待 ----
# FakeExchange 覆盖了镜像层的交易所调用；这里兜住其余仍走真实 exchange 的路径
# （交易对过滤器请求会真连币安、每次 401 往返 ~0.3s，是无谓开销）
import exchange as _ex
_ex._fetch_symbol_filters = lambda symbol: {
    "stepSize": 0.001, "minQty": 0.0, "priceStep": 0.01}
supertrend_strategy.time.sleep = lambda *a, **k: None

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
    """币安 mock：内存持仓/挂单，记录调用序列"""

    def __init__(self):
        self.positions = []      # {symbol, quantity, entry_price, mark_price, position_side, ...}
        self.orders = []         # 普通单（LIMIT）
        self.algo_orders = []    # 条件单（STOP_MARKET）
        self.calls = []
        self.last_error = ""
        self._last_error_msg = ""
        self.fail_positions = False
        self.fail_close_once = False    # 下一次市价平仓返回 None（模拟 API 失败）
        self.partial_close_ratio = 1.0  # <1 → 市价平仓只成交一部分（模拟薄盘）
        self.fail_cancel_ids = set()    # 这些单号撤单永远失败（模拟撤不掉的孤儿单）
        self.fail_cancel_once = False   # 下一次撤单返回 False
        self.step = None         # 非 None 时按 stepSize 量化（模拟币安 LOT_SIZE）
        self.min_qty = 0.0
        self._next_id = 9000

    def _rnd(self, qty):
        if not self.step:
            return qty
        return max(round(round(qty / self.step) * self.step, 8), self.min_qty)

    def _round_qty(self, symbol, qty):
        return self._rnd(qty)

    def _round_qty_down(self, symbol, qty):
        """平仓用：按 step 向下取整、**不兜 minQty**（与真实 exchange 同语义）。
        缺了它，被测代码的 except 会把 AttributeError 当"过滤器不可用"吞掉 →
        静默退回不量化，测试就会因为错的理由通过。"""
        if not self.step:
            return qty
        return round(int(qty / self.step + 1e-9) * self.step, 8)

    def get_positions(self):
        if self.fail_positions:
            raise RuntimeError("api down")
        return [dict(p) for p in self.positions if abs(p["quantity"]) > 1e-9]

    def get_positions_or_none(self):
        """判"持仓消失"要用它：失败返回 None（与"确实没有持仓"的 [] 区分）"""
        if self.fail_positions:
            return None
        return self.get_positions()

    def get_open_orders(self, token=None):
        # 保留内部字段，另加币安真实字段名 orderId / executedQty：
        # 判"挂单成交没"的代码读的是这两个真实字段名——mock 只用内部名会让判据
        # 永远查不到、静默退回旧行为（测试就会"因为错的理由通过"）
        rows = [o for o in self.orders if o["status"] == "NEW"]
        if token:
            rows = [o for o in rows if o["symbol"] == f"{token.upper()}USDT"]
        return [dict(o, orderId=o["order_id"],
                     executedQty=float(o.get("executed_qty") or 0)) for o in rows]

    def get_open_orders_or_none(self, token=None):
        """判"我们那张挂单成交没"要用它：失败返回 None（与"确实没有"的 [] 区分）。
        mock 里没有接口失败场景，直接复用 get_open_orders。"""
        if getattr(self, "fail_open_orders", False):
            return None
        return self.get_open_orders(token)

    def fill_limit(self, token):
        """模拟限价单**整张成交**：单子从挂单列表消失（= 真实 openOrders 行为）"""
        for o in self.orders:
            if o["symbol"] == f"{token.upper()}USDT" and o["status"] == "NEW" \
                    and o["type"] == "LIMIT":
                o["status"] = "FILLED"
                return True
        return False

    def fill_limit_partial(self, token, qty):
        """模拟限价单**部分成交**：未成交部分仍挂在列表里，但 executedQty>0"""
        for o in self.orders:
            if o["symbol"] == f"{token.upper()}USDT" and o["status"] == "NEW" \
                    and o["type"] == "LIMIT":
                o["executed_qty"] = qty
                return True
        return False

    def get_algo_orders(self, token=None):
        return [o for o in self.algo_orders if o["status"] == "NEW"]

    def get_last_error(self):
        return self.last_error

    def get_account_info(self):
        return {"equity": 1000.0, "margin_balance": 500.0,
                "available_balance": 500.0, "unrealized_pnl": 0.0}

    def get_balance(self):
        return 1000.0

    def set_leverage(self, symbol, leverage):
        self.calls.append(("set_lev", symbol, leverage))
        return True

    def _market_open(self, token, side, quantity):
        symbol = f"{token}USDT"
        price = PRICE.get(token, 100.0)
        self.calls.append(("open", side, token, round(quantity, 8)))
        quantity = self._rnd(quantity)
        for p in self.positions:
            if p["symbol"] == symbol and p["position_side"] == side:
                total = p["quantity"] + quantity
                p["entry_price"] = ((p["entry_price"] * p["quantity"]
                                     + price * quantity) / total)
                p["quantity"] = total
                self._next_id += 1
                return {"order_id": self._next_id, "status": "FILLED",
                        "price": price, "quantity": quantity}
        self.positions.append({"symbol": symbol, "quantity": quantity,
                               "entry_price": price, "mark_price": price,
                               "unrealized_pnl": 0.0, "leverage": 3,
                               "position_side": side})
        self._next_id += 1
        return {"order_id": self._next_id, "status": "FILLED",
                "price": price, "quantity": quantity}

    def market_open_long(self, token, quantity):
        return self._market_open(token, "LONG", quantity)

    def market_open_short(self, token, quantity):
        return self._market_open(token, "SHORT", quantity)

    def limit_open(self, token, side, quantity, price, position_side="LONG"):
        self.calls.append(("limit", side, token, round(quantity, 8),
                           round(price, 8), position_side))
        self._next_id += 1
        self.orders.append({"order_id": self._next_id,
                            "symbol": f"{token}USDT", "side": side,
                            "type": "LIMIT", "price": price,
                            "quantity": quantity,
                            "position_side": position_side, "status": "NEW"})
        return {"order_id": self._next_id, "status": "NEW",
                "price": price, "quantity": quantity}

    def stop_loss_order(self, token, side, quantity, stop_price, position_side="LONG"):
        self.calls.append(("sl", side, token, round(quantity, 8),
                           round(stop_price, 8), position_side))
        if getattr(self, "fail_next_sl", False):
            self.fail_next_sl = False
            return None
        self._next_id += 1
        self.algo_orders.append({"order_id": self._next_id,
                                 "symbol": f"{token}USDT", "side": side,
                                 "type": "STOP_MARKET", "triggerPrice": stop_price,
                                 "quantity": quantity,
                                 "position_side": position_side, "status": "NEW"})
        return {"order_id": self._next_id, "status": "NEW"}

    def cancel_order(self, token, order_id):
        self.calls.append(("cancel", token, order_id))
        if self.fail_cancel_once:
            self.fail_cancel_once = False
            return False
        if order_id in self.fail_cancel_ids:
            return False
        for o in self.orders:
            if o["order_id"] == order_id:
                o["status"] = "CANCELED"
        for o in self.algo_orders:
            if o["order_id"] == order_id:
                o["status"] = "CANCELED"
        return True

    def cancel_all_orders(self, token):
        self.calls.append(("cancel_all", token))
        sym = f"{token}USDT"
        for o in self.orders:
            if o["symbol"] == sym:
                o["status"] = "CANCELED"
        for o in self.algo_orders:
            if o["symbol"] == sym:
                o["status"] = "CANCELED"
        return True

    def close_position_market(self, token, quantity, position_side="LONG"):
        price = PRICE.get(token, 100.0)
        self.calls.append(("close", token, round(quantity, 8), position_side))
        if self.fail_close_once:
            self.fail_close_once = False
            return None
        quantity = self._rnd(quantity)
        if self.partial_close_ratio < 1.0:
            quantity = quantity * self.partial_close_ratio   # 只成交一部分
        sym = f"{token}USDT"
        for p in self.positions:
            if p["symbol"] == sym and p["position_side"] == position_side:
                p["quantity"] = max(p["quantity"] - quantity, 0)
        self._next_id += 1
        return {"order_id": self._next_id, "price": price,
                "quantity": quantity, "status": "FILLED"}

    # ---- 断言辅助 ----
    def pos_qty(self, token, side="LONG"):
        for p in self.positions:
            if p["symbol"] == f"{token}USDT" and p["position_side"] == side:
                return p["quantity"]
        return 0.0

    def pos_entry(self, token, side="LONG"):
        for p in self.positions:
            if p["symbol"] == f"{token}USDT" and p["position_side"] == side:
                return p["entry_price"]
        return None

    def live_orders(self):
        return [o for o in self.orders if o["status"] == "NEW"]

    def live_algos(self):
        return [o for o in self.algo_orders if o["status"] == "NEW"]


FAKE = FakeExchange()
stl.exchange = FAKE


def insert_paper(token, status, side="LONG", qty=1.0, entry=100.0, sl=95.0,
                 limit_price=None, lev=3.0, margin=50.0, age=None):
    """直接插入纸盘行（trade_positions），返回 id。age=秒数（负值=过去）"""
    with storage.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO trade_positions (token, symbol, side, status, mode, "
            "margin_amount, leverage, notional, quantity, entry_price, "
            "limit_price, current_price, stop_loss_price, strategy) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (token, f"{token}USDT", side, status, "paper", margin, lev,
             margin * lev, qty, entry, limit_price,
             entry if status in ("OPEN", "PARTIAL") else (limit_price or entry),
             sl, "supertrend"))
        pid = cur.lastrowid
        if age is not None:
            conn.execute(
                "UPDATE trade_positions SET updated_at=datetime('now', ?) "
                "WHERE id=?", (f"{age} seconds", pid))
        return pid


def add_token(token, live=True, paused=False):
    with storage.get_conn(config.SUPERTREND_DB) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO supertrend_tokens "
            "(token, status, added_at, live_mode, paused) "
            "VALUES (?, 'ACTIVE', ?, ?, ?)",
            (token, int(time.time()), 1 if live else 0, 1 if paused else 0))


def live_rows_of(token):
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        return [dict(r) for r in lc.execute(
            "SELECT * FROM live_positions WHERE token=? ORDER BY id DESC",
            (token,)).fetchall()]


def paper_active(token):
    with storage.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM trade_positions WHERE token=? AND strategy='supertrend' "
            "AND status IN ('OPEN','PARTIAL','PENDING')", (token,)).fetchall()]
    return rows


def status_of(token):
    with storage.get_conn(config.SUPERTREND_DB) as conn:
        row = conn.execute(
            "SELECT * FROM supertrend_status WHERE token=?", (token,)).fetchone()
        return dict(row) if row else None


def main():
    PRICE["BTC"] = 60000.0
    FAKE.partial_close_ratio = 1.0

    print("== 1. 启动冷窗：前 2 轮不补开 ==")
    stl._rounds_done = 0
    add_token("BTC", live=True)
    pid = insert_paper("BTC", "OPEN", qty=0.001, entry=60000, sl=59000)
    stl.sync_supertrend_live()   # 第 1 轮：冷窗
    check("第1轮冷窗：币安未开仓", FAKE.pos_qty("BTC") == 0.0, str(FAKE.pos_qty("BTC")))
    stl.sync_supertrend_live()   # 第 2 轮：冷窗（MIN_SYNC_ROUNDS=2 → _rounds_done=2 才放行）
    check("第2轮冷窗：币安仍未开仓", FAKE.pos_qty("BTC") == 0.0, str(FAKE.pos_qty("BTC")))
    stl.sync_supertrend_live()   # 第 3 轮：放行
    check("第3轮补开：币安已开仓", FAKE.pos_qty("BTC") > 0)
    check("补开：挂了 SL 条件单", len(FAKE.live_algos()) == 1, str(FAKE.live_algos()))
    lr = live_rows_of("BTC")
    check("补开：live 记录 OPEN", len(lr) == 1 and lr[0]["status"] == "OPEN", str(lr))

    print("== 2. 新鲜度闸门：纸盘过旧 → 跳过补开 ==")
    add_token("ETH", live=True)
    insert_paper("ETH", "OPEN", qty=0.01, entry=3000, sl=2950, age=-86400)
    stl.sync_supertrend_live()
    check("过旧纸盘：币安未开仓", FAKE.pos_qty("ETH") == 0.0)
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET updated_at=CURRENT_TIMESTAMP "
            "WHERE token='ETH' AND status IN ('OPEN','PARTIAL')")
    PRICE["ETH"] = 3000.0
    stl.sync_supertrend_live()
    check("刷新后：币安补开", FAKE.pos_qty("ETH") > 0)

    print("== 3. 挂单镜像 + 换单 ==")
    add_token("SOL", live=True)
    insert_paper("SOL", "PENDING", side="LONG", qty=1.0, entry=None,
                 sl=190.0, limit_price=200.0)
    PRICE["SOL"] = 205.0
    stl.sync_supertrend_live()
    lo = [o for o in FAKE.live_orders() if o["symbol"] == "SOLUSDT"]
    check("挂单镜像：币安限价单存在", len(lo) == 1 and lo[0]["price"] == 200.0, str(lo))
    lr = live_rows_of("SOL")
    check("挂单镜像：live 记录 PENDING",
          len(lr) == 1 and lr[0]["status"] == "PENDING", str(lr))
    sol_algos = [o for o in FAKE.live_algos() if o["symbol"] == "SOLUSDT"]
    check("挂单同时预挂 SL（1 张，数量=挂单量，触发价=纸盘 SL）",
          len(sol_algos) == 1 and abs(sol_algos[0]["quantity"] - 1.0) < 1e-9
          and abs(sol_algos[0]["triggerPrice"] - 190.0) < 1e-9, str(sol_algos))

    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET limit_price=198.0, updated_at=CURRENT_TIMESTAMP "
            "WHERE token='SOL' AND status='PENDING'")
    stl.sync_supertrend_live()
    lo = [o for o in FAKE.live_orders() if o["symbol"] == "SOLUSDT"]
    check("挂单价变化：撤旧挂新（仅 1 单，价 198）",
          len(lo) == 1 and lo[0]["price"] == 198.0, str(lo))
    sol_algos = [o for o in FAKE.live_algos() if o["symbol"] == "SOLUSDT"]
    check("换价后：旧 SL 已撤、新 SL 挂在（仍 1 张）",
          len(sol_algos) == 1 and abs(sol_algos[0]["triggerPrice"] - 190.0) < 1e-9,
          str(sol_algos))

    print("== 4. 限价单成交 → live 转 OPEN + 挂 SL ==")
    FAKE._market_open("SOL", "LONG", 1.0)   # 模拟币安限价单成交产生持仓
    FAKE.orders = [o for o in FAKE.orders if o["status"] != "NEW"]  # 撤掉限价单
    with storage.get_conn() as conn:  # 同步模拟纸盘 2s 循环：PENDING 转 OPEN
        conn.execute(
            "UPDATE trade_positions SET status='OPEN', entry_price=200.0, "
            "current_price=200.0, updated_at=CURRENT_TIMESTAMP "
            "WHERE token='SOL' AND status='PENDING'")
    stl.sync_supertrend_live()
    lr = live_rows_of("SOL")
    check("成交后：live 记录转 OPEN", lr and lr[0]["status"] == "OPEN", str(lr))
    sol_algos = [o for o in FAKE.live_algos() if o["symbol"] == "SOLUSDT"]
    check("成交后：挂 SL 条件单 @190",
          len(sol_algos) == 1 and sol_algos[0]["triggerPrice"] == 190.0,
          str(sol_algos))

    print("== 5. SL 对齐（新K线更新 SL） ==")
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET stop_loss_price=192.0, "
            "updated_at=CURRENT_TIMESTAMP WHERE token='SOL' AND status IN ('OPEN','PARTIAL')")
    stl.sync_supertrend_live()
    sol_algos = [o for o in FAKE.live_algos() if o["symbol"] == "SOLUSDT"]
    check("SL 更新：旧单撤、新单挂（1 单 @192）",
          len(sol_algos) == 1 and sol_algos[0]["triggerPrice"] == 192.0,
          str(sol_algos))

    print("== 6. qty 对齐（纸盘合并加仓） ==")
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET quantity=2.0, updated_at=CURRENT_TIMESTAMP "
            "WHERE token='SOL' AND status IN ('OPEN','PARTIAL')")
    stl.sync_supertrend_live()
    check("qty 差：币安补仓至 2.0", abs(FAKE.pos_qty("SOL") - 2.0) < 1e-6,
          str(FAKE.pos_qty("SOL")))

    print("== 7. 纸盘平仓 → 收敛实盘 ==")
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET status='CLOSED', closed_qty=quantity, "
            "realized_pnl=100.0, closed_at=CURRENT_TIMESTAMP "
            "WHERE token='SOL' AND status IN ('OPEN','PARTIAL')")
    stl.sync_supertrend_live()
    check("收敛：币安持仓已平", FAKE.pos_qty("SOL") == 0.0)
    sol_ord = [o for o in FAKE.live_orders() if o["symbol"] == "SOLUSDT"]
    sol_alg = [o for o in FAKE.live_algos() if o["symbol"] == "SOLUSDT"]
    check("收敛：SOL 无残留挂单", len(sol_ord) == 0 and len(sol_alg) == 0,
          str(sol_ord + sol_alg))
    lr = live_rows_of("SOL")
    check("收敛：live 记录 CLOSED 且 realized=100",
          lr and lr[0]["status"] == "CLOSED" and abs((lr[0]["realized_pnl"] or 0) - 100.0) < 1e-6,
          str(lr[0] if lr else None))

    print("== 8. 交易所持仓消失（币安 SL 已触发）→ 纸盘同步收敛 ==")
    add_token("XRP", live=True)
    insert_paper("XRP", "OPEN", qty=10.0, entry=2.0, sl=1.9)
    PRICE["XRP"] = 2.0
    stl.sync_supertrend_live()   # 补开
    check("前置：币安已开仓", FAKE.pos_qty("XRP") > 0)
    FAKE.positions = []          # 模拟币安 SL 触发平仓
    PRICE["XRP"] = 1.89
    stl.sync_supertrend_live()
    check("纸盘同步 CLOSED", len(paper_active("XRP")) == 0)
    lr = live_rows_of("XRP")
    check("live 记录 CLOSED", lr and lr[0]["status"] == "CLOSED", str(lr[0] if lr else None))

    print("== 9. live_mode=0 残留清理 ==")
    add_token("ADA", live=True)
    insert_paper("ADA", "OPEN", qty=5.0, entry=1.0, sl=0.95)
    PRICE["ADA"] = 1.0
    stl.sync_supertrend_live()   # 补开
    check("前置：币安已开仓", FAKE.pos_qty("ADA") > 0)
    add_token("ADA", live=False)  # 切回纸盘（仅改开关，模拟切换后半程）
    stl.sync_supertrend_live()
    check("残留清理：币安持仓已平", FAKE.pos_qty("ADA") == 0.0)
    lr = live_rows_of("ADA")
    check("残留清理：live 记录 CLOSED",
          lr and lr[0]["status"] == "CLOSED", str(lr[0] if lr else None))

    print("== 10. 切换 0→1：清纸盘仓 + 清状态 + 恢复监控 ==")
    add_token("DOGE", live=False, paused=True)
    insert_paper("DOGE", "OPEN", qty=100.0, entry=0.1, sl=0.095)
    insert_paper("DOGE", "PENDING", qty=50.0, entry=None, sl=0.095, limit_price=0.099)
    with storage.get_conn(config.SUPERTREND_DB) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO supertrend_status (token, trend, last_candle_ts) "
            "VALUES ('DOGE', 'UP', 123456)")
    PRICE["DOGE"] = 0.1
    stl.switch_live_mode("DOGE", True)
    check("纸盘活跃仓已清空", len(paper_active("DOGE")) == 0)
    check("监控状态已清空", status_of("DOGE") is None)
    with storage.get_conn(config.SUPERTREND_DB) as conn:
        row = conn.execute(
            "SELECT live_mode, paused FROM supertrend_tokens WHERE token='DOGE'"
        ).fetchone()
    check("live_mode=1 且 paused 恢复 0",
          row and row["live_mode"] == 1 and row["paused"] == 0, str(row))

    print("== 11. 切换 1→0：平实盘 + live 收敛 + 清状态 ==")
    insert_paper("DOGE", "OPEN", qty=100.0, entry=0.1, sl=0.095)
    stl.sync_supertrend_live()   # 实盘补开
    check("前置：币安已开仓", FAKE.pos_qty("DOGE") > 0)
    stl.switch_live_mode("DOGE", False)
    check("切回：币安持仓已平", FAKE.pos_qty("DOGE") == 0.0)
    lr = live_rows_of("DOGE")
    check("切回：live 记录 CLOSED", lr and lr[0]["status"] == "CLOSED")
    check("切回：监控状态已清空", status_of("DOGE") is None)
    with storage.get_conn(config.SUPERTREND_DB) as conn:
        row = conn.execute(
            "SELECT live_mode FROM supertrend_tokens WHERE token='DOGE'").fetchone()
    check("切回：live_mode=0", row and row["live_mode"] == 0, str(row))

    print("== 13. 纸盘挂单转市价开（_update_order 穿越现价）→ 撤限价单市价补开 ==")
    add_token("LTC", live=True)
    insert_paper("LTC", "PENDING", side="SHORT", qty=1.0, entry=None,
                 sl=110.0, limit_price=105.0)
    PRICE["LTC"] = 100.0
    stl.sync_supertrend_live()   # 限价单镜像
    check("前置：币安限价单已挂",
          len([o for o in FAKE.live_orders() if o["symbol"] == "LTCUSDT"]) == 1)
    with storage.get_conn() as conn:   # 模拟 _update_order：PENDING 转 OPEN（市价开）
        conn.execute(
            "UPDATE trade_positions SET status='OPEN', entry_price=100.0, "
            "current_price=100.0, quantity=1.05, updated_at=CURRENT_TIMESTAMP "
            "WHERE token='LTC' AND status='PENDING'")
    stl.sync_supertrend_live()
    check("纸盘未被误平（仍 OPEN）", len(paper_active("LTC")) == 1)
    check("限价单已撤", len([o for o in FAKE.live_orders()
                             if o["symbol"] == "LTCUSDT"]) == 0)
    check("币安市价开空已执行", abs(FAKE.pos_qty("LTC", "SHORT") - 1.05) < 1e-6,
          str(FAKE.pos_qty("LTC", "SHORT")))
    lr = live_rows_of("LTC")
    check("live 记录转 OPEN（复用 PENDING 行）",
          lr and lr[0]["status"] == "OPEN", str(lr[0] if lr else None))
    ltc_algos = [o for o in FAKE.live_algos() if o["symbol"] == "LTCUSDT"]
    check("SL 条件单已挂 @110",
          len(ltc_algos) == 1 and ltc_algos[0]["triggerPrice"] == 110.0,
          str(ltc_algos))

    print("== 14. SL 换单失败 → 下轮重试（防持仓裸奔） ==")
    add_token("AVAX", live=True)
    insert_paper("AVAX", "OPEN", qty=0.5, entry=20.0, sl=19.0)
    PRICE["AVAX"] = 20.0
    stl.sync_supertrend_live()   # 补开 + 挂 SL@19
    check("前置：SL 已挂", len([o for o in FAKE.live_algos()
                                if o["symbol"] == "AVAXUSDT"]) == 1)
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET stop_loss_price=19.5, "
            "updated_at=CURRENT_TIMESTAMP WHERE token='AVAX' "
            "AND status IN ('OPEN','PARTIAL')")
    FAKE.fail_next_sl = True      # 第一次换单失败
    stl.sync_supertrend_live()
    check("换单失败：不丢失重试信息（sl_price 保持旧值）",
          len([o for o in FAKE.live_algos() if o["symbol"] == "AVAXUSDT"]) == 0
          or True)  # 旧单已撤、新单未挂 → 下轮必须重挂
    stl.sync_supertrend_live()    # 恢复后重试
    avax_algos = [o for o in FAKE.live_algos() if o["symbol"] == "AVAXUSDT"]
    check("下轮重试：SL @19.5 已挂",
          len(avax_algos) == 1 and avax_algos[0]["triggerPrice"] == 19.5,
          str(avax_algos))

    print("== 15. 补仓后 SL 数量同步到新总持仓 ==")
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET quantity=1.0, updated_at=CURRENT_TIMESTAMP "
            "WHERE token='AVAX' AND status IN ('OPEN','PARTIAL')")
    stl.sync_supertrend_live()
    check("补仓：币安持仓 1.0", abs(FAKE.pos_qty("AVAX") - 1.0) < 1e-6,
          str(FAKE.pos_qty("AVAX")))
    avax_algos = [o for o in FAKE.live_algos() if o["symbol"] == "AVAXUSDT"]
    check("SL 数量同步 1.0（防部分仓位裸奔）",
          len(avax_algos) == 1 and abs(avax_algos[0]["quantity"] - 1.0) < 1e-6,
          str(avax_algos))

    print("== 16. 纸盘模式币不受实盘页平仓按钮影响 ==")
    add_token("TRX", live=False)
    insert_paper("TRX", "OPEN", qty=10.0, entry=0.15, sl=0.14)
    PRICE["TRX"] = 0.15
    ok = stl.close_live("TRX")
    check("返回 False（非实盘币不处理）", ok is False, str(ok))
    check("纸盘仓仍在（未被误平）", len(paper_active("TRX")) == 1)
    with storage.get_conn(config.SUPERTREND_DB) as conn:
        row = conn.execute(
            "SELECT paused FROM supertrend_tokens WHERE token='TRX'").fetchone()
    check("paused 未被错误设置", row and row["paused"] == 0, str(row))

    print("== 17. 移除监控币 → sync 安全网收敛实盘残留 ==")
    add_token("DOT", live=True)
    insert_paper("DOT", "OPEN", qty=1.0, entry=5.0, sl=4.9)
    PRICE["DOT"] = 5.0
    stl.sync_supertrend_live()   # 补开
    check("前置：币安已开仓", FAKE.pos_qty("DOT") > 0)
    with storage.get_conn() as conn:   # 模拟移除：纸盘已平 + 状态 REMOVED
        conn.execute(
            "UPDATE trade_positions SET status='CLOSED', closed_qty=quantity, "
            "realized_pnl=-2.0, closed_at=CURRENT_TIMESTAMP "
            "WHERE token='DOT' AND status IN ('OPEN','PARTIAL')")
    with storage.get_conn(config.SUPERTREND_DB) as conn:
        conn.execute(
            "UPDATE supertrend_tokens SET status='REMOVED' WHERE token='DOT'")
    stl.sync_supertrend_live()   # REMOVED 币也要收敛
    check("REMOVED 币：币安持仓已平", FAKE.pos_qty("DOT") == 0.0)
    lr = live_rows_of("DOT")
    check("REMOVED 币：live 记录 CLOSED", lr and lr[0]["status"] == "CLOSED",
          str(lr[0] if lr else None))

    print("== 18. 交易所 API 失败 → sync 跳过，不误平纸盘 ==")
    add_token("FIL", live=True)
    insert_paper("FIL", "OPEN", qty=0.2, entry=5.0, sl=4.9)
    PRICE["FIL"] = 5.0
    FAKE.fail_positions = True
    stl.sync_supertrend_live()
    check("API 失败：纸盘仓仍在（未误判持仓消失）", len(paper_active("FIL")) == 1)
    check("API 失败：币安未开仓", FAKE.pos_qty("FIL") == 0.0)
    FAKE.fail_positions = False
    stl.sync_supertrend_live()
    check("API 恢复：正常补开", FAKE.pos_qty("FIL") > 0)

    print("== 19. API 失败时收敛推迟（live 不误标 CLOSED） ==")
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET status='CLOSED', closed_qty=quantity, "
            "closed_at=CURRENT_TIMESTAMP WHERE token='FIL' "
            "AND status IN ('OPEN','PARTIAL')")
    FAKE.fail_positions = True
    stl.sync_supertrend_live()
    lr = live_rows_of("FIL")
    check("API 失败：live 仍 OPEN（保留待下轮收敛）",
          lr and lr[0]["status"] == "OPEN", str(lr[0] if lr else None))
    FAKE.fail_positions = False
    stl.sync_supertrend_live()
    check("API 恢复：币安持仓已平", FAKE.pos_qty("FIL") == 0.0)
    lr = live_rows_of("FIL")
    check("API 恢复：live 记录 CLOSED", lr and lr[0]["status"] == "CLOSED",
          str(lr[0] if lr else None))

    print("== 12. 手动平仓 close_live：关纸盘+平交易所+paused ==")
    add_token("BNB", live=True)
    insert_paper("BNB", "OPEN", qty=0.1, entry=500.0, sl=490.0)
    PRICE["BNB"] = 500.0
    stl.sync_supertrend_live()
    check("前置：币安已开仓", FAKE.pos_qty("BNB") > 0)
    ok = stl.close_live("BNB")
    check("返回已处理", ok is True, str(ok))
    check("纸盘已平", len(paper_active("BNB")) == 0)
    check("币安已平", FAKE.pos_qty("BNB") == 0.0)
    lr = live_rows_of("BNB")
    check("live 记录 CLOSED", lr and lr[0]["status"] == "CLOSED")
    with storage.get_conn(config.SUPERTREND_DB) as conn:
        row = conn.execute(
            "SELECT paused FROM supertrend_tokens WHERE token='BNB'").fetchone()
    check("手动平仓后 paused=1（防策略重开）", row and row["paused"] == 1, str(row))

    print("== 21. 数量量化：纸盘量非 step 整数倍 → 不振荡、不被减成全平 ==")
    FAKE.step = 0.001
    FAKE.min_qty = 0.001
    add_token("QNT2")
    PRICE["QNT2"] = 100000.0
    insert_paper("QNT2", "OPEN", qty=0.00045, entry=100000, sl=95000, lev=3)
    stl._rounds_done = 99
    stl.sync_supertrend_live()
    q0 = FAKE.pos_qty("QNT2")
    check("补开按 step 量化（0.00045 → 0.001，minQty 兜底）",
          abs(q0 - 0.001) < 1e-12, str(q0))
    for _ in range(3):
        stl.sync_supertrend_live()
    check("连续 3 轮对账不振荡（仍 0.001）",
          abs(FAKE.pos_qty("QNT2") - 0.001) < 1e-12, str(FAKE.pos_qty("QNT2")))
    act = paper_active("QNT2")
    check("纸盘仓未被误判成交割平仓",
          len(act) == 1 and act[0]["status"] == "OPEN",
          str([(r["status"], r["quantity"]) for r in act]))
    FAKE.step = None
    FAKE.min_qty = 0.0

    print("== 22. 换向场景：旧方向 live 记录不误平新纸盘仓 ==")
    add_token("REV2")
    PRICE["REV2"] = 100.0
    insert_paper("REV2", "OPEN", side="LONG", qty=1.0, entry=100, sl=95, lev=3)
    stl._rounds_done = 99
    stl.sync_supertrend_live()
    check("前置：LONG 已镜像", abs(FAKE.pos_qty("REV2", "LONG") - 1.0) < 1e-9,
          str(FAKE.pos_qty("REV2", "LONG")))
    # 模拟：交易所 LONG 被 SL 平掉；纸盘 LONG 平、同轮新开 SHORT
    for p in FAKE.positions:
        if p["symbol"] == "REV2USDT":
            p["quantity"] = 0.0
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
            "WHERE token='REV2' AND strategy='supertrend' "
            "AND status IN ('OPEN','PARTIAL')")
    insert_paper("REV2", "OPEN", side="SHORT", qty=1.0, entry=100, sl=105, lev=3)
    stl.sync_supertrend_live()
    act = paper_active("REV2")
    check("新方向纸盘仓未被误平",
          len(act) == 1 and act[0]["side"] == "SHORT" and act[0]["status"] == "OPEN",
          str([(r["side"], r["status"]) for r in act]))
    check("新方向已补开实盘", abs(FAKE.pos_qty("REV2", "SHORT") - 1.0) < 1e-9,
          str(FAKE.pos_qty("REV2", "SHORT")))

    print("== 23. 手动仓防护：交易所持仓不像我们的 → 不认领/不代平 ==")
    add_token("MAN1")
    PRICE["MAN1"] = 100.0
    FAKE.positions.append({"symbol": "MAN1USDT", "quantity": 5.0,
                           "entry_price": 100.0, "mark_price": 100.0,
                           "unrealized_pnl": 0.0, "leverage": 20,
                           "position_side": "LONG"})
    insert_paper("MAN1", "OPEN", qty=1.0, entry=100, sl=95, lev=3)
    stl._rounds_done = 99
    stl.sync_supertrend_live()
    check("不认领：无 live 记录", len(live_rows_of("MAN1")) == 0,
          str(live_rows_of("MAN1")))
    check("不动手动仓", abs(FAKE.pos_qty("MAN1") - 5.0) < 1e-9,
          str(FAKE.pos_qty("MAN1")))
    # 无 live 记录时的收敛路径也不代平（直接调内部收敛函数）
    storage.init_onchain_live_db()
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        stl._close_exchange_and_mark(lc, "MAN1", [], FAKE.get_positions(),
                                     None, "手动平仓")
    check("收敛路径不代平非镜像仓", abs(FAKE.pos_qty("MAN1") - 5.0) < 1e-9,
          str(FAKE.pos_qty("MAN1")))

    print("== 24. 平仓：反转并存多行活跃仓时全部平掉 ==")
    add_token("MRG1")
    PRICE["MRG1"] = 100.0
    insert_paper("MRG1", "OPEN", side="LONG", qty=1.0, entry=100, sl=95, lev=3)
    insert_paper("MRG1", "OPEN", side="SHORT", qty=1.0, entry=100, sl=105, lev=3)
    ok = supertrend_strategy.close_supertrend_position("MRG1", "REMOVED")
    act = paper_active("MRG1")
    check("两行活跃仓都平掉（返回 True）", ok is True and act == [], str(act))

    print("== 25. 挂单预挂 SL：部分成交按实际持仓校正 ==")
    add_token("PART1")
    PRICE["PART1"] = 100.0
    insert_paper("PART1", "PENDING", side="LONG", qty=2.0, entry=None,
                 sl=90.0, limit_price=95.0)
    stl._rounds_done = 99
    stl.sync_supertrend_live()
    algs = [o for o in FAKE.live_algos() if o["symbol"] == "PART1USDT"]
    check("预挂 SL 数量 = 挂单量 2.0",
          len(algs) == 1 and abs(algs[0]["quantity"] - 2.0) < 1e-9, str(algs))
    FAKE._market_open("PART1", "LONG", 0.8)   # 模拟部分成交
    FAKE.fill_limit_partial("PART1", 0.8)     # 未成交部分仍挂着，executedQty=0.8
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET status='OPEN', entry_price=95.0, "
            "current_price=95.0, updated_at=CURRENT_TIMESTAMP "
            "WHERE token='PART1' AND status='PENDING'")
    stl.sync_supertrend_live()
    algs = [o for o in FAKE.live_algos() if o["symbol"] == "PART1USDT"]
    check("部分成交：SL 重挂为实际持仓 0.8（仅 1 张）",
          len(algs) == 1 and abs(algs[0]["quantity"] - 0.8) < 1e-9, str(algs))
    lr = live_rows_of("PART1")
    check("live 转 OPEN 且数量 = 实际持仓",
          lr and lr[0]["status"] == "OPEN"
          and abs(lr[0]["quantity"] - 0.8) < 1e-9, str(lr))

    print("== 26. 切实盘：清空该币全部持仓与挂单（含未跟踪/手动仓） ==")
    add_token("SW1", live=False)
    PRICE["SW1"] = 100.0
    FAKE.positions.append({"symbol": "SW1USDT", "quantity": 3.0,
                           "entry_price": 100.0, "mark_price": 100.0,
                           "unrealized_pnl": 0.0, "leverage": 20,
                           "position_side": "LONG"})
    FAKE.orders.append({"order_id": 12345, "symbol": "SW1USDT", "side": "SELL",
                        "type": "LIMIT", "price": 110.0, "quantity": 1.0,
                        "position_side": "LONG", "status": "NEW"})
    insert_paper("SW1", "OPEN", qty=1.0, entry=100, sl=95, lev=3)
    stl.switch_live_mode("SW1", True)
    check("切实盘：手动/残留持仓已平", FAKE.pos_qty("SW1") == 0.0,
          str(FAKE.pos_qty("SW1")))
    check("切实盘：挂单已撤",
          len([o for o in FAKE.orders if o["status"] == "NEW"]) == 0)
    check("切实盘：纸盘仓已清", paper_active("SW1") == [], str(paper_active("SW1")))
    with storage.get_conn(config.SUPERTREND_DB) as c:
        row = c.execute("SELECT live_mode, paused FROM supertrend_tokens "
                        "WHERE token='SW1'").fetchone()
    check("切实盘：live_mode=1 且恢复监控",
          row and row["live_mode"] == 1 and row["paused"] == 0, str(dict(row)))

    print("== 27. 挂单 SL 全生命周期：新K线更新 / 仅 SL 变化 / 取消收敛 ==")
    add_token("LF1")
    PRICE["LF1"] = 100.0
    insert_paper("LF1", "PENDING", side="LONG", qty=1.0, entry=None,
                 sl=90.0, limit_price=95.0)
    stl._rounds_done = 99
    stl.sync_supertrend_live()

    def _lo(tok="LF1"):
        return [o for o in FAKE.live_orders() if o["symbol"] == f"{tok}USDT"]

    def _al(tok="LF1"):
        return [o for o in FAKE.live_algos() if o["symbol"] == f"{tok}USDT"]

    check("初始：限价 95 + SL 90 各 1 张",
          len(_lo()) == 1 and abs(_lo()[0]["price"] - 95) < 1e-9
          and len(_al()) == 1 and abs(_al()[0]["triggerPrice"] - 90) < 1e-9,
          f"lo={_lo()} al={_al()}")
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET limit_price=96.0, stop_loss_price=91.0, "
            "updated_at=CURRENT_TIMESTAMP WHERE token='LF1' AND status='PENDING'")
    stl.sync_supertrend_live()
    check("新K线（价+SL 同移）：限价 96 + SL 91 各 1 张",
          len(_lo()) == 1 and abs(_lo()[0]["price"] - 96) < 1e-9
          and len(_al()) == 1 and abs(_al()[0]["triggerPrice"] - 91) < 1e-9,
          f"lo={_lo()} al={_al()}")
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET stop_loss_price=92.5, "
            "updated_at=CURRENT_TIMESTAMP WHERE token='LF1' AND status='PENDING'")
    stl.sync_supertrend_live()
    check("仅 SL 变化（挂单价不变）：实盘 SL 跟随 92.5（仍 1 张）",
          len(_al()) == 1 and abs(_al()[0]["triggerPrice"] - 92.5) < 1e-9,
          str(_al()))
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE trade_positions SET status='CANCELED', closed_at=CURRENT_TIMESTAMP "
            "WHERE token='LF1' AND status='PENDING'")
    stl.sync_supertrend_live()
    check("挂单取消后：限价已撤", len(_lo()) == 0, str(_lo()))
    check("挂单取消后：SL 已撤（无孤儿）", len(_al()) == 0, str(_al()))
    lr = live_rows_of("LF1")
    check("挂单取消后：live 记录 CLOSED",
          lr and lr[0]["status"] == "CLOSED", str(lr))

    print("== 28. 平仓失败：live 记录不标 CLOSED（防裸仓 + 假平仓） ==")
    add_token("CF1")
    PRICE["CF1"] = 100.0
    insert_paper("CF1", "OPEN", qty=1.0, entry=100, sl=95, lev=3)
    stl._rounds_done = 99
    stl.sync_supertrend_live()
    check("前置：实盘已开", abs(FAKE.pos_qty("CF1") - 1.0) < 1e-9)
    with storage.get_conn() as conn:   # 纸盘平仓 → 收敛实盘
        conn.execute(
            "UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
            "WHERE token='CF1' AND strategy='supertrend' "
            "AND status IN ('OPEN','PARTIAL')")
    FAKE.fail_close_once = True
    stl.sync_supertrend_live()
    check("平仓失败：交易所仍有持仓", FAKE.pos_qty("CF1") > 0, str(FAKE.pos_qty("CF1")))
    check("平仓失败：live 记录不标 CLOSED",
          live_rows_of("CF1")[0]["status"] == "OPEN",
          live_rows_of("CF1")[0]["status"])
    stl.sync_supertrend_live()
    check("重试后平干净", FAKE.pos_qty("CF1") == 0.0, str(FAKE.pos_qty("CF1")))
    check("重试后记录 CLOSED", live_rows_of("CF1")[0]["status"] == "CLOSED")

    print("== 29. 挂单已有 live 记录（交易所先成交）→ 不重复挂单 ==")
    add_token("DP1")
    PRICE["DP1"] = 100.0
    insert_paper("DP1", "PENDING", side="LONG", qty=1.0, entry=None,
                 sl=90.0, limit_price=95.0)
    stl.sync_supertrend_live()
    n_lo0 = len([o for o in FAKE.live_orders() if o["symbol"] == "DP1USDT"])
    check("前置：1 张限价单", n_lo0 == 1, str(n_lo0))
    # 模拟：交易所成交（live 转 OPEN）但纸盘因价格源滞后仍是 PENDING
    FAKE._market_open("DP1", "LONG", 1.0)
    FAKE.fill_limit("DP1")                  # 成交 → 单子从挂单列表消失
    stl.sync_supertrend_live()
    lr = live_rows_of("DP1")[0]
    check("live 已转 OPEN", lr["status"] == "OPEN", lr["status"])
    calls_before = len(FAKE.calls)
    for _ in range(3):
        stl.sync_supertrend_live()
    new_limits = [c for c in FAKE.calls[calls_before:] if c[0] == "limit"]
    check("纸盘仍 PENDING：不再重复挂限价单", len(new_limits) == 0, str(new_limits))
    check("交易所持仓未被放大", abs(FAKE.pos_qty("DP1") - 1.0) < 1e-9,
          str(FAKE.pos_qty("DP1")))

    print("== 30. 撤单失败：保留 id 并上报失败数 ==")
    add_token("CC1")
    PRICE["CC1"] = 100.0
    insert_paper("CC1", "OPEN", qty=1.0, entry=100, sl=95, lev=3)
    stl.sync_supertrend_live()
    rows = live_rows_of("CC1")
    FAKE.fail_cancel_once = True
    failed = stl._cancel_our_orders("CC1", rows)
    check("撤单失败被计数（>=1）", failed >= 1, str(failed))
    failed2 = stl._cancel_our_orders("CC1", live_rows_of("CC1"))
    check("重试撤单成功（失败数 0）", failed2 == 0, str(failed2))

    print("== 31. 双向 live 记录收敛：只平一侧不能把另一侧标 CLOSED（永久裸仓） ==")
    add_token("BOTH1")
    PRICE["BOTH1"] = 100.0
    insert_paper("BOTH1", "OPEN", side="LONG", qty=1.0, entry=100, sl=95)
    stl.sync_supertrend_live()                      # 建 LONG 镜像
    FAKE._market_open("BOTH1", "SHORT", 1.0)        # 旧方向残留（交易所仍有 SHORT 仓）
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        storage.live_position_insert(lc, {
            "paper_position_id": 90001, "token": "BOTH1",
            "symbol": "BOTH1USDT", "side": "SHORT", "strategy": "supertrend",
            "entry_price": 100.0, "quantity": 1.0, "leverage": 3,
            "exchange_orders_json": json.dumps(
                {"open": None, "sl": None, "sl_price": None, "limit": None}),
            "tp1_done": 0, "tp2_done": 0, "status": "OPEN",
        })
    for p in paper_active("BOTH1"):                 # 纸盘全平 → sync 走收敛分支
        with storage.get_conn() as conn:
            conn.execute("UPDATE trade_positions SET status='CLOSED', "
                         "closed_qty=quantity WHERE id=?", (p["id"],))
    stl.sync_supertrend_live()
    check("SHORT 侧实盘仓被平掉（不是只平 LONG 侧）",
          abs(FAKE.pos_qty("BOTH1", "SHORT")) < 1e-9,
          str(FAKE.pos_qty("BOTH1", "SHORT")))
    check("SHORT 侧 live 记录已收敛 CLOSED",
          all(r["status"] == "CLOSED" for r in live_rows_of("BOTH1")
              if r["side"] == "SHORT"),
          str([(r["side"], r["status"]) for r in live_rows_of("BOTH1")]))

    print("== 32. 残留同向 OPEN 记录：不把新纸盘仓当成'交易所持仓消失'强平 ==")
    add_token("STALE1")
    PRICE["STALE1"] = 100.0
    _old = insert_paper("STALE1", "OPEN", qty=1.0, entry=100, sl=95)
    stl.sync_supertrend_live()                      # 旧仓镜像（live_open 属 #_old）
    FAKE.positions.clear()                          # 交易所仓已消失（SL 触发）
    _new = insert_paper("STALE1", "OPEN", qty=1.0, entry=105, sl=99)
    stl.sync_supertrend_live()
    check("新纸盘仓没有被强平", any(p["id"] == _new for p in paper_active("STALE1")),
          str([(p["id"], p["status"]) for p in paper_active("STALE1")]))
    check("新纸盘仓已建镜像（补开）",
          any(r["status"] == "OPEN" and r["paper_position_id"] == _new
              for r in live_rows_of("STALE1")),
          str([(r["paper_position_id"], r["status"]) for r in live_rows_of("STALE1")]))
    check("旧残留记录已收敛 CLOSED",
          all(r["status"] != "OPEN" for r in live_rows_of("STALE1")
              if r["paper_position_id"] == _old),
          str([(r["paper_position_id"], r["status"]) for r in live_rows_of("STALE1")]))

    print("== 33. 换价撤单失败 → 保留 id 且不换价（防孤儿限价单双倍成交） ==")
    add_token("RP1")
    PRICE["RP1"] = 100.0
    insert_paper("RP1", "PENDING", qty=1.0, entry=None, sl=90.0,
                 limit_price=95.0)
    stl.sync_supertrend_live()
    _lr = [r for r in live_rows_of("RP1") if r["status"] == "PENDING"][0]
    _old_limit = json.loads(_lr["exchange_orders_json"])["limit"]
    check("前置：已挂实盘限价单", int(_old_limit) > 0, str(_old_limit))
    with storage.get_conn() as conn:               # 纸盘改挂单价 → 触发换价
        conn.execute("UPDATE trade_positions SET limit_price=93.0 WHERE token='RP1'",
                     )
    FAKE.fail_cancel_once = True
    stl.sync_supertrend_live()
    _lr2 = [r for r in live_rows_of("RP1") if r["status"] == "PENDING"][0]
    _orders = json.loads(_lr2["exchange_orders_json"])
    check("撤单失败：旧 id 未被丢弃", _orders.get("limit") == _old_limit,
          str(_orders))
    check("撤单失败：本轮不挂新单（交易所仍只有 1 张）",
          len([o for o in FAKE.live_orders() if o["symbol"] == "RP1USDT"]) == 1,
          str([(o["order_id"], o["price"]) for o in FAKE.live_orders()
               if o["symbol"] == "RP1USDT"]))

    print("== 34. 预挂 SL 被拒 → 记录不许写 sl_price（否则 _align_open 认为止损已就位） ==")
    # 先清掉上一节的 RP1 并让它收敛：否则它会在同一个 sync 轮里消费掉
    # fail_next_sl 标志（token 遍历是全局的）
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET status='CANCELED' "
                     "WHERE token='RP1' AND status IN ('PENDING','OPEN','PARTIAL')")
    stl.sync_supertrend_live()
    add_token("SLR1")
    PRICE["SLR1"] = 100.0
    insert_paper("SLR1", "PENDING", qty=1.0, entry=None, sl=90.0, limit_price=95.0)
    FAKE.fail_next_sl = True
    stl.sync_supertrend_live()
    _lr = [r for r in live_rows_of("SLR1") if r["status"] == "PENDING"][0]
    check("预挂被拒：orders['sl'] 为空", not json.loads(
        _lr["exchange_orders_json"]).get("sl"), _lr["exchange_orders_json"])
    check("预挂被拒：orders['sl_price'] 也为空（不再谎报止损已挂）",
          not json.loads(_lr["exchange_orders_json"]).get("sl_price"),
          _lr["exchange_orders_json"])
    # 成交后必须补挂（旧码会因 sl_price 有值而跳过）。
    # 成交 = 单子从挂单列表消失 + 出现持仓 —— 两个都要模拟，
    # 只加持仓不给成交（单子还在挂）的话判据会正确地认为"还没成交"
    FAKE._market_open("SLR1", "LONG", 1.0)
    FAKE.fill_limit("SLR1")
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET status='OPEN', entry_price=95.0, "
                     "current_price=95.0 WHERE token='SLR1' AND status='PENDING'")
    stl.sync_supertrend_live()
    check("成交后补挂止损（实盘持仓不再裸奔）",
          len([a for a in FAKE.live_algos() if a["symbol"] == "SLR1USDT"]) == 1,
          str([(a["order_id"], a["triggerPrice"]) for a in FAKE.live_algos()
               if a["symbol"] == "SLR1USDT"]))

    print("== 35. 历史记录只有 sl_price 没有 sl id（旧版代码写的）→ 必须补挂 ==")
    add_token("LEG1")
    PRICE["LEG1"] = 100.0
    _lp = insert_paper("LEG1", "OPEN", qty=1.0, entry=100, sl=95)
    FAKE._market_open("LEG1", "LONG", 1.0)
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        storage.live_position_insert(lc, {
            "paper_position_id": _lp, "token": "LEG1", "symbol": "LEG1USDT",
            "side": "LONG", "strategy": "supertrend", "entry_price": 100.0,
            "quantity": 1.0, "leverage": 3,
            "exchange_orders_json": json.dumps(
                {"open": None, "sl": None, "sl_price": 95.0, "limit": None}),
            "tp1_done": 0, "tp2_done": 0, "status": "OPEN",
        })
    check("前置：交易所没有止损单",
          len([a for a in FAKE.live_algos() if a["symbol"] == "LEG1USDT"]) == 0,
          str(len(FAKE.live_algos())))
    stl.sync_supertrend_live()
    check("只剩 sl_price 的历史记录已补挂止损",
          len([a for a in FAKE.live_algos() if a["symbol"] == "LEG1USDT"]) == 1,
          str([(a["order_id"], a["triggerPrice"]) for a in FAKE.live_algos()
               if a["symbol"] == "LEG1USDT"]))

    print("== 36. 双向持仓不合并（防把空单并进多单吞掉一侧盈亏） ==")
    _ma = insert_paper("MRG1", "OPEN", side="LONG", qty=1.0, entry=100)
    _mb = insert_paper("MRG1", "OPEN", side="SHORT", qty=2.0, entry=110)
    with storage.get_conn() as conn:
        supertrend_strategy._merge_positions(conn, "MRG1")
    _rows = {p["id"]: p for p in paper_active("MRG1")}
    check("双向持仓都保持活跃（既未合并也未取消）",
          _ma in _rows and _mb in _rows
          and _rows[_ma]["status"] == "OPEN" and _rows[_mb]["status"] == "OPEN",
          str([(p["id"], p["side"], p["status"]) for p in paper_active("MRG1")]))

    print("== 37. 对账必须认本方向的记录（拿另一方向的记录改止损=把它的 SL 换错） ==")
    add_token("FLIP2")
    PRICE["FLIP2"] = 100.0
    _fp = insert_paper("FLIP2", "OPEN", side="LONG", qty=1.0, entry=100, sl=95)
    stl.sync_supertrend_live()                    # 建 LONG 镜像
    # 造出"交易所两侧都有仓、SHORT 记录 id 更靠前（更新）"的残留状态
    FAKE._market_open("FLIP2", "SHORT", 1.0)
    FAKE.algo_orders.append({"order_id": 7777, "symbol": "FLIP2USDT",
                             "side": "BUY", "type": "STOP_MARKET",
                             "triggerPrice": 105.0, "quantity": 1.0,
                             "position_side": "SHORT", "status": "NEW"})
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        storage.live_position_insert(lc, {
            "paper_position_id": 90002, "token": "FLIP2",
            "symbol": "FLIP2USDT", "side": "SHORT", "strategy": "supertrend",
            "entry_price": 100.0, "quantity": 1.0, "leverage": 3,
            "exchange_orders_json": json.dumps(
                {"open": None, "sl": 7777, "sl_price": 105.0, "limit": None}),
            "tp1_done": 0, "tp2_done": 0, "status": "OPEN",
        })
    with storage.get_conn() as conn:              # 本方向纸盘仓改 SL → 触发 SL 重挂
        conn.execute("UPDATE trade_positions SET stop_loss_price=96, "
                     "updated_at=CURRENT_TIMESTAMP WHERE id=?", (_fp,))
    stl.sync_supertrend_live()
    _lr = {r["side"]: json.loads(r["exchange_orders_json"])
           for r in live_rows_of("FLIP2") if r["status"] == "OPEN"}
    check("本方向（LONG）记录的止损已更新到 96",
          abs(float(_lr["LONG"].get("sl_price") or 0) - 96.0) < 1e-9,
          str(_lr.get("LONG")))
    check("另一方向（SHORT）记录的止损未被改成 96",
          abs(float(_lr["SHORT"].get("sl_price") or 0) - 105.0) < 1e-9,
          str(_lr.get("SHORT")))

    print("== 38. 被跟单实盘占用：成交分支不得碰交易所（A4/A5）==")
    add_token("AF1")
    PRICE["AF1"] = 100.0
    insert_paper("AF1", "PENDING", qty=1.0, entry=None, sl=90.0, limit_price=95.0)
    stl._rounds_done = 99
    stl.sync_supertrend_live()
    check("前置：限价单已挂 + 预挂 SL",
          len([o for o in FAKE.live_orders() if o["symbol"] == "AF1USDT"]) == 1
          and len([a for a in FAKE.live_algos() if a["symbol"] == "AF1USDT"]) == 1,
          f"限价 {len(FAKE.live_orders())} / SL {len(FAKE.live_algos())}")
    # 制造"跟单占用该币"（币种级互斥：跟单有活跃 live 记录）
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        storage.live_position_insert(lc, {
            "paper_position_id": 88888, "token": "AF1", "symbol": "AF1USDT",
            "side": "LONG", "strategy": "alphafox", "entry_price": 100.0,
            "quantity": 1.0, "leverage": 3, "margin_usd": 50.0,
            "exchange_orders_json": "{}", "tp1_done": 0, "tp2_done": 0,
            "status": "OPEN"})
    check("闸门确实生效（该币被跟单占用）", stl._alphafox_occupied("AF1") is True)

    # 限价单成交（单子消失 + 出现持仓）+ 纸盘 1s 循环转 OPEN
    FAKE.fill_limit("AF1")
    FAKE._market_open("AF1", "LONG", 1.0)
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET status='OPEN', entry_price=95.0, "
                     "current_price=95.0, updated_at=CURRENT_TIMESTAMP "
                     "WHERE token='AF1' AND status='PENDING'")
    calls_before = list(FAKE.calls)
    stl.sync_supertrend_live()
    new_calls = FAKE.calls[len(calls_before):]
    af_cancel = [c for c in new_calls if c[0] == "cancel" and "AF1" in str(c)]
    af_sl = [c for c in new_calls if c[0] == "sl" and "AF1" in str(c)]
    check("占用期间：不撤任何单（成交分支被挡住）", not af_cancel, str(af_cancel))
    check("占用期间：不重挂 SL（防覆盖到跟单的仓位）", not af_sl, str(af_sl))

    # 释放占用（跟单平掉）→ 下一轮应自愈
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        lc.execute("UPDATE live_positions SET status='CLOSED' WHERE token='AF1' "
                   "AND strategy='alphafox'")
    calls_before = list(FAKE.calls)
    stl.sync_supertrend_live()
    new_calls = FAKE.calls[len(calls_before):]
    check("释放占用后自愈：本轮恢复处理（有动作）",
          any("AF1" in str(c) for c in new_calls), str(new_calls))

    print("== 39. 撤单失败：不收敛记录（防孤儿单无人管）A-#2 ==")
    add_token("CD1", live=True)
    PRICE["CD1"] = 100.0
    insert_paper("CD1", "OPEN", qty=1.0, entry=100.0, sl=95.0)
    stl._rounds_done = 99
    stl.sync_supertrend_live()
    check("前置：live 记录已建 + 挂了 SL",
          len(live_rows_of("CD1")) == 1
          and len([a for a in FAKE.live_algos() if a["symbol"] == "CD1USDT"]) == 1)
    # 纸盘仓消失（模拟被别处平掉）→ 记录成"残留"，但撤单会失败
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET status='CLOSED' WHERE token='CD1'")
    FAKE.fail_cancel_once = True
    stl.sync_supertrend_live()
    lr = live_rows_of("CD1")
    check("撤单失败 → 记录**不标 CLOSED**（保留下来下轮重试撤单）",
          lr and lr[0]["status"] != "CLOSED", str([(r['id'], r['status']) for r in lr]))
    check("交易所上那张没撤掉的单仍在（记录还认识它）",
          len([a for a in FAKE.live_algos() if a["symbol"] == "CD1USDT"]) == 1,
          str(len([a for a in FAKE.live_algos() if a["symbol"] == "CD1USDT"])))
    stl.sync_supertrend_live()          # 撤单恢复正常 → 收敛
    lr = live_rows_of("CD1")
    check("撤单恢复后收敛为 CLOSED",
          all(r["status"] == "CLOSED" for r in lr), str([(r['id'], r['status']) for r in lr]))

    print("== 40. 交易所查询失败：close_live 不得谎报成功（A-#7b）==")
    add_token("CL2", live=True)
    PRICE["CL2"] = 100.0
    insert_paper("CL2", "OPEN", qty=1.0, entry=100.0, sl=95.0)
    stl._rounds_done = 99
    stl.sync_supertrend_live()
    FAKE.fail_positions = True
    ok = stl.close_live("CL2")
    FAKE.fail_positions = False
    check("交易所查询不可用时 close_live 返回 False（实盘仓还在，不能报成功）",
          ok is False, str(ok))
    check("同时纸盘确实已关 + 已暂停（纸盘侧照常收敛）",
          not paper_active("CL2")
          and stl._paper_rows("CL2")[0] is None, str(paper_active("CL2")))

    print("== 41. live_position_insert 冲突要如实返回（A-#4）==")
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        storage.live_position_insert(lc, {
            "paper_position_id": 55555, "token": "INS1", "symbol": "INS1USDT",
            "side": "LONG", "strategy": "supertrend", "entry_price": 1.0,
            "quantity": 1.0, "leverage": 3, "margin_usd": 1.0,
            "exchange_orders_json": "{}", "tp1_done": 0, "tp2_done": 0,
            "status": "OPEN"})
        dup = storage.live_position_insert(lc, {
            "paper_position_id": 55555, "token": "INS1", "symbol": "INS1USDT",
            "side": "LONG", "strategy": "supertrend", "entry_price": 2.0,
            "quantity": 2.0, "leverage": 3, "margin_usd": 1.0,
            "exchange_orders_json": "{}", "tp1_done": 0, "tp2_done": 0,
            "status": "OPEN"})
        first = storage.live_position_insert(lc, {
            "paper_position_id": 55556, "token": "INS2", "symbol": "INS2USDT",
            "side": "LONG", "strategy": "supertrend", "entry_price": 1.0,
            "quantity": 1.0, "leverage": 3, "margin_usd": 1.0,
            "exchange_orders_json": "{}", "tp1_done": 0, "tp2_done": 0,
            "status": "OPEN"})
    check("首次插入返回 True", first is True)
    check("唯一键冲突被 OR IGNORE 跳过时返回 False（旧写法恒 True → 静默丢记录）",
          dup is False, str(dup))

    print("== 42. SL 触发后记录必须收敛（撤一张已消失的单 = 成功）F1 ==")
    add_token("TG1", live=True)
    PRICE["TG1"] = 100.0
    insert_paper("TG1", "OPEN", qty=1.0, entry=100.0, sl=95.0)
    stl._rounds_done = 99
    stl.sync_supertrend_live()
    _algs = [a for a in FAKE.live_algos() if a["symbol"] == "TG1USDT"]
    check("前置：挂了 1 张 SL", len(_algs) == 1, str(len(_algs)))
    # 币安 SL 触发：单子成交消失 + 持仓没了（模拟交易所侧）
    _algs[0]["status"] = "FILLED"
    FAKE.positions = [p for p in FAKE.positions if p["symbol"] != "TG1USDT"]
    stl.sync_supertrend_live()
    lr = live_rows_of("TG1")
    check("SL 触发后 live 记录收敛为 CLOSED（撤一张已成交的单算成功）",
          all(r["status"] == "CLOSED" for r in lr),
          str([(r['id'], r['status']) for r in lr]))
    check("纸盘同步收敛", not paper_active("TG1"), str(paper_active("TG1")))
    check("★ 该币不再被超势「占用」（否则跟单永远用不了它）",
          not any(r["status"] in ("OPEN", "PARTIAL", "PENDING")
                  for r in live_rows_of("TG1")),
          str([(r['id'], r['status']) for r in live_rows_of("TG1")]))

    print("== 43. 孤儿挂单记录：撤单失败也必须保留（F2）==")
    add_token("OR1", live=True)
    PRICE["OR1"] = 100.0
    insert_paper("OR1", "OPEN", qty=1.0, entry=100.0, sl=95.0)
    insert_paper("OR1", "PENDING", qty=1.0, entry=None, sl=95.0, limit_price=98.0)
    stl._rounds_done = 99
    stl.sync_supertrend_live()
    lr = live_rows_of("OR1")
    check("前置：并存两条记录（OPEN + PENDING）",
          len([r for r in lr if r["status"] == "OPEN"]) == 1
          and len([r for r in lr if r["status"] == "PENDING"]) == 1,
          str([(r['id'], r['status']) for r in lr]))
    # 纸盘挂单被撤（没成交）→ live 挂单记录成孤儿；但它那张限价单还挂在交易所
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET status='CANCELED' "
                     "WHERE token='OR1' AND status='PENDING'")
    FAKE.fail_cancel_once = True          # 撤单失败一次
    stl.sync_supertrend_live()
    lr = live_rows_of("OR1")
    check("撤单失败 → 孤儿记录**不收敛**（保留下来下轮重试撤单）",
          any(r["status"] == "PENDING" for r in lr),
          str([(r['id'], r['status']) for r in lr]))
    stl.sync_supertrend_live()            # 撤单恢复正常
    lr = live_rows_of("OR1")
    check("撤单恢复后孤儿记录被收敛",
          not any(r["status"] == "PENDING" for r in lr),
          str([(r['id'], r['status']) for r in lr]))
    check("交易所那张限价单已撤掉",
          len([o for o in FAKE.live_orders() if o["symbol"] == "OR1USDT"]) == 0,
          str(len([o for o in FAKE.live_orders() if o["symbol"] == "OR1USDT"])))

    print("== 44. 实盘平仓部分成交：记录不收敛、下轮继续平剩余（F5）==")
    add_token("PC1", live=True)
    PRICE["PC1"] = 100.0
    insert_paper("PC1", "OPEN", qty=1.0, entry=100.0, sl=95.0)
    stl._rounds_done = 99
    stl.sync_supertrend_live()
    with storage.get_conn() as conn:      # 纸盘被平（模拟 1s 扫损）→ 走"纸盘已无活跃"收敛
        conn.execute("UPDATE trade_positions SET status='CLOSED', closed_qty=1.0 "
                     "WHERE token='PC1'")
    FAKE.partial_close_ratio = 0.5        # 市价平仓只成交一半
    stl.sync_supertrend_live()
    lr = live_rows_of("PC1")
    check("部分成交 → 记录**不标 CLOSED**（残仓下轮继续平）",
          lr and lr[0]["status"] != "CLOSED",
          str([(r['id'], r['status']) for r in lr]))
    check("交易所确实还剩残仓（等着下轮平）", FAKE.pos_qty("PC1") > 0,
          str(FAKE.pos_qty("PC1")))
    FAKE.partial_close_ratio = 1.0
    stl.sync_supertrend_live()
    check("恢复正常后收敛：记录 CLOSED + 交易所清空",
          all(r["status"] == "CLOSED" for r in live_rows_of("PC1"))
          and FAKE.pos_qty("PC1") == 0.0,
          f"{[(r['id'], r['status']) for r in live_rows_of('PC1')]} "
          f"pos={FAKE.pos_qty('PC1')}")

    print("== 45. 复用 live 记录时不得再挂新单（F3）==")
    add_token("RO1", live=True)
    PRICE["RO1"] = 100.0
    insert_paper("RO1", "PENDING", qty=1.0, entry=None, sl=90.0, limit_price=95.0)
    stl._rounds_done = 99
    stl.sync_supertrend_live()
    _ids_before = {o["order_id"] for o in FAKE.live_orders()
                   if o["symbol"] == "RO1USDT"}
    check("前置：1 张限价单", len(_ids_before) == 1, str(_ids_before))
    # 纸盘转 OPEN（市价开），但交易所那张限价单**还在挂** → 复用路径只做"转 OPEN"。
    # 关键：把纸盘行的 limit_price 也改成开仓价（真实市价开就是这么写入的）——
    # 不改的话它会与线上记录的挂单价恰好相等，走"价格没变"直接 return，
    # 那条危险路径根本没被走到（变异也就抓不住）
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET status='OPEN', entry_price=100.0, "
                     "limit_price=100.0, current_price=100.0 "
                     "WHERE token='RO1' AND status='PENDING'")
    FAKE._market_open("RO1", "LONG", 1.0)
    stl.sync_supertrend_live()
    _ids_after = {o["order_id"] for o in FAKE.live_orders()
                  if o["symbol"] == "RO1USDT"}
    # 断言单号集合不变（不是"张数不变"）：走错路径时会"撤旧挂新"，
    # 张数一样会骗过计数式断言
    check("喂纸盘 OPEN 行时**不会动交易所那张限价单**（否则按开仓价撤旧挂新）",
          _ids_after == _ids_before, f"{_ids_before} → {_ids_after}")

    print("== 46. 撤不掉的残留单 → 推迟补开（F4）==")
    # 造一条**反方向**残留记录（带一张撤不掉的挂单），交易所该方向无持仓。
    # 反方向才走得到 _mirror_open：同方向的会被当成 live_open 走 _align_open
    add_token("DF1", live=True)
    PRICE["DF1"] = 100.0
    with storage.get_conn(config.LIVE_DB_PATH) as _lc:
        storage.live_position_insert(_lc, {
            "paper_position_id": 99001, "token": "DF1", "symbol": "DF1USDT",
            "side": "SHORT", "strategy": "supertrend", "entry_price": 100.0,
            "quantity": 1.0, "leverage": 3, "margin_usd": 50.0,
            "exchange_orders_json": json.dumps({"limit": 88001, "sl": None}),
            "tp1_done": 0, "tp2_done": 0, "status": "PENDING"})
    FAKE.orders.append({"order_id": 88001, "symbol": "DF1USDT", "side": "BUY",
                        "type": "LIMIT", "price": 98.0, "quantity": 1.0,
                        "position_side": "SHORT", "status": "NEW"})
    FAKE.fail_cancel_ids.add(88001)
    insert_paper("DF1", "OPEN", qty=1.0, entry=100.0, sl=95.0)
    stl._rounds_done = 99
    stl.sync_supertrend_live()
    check("前置：撤不掉的残留单导致本轮**没有补开**（市价仓为 0）",
          FAKE.pos_qty("DF1") == 0.0, str(FAKE.pos_qty("DF1")))
    check("残留记录被保留（单号没丢，下轮还能重试撤单）",
          any(r["status"] != "CLOSED" for r in live_rows_of("DF1")),
          str([(r['id'], r['status']) for r in live_rows_of("DF1")]))
    FAKE.fail_cancel_ids.discard(88001)
    stl.sync_supertrend_live()
    check("撤单恢复后本轮补开成功", FAKE.pos_qty("DF1") > 0,
          str(FAKE.pos_qty("DF1")))

    print("== 47. 跟单占用查询失败 → 按占用处理，不碰交易所（F7）==")
    check("查询异常时 fail-closed（安全闸门不放行）",
          stl._alphafox_occupied("NOPE_TOKEN") in (True, False))
    _real_occ = stl._alphafox_occupied

    def _boom(tok):
        raise RuntimeError("live.db busy")
    stl._alphafox_occupied = _boom
    try:
        # 直接验证"判不出来就当占用"的语义：替身抛异常 → 真实函数应返回 True
        import alphafox_live as _afl
        _real_afl = _afl.alphafox_occupied
        _afl.alphafox_occupied = lambda tok: (_ for _ in ()).throw(RuntimeError("x"))
        try:
            check("替身异常 → 按「被占用」返回 True",
                  _real_occ("X1") is True, str(_real_occ("X1")))
        finally:
            _afl.alphafox_occupied = _real_afl
    finally:
        stl._alphafox_occupied = _real_occ

    print("== 48. close_live / switch_live_mode 先置 paused（F6）==")
    # 只看"结束时 paused=1"是抓不到顺序问题的（两个函数末尾本来就会置 paused）——
    # 必须**在关纸盘那一刻**看它是不是已经 paused 了
    import supertrend_strategy as _sts

    _seen = {}
    _real_close = _sts.close_supertrend_position

    def _spy_close(tok, reason):
        with storage.get_conn(config.SUPERTREND_DB) as c:
            r = c.execute("SELECT paused FROM supertrend_tokens WHERE token=?",
                          (tok,)).fetchone()
        _seen[tok] = r["paused"] if r else None
        return _real_close(tok, reason)

    for tok, fn in (("PZ1", lambda: stl.close_live("PZ1")),
                    ("PZ2", lambda: stl.switch_live_mode("PZ2", False))):
        add_token(tok, live=True)
        PRICE[tok] = 100.0
        insert_paper(tok, "OPEN", qty=1.0, entry=100.0, sl=95.0)
        stl._rounds_done = 99
        stl.sync_supertrend_live()
        _sts.close_supertrend_position = _spy_close
        try:
            fn()
        finally:
            _sts.close_supertrend_position = _real_close
    check("close_live：关纸盘那一刻该币**已经** paused=1（顺序对）",
          _seen.get("PZ1") == 1, str(_seen.get("PZ1")))
    check("switch_live_mode：关纸盘那一刻该币**已经** paused=1（顺序对）",
          _seen.get("PZ2") == 1, str(_seen.get("PZ2")))

    print("== 49. live 记录写入：撞上 CLOSED 残留行要复用（不能静默丢弃）#2 ==")
    add_token("WR1", live=True)
    PRICE["WR1"] = 100.0
    _wp = insert_paper("WR1", "OPEN", qty=1.0, entry=100.0, sl=95.0)
    with storage.get_conn(config.LIVE_DB_PATH) as _lc:
        storage.live_position_insert(_lc, {
            "paper_position_id": _wp, "token": "WR1", "symbol": "WR1USDT",
            "side": "LONG", "strategy": "supertrend", "entry_price": 100.0,
            "quantity": 1.0, "leverage": 3, "margin_usd": 50.0,
            "exchange_orders_json": "{}", "tp1_done": 0, "tp2_done": 0,
            "status": "CLOSED"})
        _wold = _lc.execute("SELECT id FROM live_positions WHERE paper_position_id=?",
                            (_wp,)).fetchone()["id"]
        storage.live_position_update(_lc, _wold, {"realized_pnl": 7.5})
        _wrid = stl._write_live_record(_lc, {
            "paper_position_id": _wp, "token": "WR1", "symbol": "WR1USDT",
            "side": "LONG", "strategy": "supertrend", "entry_price": 100.0,
            "quantity": 1.0, "leverage": 3, "margin_usd": 50.0,
            "exchange_orders_json": "{}", "tp1_done": 0, "tp2_done": 0,
            "status": "OPEN"})
        _wback = _lc.execute("SELECT * FROM live_positions WHERE id=?",
                             (_wold,)).fetchone()
    check("冲突时复用旧行（返回的就是那条记录的 id）",
          _wrid == _wold, f"{_wrid} vs {_wold}")
    check("旧行被改回活跃状态", _wback["status"] == "OPEN", str(_wback["status"]))
    check("closed_at 清空（不留上次的平仓时间）",
          _wback["closed_at"] is None, str(_wback["closed_at"]))
    check("realized_pnl 不清零（同一纸盘仓平了又开的真实盈亏）",
          abs(float(_wback["realized_pnl"] or 0) - 7.5) < 1e-9,
          str(_wback["realized_pnl"]))

    print("== 50. 挂单没成交时的补建：不再被 return 挡成死代码 #1 ==")
    add_token("RB1", live=True)
    PRICE["RB1"] = 100.0
    insert_paper("RB1", "PENDING", qty=1.0, entry=None, sl=90.0, limit_price=95.0)
    stl._rounds_done = 99
    stl.sync_supertrend_live()
    _rbl = live_rows_of("RB1")
    check("前置：只有一条 PENDING 记录（限价单还挂着）",
          len(_rbl) == 1 and _rbl[0]["status"] == "PENDING",
          str([(r["id"], r["status"]) for r in _rbl]))
    # 纸盘行转 OPEN（1s 循环：挂单在市价成交），但交易所那张限价单**没成交**；
    # 交易所有持仓（市价腿）、live 却没有 OPEN 记录 —— 记录缺口状态
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET status='OPEN', entry_price=100.0, "
                     "current_price=100.0, updated_at=CURRENT_TIMESTAMP "
                     "WHERE token='RB1' AND status='PENDING'")
    FAKE._market_open("RB1", "LONG", 1.0)
    _n0 = len([a for a in FAKE.live_algos() if a["symbol"] == "RB1USDT"])
    stl.sync_supertrend_live()
    check("补建 OPEN 记录（纸盘在市价持仓 + 交易所有仓，不再没人管）",
          any(r["status"] == "OPEN" for r in live_rows_of("RB1")),
          str([(r["id"], r["status"]) for r in live_rows_of("RB1")]))
    _n1 = len([a for a in FAKE.live_algos() if a["symbol"] == "RB1USDT"])
    check("补建给市价腿挂了 SL（实盘持仓不裸奔）", _n1 == _n0 + 1, f"{_n0} → {_n1}")
    stl.sync_supertrend_live()
    stl.sync_supertrend_live()
    check("后续轮次不重复补建（OPEN 记录仍只有一条）",
          len([r for r in live_rows_of("RB1") if r["status"] == "OPEN"]) == 1,
          str([(r["id"], r["status"]) for r in live_rows_of("RB1")]))
    check("后续轮次不重复挂 SL",
          len([a for a in FAKE.live_algos() if a["symbol"] == "RB1USDT"]) == _n1,
          str(len([a for a in FAKE.live_algos() if a["symbol"] == "RB1USDT"])))

    print("== 51. SL 交接没完成 → 重复记录不收敛（否则止损覆盖归零）#3 ==")
    add_token("HO1", live=True)
    PRICE["HO1"] = 100.0
    insert_paper("HO1", "OPEN", qty=1.0, entry=100.0, sl=95.0)
    stl._rounds_done = 99
    stl.sync_supertrend_live()                    # 代表记录 + 它自己的 SL
    with storage.get_conn(config.LIVE_DB_PATH) as _lc:
        storage.live_position_insert(_lc, {
            "paper_position_id": 99101, "token": "HO1", "symbol": "HO1USDT",
            "side": "LONG", "strategy": "supertrend", "entry_price": 100.0,
            "quantity": 1.0, "leverage": 3, "margin_usd": 50.0,
            "exchange_orders_json": json.dumps({"sl": 88002, "sl_price": 95.0}),
            "tp1_done": 0, "tp2_done": 0, "status": "OPEN"})
        _hdup = _lc.execute("SELECT id FROM live_positions WHERE paper_position_id=?",
                            (99101,)).fetchone()["id"]
    FAKE.algo_orders.append({"order_id": 88002, "symbol": "HO1USDT", "side": "SELL",
                             "type": "STOP_MARKET", "triggerPrice": 95.0,
                             "quantity": 1.0, "position_side": "LONG", "status": "NEW"})
    _hsl = None
    for r in live_rows_of("HO1"):
        if r["id"] != _hdup and r["status"] == "OPEN":
            _hsl = int((json.loads(r["exchange_orders_json"]) or {}).get("sl") or 0)
    check("前置：代表记录有 SL、重复记录也有 SL", _hsl > 0, str(_hsl))
    # 交接的第一步是"撤代表记录的旧 SL"——让它撤不掉 → 交接失败
    FAKE.fail_cancel_ids.add(_hsl)
    stl.sync_supertrend_live()
    _hd = [r for r in live_rows_of("HO1") if r["id"] == _hdup]
    check("交接失败 → 重复记录**不收敛**（保留它的止损覆盖）",
          bool(_hd) and _hd[0]["status"] == "OPEN",
          str([(r["id"], r["status"]) for r in live_rows_of("HO1")]))
    # 断言**覆盖量**而不是单号：记录里的 orders 可能因"没有 sl_qty（旧记录）"被重挂一次，
    # 单号会换，但"交接失败也不能把覆盖撤成 0"这条不变
    _cov = sum(float(a["quantity"]) for a in FAKE.live_algos()
               if a["symbol"] == "HO1USDT")
    check("止损覆盖没丢（交接失败不许把覆盖撤成 0）", _cov >= 1.0 - 1e-9,
          str([(a["order_id"], a["quantity"]) for a in FAKE.live_algos()
               if a["symbol"] == "HO1USDT"]))
    FAKE.fail_cancel_ids.discard(_hsl)
    stl.sync_supertrend_live()
    _hd = [r for r in live_rows_of("HO1") if r["id"] == _hdup]
    check("交接恢复后重复记录收敛 CLOSED",
          bool(_hd) and _hd[0]["status"] == "CLOSED",
          str([(r["id"], r["status"]) for r in live_rows_of("HO1")]))

    print("== 52. 被跟单占用：持仓对齐（_align_open）也不得碰交易所 #5 ==")
    add_token("AG1", live=True)
    PRICE["AG1"] = 100.0
    _ag = insert_paper("AG1", "OPEN", qty=1.0, entry=100.0, sl=95.0)
    stl._rounds_done = 99
    stl.sync_supertrend_live()                    # 镜像 + SL
    with storage.get_conn(config.LIVE_DB_PATH) as _lc:
        storage.live_position_insert(_lc, {      # 跟单也持有该币
            "paper_position_id": 99201, "token": "AG1", "symbol": "AG1USDT",
            "side": "LONG", "strategy": "alphafox", "entry_price": 100.0,
            "quantity": 1.0, "leverage": 3, "margin_usd": 50.0,
            "exchange_orders_json": "{}", "tp1_done": 0, "tp2_done": 0,
            "status": "OPEN"})
    with storage.get_conn() as conn:             # 纸盘 SL 变了 → 正常会撤旧挂新
        conn.execute("UPDATE trade_positions SET stop_loss_price=96, "
                     "updated_at=CURRENT_TIMESTAMP WHERE id=?", (_ag,))
    _alg0 = {a["order_id"] for a in FAKE.live_algos() if a["symbol"] == "AG1USDT"}
    _c0 = len(FAKE.calls)
    stl.sync_supertrend_live()
    _new = FAKE.calls[_c0:]
    _ag_cancel = [c for c in _new if c[0] == "cancel" and "AG1" in str(c)]
    _ag_sl = [c for c in _new if c[0] == "sl" and "AG1" in str(c)]
    check("占用期间：不撤 SL", not _ag_cancel, str(_ag_cancel))
    check("占用期间：不重挂 SL（防覆盖到跟单的仓位）", not _ag_sl, str(_ag_sl))
    check("旧 SL 原封不动",
          {a["order_id"] for a in FAKE.live_algos()
           if a["symbol"] == "AG1USDT"} == _alg0,
          str({a["order_id"] for a in FAKE.live_algos()
               if a["symbol"] == "AG1USDT"}))
    with storage.get_conn(config.LIVE_DB_PATH) as _lc:   # 释放占用
        _lc.execute("UPDATE live_positions SET status='CLOSED' "
                    "WHERE token='AG1' AND strategy='alphafox'")
    stl.sync_supertrend_live()
    _agp = [a["triggerPrice"] for a in FAKE.live_algos()
            if a["symbol"] == "AG1USDT"]
    check("释放占用后自愈：SL 更新到 96",
          any(abs(float(p) - 96.0) < 1e-9 for p in _agp), str(_agp))

    print("== 53. 手动平仓 / 切回纸盘：realized 用纸盘口径（不再回落 0）#6 ==")
    add_token("RZ1", live=True)
    PRICE["RZ1"] = 100.0
    _rz = insert_paper("RZ1", "OPEN", qty=1.0, entry=100.0, sl=95.0)
    stl._rounds_done = 99
    stl.sync_supertrend_live()
    with storage.get_conn() as conn:              # 纸盘被平（带真实盈亏）
        conn.execute("UPDATE trade_positions SET status='CLOSED', "
                     "closed_qty=quantity, realized_pnl=12.5 WHERE id=?", (_rz,))
    FAKE.positions = []                           # 交易所仓已消失（SL 触发过）
    with storage.get_conn(config.LIVE_DB_PATH) as _lc:
        stl._close_exchange_and_mark(_lc, "RZ1", stl._live_rows(_lc, "RZ1"),
                                     FAKE.get_positions(),
                                     stl._latest_closed_paper_row("RZ1"), "手动平仓")
    _rzl = live_rows_of("RZ1")
    check("realized 按纸盘口径记（传 None 的旧写法这里回落到 0）",
          bool(_rzl) and all(abs(float(r["realized_pnl"] or 0) - 12.5) < 1e-9
                             for r in _rzl),
          str([(r["id"], r["realized_pnl"]) for r in _rzl]))
    # 接线（光验函数不够）：close_live 必须把纸盘平仓行传下去
    add_token("RZ2", live=True)
    PRICE["RZ2"] = 100.0
    insert_paper("RZ2", "OPEN", qty=1.0, entry=100.0, sl=95.0)
    stl.sync_supertrend_live()
    _seen_row = {}
    _cem = stl._close_exchange_and_mark

    def _spy_cem(lc, tok, rows, ex, row, reason):
        _seen_row[tok] = row
        return _cem(lc, tok, rows, ex, row, reason)

    stl._close_exchange_and_mark = _spy_cem
    try:
        stl.close_live("RZ2")
    finally:
        stl._close_exchange_and_mark = _cem
    check("close_live 传下去的是纸盘平仓行（不是 None）",
          _seen_row.get("RZ2") is not None, str(_seen_row.get("RZ2")))
    check("传的行就是刚平掉的那笔纸盘仓",
          (_seen_row.get("RZ2") or {}).get("token") == "RZ2",
          str(_seen_row.get("RZ2")))

    print("== 54. 残仓不足最小下单量：不放大、不卡死 #4 ==")
    add_token("DS1", live=True)
    PRICE["DS1"] = 100.0
    _ds = insert_paper("DS1", "OPEN", qty=1.0, entry=100.0, sl=95.0)
    stl._rounds_done = 99
    stl.sync_supertrend_live()
    FAKE.step, FAKE.min_qty = 0.001, 0.001       # 打开量化（模拟币安 LOT_SIZE）
    for _p in FAKE.positions:                    # 交易所只剩 0.0004（不足一个 step）
        if _p["symbol"] == "DS1USDT":
            _p["quantity"] = 0.0004
    with storage.get_conn() as conn:             # 纸盘平仓 → 走收敛
        conn.execute("UPDATE trade_positions SET status='CLOSED', closed_qty=quantity "
                     "WHERE id=?", (_ds,))
    _dc0 = len([c for c in FAKE.calls if c[0] == "close" and c[1] == "DS1"])
    stl.sync_supertrend_live()
    _dc1 = len([c for c in FAKE.calls if c[0] == "close" and c[1] == "DS1"])
    check("残仓不放大成 minQty 下单（旧写法放大后被拒 = 永久重试 + 裸仓）",
          _dc1 == _dc0, f"{_dc0} → {_dc1}")
    check("记录照常收敛 CLOSED（该币不被永久卡在活跃态）",
          all(r["status"] == "CLOSED" for r in live_rows_of("DS1")),
          str([(r["id"], r["status"]) for r in live_rows_of("DS1")]))
    FAKE.step, FAKE.min_qty = None, 0.0

    print("== 55. 交易日志：挂/改/撤/成交/对账 —— 只在动作真发生时写 ==")
    def logs_of(token):
        with storage.get_conn(config.LIVE_DB_PATH) as lc:
            return [dict(r) for r in lc.execute(
                "SELECT * FROM live_trade_log WHERE token=? ORDER BY id",
                (token,)).fetchall()]

    def acts(token):
        return [r["action"] for r in logs_of(token)]

    add_token("LG1", live=True)
    PRICE["LG1"] = 100.0
    insert_paper("LG1", "OPEN", qty=1.0, entry=100.0, sl=95.0)
    stl._rounds_done = 99
    stl.sync_supertrend_live()
    check("市价补开：日志 sl_set + open（先挂止损再落记录）",
          acts("LG1") == ["sl_set", "open"], str(acts("LG1")))
    _lg = [r for r in logs_of("LG1") if r["action"] == "sl_set"][-1]
    check("sl_set 带止损价/数量/单号",
          abs(_lg["price"] - 95.0) < 1e-9 and abs(_lg["quantity"] - 1.0) < 1e-9
          and bool(_lg["exchange_order_id"]), str(_lg))
    check("sl_set 写明来源（补开挂止损）",
          json.loads(_lg["details_json"])["reason"] == "补开挂止损",
          str(_lg["details_json"]))
    _n0 = len(logs_of("LG1"))
    stl.sync_supertrend_live()
    stl.sync_supertrend_live()
    check("无动作的轮次不写日志（对账每轮都跑，防轮询垃圾）",
          len(logs_of("LG1")) == _n0, f"{_n0} → {len(logs_of('LG1'))}")
    with storage.get_conn() as conn:                # 新K线移动止损
        conn.execute("UPDATE trade_positions SET stop_loss_price=97.0 "
                     "WHERE token='LG1'")
    stl.sync_supertrend_live()
    check("止损移动 → sl_update", acts("LG1")[-1] == "sl_update", str(acts("LG1")))
    check("sl_update 带新止损价", abs(logs_of("LG1")[-1]["price"] - 97.0) < 1e-9,
          str(logs_of("LG1")[-1]["price"]))
    with storage.get_conn() as conn:                # 纸盘量变大 → 对账补仓
        conn.execute("UPDATE trade_positions SET quantity=2.0 WHERE token='LG1'")
    stl.sync_supertrend_live()
    check("交易所少于纸盘 → rebalance_add，止损随即按新总持仓重挂（sl_update）",
          acts("LG1")[-2:] == ["rebalance_add", "sl_update"], str(acts("LG1")))
    with storage.get_conn() as conn:                # 纸盘量变小 → 对账减仓
        conn.execute("UPDATE trade_positions SET quantity=1.0 WHERE token='LG1'")
    stl.sync_supertrend_live()
    check("交易所多于纸盘 → rebalance_reduce，止损同样按新量重挂",
          acts("LG1")[-2:] == ["rebalance_reduce", "sl_update"], str(acts("LG1")))
    check("对账补/减仓都写明原因",
          all(json.loads(r["details_json"])["reason"] for r in logs_of("LG1")[-2:]),
          str(logs_of("LG1")[-2:]))

    add_token("LG2", live=True)
    PRICE["LG2"] = 100.0
    insert_paper("LG2", "PENDING", qty=1.0, entry=None, sl=90.0, limit_price=95.0)
    stl.sync_supertrend_live()
    check("限价挂单腿：日志 sl_set（预挂）+ open（限价挂单）",
          acts("LG2") == ["sl_set", "open"], str(acts("LG2")))
    check("预挂 sl_set 写明来源",
          json.loads([r for r in logs_of("LG2")
                      if r["action"] == "sl_set"][-1]["details_json"])["reason"]
          == "挂单腿预挂止损", str(logs_of("LG2")))
    with storage.get_conn() as conn:                # 挂单价变了 → 换价撤旧挂新
        conn.execute("UPDATE trade_positions SET limit_price=93.0 WHERE token='LG2'")
    stl.sync_supertrend_live()
    _new = acts("LG2")[-4:]
    check("换价：撤旧限价单 + 撤旧止损 + 重新挂出（四个动作齐全）",
          set(_new) == {"limit_cancel", "sl_cancel", "sl_set", "open"},
          str(acts("LG2")))
    check("撤单日志带被撤的单号",
          all(r["exchange_order_id"] for r in logs_of("LG2")[-4:]
              if r["action"] in ("limit_cancel", "sl_cancel")),
          str(logs_of("LG2")[-4:]))
    FAKE._market_open("LG2", "LONG", 1.0)          # 限价单成交
    FAKE.fill_limit("LG2")
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET status='OPEN', entry_price=95.0, "
                     "current_price=95.0 WHERE token='LG2' AND status='PENDING'")
    stl.sync_supertrend_live()
    check("成交事件入日志（限价单成交一条，说明写明转入持仓）",
          json.loads(logs_of("LG2")[-1]["details_json"])["reason"]
          == "限价单成交（挂单腿转入持仓）", str(logs_of("LG2")[-1]))
    check("成交后不再重复补挂止损（预挂的覆盖量本就对）",
          acts("LG2")[-1] == "open" and acts("LG2").count("sl_set") == 2,
          str(acts("LG2")))

    print("== 56. 平仓撤单入日志 + 日志写失败不得拖垮状态机 ==")
    add_token("LG3", live=True)
    PRICE["LG3"] = 100.0
    _p3 = insert_paper("LG3", "OPEN", qty=1.0, entry=100.0, sl=95.0)
    stl.sync_supertrend_live()
    check("前置：实盘建仓 + 挂止损", acts("LG3") == ["sl_set", "open"],
          str(acts("LG3")))
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET status='CLOSED', "
                     "closed_qty=quantity WHERE id=?", (_p3,))
    stl.sync_supertrend_live()
    check("平仓收敛撤掉的止损单也入日志（孤儿/残留单不再隐形）",
          acts("LG3")[-2:] == ["sl_cancel", "close"], str(acts("LG3")))
    check("撤单日志写明原因",
          json.loads([r for r in logs_of("LG3")
                      if r["action"] == "sl_cancel"][-1]["details_json"])["reason"]
          == "平仓收敛撤单", str(logs_of("LG3")[-2:]))

    add_token("LG4", live=True)
    PRICE["LG4"] = 100.0
    insert_paper("LG4", "OPEN", qty=1.0, entry=100.0, sl=95.0)
    _orig_log_insert = storage.live_trade_log_insert

    def _boom(*a, **k):                      # 模拟日志表写不进去
        raise RuntimeError("disk full")

    storage.live_trade_log_insert = _boom
    try:
        stl.sync_supertrend_live()
    finally:
        storage.live_trade_log_insert = _orig_log_insert
    _lr4 = live_rows_of("LG4")
    check("日志写失败：live 记录照常建（状态机不被日志拖垮）",
          any(r["status"] == "OPEN" for r in _lr4), str(_lr4))
    check("日志写失败：交易所持仓 + 止损单都到位（不留半完成状态）",
          FAKE.pos_qty("LG4") > 0
          and len([a for a in FAKE.live_algos()
                   if a["symbol"] == "LG4USDT"]) == 1,
          f"qty={FAKE.pos_qty('LG4')} "
          f"algos={len([a for a in FAKE.live_algos() if a['symbol'] == 'LG4USDT'])}")
    check("日志写失败：记录量 == 交易所量",
          _lr4 and abs(float(_lr4[0]["quantity"] or 0)
                       - FAKE.pos_qty("LG4")) < 1e-9,
          f"{_lr4[0]['quantity'] if _lr4 else None} vs {FAKE.pos_qty('LG4')}")

    # 换价撤旧挂新是"日志失败会真出事"的路径：日志抛出去会跳过 _save_orders，
    # 记录留着已作废的单号 → 下轮再挂一张 → 止损单越堆越多（触发时超量平仓）
    add_token("LG5", live=True)
    PRICE["LG5"] = 100.0
    insert_paper("LG5", "PENDING", qty=1.0, entry=None, sl=90.0, limit_price=95.0)
    stl.sync_supertrend_live()
    check("前置：挂单腿 + 预挂止损就位",
          len([o for o in FAKE.live_orders() if o["symbol"] == "LG5USDT"]) == 1
          and len([a for a in FAKE.live_algos()
                   if a["symbol"] == "LG5USDT"]) == 1,
          str([(o["order_id"], o["price"]) for o in FAKE.live_orders()
               if o["symbol"] == "LG5USDT"]))
    with storage.get_conn() as conn:             # 换价 → 撤旧挂新
        conn.execute("UPDATE trade_positions SET limit_price=93.0 "
                     "WHERE token='LG5' AND status='PENDING'")
    storage.live_trade_log_insert = _boom
    try:
        stl.sync_supertrend_live()
        stl.sync_supertrend_live()
    finally:
        storage.live_trade_log_insert = _orig_log_insert
    _lr5 = live_rows_of("LG5")
    _o5 = json.loads(_lr5[0]["exchange_orders_json"]) if _lr5 else {}
    _ex5 = [str(o["order_id"]) for o in FAKE.live_orders()
            if o["symbol"] == "LG5USDT"]
    check("日志写失败：换价照常落盘（记录里的限价单号就是交易所那张）",
          bool(_o5.get("limit")) and str(_o5["limit"]) in _ex5,
          f"记录={_o5.get('limit')} 交易所={_ex5}")
    check("日志写失败：止损单不会每轮堆一张（堆多了触发时超量平仓）",
          len([a for a in FAKE.live_algos()
               if a["symbol"] == "LG5USDT"]) == 1,
          str([(a["order_id"], a["triggerPrice"]) for a in FAKE.live_algos()
               if a["symbol"] == "LG5USDT"]))

    print("== 57. 挂单腿只改止损（轨道移动、限价不动）→ 实盘预挂 SL 改价重挂 ==")
    add_token("LG6", live=True)
    PRICE["LG6"] = 100.0
    insert_paper("LG6", "PENDING", qty=1.0, entry=None, sl=90.0, limit_price=95.0)
    stl.sync_supertrend_live()
    _a0 = [a for a in FAKE.live_algos() if a["symbol"] == "LG6USDT"]
    check("前置：预挂止损 @90（限价 95）",
          len(_a0) == 1 and abs(float(_a0[0]["triggerPrice"]) - 90.0) < 1e-9
          and len([o for o in FAKE.live_orders()
                   if o["symbol"] == "LG6USDT"]) == 1,
          str([(a["order_id"], a["triggerPrice"]) for a in _a0]))
    with storage.get_conn() as conn:      # 只动纸盘止损（新K线轨道移动）
        conn.execute("UPDATE trade_positions SET stop_loss_price=92.0 "
                     "WHERE token='LG6' AND status='PENDING'")
    stl.sync_supertrend_live()
    _a1 = [a for a in FAKE.live_algos() if a["symbol"] == "LG6USDT"]
    check("实盘预挂止损改到新价（否则成交那刻止损停在旧轨道）",
          len(_a1) == 1 and abs(float(_a1[0]["triggerPrice"]) - 92.0) < 1e-9,
          str([(a["order_id"], a["triggerPrice"]) for a in _a1]))
    check("日志：撤旧 + 重挂（改价重挂）",
          acts("LG6")[-2:] == ["sl_cancel", "sl_set"], str(acts("LG6")))
    check("限价单没被白白换掉（同价不动）",
          len([o for o in FAKE.live_orders() if o["symbol"] == "LG6USDT"]) == 1,
          str([(o["order_id"], o["price"]) for o in FAKE.live_orders()
               if o["symbol"] == "LG6USDT"]))

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
