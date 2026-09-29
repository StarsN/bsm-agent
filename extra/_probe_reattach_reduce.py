"""探针：「恢复中」期间交易员减仓 —— 实盘会被按**旧 k** 减掉吗？

背景：`apply_signal` 的 REDUCE 分支没有托管守卫（只有 OPEN/ADD 有，:812），而
纸盘层的 `_align_our_position` 对"恢复中(2)"是**放行**的（追平语义）。所以理论上
"恢复中"的仓可能带着 executed=True 的 REDUCE 信号进实盘层 —— 而那一刻记录里的 k
还是**没按现状重冻的旧值**，目标量会算成"交易员新量 × 旧 k"，把未托管期间手动加
的仓按旧目标市价减掉。

跑真引擎（process → apply_signal → 对账），只观察不修数据、不做断言。
"""
import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
_T = tempfile.mkdtemp(prefix="af_probe_")
config.DB_PATH = os.path.join(_T, "main.db")
config.SUPERTREND_DB = os.path.join(_T, "st.db")
config.LIVE_DB_PATH = os.path.join(_T, "live.db")
config.ALPHAFOX_DB = os.path.join(_T, "af.db")
config.BINANCE_HEDGE_MODE = True

import storage  # noqa: E402
import alphafox_trade as aft  # noqa: E402
import alphafox_live as afl  # noqa: E402

with storage.get_conn() as _c:
    _c.execute("PRAGMA journal_mode=WAL")
    _c.executescript(storage.SCHEMA)
    _c.executescript("""
    DROP TABLE IF EXISTS trade_signal_locks;
    ALTER TABLE trade_positions ADD COLUMN managed INTEGER DEFAULT 1;
    CREATE TABLE trade_signal_locks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL,
        signal_key TEXT NOT NULL, strategy TEXT DEFAULT 'agent',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(token, signal_key, strategy));
    """)
aft.init_db()
storage.init_onchain_live_db()

PRICE = {"TOKR": 100.0}
aft.get_mark_price = lambda tok: PRICE.get(tok)
aft.has_tradable_perp = lambda tok: True
afl.get_mark_price = lambda tok: PRICE.get(tok)
aft._map_cache = {}


class FAKE:
    positions = []
    orders = []
    lev = {}
    _id = 9000
    balance = 500.0

    @classmethod
    def get_account_info(cls):
        return {"available_balance": cls.balance, "equity": cls.balance}

    @classmethod
    def get_balance(cls):
        return cls.balance

    @classmethod
    def get_positions(cls):
        return [dict(p) for p in cls.positions if abs(p["quantity"]) > 1e-9]

    get_last_error = classmethod(lambda c: "")
    _round_qty = staticmethod(lambda symbol, qty: round(qty, 8))
    _round_qty_down = staticmethod(lambda symbol, qty: round(qty, 8))
    _fetch_symbol_filters = staticmethod(
        lambda symbol: {"stepSize": 0.0, "minQty": 0.0, "priceStep": 0.01})

    @classmethod
    def set_leverage(cls, symbol, leverage):
        cls.lev[symbol] = leverage
        return True

    @classmethod
    def _open(cls, tok, side, qty):
        sym = f"{tok}USDT"
        price = PRICE.get(tok, 0.0)
        cls._id += 1
        cls.orders.append({"id": cls._id, "token": tok, "side": side,
                           "qty": qty, "price": price, "action": "open"})
        for p in cls.positions:
            if p["symbol"] == sym and p["position_side"] == side:
                p["quantity"] += qty
                return {"order_id": cls._id, "status": "FILLED",
                        "price": price, "quantity": qty}
        cls.positions.append({"symbol": sym, "quantity": qty, "entry_price": price,
                              "mark_price": price, "unrealized_pnl": 0.0,
                              "leverage": cls.lev.get(sym, 3),
                              "position_side": side})
        return {"order_id": cls._id, "status": "FILLED", "price": price,
                "quantity": qty}

    market_open_long = classmethod(lambda c, tok, qty: c._open(tok, "LONG", qty))

    @classmethod
    def close_position_market(cls, tok, qty, position_side="LONG"):
        sym = f"{tok}USDT"
        price = PRICE.get(tok, 0.0)
        cls._id += 1
        cls.orders.append({"id": cls._id, "token": tok, "side": position_side,
                           "qty": qty, "price": price, "action": "close"})
        for p in cls.positions:
            if p["symbol"] == sym and p["position_side"] == position_side:
                p["quantity"] = max(p["quantity"] - qty, 0)
        return {"order_id": cls._id, "price": price, "quantity": qty,
                "status": "FILLED"}

    @classmethod
    def pos_qty(cls, tok, side="LONG"):
        return sum(p["quantity"] for p in cls.positions
                   if p["symbol"] == f"{tok}USDT"
                   and p["position_side"] == side)


afl.exchange = FAKE
PARAMS = {"budget": 1000.0, "slip": 0.05, "fee": 0.04, "allow_open": True,
          "live_notional": 0.0}
LIVE = (True, 0.0, 3.0)
TRADER_MARGIN = 100000.0
K = FAKE.balance / TRADER_MARGIN          # 0.005
TR = "bn-PROBE"
TOK = "TOKR"


def snap(positions, orders=None):
    return {"data": {"traderName": "探针", "marginBalance": TRADER_MARGIN,
                     "positions": positions, "latestOrders": orders or []}}


def tp(symbol, side, qty, avg, mark, lev=3):
    return {"symbol": symbol, "side": side, "absQty": qty, "avgPrice": avg,
            "markPrice": mark, "notional": qty * mark, "leverage": lev}


def mk_order(action, qty, price, t):
    canon = {"OPEN": "open_long", "ADD": "increase_long",
             "REDUCE": "decrease_long", "CLOSE": "close_long"}[action]
    return {"symbol": f"{TOK}USDT", "canonicalAction": canon, "side": "BUY",
            "executedQty": qty, "avgPrice": price, "notional": qty * price,
            "time": t, "realizedPnl": 0.0}


def poll(positions, orders=None):
    sigs = aft.process(TR, snap(positions, orders), PARAMS)
    print(f"   [信号] {[(s['action'], s['executed']) for s in sigs]}")
    for s in sigs:
        afl.apply_signal(s, LIVE[0], LIVE[1], LIVE[2])
    afl.sync_alphafox_live(LIVE[0], LIVE[1], LIVE[2])
    return sigs


def paper():
    with storage.get_conn() as c:
        r = c.execute("SELECT * FROM trade_positions WHERE token=? "
                      "AND strategy='alphafox' ORDER BY id DESC LIMIT 1",
                      (TOK,)).fetchone()
        return dict(r) if r else None


def live():
    with storage.get_conn(config.LIVE_DB_PATH) as c:
        r = c.execute("SELECT * FROM live_positions WHERE token=? "
                      "AND strategy='alphafox' ORDER BY id DESC LIMIT 1",
                      (TOK,)).fetchone()
        return dict(r) if r else None


def report(tag):
    p, lr = paper(), live()
    k = json.loads((lr or {}).get("exchange_orders_json") or "{}").get("k")
    print(f"  [{tag}] 纸盘净量={float(p['quantity']) - float(p['closed_qty'] or 0):.6g}"
          f" managed={p['managed']} status={p['status']}"
          f" | 记录状态={(lr or {}).get('status')} 量={float((lr or {}).get('quantity') or 0):.6g} k={k}"
          f" | 交易所={FAKE.pos_qty(TOK):.6g}")


print("========== 1. 交易员开仓 10 → 纸盘 0.1 / 实盘 0.05 ==========")
aft.set_follow(TR, "探针", "", True)
aft._prev_snap.clear()
aft._seen_orders.clear()
poll([])
poll([tp(f"{TOK}USDT", "LONG", 10, 100.0, 100.0)], [mk_order("OPEN", 10, 100.0, 1000)])
report("开仓后")

print("\n========== 2. 取消托管 + 在币安手动加到 0.15 ==========")
with storage.get_conn() as c:
    c.execute("UPDATE trade_positions SET managed=0 WHERE id=?", (paper()["id"],))
for _p in FAKE.positions:
    if _p["symbol"] == f"{TOK}USDT":
        _p["quantity"] = 0.15
poll([tp(f"{TOK}USDT", "LONG", 10, 100.0, 100.0)])
report("事实同步后")

print("\n========== 3. 点「恢复托管」，但实盘侧未就绪（留在恢复中） ==========")
with storage.get_conn() as c:
    c.execute("UPDATE trade_positions SET managed=2 WHERE id=?", (paper()["id"],))
_real = afl.on_reattach
afl.on_reattach = lambda *a, **k: False       # 模拟"交易所查询这一轮不可用"
poll([tp(f"{TOK}USDT", "LONG", 10, 100.0, 100.0)])
report("未就绪一轮后")

print("\n========== 4. 恢复中期间交易员减仓 10 → 5 ==========")
_o_before = len(FAKE.orders)
poll([tp(f"{TOK}USDT", "LONG", 5, 100.0, 100.0)])
report("交易员减仓后")
_new = FAKE.orders[_o_before:]
print(f"  [交易所下单] {_new}")

print("\n========== 5. 恢复托管落定（接口恢复） ==========")
afl.on_reattach = _real
poll([tp(f"{TOK}USDT", "LONG", 5, 100.0, 100.0)], [mk_order("REDUCE", 5, 100.0, 2000)])
report("落定后")

print("\n结论：第 4 步若出现 action='close' 的单，就是「恢复中按旧 k 减仓」的洞。")
