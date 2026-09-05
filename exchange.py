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

_symbol_filters_cache = {}
_last_error_msg = ""  # 供 bridge 读取最后一次错误


def get_last_error() -> str:
    return _last_error_msg


def _fetch_symbol_filters(symbol: str) -> dict:
    """查交易对的 stepSize / minQty。缓存 1 小时。"""
    now = time.time()
    entry = _symbol_filters_cache.get(symbol)
    if entry and now - entry["_ts"] < 3600:
        return entry

    r = _signed_request("GET", "/fapi/v1/exchangeInfo")
    if not r:
        return {"stepSize": 0.001, "minQty": 0}

    for s in r.get("symbols", []):
        sym = s.get("symbol", "")
        for f in s.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                info = {
                    "stepSize": float(f.get("stepSize", 0.001)),
                    "minQty": float(f.get("minQty", 0)),
                    "_ts": now,
                }
                _symbol_filters_cache[sym] = info

    return _symbol_filters_cache.get(symbol, {"stepSize": 0.001, "minQty": 0})


def _round_qty(symbol: str, qty: float) -> float:
    """将数量对齐到 symbol 的 stepSize"""
    filters = _fetch_symbol_filters(symbol)
    step = filters["stepSize"]
    ticks = round(qty / step)
    result = round(ticks * step, 8)
    return max(result, filters["minQty"])


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
            _last_error_msg = f"{code} {msg}"
            print(f"[exchange] {method} {path} HTTP {e.code}: {code} {msg}")
            if e.code == 429:
                time.sleep(10)
            elif e.code >= 500:
                time.sleep(3)
            else:
                return None
        except Exception as e:
            _last_error_msg = str(e)
            print(f"[exchange] {method} {path} 网络异常: {e}")
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
    qty = _round_qty(symbol, quantity)
    params = {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quantity": qty,
    }
    if config.BINANCE_HEDGE_MODE:
        params["positionSide"] = "LONG"
    r = _signed_request("POST", "/fapi/v1/order", params)
    if not r:
        return None
    return {
        "order_id": r.get("orderId"),
        "symbol": symbol,
        "price": float(r.get("fills")[0]["price"]) if r.get("fills") else 0,
        "quantity": float(r.get("executedQty", 0)),
        "status": r.get("status"),
    }


def market_open_short(token: str, quantity: float) -> dict | None:
    """市价开空。返回: {order_id, price, quantity, status}"""
    symbol = f"{token.upper()}USDT"
    qty = _round_qty(symbol, quantity)
    params = {
        "symbol": symbol,
        "side": "SELL",
        "type": "MARKET",
        "quantity": qty,
        "positionSide": "SHORT",
    }
    r = _signed_request("POST", "/fapi/v1/order", params)
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
                    stop_price: float, position_side: str = "LONG") -> dict | None:
    """挂止损单（STOP_MARKET, algoOrder 端点）"""
    symbol = f"{token.upper()}USDT"
    qty = _round_qty(symbol, quantity)
    params = {
        "symbol": symbol, "side": side,
        "type": "STOP_MARKET", "algoType": "CONDITIONAL",
        "quantity": qty, "triggerPrice": round(stop_price, 6),
        "workingType": "MARK_PRICE",
    }
    if config.BINANCE_HEDGE_MODE:
        params["positionSide"] = position_side
    r = _signed_request("POST", "/fapi/v1/algoOrder", params)
    if not r:
        return None
    return {"order_id": r.get("algoId"), "status": r.get("algoStatus")}


def take_profit_order(token: str, side: str, quantity: float,
                      stop_price: float, position_side: str = "LONG") -> dict | None:
    """挂止盈单（TAKE_PROFIT_MARKET, algoOrder 端点）"""
    symbol = f"{token.upper()}USDT"
    qty = _round_qty(symbol, quantity)
    params = {
        "symbol": symbol, "side": side,
        "type": "TAKE_PROFIT_MARKET", "algoType": "CONDITIONAL",
        "quantity": qty, "triggerPrice": round(stop_price, 6),
        "workingType": "MARK_PRICE",
    }
    if config.BINANCE_HEDGE_MODE:
        params["positionSide"] = position_side
    r = _signed_request("POST", "/fapi/v1/algoOrder", params)
    if not r:
        return None
    return {"order_id": r.get("algoId"), "status": r.get("algoStatus")}


def cancel_order(token: str, order_id: int) -> bool:
    """撤单（algo 优先，再试普通订单）"""
    symbol = f"{token.upper()}USDT"
    # 先试 algo 撤单（绝大多数取消的都是条件单）
    r = _signed_request("DELETE", "/fapi/v1/algoOrder", {
        "symbol": symbol, "algoId": order_id,
    })
    if r:
        return True
    # 回退普通撤单
    r = _signed_request("DELETE", "/fapi/v1/order", {
        "symbol": symbol, "orderId": order_id,
    })
    return bool(r)


def cancel_all_orders(token: str) -> bool:
    """撤销某币所有挂单（普通 + algo）"""
    symbol = f"{token.upper()}USDT"
    ok1 = bool(_signed_request("DELETE", "/fapi/v1/allOpenOrders", {"symbol": symbol}))
    ok2 = bool(_signed_request("DELETE", "/fapi/v1/algoOpenOrders", {"symbol": symbol}))
    return ok1 or ok2


def get_open_orders(token: str = None) -> list[dict]:
    """查询挂单"""
    params = {}
    if token:
        params["symbol"] = f"{token.upper()}USDT"
    r = _signed_request("GET", "/fapi/v1/openOrders", params)
    return r if isinstance(r, list) else []


def close_position_market(token: str, quantity: float, position_side: str = "LONG") -> dict | None:
    """市价平仓（Hedge 模式用 positionSide 区分），默认平多"""
    is_short = position_side == "SHORT"
    symbol = f"{token.upper()}USDT"
    qty = _round_qty(symbol, quantity)
    params = {
        "symbol": symbol,
        "side": "BUY" if is_short else "SELL",
        "type": "MARKET",
        "quantity": qty,
        "reduceOnly": "true",
    }
    if config.BINANCE_HEDGE_MODE:
        params["positionSide"] = position_side
        params.pop("reduceOnly", None)
    r = _signed_request("POST", "/fapi/v1/order", params)
    if not r:
        return None
    return {
        "order_id": r.get("orderId"),
        "price": float(r.get("fills")[0]["price"]) if r.get("fills") else 0,
        "quantity": float(r.get("executedQty", 0)),
        "status": r.get("status"),
    }


def trailing_stop_order(token: str, side: str, quantity: float,
                        callback_rate: float, position_side: str = "LONG") -> dict | None:
    """挂追踪止盈单（TRAILING_STOP_MARKET, algoOrder 端点）"""
    symbol = f"{token.upper()}USDT"
    qty = _round_qty(symbol, quantity)
    params = {
        "symbol": symbol, "side": side,
        "type": "TRAILING_STOP_MARKET", "algoType": "CONDITIONAL",
        "quantity": qty, "callbackRate": round(callback_rate, 1),
        "workingType": "MARK_PRICE",
    }
    if config.BINANCE_HEDGE_MODE:
        params["positionSide"] = position_side
    r = _signed_request("POST", "/fapi/v1/algoOrder", params)
    if not r:
        return None
    return {"order_id": r.get("algoId"), "status": r.get("algoStatus")}
