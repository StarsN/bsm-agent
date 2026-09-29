"""探针：取消托管期间交易员改过仓 → 再点「⚡ 快速跟随」会发生什么？

怀疑点（读代码得出）：`quick_follow` 的"已有仓"分支里
    _align_our_position(...)      # 对 managed=0/3 会被守卫直接 return False
    _set_managed(row, ON)         # 但返回值被忽略，状态照样翻成"托管中"
→ 纸盘没有追平到交易员当前量，`_trader_qty_of(pp)` 还是**旧量**，
  而实盘目标 = 旧量 × 冻结k → 与交易所现状（用户手动加过）不等 → **反向减仓**。

跑真引擎（process → quick_follow → 对账），只观察不下结论。
"""
import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
_T = tempfile.mkdtemp(prefix="af_qfstale_")
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

PRICE = {}
aft.get_mark_price = lambda tok: PRICE.get(tok)
aft.has_tradable_perp = lambda tok: True
afl.get_mark_price = lambda tok: PRICE.get(tok)
aft._map_cache = {}


class FAKE:
    positions = []
    orders = []
    lev = {}
    _id = 9000
    balance = 1000.0

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
        price = PRICE.get(tok, 100.0)
        cls._id += 1
        cls.orders.append({"id": cls._id, "token": tok, "act": "open", "qty": qty})
        for p in cls.positions:
            if p["symbol"] == sym and p["position_side"] == side:
                p["quantity"] += qty
                return {"order_id": cls._id, "status": "FILLED",
                        "price": price, "quantity": qty}
        cls.positions.append({"symbol": sym, "quantity": qty, "entry_price": price,
                              "mark_price": price, "unrealized_pnl": 0.0,
                              "leverage": cls.lev.get(sym, 3), "position_side": side})
        return {"order_id": cls._id, "status": "FILLED", "price": price,
                "quantity": qty}

    market_open_long = classmethod(lambda c, tok, qty: c._open(tok, "LONG", qty))

    @classmethod
    def close_position_market(cls, tok, qty, position_side="LONG"):
        sym = f"{tok}USDT"
        price = PRICE.get(tok, 100.0)
        cls._id += 1
        cls.orders.append({"id": cls._id, "token": tok, "act": "close", "qty": qty})
        for p in cls.positions:
            if p["symbol"] == sym and p["position_side"] == position_side:
                p["quantity"] = max(p["quantity"] - qty, 0)
        return {"order_id": cls._id, "price": price, "quantity": qty,
                "status": "FILLED"}

    @classmethod
    def pos_qty(cls, tok, side="LONG"):
        return sum(p["quantity"] for p in cls.positions
                   if p["symbol"] == f"{tok}USDT" and p["position_side"] == side)


afl.exchange = FAKE
PARAMS = {"budget": 1000.0, "slip": 0.05, "fee": 0.04, "allow_open": True,
          "live_notional": 0.0}
LIVE = (True, 0.0, 3.0)
TM = 100000.0
K = FAKE.balance / TM                      # 0.01
TOK = "STALE"
TR = "bn-ST"


def snap(positions):
    return {"data": {"traderName": "陈旧探针", "marginBalance": TM,
                     "positions": positions, "latestOrders": []}}


def tp(qty, avg=100.0, mark=100.0, lev=3):
    return {"symbol": f"{TOK}USDT", "side": "LONG", "absQty": qty, "avgPrice": avg,
            "markPrice": mark, "notional": qty * mark, "leverage": lev}


def poll(positions):
    sigs = aft.process(TR, snap(positions), PARAMS)
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
                      "AND strategy='alphafox' AND status IN ('OPEN','PARTIAL') "
                      "ORDER BY id DESC LIMIT 1", (TOK,)).fetchone()
        return dict(r) if r else None


def stock(tag):
    p, lr = paper(), live()
    net = float(p["quantity"]) - float(p["closed_qty"] or 0)
    kq = json.loads(p["signal_snapshot"] or "{}").get("k_qty")
    implied = net / float(kq) if kq else 0.0
    k = json.loads((lr or {}).get("exchange_orders_json") or "{}").get("k")
    print(f"  [{tag}] 纸盘净量={net:.6g}（反推交易员 {implied:.6g}） managed={p['managed']} "
          f"| 记录量={float((lr or {}).get('quantity') or 0):.6g} k={k} "
          f"| 交易所={FAKE.pos_qty(TOK):.6g}")


print("========== 1. 交易员开仓 10 → 纸盘 0.1 / 实盘 0.1 ==========")
aft.set_follow(TR, "陈旧探针", "", True)
aft._prev_snap.clear()
PRICE[TOK] = 100.0
poll([])
poll([tp(10)])
stock("开仓后")

print("\n========== 2. 取消托管 ==========")
with storage.get_conn() as c:
    c.execute("UPDATE trade_positions SET managed=0 WHERE id=?", (paper()["id"],))
poll([tp(10)])
stock("取消托管后")

print("\n========== 3. 交易员加到 20（纸盘不跟 → 纸盘反推仍是 10）==========")
poll([tp(20)])
stock("交易员加仓后")

print("\n========== 4. 用户在交易所手动加到 0.30 ==========")
for _p in FAKE.positions:
    if _p["symbol"] == f"{TOK}USDT":
        _p["quantity"] = 0.30
poll([tp(20)])
stock("事实同步后")

print("\n========== 5. 点「⚡ 快速跟随」==========")
_n = len(FAKE.orders)
r = aft.quick_follow(TR, snap([tp(20)]), PARAMS)
print("   返回:", json.dumps(r, ensure_ascii=False)[:300])
stock("快速跟随当轮")
print("   本轮交易所下单:", FAKE.orders[_n:])

print("\n========== 6. 再走一轮对账 ==========")
_n2 = len(FAKE.orders)
poll([tp(20)])
stock("下一轮")
print("   本轮交易所下单:", FAKE.orders[_n2:])

print("\n结论：若第 5/6 步出现 action='close' 的单、且交易所从 0.30 掉回 0.10，")
print("      就是「纸盘没追平 → 实盘按旧量反向减仓」——用户手动加的仓被抹掉。")
