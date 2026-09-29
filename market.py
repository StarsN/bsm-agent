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
# 美股/商品类永续（contractType=TRADIFI_PERPETUAL：CL、NVDA、XAU…）。
# **故意**不并进 _FUTURES_SYMBOLS_CACHE：那 200 多个股票/ETF 符号一旦进了
# get_futures_symbols()，会涌进热度榜/自动交易/链上扫描。跟单这类接口单独用
# has_tradable_perp() 认它们（同一个 exchangeInfo 响应里顺带收集，零额外请求）。
_TRADIFI_SYMBOLS_CACHE: dict = {"ts": 0.0, "symbols": set()}
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
    global _FUTURES_SYMBOLS_CACHE, _LISTING_CACHE, _TRADIFI_SYMBOLS_CACHE
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

    tradifi = {s.get("baseAsset", "").upper()
               for s in data.get("symbols", [])
               if (s.get("contractType") == "TRADIFI_PERPETUAL"
                   and s.get("quoteAsset") == "USDT"
                   and s.get("status") == "TRADING")}

    _FUTURES_SYMBOLS_CACHE = {"ts": now, "symbols": symbols}
    _TRADIFI_SYMBOLS_CACHE = {"ts": now, "symbols": tradifi}
    _LISTING_CACHE = {"ts": now, "open_times": open_times}
    return symbols


# 现货上市列表缓存（与合约缓存对称，同一 TTL）
_SPOT_SYMBOLS_CACHE: dict = {"ts": 0.0, "symbols": set()}


def get_spot_symbols() -> set[str]:
    """返回币安现货 USDT 交易对的所有 base 币种 set，比如 {'BTC', 'ETH', 'PEPE', ...}"""
    global _SPOT_SYMBOLS_CACHE
    now = time.time()
    if now - _SPOT_SYMBOLS_CACHE["ts"] < _CACHE_TTL and _SPOT_SYMBOLS_CACHE["symbols"]:
        return _SPOT_SYMBOLS_CACHE["symbols"]

    data = _http_get("https://api.binance.com/api/v3/exchangeInfo")
    if not data:
        return _SPOT_SYMBOLS_CACHE["symbols"]

    symbols = set()
    for s in data.get("symbols", []):
        if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING":
            symbols.add(s.get("baseAsset", "").upper())

    _SPOT_SYMBOLS_CACHE = {"ts": now, "symbols": symbols}
    return symbols


def has_spot(token: str) -> bool:
    return token.upper() in get_spot_symbols()


def has_perpetual(token: str) -> bool:
    return token.upper() in get_futures_symbols()


def get_tradifi_symbols() -> set[str]:
    """美股/商品类永续（TRADIFI_PERPETUAL）的 base 币种集合，如 CL/NVDA/XAU。

    零额外请求：与 get_futures_symbols() 共用一次 exchangeInfo 和同一个 TTL
    （那个函数刷新时顺带填这份缓存，所以这里调它只为保证"已刷新过"）。"""
    get_futures_symbols()
    return _TRADIFI_SYMBOLS_CACHE["symbols"]


def has_tradable_perp(token: str) -> bool:
    """币安 USDT 永续能不能交易（**含**美股/商品类 TRADIFI_PERPETUAL）。

    跟单链路用这个、不用 has_perpetual：交易员会开 CL（原油）/NVDA 这类仓，
    它们不在 get_futures_symbols() 里（那是给热度榜/自动交易用的纯加密符号表），
    拿 has_perpetual 判断会把能交易的仓当成"币安无该永续"静默跳过。"""
    t = token.upper()
    return t in get_futures_symbols() or t in get_tradifi_symbols()


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
        "ma20_deviation_pct": None,     # MA20 乖离率（15m K线 × 20 = 5小时均线）
        "klines_1h": None,              # 1H K线近6根 [O,H,L,C,Vol,QuoteVol]
        "klines_4h": None,              # 4H K线近6根
    }

    # 1) 标记价 + 资金费率 + 基差
    prem = _http_get(f"{FAPI_BASE}/fapi/v1/premiumIndex", {"symbol": symbol})
    if prem:
        try:
            snap["mark_price"] = float(prem.get("markPrice"))
            fr = float(prem.get("lastFundingRate"))
            snap["funding_rate"] = fr
            snap["funding_rate_pct"] = fr * 100
            idx = float(prem.get("indexPrice", 0))
            if idx > 0:
                snap["basis_pct"] = round((snap["mark_price"] - idx) / idx * 100, 4)
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
            if len(closes) >= 20:
                ma20 = sum(closes[-20:]) / 20
                if ma20 > 0:
                    snap["ma20_deviation_pct"] = (now_price - ma20) / ma20 * 100
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # 聚合 K 线：从 15m 蜡烛合成 1H/4H [O,H,L,C,Vol,QuoteVol]
    def _agg_candle(group):
        try:
            return [
                float(group[0][1]), float(max(float(c[2]) for c in group)),
                float(min(float(c[3]) for c in group)), float(group[-1][4]),
                sum(float(c[5]) for c in group), sum(float(c[7]) for c in group),
            ]
        except Exception:
            return None

    if klines and len(klines) >= 96:
        n = len(klines)
        snap["klines_1h"] = list(filter(None, (_agg_candle(klines[i:i+4]) for i in range(n-96, n, 4))))
    elif klines and len(klines) >= 24:
        n = len(klines)
        snap["klines_1h"] = list(filter(None, (_agg_candle(klines[i:i+4]) for i in range(n-24, n, 4))))
    if klines and len(klines) >= 96:
        n = len(klines)
        snap["klines_4h"] = list(filter(None, (_agg_candle(klines[i:i+16]) for i in range(n-96, n, 16))))

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


# ==========================================================================
# CoinGecko 免费 API — 代币名 → 链 + 市值 + 合约地址
# ==========================================================================

_cg_cache: dict = {}      # {token_lower: {data, ts}}
_CG_CACHE_TTL = 3600       # 缓存 1 小时


def _coingecko_search(token: str) -> str | None:
    """代币名 → CoinGecko coin_id"""
    from urllib.parse import quote
    url = f"https://api.coingecko.com/api/v3/search?query={quote(token)}"
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        coins = data.get("coins", [])
        if not coins:
            return None
        token_u = token.upper()
        for c in coins:
            if c.get("symbol", "").upper() == token_u:
                return c["id"]
        return None  # 无精确匹配
    except HTTPError as e:
        if e.code == 429:
            print(f"  [CG] 搜索限流(429): {token}")
        return None
    except Exception:
        return None


def _coingecko_meta(coin_id: str) -> dict | None:
    """CoinGecko coin_id → {chain, contract_address, market_cap_usd, fdv_usd}"""
    url = (
        f"https://api.coingecko.com/api/v3/coins/{coin_id}"
        "?localization=false&tickers=false&community_data=false"
        "&developer_data=false&sparkline=false"
    )
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        if e.code == 429:
            print(f"  [CG] meta限流(429): {coin_id}")
        return None
    except Exception:
        return None

    result = {}
    # 市值
    mcap = data.get("market_data", {}).get("market_cap", {}).get("usd")
    if mcap:
        result["market_cap_usd"] = mcap
    fdv = data.get("market_data", {}).get("fully_diluted_valuation", {}).get("usd")
    if fdv:
        result["fdv_usd"] = fdv

    # 链 + 合约地址
    # 优先 BSC，其次 asset_platform_id
    chain = None
    address = None
    platforms = data.get("detail_platforms", {})
    # 1) 遍历 detail_platforms，有 bsc 优先取
    for name, info in platforms.items():
        if "bsc" in name.lower() or "binance-smart" in name.lower():
            if isinstance(info, dict) and info.get("contract_address"):
                chain, address = name, info["contract_address"]
                break
    # 2) 没有 bsc，再看 asset_platform_id
    if not chain:
        native_chain = data.get("asset_platform_id")
        if native_chain and native_chain in _CG_TO_GMGN_CHAIN:
            info = platforms.get(native_chain)
            if isinstance(info, dict) and info.get("contract_address"):
                chain, address = native_chain, info["contract_address"]
    if chain:
        result["chain"], result["contract_address"] = chain, address

    return result if result else None  # 至少要有市值或链信息


def _ave_token_info(token: str) -> dict | None:
    """Ave.ai 搜索 → {chain, contract_address, market_cap_usd, fdv_usd}"""
    try:
        api_key = os.environ.get("AVE_API_KEY", getattr(config, "AVE_API_KEY", ""))
        if not api_key:
            return None
        from urllib.parse import quote
        url = f"https://prod.ave-api.com/v2/tokens?keyword={quote(token)}&limit=10"
        req = Request(url, headers={"X-API-KEY": api_key})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("status") != 1:
            return None
        tokens = data.get("data", [])
        if not tokens:
            return None
        # 精确匹配 symbol
        token_u = token.upper()
        for t in tokens:
            if t.get("symbol", "").upper() == token_u:
                result = {}
                mcap = t.get("market_cap")
                if mcap:
                    result["market_cap_usd"] = float(mcap)
                fdv = t.get("fdv")
                if fdv:
                    result["fdv_usd"] = float(fdv)
                chain_name = t.get("chain")
                contract_addr = t.get("token")
                if chain_name and contract_addr:
                    # 映射到 CG 链名格式，确保跟现有链名一致
                    result["chain"] = chain_name
                    result["contract_address"] = contract_addr
                return result if result else None
        return None
    except Exception:
        return None


def get_coingecko_metrics(tokens: list[str]) -> dict[str, dict]:
    """批量获取 CoinGecko 数据，内置缓存"""
    global _cg_cache
    now = time.time()
    result = {}
    to_fetch = []

    for t in tokens:
        key = t.lower()
        cached = _cg_cache.get(key)
        if cached and now - cached["ts"] < _CG_CACHE_TTL:
            if cached["data"]:
                result[t] = cached["data"]
        else:
            to_fetch.append(t)

    if not to_fetch:
        return result

    for token in to_fetch:
        coin_id = _coingecko_search(token)
        meta = _coingecko_meta(coin_id) if coin_id else None
        if not meta:
            meta = _ave_token_info(token)  # CG 失败，Ave 兜底
        if meta:
            _cg_cache[token.lower()] = {"data": meta, "ts": now}
            result[token] = meta

    return result


# ==========================================================================
# GMGN 持仓分布 — 合约地址 → 前10持仓占比
# ==========================================================================

_gmgn_cache: dict = {}       # {(chain, address): {holders, info, ts}}
_GMGN_CACHE_TTL = 1800        # 缓存 30 分钟

_CG_TO_GMGN_CHAIN = {
    "solana": "sol", "ethereum": "eth", "binance-smart-chain": "bsc",
    "base": "base",
}

# GMGN CLI 实际支持的链（npx gmgn-cli 报错提示的合法值）
_GMGN_SUPPORTED_CHAINS = {"sol", "bsc", "base", "eth", "monad"}


def _gmgn_chain(chain: str) -> str | None:
    """CoinGecko chain → GMGN CLI 格式；不支持则返回 None"""
    mapped = _CG_TO_GMGN_CHAIN.get(chain.lower(), chain.lower())
    return mapped if mapped in _GMGN_SUPPORTED_CHAINS else None


def get_gmgn_holders(chain: str, address: str) -> dict | None:
    """GMGN CLI → {top10_pct, top10: [{rank, address_short, tag, pct}, ...]}"""
    import subprocess
    cache_key = (chain, address)
    cached = _gmgn_cache.get(cache_key)
    if cached and time.time() - cached.get("holders_ts", 0) < _GMGN_CACHE_TTL and cached.get("holders"):
        return cached.get("holders")
    orig_chain = chain
    try:
        chain = _gmgn_chain(chain)
        if chain is None:
            raise ValueError("unsupported chain")
        npm_bin = os.environ.get("APPDATA", "") + r"\npm\gmgn-cli.cmd"
        if os.path.exists(npm_bin):
            npx_cmd = [npm_bin]
        else:
            npx_cmd = ["npx", "gmgn-cli"]
        cmd = npx_cmd + [
            "token", "holders",
            "--chain", chain,
            "--address", address,
            "--limit", "10",
            "--raw",
        ]
        env = os.environ.copy()
        env["GMGN_API_KEY"] = env.get("GMGN_API_KEY", "")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            if "429" in err or "rate" in err.lower():
                print(f"  [GMGN] holders限流: {address[:10]}...")
            raise RuntimeError("GMGN CLI failed")
        data = json.loads(proc.stdout)
        holders = data.get("list", [])
        if not holders:
            raise RuntimeError("GMGN returned empty holders")
        top10 = holders[:10]
        top10_pct = sum(
            h.get("amount_percentage", 0) * 100 for h in top10
            if h.get("amount_percentage")
        )
        top = []
        total_profit = 0.0
        total_cost = 0.0
        in_profit = 0
        for i, h in enumerate(top10):
            tag = h.get("wallet_tag_v2") or h.get("name") or ""
            pct = (h.get("amount_percentage") or 0) * 100
            addr_short = h.get("address", "")[:6] if h.get("address") else ""
            cost = _safe_float(h.get("avg_cost"))
            profit = _safe_float(h.get("total_profit") or h.get("profit")) or 0
            realized = _safe_float(h.get("realized_profit")) or 0
            unrealized = _safe_float(h.get("unrealized_profit")) or 0
            bought = _safe_float(h.get("history_bought_cost")) or 0
            sold = _safe_float(h.get("history_sold_income")) or 0
            netflow = (bought or 0) - (sold or 0)
            total_profit += profit
            if cost:
                total_cost += cost * (pct / 100 if top10_pct > 0 else 0)
            if profit > 0:
                in_profit += 1
            top.append({
                "rank": i + 1, "address_short": addr_short, "tag": tag, "pct": pct,
                "cost": cost, "profit": profit,
                "realized": realized, "unrealized": unrealized,
                "bought": bought, "sold": sold, "netflow": netflow,
            })
        # 加权均价 = Σ(cost × pct/100) / Σ(pct/100)
        weight_sum = top10_pct / 100 if top10_pct > 0 else 0
        avg_cost = total_cost / weight_sum if total_cost > 0 and weight_sum > 0 else None
        result = {
            "top10_pct": top10_pct, "top10": top,
            "top10_total_profit": total_profit,
            "top10_in_profit": in_profit,
            "top10_avg_cost": avg_cost,
        }
        entry = _gmgn_cache.get(cache_key, {})
        entry["holders"], entry["holders_ts"] = result, time.time()
        _gmgn_cache[cache_key] = entry
        return result
    except Exception:
        pass

    # GMGN 失败，Ave.ai 兜底
    print(f"  [GMGN] holders失败, Ave兜底: {address[:10]}...")
    return get_ave_holders(orig_chain, address)


def get_ave_holders(chain: str, address: str) -> dict | None:
    """Ave.ai → 与 get_gmgn_holders 相同格式的持仓数据"""
    cache_key = (chain, address)
    cached = _gmgn_cache.get(cache_key)
    if cached and time.time() - cached.get("holders_ts", 0) < _GMGN_CACHE_TTL and cached.get("holders"):
        return cached.get("holders")
    try:
        mapped_chain = _gmgn_chain(chain)
        if mapped_chain is None:
            return None
        api_key = os.environ.get("AVE_API_KEY", getattr(config, "AVE_API_KEY", ""))
        if not api_key:
            return None
        url = f"https://prod.ave-api.com/v2/tokens/top100/{address}-{mapped_chain}?limit=10"
        req = Request(url, headers={"X-API-KEY": api_key})
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("status") != 1:
            return None
        holders = data.get("data", [])
        if not holders:
            return None
        top10 = holders[:10]
        top10_pct = sum(
            (h.get("balance_ratio") or 0) * 100 for h in top10
        )
        top = []
        total_profit = 0.0
        total_cost = 0.0
        in_profit = 0
        for i, h in enumerate(top10):
            tag = h.get("new_tags") or ""
            pct = (h.get("balance_ratio") or 0) * 100
            holder_addr = h.get("holder") or h.get("address", "")
            addr_short = holder_addr[:6] if holder_addr else ""
            cost = _safe_float(h.get("avg_purchase_price"))
            profit = _safe_float(h.get("total_profit")) or 0
            realized = _safe_float(h.get("realized_profit")) or 0
            unrealized = _safe_float(h.get("unrealized_profit")) or 0
            bought = _safe_float(h.get("buy_volume_cur")) or 0
            sold = _safe_float(h.get("sell_volume_cur")) or 0
            netflow = (bought or 0) - (sold or 0)
            total_profit += profit
            if cost:
                total_cost += cost * (pct / 100 if top10_pct > 0 else 0)
            if profit > 0:
                in_profit += 1
            top.append({
                "rank": i + 1, "address_short": addr_short, "tag": tag, "pct": pct,
                "cost": cost, "profit": profit,
                "realized": realized, "unrealized": unrealized,
                "bought": bought, "sold": sold, "netflow": netflow,
            })
        weight_sum = top10_pct / 100 if top10_pct > 0 else 0
        avg_cost = total_cost / weight_sum if total_cost > 0 and weight_sum > 0 else None
        result = {
            "top10_pct": top10_pct, "top10": top,
            "top10_total_profit": total_profit,
            "top10_in_profit": in_profit,
            "top10_avg_cost": avg_cost,
        }
        entry = _gmgn_cache.get(cache_key, {})
        entry["holders"], entry["holders_ts"] = result, time.time()
        _gmgn_cache[cache_key] = entry
        print(f"  [Ave] holders成功: {address[:10]}... ({len(top10)}个)")
        return result
    except Exception:
        print(f"  [Ave] holders失败: {address[:10]}...")
        return None


def get_gmgn_token_info(chain: str, address: str) -> dict | None:
    """GMGN token info → 聪明钱统计 + 持仓安全指标"""
    import subprocess
    cache_key = (chain, address)
    cached = _gmgn_cache.get(cache_key)
    if cached and time.time() - cached.get("info_ts", 0) < _GMGN_CACHE_TTL and cached.get("info"):
        return cached.get("info")
    try:
        chain = _gmgn_chain(chain)
        if chain is None:
            return None
        npm_bin = os.environ.get("APPDATA", "") + r"\npm\gmgn-cli.cmd"
        if os.path.exists(npm_bin):
            npx_cmd = [npm_bin]
        else:
            npx_cmd = ["npx", "gmgn-cli"]
        cmd = npx_cmd + [
            "token", "info",
            "--chain", chain,
            "--address", address,
            "--raw",
        ]
        env = os.environ.copy()
        env["GMGN_API_KEY"] = env.get("GMGN_API_KEY", "")
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            if "429" in err or "rate" in err.lower():
                print(f"  [GMGN] token info限流: {address[:10]}...")
            return None
        data = json.loads(proc.stdout)
        wts = data.get("wallet_tags_stat", {})
        stat = data.get("stat", {})
        result = {
            "gmgn_smart_wallets": wts.get("smart_wallets", 0),
            "gmgn_whale_wallets": wts.get("whale_wallets", 0),
            "gmgn_renowned_wallets": wts.get("renowned_wallets", 0),
            "gmgn_sniper_wallets": wts.get("sniper_wallets", 0),
            "gmgn_fresh_wallets": wts.get("fresh_wallets", 0),
            "gmgn_bundler_wallets": wts.get("bundler_wallets", 0),
            "gmgn_rat_trader_wallets": wts.get("rat_trader_wallets", 0),
            "gmgn_top_10_holder_rate": _pct(stat.get("top_10_holder_rate")),
            "gmgn_dev_hold_rate": _pct(stat.get("dev_team_hold_rate")),
            "gmgn_sniper_hold_rate": _pct(stat.get("top70_sniper_hold_rate")),
            "gmgn_holder_count": stat.get("holder_count", 0),
        }
        entry = _gmgn_cache.get(cache_key, {})
        entry["info"], entry["info_ts"] = result, time.time()
        _gmgn_cache[cache_key] = entry
        return result
    except Exception:
        return None


def _safe_float(v):
    try:
        if v is not None:
            return float(v)
    except (ValueError, TypeError):
        pass
    return None


def _pct(v) -> float | None:
    """GMGN 小数 rate → 百分比，如 0.084 → 8.4"""
    f = _safe_float(v)
    return f * 100 if f is not None else None


def get_daily_klines(token: str, limit: int = 7) -> list[list[float]] | None:
    """拉日线 [O,H,L,C,Vol,QuoteVol] × limit，供 KOL 策略用"""
    klines = _http_get(
        f"{FAPI_BASE}/fapi/v1/klines",
        {"symbol": _perp_symbol(token), "interval": "1d", "limit": limit},
        timeout=5,
    )
    if klines and len(klines) >= 3:  # 至少 3 根才有结构意义
        try:
            return [[float(k[1]), float(k[2]), float(k[3]), float(k[4]),
                     float(k[5]), float(k[7])] for k in klines]
        except Exception:
            pass
    return None
