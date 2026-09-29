"""验证挂单查询链路（实盘页"当前挂单"一直是空的根因）。

根因：get_algo_orders 查的 /fapi/v1/algoOpenOrders 在币安不存在（404 + HTML
错误页）→ _signed_request 在 except 里 json.loads 非 JSON 正文 → JSONDecodeError
冲出函数 → 上层把普通挂单的结果也一起清空。
"""
import io
import os
import sys
from urllib.error import HTTPError

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)

import exchange  # noqa: E402

fails = 0


def check(name, cond, extra=""):
    global fails
    ok = bool(cond)
    print(("[PASS] " if ok else "[FAIL] ") + name + ("" if ok else f"  {extra}"))
    if not ok:
        fails += 1


# ---- 1. 端点正确性 ----
calls = []
_real = exchange._signed_request


def _spy(method, path, params=None):
    calls.append((method, path))
    return []


exchange._signed_request = _spy
try:
    exchange.get_open_orders()
    exchange.get_algo_orders()
    exchange.get_algo_orders("BTC")
finally:
    exchange._signed_request = _real

paths = [p for _, p in calls]
check("普通挂单用 /fapi/v1/openOrders", "/fapi/v1/openOrders" in paths, str(paths))
check("条件单用 /fapi/v1/openAlgoOrders（正确端点）",
      paths.count("/fapi/v1/openAlgoOrders") == 2, str(paths))
check("不再使用不存在的 /fapi/v1/algoOpenOrders",
      "/fapi/v1/algoOpenOrders" not in paths, str(paths))

# ---- 2. 非 JSON 错误页不得抛异常（根因）----
def _html_404(req, timeout=10):
    raise HTTPError(req.full_url, 404, "Not Found", {},
                    io.BytesIO(b"<!DOCTYPE html><html><head>error</head></html>"))


_real_urlopen = exchange.urlopen
_real_sleep = exchange.time.sleep
exchange.urlopen = _html_404
exchange.time.sleep = lambda *_: None
try:
    r = exchange._signed_request("GET", "/fapi/v1/algoOpenOrders")
    raised = None
except Exception as e:  # noqa: BLE001
    r, raised = None, e
finally:
    exchange.urlopen = _real_urlopen
    exchange.time.sleep = _real_sleep
check("非 JSON 错误响应不抛异常（返回 None）",
      r is None and raised is None, repr(raised))
check("错误信息被记录（get_last_error 非空）", bool(exchange.get_last_error()),
      exchange.get_last_error())

# ---- 3. 条件单返回非 list 时兜底为空 ----
exchange._signed_request = lambda m, p, params=None: {"code": -1, "msg": "x"}
try:
    check("条件单返回非 list → 返回空列表（不抛）",
          exchange.get_algo_orders() == [], "")
finally:
    exchange._signed_request = _real

# ---- 3. cancel_order：单子"已经不在"必须算成功 ----
# 语义：调用方撤单要的是"这张单消失"，它已经成交（SL 触发）/已被撤 = 目标达成。
# 判成失败会让调用方以为"还有单没撤掉" → 不收敛记录 → 永久卡在 OPEN
# （生产事故：SL 触发后撤单必被拒 → 记录永不收敛 → 该币永久无法被跟单使用）。
_real_sr = exchange._signed_request


def _sr_factory(delete_ok=False, open_normal=None, open_algo=None):
    def _fake(method, path, params=None):
        if method == "DELETE":
            return {"ok": 1} if delete_ok else None
        if path.endswith("/openOrders"):
            return open_normal
        if path.endswith("/openAlgoOrders"):
            return open_algo
        return None
    return _fake


try:
    exchange._signed_request = _sr_factory(delete_ok=True)
    check("撤单成功 → True", exchange.cancel_order("BTC", 111) is True)

    exchange._signed_request = _sr_factory(
        delete_ok=False, open_normal=[{"orderId": 222}], open_algo=[{"algoId": 333}])
    check("撤单被拒但单子已不在挂单列表 → True（已成交/已撤 = 目标达成）",
          exchange.cancel_order("BTC", 111) is True)

    exchange._signed_request = _sr_factory(
        delete_ok=False, open_normal=[{"orderId": 111}], open_algo=[])
    check("撤单被拒且单子仍在普通挂单里 → False",
          exchange.cancel_order("BTC", 111) is False)
    exchange._signed_request = _sr_factory(
        delete_ok=False, open_normal=[], open_algo=[{"algoId": 111}])
    check("撤单被拒且单子仍在条件单里 → False",
          exchange.cancel_order("BTC", 111) is False)

    exchange._signed_request = _sr_factory(delete_ok=False, open_normal=None,
                                           open_algo=None)
    check("核对查询也失败 → False（保守，绝不把'查不到'当成'已消失'）",
          exchange.cancel_order("BTC", 111) is False)
finally:
    exchange._signed_request = _real_sr

# ---- 4. close_position_market：平仓量只许向下、不足一个 step 不发单 ----
# 旧写法 _round_qty 会 max(result, minQty) 把残仓放大到 minQty → 超量平仓被拒 →
# 调用方"记录不收敛"永久重试（而止损已撤 = 裸仓）
_saved_filters = exchange._fetch_symbol_filters
exchange._fetch_symbol_filters = lambda symbol: {
    "stepSize": 0.001, "minQty": 0.001, "priceStep": 0.01}
_sent = []


def _sr_capture(method, path, params=None):
    _sent.append((method, path, dict(params or {})))
    return {"orderId": 1, "executedQty": (params or {}).get("quantity"),
            "fills": [], "status": "FILLED"}


try:
    exchange._signed_request = _sr_capture
    exchange.close_position_market("BTC", 0.0004)      # < 半个 step
    check("残仓不足一个 step → 不发单（不许放大到 minQty）", not _sent, str(_sent))
    exchange.close_position_market("BTC", 0.0019)      # 1.9 step
    check("平仓量向下取整（1.9 step → 1 step）",
          bool(_sent) and abs(float(_sent[-1][2]["quantity"]) - 0.001) < 1e-12,
          str(_sent[-1] if _sent else None))
    exchange.close_position_market("BTC", 0.0025)      # 2.5 step：四舍五入会变 3
    check("不四舍五入放大（2.5 step → 2 step）",
          abs(float(_sent[-1][2]["quantity"]) - 0.002) < 1e-12, str(_sent[-1]))
    check("_round_qty_down 单元：0.0004→0 / 0.0019→0.001 / 0.0025→0.002",
          exchange._round_qty_down("BTCUSDT", 0.0004) == 0.0
          and abs(exchange._round_qty_down("BTCUSDT", 0.0019) - 0.001) < 1e-12
          and abs(exchange._round_qty_down("BTCUSDT", 0.0025) - 0.002) < 1e-12,
          "")
finally:
    exchange._signed_request = _real_sr
    exchange._fetch_symbol_filters = _saved_filters

# ---- 5. 条件单触发价必须按 tickSize 对齐 ----
# 旧写法 round(stop_price, 6)：ETHUSDT 的 tick 是 0.01，纸盘算出的 SL 2467.7928（4 位小数）
# → 币安 -1111 Precision is over the maximum defined for this asset → **实盘止损从来没挂上**
# （2026-09-14 生产实测：267 轮全是 -1111，ETH 实盘仓裸奔）
_saved_f2 = exchange._fetch_symbol_filters
exchange._fetch_symbol_filters = lambda symbol: {
    "stepSize": 0.001, "minQty": 0.001, "priceStep": 0.01}
_sent2 = []


def _sr_cap2(method, path, params=None):
    _sent2.append(dict(params or {}))
    return {"algoId": 1, "algoStatus": "NEW"}


try:
    exchange._signed_request = _sr_cap2
    exchange.stop_loss_order("ETH", "SELL", 0.04, 2467.7928, position_side="LONG")
    check("止损触发价按 tickSize 对齐（2467.7928 → 2467.79）",
          bool(_sent2) and abs(float(_sent2[-1]["triggerPrice"]) - 2467.79) < 1e-9,
          str(_sent2[-1] if _sent2 else None))
    exchange.take_profit_order("ETH", "SELL", 0.04, 2467.7928, position_side="LONG")
    check("止盈触发价也按 tickSize 对齐",
          abs(float(_sent2[-1]["triggerPrice"]) - 2467.79) < 1e-9, str(_sent2[-1]))
finally:
    exchange._signed_request = _real_sr
    exchange._fetch_symbol_filters = _saved_f2

print(f"\n结果: {'全部通过' if not fails else str(fails) + ' 项失败'}")
sys.exit(1 if fails else 0)
