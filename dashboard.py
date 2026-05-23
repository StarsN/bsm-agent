"""
市场宏观与风控监控面板 — 数据聚合模块
全部使用 OKX CLI，零币安调用
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone, timedelta


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
                         "desc": f"{'低' if vol_15m_ratio < 1 else '高'}流动性 · 广度待计算",
                         "color": liq_color},
            "macro_3h": {"value": round(chg_3h, 3),
                         "desc": f"成交量{'↑' if volume_trend == 'up' else '↓' if volume_trend == 'down' else '→'}",
                         "color": "yellow" if chg_3h < -1 else "green"},
            "data_age": (lambda age: {"value": f"{age}s" if age < 60 else f"{age//60}m",
                        "desc": f"{'实时' if age < 30 else '延迟'} · OKX",
                        "color": "green" if age < 30 else "yellow"})(
                        int(time.time() - _last_fetch_ok) if _last_fetch_ok else 999)
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

    recent = events[:4]

    return {
        "current_risk": current_risk,
        "next_event": next_event,
        "countdown_str": countdown_str,
        "recent_events": recent,
    }


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

    # BTC 24h
    btc = _okx(["market", "ticker", "BTC-USDT-SWAP"])
    btc_chg = 0
    if btc and isinstance(btc, dict):
        btc_chg = float(btc.get("24h change %", "0").replace("%", ""))

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


def get_ai_regime(alpha_data: dict) -> dict:
    """AI 市场研判 — 30 分钟缓存，减少 LLM 调用"""
    from kol_agent import call_deepseek

    now = (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%H:%M:%S")

    # alpha 数据不可用时直接返回 fallback
    if "error" in alpha_data or "total" not in alpha_data:
        return {
            "current_regime": "chop",
            "confidence": 0,
            "model": "N/A",
            "updated_at": now,
            "judgment_text": "Alpha 池数据不可用，无法生成研判",
            "evidence_support": [],
            "evidence_counter": "",
            "data_sources": [],
            "timeline_24h": _load_timeline(),
        }

    # 从 DB 读间隔配置（默认 30 分钟）
    import config as _cfg
    try:
        import storage as _st
        with _st.get_conn() as conn:
            ts = _st.trading_settings_get(conn)
        cache_ttl = int(ts.get("ai_regime_interval_minutes", 30)) * 60
    except Exception:
        cache_ttl = getattr(_cfg, "AI_REGIME_INTERVAL_MINUTES", 30) * 60

    # 更新 timeline（每次调都更新，本地操作不花钱）
    regime_key = alpha_data.get("altseason", "chop")
    _update_timeline(now, regime_key, 50)

    # 缓存命中 → 直接返回
    global _ai_cache
    if time.time() - _ai_cache["ts"] < cache_ttl and _ai_cache["data"]:
        d = dict(_ai_cache["data"])
        d["updated_at"] = now
        return d

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

    raw = call_deepseek(system, user, max_tokens=1024)
    if raw:
        try:
            data = json.loads(raw)
            regime = data.get("regime", alpha_data.get("altseason", "chop")).lower()
            conf = data.get("conf", 50)
            judgment = data.get("judgment", "暂无AI研判")
            support = data.get("evidence_support", [])
            counter = data.get("evidence_counter", "")
        except Exception:
            regime = alpha_data.get("altseason", "chop")
            conf = 50
            judgment = f"AI 解析失败，基于数据判定: {regime}，上涨{alpha_data.get('up_pct', 0)}%"
            support = [f"上涨占比 {alpha_data.get('up_pct', 0)}%", f"BTC 24h {alpha_data.get('btc_24h', 0)}%"]
            counter = ""
    else:
        regime = alpha_data.get("altseason", "chop")
        conf = 50
        judgment = f"AI 不可用，基于数据: 上涨{alpha_data.get('up_pct', 0)}%，中位{alpha_data.get('median', 0)}%，判定 {regime}"
        support = [f"上涨占比 {alpha_data.get('up_pct', 0)}%"]
        counter = ""

    # 更新 timeline 真实 regime
    _update_timeline(now, regime, conf)

    result = {
        "current_regime": regime,
        "confidence": conf,
        "model": "deepseek-v4-pro",
        "updated_at": now,
        "refresh_interval_min": cache_ttl // 60,
        "judgment_text": judgment,
        "evidence_support": support,
        "evidence_counter": counter,
        "data_sources": ["user:alpha池数据", "web:OKX funding", "web:BTC price"],
        "timeline_24h": _load_timeline(),
    }
    _ai_cache = {"ts": time.time(), "data": result}
    return result


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
