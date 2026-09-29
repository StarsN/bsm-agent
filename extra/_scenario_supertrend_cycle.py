"""主流趋势「完整生命周期」流程走查 —— 跑**真引擎**，不是手工摆 DB 状态。

已有套件（test_supertrend_live / test_coexist_sync / test_double_sl）都是先手工
INSERT 纸盘行、再验证镜像层在某个状态下的行为。这一套反过来：用合成的 OKX 信号
驱动真的 `supertrend_strategy._run_round()`（内部 = 合并 → 逐币 `_process_token`
→ 实盘镜像对账），再用真的 1s 循环 `trade_logic.update_paper_positions` 推进成交/
扫损。每一步都验四项一致性：

    (a) 纸盘活跃持仓量 == 交易所该币持仓量
    (b) 纸盘活跃行数 == live 活跃记录数（OPEN/PARTIAL 与 PENDING 分开数）
    (c) SL 总覆盖 == 交易所持仓 + 纸盘挂单量（挂单腿的 SL 是预挂的）
    (d) 交易所上每张 NEW 单都能在 live 记录的 exchange_orders_json 里找到（无孤儿）

走查的生命周期：
  A（BOTH）首次加入 → 等闭合K线 → 挂单 → 限价成交 → SL 跟随 →
          扫损 → 价格到位市价开 → 反转反手（挂空单）
  B（LONG_ONLY + 反转市价）方向受限等待 → 反转（容忍内）市价开+同轮挂单并存 →
          挂单腿成交合并 → 扫损 → 反转但**超容忍** → 只挂单（留痕）

运行：python extra/_scenario_supertrend_cycle.py
"""
import io
import json
import os
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

_TMP = tempfile.mkdtemp(prefix="st_cycle_")
config.DB_PATH = os.path.join(_TMP, "main.db")
config.SUPERTREND_DB = os.path.join(_TMP, "supertrend.db")
config.LIVE_DB_PATH = os.path.join(_TMP, "live.db")

import storage  # noqa: E402
import trade_logic  # noqa: E402
import supertrend_strategy as st  # noqa: E402
import supertrend_live as stl  # noqa: E402

with storage.get_conn() as _c:
    _c.execute("PRAGMA journal_mode = WAL")
    _c.executescript(storage.SCHEMA)
    _c.executescript("""
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
st.init_db()
storage.init_onchain_live_db()

# ---- 隔离：不发网络请求、不真等待 ----
st.time.sleep = lambda *a, **k: None
import exchange as _ex  # noqa: E402

_ex._fetch_symbol_filters = lambda symbol: {
    "stepSize": 0.001, "minQty": 0.0, "priceStep": 0.01}

MOCK_SIG, PRICE = {}, {}
st._fetch_okx_supertrend = lambda tok: MOCK_SIG.get(tok)
st.get_mark_price = lambda tok: PRICE.get(tok)
trade_logic.get_mark_price = lambda tok: PRICE.get(tok)

import tg_notify  # noqa: E402

tg_notify.send_supertrend_reversal = lambda *a, **k: None

CANDLE = 15 * 60 * 1000
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
    """币安 mock：内存持仓/挂单（字段名对齐币安真实名，见 test_supertrend_live 的教训）"""

    def __init__(self):
        self.positions = []
        self.orders = []
        self.algo_orders = []
        self._next_id = 9000
        self.sl_calls = 0        # 挂 SL 的调用次数（查"同一轮重复重挂"）
        self.close_calls = 0     # 市价平仓调用次数（查"越界减仓"）

    # ---- 查询 ----
    @staticmethod
    def _sym(tok):
        return f"{tok}USDT"

    def get_positions(self):
        return [dict(p) for p in self.positions if abs(p["quantity"]) > 1e-9]

    def get_positions_or_none(self):
        return self.get_positions()

    def get_open_orders(self, token=None):
        rows = [o for o in self.orders if o["status"] == "NEW"]
        if token:
            rows = [o for o in rows if o["symbol"] == self._sym(token)]
        return [dict(o, orderId=o["order_id"],
                     executedQty=float(o.get("executed_qty") or 0)) for o in rows]

    def get_open_orders_or_none(self, token=None):
        return self.get_open_orders(token)

    def get_algo_orders(self, token=None):
        return [o for o in self.algo_orders if o["status"] == "NEW"]

    def get_last_error(self):
        return ""

    def get_account_info(self):
        return {"equity": 1000.0, "margin_balance": 500.0,
                "available_balance": 500.0, "unrealized_pnl": 0.0}

    def get_balance(self):
        return 1000.0

    # ---- 下单/撤单 ----
    def set_leverage(self, symbol, leverage):
        return True

    def _market_open(self, tok, side, quantity, price=None):
        sym = self._sym(tok)
        price = float(price if price is not None else PRICE.get(tok, 100.0))
        quantity = round(quantity, 8)
        for p in self.positions:
            if p["symbol"] == sym and p["position_side"] == side:
                total = p["quantity"] + quantity
                p["entry_price"] = ((p["entry_price"] * p["quantity"]
                                     + price * quantity) / total)
                p["quantity"] = total
                self._next_id += 1
                return {"order_id": self._next_id, "status": "FILLED",
                        "price": price, "quantity": quantity}
        self.positions.append({"symbol": sym, "quantity": quantity,
                               "entry_price": price, "mark_price": price,
                               "unrealized_pnl": 0.0, "leverage": 3,
                               "position_side": side})
        self._next_id += 1
        return {"order_id": self._next_id, "status": "FILLED",
                "price": price, "quantity": quantity}

    def market_open_long(self, tok, quantity):
        return self._market_open(tok, "LONG", quantity)

    def market_open_short(self, tok, quantity):
        return self._market_open(tok, "SHORT", quantity)

    def limit_open(self, tok, side, quantity, price, position_side="LONG"):
        self._next_id += 1
        self.orders.append({"order_id": self._next_id, "symbol": self._sym(tok),
                            "side": side, "type": "LIMIT", "price": price,
                            "quantity": quantity,
                            "position_side": position_side, "status": "NEW"})
        return {"order_id": self._next_id, "status": "NEW",
                "price": price, "quantity": quantity}

    def stop_loss_order(self, tok, side, quantity, stop_price, position_side="LONG"):
        self.sl_calls += 1
        self._next_id += 1
        self.algo_orders.append({"order_id": self._next_id, "symbol": self._sym(tok),
                                 "side": side, "type": "STOP_MARKET",
                                 "triggerPrice": stop_price, "quantity": quantity,
                                 "position_side": position_side, "status": "NEW"})
        return {"order_id": self._next_id, "status": "NEW"}

    def cancel_order(self, tok, order_id):
        for seq in (self.orders, self.algo_orders):
            for o in seq:
                if o["order_id"] == order_id and o["status"] == "NEW":
                    o["status"] = "CANCELED"
                    return True
        return True          # 已不在挂单列表 = 目标达成（同真实 exchange 语义）

    def cancel_all_orders(self, tok):
        sym = self._sym(tok)
        for seq in (self.orders, self.algo_orders):
            for o in seq:
                if o["symbol"] == sym:
                    o["status"] = "CANCELED"
        return True

    def close_position_market(self, tok, quantity, position_side="LONG"):
        self.close_calls += 1
        sym = self._sym(tok)
        price = float(PRICE.get(tok, 100.0))
        qty = round(quantity, 8)
        for p in self.positions:
            if p["symbol"] == sym and p["position_side"] == position_side:
                p["quantity"] = max(p["quantity"] - qty, 0)
        self._next_id += 1
        return {"order_id": self._next_id, "price": price,
                "quantity": qty, "status": "FILLED"}

    def _round_qty(self, symbol, qty):
        return round(qty, 8)

    def _round_qty_down(self, symbol, qty):
        return round(qty, 8)

    # ---- 断言辅助 ----
    def pos_qty(self, tok, side=None):
        rows = [p for p in self.positions if p["symbol"] == self._sym(tok)
                and (side is None or p["position_side"] == side)]
        return sum(p["quantity"] for p in rows)

    def live_orders(self):
        return [o for o in self.orders if o["status"] == "NEW"]

    def live_algos(self):
        return [o for o in self.algo_orders if o["status"] == "NEW"]


FAKE = FakeExchange()
stl.exchange = FAKE
stl._rounds_done = 99          # 越过启动冷窗


# ---- 状态与一致性 ----
def paper_rows(tok, statuses=("OPEN", "PARTIAL", "PENDING")):
    ph = ",".join("?" for _ in statuses)
    with storage.get_conn() as mc:
        return [dict(r) for r in mc.execute(
            f"SELECT * FROM trade_positions WHERE token=? AND strategy='supertrend' "
            f"AND status IN ({ph})", (tok, *statuses))]


def live_rows(tok, statuses=("OPEN", "PARTIAL", "PENDING")):
    ph = ",".join("?" for _ in statuses)
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        return [dict(r) for r in lc.execute(
            f"SELECT * FROM live_positions WHERE token=? AND strategy='supertrend' "
            f"AND status IN ({ph})", (tok, *statuses))]


def all_logs(tok=None):
    sql = ("SELECT * FROM live_trade_log WHERE strategy='supertrend'")
    args = ()
    if tok:
        sql += " AND token=?"
        args = (tok,)
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        return [dict(r) for r in lc.execute(sql + " ORDER BY id", args).fetchall()]


def snapshot(tok):
    p = paper_rows(tok)
    l = live_rows(tok)
    p_open = [r for r in p if r["status"] in ("OPEN", "PARTIAL")]
    p_pend = [r for r in p if r["status"] == "PENDING"]
    l_open = [r for r in l if r["status"] in ("OPEN", "PARTIAL")]
    l_pend = [r for r in l if r["status"] == "PENDING"]
    owned = set()
    for r in l:
        try:
            o = json.loads(r["exchange_orders_json"] or "{}")
        except ValueError:
            o = {}
        for k in ("limit", "sl"):
            if o.get(k):
                owned.add(int(o[k]))
    orphan = [o["order_id"] for o in FAKE.live_orders() + FAKE.live_algos()
              if o["symbol"] == f"{tok}USDT" and o["order_id"] not in owned]
    return {
        "p_qty": sum(max(float(r["quantity"] or 0) - float(r["closed_qty"] or 0), 0)
                     for r in p_open),
        "p_pend_qty": sum(float(r["quantity"] or 0) for r in p_pend),
        "p_open_n": len(p_open), "p_pend_n": len(p_pend),
        "l_open_n": len(l_open), "l_pend_n": len(l_pend),
        "ex_qty": FAKE.pos_qty(tok),
        "ex_lim_n": len([o for o in FAKE.live_orders()
                         if o["symbol"] == f"{tok}USDT"]),
        "sl_n": len([a for a in FAKE.live_algos() if a["symbol"] == f"{tok}USDT"]),
        "sl_qty": sum(float(a["quantity"]) for a in FAKE.live_algos()
                      if a["symbol"] == f"{tok}USDT"),
        "orphan": orphan,
        "live": [(r["id"], r["status"], round(float(r["quantity"] or 0), 8))
                 for r in l],
    }


def show(tag, tok):
    s = snapshot(tok)
    print(f"    {tag}: 纸盘 {s['p_qty']:.6g}({s['p_open_n']}行)/挂{s['p_pend_qty']:.6g}"
          f" | 交易所 {s['ex_qty']:.6g}/{s['ex_lim_n']}单 | SL {s['sl_n']}张"
          f"{s['sl_qty']:.6g} | live {s['live']}")
    return s


def verify(tok, tag, expect_pend=None):
    """四项一致性（每一步都跑）。expect_pend：纸盘挂单量（None=不检查）"""
    s = show(tag, tok)
    ok = True
    if abs(s["p_qty"] - s["ex_qty"]) > 1e-8:
        check(f"{tag} (a) 纸盘持仓 == 交易所持仓", False,
              f"{s['p_qty']:.8f} vs {s['ex_qty']:.8f}")
        ok = False
    if s["p_open_n"] != s["l_open_n"] or s["p_pend_n"] != s["l_pend_n"]:
        check(f"{tag} (b) 纸盘行数 == live 记录数", False,
              f"纸盘 {s['p_open_n']}/{s['p_pend_n']} vs live "
              f"{s['l_open_n']}/{s['l_pend_n']}")
        ok = False
    if abs(s["sl_qty"] - (s["ex_qty"] + s["p_pend_qty"])) > 1e-8:
        check(f"{tag} (c) SL 覆盖 == 持仓 + 挂单量", False,
              f"SL {s['sl_qty']:.8f} vs {s['ex_qty'] + s['p_pend_qty']:.8f}")
        ok = False
    if s["orphan"]:
        check(f"{tag} (d) 无孤儿挂单", False, str(s["orphan"]))
        ok = False
    if expect_pend is not None and abs(s["p_pend_qty"] - expect_pend) > 1e-8:
        check(f"{tag} 挂单量 == {expect_pend}", False, f"{s['p_pend_qty']:.8f}")
        ok = False
    if ok:
        check(f"{tag} 四项一致性", True)
    return s


# ---- 引擎驱动 ----
def closed_ts(offset=0):
    now_ms = int(time.time() * 1000)
    return (now_ms // CANDLE) * CANDLE - CANDLE + offset * CANDLE


def set_sig(tok, trend, upper, lower):
    # 已有信号 → 保留它的 ts（否则会把 next_candle 推进的那一根重置回去，
    # 引擎就永远看不到"新闭合K线"，整条流程走不动）
    ts = MOCK_SIG[tok]["ts"] if tok in MOCK_SIG else closed_ts()
    MOCK_SIG[tok] = {"ts": ts, "trend": trend,
                     "upper_band": float(upper), "lower_band": float(lower)}


def next_candle(tok):
    MOCK_SIG[tok]["ts"] += CANDLE


def engine(tok_price=None):
    """跑真引擎一轮：合并 → 逐币 _process_token → 实盘镜像对账"""
    if tok_price:
        for k, v in tok_price.items():
            PRICE[k] = v
    st._run_round()


def tick(price):
    """跑真 1s 循环（成交/扫损都由它推进）"""
    for k, v in price.items():
        PRICE[k] = v
    with storage.get_conn() as mc:
        trade_logic.update_paper_positions(mc)


def add_token(tok, direction="BOTH", flip=0, flip_tol=0.0, live=1):
    with storage.get_conn(config.SUPERTREND_DB) as c:
        c.execute("INSERT OR REPLACE INTO supertrend_tokens "
                  "(token, status, added_at, live_mode, direction, flip_market_open, "
                  "flip_market_tolerance_pct) VALUES (?,?,?,?,?,?,?)",
                  (tok, "ACTIVE", int(time.time()), live, direction, flip, flip_tol))


def fill_limit_real(tok):
    """模拟交易所限价单**真成交**：单子消失 + 按该单数量/价格出现持仓"""
    for o in FAKE.orders:
        if o["symbol"] == f"{tok}USDT" and o["status"] == "NEW" \
                and o["type"] == "LIMIT":
            o["status"] = "FILLED"
            FAKE._market_open(tok, o["position_side"], o["quantity"],
                              price=o["price"])
            return o
    return None


def sl_trigger_real(tok, price):
    """模拟交易所 STOP_MARKET 触发：SL 单成交 + 持仓清零"""
    for a in FAKE.algo_orders:
        if a["symbol"] == f"{tok}USDT" and a["status"] == "NEW":
            a["status"] = "FILLED"
            break
    FAKE.positions = [p for p in FAKE.positions if p["symbol"] != f"{tok}USDT"]


def open_reason_of(tok):
    with storage.get_conn() as mc:
        r = mc.execute("SELECT open_reason FROM trade_positions WHERE token=? "
                       "AND strategy='supertrend' ORDER BY id DESC LIMIT 1",
                       (tok,)).fetchone()
    return (r["open_reason"] if r else "") or ""


# =====================================================================
print("========== A. BOTH：完整生命周期 ==========")
A = "CYC"
add_token(A, direction="BOTH")
PRICE[A] = 100.0
set_sig(A, "UP", 110.0, 95.0)

print("---- A1 首次加入：只记基准，不打单（等闭合K线）----")
engine()
s = show("A1", A)
check("A1 无纸盘行、无 live 记录、交易所空",
      not paper_rows(A) and not live_rows(A) and s["ex_qty"] == 0,
      str(s["live"]))

print("---- A2 新闭合K线（UP）→ 挂单 + 实盘限价 + 预挂 SL ----")
next_candle(A)
set_sig(A, "UP", 110.0, 95.0)
engine()
s = verify(A, "A2")
check("A2 挂单价 = 下轨×(1+挂单容忍1%)", abs(s["p_pend_qty"] - 50.0 / (95 * 1.01)) < 1e-8,
      str(s["p_pend_qty"]))
check("A2 交易所 1 张限价单", s["ex_lim_n"] == 1, str(s["ex_lim_n"]))

print("---- A3 价格触及挂单价 → 交易所成交 → 1s 循环转 OPEN → 镜像转 OPEN ----")
PRICE[A] = 95.5
_lim_qty = float(fill_limit_real(A)["quantity"])
tick({A: 95.5})
show("A3-纸盘成交后（引擎还没跑，镜像暂时落后是正常的）", A)
check("A3 纸盘挂单已转 OPEN", not paper_rows(A, ("PENDING",))
      and len(paper_rows(A, ("OPEN", "PARTIAL"))) == 1,
      str([(r["id"], r["status"]) for r in paper_rows(A)]))
engine()
s = verify(A, "A3-镜像")
check("A3 交易所持仓 == 挂单腿数量", abs(s["ex_qty"] - _lim_qty) < 1e-8,
      f"{s['ex_qty']} vs {_lim_qty}")
check("A3 SL 只有 1 张（预挂的那张按实际成交量校正）", s["sl_n"] == 1, str(s["sl_n"]))

print("---- A4 新K线轨道上移 → 纸盘 SL 上移 → 实盘撤旧挂新 ----")
next_candle(A)
set_sig(A, "UP", 112.0, 99.0)
engine()
s = verify(A, "A4")
check("A4 SL 仍只有 1 张（撤旧挂新，不是并存两张）", s["sl_n"] == 1, str(s["sl_n"]))
check("A4 实盘 SL 已到新轨道 99×0.99",
      any(abs(float(a["triggerPrice"]) - 99 * 0.99) < 1e-6
          for a in FAKE.live_algos() if a["symbol"] == f"{A}USDT"),
      str([a["triggerPrice"] for a in FAKE.live_algos()]))

print("---- A4b 同一根K线内重复跑：不许反复重挂 SL（churn）----")
_ids0 = sorted(a["order_id"] for a in FAKE.live_algos()
               if a["symbol"] == f"{A}USDT")
_ords0 = sorted(o["order_id"] for o in FAKE.live_orders()
                if o["symbol"] == f"{A}USDT")
engine()
engine()
_ids1 = sorted(a["order_id"] for a in FAKE.live_algos()
               if a["symbol"] == f"{A}USDT")
_ords1 = sorted(o["order_id"] for o in FAKE.live_orders()
                if o["symbol"] == f"{A}USDT")
check("A4b 重复轮次不重挂 SL（单号集合不变）", _ids0 == _ids1,
      f"{_ids0} → {_ids1}")
check("A4b 重复轮次不重挂限价单", _ords0 == _ords1, f"{_ords0} → {_ords1}")
verify(A, "A4b")

print("---- A5 价格跌破 SL → 1s 循环扫损 → 镜像收敛 ----")
PRICE[A] = 97.0
sl_trigger_real(A, 97.0)
tick({A: 97.0})
check("A5 纸盘已被扫损平掉", not paper_rows(A),
      str([(r["id"], r["status"]) for r in paper_rows(A)]))
engine()
s = verify(A, "A5")
check("A5 live 记录全部收敛（不再占用该币）",
      not live_rows(A), str(s["live"]))

print("---- A6 空仓轮次：不重开（等新闭合K线）----")
engine()
s = verify(A, "A6")
check("A6 仍无纸盘行", not paper_rows(A), str(s["live"]))

print("---- A7 新K线：价格已跌到挂单价下方 → 直接市价开（价格到位）----")
next_candle(A)
set_sig(A, "UP", 113.0, 100.0)
engine()
s = verify(A, "A7")
check("A7 市价开：纸盘 1 行 OPEN、无挂单",
      s["p_open_n"] == 1 and s["p_pend_n"] == 0, str(s["live"]))

print("---- A8 趋势反转 DOWN：LONG 的 SL 只朝有利 → 不动 ----")
_sl_before = open_reason_of(A)
next_candle(A)
set_sig(A, "DOWN", 105.0, 90.0)
engine()
s = verify(A, "A8")
with storage.get_conn() as mc:
    _sl_now = mc.execute("SELECT stop_loss_price FROM trade_positions WHERE token=? "
                         "AND strategy='supertrend' AND status='OPEN'",
                         (A,)).fetchone()
check("A8 反转不降 SL（ratchet 生效）",
      _sl_now is not None and abs(float(_sl_now["stop_loss_price"]) - 100 * 0.99) < 1e-6,
      str(dict(_sl_now) if _sl_now else None))

print("---- A9 扫损 → 空仓 ----")
PRICE[A] = 98.0
sl_trigger_real(A, 98.0)
tick({A: 98.0})
engine()
s = verify(A, "A9")
check("A9 空仓且 live 收敛", not live_rows(A), str(s["live"]))

print("---- A10 新K线（趋势已 DOWN）→ 反手挂空单 ----")
next_candle(A)
set_sig(A, "DOWN", 105.0, 90.0)
engine()
s = verify(A, "A10")
with storage.get_conn() as mc:
    _p = mc.execute("SELECT side, limit_price FROM trade_positions WHERE token=? "
                    "AND strategy='supertrend' AND status='PENDING'",
                    (A,)).fetchone()
check("A10 反手挂的是空单（方向跟随新趋势）",
      _p is not None and _p["side"] == "SHORT", str(dict(_p) if _p else None))
check("A10 挂单价 = 上轨×(1−1%)",
      _p is not None and abs(float(_p["limit_price"]) - 105 * 0.99) < 1e-6,
      str(dict(_p) if _p else None))

# =====================================================================
print("\n========== B. LONG_ONLY + 反转市价（容忍） ==========")
B = "ONLY"
add_token(B, direction="LONG_ONLY", flip=1, flip_tol=0.5)
PRICE[B] = 100.0
set_sig(B, "DOWN", 105.0, 90.0)

print("---- B1 方向受限 + 趋势 DOWN → 等待，不打单 ----")
engine()
s = show("B1", B)
check("B1 无纸盘行（不在允许方向）", not paper_rows(B), str(s["live"]))

print("---- B2 反转 UP（容忍内）→ 市价开 + 同轮挂单（两腿并存）----")
next_candle(B)
set_sig(B, "UP", 110.0, 95.0)
engine()
s = verify(B, "B2")
check("B2 两腿并存：纸盘 1 市价仓 + 1 挂单",
      s["p_open_n"] == 1 and s["p_pend_n"] == 1, str(s["live"]))
check("B2 交易所 2 张 SL（每条腿各一张）", s["sl_n"] == 2, str(s["sl_n"]))

print("---- B3 挂单腿成交 → 合并成一笔 → 镜像收敛成一条记录 ----")
PRICE[B] = 95.5
_lim_qty = float(fill_limit_real(B)["quantity"])
tick({B: 95.5})
engine()
s = verify(B, "B3")
check("B3 合并后只剩 1 行 OPEN", s["p_open_n"] == 1 and s["p_pend_n"] == 0,
      str(s["live"]))
check("B3 live 只剩 1 条记录", s["l_open_n"] == 1 and s["l_pend_n"] == 0,
      str(s["live"]))
check("B3 只剩 1 张 SL（覆盖整仓）", s["sl_n"] == 1, str(s["sl_n"]))
# 这条腿的 live 记录是被**孤儿收敛**收掉的（纸盘挂单行已被合并 → 它不是当前活跃挂单），
# 撤掉的限价单 + 预挂 SL 都是真金白银动作，日志必须留痕（交接路径由 test_double_sl 覆盖）
_b3 = all_logs(B)


def _b3_reason(r):
    return (json.loads(r["details_json"]) if r.get("details_json") else {}).get("reason", "")


check("B3 孤儿挂单记录收敛留痕（限价单 + 预挂止损都被撤）",
      {"limit_cancel", "sl_cancel"} <=
      {r["action"] for r in _b3 if "孤儿" in _b3_reason(r)},
      str([(r["action"], _b3_reason(r)[:16]) for r in _b3[-5:]]))
check("B3 收敛后止损按整仓重挂也留痕",
      any(r["action"] == "sl_update" for r in _b3),
      str([(r["action"], _b3_reason(r)[:16]) for r in _b3[-5:]]))

print("---- B4 趋势转 DOWN → 持仓被扫损 → 空仓 ----")
next_candle(B)
set_sig(B, "DOWN", 105.0, 88.0)
engine()
_slB = None
with storage.get_conn() as mc:
    _r = mc.execute("SELECT stop_loss_price FROM trade_positions WHERE token=? "
                    "AND strategy='supertrend' AND status='OPEN'", (B,)).fetchone()
    _slB = float(_r["stop_loss_price"]) if _r else None
check("B4 前置：拿到了当前 SL", _slB is not None, str(_slB))
PRICE[B] = _slB - 0.5
sl_trigger_real(B, _slB - 0.5)
tick({B: _slB - 0.5})
engine()
s = verify(B, "B4")
check("B4 空仓且 live 收敛", not paper_rows(B) and not live_rows(B), str(s["live"]))

print("---- B5 反转回 UP 但**现价超容忍** → 不打市价、只挂限价单（留痕）----")
# 参考线语义：反转前那一轮写进 status 的**上轨**（DOWN 趋势画上轨）= 100，
# 容忍 0.5% → 阈值 100.5；现价 106 超出 → 跳过市价、只挂单。
# 用 SQL 直接摆好"上一轮写完"的状态（等价于 DOWN 那轮刚跑完），本轮是相邻的新 K 线。
set_sig(B, "UP", 110.0, 95.0)
next_candle(B)
_tsB = MOCK_SIG[B]["ts"]
with storage.get_conn(config.SUPERTREND_DB) as c:
    c.execute("UPDATE supertrend_status SET trend='DOWN', upper_band=100.0, "
              "lower_band=90.0, last_candle_ts=? WHERE token=?", (_tsB - CANDLE, B))
engine({B: 106.0})
s = verify(B, "B5")
check("B5 没有市价仓（只挂单）", s["p_open_n"] == 0 and s["ex_qty"] == 0,
      str(s["live"]))
check("B5 有 1 张限价单", s["p_pend_n"] == 1 and s["ex_lim_n"] == 1, str(s))
check("B5 留痕：open_reason 写了跳过原因",
      "反转市价跳过" in open_reason_of(B), open_reason_of(B))

print("---- B6 这张挂单后来成交了 → 实盘转 OPEN、预挂 SL 接管整段 ----")
PRICE[B] = 95.5
_limB = float(fill_limit_real(B)["quantity"])
tick({B: 95.5})
engine()
s = verify(B, "B6")
check("B6 纸盘挂单转 OPEN、无挂单", s["p_open_n"] == 1 and s["p_pend_n"] == 0,
      str(s["live"]))
check("B6 实盘记录转 OPEN、无 PENDING 记录",
      s["l_open_n"] == 1 and s["l_pend_n"] == 0, str(s["live"]))
check("B6 交易所持仓 == 该挂单成交的量", abs(s["ex_qty"] - _limB) < 1e-8,
      f"{s['ex_qty']} vs {_limB}")
check("B6 SL 只有 1 张（预挂那张直接接管，不重挂）", s["sl_n"] == 1, str(s["sl_n"]))

print("---- B7 单子成交后又跑一轮：不许重挂（churn）----")
_ids0 = sorted(a["order_id"] for a in FAKE.live_algos()
               if a["symbol"] == f"{B}USDT")
engine()
engine()
_ids1 = sorted(a["order_id"] for a in FAKE.live_algos()
               if a["symbol"] == f"{B}USDT")
check("B7 重复轮次不重挂 SL", _ids0 == _ids1, f"{_ids0} → {_ids1}")
verify(B, "B7")

# =====================================================================
print("\n========== D. 对账不重复动作 / 不丢动作 ==========")
D = "DBL"
add_token(D)
PRICE[D] = 100.0
set_sig(D, "UP", 110.0, 95.0)
engine()                        # 记基准
next_candle(D)
set_sig(D, "UP", 113.0, 100.0)
engine({D: 97.0})               # 价格已到位 → 市价开
_pq = snapshot(D)["p_qty"]
check("D-前置：纸盘/交易所/记录数量一致",
      abs(snapshot(D)["ex_qty"] - _pq) < 1e-8, str(snapshot(D)))


def _set_live_sl_qty(tok, val):
    with storage.get_conn(config.LIVE_DB_PATH) as _lc:
        _r = _lc.execute("SELECT id, exchange_orders_json FROM live_positions "
                         "WHERE token=? AND strategy='supertrend'", (tok,)).fetchone()
        _o = json.loads(_r["exchange_orders_json"])
        _o["sl_qty"] = val
        _lc.execute("UPDATE live_positions SET exchange_orders_json=? WHERE id=?",
                    (json.dumps(_o), _r["id"]))


print("---- D1 覆盖量过期 + 同时要补仓：同一轮只许重挂一次 SL ----")
for _p in FAKE.positions:                    # 交易所只剩一半（薄盘/手动减仓）
    if _p["symbol"] == f"{D}USDT":
        _p["quantity"] = round(_pq * 0.5, 8)
_set_live_sl_qty(D, round(_pq * 0.5, 8))     # 记录的覆盖量也停在半仓
FAKE.sl_calls = 0
engine()
check("D1-a 补仓补回来了", abs(snapshot(D)["ex_qty"] - _pq) < 1e-8,
      str(snapshot(D)["ex_qty"]))
check("D1-b 同一轮只重挂一次 SL（SL 块放前面会 cancel+place 两遍）",
      FAKE.sl_calls == 1, str(FAKE.sl_calls))
check("D1-c SL 覆盖量跟上持仓",
      abs(snapshot(D)["sl_qty"] - _pq) < 1e-8, str(snapshot(D)["sl_qty"]))

print("---- D2 「疑似手动仓」跳过减仓时，SL 维护不能跟着停 ----")
for _p in FAKE.positions:                    # 手动加仓：交易所远超镜像记录
    if _p["symbol"] == f"{D}USDT":
        _p["quantity"] = round(_pq * 4, 8)
_set_live_sl_qty(D, round(_pq * 0.1, 8))     # 覆盖量过期
_c0 = FAKE.close_calls
FAKE.sl_calls = 0
engine()
check("D2-a 没有越界减仓（手动仓不动）", FAKE.close_calls == _c0,
      f"{_c0} → {FAKE.close_calls}")
check("D2-b SL 照常重挂（不因混了手动仓就放弃我们的止损）",
      FAKE.sl_calls == 1, str(FAKE.sl_calls))
check("D2-c 重挂后的覆盖量 = 纸盘量",
      abs(snapshot(D)["sl_qty"] - _pq) < 1e-8, str(snapshot(D)["sl_qty"]))

# =====================================================================
print("\n========== F. 总开关关闭：不新开仓，但实盘对账照常（停策略≠撤单） ==========")
# 用户确认的语义：关掉总开关只是"不再开新仓"，已挂出的单**有意不撤**、
# 实盘对账不能停（否则币安 SL 触发/手动平仓后纸盘与 live 记录长期不收敛）。
F = "OFF1"
add_token(F, live=1)
PRICE[F] = 100.0
set_sig(F, "UP", 105.0, 95.0)
engine()                             # 首轮只记基准（等闭合K线）
next_candle(F)
set_sig(F, "UP", 105.0, 95.0)
engine()                             # 新K线 → 挂单 + 实盘限价 + 预挂 SL
_sf = verify(F, "F0 前置")
check("F0 前置：纸盘挂单 + 实盘限价单 + 预挂止损",
      _sf["p_pend_n"] == 1 and _sf["ex_lim_n"] == 1 and _sf["sl_n"] == 1, str(_sf))

with storage.get_conn() as _mc:
    storage.trading_settings_update(_mc, {"supertrend_enabled": False})
for _ in range(2):
    engine()
    tick({})
_sf = snapshot(F)
check("F1 关闭期间挂单/止损都还在（停策略≠撤单）",
      _sf["p_pend_n"] == 1 and _sf["ex_lim_n"] == 1 and _sf["sl_n"] == 1, str(_sf))

print("---- F2 关闭期间：挂单成交 + 扫损 → 实盘记录照样收敛 ----")
PRICE[F] = 95.0
fill_limit_real(F)
tick({F: 95.0})                      # 1s 循环把纸盘挂单转持仓（不依赖总开关）
engine()
check("F2 成交后：纸盘转持仓、实盘转 OPEN",
      snapshot(F)["p_open_n"] == 1 and snapshot(F)["l_open_n"] == 1,
      str(snapshot(F)))
_slF = float(paper_rows(F)[0]["stop_loss_price"])
PRICE[F] = _slF - 0.5
sl_trigger_real(F, _slF - 0.5)       # 交易所侧止损触发
tick({F: _slF - 0.5})                # 纸盘扫损
engine()
check("F2 关闭期间扫损：纸盘平 + live 记录收敛（镜像没停）",
      not paper_rows(F) and not live_rows(F), str(snapshot(F)))
_lf = all_logs(F)
check("F2 收敛过程留痕（close 日志）",
      any(r["action"] == "close" for r in _lf),
      str([(r["action"], r["id"]) for r in _lf[-4:]]))
with storage.get_conn() as _mc:
    storage.trading_settings_update(_mc, {"supertrend_enabled": True})

# =====================================================================
print("\n========== E. 交易日志审计（实盘动作留痕 + 稳定态零噪音） ==========")


# E1：先跑到稳定态（D2 的收尾是**跨轮**的：1s 循环扫损平掉纸盘仓 → 下一轮
# 实盘镜像才收敛，所以预热要跑够两轮），再验 3 轮零新增。
# 对账每轮都跑，稳定态还写流水 = 把有用的行淹没
for _ in range(3):
    engine()
    tick({})
_id_before = max((r["id"] for r in all_logs()), default=0)
for _ in range(3):
    engine()
    tick({})
_new_rows = [r for r in all_logs() if r["id"] > _id_before]
check("E1 稳定态零噪音：3 轮空转不写日志", not _new_rows,
      str([(r["token"], r["action"],
            (json.loads(r["details_json"] or "{}") or {}).get("reason"))
           for r in _new_rows]))

# E2：动作白名单（任何新 action 都要显式加进来，避免拼写错/临时名字流到现场）
ACT_WHITELIST = {"open", "close", "sl_set", "sl_update", "sl_cancel",
                 "limit_cancel", "rebalance_add", "rebalance_reduce", "rebuild"}
_logs = all_logs()
_acts = {r["action"] for r in _logs}
check("E2 日志动作都在白名单内", _acts and _acts <= ACT_WHITELIST,
      f"extra={_acts - ACT_WHITELIST} all={sorted(_acts)}")

# E3：实盘层写的动作，趋势实盘页必须有中文映射（否则页面直接显示英文 action）
import re                                    # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
from _mut_src import read_module_source      # noqa: E402（变异注入的源码优先）
_web = read_module_source("web")
# 只看趋势实盘页那一份 ACT_CN：web.py 里跟单托管页也有一份同名表（先出现），
# 直接全局 search 会抓到跟单那份 → 误报"页面缺映射"
_mh = re.search(r'SUPERTREND_LIVE_HTML = """(.*?)\n"""', _web, re.S)
_mm = re.search(r"const ACT_CN = \{(.*?)\};", _mh.group(1), re.S) if _mh else None
_page_acts = set(re.findall(r"(\w+)\s*:'", _mm.group(1))) if _mm else set()
check("E3 趋势实盘页 ACT_CN 覆盖实盘层的全部动作（否则显示英文）",
      bool(_mm) and _acts <= _page_acts, f"页面缺={sorted(_acts - _page_acts)}")

# E4：平过仓的币必须有 close 行；有 live 记录的币必须有开仓类行
with storage.get_conn() as _mc:
    _closed_toks = {r["token"] for r in _mc.execute(
        "SELECT DISTINCT token FROM trade_positions WHERE strategy='supertrend' "
        "AND status='CLOSED'")}
_close_toks = {r["token"] for r in _logs if r["action"] == "close"}
check("E4 每个平过仓的币都有 close 日志",
      _closed_toks <= _close_toks, f"缺={sorted(_closed_toks - _close_toks)}")

with storage.get_conn(config.LIVE_DB_PATH) as _lc:
    _live_toks = {r["token"] for r in _lc.execute(
        "SELECT DISTINCT token FROM live_positions WHERE strategy='supertrend'")}
_openish = {r["token"] for r in _logs if r["action"] in ("open", "rebuild")}
check("E5 每个有 live 记录的币都有开仓/补建日志",
      _live_toks <= _openish, f"缺={sorted(_live_toks - _openish)}")

# E6：同一个单号不该被撤两次（撤了没落盘 → 下轮再撤 = 撤单循环；日志会露出来）
_cancel_ids = [r["exchange_order_id"] for r in _logs
               if r["action"] in ("sl_cancel", "limit_cancel")
               and r["exchange_order_id"]]
check("E6 没有单号被重复撤（撤单循环会在这里露出来）",
      len(_cancel_ids) == len(set(_cancel_ids)),
      f"重复={[i for i in set(_cancel_ids) if _cancel_ids.count(i) > 1]}")

# E7：每行日志都要有 token / 时间 / 可读原因（页面三列都靠它）
_bad = [r["id"] for r in _logs
        if not r["token"] or not r["created_at"]
        or r["action"] not in ("open", "close") and not r["details_json"]]
check("E7 日志行都带 token/时间/原因", not _bad, str(_bad[:5]))

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
