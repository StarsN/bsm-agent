"""
币安合约 API 封装（实盘交易）。
需要 API Key + Secret，配置在 config.py 中。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import config


FAPI_REST = "https://fapi.binance.com"


def _sign(params: dict) -> str:
    params["timestamp"] = int(time.time() * 1000)
    query = urlencode(params)
    signature = hmac.new(
        config.BINANCE_API_SECRET.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{query}&signature={signature}"


def _signed_request(method: str, path: str, params: dict = None) -> Optional[dict]:
    """签名请求"""
    if params is None:
        params = {}
    url = f"{FAPI_REST}{path}?{_sign(params)}"
    req = Request(url, method=method)
    req.add_header("X-MBX-APIKEY", config.BINANCE_API_KEY)
    req.add_header("User-Agent", "Mozilla/5.0")
    for attempt in range(3):
        try:
            with urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            err = json.loads(e.read().decode("utf-8"))
            code = err.get("code", 0)
            msg = err.get("msg", "")
            print(f"[exchange] {path} HTTP {e.code}: {code} {msg}")
            if e.code == 429:
                time.sleep(10)
            elif e.code >= 500:
                time.sleep(3)
            else:
                return None
        except Exception as e:
            print(f"[exchange] {path} 网络异常: {e}")
            if attempt < 2:
                time.sleep(2)
    return None


def set_leverage(symbol: str, leverage: int) -> bool:
    """设置杠杆倍数"""
    r = _signed_request("POST", "/fapi/v1/leverage", {
        "symbol": symbol, "leverage": leverage,
    })
    return bool(r and r.get("leverage") == leverage)


def get_balance(asset: str = "USDT") -> float:
    """查询可用余额"""
    r = _signed_request("GET", "/fapi/v2/balance")
    if not r:
        return 0.0
    for item in r:
        if item.get("asset") == asset:
            return float(item.get("availableBalance", 0))
    return 0.0


def get_positions() -> list[dict]:
    """查询当前持仓"""
    r = _signed_request("GET", "/fapi/v2/positionRisk")
    if not r:
        return []
    return [
        {
            "symbol": p["symbol"],
            "quantity": float(p["positionAmt"]),
            "entry_price": float(p["entryPrice"]),
            "mark_price": float(p["markPrice"]),
            "unrealized_pnl": float(p["unRealizedProfit"]),
            "leverage": int(p["leverage"]),
        }
        for p in r
        if float(p["positionAmt"]) != 0
    ]


def market_open_long(token: str, quantity: float) -> dict | None:
    """市价开多。返回: {order_id, price, quantity, status}"""
    symbol = f"{token.upper()}USDT"
    r = _signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quantity": quantity,
    })
    if not r:
        return None
    return {
        "order_id": r.get("orderId"),
        "symbol": symbol,
        "price": float(r.get("fills")[0]["price"]) if r.get("fills") else 0,
        "quantity": float(r.get("executedQty", 0)),
        "status": r.get("status"),
    }


def stop_loss_order(token: str, side: str, quantity: float,
                    stop_price: float) -> dict | None:
    """挂止损单（STOP_MARKET）"""
    symbol = f"{token.upper()}USDT"
    r = _signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "STOP_MARKET",
        "quantity": quantity,
        "stopPrice": round(stop_price, 6),
        "reduceOnly": "true",
    })
    if not r:
        return None
    return {"order_id": r.get("orderId"), "status": r.get("status")}


def take_profit_order(token: str, side: str, quantity: float,
                      stop_price: float) -> dict | None:
    """挂止盈单（TAKE_PROFIT_MARKET）"""
    symbol = f"{token.upper()}USDT"
    r = _signed_request("POST", "/fapi/v1/order", {
        "symbol": symbol,
        "side": side,
        "type": "TAKE_PROFIT_MARKET",
        "quantity": quantity,
        "stopPrice": round(stop_price, 6),
        "reduceOnly": "true",
    })
    if not r:
        return None
    return {"order_id": r.get("orderId"), "status": r.get("status")}


def cancel_order(token: str, order_id: int) -> bool:
    """撤单"""
    symbol = f"{token.upper()}USDT"
    r = _signed_request("DELETE", "/fapi/v1/order", {
        "symbol": symbol, "orderId": order_id,
    })
    return bool(r)


def cancel_all_orders(token: str) -> bool:
    """撤销某币所有挂单"""
    symbol = f"{token.upper()}USDT"
    r = _signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol})
    return bool(r)


def get_open_orders(token: str = None) -> list[dict]:
    """查询挂单"""
    params = {}
    if token:
        params["symbol"] = f"{token.upper()}USDT"
    r = _signed_request("GET", "/fapi/v1/openOrders", params)
    return r if isinstance(r, list) else []
