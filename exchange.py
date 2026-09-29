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
    """查交易对的 stepSize / minQty / tickSize。缓存 1 小时。"""
    now = time.time()
    entry = _symbol_filters_cache.get(symbol)
    if entry and now - entry["_ts"] < 3600:
        return entry

    r = _signed_request("GET", "/fapi/v1/exchangeInfo")
    if not r:
        return {"stepSize": 0.001, "minQty": 0, "priceStep": 0.01}

    for s in r.get("symbols", []):
        sym = s.get("symbol", "")
        for f in s.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                info = _symbol_filters_cache.setdefault(sym, {
                    "stepSize": 0.001, "minQty": 0, "priceStep": 0.01,
                })
                info["stepSize"] = float(f.get("stepSize", 0.001))
                info["minQty"] = float(f.get("minQty", 0))
                info["_ts"] = now
            elif f["filterType"] == "PRICE_FILTER":
                info = _symbol_filters_cache.setdefault(sym, {
                    "stepSize": 0.001, "minQty": 0, "priceStep": 0.01,
                })
                info["priceStep"] = float(f.get("tickSize", 0.01))
                info["_ts"] = now

    return _symbol_filters_cache.get(symbol, {"stepSize": 0.001, "minQty": 0, "priceStep": 0.01})


def _round_qty(symbol: str, qty: float) -> float:
    """将数量对齐到 symbol 的 stepSize"""
    filters = _fetch_symbol_filters(symbol)
    step = filters["stepSize"]
    ticks = round(qty / step)
    result = round(ticks * step, 8)
    return max(result, filters["minQty"])


def _round_qty_down(symbol: str, qty: float) -> float:
    """按 stepSize **向下**取整（平仓用：宁少勿多，绝不下超过持有的量）。

    不做 minQty 兜底——那是**开仓**的下限。平仓量不足最小下单量时把它放大到
    minQty 会变成超量平仓：轻则被交易所拒单 → 调用方"记录不收敛"永久重试，
    重则反向开仓。量化到 0 时返回 0，由调用方决定怎么处理。"""
    filters = _fetch_symbol_filters(symbol)
    step = float(filters.get("stepSize") or 0)
    if step <= 0 or qty <= 0:
        return 0.0
    return round(int(qty / step + 1e-9) * step, 8)


def _round_price(symbol: str, price: float) -> float:
    """将价格对齐到 symbol 的 tickSize"""
    filters = _fetch_symbol_filters(symbol)
    step = filters["priceStep"]
    return round(round(price / step) * step, 8)


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
    global _last_error_msg     # 少了这行，下面所有赋值都只写局部变量 →
    # get_last_error() 永远是空的，所有"失败原因"打印出来都是空白
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
            # 错误响应不一定是 JSON：端点是 HTML 404 页时 json.loads 会在 except 里
            # 再抛 JSONDecodeError，异常冲出本函数 → 调用方逐个 except 全废
            # （实盘页"当前挂单"就是这么一直是空的：条件单查询 404，把普通挂单结果也带走了）
            body = e.read().decode("utf-8", "replace")
            try:
                err = json.loads(body)
                code = err.get("code", 0)
                msg = err.get("msg", "")
            except (json.JSONDecodeError, ValueError):
                code, msg = 0, body.strip()[:80]
                print(f"[exchange] {method} {path} HTTP {e.code} 非 JSON 错误页: "
                      f"{msg}")
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


def get_account_info() -> dict | None:
    """查询账户：总权益/保证金余额/可用余额/未实现盈亏"""
    r = _signed_request("GET", "/fapi/v2/account")
    if not r:
        return None
    return {
        "equity": float(r.get("totalWalletBalance", 0)),
        "margin_balance": float(r.get("totalMarginBalance", 0)),
        "available_balance": float(r.get("availableBalance", 0)),
        "unrealized_pnl": float(r.get("totalUnrealizedProfit", 0)),
    }


def get_positions_or_none() -> list[dict] | None:
    """查询当前持仓；**失败返回 None**（与"确实没有持仓"的空列表区分开）。

    给"拿它做收敛判据"的调用方用：把 `[]` 当成"交易所有持仓吗？没有"会误判
    "持仓消失" → 平纸盘 + 记录标 CLOSED → 实盘裸仓。

    不能用 `get_positions() + get_last_error()` 代替：`_last_error_msg` 是**模块级
    共享变量**（跟单/超势/实盘页都在清它），多线程下会被别的调用清空 → 失败结果
    被当成有效 → 正是上面那个误判。
    """
    r = _signed_request("GET", "/fapi/v2/positionRisk")
    if r is None:
        return None
    if not isinstance(r, list):
        return None
    out = []
    for p in r:
        try:
            amt = float(p["positionAmt"])
        except (KeyError, TypeError, ValueError):
            return None          # 字段缺失/脏值 → 整体当"查询不可用"，别当成"没持仓"
        if amt == 0:
            continue
        out.append({
            "symbol": p["symbol"],
            "quantity": amt,
            "entry_price": float(p["entryPrice"]),
            "mark_price": float(p["markPrice"]),
            "unrealized_pnl": float(p["unRealizedProfit"]),
            "leverage": int(p["leverage"]),
            "position_side": p.get("positionSide", "BOTH"),
        })
    return out


def get_positions() -> list[dict]:
    """查询当前持仓（Hedge 模式下同 symbol 分 LONG/SHORT 两行，position_side 区分）"""
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
            "position_side": p.get("positionSide", "BOTH"),
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


def limit_open(token: str, side: str, quantity: float, price: float,
               position_side: str = "LONG") -> dict | None:
    """限价挂单（LIMIT GTC）。side: BUY/SELL。返回 {order_id, status, price, quantity}"""
    symbol = f"{token.upper()}USDT"
    qty = _round_qty(symbol, quantity)
    params = {
        "symbol": symbol,
        "side": side,
        "type": "LIMIT",
        "timeInForce": "GTC",
        "quantity": qty,
        "price": _round_price(symbol, price),
    }
    if config.BINANCE_HEDGE_MODE:
        params["positionSide"] = position_side
    r = _signed_request("POST", "/fapi/v1/order", params)
    if not r:
        return None
    return {
        "order_id": r.get("orderId"),
        "status": r.get("status"),
        "price": float(r.get("price") or 0),
        "quantity": float(r.get("origQty") or 0),
    }


def stop_loss_order(token: str, side: str, quantity: float,
                    stop_price: float, position_side: str = "LONG") -> dict | None:
    """挂止损单（STOP_MARKET, algoOrder 端点）"""
    symbol = f"{token.upper()}USDT"
    qty = _round_qty(symbol, quantity)
    params = {
        "symbol": symbol, "side": side,
        "type": "STOP_MARKET", "algoType": "CONDITIONAL",
        "quantity": qty, "triggerPrice": _round_price(symbol, stop_price),
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
        "quantity": qty, "triggerPrice": _round_price(symbol, stop_price),
        "workingType": "MARK_PRICE",
    }
    if config.BINANCE_HEDGE_MODE:
        params["positionSide"] = position_side
    r = _signed_request("POST", "/fapi/v1/algoOrder", params)
    if not r:
        return None
    return {"order_id": r.get("algoId"), "status": r.get("algoStatus")}


def cancel_order(token: str, order_id: int) -> bool:
    """撤单（algo 优先，再试普通订单）。

    两张端点都失败时**核对一次挂单列表**：单子已经不在了（已成交 / 已被撤）就算成功。
    调用方撤单要的就是"这张单消失"，它已经消失 = 目标达成。否则 SL 触发后撤单必然
    被拒（单不存在）→ 调用方以为"还有单没撤掉"→ 记录永不收敛 → 永久卡在 OPEN。
    """
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
    if r:
        return True
    # 都失败 → 核对它是否已经不在挂单里。查不到（接口失败）→ 保守当失败
    normal = _signed_request("GET", "/fapi/v1/openOrders", {"symbol": symbol})
    algos = _signed_request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol})
    if not isinstance(normal, list) or not isinstance(algos, list):
        return False
    try:
        want = int(order_id)
        for o in normal:
            if int(o.get("orderId") or 0) == want:
                return False
        for o in algos:
            if int(o.get("algoId") or o.get("orderId") or 0) == want:
                return False
    except (TypeError, ValueError):
        return False
    return True          # 两个列表里都没有 → 已经消失（成交/被撤）


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


def get_open_orders_or_none(token: str = None) -> list[dict] | None:
    """查询挂单；**失败返回 None**（与"确实没有挂单"的空列表区分开）。

    要判"我们那条挂单还在不在交易所"，必须用这个：`get_open_orders` 失败时也返回
    `[]`，两者长得一模一样 → 会把"查单失败"当成"单子已经没了" → 误判成交 →
    撤掉真正还在挂的单。调用方必须把 `k is None` 当"未知"处理（保守不动）。
    """
    params = {}
    if token:
        params["symbol"] = f"{token.upper()}USDT"
    r = _signed_request("GET", "/fapi/v1/openOrders", params)
    return r if isinstance(r, list) else None


def get_algo_orders(token: str = None) -> list[dict]:
    """查询条件单（STOP_MARKET / TAKE_PROFIT / TRAILING）。

    端点必须是 `openAlgoOrders`：`algoOpenOrders` 在币安不存在（404 + HTML 错误页），
    实盘页的"当前挂单"因此一直是空的。
    """
    params = {}
    if token:
        params["symbol"] = f"{token.upper()}USDT"
    r = _signed_request("GET", "/fapi/v1/openAlgoOrders", params)
    return r if isinstance(r, list) else []


def close_position_market(token: str, quantity: float, position_side: str = "LONG") -> dict | None:
    """市价平仓（Hedge 模式用 positionSide 区分），默认平多。
    数量**向下**取整且不兜 minQty：平仓单只许小、不许大（放大到 minQty 的超量单
    会被拒，调用方会当成"平仓失败"永久重试；量化到 0 = 残仓不足最小下单量，
    直接返回 None 不落单）。"""
    is_short = position_side == "SHORT"
    symbol = f"{token.upper()}USDT"
    qty = _round_qty_down(symbol, quantity)
    if qty <= 0:
        return None
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
