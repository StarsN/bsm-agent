"""
市场宏观与风控监控面板 — 数据聚合模块
全部使用 OKX CLI，零币安调用
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen


def _okx(args: list[str], live: bool = False) -> dict | list | None:
    """调 OKX CLI，返回解析后的 JSON"""
    cmd = ["okx"]
    if live:
        cmd += ["--profile", "live"]
    cmd += args + ["--json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if r.returncode != 0:
            return None
        out = r.stdout.strip()
        if not out:
            return None
        # 优先直接解析全量输出
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            pass
        # 去掉环境行：从第一个 { 或 [ 开始截取
        for i, ch in enumerate(out):
            if ch in ('{', '['):
                try:
                    return json.loads(out[i:])
                except json.JSONDecodeError:
                    break
        return None
    except Exception:
        return None


def _chg_pct(last: float, open24h: float) -> float:
    if not open24h or open24h == 0:
        return 0
    return (last - open24h) / open24h * 100


# 最后一次成功获取 OKX 数据的时间
_last_fetch_ok: float = 0.0


# ══════════════════════════════════════════════════════════════════════
# 1. 风控 / 流动性指标
# ══════════════════════════════════════════════════════════════════════

def _get_fear_greed() -> dict:
    """拉取 alternative.me 恐惧贪婪指数"""
    try:
        url = "https://api.alternative.me/fng/?limit=2"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data and data.get("data"):
            latest = data["data"][0]
            prev = data["data"][1] if len(data["data"]) > 1 else None
            value = int(latest.get("value", 0))
            # 颜色: 0-25 极度恐惧(红), 26-45 恐惧(黄), 46-55 中性(灰), 56-75 贪婪(绿), 76-100 极度贪婪(绿)
            if value <= 25:
                color = "red"
            elif value <= 45:
                color = "yellow"
            elif value <= 55:
                color = "var(--muted)"
            else:
                color = "green"
            return {
                "value": value,
                "desc": latest.get("value_classification", ""),
                "color": color,
                "prev_value": int(prev.get("value", 0)) if prev else None,
            }
    except Exception:
        pass
    return {"value": None, "desc": "无数据", "color": "var(--muted)", "prev_value": None}


def get_risk_metrics() -> dict:
    now = datetime.now(timezone.utc) + timedelta(hours=8)
    last_updated = now.strftime("%H:%M:%S")

    # BTC 盘口深度（OKX CLI 返回 [{...}]，取第一个元素）
    btc_ob_raw = _okx(["market", "orderbook", "BTC-USDT-SWAP", "--sz", "20"])
    btc_ob = btc_ob_raw[0] if isinstance(btc_ob_raw, list) and btc_ob_raw else None
    btc_spread, btc_imbalance, btc_bid_depth, btc_ask_depth = 0.0, 1.0, 0.0, 0.0
    global _last_fetch_ok
    if btc_ob and isinstance(btc_ob, dict):
        _last_fetch_ok = time.time()
        bids = btc_ob.get("bids", [])
        asks = btc_ob.get("asks", [])
        if bids and asks:
            bid_px = float(bids[0][0])
            ask_px = float(asks[0][0])
            btc_spread = (ask_px - bid_px) / bid_px * 100 if bid_px else 0
            bid_depth = sum(float(b[1]) * float(b[0]) for b in bids[:20])
            ask_depth = sum(float(a[1]) * float(a[0]) for a in asks[:20])
            btc_bid_depth = bid_depth
            btc_ask_depth = ask_depth
            btc_imbalance = bid_depth / ask_depth if ask_depth else 1

    # BTC 1h/3h 大局 (从 K 线算)
    btc_candles = _okx(["market", "candles", "BTC-USDT-SWAP", "--bar", "1H", "--limit", "4"])
    chg_1h, chg_3h, volume_trend = 0.0, 0.0, "stable"
    if btc_candles and isinstance(btc_candles, list) and len(btc_candles) >= 2:
        cur = float(btc_candles[0][4])
        prev = float(btc_candles[1][4])
        chg_1h = (cur - prev) / prev * 100 if prev else 0
        cur_vol = float(btc_candles[0][5])
        prev_vol = float(btc_candles[1][5])
        if prev_vol > 0:
            vol_ratio = cur_vol / prev_vol
            volume_trend = "up" if vol_ratio > 1.3 else "down" if vol_ratio < 0.7 else "stable"
        if len(btc_candles) >= 4:
            p3 = float(btc_candles[3][4])
            chg_3h = (cur - p3) / p3 * 100 if p3 else 0

    # 15m 流动性趋势
    btc_15m = _okx(["market", "candles", "BTC-USDT-SWAP", "--bar", "15m", "--limit", "2"])
    vol_15m_ratio = 1.0
    if btc_15m and isinstance(btc_15m, list) and len(btc_15m) >= 2:
        v0, v1 = float(btc_15m[0][5]), float(btc_15m[1][5])
        vol_15m_ratio = v0 / v1 if v1 else 1

    # 流动性状态判定
    liq_status = "正常"
    liq_color = "green"
    if btc_imbalance < 0.8 or btc_spread > 0.1:
        liq_status = "流动性警戒"
        liq_color = "red"
    elif btc_imbalance < 0.9 or btc_spread > 0.05:
        liq_status = "流动性偏弱"
        liq_color = "yellow"

    # z_depth
    total_depth = btc_bid_depth + btc_ask_depth
    z_depth_val = round(total_depth / 1000000, 2)  # 百万 USD

    # 恐惧贪婪指数
    fng = _get_fear_greed()

    # 全局状态
    global_status = "warning" if liq_color != "green" else "normal"

    return {
        "last_updated": last_updated,
        "refresh_rate": "15s",
        "global_status": global_status,
        "metrics": {
            "liquidity": {"status": liq_status, "color": liq_color,
                          "desc": f"价差{btc_spread:.3f}% 失衡比{btc_imbalance:.2f}"},
            "z_depth": {"value": z_depth_val,
                        "desc": f"{'RISK_OFF' if liq_color != 'green' else '正常'} · 盘口${z_depth_val:.1f}M"},
            "macro_1h": {"value": round(chg_1h, 3),
                         "desc": f"成交量{'↑' if vol_15m_ratio > 1 else '↓'} · 流动性{'高' if vol_15m_ratio > 1 else '低'}",
                         "color": "green" if chg_1h >= 0 else "red"},
            "macro_3h": {"value": round(chg_3h, 3),
                         "desc": f"成交量{'↑' if volume_trend == 'up' else '↓' if volume_trend == 'down' else '→'}",
                         "color": "yellow" if chg_3h < -1 else "green"},
            "data_age": (lambda age: {"value": f"{age}s" if age < 60 else f"{age//60}m",
                        "desc": f"{'实时' if age < 30 else '延迟'} · OKX",
                        "color": "green" if age < 30 else "yellow"})(
                        int(time.time() - _last_fetch_ok) if _last_fetch_ok else 999),
            "fear_greed": {"value": fng["value"] if fng["value"] is not None else "--",
                          "desc": fng["desc"],
                          "color": fng.get("color", "var(--muted)")},
        }
    }


# ══════════════════════════════════════════════════════════════════════
# 2. 宏观事件
# ══════════════════════════════════════════════════════════════════════

# 核心宏观事件关键词（事件名称匹配）
_CORE_EVENT_KEYWORDS = (
    "fomc", "fed fund", "interest rate", "rate decision",
    "non-farm", "nonfarm", "nfp",
    "cpi", "consumer price",
    "pce", "core pce",
    "gdp",
    "unemployment",
)


def _is_core_event(name: str) -> bool:
    """通过事件名称关键词判断是否为核心宏观事件"""
    low = name.lower()
    return any(kw in low for kw in _CORE_EVENT_KEYWORDS)


def get_macro_events() -> dict:
    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)
    week_ms = int((now + timedelta(days=7)).timestamp() * 1000)

    data = _okx([
        "news", "economic-calendar",
        "--region", "united_states",
        "--importance", "3",
        "--before", str(now_ms),
        "--after", str(week_ms),
        "--limit", "50",
    ], live=True)

    events = []
    if data and isinstance(data, list):
        for e in data:
            # OKX 返回 "date" 字段（Unix 毫秒），不是 "time"
            date_ms = e.get("date", "")
            if not date_ms:
                continue
            try:
                dt_utc = datetime.fromtimestamp(int(date_ms) / 1000, tz=timezone.utc)
                dt_cst = dt_utc + timedelta(hours=8)
                t_str = dt_cst.strftime("%Y/%m/%d %H:%M:%S")
            except (ValueError, OSError):
                continue
            name = e.get("event", "")
            tag = "核心" if _is_core_event(name) else "观察"
            events.append({
                "name": name,
                "type": e.get("category", "MACRO"),
                "time": t_str,
                "tag": tag,
                "forecast": e.get("forecast", ""),
                "previous": e.get("previous", ""),
                "region": e.get("region", ""),
            })

    next_event = None
    countdown_str = ""
    current_risk = "normal"
    now_cst = now + timedelta(hours=8)

    if events:
        # 用 datetime 比较，避免 OKX 不补零月份("2026/5/21")与 Python 补零("2026/05/23")的字符串比较 bug
        now_naive = now_cst.replace(tzinfo=None)
        future = []
        for e in events:
            try:
                t_str = e["time"].strip()
                # OKX 格式: "2026/5/21 20:30:00" 或 "2026/05/21 20:30:00"
                fmt = "%Y/%m/%d %H:%M:%S" if len(t_str) > 16 else "%Y/%m/%d %H:%M"
                et = datetime.strptime(t_str, fmt)
                if et > now_naive:
                    future.append((et, e))
            except Exception:
                continue
        future.sort(key=lambda x: x[0])
        if future:
            next_event = future[0][1]
            delta = future[0][0] - now_naive
            if delta.total_seconds() > 0:
                h = int(delta.total_seconds() // 3600)
                m = int((delta.total_seconds() % 3600) // 60)
                countdown_str = f"还有 {h}h {m}m"

        # 宏观风险评级（基于事件名称关键词匹配）
        core_count = sum(1 for e in events if e.get("tag") == "核心")
        if core_count >= 2:
            current_risk = "high"
        elif core_count >= 1:
            current_risk = "medium"

    events_sorted = sorted(events, key=lambda e: e["time"])
    recent = events_sorted[:4]

    return {
        "current_risk": current_risk,
        "next_event": next_event,
        "countdown_str": countdown_str,
        "recent_events": recent,
        "all_events": events_sorted,
    }


# ══════════════════════════════════════════════════════════════════════
# 2b. OKX 衍生数据批量拉取（费率对比、费率历史、Basis基差）
# ══════════════════════════════════════════════════════════════════════

def get_multi_token_okx_metrics(tokens: list[str], heavy: bool = False) -> dict:
    """
    批量拉取 OKX 侧衍生数据，用于 merge 到 market_snapshots。
    返回: {"BTC": {"basis_pct": ..., "okx_funding_pct": ..., "funding_history": [...]}, ...}
    basis 从批量 tickers 计算，逐币费率通过线程池并行拉取，所有 token 全覆盖。
    """
    if not tokens:
        return {}
    result: dict[str, dict] = {t.upper(): {} for t in tokens}

    # 1. 批量拉合约 tickers → 各币合约价
    swap_tickers: dict[str, float] = {}
    raw = _okx(["market", "tickers", "SWAP"])
    if raw and isinstance(raw, list):
        for t in raw:
            inst = t.get("instId", "")
            if not inst.endswith("-USDT-SWAP"):
                continue
            try:
                swap_tickers[inst.replace("-USDT-SWAP", "")] = float(t.get("last", 0) or 0)
            except (ValueError, TypeError):
                pass

    # 2. 批量拉现货 tickers → 各币现货价（用于算基差，instType=SPOT 非 USDT）
    spot_tickers: dict[str, float] = {}
    raw_s = _okx(["market", "tickers", "SPOT"])
    if raw_s and isinstance(raw_s, list):
        for t in raw_s:
            inst = t.get("instId", "")
            if not inst.endswith("-USDT"):
                continue
            try:
                spot_tickers[inst.replace("-USDT", "")] = float(t.get("last", 0) or 0)
            except (ValueError, TypeError):
                pass

    # 3. 计算基差（批量，全部 token）
    for token in result:
        swap_px = swap_tickers.get(token)
        spot_px = spot_tickers.get(token)
        if swap_px and spot_px and spot_px > 0:
            result[token]["basis_pct"] = round((swap_px - spot_px) / spot_px * 100, 4)

    # 4. 逐币费率 + 费率历史 — 线程池并行拉取，全部 token 覆盖
    def _fetch_funding(token: str):
        inst_id = f"{token}-USDT-SWAP"
        out: dict = {}
        fr_raw = _okx(["market", "funding-rate", inst_id])
        fr_data = fr_raw[0] if isinstance(fr_raw, list) and fr_raw else None
        if fr_data and isinstance(fr_data, dict):
            try:
                out["okx_funding_pct"] = float(fr_data.get("fundingRate", 0)) * 100
            except (ValueError, TypeError):
                pass
        hist = _okx(["market", "funding-rate", inst_id, "--history", "--limit", "10"])
        if hist and isinstance(hist, list):
            out["funding_history"] = [
                {"t": h.get("fundingTime", ""),
                 "r": float(h.get("fundingRate", 0)) * 100}
                for h in hist[:10]
            ]
        return token, out

    # 只对有 OKX 合约 ticker 的 token 拉费率，并发度 5 控制资源占用
    tokens_with_swap = [t for t in result if t in swap_tickers]
    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {pool.submit(_fetch_funding, t): t for t in tokens_with_swap}
        for f in as_completed(futures):
            try:
                token, data = f.result()
                result[token].update(data)
            except Exception:
                pass

    # === 市值（一次拉 TOP 100，按 token 匹配；未命中的逐个补拉）===
    mc_data = _okx(["market", "filter", "--instType", "SPOT",
                    "--sortBy", "marketCapUsd", "--sortOrder", "desc",
                    "--limit", "100", "--quoteCcy", "USDT"])
    if mc_data and isinstance(mc_data, list):
        for row in mc_data[0].get("rows", []):
            t = row.get("baseCcy", "").upper()
            if t in result:
                result[t]["market_cap_usd"] = float(row.get("marketCapUsd", 0))
                result[t]["market_cap_rank"] = int(row.get("rank", 0))

    # 补拉：TOP 100 未覆盖的 token，逐个查市值
    missing_mc = [t for t in result if "market_cap_usd" not in result[t]]
    if missing_mc:
        def _fetch_mc(token: str):
            data = _okx(["market", "filter", "--instType", "SPOT",
                         "--baseCcy", token, "--limit", "1"])
            try:
                rows = data[0].get("rows", []) if data and isinstance(data, list) else []
                if rows and rows[0].get("baseCcy", "").upper() == token:
                    return token, {
                        "market_cap_usd": float(rows[0].get("marketCapUsd", 0)),
                        "market_cap_rank": int(rows[0].get("rank", 0)),
                    }
            except Exception:
                pass
            return token, {}

        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(_fetch_mc, t): t for t in missing_mc}
            for f in as_completed(futures):
                try:
                    token, data = f.result()
                    if data:
                        result[token].update(data)
                except Exception:
                    pass

    # === 聪明钱共识信号（按候选币列表直接查，100% 覆盖）===
    if result:
        sm_data = _okx(["smartmoney", "signal-overview-by-filter",
                        "--instCcyList", ",".join(result.keys()),
                        "--sortBy", "pnl", "--period", "7"], live=True)
        if sm_data and isinstance(sm_data, dict):
            sm_items = sm_data.get("data", [])
            if isinstance(sm_items, list):
                for item in sm_items:
                    t = item.get("ccy", "").upper()
                    if t in result:
                        lsr = item.get("longShortRatio", {})
                        nt = item.get("notional", {})
                        wr = item.get("winRate", {})
                        result[t].update({
                            "sm_long_ratio": float(lsr.get("longRatio", 0)) * 100,
                            "sm_net_notional_usdt": float(nt.get("netNotionalUsdt", 0)),
                            "sm_long_avg_entry": float(nt.get("smartMoneyLongAvgEntry", 0)),
                            "sm_short_avg_entry": float(nt.get("smartMoneyShortAvgEntry", 0)),
                            "sm_avg_long_win_rate": float(wr.get("avgLongWinRate", 0)) * 100,
                            "sm_avg_short_win_rate": float(wr.get("avgShortWinRate", 0)) * 100,
                            "sm_traders_with_position": int(item.get("tradersWithPosition", 0)),
                        })

    return result


# ══════════════════════════════════════════════════════════════════════
# 3. Alpha 广度 + AI 研判
# ══════════════════════════════════════════════════════════════════════

def get_alpha_breadth() -> dict:
    """Alpha 池广度分析"""
    tickers = _okx(["market", "tickers", "SWAP"])
    if not tickers or not isinstance(tickers, list):
        return {"error": "无数据"}

    up_count = 0
    total = 0
    changes = []
    extreme_up = []
    extreme_down = []

    for t in tickers:
        inst_id = t.get("instId", "")
        if not inst_id.endswith("-USDT-SWAP"):
            continue
        last = float(t.get("last", 0) or 0)
        open24h = float(t.get("open24h", 0) or 0)
        if open24h <= 0:
            continue
        pct = _chg_pct(last, open24h)
        total += 1
        changes.append(pct)
        if pct > 0:
            up_count += 1
        if pct > 10:
            extreme_up.append({"token": inst_id.replace("-USDT-SWAP", ""), "chg": round(pct, 1)})
        elif pct < -10:
            extreme_down.append({"token": inst_id.replace("-USDT-SWAP", ""), "chg": round(pct, 1)})

    changes.sort()
    up_pct = up_count / total * 100 if total else 0
    median = changes[len(changes) // 2] if changes else 0
    avg = sum(changes) / len(changes) if changes else 0

    # BTC 24h（OKX CLI 可能返回 [{...}] 或 {...}，兼容两种格式）
    btc_raw = _okx(["market", "ticker", "BTC-USDT-SWAP"])
    btc = btc_raw[0] if isinstance(btc_raw, list) and btc_raw else btc_raw
    btc_chg = 0
    if isinstance(btc, dict):
        chg_str = str(btc.get("open24h", "0"))
        try:
            last = float(btc.get("last", 0) or 0)
            open24 = float(chg_str)
            if open24 > 0:
                btc_chg = (last - open24) / open24 * 100
        except (ValueError, TypeError):
            pass

    # Altseason
    if up_pct >= 70:
        altseason = "alt_season"
    elif up_pct >= 50:
        altseason = "alt_pullback"
    else:
        altseason = "chop"

    return {
        "total": total,
        "up_pct": round(up_pct, 1),
        "up_count": up_count,
        "median": round(median, 2),
        "avg": round(avg, 2),
        "btc_24h": round(btc_chg, 2),
        "altseason": altseason,
        "cmc_index": round(up_pct),  # 自算替代
        "extreme_up": extreme_up[:5],
        "extreme_down": extreme_down[:5],
        "changes": changes,  # 给 AI 做分析
    }


# AI 研判缓存（启动偏移 5 分钟首次触发，之后每隔配置间隔刷新，永不撞车）
_ai_cache: dict = {"ts": time.time() - (32*60 - 5*60), "data": {}}
_ai_loading: bool = False


def _log_ai_regime(success: bool, req_bytes: int, resp_bytes: int, ms: int, err: str = ""):
    """写 AI Regime LLM 调用日志到文件，方便排查"""
    try:
        import config as _cfg
        log_path = os.path.join(os.path.dirname(__file__), "logs", "ai_regime_llm.log")
        ts = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%m-%d %H:%M:%S")
        provider = getattr(_cfg, "AI_REGIME_API_BASE", "")[:40]
        line = f"[{ts}] {'OK' if success else 'FAIL'} req={req_bytes} resp={resp_bytes} {ms}ms {provider}"
        if err:
            line += f" | {err}"
        line += "\n"
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass  # 日志写失败不影响主流程


def _call_ai_regime_llm(system: str, user: str) -> str | None:
    """V3 AI Regime 专用 LLM 调用，独立的 API key，不与 KOL Agent 共用"""
    import urllib.request
    import config as _cfg
    api_key = getattr(_cfg, "AI_REGIME_API_KEY", "")
    if not api_key:
        return None
    body = json.dumps({
        "model": getattr(_cfg, "AI_REGIME_MODEL", "deepseek-v4-pro"),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": 32768,
        "temperature": 0.3,
        "reasoning_effort": "max",
    }).encode("utf-8")
    url = f"{getattr(_cfg, 'AI_REGIME_API_BASE', 'https://api.deepseek.com/v1').rstrip('/')}/chat/completions"
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })
    import urllib.error as _urle
    resp_body = b""
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            resp_body = resp.read()
            data = json.loads(resp_body.decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            _log_ai_regime(True, len(body), len(resp_body), int((time.time() - t0) * 1000))
            return content
    except _urle.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:500]
        _log_ai_regime(False, len(body), 0, int((time.time() - t0) * 1000), f"HTTP {e.code}: {err}")
        return None
    except Exception as e:
        detail = str(e)
        if resp_body:
            detail += f" | body={resp_body.decode('utf-8', errors='replace')[:300]}"
        _log_ai_regime(False, len(body), len(resp_body), int((time.time() - t0) * 1000), detail)
        return None


def _parse_llm_response(raw: str | None, alpha_data: dict) -> dict:
    """将 LLM 返回解析为 regime 结果字典（不更新缓存/timeline）"""
    if raw:
        try:
            data = json.loads(raw)
            return {
                "regime": data.get("regime", alpha_data.get("altseason", "chop")).lower(),
                "conf": data.get("conf", 50),
                "judgment": data.get("judgment", "暂无AI研判"),
                "support": data.get("evidence_support", []),
                "counter": data.get("evidence_counter", ""),
            }
        except Exception:
            pass
    return {
        "regime": alpha_data.get("altseason", "chop"),
        "conf": 50,
        "judgment": f"AI 不可用，基于数据: 上涨{alpha_data.get('up_pct', 0)}%，中位{alpha_data.get('median', 0)}%，判定 {alpha_data.get('altseason', 'chop')}",
        "support": [f"上涨占比 {alpha_data.get('up_pct', 0)}%"],
        "counter": "",
    }


def _build_ai_regime_prompt(alpha_data: dict):
    """构建 AI Regime LLM 的 system/user prompt"""
    system = """你是加密货币量化分析师。根据提供的 Alpha 池数据，用中文输出市场研判。

返回 JSON：
{
  "regime": "CHOP / ALT_PULLBACK / ALT_SEASON / RISK_OFF",
  "conf": 0-100,
  "judgment": "一段话总结当前市场状态（100-150字）",
  "evidence_support": ["证据1", "证据2", "证据3"],
  "evidence_counter": "如果市场恶化/好转的可能因素"
}"""

    user = f"""Alpha 池数据:
- 总合约数: {alpha_data.get('total', 0)}
- 上涨占比: {alpha_data.get('up_pct', 0)}% ({alpha_data.get('up_count', 0)}/{alpha_data.get('total', 0)})
- 中位涨跌: {alpha_data.get('median', 0)}%
- 平均涨跌: {alpha_data.get('avg', 0)}%
- BTC 24h: {alpha_data.get('btc_24h', 0)}%
- 暴涨>10%: {len(alpha_data.get('extreme_up', []))}个
- 暴跌>10%: {len(alpha_data.get('extreme_down', []))}个
- 自算 Altseason: {alpha_data.get('altseason', 'chop')}

请给出市场研判。"""

    return system, user


def _ai_cache_ttl():
    """从 DB/config 读取 AI Regime 缓存 TTL（秒）"""
    import config as _cfg
    try:
        import storage as _st
        with _st.get_conn() as conn:
            ts = _st.trading_settings_get(conn)
        return int(ts.get("ai_regime_interval_minutes", 32)) * 60
    except Exception:
        return getattr(_cfg, "AI_REGIME_INTERVAL_MINUTES", 32) * 60


def _ai_cache_age() -> tuple:
    """返回 (age_seconds, age_display)"""
    age_seconds = int(time.time() - _ai_cache["ts"])
    if age_seconds < 60:
        return age_seconds, "几秒前"
    elif age_seconds < 3600:
        return age_seconds, f"{age_seconds // 60}分钟前"
    else:
        return age_seconds, f"{age_seconds // 3600}小时前"


def refresh_ai_regime(alpha_data: dict):
    """collector 专用：无条件启动后台 LLM 刷新缓存"""
    if "error" in alpha_data or "total" not in alpha_data:
        return

    import threading
    global _ai_loading
    if _ai_loading:
        return

    system, user = _build_ai_regime_prompt(alpha_data)
    cache_ttl = _ai_cache_ttl()

    _ai_loading = True

    def _bg_fetch():
        global _ai_cache, _ai_loading
        try:
            raw = None
            for attempt in range(3):
                raw = _call_ai_regime_llm(system, user)
                if raw:
                    break
                if attempt < 2:
                    time.sleep(3)
            parsed = _parse_llm_response(raw, alpha_data)
            now_done = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%H:%M:%S")
            _update_timeline(now_done, parsed["regime"], parsed["conf"])
            result = _build_result(parsed, now_done, cache_ttl)
            _ai_cache = {"ts": time.time(), "data": result}
        finally:
            _ai_loading = False

    threading.Thread(target=_bg_fetch, daemon=True, name="ai-regime-llm").start()


def get_ai_regime() -> dict:
    """纯读缓存 — 页面 API 专用，永远不阻塞也不触发 LLM"""
    global _ai_cache

    import config as _cfg
    now = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%H:%M:%S")
    cache_ttl = _ai_cache_ttl()
    age_seconds, age_display = _ai_cache_age()

    if _ai_cache["data"]:
        d = dict(_ai_cache["data"])
        d["updated_at"] = now
        d["cache_age_seconds"] = age_seconds
        d["cache_age_display"] = age_display
        return d

    return {
        "current_regime": "chop", "confidence": 0,
        "model": getattr(_cfg, "AI_REGIME_MODEL", "deepseek-v4-pro"),
        "updated_at": now,
        "refresh_interval_min": cache_ttl // 60,
        "judgment_text": "AI 研判生成中，请稍后刷新...",
        "evidence_support": [], "evidence_counter": "", "data_sources": [],
        "timeline_24h": _load_timeline(),
        "cache_age_seconds": -1,
        "cache_age_display": "等待首次刷新",
    }


def _build_result(parsed: dict, now: str, cache_ttl: int, loading: bool = False) -> dict:
    """组装 get_ai_regime 返回"""
    import config as _cfg
    return {
        "current_regime": parsed["regime"],
        "confidence": parsed["conf"],
        "model": getattr(_cfg, "AI_REGIME_MODEL", "deepseek-v4-pro"),
        "updated_at": now,
        "refresh_interval_min": cache_ttl // 60,
        "judgment_text": "AI 研判生成中，请稍后刷新..." if loading else parsed["judgment"],
        "evidence_support": [] if loading else parsed["support"],
        "evidence_counter": "" if loading else parsed["counter"],
        "data_sources": [] if loading else ["user:alpha池数据", "web:OKX funding", "web:BTC price"],
        "timeline_24h": _load_timeline(),
    }


def _update_timeline(hour_label: str, regime: str, conf: int):
    timeline_file = os.path.join(os.path.dirname(__file__), "regime_timeline.json")
    timeline = []
    try:
        if os.path.exists(timeline_file):
            with open(timeline_file) as f:
                timeline = json.load(f)
    except Exception:
        pass
    h = hour_label[:2]
    # 同小时不重复追加
    if not timeline or timeline[-1].get("h") != h:
        timeline.append({"h": h, "r": regime, "c": conf})
    timeline = timeline[-24:]
    try:
        with open(timeline_file, "w") as f:
            json.dump(timeline, f)
    except Exception:
        pass


def _load_timeline() -> list:
    timeline_file = os.path.join(os.path.dirname(__file__), "regime_timeline.json")
    try:
        if os.path.exists(timeline_file):
            with open(timeline_file) as f:
                return [t["r"] for t in json.load(f)]
    except Exception:
        pass
    return []
