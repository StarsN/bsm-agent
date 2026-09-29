"""探针：快速跟随在四种托管态下会不会出错（复现"纸盘没追平 → 按旧量减仓"）。

每个状态跑同一条链路：
  开仓 10 → 设成目标托管态 → 交易员加到 20（纸盘不跟、变陈旧）→
  我在币安手动加到 0.30 → 点「⚡ 快速跟随」→ 看纸盘/交易所/key 怎么变
"""
import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
_T = tempfile.mkdtemp(prefix="af_qfstates_")
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
    positions, orders, lev = [], [], {}
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
TR = "bn-QS"
STATE_CN = {0: "取消托管", 1: "托管中", 2: "恢复中", 3: "已脱离"}
TOKENS = {0: "QS0", 1: "QS1", 2: "QS2", 3: "QS3"}


def snap(positions):
    return {"data": {"traderName": "状态探针", "marginBalance": TM,
                     "positions": positions, "latestOrders": []}}


def tp(tok, qty, lev=3):
    return {"symbol": f"{tok}USDT", "side": "LONG", "absQty": qty, "avgPrice": 100.0,
            "markPrice": 100.0, "notional": qty * 100.0, "leverage": lev}


def poll(positions):
    sigs = aft.process(TR, snap(positions), PARAMS)
    for s in sigs:
        afl.apply_signal(s, LIVE[0], LIVE[1], LIVE[2])
    afl.sync_alphafox_live(LIVE[0], LIVE[1], LIVE[2])
    return sigs


def paper(tok):
    with storage.get_conn() as c:
        r = c.execute("SELECT * FROM trade_positions WHERE token=? "
                      "AND strategy='alphafox' ORDER BY id DESC LIMIT 1",
                      (tok,)).fetchone()
        return dict(r) if r else None


def live(tok):
    with storage.get_conn(config.LIVE_DB_PATH) as c:
        r = c.execute("SELECT * FROM live_positions WHERE token=? "
                      "AND strategy='alphafox' AND status IN ('OPEN','PARTIAL') "
                      "ORDER BY id DESC LIMIT 1", (tok,)).fetchone()
        return dict(r) if r else None


def stock(tag, tok):
    p, lr = paper(tok), live(tok)
    net = float(p["quantity"]) - float(p["closed_qty"] or 0)
    kq = float(json.loads(p["signal_snapshot"] or "{}").get("k_qty") or 0)
    k = json.loads((lr or {}).get("exchange_orders_json") or "{}").get("k")
    print(f"  [{tag}] 纸盘={net:.6g}（反推交易员 {net / kq if kq else 0:.6g}） "
          f"managed={p['managed']} | 记录={float((lr or {}).get('quantity') or 0):.6g} "
          f"k={k} | 交易所={FAKE.pos_qty(tok):.6g}")


aft.set_follow(TR, "状态探针", "", True)

for state in (0, 1, 2, 3):
    tok = TOKENS[state]
    print(f"\n########## managed={state}（{STATE_CN[state]}）##########")
    PRICE[tok] = 100.0
    aft._prev_snap.clear()
    poll([])
    poll([tp(tok, 10)])
    stock("开仓后", tok)
    with storage.get_conn() as c:
        c.execute("UPDATE trade_positions SET managed=? WHERE id=?",
                  (state, paper(tok)["id"]))
    poll([tp(tok, 10)])
    stock(f"设为{STATE_CN[state]}后", tok)
    poll([tp(tok, 20)])                      # 交易员加到 20
    stock("交易员→20 后", tok)
    for _p in FAKE.positions:                 # 我手动加到 0.30
        if _p["symbol"] == f"{tok}USDT":
            _p["quantity"] = 0.30
    poll([tp(tok, 20)])
    stock("我手动→0.30 后", tok)
    n = len(FAKE.orders)
    r = aft.quick_follow(TR, snap([tp(tok, 20)]), PARAMS)
    res = (r.get("results") or [{}])[0]
    print(f"   快速跟随返回: action={res.get('action')} live={res.get('live')}")
    stock("快速跟随当轮", tok)
    print("   本轮交易所下单:", FAKE.orders[n:])
    poll([tp(tok, 20)])
    stock("再走一轮", tok)
