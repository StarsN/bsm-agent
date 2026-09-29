"""清理测试 Algo 订单"""
import hashlib, hmac, json, time
from urllib.request import Request, urlopen
from urllib.parse import urlencode

KEY = ""
SECRET = ""
FAPI = "https://fapi.binance.com"

def signed(method, path, params=None):
    if params is None: params = {}
    params["timestamp"] = int(time.time() * 1000)
    query = urlencode(params)
    sig = hmac.new(SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    url = f"{FAPI}{path}?{query}&signature={sig}"
    req = Request(url, method=method)
    req.add_header("X-MBX-APIKEY", KEY)
    try:
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode()), None
    except Exception as e:
        return None, (e.read().decode() if hasattr(e, "read") else str(e))[:400]

r, _ = signed("GET", "/fapi/v1/openAlgoOrders")
print(f"Open algo orders: {len(r) if isinstance(r, list) else r}")
if isinstance(r, list):
    for o in r:
        print(f"  algoId={o['algoId']} {o['orderType']} status={o['algoStatus']}")
    for o in r:
        cr, _ = signed("DELETE", "/fapi/v1/algoOrder", {"symbol": o["symbol"], "algoId": o["algoId"]})
        print(f"  Cancel {o['algoId']}: {cr}")
r, _ = signed("GET", "/fapi/v1/openAlgoOrders")
print(f"After cleanup: {len(r) if isinstance(r, list) else r} open")
