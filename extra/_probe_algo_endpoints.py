"""探测币安 USDⓈ-M 上"查询条件单"的正确端点（只读 GET，不下单）。

区分两类响应：
  HTTPError + HTML 错误页 → 端点不存在
  HTTPError + JSON（如 -1102 缺参数） → 端点存在但参数不对
"""
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.path.insert(0, "/root/binance-monitor/bsm-agent")
import exchange  # noqa: E402

CANDS = [
    "/fapi/v1/algoOpenOrders",        # 代码里现在用的（怀疑不存在）
    "/fapi/v1/algoOrders",
    "/fapi/v1/openAlgoOrders",
    "/fapi/v1/allAlgoOrders",
    "/fapi/v1/algo/orders",
    "/fapi/v1/algo/order/list",
    "/sapi/v1/algo/futures/openOrders",
    "/fapi/v1/conditionalOrders",
    "/fapi/v1/openOrders",            # 对照：确认普通挂单端点可用
]

for path in CANDS:
    url = exchange.FAPI_REST + path + "?" + exchange._sign({})
    req = Request(url, method="GET")
    req.add_header("X-MBX-APIKEY", exchange.config.BINANCE_API_KEY)
    try:
        with urlopen(req, timeout=10) as r:
            body = r.read().decode("utf-8", "replace")
            print(f"{path:38} 200  {body[:120]}")
    except HTTPError as e:
        b = e.read().decode("utf-8", "replace").strip()
        kind = "HTML=不存在" if b.startswith("<") else "JSON=存在"
        print(f"{path:38} HTTP {e.code} {kind}  {b[:110]}")
    except Exception as e:  # noqa: BLE001
        print(f"{path:38} ERR {type(e).__name__} {e}")
