"""
实盘全流程测试 — 完全独立，不依赖系统代码
开仓 → 挂 TP1/TP2/SL → 模拟 TP1 触发 → SL 移保本 → 全平
用法:
  python3 test_live_trade.py open      # 开仓
  python3 test_live_trade.py orders    # 挂 TP1+TP2+SL
  python3 test_live_trade.py tp1_done  # 模拟TP1触发: 取消旧SL, 挂保本SL
  python3 test_live_trade.py trailing  # 模拟TP2触发: 取消SL, 挂追迹
  python3 test_live_trade.py close     # 市价全平
  python3 test_live_trade.py status    # 查看
"""
import hashlib, hmac, json, time, sys
from urllib.request import Request, urlopen
from urllib.parse import urlencode

FAPI = "https://fapi.binance.com"
KEY = ""
SECRET = ""
TOKEN = "SOL"
SYMBOL = "SOLUSDT"

TP1_CLOSE_PCT = 0.8  # TP1 平 80%
TP2_CLOSE_PCT = 0.1  # TP2 平 10%
TRAIL_CALLBACK = 2.0  # 追迹回撤 2%


def signed(method, path, params=None):
    if params is None:
        params = {}
    params["timestamp"] = int(time.time() * 1000)
    query = urlencode(params)
    sig = hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{FAPI}{path}?{query}&signature={sig}"
    req = Request(url, method=method)
    req.add_header("X-MBX-APIKEY", KEY)
    for attempt in range(3):
        try:
            with urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            body = e.read().decode() if hasattr(e, "read") else str(e)
            print(f"  [{method} {path}] {body[:200]}")
            if "429" in body: time.sleep(10)
            elif "5" in body[:20]: time.sleep(3)
            else: return None
    return None


def get_balance():
    r = signed("GET", "/fapi/v2/balance")
    return float(next((b["availableBalance"] for b in r if b["asset"] == "USDT"), 0)) if r else 0


def get_mark_price():
    r = signed("GET", "/fapi/v1/premiumIndex", {"symbol": SYMBOL})
    return float(r["markPrice"]) if r else 0


def get_positions():
    r = signed("GET", "/fapi/v2/positionRisk")
    return [p for p in r if float(p["positionAmt"]) != 0] if r else []


def get_open_orders():
    """获取所有挂单（普通 + algo）"""
    orders = signed("GET", "/fapi/v1/openOrders", {"symbol": SYMBOL})
    algo = signed("GET", "/fapi/v1/openAlgoOrders", {"symbol": SYMBOL})
    result = []
    if isinstance(orders, list): result.extend(orders)
    if isinstance(algo, list): result.extend(algo)
    return result


def cancel_order(oid):
    """撤单（兼容普通 orderId 和 algo algoId）"""
    r1 = signed("DELETE", "/fapi/v1/order", {"symbol": SYMBOL, "orderId": oid})
    r2 = signed("DELETE", "/fapi/v1/algoOrder", {"symbol": SYMBOL, "algoId": oid})
    return r1 or r2


def cancel_all():
    ok1 = signed("DELETE", "/fapi/v1/allOpenOrders", {"symbol": SYMBOL})
    ok2 = signed("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": SYMBOL})
    return ok1 or ok2


def set_leverage():
    return bool(signed("POST", "/fapi/v1/leverage", {"symbol": SYMBOL, "leverage": 1}))


def market_order(side, qty, reduce_only=False):
    params = {"symbol": SYMBOL, "side": side, "type": "MARKET",
              "quantity": qty, "positionSide": "LONG"}
    # Hedge 模式不需要 reduceOnly（positionSide 已限制方向）
    return signed("POST", "/fapi/v1/order", params)


def stop_loss_order(qty, stop_price):
    return signed("POST", "/fapi/v1/algoOrder", {
        "symbol": SYMBOL, "side": "SELL", "type": "STOP_MARKET",
        "algoType": "CONDITIONAL",
        "quantity": qty, "triggerPrice": round(stop_price, 6),
        "positionSide": "LONG", "workingType": "MARK_PRICE",
    })


def take_profit_order(qty, stop_price):
    return signed("POST", "/fapi/v1/algoOrder", {
        "symbol": SYMBOL, "side": "SELL", "type": "TAKE_PROFIT_MARKET",
        "algoType": "CONDITIONAL",
        "quantity": qty, "triggerPrice": round(stop_price, 6),
        "positionSide": "LONG", "workingType": "MARK_PRICE",
    })


def trailing_stop_order(qty, callback):
    return signed("POST", "/fapi/v1/algoOrder", {
        "symbol": SYMBOL, "side": "SELL", "type": "TRAILING_STOP_MARKET",
        "algoType": "CONDITIONAL",
        "quantity": qty, "callbackRate": round(callback, 1),
        "positionSide": "LONG", "workingType": "MARK_PRICE",
    })


def cancel_algo(oid):
    return signed("DELETE", "/fapi/v1/algoOrder", {"symbol": SYMBOL, "algoId": oid})


# ============ 步骤函数 ============

def step_status():
    bal = get_balance()
    pos_list = get_positions()
    orders = get_open_orders()
    print(f"余额: ${bal:.2f}  持仓: {len(pos_list)} 个  挂单: {len(orders)} 个")
    for p in pos_list:
        qty = float(p["positionAmt"])
        entry = float(p["entryPrice"] or 0)
        pnl = float(p["unRealizedProfit"] or 0)
        print(f"  {p['symbol']} qty={qty} entry={entry} pnl={pnl}")
    for o in orders:
        print(f"  order #{o['orderId']} {o['type']} {o['side']} qty={o['origQty']} stop={o.get('stopPrice','-')}")


def step_open():
    bal = get_balance()
    price = get_mark_price()
    print(f"余额: ${bal:.2f}  标记价: ${price:.4f}")
    qty = round(6.0 / price, 2) or 0.01
    print(f"计划开多: {qty} SOL")
    if not set_leverage():
        print("设杠杆失败"); return
    r = market_order("BUY", qty)
    if not r:
        print("开仓失败"); return
    exec_qty = float(r.get("executedQty", qty) or qty)
    fill = float(r.get("avgPrice", price) or price)
    print(f"✅ 开仓: {exec_qty} SOL @ ${fill:.4f}")


def step_orders():
    """挂 TP1 + TP2 + SL"""
    pos_list = get_positions()
    ex = next((p for p in pos_list if p["symbol"] == SYMBOL), None)
    if not ex:
        print("无持仓"); return
    total_qty = abs(float(ex["positionAmt"]))
    entry = float(ex["entryPrice"] or 0)
    sl = round(entry * 0.98, 6)  # 2% 止损
    risk = entry - sl
    tp1 = round(entry + risk * 1.0, 6)  # +1R
    tp2 = round(entry + risk * 2.0, 6)  # +2R

    tp1_qty = round(total_qty * TP1_CLOSE_PCT, 2) or 0.01
    tp2_qty = round(total_qty * TP2_CLOSE_PCT, 2) or 0.01

    print(f"持仓: {total_qty} SOL  entry=${entry}")
    print(f"TP1: {tp1_qty} @ ${tp1}  TP2: {tp2_qty} @ ${tp2}  SL: {total_qty} @ ${sl}")
    print()

    r1 = take_profit_order(tp1_qty, tp1)
    print(f"TP1: {'✅' if r1 else '❌'} {r1}")
    r2 = take_profit_order(tp2_qty, tp2)
    print(f"TP2: {'✅' if r2 else '❌'} {r2}")
    r3 = stop_loss_order(total_qty, sl)
    print(f"SL:  {'✅' if r3 else '❌'} {r3}")


def step_tp1_done():
    """模拟 TP1 触发后: 取消旧 SL, 挂保本 SL"""
    pos_list = get_positions()
    ex = next((p for p in pos_list if p["symbol"] == SYMBOL), None)
    if not ex:
        print("无持仓"); return
    total_qty = abs(float(ex["positionAmt"]))
    entry = float(ex["entryPrice"] or 0)
    remaining = round(total_qty * (1 - TP1_CLOSE_PCT), 2) or 0.01

    # 取消所有旧挂单
    orders = get_open_orders()
    print(f"取消 {len(orders)} 个旧挂单...")
    for o in orders:
        oid = o.get("orderId") or o.get("algoId")
        cancel_order(oid)

    # 挂新 SL@保本价
    r = stop_loss_order(remaining, entry)
    print(f"新 SL: {remaining} SOL @ ${entry} {'✅' if r else '❌'}")


def step_trailing():
    """模拟 TP2 触发后: 取消 SL, 挂追迹止盈"""
    pos_list = get_positions()
    ex = next((p for p in pos_list if p["symbol"] == SYMBOL), None)
    if not ex:
        print("无持仓"); return
    total_qty = abs(float(ex["positionAmt"]))
    remaining = round(total_qty * (1 - TP1_CLOSE_PCT - TP2_CLOSE_PCT), 2) or 0.01

    # 取消 SL
    orders = get_open_orders()
    for o in orders:
        ot = o.get("type") or o.get("orderType", "")
        oid = o.get("orderId") or o.get("algoId")
        if ot in ("STOP_MARKET", "STOP"):
            cancel_order(oid)

    # 挂追迹止盈
    r = trailing_stop_order(remaining, TRAIL_CALLBACK)
    print(f"追迹: {remaining} SOL callback={TRAIL_CALLBACK}% {'✅' if r else '❌'}")


def step_close():
    pos_list = get_positions()
    ex = next((p for p in pos_list if p["symbol"] == SYMBOL), None)
    if not ex:
        print("无持仓"); return
    qty = abs(float(ex["positionAmt"]))
    # 先取消所有挂单
    cancel_all()
    # 市价平仓
    order = market_order("SELL", qty, reduce_only=True)
    if order:
        exec_qty = float(order.get("executedQty", qty) or qty)
        print(f"✅ 平仓: {exec_qty} SOL  余额: ${get_balance():.2f}")
    else:
        print("平仓失败!")


if __name__ == "__main__":
    cmds = {"status": step_status, "open": step_open, "orders": step_orders,
            "tp1_done": step_tp1_done, "trailing": step_trailing, "close": step_close}
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd in cmds:
        cmds[cmd]()
    else:
        print("用法: open | orders | tp1_done | trailing | close | status")
