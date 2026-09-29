"""复现：把服务器上 ETH 的真实状态搬到本地，跑**修好后的新代码**，看会不会接管。

关键（上一版翻车的地方）：
  · **保留真的 exchange 模块**，只桩 `_signed_request`（HTTP 层）—— 取整/拒单都是真逻辑；
    桩里按币安规则拒单（triggerPrice 必须落在 tickSize=0.01 上，quantity 落在 stepSize=0.001 上）
  · 纸盘行用**显式 id 1892/1893**，与 live 记录的 paper_position_id 对得上

源数据 = 2026-09-14 03:20(CST) 服务器实况：
  纸盘 #1892 OPEN LONG 0.04010508464861786 entry2493.4494186 sl2470.8222 lev10 notional100
  纸盘 #1893 PENDING LONG 0.040067634166473 limit2495.78 sl2470.8222
  live #17 OPEN(paper1892) 0.04010508464861786 orders={"open":8389766276068473378,"sl":null,...}
  live #18 OPEN(paper1893) 0.04 orders={"limit":null,"sl":null,"limit_price":2467.96,...}
  交易所 ETH LONG 0.04（限价单已被手动撤）
"""
import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
_T = tempfile.mkdtemp(prefix="st_eth_")
config.DB_PATH = os.path.join(_T, "main.db")
config.SUPERTREND_DB = os.path.join(_T, "st.db")
config.LIVE_DB_PATH = os.path.join(_T, "live.db")

import storage  # noqa: E402
import trade_logic  # noqa: E402
import supertrend_strategy as st  # noqa: E402
import supertrend_live as stl  # noqa: E402
import exchange  # noqa: E402

with storage.get_conn() as _c:
    _c.execute("PRAGMA journal_mode=WAL")
    _c.executescript(storage.SCHEMA)
    _c.execute("DROP TABLE IF EXISTS trade_signal_locks")
    _c.execute("""CREATE TABLE trade_signal_locks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL,
        signal_key TEXT NOT NULL, strategy TEXT DEFAULT 'agent',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(token, signal_key, strategy))""")
st.init_db()
storage.init_onchain_live_db()

PRICE = {"ETH": 2495.0}
st._fetch_okx_supertrend = lambda t: None      # 只跑镜像层，不跑策略轮
st.get_mark_price = lambda t: PRICE.get(t)
trade_logic.get_mark_price = lambda t: PRICE.get(t)
st.time.sleep = lambda *a, **k: None
exchange._fetch_symbol_filters = lambda s: {"stepSize": 0.001, "minQty": 0.0,
                                            "priceStep": 0.01}

# ---- 只桩 HTTP 层：按币安规则拒单（-1111），其余原样 ----
POS = [{"symbol": "ETHUSDT", "quantity": 0.04, "entry_price": 2492.96,
        "mark_price": 2495.0, "unrealized_pnl": 0.0, "leverage": 10,
        "position_side": "LONG"}]
ORDERS, ALGOS, SENT = [], [], []
_N = [9000]


def _sr(method, path, params=None):
    params = params or {}
    SENT.append((method, path, dict(params)))
    if path.endswith("/fapi/v1/algoOrder"):
        tp = float(params.get("triggerPrice") or 0)
        q = float(params.get("quantity") or 0)
        if round(tp, 2) != round(tp, 8) or round(q, 3) != round(q, 8):
            exchange._last_error_msg = ("-1111 Precision is over the maximum "
                                        "defined for this asset.")
            print(f"    [HTTP] 拒单 -1111  triggerPrice={tp} quantity={q}")
            return None
        _N[0] += 1
        ALGOS.append({"order_id": _N[0], "status": "NEW", "triggerPrice": tp,
                      "quantity": q, "symbol": params.get("symbol")})
        return {"algoId": _N[0], "algoStatus": "NEW"}
    if path.endswith("/fapi/v1/order"):
        _N[0] += 1
        q = float(params.get("quantity") or 0)
        if params.get("type") == "MARKET" and POS:      # 市价单要真的改持仓
            POS[0]["quantity"] += q if params.get("side") == "BUY" else -q
            POS[0]["mark_price"] = PRICE["ETH"]
        return {"orderId": _N[0], "status": "FILLED", "executedQty": q,
                "origQty": q, "fills": [{"price": str(PRICE["ETH"])}]}
    if path.endswith("/fapi/v1/leverage"):
        return {"leverage": params.get("leverage")}
    return {}


exchange._signed_request = _sr
exchange.get_positions = lambda: [dict(p) for p in POS]
exchange.get_positions_or_none = lambda: exchange.get_positions()
exchange.get_open_orders = lambda token=None: [
    dict(o, orderId=o["order_id"], executedQty=float(o.get("executed_qty") or 0))
    for o in ORDERS if o["status"] == "NEW"]
exchange.get_open_orders_or_none = lambda token=None: exchange.get_open_orders(token)
exchange.get_algo_orders = lambda token=None: [
    o for o in ALGOS if o.get("status") == "NEW"]
exchange.cancel_order = lambda token, oid: True

# ---- 摆出服务器状态 ----
with storage.get_conn() as c:
    c.execute("INSERT INTO trade_positions (id,token,symbol,side,status,mode,"
              "margin_amount,leverage,notional,quantity,entry_price,limit_price,"
              "current_price,stop_loss_price,strategy,open_reason) "
              "VALUES (1892,'ETH','ETHUSDT','LONG','OPEN','paper',10.0,10.0,100.0,"
              "0.04010508464861786,2493.4494186,2493.4494186,2495.0,2470.8222,"
              "'supertrend','趋势反转到允许方向，市价开多')")
    c.execute("INSERT INTO trade_positions (id,token,symbol,side,status,mode,"
              "margin_amount,leverage,notional,quantity,entry_price,limit_price,"
              "current_price,stop_loss_price,strategy,open_reason) "
              "VALUES (1893,'ETH','ETHUSDT','LONG','PENDING','paper',10.0,10.0,100.0,"
              "0.040067634166473,NULL,2495.78,2495.78,2470.8222,"
              "'supertrend','超级趋势 15m UP 挂多单')")
with storage.get_conn(config.SUPERTREND_DB) as c:
    c.execute("INSERT OR REPLACE INTO supertrend_tokens (token,status,added_at,"
              "live_mode,direction,flip_market_open,notional_usd,leverage,"
              "sl_tolerance_pct,order_tolerance_pct) "
              "VALUES ('ETH','ACTIVE',?/1000,1,'LONG_ONLY',1,100.0,10.0,1.0,0.0)",
              (__import__("time").time(),))
with storage.get_conn(config.LIVE_DB_PATH) as lc:
    storage.live_position_insert(lc, {
        "paper_position_id": 1892, "token": "ETH", "symbol": "ETHUSDT",
        "side": "LONG", "strategy": "supertrend", "entry_price": 2493.4494186,
        "quantity": 0.04010508464861786, "leverage": 10, "margin_usd": 10.0,
        "exchange_orders_json": json.dumps({"open": 8389766276068473378, "sl": None,
                                            "sl_price": None, "limit": None,
                                            "limit_price": None, "limit_qty": None}),
        "tp1_done": 0, "tp2_done": 0, "status": "OPEN"})
    storage.live_position_insert(lc, {
        "paper_position_id": 1893, "token": "ETH", "symbol": "ETHUSDT",
        "side": "LONG", "strategy": "supertrend", "entry_price": 2492.96,
        "quantity": 0.04, "leverage": 10, "margin_usd": 10.0,
        "exchange_orders_json": json.dumps(
            {"open": None, "sl": None, "sl_price": None, "limit": None,
             "limit_price": 2467.96, "limit_qty": 0.04051929528841634}),
        "tp1_done": 0, "tp2_done": 0, "status": "OPEN"})

# 越过启动冷窗
stl._rounds_done = 99


def dump(tag):
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        rows = [dict(r) for r in lc.execute(
            "SELECT id,status,quantity,exchange_orders_json FROM live_positions "
            "WHERE token='ETH' ORDER BY id")]
    print(f"  [{tag}] 交易所持仓={POS[0]['quantity']:.8g}  "
          f"活的SL={[(a['order_id'], a['quantity'], a['triggerPrice']) for a in ALGOS if a['status'] == 'NEW']}")
    for r in rows:
        print(f"      live#{r['id']} {r['status']} qty={r['quantity']:.8g} "
              f"{r['exchange_orders_json']}")


print("===== 第一步：部署后第一轮对账 =====")
stl.sync_supertrend_live()
dump("第一轮后")

print("\n===== 第二步：价格跌到 2494（纸盘挂单 2495.78 被 1s 循环判成交）=====")
PRICE["ETH"] = 2494.0
with storage.get_conn() as mc:
    trade_logic.update_paper_positions(mc)
with storage.get_conn() as c:
    print("  纸盘行:", [(r["id"], r["status"], round(float(r["quantity"]), 8))
                        for r in c.execute("SELECT id,status,quantity FROM "
                                           "trade_positions WHERE token='ETH' ORDER BY id")])

print("  （补上真实策略轮开头那一步：_merge_positions）")
with storage.get_conn() as mc:
    st._merge_positions(mc, "ETH")
with storage.get_conn() as c:
    print("  合并后纸盘行:", [(r["id"], r["status"], round(float(r["quantity"]), 8))
                              for r in c.execute("SELECT id,status,quantity FROM "
                                                 "trade_positions WHERE token='ETH' "
                                                 "ORDER BY id")])

print("\n===== 第三步：再跑一轮对账 =====")
stl.sync_supertrend_live()
dump("第二步腿成交后")

print("\n===== 第四步：再跑一轮（看是否收敛稳定）=====")
stl.sync_supertrend_live()
dump("第三轮后")
