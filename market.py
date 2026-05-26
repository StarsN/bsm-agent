"""
币安公开行情 API 封装（只读，不需要 API Key）

涉及接口：
- /fapi/v1/exchangeInfo         合约上市列表（判断某币有没有永续合约）
- /fapi/v1/premiumIndex         合约标记价 + 资金费率
- /fapi/v1/openInterest         未平仓合约量
- /futures/data/openInterestHist OI 历史（用于算变化率）
- /futures/data/globalLongShortAccountRatio  全网多空账户比
- /futures/data/topLongShortPositionRatio    大户持仓多空比
- /fapi/v1/klines               合约 K 线（算短期动量/波动）
"""
from __future__ import annotations
import os
import random
import time
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import json
import config


FAPI_BASE = "https://fapi.binance.com"

# 简单内存缓存：合约上市列表几小时才变一次，没必要每轮都请求
_FUTURES_SYMBOLS_CACHE: dict = {"ts": 0.0, "symbols": set()}
_LISTING_CACHE: dict = {"ts": 0.0, "open_times": {}}  # token → openTime(ms)
_CACHE_TTL = 3600  # 1 小时

# 请求频率限制：所有 _http_get 调用都走这里，确保不超过 MAX_RPS
_rate_timestamps: list[float] = []


def _rate_limit(max_rps: int = 0):
    if max_rps <= 0:
        max_rps = getattr(config, "MARKET_MAX_RPS", 4)
    now = time.time()
    cutoff = now - 1.0
    while _rate_timestamps and _rate_timestamps[0] < cutoff:
        _rate_timestamps.pop(0)
    if len(_rate_timestamps) >= max_rps:
        sleep_for = _rate_timestamps[0] + 1.0 - now + 0.05
        if sleep_for > 0:
            time.sleep(sleep_for)
            now = time.time()
            cutoff = now - 1.0
            while _rate_timestamps and _rate_timestamps[0] < cutoff:
                _rate_timestamps.pop(0)
    _rate_timestamps.append(time.time())


# 退避与熔断：连续失败时自动降速，收到 403/429 时全局暂停
_consecutive_failures = 0
_circuit_open_until = 0.0


def _backoff_sleep(attempt: int):
    delay = min(2 ** attempt + random.uniform(0, 1), 60)
    time.sleep(delay)


def _circuit_break(reason: str):
    global _circuit_open_until
    cooldown = random.uniform(30, 90)
    _circuit_open_until = time.time() + cooldown
    print(f"[market] ⚠ 熔断触发: {reason}，暂停 {cooldown:.0f}s")


def _http_get(url: str, params: dict = None, timeout: int = 15,
              max_retries: int = 2) -> Optional[dict]:
    global _consecutive_failures, _circuit_open_until

    if params:
        url = f"{url}?{urlencode(params)}"

    for attempt in range(max_retries + 1):
        # 熔断检查
        if _circuit_open_until > 0:
            remaining = _circuit_open_until - time.time()
            if remaining > 0:
                time.sleep(min(remaining, 10))
                continue
            else:
                _circuit_open_until = 0.0
                _consecutive_failures = 0

        _rate_limit()

        try:
            req = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"})
            with urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8")
                _consecutive_failures = 0
                return json.loads(body)

        except HTTPError as e:
            status = e.code
            if status == 429:
                _circuit_break(f"HTTP 429 Too Many Requests")
            elif status == 403:
                _circuit_break(f"HTTP 403 Forbidden（可能 IP 被禁）")
            elif status >= 500:
                _consecutive_failures += 1
                if attempt < max_retries:
                    _backoff_sleep(attempt + 1)
                    continue
            return None

        except Exception:
            _consecutive_failures += 1
            if attempt < max_retries:
                _backoff_sleep(attempt + 1)
                continue
            return None

    return None


def get_futures_symbols() -> set[str]:
    """返回币安 USDT 永续合约上市的所有 base 币种 set，比如 {'BTC', 'ETH', 'PEPE', ...}"""
    global _FUTURES_SYMBOLS_CACHE, _LISTING_CACHE
    now = time.time()
    if now - _FUTURES_SYMBOLS_CACHE["ts"] < _CACHE_TTL and _FUTURES_SYMBOLS_CACHE["symbols"]:
        return _FUTURES_SYMBOLS_CACHE["symbols"]

    data = _http_get(f"{FAPI_BASE}/fapi/v1/exchangeInfo")
    if not data:
        return _FUTURES_SYMBOLS_CACHE["symbols"]  # 返回上次的缓存

    symbols = set()
    open_times = {}  # token → openTime (Unix ms)
    for s in data.get("symbols", []):
        # 只收 USDT 永续、状态为 TRADING
        if (s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"):
            token = s.get("baseAsset", "").upper()
            symbols.add(token)
            ot = s.get("onboardDate")  # 合约接口用 onboardDate，非 openTime
            if ot:
                open_times[token] = int(ot)

    _FUTURES_SYMBOLS_CACHE = {"ts": now, "symbols": symbols}
    _LISTING_CACHE = {"ts": now, "open_times": open_times}
    return symbols


def has_perpetual(token: str) -> bool:
    return token.upper() in get_futures_symbols()


def _load_token_tags() -> dict:
    """加载 token 类型/链 静态配置"""
    global _TOKEN_TAGS_CACHE
    if _TOKEN_TAGS_CACHE is not None:
        return _TOKEN_TAGS_CACHE
    import json
    tag_path = os.path.join(os.path.dirname(__file__), "extra", "token_tags.json")
    try:
        with open(tag_path, encoding="utf-8") as f:
            _TOKEN_TAGS_CACHE = json.load(f)
    except Exception:
        _TOKEN_TAGS_CACHE = {}
    return _TOKEN_TAGS_CACHE


_TOKEN_TAGS_CACHE: dict | None = None


def get_token_tags(token: str) -> dict:
    """返回币种的品类和公链 {'sector': 'Layer1', 'chain': 'Ethereum'}"""
    tags = _load_token_tags()
    return tags.get(token.upper(), {"sector": None, "chain": None})


def get_listing_age_days(token: str) -> float | None:
    """返回币种在 Binance 上线的天数，从 exchangeInfo.openTime 计算"""
    get_futures_symbols()  # 确保缓存已填充
    global _LISTING_CACHE
    ot = _LISTING_CACHE.get("open_times", {}).get(token.upper())
    if not ot:
        return None
    return (time.time() * 1000 - ot) / (86400 * 1000)


def _perp_symbol(token: str) -> str:
    return f"{token.upper()}USDT"


def get_mark_price(token: str) -> Optional[float]:
    """轻量拉取 USDT 永续标记价，用于持仓价格兜底刷新。"""
    data = _http_get(
        f"{FAPI_BASE}/fapi/v1/premiumIndex",
        {"symbol": _perp_symbol(token)},
        timeout=5,
    )
    if not data:
        return None
    try:
        return float(data.get("markPrice"))
    except (TypeError, ValueError):
        return None


def get_klines_1h(token: str, limit: int = 30) -> list[dict] | None:
    """
    拉 1h K 线，用于 ATR 止损计算。返回按时间升序的 list，每项：
        {"open_time", "open", "high", "low", "close", "volume"}
    失败返回 None。
    """
    raw = _http_get(
        f"{FAPI_BASE}/fapi/v1/klines",
        {"symbol": _perp_symbol(token), "interval": "1h", "limit": limit},
        timeout=8,
    )
    if not raw or not isinstance(raw, list):
        return None
    try:
        return [
            {
                "open_time": int(k[0]),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            }
            for k in raw
        ]
    except (TypeError, ValueError, IndexError):
        return None


def _pct_change(latest: float, past: float) -> Optional[float]:
    if past <= 0:
        return None
    return (latest - past) / past * 100


def _oi_value(row: dict) -> Optional[float]:
    try:
        return float(row.get("sumOpenInterestValue"))
    except (TypeError, ValueError, AttributeError):
        return None


def _depth_metrics(depth: dict, mark_price: float | None) -> dict:
    metrics = {
        "bid_ask_spread_pct": None,
        "depth_bid_1pct_usd": None,
        "depth_ask_1pct_usd": None,
        "depth_imbalance_pct": None,
    }
    if not depth:
        return metrics
    try:
        bids = [(float(p), float(q)) for p, q in depth.get("bids", [])]
        asks = [(float(p), float(q)) for p, q in depth.get("asks", [])]
    except (TypeError, ValueError):
        return metrics
    if not bids or not asks:
        return metrics
    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = mark_price or ((best_bid + best_ask) / 2)
    if mid <= 0:
        return metrics
    metrics["bid_ask_spread_pct"] = (best_ask - best_bid) / mid * 100
    band = config.DEPTH_RANGE_PCT / 100
    bid_floor = mid * (1 - band)
    ask_ceiling = mid * (1 + band)
    bid_usd = sum(price * qty for price, qty in bids if price >= bid_floor)
    ask_usd = sum(price * qty for price, qty in asks if price <= ask_ceiling)
    metrics["depth_bid_1pct_usd"] = bid_usd
    metrics["depth_ask_1pct_usd"] = ask_usd
    total = bid_usd + ask_usd
    if total > 0:
        metrics["depth_imbalance_pct"] = (bid_usd - ask_usd) / total * 100
    return metrics


def _taker_metrics(rows: list) -> dict:
    """
    从 taker long/short ratio 数据计算指标。

    rows 通常是 Binance /futures/data/takerlongshortRatio 返回的 list，
    按时间升序（最早在前，最新在后），每项含 buyVol / sellVol。

    返回：
      taker_buy_sell_ratio  —— 所有区间总合的 buy/sell（现有行为）
      taker_buy_pct         —— buy / (buy+sell) * 100
      taker_ratio_recent    —— 最新一根的 buy/sell
      taker_ratio_older     —— 较早 2 根的平均 buy/sell
      taker_trend_pct       —— (recent - older) / older * 100，正=买盘增强，负=衰退
    """
    metrics = {
        "taker_buy_sell_ratio": None,
        "taker_buy_vol": None,
        "taker_sell_vol": None,
        "taker_buy_pct": None,
        "taker_ratio_recent": None,
        "taker_ratio_older": None,
        "taker_trend_pct": None,
    }
    clean = []
    for row in rows or []:
        try:
            b = float(row.get("buyVol") or 0)
            s = float(row.get("sellVol") or 0)
            if b >= 0 and s >= 0:
                clean.append((b, s))
        except (TypeError, ValueError, AttributeError):
            continue

    if not clean:
        return metrics

    # 总合比例（保持旧行为）
    buy_total = sum(b for b, _ in clean)
    sell_total = sum(s for _, s in clean)
    if buy_total <= 0 and sell_total <= 0:
        return metrics
    metrics["taker_buy_vol"] = buy_total
    metrics["taker_sell_vol"] = sell_total
    if sell_total > 0:
        metrics["taker_buy_sell_ratio"] = buy_total / sell_total
    tot = buy_total + sell_total
    if tot > 0:
        metrics["taker_buy_pct"] = buy_total / tot * 100

    # 趋势指标：需要至少 2 根数据才有意义
    if len(clean) >= 2:
        b_recent, s_recent = clean[-1]
        # 除最新外的所有作为"较早"基线（取平均，抗噪）
        older = clean[:-1]
        b_older = sum(b for b, _ in older) / len(older)
        s_older = sum(s for _, s in older) / len(older)

        r_recent = (b_recent / s_recent) if s_recent > 0 else None
        r_older = (b_older / s_older) if s_older > 0 else None
        metrics["taker_ratio_recent"] = r_recent
        metrics["taker_ratio_older"] = r_older

        if r_recent is not None and r_older is not None and r_older > 0:
            metrics["taker_trend_pct"] = (r_recent - r_older) / r_older * 100

    return metrics


def get_market_snapshot(token: str, heavy: bool = True) -> Optional[dict]:
    """
    拉取某代币的完整市场快照。有永续合约才调用（has_perpetual 外部先判断）

    heavy=True:  全量（含 48h OI/价格、大户多空比）
    heavy=False: 省去重型端点，减少请求数。每 N 轮做一次 heavy=True 即可。

    返回字段：
      symbol              USDT 永续交易对名
      mark_price          合约标记价
      funding_rate        当前资金费率（每 8h）
      funding_rate_pct    百分比形式（0.0001 -> 0.01%）
      oi_usd              当前未平仓合约金额（美元）
      oi_change_1h_pct    OI 1 小时变化率（%）
      change_15m_pct      15 分钟价格变化（%）
      change_1h_pct       1 小时价格变化（%）
      change_4h_pct       4 小时价格变化（%）
      change_24h_pct      24 小时价格变化（%）
      volume_24h_usd      24 小时成交额
      long_short_ratio    全网多空账户比（>1 = 多头多）
      top_trader_ls_ratio 大户持仓多空比
    任何字段取不到就是 None
    """
    symbol = _perp_symbol(token)
    snap = {
        "token": token.upper(),
        "symbol": symbol,
        "mark_price": None,
        "funding_rate": None,
        "funding_rate_pct": None,
        "oi_usd": None,
        "oi_change_15m_pct": None,
        "oi_change_1h_pct": None,
        "oi_change_4h_pct": None,
        "oi_change_48h_pct": None,       # 新增：48h OI 变化
        "change_15m_pct": None,
        "change_1h_pct": None,
        "change_4h_pct": None,
        "change_24h_pct": None,
        "change_48h_pct": None,          # 新增：48h 价格变化
        "volume_24h_usd": None,
        "long_short_ratio": None,
        "top_trader_ls_ratio": None,
        "taker_buy_sell_ratio": None,
        "taker_buy_vol": None,
        "taker_sell_vol": None,
        "taker_buy_pct": None,
        "bid_ask_spread_pct": None,
        "depth_bid_1pct_usd": None,
        "depth_ask_1pct_usd": None,
        "depth_imbalance_pct": None,
        "klines_15m_count": 0,
    }

    # 1) 标记价 + 资金费率
    prem = _http_get(f"{FAPI_BASE}/fapi/v1/premiumIndex", {"symbol": symbol})
    if prem:
        try:
            snap["mark_price"] = float(prem.get("markPrice"))
            fr = float(prem.get("lastFundingRate"))
            snap["funding_rate"] = fr
            snap["funding_rate_pct"] = fr * 100
        except (TypeError, ValueError):
            pass

    # 2) 未平仓合约
    oi_now = _http_get(f"{FAPI_BASE}/fapi/v1/openInterest", {"symbol": symbol})
    if oi_now and snap["mark_price"]:
        try:
            oi_coins = float(oi_now.get("openInterest"))
            snap["oi_usd"] = oi_coins * snap["mark_price"]
        except (TypeError, ValueError):
            pass

    # 3) OI 历史（近 1 小时变化）—— 用 5m 粒度，取最近 13 个点
    oi_hist = _http_get(
        f"{FAPI_BASE}/futures/data/openInterestHist",
        {"symbol": symbol, "period": "5m", "limit": 13},
    )
    if oi_hist and len(oi_hist) >= 2:
        oi_latest = _oi_value(oi_hist[-1])
        if oi_latest is not None:
            if len(oi_hist) >= 4:
                oi_15m = _oi_value(oi_hist[-4])
                if oi_15m is not None:
                    snap["oi_change_15m_pct"] = _pct_change(oi_latest, oi_15m)
            oi_1h = _oi_value(oi_hist[0])
            if oi_1h is not None:
                snap["oi_change_1h_pct"] = _pct_change(oi_latest, oi_1h)

    if heavy:
        oi_hist_4h = _http_get(
            f"{FAPI_BASE}/futures/data/openInterestHist",
            {"symbol": symbol, "period": "15m", "limit": 17},
        )
        if oi_hist_4h and len(oi_hist_4h) >= 2:
            oi_latest = _oi_value(oi_hist_4h[-1])
            oi_past = _oi_value(oi_hist_4h[0])
            if oi_latest is not None and oi_past is not None:
                snap["oi_change_4h_pct"] = _pct_change(oi_latest, oi_past)

        # 3b) 48h OI 变化 —— 用 4h 粒度，取 13 个点（覆盖 48h）
        oi_hist_48h = _http_get(
            f"{FAPI_BASE}/futures/data/openInterestHist",
            {"symbol": symbol, "period": "4h", "limit": 13},
        )
        if oi_hist_48h and len(oi_hist_48h) >= 2:
            try:
                oi_latest = float(oi_hist_48h[-1].get("sumOpenInterestValue"))
                oi_past = float(oi_hist_48h[0].get("sumOpenInterestValue"))
                if oi_past > 0:
                    snap["oi_change_48h_pct"] = (oi_latest - oi_past) / oi_past * 100
            except (TypeError, ValueError):
                pass

    # 4) 价格动量 —— 用合约 K 线，15m 粒度
    klines_count = 0
    klines = _http_get(
        f"{FAPI_BASE}/fapi/v1/klines",
        {"symbol": symbol, "interval": "15m", "limit": 100},
    )
    if klines:
        klines_count = len(klines)
        snap["klines_15m_count"] = klines_count
    if klines and len(klines) >= 17:
        try:
            # K 线第 4 位是收盘价
            closes = [float(k[4]) for k in klines]
            now_price = closes[-1]
            # 最近一根的起始价（15m 前）
            snap["change_15m_pct"] = (now_price - closes[-2]) / closes[-2] * 100
            # 1h 前（4 根）
            if len(closes) >= 5:
                snap["change_1h_pct"] = (now_price - closes[-5]) / closes[-5] * 100
            # 4h 前（16 根）
            if len(closes) >= 17:
                snap["change_4h_pct"] = (now_price - closes[-17]) / closes[-17] * 100
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    if heavy:
        # 4b) 48h 价格变化 —— 用 1h 粒度，取 49 个点
        klines_48h = _http_get(
            f"{FAPI_BASE}/fapi/v1/klines",
            {"symbol": symbol, "interval": "1h", "limit": 49},
        )
        if klines_48h and len(klines_48h) >= 49:
            try:
                closes = [float(k[4]) for k in klines_48h]
                if closes[0] > 0:
                    snap["change_48h_pct"] = (closes[-1] - closes[0]) / closes[0] * 100
            except (TypeError, ValueError, ZeroDivisionError):
                pass

    # 5) 24h 涨跌 + 成交额：直接用已拉到的合约 15m K 线算（96 根 = 24h）
    if klines and len(klines) >= 97:
        try:
            close_24h_ago = float(klines[len(klines) - 97][4])
            now_close = float(klines[-1][4])
            if close_24h_ago > 0:
                snap["change_24h_pct"] = (now_close - close_24h_ago) / close_24h_ago * 100
            vol_24h = sum(float(k[7]) for k in klines[-96:])
            snap["volume_24h_usd"] = vol_24h
        except (TypeError, ValueError, ZeroDivisionError, IndexError):
            pass

    # 6) 多空比
    lsr = _http_get(
        f"{FAPI_BASE}/futures/data/globalLongShortAccountRatio",
        {"symbol": symbol, "period": "15m", "limit": 1},
    )
    if lsr and len(lsr) >= 1:
        try:
            snap["long_short_ratio"] = float(lsr[0].get("longShortRatio"))
        except (TypeError, ValueError):
            pass

    if heavy:
        # 7) 大户持仓多空比（15m 更新一次，没必要 5m 频率拉）
        tlsr = _http_get(
            f"{FAPI_BASE}/futures/data/topLongShortPositionRatio",
            {"symbol": symbol, "period": "15m", "limit": 1},
        )
        if tlsr and len(tlsr) >= 1:
            try:
                snap["top_trader_ls_ratio"] = float(tlsr[0].get("longShortRatio"))
            except (TypeError, ValueError):
                pass

    # 8) 主动买卖量：近 20m taker buy/sell（4 根 5m，用于计算趋势）
    taker = _http_get(
        f"{FAPI_BASE}/futures/data/takerlongshortRatio",
        {"symbol": symbol, "period": "5m", "limit": 4},
    )
    snap.update(_taker_metrics(taker if isinstance(taker, list) else []))

    # 9) 盘口深度 / 流动性
    depth = _http_get(
        f"{FAPI_BASE}/fapi/v1/depth",
        {"symbol": symbol, "limit": config.DEPTH_LIMIT},
    )
    snap.update(_depth_metrics(depth, snap.get("mark_price")))

    # token 类型/链 + 上线天数
    tags = get_token_tags(token)
    snap["sector"] = tags.get("sector")
    snap["chain"] = tags.get("chain")
    snap["listing_age_days"] = get_listing_age_days(token)

    # vol/OI 比值 — 衡量市场换手率与投机热度
    snap["vol_oi_ratio"] = (
        snap["volume_24h_usd"] / snap["oi_usd"]
        if snap.get("volume_24h_usd") and snap.get("oi_usd") else None
    )

    return snap
