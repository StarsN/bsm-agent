"""
Web 仪表盘服务
运行：python web.py
访问：http://localhost:8000
"""
import json
import subprocess
import sys
import time
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

import config
import storage
from okx_source import (
    search_token_address,
    fetch_cluster_overview,
    fetch_cluster_list,
    fetch_wallet_analysis,
    fetch_token_trades,
)
import trade_logic
from analyzer import compute_composite_scores, get_current_heat_scores
from market import has_perpetual, get_market_snapshot, get_futures_symbols
from signals import analyze as analyze_signals
import kol_agent


app = FastAPI(title="Binance Square Monitor")


# ============================================================
# 轻量内存缓存：给高频只读 API 加 2 秒 TTL
# 目的：worker 正在跑重活时，前端刷新不会每次都挤进 SQLite 排队
# ============================================================
_cache = {}
_cache_lock = threading.Lock()
_max_dd_cache = {}


def _cached(key: str, ttl_seconds: float, fn):
    """非常简单的内存缓存：<ttl 秒内返回缓存，过期重新计算"""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (now - hit[0]) < ttl_seconds:
            return hit[1]
    # 缓存过期或未命中：重新计算（在锁外算，避免一个慢请求阻塞其他）
    value = fn()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def _cache_invalidate(*keys):
    """用户写操作（收藏/取消/改设置）后调用，让缓存立即失效"""
    with _cache_lock:
        if not keys:
            _cache.clear()
        else:
            for k in keys:
                _cache.pop(k, None)


# === Agent 候选币收集器 ===
# 后台线程每 3 秒复现面板"做多建议"，
# 累计后每隔指定分钟入库 agent_candidates 并触发 Agent。
_collected = {}          # token → {token, data, tier, passed, ...}
_last_flush = time.time()
_flush_lock = threading.Lock()

# KOL Agent 独立累积器
_kol_collected = {}
_kol_last_flush = 0.0
_kol_round = 0
_kol_running = False          #

# KOL 认知快照 Agent 独立累积池
_kol_snapshot_collected = {}
_kol_snapshot_last_flush = 0.0
_kol_snapshot_round = 0
_kol_snapshot_running = False

# 防重叠：上一轮未完成时不触发

# 无教训版 Agent 独立累积器
_nl_collected = {}
_nl_last_flush = 0.0
_nl_round = 0
_nl_running = False

# 做空版 Agent 独立累积器
_short_collected = {}
_short_last_flush = 0.0
_short_round = 0
_short_running = False


def _build_data_blob(candidate: dict) -> dict:
    """组装与 extract 脚本同格式的候选币 data JSON"""
    snap = candidate["market"]["snapshot"]
    analysis = candidate["market"].get("analysis") or {}
    score_row = candidate["score"]
    return {
        "token": candidate["token"], "social_score": score_row.get("score", 0),
        "mentions": score_row.get("mentions", 0),
        "price": snap.get("mark_price"), "15m": snap.get("change_15m_pct"),
        "1h": snap.get("change_1h_pct"), "4h": snap.get("change_4h_pct"),
        "24h": snap.get("change_24h_pct"), "oi_15m": snap.get("oi_change_15m_pct"),
        "oi_1h": snap.get("oi_change_1h_pct"), "oi_4h": snap.get("oi_change_4h_pct"),
        "oi_48h": snap.get("oi_change_48h_pct"), "funding": snap.get("funding_rate_pct"),
        "lsr": snap.get("long_short_ratio"), "top_lsr": snap.get("top_trader_ls_ratio"),
        "taker": snap.get("taker_buy_sell_ratio"), "taker_pct": snap.get("taker_buy_pct"),
        "taker_trend": snap.get("taker_trend_pct"),
        "spread": snap.get("bid_ask_spread_pct"),
        "depth_bid": snap.get("depth_bid_1pct_usd"),
        "depth_ask": snap.get("depth_ask_1pct_usd"),
        "imbalance": snap.get("depth_imbalance_pct"),
        "vol_24h": snap.get("volume_24h_usd"), "oi_usd": snap.get("oi_usd"),
        "chg_48h": snap.get("change_48h_pct"),
        "oi_divergence": analysis.get("oi_divergence"),
        "verdict": analysis.get("verdict"), "direction": analysis.get("direction"),
        "tags": analysis.get("tags", []), "notes": analysis.get("notes", []),
        "age": (None if not snap.get("klines_15m_count") else
                ">1d" if snap["klines_15m_count"] >= 96 else
                f"{snap['klines_15m_count'] * 15 // 60 // 24}d"
                f"{snap['klines_15m_count'] * 15 // 60 % 24}h"),
        "tier": candidate["tier"], "passed": candidate["passed"],
        "hard_block": candidate["hard_block"],
        "pass_count": candidate["pass_count"],
        "signal_key": candidate.get("signal_key", ""),
        "suggestion": candidate["suggestion"], "reasons": candidate["reasons"],
        "market_cap_usd": snap.get("market_cap_usd"),
    }


def _build_kol_data_blob(candidate: dict) -> dict:
    """KOL Agent 专用 — 通用 blob + 全景字段"""
    blob = _build_data_blob(candidate)
    snap = candidate["market"]["snapshot"]
    blob.update({
        "vol_oi": snap.get("vol_oi_ratio"),
        "listing_days": snap.get("listing_age_days"),
        "sector": snap.get("sector"),
        "chain": snap.get("chain"),
        "basis": snap.get("basis_pct"),
        "okx_funding": snap.get("okx_funding_pct"),
        "funding_hist": snap.get("funding_history"),
        # 乖离率 + 市值 + 聪明钱
        "ma20_deviation": snap.get("ma20_deviation_pct"),
        "market_cap_usd": snap.get("market_cap_usd"),
        "fdv_usd": snap.get("fdv_usd"),
        "market_cap_rank": snap.get("market_cap_rank"),
        "sm_long_ratio": snap.get("sm_long_ratio"),
        "sm_net_notional_usdt": snap.get("sm_net_notional_usdt"),
        "sm_long_avg_entry": snap.get("sm_long_avg_entry"),
        "sm_short_avg_entry": snap.get("sm_short_avg_entry"),
        "sm_avg_long_win_rate": snap.get("sm_avg_long_win_rate"),
        "sm_avg_short_win_rate": snap.get("sm_avg_short_win_rate"),
        "sm_traders_with_position": snap.get("sm_traders_with_position"),
        "oi_marketcap": snap.get("oi_marketcap_ratio"),
        "klines_1h": snap.get("klines_1h"),
        "klines_4h": snap.get("klines_4h"),
    })
    return blob


def _scan_candidates():
    with storage.get_conn() as conn:
        items, skipped = _build_leaderboard_items(conn)
        return trade_logic.build_trade_candidates_from_leaderboard(
            conn, items, passed_only=True)


def _scan_short_candidates():
    """做空候选币"""
    with storage.get_conn() as conn:
        items, skipped = _build_leaderboard_items(conn)
        return trade_logic.build_trade_candidates_from_leaderboard(
            conn, items, passed_only=True, direction="short")


def _scan_candidates_kol():
    """KOL 专用 — 从做多建议中获取 passed=True 的候选"""
    with storage.get_conn() as conn:
        items, skipped = _build_leaderboard_items(conn)
        return trade_logic.build_trade_candidates_from_leaderboard(
            conn, items, passed_only=True)


def _scan_kol_display():
    """KOL 候选展示 — 跟 KOL 策略同筛选逻辑，纯读，不触发 LLM"""
    with storage.get_conn() as conn:
        items, skipped = _build_leaderboard_items(conn)
        candidates = []
        for rank, item in enumerate(items, 1):
            is_kol, kol_reasons = kol_agent.kol_is_interesting(item)
            if not is_kol:
                continue
            market = item.get("market") or trade_logic._load_market(conn, item["token"])
            realtime = trade_logic._load_realtime(conn, item["token"])
            price = trade_logic._current_price(market, realtime)
            candidates.append({
                "token": item["token"], "rank": rank, "price": price,
                "suggestion": "KOL关注", "reasons": kol_reasons,
                "has_active_position": storage.trade_has_active(conn, item["token"]),
                "direction": "kol", "passed": True,
            })
        return candidates


def _run_kol_agent():
    """后台执行 KOL Agent，不阻塞 collector"""
    global _kol_running
    try:
        with storage.get_conn() as conn:
            kol_agent.analyze_candidates(conn)
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        _kol_running = False


def _run_kol_snapshot_agent():
    """后台执行 KOL 认知快照 Agent"""
    global _kol_snapshot_running
    try:
        with storage.get_conn() as conn:
            kol_agent.analyze_candidates_snapshot(conn)
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        _kol_snapshot_running = False


def _run_nl_agent():
    """后台触发无教训版 Agent（hermes cron）"""
    global _nl_running
    try:
        job_id = getattr(config, "NL_AGENT_HERMES_JOB_ID", "")
        if job_id:
            subprocess.run(
                ["hermes", "cron", "run", job_id],
                timeout=10, capture_output=True, text=True,
            )
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        _nl_running = False


def _run_short_agent():
    """后台触发做空版 Agent（hermes cron）"""
    global _short_running
    try:
        job_id = getattr(config, "AGENT_SHORT_HERMES_JOB_ID", "")
        if job_id:
            subprocess.run(
                ["hermes", "cron", "run", job_id],
                timeout=10, capture_output=True, text=True,
            )
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        _short_running = False


def _collect_loop():
    global _collected, _last_flush, _kol_collected, _kol_last_flush, _kol_round, _kol_running, _nl_collected, _nl_last_flush, _nl_round, _nl_running, _kol_snapshot_collected, _kol_snapshot_last_flush, _kol_snapshot_round, _kol_snapshot_running, _short_collected, _short_last_flush, _short_round, _short_running
    poll_seconds = getattr(config, "AGENT_COLLECT_POLL_SECONDS", 5)
    cache_ttl = getattr(config, "AGENT_COLLECT_CACHE_TTL", 5)

    def _read_interval():
        try:
            with storage.get_conn() as conn:
                settings = storage.trading_settings_get(conn)
                return int(settings.get("agent_collect_interval_minutes",
                         getattr(config, "AGENT_COLLECT_INTERVAL_MINUTES", 15)))
        except Exception:
            return getattr(config, "AGENT_COLLECT_INTERVAL_MINUTES", 15)

    def _read_kol_interval():
        try:
            with storage.get_conn() as conn:
                ts = storage.trading_settings_get(conn)
            return int(ts.get("kol_agent_interval_minutes",
                       getattr(config, "KOL_AGENT_INTERVAL_MINUTES", 8)))
        except Exception:
            return getattr(config, "KOL_AGENT_INTERVAL_MINUTES", 8)

    def _read_nl_interval():
        try:
            with storage.get_conn() as conn:
                ts = storage.trading_settings_get(conn)
            return int(ts.get("nl_agent_interval_minutes",
                       getattr(config, "NL_AGENT_INTERVAL_MINUTES", 30)))
        except Exception:
            return getattr(config, "NL_AGENT_INTERVAL_MINUTES", 30)

    def _read_short_interval():
        try:
            with storage.get_conn() as conn:
                ts = storage.trading_settings_get(conn)
            return int(ts.get("agent_short_interval_minutes",
                       getattr(config, "AGENT_SHORT_INTERVAL_MINUTES", 16)))
        except Exception:
            return getattr(config, "AGENT_SHORT_INTERVAL_MINUTES", 16)

    interval_seconds = _read_interval() * 60
    kol_interval_seconds = _read_kol_interval() * 60
    kol_snapshot_interval_seconds = kol_interval_seconds
    nl_interval_seconds = _read_nl_interval() * 60
    short_interval_seconds = _read_short_interval() * 60
    now_start = time.time()
    # 偏移量防撞车: KOL(8,offset=0) 主(16,offset=1) NL(16,offset=3) 做空(16,offset=5) AI(32,offset=5) 永不重叠
    _ai_regime_last_refresh = now_start - (32*60 - 5*60)  # 首次 5 分钟后
    _last_flush       = now_start - (16*60 - 1*60)   # 首次 1 分钟后
    _kol_last_flush            = now_start               # 首次 8 分钟后
    _kol_snapshot_last_flush   = now_start - (8*60 - 4*60)  # offset=4
    _nl_last_flush    = now_start - (16*60 - 3*60)   # 首次 3 分钟后
    _short_last_flush = now_start - (16*60 - 5*60)   # 首次 5 分钟后
    _last_heartbeat = 0
    ts_settings = {}  # 心跳刷新，供 flush 点读取开关
    # 从 DB 恢复 _kol_round 和 _nl_round，防止重启后读到旧轮数据
    try:
        with storage.get_conn(config.KOL_DB) as conn:
            row = conn.execute("SELECT COALESCE(MAX(round_number), 0) FROM kol_candidates").fetchone()
            _kol_round = row[0] if row else 0
        with storage.get_conn(config.SNAPSHOT_DB) as conn:
            row = conn.execute("SELECT COALESCE(MAX(round_number), 0) FROM kol_candidates").fetchone()
            _kol_snapshot_round = row[0] if row else 0
        with storage.get_conn(config.NL_DB) as conn:
            row = conn.execute("SELECT COALESCE(MAX(round_number), 0) FROM nl_candidates").fetchone()
            _nl_round = row[0] if row else 0
        with storage.get_conn(config.AGENT_SHORT_DB) as conn:
            row = conn.execute("SELECT COALESCE(MAX(round_number), 0) FROM agent_short_candidates").fetchone()
            _short_round = row[0] if row else 0
    except Exception:
        pass
    print(f"[collect] 线程已启动，每 {interval_seconds // 60} 分钟入库，KOL 每 {kol_interval_seconds // 60} 分钟触发，NL 每 {nl_interval_seconds // 60} 分钟触发，做空 每 {short_interval_seconds // 60} 分钟触发", flush=True)

    while True:
        try:
            time.sleep(poll_seconds)
            candidates = _cached("candidates_scan", cache_ttl, _scan_candidates)

            for c in candidates:
                # 主 Agent：开关关闭时跳过收集
                if ts_settings.get("agent_trade_enabled", True):
                    _collected[c["token"]] = {
                        "token": c["token"],
                        "data": json.dumps(_build_data_blob(c), default=str, ensure_ascii=False),
                        "tier": c["tier"],
                        "passed": 1 if c["passed"] else 0,
                        "hard_blocks": json.dumps(c.get("hard_block", []), ensure_ascii=False),
                        "pass_count": c.get("pass_count", 0),
                        "signal_key": c.get("signal_key", ""),
                    }
                # 无教训版 Agent：独立累积（开关关闭时跳过，passed 的才收）
                if ts_settings.get("nl_agent_enabled", True) and c.get("passed"):
                    _nl_collected[c["token"]] = {
                        "token": c["token"],
                        "data": json.dumps(_build_data_blob(c), default=str, ensure_ascii=False),
                        "tier": c.get("tier", ""),
                        "passed": 1,
                        "hard_blocks": json.dumps(c.get("hard_block", [])),
                        "pass_count": c.get("pass_count", 0),
                        "signal_key": c.get("signal_key", ""),
                    }
                # 做空版 Agent：取做空建议的候选币
                if ts_settings.get("agent_short_enabled", True):
                    for sc in _cached("short_candidates_scan", cache_ttl, _scan_short_candidates):
                        if sc.get("passed"):
                            _short_collected[sc["token"]] = {
                                "token": sc["token"],
                                "data": json.dumps(_build_data_blob(sc), default=str, ensure_ascii=False),
                                "tier": sc.get("tier", ""),
                                "passed": 1,
                                "hard_blocks": json.dumps(sc.get("hard_block", [])),
                                "pass_count": sc.get("pass_count", 0),
                                "signal_key": sc.get("signal_key", ""),
                            }

            # === KOL Agent：直接从做多建议采集 ===
            if ts_settings.get("kol_agent_enabled", True) or ts_settings.get("kol_snapshot_enabled", True):
                kol_candidates = _cached("kol_candidates_scan", cache_ttl, _scan_candidates_kol)
                if ts_settings.get("kol_agent_enabled", True):
                    for c in kol_candidates:
                        _kol_collected[c["token"]] = {
                            "token": c["token"],
                            "data": json.dumps(_build_kol_data_blob(c), default=str, ensure_ascii=False),
                            "tier": c.get("tier", ""),
                            "passed": 1,
                            "hard_blocks": json.dumps(c.get("hard_block", [])),
                            "pass_count": c.get("pass_count", 0),
                            "signal_key": c.get("signal_key", ""),
                        }
                if ts_settings.get("kol_snapshot_enabled", True):
                    for c in kol_candidates:
                        _kol_snapshot_collected[c["token"]] = {
                            "token": c["token"],
                            "data": json.dumps(_build_kol_data_blob(c), default=str, ensure_ascii=False),
                            "tier": c.get("tier", ""),
                            "passed": 1,
                            "hard_blocks": json.dumps(c.get("hard_block", [])),
                            "pass_count": c.get("pass_count", 0),
                            "signal_key": c.get("signal_key", ""),
                        }

            now = time.time()
            if now - _last_heartbeat >= 60:
                _last_heartbeat = now
                # 即时生效：从 DB 重读间隔
                try:
                    with storage.get_conn() as conn:
                        ts_settings = storage.trading_settings_get(conn)
                    new_interval = int(ts_settings.get("agent_collect_interval_minutes",
                               getattr(config, "AGENT_COLLECT_INTERVAL_MINUTES", 15)))
                    new_interval *= 60
                except Exception:
                    new_interval = _read_interval() * 60
                if new_interval != interval_seconds:
                    print(f"[collect] 间隔变更: {interval_seconds//60} → {new_interval//60} 分钟", flush=True)
                    interval_seconds = new_interval
                try:
                    new_kol_interval = int(ts_settings.get("kol_agent_interval_minutes",
                                           getattr(config, "KOL_AGENT_INTERVAL_MINUTES", 8))) * 60
                except Exception:
                    new_kol_interval = _read_kol_interval() * 60
                if new_kol_interval != kol_interval_seconds:
                    print(f"[kol-collect] KOL间隔变更: {kol_interval_seconds//60} → {new_kol_interval//60} 分钟", flush=True)
                    kol_interval_seconds = new_kol_interval
                try:
                    new_snapshot_interval = int(ts_settings.get("kol_snapshot_interval_minutes",
                                                 getattr(config, "KOL_SNAPSHOT_INTERVAL_MINUTES", 8))) * 60
                except Exception:
                    new_snapshot_interval = kol_interval_seconds
                if new_snapshot_interval != kol_snapshot_interval_seconds:
                    print(f"[kol-snapshot-collect] 快照间隔变更: {kol_snapshot_interval_seconds//60} → {new_snapshot_interval//60} 分钟", flush=True)
                    kol_snapshot_interval_seconds = new_snapshot_interval
                try:
                    new_nl_interval = int(ts_settings.get("nl_agent_interval_minutes",
                                           getattr(config, "NL_AGENT_INTERVAL_MINUTES", 30))) * 60
                except Exception:
                    new_nl_interval = _read_nl_interval() * 60
                if new_nl_interval != nl_interval_seconds:
                    print(f"[nl-collect] NL间隔变更: {nl_interval_seconds//60} → {new_nl_interval//60} 分钟", flush=True)
                    nl_interval_seconds = new_nl_interval
                try:
                    new_short_interval = int(ts_settings.get("agent_short_interval_minutes",
                                             getattr(config, "AGENT_SHORT_INTERVAL_MINUTES", 16))) * 60
                except Exception:
                    new_short_interval = _read_short_interval() * 60
                if new_short_interval != short_interval_seconds:
                    print(f"[short-collect] 做空间隔变更: {short_interval_seconds//60} → {new_short_interval//60} 分钟", flush=True)
                    short_interval_seconds = new_short_interval
                # AI Regime 定时刷新（后台调用，不阻塞 collector）
                try:
                    ai_interval = int(ts_settings.get("ai_regime_interval_minutes", 32)) * 60
                except Exception:
                    ai_interval = getattr(config, "AI_REGIME_INTERVAL_MINUTES", 32) * 60
                if now - _ai_regime_last_refresh >= ai_interval:
                    _ai_regime_last_refresh = now
                    try:
                        import dashboard
                        dashboard.refresh_ai_regime(dashboard.get_alpha_breadth())
                    except Exception:
                        pass
                remaining = max(0, interval_seconds - (now - _last_flush))
                kol_remaining = max(0, kol_interval_seconds - (now - _kol_last_flush))
                nl_remaining = max(0, nl_interval_seconds - (now - _nl_last_flush))
                short_remaining = max(0, short_interval_seconds - (now - _short_last_flush))
                print(f"[collect] 距下次入库约 {remaining/60:.0f} 分钟"
                      f"（已收集 {len(_collected)} 个），"
                      f"KOL 距触发约 {kol_remaining/60:.0f} 分钟（{len(_kol_collected)}），"
                      f"NL 距触发约 {nl_remaining/60:.0f} 分钟（{len(_nl_collected)}），"
                      f"做空 距触发约 {short_remaining/60:.0f} 分钟（{len(_short_collected)}）", flush=True)

            with _flush_lock:
                if now - _last_flush >= interval_seconds and _collected:
                    elapsed = now - _last_flush
                    batch = list(_collected.values())

                    if ts_settings.get("agent_trade_enabled", True):
                        with storage.get_conn() as sys_conn:
                            last_round = sys_conn.execute(
                                "SELECT COALESCE(MAX(round_number), 0) FROM token_heat_history"
                            ).fetchone()[0]
                        with storage.get_conn(config.AGENT_MAIN_DB) as conn:
                            storage.agent_candidates_insert_batch(conn, last_round, batch)
                            storage.agent_candidates_purge_old(conn, keep_last_rounds=50)

                    _collected.clear()
                    _last_flush = now

                    if ts_settings.get("agent_trade_enabled", True):
                        ts_str = datetime.now().strftime("%H:%M:%S")
                        print(f"[collect] {ts_str} 入库 {len(batch)} 个候选"
                              f"（距上次 {elapsed/60:.0f} 分钟），已触发 Agent", flush=True)
                        job_id = getattr(config, "AGENT_HERMES_JOB_ID", "")
                        if job_id:
                            subprocess.run(
                                ["hermes", "cron", "run", job_id],
                                timeout=10, capture_output=True, text=True,
                            )

            # === KOL Agent 独立定时触发 ===
            if (ts_settings.get("kol_agent_enabled", True) and _kol_collected
                    and not _kol_running and now - _kol_last_flush >= kol_interval_seconds):
                batch = list(_kol_collected.values())
                with storage.get_conn(config.KOL_DB) as conn:
                    storage.kol_candidates_insert_batch(conn, _kol_round + 1, batch)
                    storage.kol_candidates_purge_old(conn, keep_last_rounds=30)
                _kol_last_flush = now
                _kol_running = True
                _kol_round += 1
                _kol_collected.clear()
                ts_str = datetime.now().strftime("%H:%M:%S")
                print(f"[kol-collect] {ts_str} 入库 {len(batch)} 个KOL候选"
                      f"（第{_kol_round}轮），触发 DeepSeek", flush=True)
                threading.Thread(target=_run_kol_agent, daemon=True).start()

            # === KOL 认知快照 Agent 独立定时触发 ===
            if (ts_settings.get("kol_snapshot_enabled", True) and _kol_snapshot_collected
                    and not _kol_snapshot_running and now - _kol_snapshot_last_flush >= kol_snapshot_interval_seconds):
                batch = list(_kol_snapshot_collected.values())
                with storage.get_conn(config.SNAPSHOT_DB) as conn:
                    storage.kol_candidates_insert_batch(conn, _kol_snapshot_round + 1, batch)
                    storage.kol_candidates_purge_old(conn, keep_last_rounds=30)
                _kol_snapshot_last_flush = now
                _kol_snapshot_running = True
                _kol_snapshot_round += 1
                _kol_snapshot_collected.clear()
                ts_str = datetime.now().strftime("%H:%M:%S")
                print(f"[kol-snapshot-collect] {ts_str} 入库 {len(batch)} 个候选"
                      f"（第{_kol_snapshot_round}轮），触发 DeepSeek", flush=True)
                threading.Thread(target=_run_kol_snapshot_agent, daemon=True).start()

            # === 无教训版 Agent 独立定时触发 ===
            if (ts_settings.get("nl_agent_enabled", True) and _nl_collected
                    and not _nl_running and now - _nl_last_flush >= nl_interval_seconds):
                batch = list(_nl_collected.values())
                with storage.get_conn(config.NL_DB) as conn:
                    storage.nl_candidates_insert_batch(conn, _nl_round + 1, batch)
                    storage.nl_candidates_purge_old(conn, keep_last_rounds=30)
                _nl_last_flush = now
                _nl_running = True
                _nl_round += 1
                _nl_collected.clear()
                ts_str = datetime.now().strftime("%H:%M:%S")
                print(f"[nl-collect] {ts_str} 入库 {len(batch)} 个NL候选"
                      f"（第{_nl_round}轮），触发无教训版 Agent", flush=True)
                threading.Thread(target=_run_nl_agent, daemon=True).start()

            # === 做空版 Agent 独立定时触发 ===
            if (ts_settings.get("agent_short_enabled", True) and _short_collected
                    and not _short_running and now - _short_last_flush >= short_interval_seconds):
                batch = list(_short_collected.values())
                with storage.get_conn(config.AGENT_SHORT_DB) as conn:
                    storage.agent_short_candidates_insert_batch(conn, _short_round + 1, batch)
                    storage.agent_short_candidates_purge_old(conn, keep_last_rounds=30)
                _short_last_flush = now
                _short_running = True
                _short_round += 1
                _short_collected.clear()
                ts_str = datetime.now().strftime("%H:%M:%S")
                print(f"[short-collect] {ts_str} 入库 {len(batch)} 个做空候选"
                      f"（第{_short_round}轮），触发做空版 Agent", flush=True)
                threading.Thread(target=_run_short_agent, daemon=True).start()

        except Exception:
            import traceback
            traceback.print_exc()


# 启动 collector（从 DB 读数据源，fallback config）
# collector 无条件启动
_collector_thread = threading.Thread(target=_collect_loop, daemon=True)
_collector_thread.start()


class TokenBody(BaseModel):
    token: str


class TradingSettingsBody(BaseModel):
    enabled: bool | None = None
    mode: str | None = None
    initial_balance: float | None = None
    leverage: int | None = None
    order_amount: float | None = None
    agent_collect_interval_minutes: int | None = None
    agent_trigger_interval: int | None = None
    strategy_initial_agent: float | None = None
    strategy_initial_heat_agent: float | None = None
    strategy_initial_agent_short: float | None = None
    strategy_initial_system: float | None = None
    strategy_initial_manual: float | None = None
    kol_agent_interval_minutes: int | None = None
    nl_agent_interval_minutes: int | None = None
    agent_short_interval_minutes: int | None = None
    heat_agent_lessons_trigger_interval: int | None = None
    ai_regime_interval_minutes: int | None = None
    kol_llm_provider: str | None = None
    strategy_initial_kol_agent: float | None = None
    strategy_initial_agent_no_lessons: float | None = None
    strategy_initial_heat_agent_lessons: float | None = None
    agent_trade_enabled: bool | None = None
    heat_agent_enabled: bool | None = None
    agent_short_enabled: bool | None = None
    kol_agent_enabled: bool | None = None
    nl_agent_enabled: bool | None = None
    heat_agent_lessons_enabled: bool | None = None
    trading_daily_limit_enabled: bool | None = None
    kol_agent_min_confidence: int | None = None
    kol_token_cooldown_minutes: int | None = None
    limit_order_timeout_seconds: int | None = None
    system_auto_trade_enabled: bool | None = None
    kol_snapshot_enabled: bool | None = None
    kol_snapshot_interval_minutes: int | None = None
    strategy_initial_kol_snapshot: float | None = None
    kol_snapshot_min_confidence: int | None = None
    kol_snapshot_llm_provider: str | None = None
    okx_data_source_enabled: bool | None = None
    onchain_strategy_enabled: bool | None = None
    onchain_scan_enabled: bool | None = None
    strategy_initial_onchain: float | None = None
    onchain_scan_interval_hours: float | None = None
    onchain_divergence_fund_source_min: float | None = None
    onchain_surface_top10_max: float | None = None
    onchain_observation_max_days: float | None = None
    onchain_cluster_enabled: bool | None = None
    onchain_cluster_analysis_enabled: bool | None = None
    onchain_cluster_analysis_interval: int | None = None
    onchain_cluster_top_addresses: int | None = None
    onchain_cluster_min_holding_pct: float | None = None
    onchain_cluster_keep_rounds: int | None = None
    onchain_live_mode: str | None = None


class TradingResetBody(BaseModel):
    confirm: bool = False                    # 必须为 True 才执行，防误触
    new_initial_balance: float | None = None  # 可选：顺便改初始金额


class TradingResetStrategyBody(BaseModel):
    strategy: str
    confirm: bool = False
    new_initial_balance: float | None = None


def _load_snapshot(conn, token: str) -> dict | None:
    row = storage.snapshot_get(conn, token)
    if not row:
        return None
    return {
        "token": row["token"],
        "snapshot": json.loads(row["snapshot"]) if row["snapshot"] else {},
        "analysis": json.loads(row["analysis"]) if row["analysis"] else {},
        "updated_at": row["updated_at"],
    }


def _snapshot_is_stale(snap_row: dict | None, ttl_seconds: int) -> bool:
    if not snap_row or not snap_row.get("updated_at"):
        return True
    raw = str(snap_row["updated_at"]).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return True
    return (datetime.now(timezone.utc) - dt).total_seconds() >= ttl_seconds


def _refresh_watchlist_tokens(tokens: list[str]) -> dict:
    """刷新观察列表合约快照，并同步更新锚定后的浮盈浮亏追踪。"""
    if not tokens:
        return {"refreshed": 0, "skipped_no_contract": 0, "tokens": []}

    with storage.get_conn() as conn:
        short_scores = get_current_heat_scores(conn)
        social_map = {s["token"]: s["score"] for s in short_scores}

    futures_set = get_futures_symbols()
    refreshed = 0
    skipped = 0
    for token in tokens:
        up = token.upper()
        if up not in futures_set:
            skipped += 1
            continue
        try:
            snap = get_market_snapshot(up, heavy=True)
        except Exception:
            continue
        if not snap:
            continue

        # OI/市值 杠杆率
        if snap.get("market_cap_usd") and snap.get("oi_usd") and snap["market_cap_usd"] > 0:
            snap["oi_marketcap_ratio"] = snap["oi_usd"] / snap["market_cap_usd"] * 100

        analysis = analyze_signals(snap, social_map.get(up, 0.0))
        snap_json = json.dumps(snap, default=str, ensure_ascii=False)
        ana_json = json.dumps(analysis, default=str, ensure_ascii=False)
        price = snap.get("mark_price") or 0

        with storage.get_conn() as conn:
            storage.snapshot_upsert(conn, up, snap_json, ana_json)
            entry = storage.entry_get(conn, up)
            if entry is None and price > 0:
                storage.entry_upsert(conn, up, price, snap_json, ana_json)
            elif entry is not None:
                anchor = entry.get("anchor_price") or 0
                if price > 0 and anchor > 0:
                    pnl = (price - anchor) / anchor * 100
                    storage.followup_add(conn, up, price, pnl, snap_json, ana_json)
                    storage.entry_update_extremes(conn, up, pnl)
                    if pnl <= config.LOSS_ARCHIVE_THRESHOLD_PCT and not entry.get("archived"):
                        storage.archive_loss_sample(conn, up, price, pnl)
        refreshed += 1
        time.sleep(0.3)

    return {"refreshed": refreshed, "skipped_no_contract": skipped, "tokens": tokens}


# verdict 的显示优先级（越靠前越靠上）
# 来源：signals.py 里生成的字符串，带 emoji
VERDICT_ORDER = {
    "✅ 看起来健康": 0,
    "🎯 值得留意": 1,
    "⚠ 过热预警": 2,
    "📉 信号偏弱": 3,
    "⚪ 中性": 4,
    "数据不足": 5,
}


def _verdict_rank(verdict: str) -> int:
    """未知 verdict 排到最后"""
    return VERDICT_ORDER.get(verdict, 99)


def _build_leaderboard_items(conn) -> tuple[list[dict], int]:
    raw_scores = get_current_heat_scores(conn)
    # 综合热度增强：加 composite_score, trend, prev_score 等
    scored = compute_composite_scores(conn, raw_scores, config.COMPOSITE_HISTORY_WINDOW)

    watchlist = set(storage.watchlist_get_all(conn))
    # 用实时价格覆盖快照的旧 mark_price（快照 ~5min 刷新，realtime ~1s 刷新）
    tokens = [s["token"] for s in scored]
    rt_price = {}
    if tokens:
        ph = ",".join("?" * len(tokens))
        rows = conn.execute(
            f"SELECT token, snapshot FROM market_realtime_cache WHERE token IN ({ph})",
            tokens,
        ).fetchall()
        for r in rows:
            try:
                rt = json.loads(r["snapshot"]) if isinstance(r["snapshot"], str) else r["snapshot"]
                if rt and rt.get("mark_price"):
                    rt_price[r["token"]] = rt["mark_price"]
            except Exception:
                pass

    pool = []
    skipped_no_contract = 0
    for s in scored:
        snap_row = _load_snapshot(conn, s["token"])
        if not snap_row or not (snap_row.get("snapshot") or {}).get("mark_price"):
            skipped_no_contract += 1
            continue
        # 实时价格覆盖快照旧价
        if s["token"] in rt_price:
            snap_row["snapshot"]["mark_price"] = rt_price[s["token"]]
        pool.append({
            "token": s["token"],
            "score": round(s["score"], 1),
            "composite_score": s["composite_score"],
            "trend": s["trend"],
            "prev_score": s["prev_score"],
            "avg_history_score": s["avg_history_score"],
            "peak_history_score": s["peak_history_score"],
            "appeared_rounds": s["appeared_rounds"],
            "mentions": s["mentions"],
            "unique_posts": s["unique_posts"],
            "unique_authors": s.get("unique_authors", 0),
            "raw_score": s.get("raw_score", round(s["score"], 1)),
            "author_capped_posts": s.get("author_capped_posts", 0),
            "similar_posts": s.get("similar_posts", 0),
            "total_likes": s["total_likes"],
            "total_comments": s["total_comments"],
            "total_shares": s["total_shares"],
            "in_watchlist": s["token"] in watchlist,
            "market": snap_row,
            "score_row": s,
        })

    # 排序：verdict 优先 → 综合热度降序 → 当前热度降序
    def sort_key(item):
        ana = (item["market"].get("analysis") or {})
        verdict = ana.get("verdict", "")
        return (
            _verdict_rank(verdict),
            -item["composite_score"],
            -item["score"],
        )

    pool.sort(key=sort_key)
    return pool[:config.COMPOSITE_HEAT_TOP_N], skipped_no_contract


@app.get("/api/leaderboard")
def api_leaderboard():
    """15 分钟综合热度榜
    - 基于历史若干轮热度的加权综合分排序
    - 只保留有合约快照的代币
    - 同档位 verdict 优先级排序
    - 每个代币带趋势标记（↑↑/↑/—/↓/↓↓/🆕）

    性能：2 秒缓存，避免前端频繁刷新时每次都重算
    """
    def compute():
        with storage.get_conn() as conn:
            result, skipped_no_contract = _build_leaderboard_items(conn)
        return {
            "updated_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
            "items": result,
            "skipped_no_contract": skipped_no_contract,
        }
    return _cached("leaderboard", 2.0, compute)


@app.get("/api/watchlist")
def api_watchlist():
    """观察列表 + 每个代币的合约数据 + 锚定信息 + 浮盈浮亏"""
    with storage.get_conn() as conn:
        tokens = storage.watchlist_get_all(conn)
        ttl = getattr(config, "WATCHLIST_REALTIME_REFRESH_SECONDS",
                      config.WATCHLIST_REFRESH_SECONDS)
        stale_tokens = [
            t for t in tokens
            if _snapshot_is_stale(_load_snapshot(conn, t), ttl)
        ]

    if stale_tokens:
        _refresh_watchlist_tokens(stale_tokens)

    with storage.get_conn() as conn:
        scores = get_current_heat_scores(conn)
        score_map = {s["token"]: s for s in scores}

        items = []
        for token in tokens:
            snap_row = _load_snapshot(conn, token)
            social = score_map.get(token)
            entry = storage.entry_get(conn, token)

            cur_price = None
            if snap_row and snap_row.get("snapshot"):
                cur_price = (snap_row["snapshot"] or {}).get("mark_price")

            # 计算当前浮盈浮亏
            pnl_pct = None
            anchor_price = None
            if entry:
                anchor_price = entry.get("anchor_price")
                if cur_price and anchor_price and anchor_price > 0:
                    pnl_pct = round((cur_price - anchor_price) / anchor_price * 100, 2)

            items.append({
                "token": token,
                "social": social,
                "market": snap_row,
                "anchor_price": anchor_price,
                "anchored_at": entry.get("anchored_at") if entry else None,
                "current_price": cur_price,
                "pnl_pct": pnl_pct,
                "max_drawdown": entry.get("max_drawdown") if entry else None,
                "peak_profit": entry.get("peak_profit") if entry else None,
                "archived": bool(entry.get("archived")) if entry else False,
            })
    return {"items": items}


@app.post("/api/watchlist/add")
def api_watchlist_add(body: TokenBody):
    """收藏代币，用当前快照建锚定。不再自动开仓。"""
    token = body.token.strip().upper()
    if not token:
        raise HTTPException(400, "token required")
    with storage.get_conn() as conn:
        storage.watchlist_add(conn, token)
        snap_row = _load_snapshot(conn, token)
        if snap_row and snap_row.get("snapshot"):
            snap = snap_row["snapshot"]
            price = snap.get("mark_price") if isinstance(snap, dict) else None
            if price and price > 0:
                storage.entry_upsert(
                    conn, token, price,
                    json.dumps(snap_row.get("snapshot"), default=str, ensure_ascii=False),
                    json.dumps(snap_row.get("analysis"), default=str, ensure_ascii=False),
                )
    _cache_invalidate()
    return {"ok": True, "token": token}


@app.post("/api/watchlist/remove")
def api_watchlist_remove(body: TokenBody):
    token = body.token.strip().upper()
    with storage.get_conn() as conn:
        storage.watchlist_remove(conn, token)
        storage.entry_delete(conn, token)
    _cache_invalidate()
    return {"ok": True, "token": token}


@app.post("/api/trade/market-close")
def api_trade_market_close(body: TokenBody):
    """按市价平仓"""
    token = body.token.strip().upper()
    if not token:
        raise HTTPException(400, "token required")
    with storage.get_conn() as conn:
        if not storage.trade_has_active(conn, token):
            raise HTTPException(400, f"{token} 无活跃持仓")
        onchain_active = conn.execute(
            "SELECT 1 FROM trade_positions WHERE token=? AND strategy='onchain' AND status IN ('OPEN','PARTIAL','PENDING')",
            (token,),
        ).fetchone()
        live_row = conn.execute(
            "SELECT value FROM trading_settings WHERE key='onchain_live_mode'"
        ).fetchone()
        result = trade_logic.manual_close_on_unwatch(conn, token)
    if result.get("ok") and onchain_active:
        try:
            with storage.get_conn(config.ONCHAIN_DB) as oc:
                storage.onchain_pool_archive(oc, token, "closed")
                storage.onchain_removed_add(oc, token, "closed")
            if live_row and live_row[0] == "live":
                import exchange_bridge
                exchange_bridge.close_live_position(token)
        except Exception:
            pass
    _cache_invalidate()
    return result


@app.post("/api/trade/market-open")
def api_trade_market_open(body: TokenBody):
    """按市价开仓"""
    token = body.token.strip().upper()
    if not token:
        raise HTTPException(400, "token required")
    with storage.get_conn() as conn:
        settings = storage.trading_settings_get(conn)
        if not settings.get("enabled"):
            raise HTTPException(400, "交易未开启")
        result = trade_logic.manual_open_on_watch(conn, token, settings)
    _cache_invalidate()
    return result


@app.post("/api/watchlist/refresh")
def api_watchlist_refresh():
    """同步刷新观察列表所有代币的合约数据（直接调币安公开 API）
    这和 worker 写入的是同一张表，刷新后前端拿到的是最新数据
    """
    with storage.get_conn() as conn:
        tokens = storage.watchlist_get_all(conn)
        short_scores = get_current_heat_scores(conn)
        social_map = {s["token"]: s["score"] for s in short_scores}

    if not tokens:
        return {"ok": True, "refreshed": 0, "skipped_no_contract": 0, "tokens": []}

    try:
        result = _refresh_watchlist_tokens(tokens)
    except Exception as e:
        raise HTTPException(503, f"刷新观察列表合约数据失败: {e}")
    return {"ok": True, **result}


@app.get("/api/loss_samples")
def api_loss_samples():
    """已归档的负面样本统计（供学习参考）"""
    with storage.get_conn() as conn:
        stats = storage.loss_samples_stats(conn)
    return stats


@app.get("/api/status")
def api_status():
    """Worker 的当前状态（供前端进度面板显示）"""
    with storage.get_conn() as conn:
        s = storage.status_get(conn)
    if not s:
        return {
            "stage": "unknown",
            "detail": "Worker 尚未运行，请在另一个终端运行 python worker.py",
            "running": False,
        }
    # 判断心跳是否近期（>60s 视为掉线）
    last = s.get("last_heartbeat")
    running = False
    if last:
        try:
            # SQLite CURRENT_TIMESTAMP 是 UTC，格式 "YYYY-MM-DD HH:MM:SS"
            last_dt = datetime.fromisoformat(last).replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - last_dt).total_seconds()
            running = age < 60
            s["heartbeat_age_seconds"] = round(age)
        except Exception:
            pass
    s["running"] = running
    s["round_duration_seconds"] = config.SCRAPE_ROUND_SECONDS
    return s


@app.get("/api/trading")
def api_trading():
    """交易面板：账户、持仓、候选信号。默认模拟交易。

    性能：2 秒缓存，前端频繁轮询不会每次都重算 candidates
    """
    def compute():
        with storage.get_conn() as conn:
            account = trade_logic.account_summary(conn)
            positions = storage.trade_positions_with_kol_enrichment(conn)
            candidates = _cached("candidates_scan", ttl, _scan_candidates)
        short_candidates = _cached("short_candidates_scan", ttl, _scan_short_candidates)
        kol_candidates = _cached("kol_display_scan", ttl, _scan_kol_display)
        return {
            "account": account,
            "positions": positions,
            "candidates": candidates,
            "short_candidates": short_candidates,
            "kol_candidates": kol_candidates,
        }
    ttl = getattr(config, "AGENT_COLLECT_CACHE_TTL", 5)
    return _cached("trading", 2.0, compute)


@app.get("/api/settings")
def api_settings():
    with storage.get_conn() as conn:
        return storage.trading_settings_get(conn)


@app.post("/api/trading/settings")
def api_trading_settings(body: TradingSettingsBody):
    fields = {}
    for key in ("enabled", "mode", "initial_balance", "leverage", "order_amount", "agent_collect_interval_minutes", "agent_trigger_interval", "strategy_initial_agent", "strategy_initial_heat_agent", "strategy_initial_heat_agent_lessons", "strategy_initial_system", "strategy_initial_manual", "strategy_initial_kol_agent", "strategy_initial_agent_no_lessons", "strategy_initial_agent_short", "kol_agent_interval_minutes", "nl_agent_interval_minutes", "agent_short_interval_minutes", "heat_agent_lessons_trigger_interval", "ai_regime_interval_minutes", "kol_llm_provider", "kol_agent_min_confidence", "kol_token_cooldown_minutes", "agent_trade_enabled", "heat_agent_enabled", "kol_agent_enabled", "nl_agent_enabled", "agent_short_enabled", "heat_agent_lessons_enabled", "trading_daily_limit_enabled", "system_auto_trade_enabled", "kol_snapshot_enabled", "kol_snapshot_interval_minutes", "strategy_initial_kol_snapshot", "kol_snapshot_min_confidence", "kol_snapshot_llm_provider", "limit_order_timeout_seconds", "okx_data_source_enabled", "onchain_strategy_enabled", "onchain_scan_enabled", "strategy_initial_onchain", "onchain_scan_interval_hours", "onchain_divergence_fund_source_min", "onchain_surface_top10_max", "onchain_observation_max_days", "onchain_cluster_enabled", "onchain_cluster_analysis_enabled", "onchain_cluster_analysis_interval", "onchain_cluster_top_addresses", "onchain_cluster_min_holding_pct", "onchain_cluster_keep_rounds", "onchain_live_mode"):
        value = getattr(body, key)
        if value is not None:
            fields[key] = value
    if "mode" in fields and fields["mode"] not in {"paper", "live"}:
        raise HTTPException(400, "mode must be paper or live")
    if "leverage" in fields and fields["leverage"] <= 0:
        raise HTTPException(400, "leverage must be positive")
    if "order_amount" in fields and fields["order_amount"] <= 0:
        raise HTTPException(400, "order_amount must be positive")
    if "initial_balance" in fields and fields["initial_balance"] <= 0:
        raise HTTPException(400, "initial_balance must be positive")
    with storage.get_conn() as conn:
        storage.trading_settings_update(conn, fields)
        settings = storage.trading_settings_get(conn)
    _cache_invalidate("trading")
    return {"ok": True, "settings": settings}


@app.post("/api/trading/reset")
def api_trading_reset(body: TradingResetBody):
    """
    一键重置交易数据：清空所有持仓、信号锁、止损归档。
    可选地同时更新初始金额。配置（enabled/mode/leverage 等）保留。

    安全：前端必须显式传 confirm=true 才会执行。
    """
    if not body.confirm:
        raise HTTPException(400, "需要 confirm=true 以确认重置")
    if body.new_initial_balance is not None and body.new_initial_balance <= 0:
        raise HTTPException(400, "new_initial_balance 必须为正数")

    with storage.get_conn() as conn:
        result = storage.trade_reset_all(conn, body.new_initial_balance)
    _cache_invalidate()  # 全清，立即看到空状态
    return {"ok": True, **result}


# ---- 链上监控 API ----

@app.get("/api/onchain/status")
def api_onchain_status():
    with storage.get_conn(config.ONCHAIN_DB) as conn:
        st = storage.onchain_status_get(conn)
    if not st:
        return {"stage": "idle", "round_number": 0, "tokens_scanned": 0,
                "candidates_found": 0, "pool_size": 0, "actionable_size": 0,
                "detail": "等待首次扫描"}
    return {k: (str(v) if isinstance(v, datetime) else v) for k, v in (st or {}).items()}


# ---- 链上监控黑名单 ----

@app.get("/api/onchain/blacklist")
def api_onchain_blacklist():
    with storage.get_conn(config.ONCHAIN_DB) as conn:
        rows = storage.onchain_blacklist_get_all(conn)
    for r in rows:
        for k, v in r.items():
            if isinstance(v, datetime):
                r[k] = str(v)
    return rows


@app.post("/api/onchain/blacklist/add")
def api_onchain_blacklist_add(body: dict):
    token = (body.get("token") or "").strip().upper()
    if not token:
        raise HTTPException(400, "缺少 token")
    with storage.get_conn(config.ONCHAIN_DB) as conn:
        storage.onchain_blacklist_add(conn, token)
        storage.onchain_pool_archive(conn, token, "blacklist")
    return {"ok": True, "token": token}


@app.post("/api/onchain/blacklist/remove")
def api_onchain_blacklist_remove(body: dict):
    token = (body.get("token") or "").strip().upper()
    if not token:
        raise HTTPException(400, "缺少 token")
    with storage.get_conn(config.ONCHAIN_DB) as conn:
        storage.onchain_blacklist_remove(conn, token)
    return {"ok": True, "token": token}


# ---- 链上监控移除名单 ----

@app.get("/api/onchain/removed")
def api_onchain_removed():
    with storage.get_conn(config.ONCHAIN_DB) as conn:
        rows = storage.onchain_removed_get_all(conn)
    for r in rows:
        for k, v in r.items():
            if isinstance(v, datetime):
                r[k] = str(v)
    return rows


@app.post("/api/onchain/removed/add")
def api_onchain_removed_add(body: dict):
    token = (body.get("token") or "").strip().upper()
    reason = body.get("reason", "manual")
    if not token:
        raise HTTPException(400, "缺少 token")
    with storage.get_conn(config.ONCHAIN_DB) as conn:
        storage.onchain_removed_add(conn, token, reason)
        storage.onchain_pool_archive(conn, token, reason)
    return {"ok": True, "token": token}


# ---- 链上实盘监控 ----

@app.get("/api/live-onchain/overview")
def api_live_onchain_overview():
    try:
        bal = 0.0
        try:
            import exchange
            bal = exchange.get_balance()
        except Exception:
            pass
        with storage.get_conn(config.LIVE_DB_PATH) as lc:
            positions = storage.live_positions_open(lc, "onchain")
    except Exception:
        return {"equity": 0, "available_balance": 0, "margin_used": 0,
                "unrealized_pnl": 0, "open_count": 0}

    margin_used = 0.0
    unrealized = 0.0
    try:
        import exchange
        for p in exchange.get_positions():
            margin_used += abs(float(p.get("quantity", 0))) * float(p.get("mark_price", 0))
            unrealized += float(p.get("unrealized_pnl", 0))
    except Exception:
        pass

    return {
        "equity": round(bal, 2),
        "available_balance": round(bal - margin_used, 2),
        "margin_used": round(margin_used, 2),
        "unrealized_pnl": round(unrealized, 2),
        "open_count": len(positions),
    }


@app.get("/api/live-onchain/positions")
def api_live_onchain_positions():
    try:
        with storage.get_conn(config.LIVE_DB_PATH) as lc:
            rows = storage.live_positions_open(lc, "onchain")
    except Exception:
        return []
    try:
        import exchange
        ex_map = {p["symbol"]: p for p in exchange.get_positions()}
    except Exception:
        ex_map = {}
    result = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = str(v + timedelta(hours=8))
        ex = ex_map.get(d.get("symbol", ""), {})
        d["mark_price"] = ex.get("mark_price")
        d["ex_unrealized_pnl"] = ex.get("unrealized_pnl")
        d["ex_quantity"] = ex.get("quantity")
        result.append(d)
    return result


@app.get("/api/live-onchain/trades")
def api_live_onchain_trades(token: str = "", limit: int = 30):
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        if token:
            rows = storage.live_trade_log_for_token(lc, token.upper(), limit)
        else:
            rows = lc.execute(
                "SELECT * FROM live_trade_log ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            rows = [dict(r) for r in rows]
    for r in rows:
        for k, v in r.items():
            if isinstance(v, datetime):
                r[k] = str(v + timedelta(hours=8))
    return rows

@app.get("/api/onchain/pool")
def api_onchain_pool(stage: str = None):
    with storage.get_conn(config.ONCHAIN_DB) as conn:
        rows = storage.onchain_pool_get_all(conn, stage)
    now = datetime.now(timezone.utc)
    for r in rows:
        for k, v in r.items():
            if isinstance(v, datetime):
                r[k] = str(v)
        # 从 first_seen_at 实时计算 days_in_pool
        first_seen = r.get("first_seen_at")
        if first_seen:
            try:
                if isinstance(first_seen, str):
                    first_seen = datetime.fromisoformat(first_seen)
                r["days_in_pool"] = round((now - first_seen).total_seconds() / 86400, 1)
            except (ValueError, TypeError):
                pass
        try:
            with storage.get_conn(config.ONCHAIN_DB) as conn:
                snaps = storage.onchain_snapshots_for_token(conn, r["token"], limit=6)
                snaps.reverse()
                r["sparkline"] = [s.get("effective_concentration") for s in snaps]
        except Exception:
            r["sparkline"] = []
    return rows


@app.get("/api/onchain/snapshots")
def api_onchain_snapshots(token: str = "", limit: int = 10):
    if not token:
        return []
    with storage.get_conn(config.ONCHAIN_DB) as conn:
        rows = storage.onchain_snapshots_for_token(conn, token, limit)
    for r in rows:
        for k, v in r.items():
            if isinstance(v, datetime):
                r[k] = str(v)
    return rows


@app.get("/api/onchain/clusters")
def api_onchain_clusters(token: str = ""):
    """返回指定 token 最新一轮的集群列表 + 地址 + 标签"""
    if not token:
        raise HTTPException(400, "缺少 token 参数")
    with storage.get_conn(config.ONCHAIN_DB) as conn:
        clusters = storage.onchain_clusters_for_token(conn, token, limit_rounds=1)
        tags = storage.onchain_cluster_tags_for_token(conn, token, limit_rounds=1)
    result = []
    for c in clusters:
        c_dict = dict(c)
        for k, v in c_dict.items():
            if isinstance(v, datetime):
                c_dict[k] = str(v)
        try:
            c_dict["addresses"] = json.loads(c_dict.get("addresses_json") or "[]")
        except Exception:
            c_dict["addresses"] = []
        c_dict["tags"] = [dict(t) for t in tags if t["cluster_index"] == c["cluster_index"]]
        for t in c_dict["tags"]:
            for k, v in t.items():
                if isinstance(v, datetime):
                    t[k] = str(v)
        result.append(c_dict)
    return result


@app.get("/api/onchain/cluster/portfolio")
def api_onchain_cluster_portfolio(token: str = "", cluster_index: int = 0):
    """返回指定集群的地址持仓数据"""
    if not token:
        raise HTTPException(400, "缺少 token 参数")
    with storage.get_conn(config.ONCHAIN_DB) as conn:
        rows = storage.onchain_address_portfolio_for_token(conn, token)
    result = []
    for r in rows:
        if r["cluster_index"] != cluster_index:
            continue
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = str(v)
        try:
            d["portfolio"] = json.loads(d.get("portfolio_json") or "{}")
            del d["portfolio_json"]
        except Exception:
            d["portfolio"] = {}
        result.append(d)
    return result


@app.get("/api/onchain/cluster/trades")
def api_onchain_cluster_trades(token: str = "", cluster_index: int = 0):
    """返回指定集群的交易记录"""
    if not token:
        raise HTTPException(400, "缺少 token 参数")
    with storage.get_conn(config.ONCHAIN_DB) as conn:
        rows = storage.onchain_cluster_trades_for_token(conn, token)
    result = []
    for r in rows:
        if r["cluster_index"] != cluster_index:
            continue
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = str(v)
        try:
            d["trades"] = json.loads(d.get("trades_json") or "[]")
            del d["trades_json"]
        except Exception:
            d["trades"] = []
        result.append(d)
    return result


@app.get("/api/onchain/cluster/analysis")
def api_onchain_cluster_analysis(token: str = ""):
    """返回指定 token 全部集群的钱包分析数据"""
    if not token:
        raise HTTPException(400, "缺少 token 参数")
    with storage.get_conn(config.ONCHAIN_DB) as conn:
        rows = storage.onchain_wallet_analysis_for_token(conn, token)
    result = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = str(v)
        result.append(d)
    return result


# ---- Alpha 集群查询 API ----

@app.post("/api/alpha/cluster/fetch")
def api_alpha_cluster_fetch(body: dict):
    """按 Token 符号查询集群数据（拉 CLI → 存 DB → 返回）"""
    token = (body.get("token") or "").strip().upper()
    chain = body.get("chain", "bsc")
    if not token:
        raise HTTPException(400, "缺少 token 参数")

    # 1. 查合约地址
    info = search_token_address(token, chain)
    if not info or not info.get("token_address"):
        raise HTTPException(404, f"未在 {chain} 链找到代币 {token}，请确认符号或手动输入合约地址")

    addr = info["token_address"]

    # 2. 拉集群数据
    overview = fetch_cluster_overview(addr, chain) or {}
    cluster_list, _ = fetch_cluster_list(addr, chain)
    if not cluster_list:
        return {"token": token, "token_address": addr, "chain": chain,
                "overview": {"effective_concentration": None, "fund_source_same_pct": None},
                "clusters": [], "analysis": {}}

    # 0. 加载上次集群持仓（用于 holding_change 对比）
    prev_hp_map = {}
    try:
        with storage.get_conn(config.ALPHA_CLUSTER_DB) as c0:
            for r in c0.execute(
                "SELECT cluster_index, holding_percent FROM onchain_clusters WHERE token=?",
                (token,)
            ).fetchall():
                prev_hp_map[r["cluster_index"]] = r["holding_percent"]
    except Exception:
        pass

    # 3. 组装集群 + 钱包分析 + 交易记录
    clusters_out = []
    analysis_map = {}
    cluster_trades_map = {}  # ci → trades_list
    addr_holdings = {}       # address → {symbols}

    for ci, cl in enumerate(cluster_list):
        has_ex = any(a["is_exchange"] for a in cl["addresses"])
        has_ct = any(a["is_contract"] for a in cl["addresses"])
        is_real = 0 if (has_ex or has_ct) else 1
        holding_pct = round((cl["holding_percent"] or 0) * 100, 2)

        real_addrs = [a for a in cl["addresses"]
                     if not a.get("is_exchange") and not a.get("is_contract")][:5]
        for a in real_addrs:
            time.sleep(0.3)
            wa = fetch_wallet_analysis(a["address"], chain)
            if wa:
                perf = wa.get("performance", {}).get("30d", {})
                trades = wa.get("activities", {}).get("trades", [])
                last_trade = trades[0].get("tradeTime") if trades else None
                if last_trade:
                    try:
                        last_trade = datetime.fromtimestamp(int(last_trade) / 1000, timezone.utc).isoformat()
                    except Exception:
                        last_trade = None
                tv_list = wa.get("totalValue") or []
                analysis_map[a["address"]] = {
                    "total_value_usd": float(tv_list[0].get("totalValue", 0)) if tv_list else None,
                    "buy_count_30d": int(perf.get("buyTxCount", 0)),
                    "sell_count_30d": int(perf.get("sellTxCount", 0)),
                    "buy_volume_30d": float(perf.get("buyTxVolume", 0)),
                    "sell_volume_30d": float(perf.get("sellTxVolume", 0)),
                    "avg_buy_value": float(perf.get("avgBuyValueUsd", 0)),
                    "avg_sell_value": round(float(perf.get("sellTxVolume", 0)) / max(int(perf.get("sellTxCount", 1)), 1), 2),
                    "realized_pnl": float(perf.get("realizedPnlUsd", 0)),
                    "win_rate": float(perf.get("winRate", 0)),
                    "last_trade_time": last_trade,
                }
                # 提取持仓（用于交叉对比）
                balances = wa.get("balances") or []
                if balances:
                    tokens_held = set()
                    for ta in balances[0].get("tokenAssets") or []:
                        sym = (ta.get("symbol") or "").upper()
                        if sym:
                            tokens_held.add(sym)
                    if tokens_held:
                        addr_holdings[a["address"]] = tokens_held

        if real_addrs:
            time.sleep(0.3)
            trades_data = fetch_token_trades(addr, chain,
                                             wallet_filter=[a["address"] for a in real_addrs])
        else:
            trades_data = None

        if trades_data:
            cluster_trades_map[ci] = trades_data

        clusters_out.append({
            "cluster_index": ci,
            "holding_percent": holding_pct,
            "trend_type": cl.get("trend_type", []),
            "pnl_percent": cl.get("pnl_percent"),
            "address_count": len(cl["addresses"]),
            "is_real_cluster": is_real,
            "addresses": cl["addresses"],
            "tags": [],
        })

    # 3.5 标签生成
    for ci, cl_out in enumerate(clusters_out):
        cl = cluster_list[ci]
        tags = cl_out["tags"]

        # api 标签
        trend_raw = cl.get("trend_type") or []
        if isinstance(trend_raw, list) and "accumulating" in trend_raw:
            tags.append({"tag_type": "api", "tag_label": "吸筹中"})
        elif isinstance(trend_raw, list) and "distributing" in trend_raw:
            tags.append({"tag_type": "api", "tag_label": "派发中"})
        if cl.get("pnl_percent") is not None:
            pnl = cl["pnl_percent"]
            tags.append({"tag_type": "api", "tag_label": f"集群盈利 +{pnl:.1f}%" if pnl >= 0 else f"集群亏损 {abs(pnl):.1f}%"})

        # holding_change 标签
        prev_hp = prev_hp_map.get(ci)
        if prev_hp is not None and cl_out["is_real_cluster"]:
            hp_change = round(cl_out["holding_percent"] - prev_hp, 2)
            if hp_change > 0.5:
                tags.append({"tag_type": "holding_change", "tag_label": f"增持 +{hp_change}%"})
            elif hp_change < -0.5:
                tags.append({"tag_type": "holding_change", "tag_label": f"减持 {hp_change}%"})
            elif abs(hp_change) <= 0.5:
                tags.append({"tag_type": "holding_change", "tag_label": "持仓稳定"})

        # common_holdings 标签
        real_addrs_ci = [a["address"] for a in cl_out["addresses"]
                        if not a.get("is_exchange") and not a.get("is_contract")]
        holdings_list = [addr_holdings[a] for a in real_addrs_ci if a in addr_holdings]
        if len(holdings_list) >= 2:
            common = set.intersection(*holdings_list)
            common.discard(token)
            if len(common) >= 3:
                tags.append({"tag_type": "common_holdings",
                            "tag_label": f"共持 {len(common)} 个币",
                            "tag_detail": ", ".join(sorted(common)[:8])})

        # trade_behavior 标签
        trades = cluster_trades_map.get(ci, [])
        sells = [t for t in trades if str(t.get("tradeType", "")) == "2"]
        buys = [t for t in trades if str(t.get("tradeType", "")) == "1"]
        if not sells:
            tags.append({"tag_type": "trade_behavior", "tag_label": "近期无卖出"})
        elif len(sells) <= 2:
            tags.append({"tag_type": "trade_behavior", "tag_label": f"少量卖出 ({len(sells)}笔)"})
        else:
            tags.append({"tag_type": "trade_behavior", "tag_label": f"活跃卖出 ({len(sells)}笔)"})
        if buys:
            tags.append({"tag_type": "trade_behavior", "tag_label": f"近期买入 ({len(buys)}笔)"})

    # 4. 写 DB
    eff_conc = 0.0
    for cl in cluster_list:
        hx = any(a["is_exchange"] for a in cl["addresses"])
        ct = any(a["is_contract"] for a in cl["addresses"])
        if not hx and not ct:
            eff_conc += cl["holding_percent"] or 0.0

    try:
        storage.init_alpha_db()
        with storage.get_conn(config.ALPHA_CLUSTER_DB) as conn:
            storage.alpha_clear_token(conn, token)
            for ci, cl in enumerate(cluster_list):
                has_ex = any(a["is_exchange"] for a in cl["addresses"])
                has_ct = any(a["is_contract"] for a in cl["addresses"])
                conn.execute(
                    """INSERT INTO onchain_clusters
                       (round_number, token, token_address, chain, cluster_index,
                        holding_percent, trend_type, pnl_percent, address_count,
                        is_real_cluster, addresses_json)
                       VALUES (0,?,?,?,?,?,?,?,?,?,?)""",
                    (token, addr, chain, ci,
                     round((cl["holding_percent"] or 0) * 100, 2),
                     json.dumps(cl.get("trend_type", [])),
                     cl.get("pnl_percent"),
                     len(cl["addresses"]),
                     0 if (has_ex or has_ct) else 1,
                     json.dumps(cl["addresses"], ensure_ascii=False)))
            # wallet_analysis (正确填写 cluster_index)
            for ci, cl_out in enumerate(clusters_out):
                real_addrs_ci = [a["address"] for a in cl_out["addresses"]
                                if not a.get("is_exchange") and not a.get("is_contract")]
                for a_addr in real_addrs_ci:
                    wa = analysis_map.get(a_addr)
                    if wa:
                        conn.execute(
                            """INSERT INTO onchain_wallet_analysis
                               (round_number, token, cluster_index, address,
                                total_value_usd, buy_count_30d, sell_count_30d,
                                buy_volume_30d, sell_volume_30d, avg_buy_value,
                                avg_sell_value, realized_pnl, win_rate, last_trade_time)
                               VALUES (0,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (token, ci, a_addr,
                             wa["total_value_usd"], wa["buy_count_30d"], wa["sell_count_30d"],
                             wa["buy_volume_30d"], wa["sell_volume_30d"], wa["avg_buy_value"],
                             wa["avg_sell_value"], wa["realized_pnl"], wa["win_rate"],
                             wa["last_trade_time"]))
            # cluster_trades
            for ci, trades_data in cluster_trades_map.items():
                cl_out = clusters_out[ci]
                real_addrs_ci = [a["address"] for a in cl_out["addresses"]
                                if not a.get("is_exchange") and not a.get("is_contract")]
                if real_addrs_ci:
                    conn.execute(
                        """INSERT INTO onchain_cluster_trades
                           (round_number, token, cluster_index, addresses_batch, trades_json)
                           VALUES (0,?,?,?,?)""",
                        (token, ci, ",".join(real_addrs_ci),
                         json.dumps(trades_data, ensure_ascii=False)))
            # tags
            for ci, cl_out in enumerate(clusters_out):
                for t in cl_out.get("tags", []):
                    conn.execute(
                        """INSERT INTO onchain_cluster_tags
                           (round_number, token, cluster_index, tag_type, tag_label, tag_detail)
                           VALUES (0,?,?,?,?,?)""",
                        (token, ci, t["tag_type"], t["tag_label"], t.get("tag_detail")))
    except Exception as e:
        print(f"[alpha] DB 写入失败: {e}")

    overview_out = {
        "token": token,
        "token_address": addr,
        "chain": chain,
        "top10_hold_pct": info.get("top10_hold_pct"),
        "market_cap_usd": info.get("market_cap_usd"),
        "effective_concentration": round(eff_conc * 100, 2) if eff_conc else None,
        "fund_source_same_pct": overview.get("holder_same_fund_pct"),
    }
    return {"overview": overview_out, "clusters": clusters_out, "analysis": analysis_map}


@app.get("/api/alpha/clusters")
def api_alpha_clusters(token: str = ""):
    """从 Alpha DB 读取集群数据（含标签）"""
    if not token:
        raise HTTPException(400, "缺少 token 参数")
    with storage.get_conn(config.ALPHA_CLUSTER_DB) as conn:
        rows = conn.execute(
            "SELECT * FROM onchain_clusters WHERE token=? ORDER BY cluster_index",
            (token.upper(),)
        ).fetchall()
        tag_rows = conn.execute(
            "SELECT * FROM onchain_cluster_tags WHERE token=?",
            (token.upper(),)
        ).fetchall()
    tags_by_ci = {}
    for t in tag_rows:
        td = dict(t)
        tags_by_ci.setdefault(td["cluster_index"], []).append({
            "tag_type": td["tag_type"], "tag_label": td["tag_label"],
            "tag_detail": td.get("tag_detail"),
        })
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["addresses"] = json.loads(d.get("addresses_json") or "[]")
        except Exception:
            d["addresses"] = []
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = str(v)
        d["tags"] = tags_by_ci.get(r["cluster_index"], [])
        result.append(d)
    return result


@app.get("/api/alpha/cluster/analysis")
def api_alpha_cluster_analysis(token: str = ""):
    """从 Alpha DB 读取钱包分析数据"""
    if not token:
        raise HTTPException(400, "缺少 token 参数")
    with storage.get_conn(config.ALPHA_CLUSTER_DB) as conn:
        rows = conn.execute(
            "SELECT * FROM onchain_wallet_analysis WHERE token=?",
            (token.upper(),)
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = str(v)
        result.append(d)
    return result


@app.post("/api/trading/reset-strategy")
def api_trading_reset_strategy(body: TradingResetStrategyBody):
    """按策略重置交易数据"""
    if not body.confirm:
        raise HTTPException(400, "需要 confirm=true 以确认重置")
    strategy = body.strategy.strip().lower()
    valid = {"agent", "heat_agent", "heat_agent_lessons", "agent_no_lessons",
             "kol_agent", "kol_snapshot", "system", "manual", "agent_short", "onchain"}
    if strategy not in valid:
        raise HTTPException(400, f"无效策略: {body.strategy}")
    with storage.get_conn() as conn:
        result = storage.trade_reset_strategy(conn, strategy, body.new_initial_balance)
    _cache_invalidate()
    return {"ok": True, **result}


@app.post("/api/trading/limit-order")
def api_limit_order(body: dict):
    """创建挂单"""
    required = {"token", "side", "limit_price"}
    if not all(k in body for k in required):
        raise HTTPException(400, "缺少必填字段: token/side/limit_price")
    with storage.get_conn() as conn:
        result = trade_logic.open_limit_position(
            conn,
            token=body["token"],
            side=body["side"],
            limit_price=float(body["limit_price"]),
            margin_amount=float(body.get("margin_amount", 50)),
            tier=body.get("tier", "half"),
            strategy=body.get("strategy", "manual"),
            bypass_max_concurrent=True,
            bypass_sector_limit=True,
            bypass_cooldown=True,
        )
    _cache_invalidate()
    return result


@app.post("/api/trading/limit-order/{order_id}/cancel")
def api_limit_order_cancel(order_id: int):
    """取消挂单"""
    with storage.get_conn() as conn:
        pos = conn.execute(
            "SELECT * FROM trade_positions WHERE id=? AND status='PENDING'",
            (order_id,)
        ).fetchone()
        if not pos:
            raise HTTPException(404, "挂单不存在或已成交")
        storage.trade_position_update(conn, order_id, {
            "status": "CANCELED",
            "advice": "手动取消",
            "closed_at": "__CURRENT_TIMESTAMP__",
        })
        storage.trade_signal_lock_release(conn, pos["token"], pos["strategy"])
        if pos["pending_decision_id"]:
            conn.execute("UPDATE pending_decisions SET status = 'expired' WHERE id = ?",
                         (pos["pending_decision_id"],))
    _cache_invalidate()
    return {"ok": True}


@app.post("/api/system/restart")
def api_system_restart():
    """重启所有进程（kill -9 + 等 10min + start）"""
    manage = Path(__file__).resolve().parent / "manage_processes.py"
    subprocess.Popen(
        [sys.executable, str(manage), "restart", "--no-browser"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return {"ok": True, "msg": "系统将在 11 分钟后自动重启"}


# === Agent 监控 API ===

@app.get("/api/agent/overview")
def api_agent_overview(strategy: str = "agent"):
    """Agent 账户概览 + 今日统计"""
    init_key = f"strategy_initial_{strategy}"
    def compute():
        with storage.get_conn() as conn:
            # 账户（策略隔离）
            settings = storage.trading_settings_get(conn)
            initial = float(settings.get(init_key, 1000))
            realized = conn.execute(
                f"SELECT COALESCE(SUM(realized_pnl),0) FROM trade_positions WHERE strategy='{strategy}'"
            ).fetchone()[0]
            unrealized = conn.execute(
                f"SELECT COALESCE(SUM(unrealized_pnl),0) FROM trade_positions "
                f"WHERE status IN ('OPEN','PARTIAL') AND strategy='{strategy}'"
            ).fetchone()[0]
            locked = conn.execute(
                f"SELECT COALESCE(SUM(margin_amount),0) FROM trade_positions "
                f"WHERE status IN ('OPEN','PARTIAL') AND strategy='{strategy}'"
            ).fetchone()[0]
            equity = round(initial + realized + unrealized, 2)
            available = round(initial + realized - locked, 2)

            # 今日统计
            today_opens = conn.execute(
                f"SELECT COUNT(*) FROM trade_positions "
                f"WHERE strategy='{strategy}' AND date(created_at, '+8 hours')=date('now', '+8 hours')"
            ).fetchone()[0]
            today_closes = conn.execute(
                f"SELECT COUNT(*) FROM trade_positions "
                f"WHERE strategy='{strategy}' AND status='CLOSED' AND closed_at IS NOT NULL "
                f"AND date(closed_at, '+8 hours')=date('now', '+8 hours')"
            ).fetchone()[0]
            today_wins = conn.execute(
                f"SELECT COUNT(*) FROM trade_positions "
                f"WHERE strategy='{strategy}' AND status='CLOSED' AND realized_pnl > 0 AND closed_at IS NOT NULL "
                f"AND date(closed_at, '+8 hours')=date('now', '+8 hours')"
            ).fetchone()[0]
            today_losses = conn.execute(
                f"SELECT COUNT(*) FROM trade_positions "
                f"WHERE strategy='{strategy}' AND status='CLOSED' AND realized_pnl <= 0 AND closed_at IS NOT NULL "
                f"AND date(closed_at, '+8 hours')=date('now', '+8 hours')"
            ).fetchone()[0]
            today_pnl = conn.execute(
                f"SELECT COALESCE(SUM(realized_pnl),0) FROM trade_positions "
                f"WHERE strategy='{strategy}' AND status='CLOSED' AND closed_at IS NOT NULL "
                f"AND date(closed_at, '+8 hours')=date('now', '+8 hours')"
            ).fetchone()[0]

            # 持仓数
            open_count = conn.execute(
                f"SELECT COUNT(*) FROM trade_positions "
                f"WHERE status IN ('OPEN','PARTIAL') AND strategy='{strategy}'"
            ).fetchone()[0]

            # 待处理决策（按 source 过滤策略）
            src_map = {"agent": "agent_candidates", "heat_agent": "token_heat_history",
                       "agent_no_lessons": "nl_candidates",
        "agent_short": "agent_candidates_short", "heat_agent_lessons": "token_heat_history_lessons",
                       "kol_agent": "kol_agent",
                       "kol_snapshot": "kol_snapshot"}
            src = src_map.get(strategy, "agent_candidates")
            pending = conn.execute(
                "SELECT COUNT(*) FROM pending_decisions WHERE status='pending' AND source=?",
                (src,)
            ).fetchone()[0]
            rejected_today = conn.execute(
                "SELECT COUNT(*) FROM pending_decisions "
                "WHERE status='rejected' AND source=? AND date(created_at, '+8 hours')=date('now', '+8 hours')",
                (src,)
            ).fetchone()[0]

            # 总体胜率
            total_closed = conn.execute(
                f"SELECT COUNT(*) FROM trade_positions WHERE strategy='{strategy}' AND status='CLOSED'"
            ).fetchone()[0]
            total_wins = conn.execute(
                f"SELECT COUNT(*) FROM trade_positions "
                f"WHERE strategy='{strategy}' AND status='CLOSED' AND realized_pnl > 0"
            ).fetchone()[0]

            # 最大回撤（1h 缓存，按策略隔离）
            global _max_dd_cache
            now = time.time()
            dd_entry = _max_dd_cache.get(strategy)
            if dd_entry and dd_entry.get("time") and now - dd_entry["time"] < 3600:
                max_dd = dd_entry["value"]
            else:
                running = float(initial or 0)
                peak = running
                max_dd = 0.0
                for r in conn.execute(f"SELECT COALESCE(realized_pnl,0) FROM trade_positions WHERE status='CLOSED' AND strategy='{strategy}' ORDER BY closed_at").fetchall():
                    running += float(r[0])
                    if running > peak: peak = running
                    if peak > 0:
                        dd = (running - peak) / peak * 100
                        if dd < max_dd: max_dd = dd
                dd = (running + unrealized - peak) / peak * 100 if peak > 0 else 0
                if dd < max_dd: max_dd = dd
                _max_dd_cache[strategy] = {"value": round(max_dd, 1), "time": time.time()}

        win_rate = round(total_wins / total_closed * 100, 1) if total_closed > 0 else 0
        today_wr = round(today_wins / today_closes * 100, 1) if today_closes > 0 else 0

        # 风险敞口
        try:
            exposure = round(locked / equity * 100, 1) if equity > 0 else 0
        except Exception:
            exposure = 0
        # 总盈亏
        try:
            total_pnl_pct = round((equity - initial) / initial * 100, 1) if initial > 0 else 0
        except Exception:
            total_pnl_pct = 0
        return {
            "equity": equity, "available": available,
            "initial": initial, "realized": round(realized, 2),
            "unrealized": round(unrealized, 2), "locked": round(locked, 2),
            "open_count": open_count,
            "exposure": exposure, "total_pnl_pct": total_pnl_pct,
            "max_drawdown": round(max_dd, 1) if max_dd is not None else 0.0,
            "today_opens": today_opens, "today_closes": today_closes,
            "today_wins": today_wins, "today_losses": today_losses,
            "today_pnl": round(today_pnl, 2), "today_win_rate": today_wr,
            "total_closed": total_closed, "total_wins": total_wins,
            "win_rate": win_rate,
            "pending_decisions": pending, "rejected_today": rejected_today,
        }
    return _cached(f"agent_overview_{strategy}", 15.0, compute)


@app.get("/api/agent/daily-pnl")
def api_agent_daily_pnl(strategy: str = "agent", days: int = 30):
    """每日账户变化：权益线 + 日收益柱。从 trade_positions 实时聚合，无需快照表"""
    with storage.get_conn() as conn:
        settings = storage.trading_settings_get(conn)
        init_key = f"strategy_initial_{strategy}"
        initial = float(settings.get(init_key, 1000))
        # 按天聚合已实现盈亏
        rows = conn.execute(
            f"SELECT date(closed_at, '+8 hours') as day,"
            f"  COALESCE(SUM(realized_pnl),0) as day_rpnl,"
            f"  COUNT(*) as trades,"
            f"  SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins"
            f" FROM trade_positions"
            f" WHERE strategy=? AND status='CLOSED' AND closed_at IS NOT NULL"
            f"  AND closed_at >= datetime('now', ?)"
            f" GROUP BY day ORDER BY day ASC",
            (strategy, f"-{days} days"),
        ).fetchall()
        # 逐日累加 → 每日权益
        daily = []
        running = float(initial)
        for r in rows:
            running += float(r["day_rpnl"])
            daily.append({
                "date": r["day"],
                "equity": round(running, 2),
                "day_pnl": round(float(r["day_rpnl"]), 2),
                "trades": r["trades"],
                "wins": r["wins"],
            })
        # 最新权益 = 最后权益 + 当前浮动盈亏
        unrealized = conn.execute(
            f"SELECT COALESCE(SUM(unrealized_pnl),0) FROM trade_positions"
            f" WHERE status IN ('OPEN','PARTIAL') AND strategy=?",
            (strategy,),
        ).fetchone()[0]
        current_equity = round(running + float(unrealized or 0), 2)
        period_change = round(current_equity - daily[0]["equity"], 2) if daily else 0
    return {
        "strategy": strategy, "initial": initial,
        "current_equity": current_equity,
        "period_change": period_change,
        "unrealized": round(float(unrealized or 0), 2),
        "days": days, "daily": daily,
    }


@app.get("/api/agent/journal")
def api_agent_journal(strategy: str = "agent", limit: int = 5, offset: int = 0):
    """操作日志：策略隔离，按时间倒排，分页"""
    with storage.get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM journal j "
            f"JOIN trade_positions tp ON j.order_id = tp.id "
            f"WHERE tp.strategy = '{strategy}'"
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT j.token, j.action, j.price, j.tier, j.pnl_pct, j.close_reason, "
            f"j.hold_duration, j.reason, j.created_at "
            f"FROM journal j "
            f"JOIN trade_positions tp ON j.order_id = tp.id "
            f"WHERE tp.strategy = '{strategy}' "
            f"ORDER BY j.id DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
    return {
        "journal": [dict(r) for r in rows],
        "has_more": (offset + limit) < total,
    }


@app.get("/api/agent/timeline")
def api_agent_timeline(strategy: str = "agent", limit: int = 30, offset: int = 0):
    """Agent 决策时间线：journal + pending_decisions 合并，按时间倒排，分页"""
    # strategy → pending_decisions source 映射
    source_map = {"agent": "agent_candidates", "heat_agent": "token_heat_history",
                  "agent_no_lessons": "nl_candidates", "heat_agent_lessons": "token_heat_history_lessons",
                  "kol_agent": "kol_agent",
                       "kol_snapshot": "kol_snapshot",
                       "agent_short": "agent_candidates_short",
                       "onchain": "onchain"}
    src = source_map.get(strategy, "agent_candidates")
    with storage.get_conn() as conn:
        journal_total = conn.execute(
            f"SELECT COUNT(*) FROM journal j "
            f"JOIN trade_positions tp ON j.order_id = tp.id "
            f"WHERE tp.strategy = '{strategy}'"
        ).fetchone()[0]
        journal_rows = conn.execute(
            f"SELECT j.id, j.token, j.action, j.price, j.tier, j.stop_loss, "
            f"j.tp1_price, j.tp2_price, j.reason, j.dimension_data, "
            f"j.market_overview, j.lesson_checked, j.pnl_pct, j.close_reason, "
            f"j.hold_duration, j.created_at "
            f"FROM journal j "
            f"JOIN trade_positions tp ON j.order_id = tp.id "
            f"WHERE tp.strategy = '{strategy}' "
            "ORDER BY j.id DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()

        decision_total = conn.execute(
            f"SELECT COUNT(*) FROM pending_decisions WHERE source='{src}'"
        ).fetchone()[0]
        decision_rows = conn.execute(
            f"SELECT id, action, token, tier, entry_price, stop_loss, "
            f"tp1_price, tp2_price, close_reason, reason, market_overview AS market_read, "
            f"status, reject_reason, consumed_at, created_at "
            f"FROM pending_decisions WHERE source='{src}' "
            f"ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()

        # 哪些 pending_decision_id 已有 journal open 记录（精确去重，避免 token 同名误杀）
        j_pd_ids = {r["pending_decision_id"] for r in conn.execute(
            f"SELECT DISTINCT j.pending_decision_id FROM journal j "
            f"JOIN trade_positions tp ON j.order_id = tp.id "
            f"WHERE tp.strategy = '{strategy}' AND j.action = 'open'"
            f" AND j.pending_decision_id IS NOT NULL"
        ).fetchall()}

    timeline = []
    for r in journal_rows:
        r = dict(r)
        r["source"] = "journal"
        timeline.append(r)
    for r in decision_rows:
        r = dict(r)
        # consumed 且已有 journal open（同 pending_decision_id）→ 跳过，避免重复显示
        if r.get("status") == "consumed" and r["id"] in j_pd_ids:
            continue
        r["source"] = "decision"
        timeline.append(r)

    timeline.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    has_more = (offset + limit) < (journal_total + decision_total)
    return {"timeline": timeline[:limit], "has_more": has_more}



@app.get("/api/agent/lessons")
def api_agent_lessons(strategy: str = "agent", limit: int = 20, offset: int = 0, all: int = 0):
    """Agent 教训库 + 统计（策略隔离）。lessons 在 system.db，按 strategy 列过滤"""
    db = config.DB_PATH
    with storage.get_conn(db) as conn:
        if all:
            total = conn.execute(
                f"SELECT COUNT(*) FROM lessons WHERE strategy='{strategy}'"
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM lessons WHERE strategy='{strategy}' ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()
        else:
            total = conn.execute(
                f"SELECT COUNT(*) FROM lessons WHERE learned=0 AND strategy='{strategy}'"
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM lessons WHERE learned=0 AND strategy='{strategy}' ORDER BY severity DESC, created_at DESC LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()
        active = [dict(r) for r in rows]
        stats = storage.lessons_stats(conn, strategy)
    return {"active": active, "stats": stats, "has_more": (offset + limit) < total}


@app.post("/api/agent/lessons/toggle")
def api_agent_lessons_toggle(id: int = 0):
    if not id:
        raise HTTPException(400, "需要 id")
    with storage.get_conn() as conn:
        row = conn.execute("SELECT learned FROM lessons WHERE id=?", (id,)).fetchone()
        if row:
            new_val = 0 if row["learned"] else 1
            conn.execute("UPDATE lessons SET learned=? WHERE id=?", (new_val, id))
            return {"ok": True, "id": id, "learned": bool(new_val)}
    raise HTTPException(404, "不存在")


# === 前端页面 ===

HTML = """
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>Binance Square Monitor</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Inter:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0f1419;
  --panel: #1a1f2e;
  --border: #2a3142;
  --text: #e6e8eb;
  --muted: #8b92a5;
  --accent: #58a6ff;
  --green: #3fb950;
  --red: #f85149;
  --yellow: #d29922;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 20px;
  font-family: 'JetBrains Mono', 'Inter', -apple-system, BlinkMacSystemFont, "Microsoft YaHei", monospace;
  background: var(--bg); color: var(--text);
}
h1, h2 { margin: 0 0 12px; }
h1 { font-size: 22px; color: var(--accent); }
h2 { font-size: 16px; color: var(--accent); margin-top: 24px; }
.updated { color: var(--muted); font-size: 12px; margin-bottom: 16px; }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin-bottom: 16px; }
input, select { border-radius: 8px; }
button { border-radius: 8px; }
table { border-radius: 8px; overflow: hidden; }
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { text-align: left; color: var(--muted); font-weight: 500; padding: 6px 6px; border-bottom: 1px solid var(--border); white-space: nowrap; }
td { padding: 5px 6px; border-bottom: 1px solid #222836; }
tr:hover { background: #1f2536; }
.star { cursor: pointer; color: var(--muted); font-size: 18px; user-select: none; }
.star.active { color: var(--accent); }
.token { font-weight: bold; color: var(--accent); }
.token-link {
  cursor: pointer;
  border-bottom: 1px dashed transparent;
  transition: border-color 0.15s;
}
.token-link:hover {
  border-bottom-color: var(--accent);
  text-decoration: none;
}

/* 跳转后目标卡片高亮 */
@keyframes target-highlight {
  0%   { box-shadow: 0 0 0 3px var(--accent); background: rgba(240, 185, 11, 0.08); }
  100% { box-shadow: 0 0 0 0 transparent; background: transparent; }
}
.deep-card.target-focus {
  animation: target-highlight 2.5s ease-out;
}
.green { color: var(--green); }
.red { color: var(--red); }
.yellow { color: var(--yellow); }
.muted { color: var(--muted); }
.right { text-align: right; }
.verdict { font-size: 12px; white-space: nowrap; }
.tag-list { font-size: 11px; color: var(--muted); margin-top: 4px; }
.notes { font-size: 12px; color: var(--muted); margin-top: 6px; padding-left: 16px; }
.notes li { margin-bottom: 3px; }
.disclaimer {
  background: #1f2335; border: 1px solid #30363d; color: #8b949e;
  padding: 10px 14px; border-radius: 6px; font-size: 12px; margin-bottom: 16px;
}
.empty { color: var(--muted); font-style: italic; padding: 20px; text-align: center; }
.refresh-btn {
  background: var(--accent); color: #000; border: none;
  padding: 6px 14px; border-radius: 4px; cursor: pointer; font-weight: 500;
}
.refresh-btn:hover { opacity: 0.85; }
.refresh-btn:disabled { opacity: 0.5; cursor: wait; }
.refresh-btn.danger-btn {
  background: #da3633; color: #fff;
}
.refresh-btn.danger-btn:hover { background: #e74c3c; }

/* 顶部进度条 */
.progress-bar {
  position: fixed; top: 0; left: 0; right: 0; height: 3px;
  background: transparent; z-index: 1000;
}
.progress-bar .fill {
  height: 100%; background: var(--accent);
  transition: width 0.5s linear;
}
.progress-bar.refreshing .fill {
  background: var(--green);
  animation: refreshing-pulse 0.8s ease-in-out infinite;
}
@keyframes refreshing-pulse {
  0%, 100% { opacity: 0.6; }
  50% { opacity: 1; }
}

/* Toast 提示 */
.toast {
  position: fixed; top: 20px; right: 20px; z-index: 1001;
  background: var(--panel); border: 1px solid var(--accent);
  padding: 10px 16px; border-radius: 6px; font-size: 13px;
  box-shadow: 0 4px 12px rgba(0,0,0,0.5);
  opacity: 0; transform: translateX(20px);
  transition: opacity 0.3s, transform 0.3s;
  pointer-events: none;
}
.toast.show { opacity: 1; transform: translateX(0); }
.toast.ok { border-color: var(--green); }
.toast.err { border-color: var(--red); }

/* 变化的行闪烁高亮 */
@keyframes row-flash {
  0%   { background: rgba(240, 185, 11, 0.3); }
  100% { background: transparent; }
}
tr.flash { animation: row-flash 1.5s ease-out; }
.deep-card.flash { animation: card-flash 1.5s ease-out; }
@keyframes card-flash {
  0%   { box-shadow: 0 0 0 2px var(--accent); }
  100% { box-shadow: 0 0 0 0 transparent; }
}

/* Worker 状态面板 */
.worker-panel {
  background: var(--panel); border: 1px solid var(--border); border-radius: 8px;
  padding: 14px 18px; margin-bottom: 16px;
}
.worker-header {
  display: flex; align-items: center; gap: 12px; margin-bottom: 10px;
  flex-wrap: wrap;
}
.worker-dot {
  width: 10px; height: 10px; border-radius: 50%;
  display: inline-block; background: var(--muted);
}
.worker-dot.running { background: var(--green); animation: pulse-dot 1.5s ease-in-out infinite; }
.worker-dot.stopped { background: var(--red); }
@keyframes pulse-dot {
  0%, 100% { box-shadow: 0 0 0 0 rgba(82, 196, 26, 0.7); }
  50%      { box-shadow: 0 0 0 6px rgba(82, 196, 26, 0); }
}
.worker-title { font-size: 14px; font-weight: 500; }
.worker-stage-badge {
  padding: 2px 8px; border-radius: 3px; font-size: 11px;
  background: #2a3142; color: var(--text);
}
.worker-stage-badge.scraping { background: #1e3a5f; color: #7eb3ff; }
.worker-stage-badge.saving   { background: #3a5f1e; color: #9eff7e; }
.worker-stage-badge.market   { background: #1e3a5f; color: #79c0ff; }
.worker-stage-badge.idle     { background: #2a3142; color: var(--muted); }
.worker-detail { color: var(--text); font-size: 13px; margin-bottom: 8px; }
.worker-progress {
  height: 6px; background: #0a0e15; border-radius: 3px; overflow: hidden;
  margin-bottom: 8px;
}
.worker-progress-fill {
  height: 100%; background: linear-gradient(90deg, var(--accent), #52c41a);
  transition: width 0.5s ease;
}
.worker-stats {
  display: flex; gap: 16px; font-size: 12px; color: var(--muted); flex-wrap: wrap;
}
.worker-stats span strong { color: var(--text); }

/* 趋势箭头 */
.badge-new {
  background: #3a1f5f; color: #c4a0ff;
  padding: 2px 8px; border-radius: 3px; font-size: 11px;
}

/* OI 背离小徽章（表格内）*/
.divergence-badge {
  display: inline-block;
  background: #2a3142; color: var(--text);
  padding: 2px 8px; border-radius: 3px; font-size: 11px;
  cursor: help;
  border-left: 2px solid var(--accent);
}

/* OI 背离大横幅（深度解读卡片内）*/
.divergence-banner {
  display: flex; align-items: center; gap: 12px;
  background: #1e3a5f; border-left: 4px solid #7eb3ff;
  padding: 10px 14px; border-radius: 4px;
  margin: 10px 0;
}
.divergence-banner.oi_distribution {
  background: #3a2f1e; border-left-color: #ffcc7e;
}
.divergence-icon { font-size: 20px; }
.divergence-title { font-weight: bold; font-size: 13px; margin-bottom: 3px; }
.divergence-detail { font-size: 12px; color: var(--muted); }

/* 归档徽章 */
.archived-badge {
  display: inline-block; margin-left: 6px;
  background: #1f2335; color: #8b949e;
  padding: 1px 6px; border-radius: 3px; font-size: 10px;
}
.watch-info { display: flex; gap: 20px; flex-wrap: wrap; font-size: 12px; }
.watch-info div { padding: 4px 8px; background: #141824; border-radius: 4px; }
/* 深度解读卡片 */
.deep-card {
  background: #141824; border: 1px solid var(--border); border-radius: 6px;
  padding: 14px 16px; margin-bottom: 12px;
}
.deep-header {
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  margin-bottom: 10px;
}
.deep-header .token-big { font-size: 18px; font-weight: bold; color: var(--accent); }
.deep-header .verdict-big { font-size: 14px; padding: 3px 10px; background: #0a0e15; border-radius: 4px; }
.deep-header .score-big { font-size: 16px; font-weight: bold; }
.deep-metrics {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 10px; margin-bottom: 10px;
}
.metric { background: #0a0e15; padding: 8px 10px; border-radius: 4px; }
.metric .label { color: var(--muted); font-size: 11px; margin-bottom: 2px; }
.metric .value { font-size: 14px; font-weight: 500; }
.deep-tags { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
.tag-chip {
  background: #2a3142; color: var(--text); padding: 2px 8px;
  border-radius: 3px; font-size: 11px;
}
.deep-notes {
  background: #0a0e15; border-left: 3px solid var(--accent);
  padding: 10px 14px; font-size: 13px; line-height: 1.6;
}
.deep-notes ul { margin: 0; padding-left: 20px; }
.deep-notes li { margin-bottom: 4px; }
.deep-notes .no-notes { color: var(--muted); font-style: italic; }
.trade-controls {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 10px; margin-bottom: 12px;
}
.trade-controls label { color: var(--muted); font-size: 11px; display: block; margin-bottom: 4px; }
.trade-controls input, .trade-controls select {
  width: 100%; background: #0a0e15; color: var(--text);
  border: 1px solid var(--border); border-radius: 4px; padding: 7px 8px;
}
.trade-summary {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  gap: 10px; margin: 12px 0;
}
.trade-summary .metric { min-height: 54px; }
.trade-position-grid {
  display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 14px; align-items: start; margin-top: 12px;
}
.trade-window {
  background: #0a0e15; border: 1px solid var(--border); border-radius: 6px;
  padding: 12px; min-width: 0;
}
.trade-window h3 {
  margin: 0 0 10px; color: var(--accent); font-size: 14px;
}
.closed-summary {
  display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px; margin-bottom: 10px;
}
.closed-summary .metric {
  background: #111722; border: 1px solid var(--border); border-radius: 4px;
  padding: 8px; min-height: 48px;
}
.closed-positions-scroll {
  max-height: 260px;
  overflow-y: auto;
  border-top: 1px solid var(--border);
}
.closed-positions-scroll table { margin-top: 0; }
.closed-positions-scroll thead th {
  position: sticky; top: 0; z-index: 1;
  background: #0a0e15;
}
.compact-table { font-size: 12px; }
.compact-table th, .compact-table td { padding: 7px 6px; }
@media (max-width: 1100px) {
  .trade-position-grid { grid-template-columns: 1fr; }
}
.candidate-list { display: grid; gap: 8px; margin-top: 10px; }
.candidate-item {
  background: #0a0e15; border: 1px solid var(--border); border-radius: 6px;
  padding: 10px 12px; font-size: 12px;
}
.candidate-item.pass { border-left: 3px solid var(--green); }
.candidate-item.wait { border-left: 3px solid var(--yellow); }
.candidate-item.short.pass { border-left: 3px solid var(--red); }
.candidate-item.short.wait { border-left: 3px solid var(--orange); }
.candidate-item.kol { border-left: 3px solid var(--purple); }
.open-btn {
  background: var(--green); color: #000; border: none;
  padding: 2px 8px; border-radius: 3px; cursor: pointer;
  font-size: 11px; font-weight: 500; margin-left: 6px;
}
.open-btn:hover { opacity: 0.8; }
.open-btn:disabled { background: #2a3142; color: var(--muted); cursor: not-allowed; }
.close-btn {
  background: #da3633; color: #fff; border: none;
  padding: 2px 8px; border-radius: 3px; cursor: pointer;
  font-size: 11px; font-weight: 500; margin-left: 6px;
}
.close-btn:hover { opacity: 0.8; }
.cell-advice {
  max-width: 180px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  display: inline-block;
  vertical-align: middle;
  cursor: default;
}
.nav-bar {
  display: flex; justify-content: center; gap: 0; margin-bottom: 16px;
  border-bottom: 1px solid var(--border); padding-bottom: 0;
}
.nav-bar a {
  padding: 10px 24px; color: var(--muted); text-decoration: none;
  font-size: 13px; font-weight: 500; border-bottom: 2px solid transparent;
  transition: all 0.2s;
}
.nav-bar a.active, .nav-bar a:hover { color: var(--accent); border-bottom-color: var(--accent); }
.nav-dropdown { position: relative; }
.nav-dropdown > a { display: inline-block; padding: 10px 24px; color: var(--muted); text-decoration: none; font-size: 13px; font-weight: 500; border-bottom: 2px solid transparent; transition: all 0.2s; }
.nav-dropdown > a:hover { color: var(--accent); border-bottom-color: var(--accent); }
.nav-dropdown-content { display: none; position: absolute; top: 100%; left: 0; background: var(--panel); border: 1px solid var(--border); border-radius: 6px; min-width: 160px; z-index: 100; padding: 4px 0; }
.nav-dropdown-content a { display: block; padding: 8px 16px; color: var(--text); border-bottom: none !important; font-size: 13px; white-space: nowrap; }
.nav-dropdown-content a:hover { background: #1a1f2e; color: var(--accent) !important; }
.nav-dropdown:hover .nav-dropdown-content { display: block; }
</style>
</head>
<body>

<div class="progress-bar" id="progress-bar"><div class="fill" id="progress-fill" style="width:0%"></div></div>
<div class="toast" id="toast"></div>

<div class="nav-bar">
  <a href="/">📊 市场监控</a>
  <a href="/strategies">📈 运行策略</a>
  <div class="nav-dropdown">
  <a href="#">🤖 Agent 面板 ▾</a>
  <div class="nav-dropdown-content">
    <a href="/agent">Agent-合约扫描</a>
    <a href="/heat-agent">Agent-热度榜单</a>
    <a href="/agent-heat-lessons">Agent-热度有教训</a>
    <a href="/agent-nl">Agent-无教训</a>
                <a href="/agent-short">Agent-做空</a>
      <a href="/agent-kol">Agent-KOL 监控</a>
              <a href="/agent-kol-snapshot">Agent-KOL 快照监控</a>
          </div>
</div>
  <a href="/dashboard">📊 市场全景</a>
  <a href="/onchain">🔍 链上监控</a>
  <a href="/alpha-cluster">🔬 Alpha 集群</a>
  <a href="/live-onchain">💹 实盘监控</a>
  <a href="/settings">⚙ 系统设置</a>
</div>

<div style="display:flex;align-items:center;gap:16px;margin-bottom:4px;">
<h1 style="margin:0">🔥 热度监控</h1>
</div>
<div class="updated" id="updated">加载中...</div>

<div class="worker-panel" id="worker-panel">
  <div class="worker-header">
    <span class="worker-dot" id="worker-dot"></span>
    <span class="worker-title">采集 Worker</span>
    <span class="worker-stage-badge" id="worker-stage">加载中</span>
    <span class="muted" style="font-size:12px; margin-left:auto;" id="worker-round">—</span>
  </div>
  <div class="worker-detail" id="worker-detail">等待状态...</div>
  <div class="worker-progress"><div class="worker-progress-fill" id="worker-progress-fill" style="width:0%"></div></div>
  <div class="worker-stats" id="worker-stats"></div>
</div>

<div class="panel">
  <h2 style="margin-top:0">做多建议</h2>
  <div id="trade-candidates"><div class="empty">等待扫描数据...</div></div>

  <h2 style="margin-top:16px">做空建议</h2>
  <div id="short-candidates"><div class="empty">等待扫描数据...</div></div>

  <h2 style="margin-top:16px">综合建议</h2>
  <div id="kol-candidates"><div class="empty">等待扫描数据...</div></div>
</div>

<div class="panel">
  <h2 style="margin-top:0">
    ⭐ 观察列表
    <button class="refresh-btn" onclick="refreshWatchlistMarket()">拉取最新合约数据</button>
    <button class="refresh-btn" style="background:#2a3142;color:var(--text);" onclick="manualRefresh()">重载页面</button>
  </h2>
  <div class="muted" style="font-size:12px;margin-bottom:10px;">
    收藏时自动锚定当前价格 · 每 5 分钟追踪浮盈浮亏 · 浮亏超过阈值自动归档为学习样本
  </div>
  <div id="watchlist"><div class="empty">暂无观察代币。去下方榜单点击 ⭐ 加入。</div></div>
  <div id="loss-samples-stats" class="loss-samples-stats muted" style="margin-top:12px;font-size:12px;"></div>
</div>

<div class="panel">
  <h2 style="margin-top:0">📊 15 分钟热度榜</h2>
  <div style="overflow-x:auto">
  <table>
    <thead>
      <tr>
        <th width="50"></th>
        <th>代币</th>
        <th class="right">综合热度</th>
        <th>趋势</th>
        <th class="right">当前</th>
        <th class="right">帖子</th>
        <th class="right">价格</th>
        <th class="right">15m</th>
        <th class="right">1h</th>
        <th class="right">4h</th>
        <th class="right">费率/8h</th>
        <th class="right">OI 15m</th>
        <th class="right">OI 1h</th>
        <th class="right">OI 4h</th>
        <th class="right">综合</th>
        <th>判断</th>
        <th>走向</th>
        <th>OI 背离</th>
      </tr>
    </thead>
    <tbody id="leaderboard"></tbody>
  </table>
  </div>
  <div id="leaderboard-note" class="muted" style="font-size:12px;margin-top:10px;"></div>
</div>

<div class="panel">
  <h2 style="margin-top:0">🔍 上榜代币合约深度解读</h2>
  <div class="muted" style="font-size:12px;margin-bottom:12px;">
    与榜单同序：看起来健康 → 值得留意 → 过热预警 → 信号偏弱 → 中性 → 数据不足。每个代币展开显示价格走势、合约持仓、多空结构，并给出基于数据的客观观察（不是投资建议）。
  </div>
  <div id="deep-analysis"><div class="empty">等待榜单数据...</div></div>
</div>

<script>
// === 工具 ===
const escHtml = s => s != null ? String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') : '';
function showToast(msg, kind = 'ok', duration = 2500) {
  const el = document.getElementById('toast');
  el.className = 'toast ' + kind;
  el.textContent = msg;
  requestAnimationFrame(() => el.classList.add('show'));
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), duration);
}

// === 进度条：显示下次自动刷新倒计时 ===
const REFRESH_INTERVAL_MS = 30000;
let lastRefreshAt = Date.now();
function tickProgress() {
  const bar = document.getElementById('progress-bar');
  if (bar.classList.contains('refreshing')) return;
  const elapsed = Date.now() - lastRefreshAt;
  const pct = Math.min(100, (elapsed / REFRESH_INTERVAL_MS) * 100);
  document.getElementById('progress-fill').style.width = pct + '%';
}
setInterval(tickProgress, 500);

// === 上一轮快照（用于 diff 出变化，做闪烁动画）===
let prevLeaderboard = {};  // token -> {score, price, in_watchlist}

function buildSnapshotMap(items) {
  const m = {};
  items.forEach(it => {
    m[it.token] = {
      score: it.score,
      price: (it.market && it.market.snapshot && it.market.snapshot.mark_price) || null,
      inWatch: it.in_watchlist,
      verdictScore: (it.market && it.market.analysis && it.market.analysis.score) || null,
    };
  });
  return m;
}

function diffTokens(oldMap, newMap) {
  const changed = new Set();
  const added = new Set();
  Object.keys(newMap).forEach(t => {
    if (!oldMap[t]) {
      added.add(t);
      return;
    }
    const a = oldMap[t], b = newMap[t];
    if (a.score !== b.score || a.price !== b.price || a.verdictScore !== b.verdictScore) {
      changed.add(t);
    }
  });
  return { added, changed };
}

function flashRows(tokens) {
  tokens.forEach(t => {
    document.querySelectorAll(`tr[data-token="${t}"]`).forEach(row => {
      row.classList.remove('flash');
      // 触发 reflow 让动画重新跑
      void row.offsetWidth;
      row.classList.add('flash');
    });
    document.querySelectorAll(`.deep-card[data-token="${t}"]`).forEach(el => {
      el.classList.remove('flash');
      void el.offsetWidth;
      el.classList.add('flash');
    });
  });
}

const fmtPct = (v, invert=false) => {
  if (v === null || v === undefined) return '<span class="muted">-</span>';
  const good = invert ? v < 0 : v > 0;
  const cls = good ? 'green' : (v === 0 ? 'muted' : 'red');
  const sign = v > 0 ? '+' : '';
  return `<span class="${cls}">${sign}${v.toFixed(2)}%</span>`;
};
const fmtFR = (v) => {
  if (v === null || v === undefined) return '<span class="muted">-</span>';
  let cls = '';
  if (v >= 0.05) cls = 'red';
  else if (v <= -0.01) cls = 'yellow';
  const sign = v > 0 ? '+' : '';
  return `<span class="${cls}">${sign}${v.toFixed(3)}%</span>`;
};
const fmtPrice = (v) => v ? v.toPrecision(5) : '-';
const fmtNum = (v, d = 2) => v !== null && v !== undefined ? v.toFixed(d) : '-';

function fmtDirection(d) {
  if (!d) return '<span class="muted">-</span>';
  if (d.indexOf('偏多') >= 0 || d.indexOf('↑') >= 0) return `<span class="green">${d}</span>`;
  if (d.indexOf('偏空') >= 0 || d.indexOf('↓') >= 0) return `<span class="red">${d}</span>`;
  if (d === '震荡') return `<span class="yellow">${d}</span>`;
  return `<span class="muted">${d}</span>`;
}

function fmtTrend(t) {
  if (!t || t === '—') return `<span class="muted">${t || '—'}</span>`;
  if (t === '🆕') return `<span class="badge-new">🆕 新</span>`;
  if (t.indexOf('↑') >= 0) return `<span class="green" style="font-weight:bold;">${t}</span>`;
  if (t.indexOf('↓') >= 0) return `<span class="red" style="font-weight:bold;">${t}</span>`;
  return t;
}

function fmtDivergence(div) {
  if (!div) return '<span class="muted">-</span>';
  const icon = div.type === 'oi_accumulation' ? '🟢' : '🟡';
  return `<span class="divergence-badge" title="${div.note}">${icon} ${div.oi_pct > 0 ? '+' : ''}${div.oi_pct}% / ${div.price_pct > 0 ? '+' : ''}${div.price_pct}%</span>`;
}

function rowFromMarket(item) {
  const m = item.market;
  if (!m) {
    return {
      price: '<span class="muted">无合约</span>',
      ch15m: '-', ch1h: '-', ch4h: '-',
      fr: '-', oi: '-', lsr: '-',
      score: '-', verdict: '<span class="muted">-</span>',
      direction: '<span class="muted">-</span>',
      divergence: '<span class="muted">-</span>',
      divergenceData: null,
      notes: null, tags: null,
    };
  }
  const s = m.snapshot || {};
  const a = m.analysis || {};
  return {
    price: fmtPrice(s.mark_price),
    ch15m: fmtPct(s.change_15m_pct),
    ch1h: fmtPct(s.change_1h_pct),
    ch4h: fmtPct(s.change_4h_pct),
    ch48h: fmtPct(s.change_48h_pct),
    fr: fmtFR(s.funding_rate_pct),
    oi15m: fmtPct(s.oi_change_15m_pct),
    oi: fmtPct(s.oi_change_1h_pct),
    oi4h: fmtPct(s.oi_change_4h_pct),
    oi48: fmtPct(s.oi_change_48h_pct),
    taker: fmtNum(s.taker_buy_sell_ratio),
    spread: fmtPct(s.bid_ask_spread_pct),
    lsr: fmtNum(s.long_short_ratio),
    score: a.score !== undefined ? a.score : '-',
    verdict: a.verdict || '<span class="muted">-</span>',
    direction: fmtDirection(a.direction),
    divergence: fmtDivergence(a.oi_divergence),
    divergenceData: a.oi_divergence || null,
    notes: a.notes || [],
    tags: a.tags || [],
  };
}

function renderDeepAnalysis(items) {
  const el = document.getElementById('deep-analysis');
  if (!items || !items.length) {
    el.innerHTML = '<div class="empty">等待榜单数据...</div>';
    return;
  }
  // 直接沿用榜单的顺序（API 已按 verdict 档位 → 综合分 → 热度排序）
  el.innerHTML = items.map(item => {
    const m = item.market || {};
    const s = m.snapshot || {};
    const a = m.analysis || {};

    const fmtFR2 = (v) => {
      if (v === null || v === undefined) return '-';
      const sign = v > 0 ? '+' : '';
      return sign + v.toFixed(3) + '%';
    };
    const fmtPct2 = (v) => {
      if (v === null || v === undefined) return '-';
      const sign = v > 0 ? '+' : '';
      return sign + v.toFixed(2) + '%';
    };
    const fmtUsd = (v) => {
      if (!v) return '-';
      if (v >= 1e9) return '$' + (v / 1e9).toFixed(2) + 'B';
      if (v >= 1e6) return '$' + (v / 1e6).toFixed(2) + 'M';
      if (v >= 1e3) return '$' + (v / 1e3).toFixed(1) + 'K';
      return '$' + v.toFixed(2);
    };
    const fmtMc = (v) => {
      if (!v) return '-';
      if (v >= 1e12) return '$' + (v/1e12).toFixed(2) + 'T';
      if (v >= 1e9) return '$' + (v/1e9).toFixed(2) + 'B';
      if (v >= 1e6) return '$' + (v/1e6).toFixed(1) + 'M';
      if (v >= 1e3) return '$' + (v/1e3).toFixed(1) + 'K';
      return '$' + v.toFixed(0);
    };
    const fmtSmTooltip = (s) => {
      return '多头均价: ' + (s.sm_long_avg_entry ? fmtPrice(s.sm_long_avg_entry) : '-') +
        ' | 空头均价: ' + (s.sm_short_avg_entry ? fmtPrice(s.sm_short_avg_entry) : '-') +
        ' | 多头胜率: ' + (s.sm_avg_long_win_rate != null ? s.sm_avg_long_win_rate.toFixed(1)+'%' : '-') +
        ' | 空头胜率: ' + (s.sm_avg_short_win_rate != null ? s.sm_avg_short_win_rate.toFixed(1)+'%' : '-');
    };
    const tagsHtml = (a.tags || []).map(t => `<span class="tag-chip">${t}</span>`).join('');
    const notesHtml = (a.notes && a.notes.length)
      ? `<ul>${a.notes.map(n => `<li>${n}</li>`).join('')}</ul>`
      : '<div class="no-notes">（暂无需特别提示的数据特征）</div>';

    return `
      <div class="deep-card" id="card-${item.token}" data-token="${item.token}">
        <div class="deep-header">
          <span class="token-big">${item.token}</span>
          <span class="verdict-big">${a.verdict || '-'}</span>
          <span class="verdict-big">${fmtDirection(a.direction)}</span>
          <span class="score-big">综合 ${a.score !== undefined ? a.score : '-'}</span>
          <span class="muted" style="font-size:12px;">社交热度 ${item.score.toFixed(1)} · ${item.unique_posts} 条帖子</span>
          <span style="margin-left:auto;" class="muted" style="font-size:11px;">
            ${(()=>{if(!m.updated_at)return'更新于 -';const d=new Date(m.updated_at+'Z');d.setHours(d.getHours()+8);return'更新于 '+d.toISOString().replace('T',' ').substring(5,16);})()}
          </span>
        </div>
        <div class="deep-metrics">
          <div class="metric"><div class="label">标记价</div><div class="value">${s.mark_price ? s.mark_price.toPrecision(5) : '-'}</div></div>
          <div class="metric"><div class="label">15m 涨跌</div><div class="value">${fmtPct2(s.change_15m_pct)}</div></div>
          <div class="metric"><div class="label">1h 涨跌</div><div class="value">${fmtPct2(s.change_1h_pct)}</div></div>
          <div class="metric"><div class="label">4h 涨跌</div><div class="value">${fmtPct2(s.change_4h_pct)}</div></div>
          <div class="metric"><div class="label">24h 涨跌</div><div class="value">${fmtPct2(s.change_24h_pct)}</div></div>
          <div class="metric"><div class="label">资金费率/8h</div><div class="value">${fmtFR2(s.funding_rate_pct)}</div></div>
          <div class="metric"><div class="label">未平仓(USD)</div><div class="value">${fmtUsd(s.oi_usd)}</div></div>
          <div class="metric"><div class="label">OI 15m 变化</div><div class="value">${fmtPct2(s.oi_change_15m_pct)}</div></div>
          <div class="metric"><div class="label">OI 1h 变化</div><div class="value">${fmtPct2(s.oi_change_1h_pct)}</div></div>
          <div class="metric"><div class="label">OI 4h 变化</div><div class="value">${fmtPct2(s.oi_change_4h_pct)}</div></div>
          <div class="metric"><div class="label">OI 48h 变化</div><div class="value">${fmtPct2(s.oi_change_48h_pct)}</div></div>
          <div class="metric"><div class="label">48h 涨跌</div><div class="value">${fmtPct2(s.change_48h_pct)}</div></div>
          <div class="metric"><div class="label">主动买/卖比</div><div class="value">${s.taker_buy_sell_ratio ? s.taker_buy_sell_ratio.toFixed(2) : '-'}</div></div>
          <div class="metric"><div class="label">盘口价差</div><div class="value">${fmtPct2(s.bid_ask_spread_pct)}</div></div>
          <div class="metric"><div class="label">1% 买盘深度</div><div class="value">${fmtUsd(s.depth_bid_1pct_usd)}</div></div>
          <div class="metric"><div class="label">1% 卖盘深度</div><div class="value">${fmtUsd(s.depth_ask_1pct_usd)}</div></div>
          <div class="metric"><div class="label">多空比(散户)</div><div class="value">${s.long_short_ratio ? s.long_short_ratio.toFixed(2) : '-'}</div></div>
          <div class="metric"><div class="label">多空比(大户)</div><div class="value">${s.top_trader_ls_ratio ? s.top_trader_ls_ratio.toFixed(2) : '-'}</div></div>
          <div class="metric"><div class="label">24h 成交额</div><div class="value">${fmtUsd(s.volume_24h_usd)}</div></div>
          <div class="metric"><div class="label">vol/OI 比值</div><div class="value">${s.vol_oi_ratio ? s.vol_oi_ratio.toFixed(2) : '-'}</div></div>
          <div class="metric"><div class="label">上线天数</div><div class="value">${s.listing_age_days ? Math.round(s.listing_age_days) + 'd' : '-'}</div></div>
          <div class="metric"><div class="label">品类</div><div class="value" style="font-size:11px">${s.sector||'-'}</div></div>
          <div class="metric"><div class="label">Basis 基差</div><div class="value">${fmtPct2(s.basis_pct)}</div></div>
          <div class="metric"><div class="label">OKX 费率</div><div class="value">${fmtFR2(s.okx_funding_pct)}</div></div>

          <div class="metric"><div class="label">OKX 费率趋势</div><div class="value" style="font-size:11px">${(()=>{const h=s.funding_history;if(!h||!h.length)return'-';return h.slice(-4).map(x=>(x.r>0?'+':'')+x.r.toFixed(4)+'%').join(' → ')})()}</div></div>
          <div class="metric"><div class="label">市值</div><div class="value">${fmtMc(s.market_cap_usd)}${s.market_cap_rank ? ' <span class="muted">#'+s.market_cap_rank+'</span>' : ''}</div></div>
          <div class="metric"><div class="label">OI/市值 (杠杆率)</div><div class="value">${s.oi_marketcap_ratio != null ? s.oi_marketcap_ratio.toFixed(2)+'%' : '-'}${s.oi_marketcap_ratio > 5 ? ' <span style="color:var(--red);font-size:11px">高杠杆</span>' : s.oi_marketcap_ratio > 2 ? ' <span style="color:var(--yellow);font-size:11px">注意</span>' : ''}</div></div>
          <div class="metric"><div class="label">MA20 乖离 (5h)</div><div class="value">${fmtPct2(s.ma20_deviation_pct)}</div></div>
          <div class="metric" title="${fmtSmTooltip(s)}" style="cursor:help"><div class="label">聪明钱 ▾</div><div class="value">多头${fmtPct2(s.sm_long_ratio)} | 净头寸${fmtUsd(s.sm_net_notional_usdt)} | ${s.sm_traders_with_position||0}人</div></div>
        </div>
        ${a.oi_divergence ? `
          <div class="divergence-banner ${a.oi_divergence.type}">
            <span class="divergence-icon">${a.oi_divergence.type === 'oi_accumulation' ? '🟢' : '🟡'}</span>
            <div>
              <div class="divergence-title">OI 背离 · ${a.oi_divergence.direction}</div>
              <div class="divergence-detail">${a.oi_divergence.note}</div>
            </div>
          </div>
        ` : ''}
        ${tagsHtml ? `<div class="deep-tags">${tagsHtml}</div>` : ''}
        <div class="deep-notes">
          <div style="font-size:11px;color:var(--muted);margin-bottom:6px;">数据观察（非投资建议）</div>
          ${notesHtml}
        </div>
      </div>
    `;
  }).join('');
}

async function loadWatchlist() {
  const resp = await fetch('/api/watchlist');
  const data = await resp.json();
  const el = document.getElementById('watchlist');
  if (!data.items.length) {
    el.innerHTML = '<div class="empty">暂无观察代币。去下方榜单点击 ⭐ 加入。</div>';
    return;
  }
  el.innerHTML = '<table><thead><tr>' +
    '<th width="50"></th>' +
    '<th>代币</th>' +
    '<th class="right">锚定价</th>' +
    '<th class="right">当前价</th>' +
    '<th class="right">浮盈/亏</th>' +
    '<th class="right">峰值/回撤</th>' +
    '<th class="right">15m</th>' +
    '<th class="right">1h</th>' +
    '<th class="right">4h</th>' +
    '<th class="right">费率/8h</th>' +
    '<th class="right">OI 1h</th>' +
    '<th>判断</th>' +
    '<th>走向</th>' +
    '<th>OI 背离</th>' +
    '</tr></thead><tbody>' +
    data.items.map(item => {
      const m = rowFromMarket(item);
      const notesHtml = m.notes && m.notes.length
        ? `<ul class="notes">${m.notes.map(n => `<li>${n}</li>`).join('')}</ul>` : '';
      const anchorDisp = item.anchor_price ? fmtPrice(item.anchor_price) : '<span class="muted">-</span>';
      const curDisp = item.current_price ? fmtPrice(item.current_price) : '<span class="muted">-</span>';
      const pnlDisp = item.pnl_pct !== null && item.pnl_pct !== undefined
        ? fmtPct(item.pnl_pct)
        : '<span class="muted">-</span>';
      const peakDisp = (item.peak_profit !== null && item.peak_profit !== undefined)
        ? `<span class="green">+${item.peak_profit.toFixed(1)}%</span> / <span class="red">${item.max_drawdown.toFixed(1)}%</span>`
        : '<span class="muted">-</span>';
      const archivedBadge = item.archived
        ? '<span class="archived-badge" title="已触发负面样本归档">已归档</span>' : '';
      return `
        <tr data-token="${item.token}">
          <td><span class="star active" onclick="toggleWatch('${item.token}', true)" title="移除">★</span></td>
          <td>
            <span class="token token-link" onclick="jumpToCard('${item.token}')">${item.token}</span>
            <button class="open-btn" onclick="openTrade('${item.token}')">开仓</button>
            ${archivedBadge}
            ${notesHtml}
          </td>
          <td class="right">${anchorDisp}</td>
          <td class="right">${curDisp}</td>
          <td class="right"><strong>${pnlDisp}</strong></td>
          <td class="right">${peakDisp}</td>
          <td class="right">${m.ch15m}</td>
          <td class="right">${m.ch1h}</td>
          <td class="right">${m.ch4h}</td>
          <td class="right">${m.fr}</td>
          <td class="right">${m.oi}</td>
          <td class="verdict">${m.verdict}</td>
          <td>${m.direction}</td>
          <td>${m.divergence}</td>
        </tr>
      `;
    }).join('') + '</tbody></table>';
}

// === 点击代币名跳转到对应的深度解读卡片 ===
function jumpToCard(token) {
  const card = document.getElementById('card-' + token);
  if (!card) {
    showToast(`未找到 ${token} 的解读卡片`, 'err', 1500);
    return;
  }
  // 平滑滚动到卡片顶部上方 20px
  const top = card.getBoundingClientRect().top + window.pageYOffset - 20;
  window.scrollTo({ top, behavior: 'smooth' });
  // 触发高亮动画
  card.classList.remove('target-focus');
  void card.offsetWidth;  // 强制重新计算以重启动画
  card.classList.add('target-focus');
}

async function openTrade(token) {
  try {
    const resp = await fetch('/api/trade/market-open', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token}),
    });
    const data = await resp.json();
    if (data.ok) {
      showToast(`${token} 市价开仓 @ ${fmtPrice(data.entry_price)}`, 'ok');
    } else {
      showToast(`${token} 开仓失败：${data.reason || '未知'}`, 'err');
    }
  } catch (e) {
    showToast('操作失败：' + e.message, 'err');
  }
  await refreshAll();
}

async function closeTrade(token) {
  try {
    const resp = await fetch('/api/trade/market-close', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token}),
    });
    const data = await resp.json();
    if (data.ok) {
      showToast(`${token} 已平仓`, 'ok');
    } else {
      showToast(`${token} 平仓失败：${data.reason || '未知'}`, 'err');
    }
  } catch (e) {
    showToast('操作失败：' + e.message, 'err');
  }
  await refreshAll();
}

async function toggleWatch(token, currentlyActive) {
  const url = currentlyActive ? '/api/watchlist/remove' : '/api/watchlist/add';
  document.querySelectorAll(`tr[data-token="${token}"] .star`).forEach(s => {
    s.classList.toggle('active', !currentlyActive);
    s.setAttribute('onclick', `toggleWatch('${token}', ${!currentlyActive})`);
  });
  try {
    const resp = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token}),
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    showToast(currentlyActive ? `已移除 ${token}` : `已收藏 ${token}`, 'ok');
  } catch (e) {
    showToast('操作失败：' + e.message, 'err');
  }
  await refreshAll();
}

async function refreshAll(opts = {}) {
  const { silent = false, manual = false } = opts;
  const bar = document.getElementById('progress-bar');
  const btns = document.querySelectorAll('.refresh-btn');
  bar.classList.add('refreshing');
  document.getElementById('progress-fill').style.width = '100%';
  btns.forEach(b => b.disabled = true);

  try {
    const [lb, _, __] = await Promise.all([
      fetch('/api/leaderboard').then(r => r.json()),
      loadWatchlist(),
      loadLossSamples(),
    ]);

    // 渲染榜单前，先算出哪些代币变化了
    const newMap = buildSnapshotMap(lb.items || []);
    const diff = diffTokens(prevLeaderboard, newMap);

    renderLeaderboard(lb);
    await loadTradingPanel();

    // 触发闪烁（仅对已出现过、这次数值有变的 token）
    const toFlash = new Set([...diff.changed]);
    if (toFlash.size) flashRows(toFlash);

    // toast 提示
    if (manual) {
      showToast('已刷新', 'ok');
    } else if (!silent && Object.keys(prevLeaderboard).length) {
      const addedCount = diff.added.size;
      const changedCount = diff.changed.size;
      if (addedCount || changedCount) {
        const parts = [];
        if (addedCount) parts.push(`${addedCount} 个新上榜`);
        if (changedCount) parts.push(`${changedCount} 个数据更新`);
        showToast(parts.join('，'), 'ok', 2000);
      }
    }

    prevLeaderboard = newMap;
    lastRefreshAt = Date.now();
  } catch (e) {
    showToast('刷新失败：' + e.message, 'err');
  } finally {
    setTimeout(() => {
      bar.classList.remove('refreshing');
      document.getElementById('progress-fill').style.width = '0%';
    }, 400);
    btns.forEach(b => b.disabled = false);
  }
}

// 把 loadLeaderboard 拆成两步：fetch 由 refreshAll 做，渲染单独提出来
function renderLeaderboard(data) {
  let ut='';if(data.updated_at){const d=new Date(data.updated_at+'Z');ut=d.toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).replace(/\//g,'-');}
  document.getElementById('updated').textContent = '最后刷新: ' + ut;
  const tbody = document.getElementById('leaderboard');
  const noteEl = document.getElementById('leaderboard-note');
  if (!data.items.length) {
    tbody.innerHTML = '<tr><td colspan="18" class="empty">榜单数据为空。worker 还没抓到，或榜单代币都没有永续合约。等下一轮...</td></tr>';
    noteEl.textContent = '';
    renderDeepAnalysis([]);
    return;
  }
  if (data.skipped_no_contract) {
    noteEl.textContent = `已过滤 ${data.skipped_no_contract} 个无永续合约的代币。`;
  } else {
    noteEl.textContent = '';
  }
  tbody.innerHTML = data.items.map(item => {
    const m = rowFromMarket(item);
    const starCls = item.in_watchlist ? 'star active' : 'star';
    const watchFlag = item.in_watchlist ? 'true' : 'false';
    return `
      <tr data-token="${item.token}">
        <td><span class="${starCls}" onclick="toggleWatch('${item.token}', ${watchFlag})">★</span></td>
        <td><span class="token token-link" onclick="jumpToCard('${item.token}')">${item.token}</span>
        <button class="open-btn" onclick="openTrade('${item.token}')">开仓</button></td>
        <td class="right"><strong>${item.composite_score.toFixed(1)}</strong></td>
        <td>${fmtTrend(item.trend)}</td>
        <td class="right">${item.score.toFixed(1)}</td>
        <td class="right">${item.unique_posts}</td>
        <td class="right">${m.price}</td>
        <td class="right">${m.ch15m}</td>
        <td class="right">${m.ch1h}</td>
        <td class="right">${m.ch4h}</td>
        <td class="right">${m.fr}</td>
        <td class="right">${m.oi15m}</td>
        <td class="right">${m.oi}</td>
        <td class="right">${m.oi4h}</td>
        <td class="right"><strong>${m.score}</strong></td>
        <td class="verdict">${m.verdict}</td>
        <td>${m.direction}</td>
        <td>${m.divergence}</td>
      </tr>
    `;
  }).join('');
  renderDeepAnalysis(data.items);
}

// 刷新按钮走 manual 分支，提示不同
function manualRefresh() {
  refreshAll({ manual: true });
}

// === 观察列表同步拉取合约数据（后端会直接调币安 API）===
async function refreshWatchlistMarket() {
  const btns = document.querySelectorAll('.refresh-btn');
  btns.forEach(b => b.disabled = true);
  showToast('正在拉取最新合约数据...', 'ok', 1500);
  try {
    const resp = await fetch('/api/watchlist/refresh', { method: 'POST' });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    const data = await resp.json();
    if (!data.tokens || !data.tokens.length) {
      showToast('观察列表为空', 'ok');
    } else {
      showToast(`已刷新 ${data.refreshed} 个代币`
        + (data.skipped_no_contract ? `（${data.skipped_no_contract} 个无合约）` : ''),
        'ok', 3000);
      // 触发观察列表涉及代币闪烁
      flashRows(new Set(data.tokens));
    }
    await refreshAll({ silent: true });
  } catch (e) {
    showToast('刷新失败：' + e.message, 'err');
  } finally {
    btns.forEach(b => b.disabled = false);
  }
}

// === 负面样本统计 ===
async function loadLossSamples() {
  try {
    const resp = await fetch('/api/loss_samples');
    const s = await resp.json();
    const el = document.getElementById('loss-samples-stats');
    if (!el) return;
    if (!s.count) {
      el.innerHTML = '📚 尚无已归档的学习样本（浮亏超过 ' +
        '<span style="color:var(--text);">10%</span> 的收藏会自动归档供参考）';
      return;
    }
    // 把 verdict 分布格式化
    const vd = s.anchor_verdict_distribution || {};
    const vdParts = Object.entries(vd).map(([k, v]) => `${k}: ${v}`).join(' · ');
    el.innerHTML = `📚 已累积 <strong style="color:var(--text);">${s.count}</strong> 个负面样本` +
      ` · 平均浮亏 <span class="red">${s.avg_drawdown_pct}%</span>` +
      (vdParts ? ` · 入场判断分布: ${vdParts}` : '');
  } catch (e) {
    // 静默
  }
}

// === 自动交易面板 ===
function renderTradingPanel(data) {
  renderTradeCandidates(data.candidates || []);
  renderShortCandidates(data.short_candidates || []);
  renderKolCandidates(data.kol_candidates || []);
}

function renderTradeCandidates(items) {
  const el = document.getElementById('trade-candidates');
  if (!items.length) {
    el.innerHTML = '<div class="empty">暂无做多建议</div>';
    return;
  }
  el.innerHTML = '<div class="candidate-list">' + items.map(c => {
    const cls = c.passed ? 'pass' : 'wait';
    const action = c.has_active_position ? '已有持仓/挂单' : c.suggestion;
    const reasons = (c.reasons || []).slice(0, 6).join(' · ');
    return `<div class="candidate-item ${cls}">
      <div><span class="token">${c.token}</span> #${c.rank} · ${action} · 市价 ${fmtPrice(c.price)}</div>
      <div class="muted" style="margin-top:5px;">${reasons}</div>
    </div>`;
  }).join('') + '</div>';
}

function renderShortCandidates(items) {
  const el = document.getElementById('short-candidates');
  if (!items.length) {
    el.innerHTML = '<div class="empty">暂无做空建议</div>';
    return;
  }
  el.innerHTML = '<div class="candidate-list">' + items.map(c => {
    const cls = c.passed ? 'pass' : 'wait';
    const action = c.has_active_position ? '已有持仓/挂单' : c.suggestion;
    const reasons = (c.reasons || []).slice(0, 6).join(' · ');
    return `<div class="candidate-item short ${cls}">
      <div><span class="token">${c.token}</span> #${c.rank} · ${action} · 市价 ${fmtPrice(c.price)}</div>
      <div class="muted" style="margin-top:5px;">${reasons}</div>
    </div>`;
  }).join('') + '</div>';
}

function renderKolCandidates(items) {
  const el = document.getElementById('kol-candidates');
  if (!items.length) {
    el.innerHTML = '<div class="empty">暂无综合建议</div>';
    return;
  }
  el.innerHTML = '<div class="candidate-list">' + items.map(c => {
    const action = c.has_active_position ? '已有持仓/挂单' : '综合建议';
    const reasons = (c.reasons || []).length ? ' · ' + c.reasons.join(' · ') : '';
    return `<div class="candidate-item kol pass">
      <div><span class="token">${c.token}</span> #${c.rank} · ${action}${reasons} · 市价 ${fmtPrice(c.price)}</div>
    </div>`;
  }).join('') + '</div>';
}

async function loadTradingPanel() {
  try {
    const resp = await fetch('/api/trading');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    renderTradingPanel(await resp.json());
  } catch (e) {
    document.getElementById('trade-candidates').innerHTML =
      `<div class="empty">交易面板加载失败：${e.message}</div>`;
  }
}

function toggleDataSourceOpts() {
  const src = document.getElementById('trade-data-source').value;
  document.getElementById('ds-opt-candidates').style.display = src === 'agent_candidates' ? '' : 'none';
  document.getElementById('ds-opt-heat').style.display = src === 'token_heat_history' ? '' : 'none';
}

async function restartSystem() {
  if (!confirm('确定要重启系统吗？\\n\\n所有进程将被强制终止（kill -9），等待 11 分钟后自动重启。\\n\\n重启期间约 12 分钟不可用。')) return;
  try {
    showToast('正在重启系统...', 'ok');
    const resp = await fetch('/api/system/restart', {method:'POST'});
    const d = await resp.json();
    showToast(d.msg || '已触发重启', 'ok');
  } catch(e) {
    showToast('重启指令发送失败，请手动重启', 'err');
  }
}

async function saveTradingSettings() {
  const body = {
    enabled: document.getElementById('trade-enabled').value === 'true',
    mode: document.getElementById('trade-mode').value,
    initial_balance: Number(document.getElementById('trade-initial').value),
    leverage: Number(document.getElementById('trade-leverage').value),
    agent_collect_interval_minutes: Number(document.getElementById('trade-collect-interval').value),
    agent_trigger_interval: Number(document.getElementById('trade-trigger-interval').value),
    kol_agent_interval_minutes: Number(document.getElementById('trade-kol-interval').value),
    kol_snapshot_interval_minutes: Number(document.getElementById('trade-kol-snapshot-interval').value),
    agent_data_source: document.getElementById('trade-data-source').value,
  };
  try {
    const resp = await fetch('/api/trading/settings', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    showToast('交易设置已保存', 'ok');
    await loadTradingPanel();
  } catch (e) {
    showToast('保存交易设置失败：' + e.message, 'err');
  }
}

async function resetTradingAccount() {
  // 拿到当前初始金额作为默认值
  const initialInput = document.getElementById('trade-initial');
  const currentInitial = Number(initialInput.value) || 1000;

  // 第一次确认：告知后果（全 ASCII 文本防编码问题）
  const confirm1 = window.confirm(
    '[警告] 重置账户将会清空:\\n\\n' +
    '  - 所有持仓 (含挂单和已平仓历史)\\n' +
    '  - 已实现盈亏 / 浮动盈亏\\n' +
    '  - 占用保证金\\n' +
    '  - 止损学习归档\\n' +
    '  - signal_lock 去重表\\n\\n' +
    '配置 (倍数/自动交易开关) 会保留。\\n\\n' +
    '此操作不可撤销！确定继续吗？'
  );
  if (!confirm1) return;

  // 第二次确认：让用户输入初始金额（顺便当作二次确认）
  const newBalance = window.prompt(
    '请输入重置后的账户初始金额 USDT (回车保持 ' + currentInitial + '):',
    String(currentInitial)
  );
  if (newBalance === null) return;  // 用户点了取消
  const parsed = Number(newBalance);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    showToast('金额必须为正数', 'err');
    return;
  }

  try {
    const resp = await fetch('/api/trading/reset', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        confirm: true,
        new_initial_balance: parsed,
      }),
    });
    if (!resp.ok) {
      const err = await resp.text();
      throw new Error(err || ('HTTP ' + resp.status));
    }
    const data = await resp.json();
    showToast(
      '账户已重置：清除 ' + data.positions_deleted + ' 条持仓 / ' +
      data.locks_deleted + ' 条锁 / ' + data.loss_archive_deleted + ' 条归档',
      'ok'
    );
    await loadTradingPanel();
    await refreshAll({ silent: true });
  } catch (e) {
    showToast('重置失败：' + e.message, 'err');
  }
}

// === Worker 状态轮询 ===
async function pollWorkerStatus() {
  try {
    const resp = await fetch('/api/status');
    const s = await resp.json();
    renderWorkerPanel(s);
  } catch (e) {
    renderWorkerPanel({ stage: 'unknown', detail: '状态接口不可用', running: false });
  }
}

function renderWorkerPanel(s) {
  const dot = document.getElementById('worker-dot');
  const stageEl = document.getElementById('worker-stage');
  const detailEl = document.getElementById('worker-detail');
  const fillEl = document.getElementById('worker-progress-fill');
  const statsEl = document.getElementById('worker-stats');
  const roundEl = document.getElementById('worker-round');

  // 圆点状态
  dot.className = 'worker-dot';
  if (s.running) dot.classList.add('running');
  else if (s.stage !== 'unknown') dot.classList.add('stopped');

  // 阶段徽章
  const stage = s.stage || 'unknown';
  stageEl.className = 'worker-stage-badge ' + stage;
  const stageLabels = {
    scraping: '抓取中', saving: '入库中', market: '查询合约',
    idle: '空闲（准备下一轮）', unknown: '未知',
    okx_fetch: '获取OKX数据', resting: '轮间休息',
  };
  stageEl.textContent = stageLabels[stage] || stage;

  detailEl.textContent = s.detail || '—';

  // 进度条：OKX 模式无抓取阶段，始终满格；币安模式抓取阶段按时间算
  let pct = 100;
  if (stage === 'scraping' && s.round_start && s.round_duration_seconds) {
    try {
      const startMs = new Date(s.round_start).getTime();
      const elapsed = (Date.now() - startMs) / 1000;
      pct = Math.min(100, (elapsed / s.round_duration_seconds) * 100);
    } catch (e) { pct = 50; }
  }
  fillEl.style.width = pct.toFixed(0) + '%';

  // 轮次（前面加数据源标识）
  if (s.round_number) {
    const srcLabel = s.data_source === 'okx' ? 'OKX' : '币安';
    roundEl.textContent = srcLabel + ' · 第 ' + s.round_number + ' 轮'
      + (s.heartbeat_age_seconds !== undefined ? ' · ' + s.heartbeat_age_seconds + 's 前更新' : '');
  } else {
    roundEl.textContent = '—';
  }

  // 统计
  const stats = [];
  if (s.data_source === 'okx') {
    if (s.okx_tokens_count !== undefined && s.okx_tokens_count > 0)
      stats.push(`<span>OKX 数据源 · <strong>${s.okx_tokens_count}</strong> 个代币</span>`);
    if (s.okx_last_round)
      stats.push(`<span>Pipeline 轮次: <strong>${s.okx_last_round}</strong></span>`);
  } else {
    if (s.posts_this_round !== undefined)
      stats.push(`<span>本轮抓到 <strong>${s.posts_this_round}</strong> 条</span>`);
    if (s.saved_this_round !== undefined)
      stats.push(`<span>入库 <strong>${s.saved_this_round}</strong> 条</span>`);
    if (s.total_posts !== undefined)
      stats.push(`<span>累计帖子 <strong>${s.total_posts}</strong></span>`);
    if (s.total_authors !== undefined)
      stats.push(`<span>累计作者 <strong>${s.total_authors}</strong></span>`);
  }
  statsEl.innerHTML = stats.join('');
}

let watchlistPollBusy = false;
async function pollWatchlistRealtime() {
  if (watchlistPollBusy) return;
  watchlistPollBusy = true;
  try {
    await loadWatchlist();
  } finally {
    watchlistPollBusy = false;
  }
}

refreshAll({ silent: true });
pollWorkerStatus();
setInterval(pollWatchlistRealtime, 5000);
setInterval(loadTradingPanel, 3000);
setInterval(() => refreshAll(), 30000);
setInterval(pollWorkerStatus, 2000);  // worker 状态高频刷新
</script>
</body>
</html>
"""


AGENT_HTML = """
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>Agent-合约扫描</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Inter:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg: #0f1419; --panel: #1a1f2e; --border: #2a3142;
  --text: #e6e8eb; --muted: #8b92a5; --accent: #58a6ff;
  --green: #3fb950; --red: #f85149; --yellow: #d29922; --blue: #1890ff;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 16px;
  font-family: 'JetBrains Mono', 'Inter', -apple-system, BlinkMacSystemFont, "Microsoft YaHei", monospace;
  background: var(--bg); color: var(--text); font-size: 13px;
}
h1 { font-size: 20px; color: var(--accent); margin: 0 0 4px; }
h2 { font-size: 14px; color: var(--accent); margin: 0 0 10px; }
.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.header-time { color: var(--muted); font-size: 12px; }
.subtitle { color: var(--muted); font-size: 12px; margin-top: 2px; }
.nav-bar-agent {
  display: flex; justify-content: center; gap: 0; margin-bottom: 16px;
  border-bottom: 1px solid var(--border); padding-bottom: 0;
}
.nav-bar-agent a {
  padding: 10px 24px; color: var(--muted); text-decoration: none;
  font-size: 13px; font-weight: 500; border-bottom: 2px solid transparent;
}
.nav-bar-agent a.active, .nav-bar-agent a:hover { color: var(--accent); border-bottom-color: var(--accent); }
.nav-bar-agent .nav-dropdown > a { display: inline-block; padding: 10px 24px; color: var(--muted); text-decoration: none; font-size: 13px; font-weight: 500; border-bottom: 2px solid transparent; transition: all 0.2s; }
.nav-bar-agent .nav-dropdown > a:hover { color: var(--accent); border-bottom-color: var(--accent); }
.nav-dropdown { position: relative; }
.nav-dropdown-content { display: none; position: absolute; top: 100%; left: 0; background: var(--panel); border: 1px solid var(--border); border-radius: 6px; min-width: 160px; z-index: 100; padding: 4px 0; }
.nav-dropdown-content a { display: block; padding: 8px 16px; color: var(--text); border-bottom: none !important; font-size: 13px; white-space: nowrap; }
.nav-dropdown-content a:hover { background: #1a1f2e; color: var(--accent) !important; }
.nav-dropdown:hover .nav-dropdown-content { display: block; }
.nav-link { color: var(--accent); text-decoration: none; font-size: 12px; }
.nav-link:hover { text-decoration: underline; }

/* 布局 */
.grid-top { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }
.grid-bottom { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
@media (max-width: 900px) {
  .grid-top, .grid-bottom { grid-template-columns: 1fr; }
}

/* 面板 */
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 14px; }
.stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
.stat { padding: 8px; background: #222836; border-radius: 6px; }
.stat-label { color: var(--muted); font-size: 11px; margin-bottom: 2px; }
.stat-value { font-size: 18px; font-weight: 600; }
.stat-sm { font-size: 13px; }
.green { color: var(--green); }
.red { color: var(--red); }
.yellow { color: var(--yellow); }
.blue { color: var(--blue); }
.muted { color: var(--muted); }

/* 时间线 */
.timeline { overflow-y: auto; }
.tl-item { padding: 10px 12px; border-left: 3px solid var(--border); margin-left: 8px; margin-bottom: 2px; position: relative; }
.tl-item::before { content: ''; position: absolute; left: -7px; top: 14px; width: 10px; height: 10px; border-radius: 50%; background: var(--border); }
.tl-item.open::before { background: var(--green); }
.tl-item.close::before { background: var(--red); }
.tl-item.pending::before { background: var(--yellow); }
.tl-item.rejected::before { background: #8b4513; }
.tl-item.expired::before { background: var(--muted); }
.tl-item.noop::before { background: var(--blue); }
.tl-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; }
.tl-time { color: var(--muted); font-size: 11px; min-width: 60px; }
.tl-badge { font-size: 11px; padding: 1px 6px; border-radius: 3px; font-weight: 500; }
.tl-badge.open { background: #1a3a1a; color: var(--green); }
.tl-badge.close { background: #3a1a1a; color: var(--red); }
.tl-badge.pending { background: #3a3a1a; color: var(--yellow); }
.tl-badge.rejected { background: #3a2a1a; color: #d4845a; }
.tl-badge.expired { background: #2a2a2a; color: var(--muted); }
.tl-token { font-weight: bold; color: var(--accent); }
.tl-reason { color: var(--text); font-size: 12px; margin-top: 4px; line-height: 1.5; }
.trunc { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; text-overflow: ellipsis; cursor: default; }
.trunc.expanded { display: block; -webkit-line-clamp: unset; }
.tl-meta { color: var(--muted); font-size: 11px; margin-top: 4px; }
.tl-detail { display: none; margin-top: 8px; padding: 8px; background: #151a26; border-radius: 4px; font-size: 11px; }
.tl-detail.show { display: block; }
.tl-toggle { cursor: pointer; color: var(--accent); font-size: 11px; user-select: none; }
.tl-toggle:hover { text-decoration: underline; }

/* 教训 */
.lesson-item { padding: 10px; background: #222836; border-radius: 6px; margin-bottom: 8px; border-left: 3px solid var(--border); }
.lesson-item.critical { border-left-color: var(--red); }
.lesson-item.warning { border-left-color: var(--yellow); }
.lesson-item.medium { border-left-color: var(--blue); }
.lesson-head { display: flex; justify-content: space-between; align-items: center; }
.lesson-token { font-weight: bold; color: var(--accent); }
.lesson-sev { font-size: 11px; padding: 1px 6px; border-radius: 3px; }
.lesson-sev.critical { background: #3a1a1a; color: var(--red); }
.lesson-sev.warning { background: #3a3a1a; color: var(--yellow); }
.lesson-sev.medium { background: #1a2a3a; color: var(--blue); }
.lesson-body { margin-top: 6px; font-size: 12px; line-height: 1.5; }
.lesson-rule { margin-top: 4px; padding: 4px 8px; background: #1a2a1a; border-radius: 3px; color: var(--green); font-size: 11px; }

/* 持仓 */
table { width: 100%; border-collapse: collapse; font-size: 12px; }
th { text-align: left; color: var(--muted); font-weight: 500; padding: 6px 8px; border-bottom: 1px solid var(--border); }
td { padding: 6px 8px; border-bottom: 1px solid #222836; }
tr:hover { background: #1f2536; }

/* 统计条 */
.stats-bar { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 10px; }
.stats-bar .item { font-size: 12px; }
.stats-bar .label { color: var(--muted); }

.empty { color: var(--muted); font-style: italic; text-align: center; padding: 20px; }

/* 滚动条 */
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: var(--bg); }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
</style>
</head>
<body>

<div class="nav-bar-agent">
  <a href="/">📊 市场监控</a>
  <a href="/strategies">📈 运行策略</a>
  <div class="nav-dropdown">
    <a href="#">🤖 Agent 面板 ▾</a>
    <div class="nav-dropdown-content">
      <a href="/agent">Agent-合约扫描</a>
      <a href="/heat-agent">Agent-热度榜单</a>
    <a href="/agent-heat-lessons">Agent-热度有教训</a>
    <a href="/agent-nl">Agent-无教训</a>
                <a href="/agent-short">Agent-做空</a>
      <a href="/agent-kol">Agent-KOL 监控</a>
              <a href="/agent-kol-snapshot">Agent-KOL 快照监控</a>
              </div>
  </div>
  <a href="/dashboard">📊 市场全景</a>
  <a href="/onchain">🔍 链上监控</a>
  <a href="/alpha-cluster">🔬 Alpha 集群</a>
  <a href="/live-onchain">💹 实盘监控</a>
  <a href="/settings">⚙ 系统设置</a>
</div>

<div class="header">
  <div>
    <h1>Agent-合约扫描</h1>
    <span class="header-time" id="clock"></span>
    <div class="subtitle">合约扫描 · 自主决策开仓</div>
  </div>
</div>
</div>


<!-- 每日账户总额变化卡片 -->
<div id="daily-card" class="panel" style="padding:16px 20px;margin-bottom:12px">
  <div id="daily-content" style="color:var(--muted);text-align:center;padding:10px">加载中...</div>
</div>

<!-- 顶部：账户概览 + 持仓 -->
<div class="grid-top">
  <div class="panel">
    <h2>账户</h2>
    <div class="stat-grid">
      <div class="stat"><div class="stat-label">总资产/可用</div><div class="stat-value" id="equity-avail">—</div></div>
      <div class="stat"><div class="stat-label">今日PnL</div><div class="stat-value" id="today-pnl">—</div></div>
      <div class="stat"><div class="stat-label">胜率</div><div class="stat-value" id="win-rate">—</div></div>
      <div class="stat"><div class="stat-label">资金利用率</div><div class="stat-value" id="exposure">—</div></div>
    </div>
    <div class="stat-grid" style="margin-top:10px">
      <div class="stat"><div class="stat-label">今日开/平</div><div class="stat-sm" id="today-trades">—</div></div>
      <div class="stat"><div class="stat-label">待处理</div><div class="stat-sm" id="pending">—</div></div>
      <div class="stat"><div class="stat-label">今日被拒</div><div class="stat-sm" id="rejected">—</div></div>
      <div class="stat"><div class="stat-label">总盈亏</div><div class="stat-sm" id="total-pnl">—</div></div>
      <div class="stat"><div class="stat-label">最大回撤</div><div class="stat-sm" id="max-dd">—</div></div>
      <div class="stat"><div class="stat-label">胜/总</div><div class="stat-sm" id="total-closed">—</div></div>
    </div>
  </div>

  <div class="panel" style="overflow:auto" id="pending-panel" >
    <h2>挂单</h2>
    <table>
      <thead><tr>
        <th>币种</th><th>方向</th><th>限价</th><th>现价</th><th>入场结构</th><th>最新结构</th><th>仓位</th><th>剩余</th><th>开仓时间</th><th>操作</th>
      </tr></thead>
      <tbody id="pending-body"></tbody>
    </table>
    <div class="empty" id="pending-empty" >无挂单</div>
  </div>

  <div class="panel" style="grid-column:1/-1;overflow:auto;max-height:380px">
    <h2>当前持仓</h2>
    <table>
      <thead><tr>
        <th>币种</th><th>方向</th><th>锁利</th><th>入场</th><th>现价</th><th>入场结构</th><th>最新结构</th><th>仓位</th><th>PnL</th><th>止损</th><th>TP1</th><th>TP2</th><th>持仓</th><th>类型</th><th>开仓时间</th>
      </tr></thead>
      <tbody id="positions-body"></tbody>
    </table>
    <div class="empty" id="positions-empty" style="display:none">无持仓</div>
  </div>
</div>

<!-- 底部：教训库 + 决策时间线 -->
<div class="grid-bottom">
  <div class="panel">
    <h2>教训库</h2>
    <div class="stats-bar" id="lesson-stats"></div>
    <div id="lessons-list"></div>
  </div>

  <div class="panel">
    <h2>决策时间线</h2>
    <div class="timeline" id="timeline"></div>
  </div>
</div>

<!-- 操作日志 -->
<div class="panel" style="margin-top:12px">
  <h2>操作日志</h2>
  <table style="font-size:12px">
    <thead><tr>
      <th>时间</th><th>操作</th><th>代币</th><th class="right">价格</th>
      <th>档位</th><th class="right">盈亏</th><th>理由</th>
    </tr></thead>
    <tbody id="journal-body"></tbody>
  </table>
  <div class="empty" id="journal-empty" style="display:none">暂无操作记录</div>
</div>

<script>
const $ = s => document.querySelector(s);
const esc = s => s ? String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') : '';
const AGENT_STRATEGY = 'agent';

function fmtPct(v) {
  if (v == null) return '—';
  const n = Number(v);
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
}
function fmtPrice(v) {
  if (v == null) return '—';
  const n = Number(v);
  return n >= 1 ? n.toFixed(4) : n.toFixed(6);
}
function fmtTime(ts) {
  if (!ts) return '';
  try {
    const d = new Date(ts.replace(' ', 'T') + 'Z');
    return d.toLocaleTimeString('zh-CN', {hour12: false, timeZone: 'Asia/Shanghai'}).substr(0, 5);
  } catch { return ts.substr(11, 5); }
}
function fmtDateTime(ts) {
  if (!ts) return '';
  try {
    const d = new Date(ts.replace(' ', 'T') + 'Z');
    return d.toLocaleString('zh-CN', {hour12: false, timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'}).replace(/\\//g, '-');
  } catch { return ts; }
}
function pnlClass(v) { return v > 0 ? 'green' : v < 0 ? 'red' : 'muted'; }
let _truncId = 0;
function truncText(text, max) {
  max = max || 100;
  if (!text || text.length <= max) return esc(text);
  const id = 'tr-' + (++_truncId);
  return `<span class="trunc" id="${id}" title="${esc(text)}" onclick="this.classList.toggle('expanded')">${esc(text)}</span>`;
}

// 账户概览
async function loadDailyCard() {
  try {
    const r = await fetch('/api/agent/daily-pnl?strategy='+AGENT_STRATEGY+'&days=30');
    const d = await r.json();
    if (!d.daily || !d.daily.length) {
      $('#daily-content').innerHTML = '<div style="text-align:center;color:var(--muted);padding:20px">暂无历史交易数据</div>';
      return;
    }
    const days = d.daily;
    const labels = days.map(dd=>dd.date.slice(5));
    labels.push('当前');
    const equityData = days.map(dd=>dd.equity);
    equityData.push(d.current_equity);
    const pnlData = days.map(dd=>dd.day_pnl);

    // 表格行
    let rows = '';
    for (let i = days.length-1; i >= Math.max(0, days.length-5); i--) {
      const dd = days[i];
      const wr = dd.trades > 0 ? Math.round(dd.wins/dd.trades*100) : 0;
      rows += '<tr>'+
        '<td>'+dd.date+'</td>'+
        '<td class="right">$'+dd.equity.toFixed(2)+'</td>'+
        '<td class="right '+(dd.day_pnl>=0?'green':'red')+'">'+(dd.day_pnl>=0?'+':'')+dd.day_pnl.toFixed(2)+'</td>'+
        '<td class="right '+(dd.day_pnl>=0?'green':'red')+'">'+(dd.day_pnl>=0?'+':'')+dd.day_pnl.toFixed(2)+'</td>'+
        '<td class="right">'+dd.trades+' / '+wr+'%</td>'+
        '<td class="right" style="color:var(--muted)">真实</td>'+
      '</tr>';
    }

    const chg = d.period_change;
    const chgCls = chg >= 0 ? 'green' : 'red';
    const chgSign = chg >= 0 ? '+' : '';

    $('#daily-content').innerHTML =
      '<div style="display:flex;gap:20px;align-items:center;flex-wrap:wrap;margin-bottom:12px">'+
        '<div>'+
          '<div style="font-size:26px;font-weight:700;font-family:JetBrains Mono,monospace">$'+d.current_equity.toFixed(2)+'</div>'+
          '<div style="font-size:13px;margin-top:2px"><span class="'+chgCls+'">'+chgSign+chg.toFixed(2)+' USDT</span><span style="color:var(--muted);font-size:11px;margin-left:8px">近 '+d.daily.length+' 天</span></div>'+
        '</div>'+
      '</div>'+
      '<div style="display:flex;gap:16px;flex-wrap:wrap">'+
        '<div style="flex:1;min-width:320px;height:200px"><canvas id="daily-equity-chart"></canvas></div>'+
        '<div style="flex:1;min-width:320px;height:200px"><canvas id="daily-pnl-chart"></canvas></div>'+
      '</div>'+
      '<table style="margin-top:12px;font-size:12px;width:100%"><thead><tr>'+
        '<th>日期</th><th class="right">权益</th><th class="right">日变化</th><th class="right">净盈亏</th><th class="right">交易</th><th class="right">数据</th>'+
      '</tr></thead><tbody>'+rows+'</tbody></table>';

    // Chart.js 渲染（需等 DOM 就绪）
    setTimeout(() => {
      const ctx1 = document.getElementById('daily-equity-chart');
      const ctx2 = document.getElementById('daily-pnl-chart');
      if (!ctx1 || !ctx2) return;

      // 销毁旧实例
      if (ctx1._chart) ctx1._chart.destroy();
      if (ctx2._chart) ctx2._chart.destroy();

      ctx1._chart = new Chart(ctx1, {
        type: 'line',
        data: {
          labels: labels,
          datasets: [{
            label: '权益',
            data: equityData,
            borderColor: '#0ECB81',
            backgroundColor: 'rgba(14,203,129,0.06)',
            fill: true,
            tension: 0.2,
            pointRadius: 1,
            borderWidth: 1.5,
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: '#8b949e', font: { size: 10 }, maxTicksLimit: 8 }, grid: { color: '#21262d' } },
            y: { ticks: { color: '#8b949e', font: { size: 10 }, callback: v => '$'+v }, grid: { color: '#21262d' } },
          },
        },
      });

      ctx2._chart = new Chart(ctx2, {
        type: 'bar',
        data: {
          labels: labels,
          datasets: [{
            data: pnlData,
            backgroundColor: ctx => ctx.raw >= 0 ? 'rgba(14,203,129,0.7)' : 'rgba(248,81,73,0.7)',
            borderRadius: 2,
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: '#8b949e', font: { size: 10 }, maxTicksLimit: 8 }, grid: { display: false } },
            y: { ticks: { color: '#8b949e', font: { size: 10 }, callback: v => (v>=0?'+':'')+'$'+v }, grid: { color: '#21262d' } },
          },
        },
      });
    }, 50);
  } catch(e) { console.error('daily', e); }
}

async function loadOverview() {
  try {
    const r = await fetch('/api/agent/overview');
    const d = await r.json();
    $('#equity-avail').innerHTML = '$' + d.equity + '<br><span style="font-size:13px;color:var(--muted)">$' + d.available + '</span>';
    $('#equity-avail').className = 'stat-value ' + (d.unrealized >= 0 ? 'green' : 'red');
    $('#today-pnl').textContent = (d.today_pnl >= 0 ? '+' : '') + '$' + Math.abs(d.today_pnl).toFixed(2);
    $('#today-pnl').className = 'stat-value ' + (d.today_pnl >= 0 ? 'green' : 'red');
    $('#win-rate').textContent = d.win_rate + '%';
    $('#today-trades').innerHTML = '<span class="green">' + d.today_opens + '</span> / <span class="red">' + d.today_closes + '</span> (胜' + d.today_wins + ' 负' + d.today_losses + ')';
    $('#pending').textContent = d.pending_decisions + ' 条';
    $('#rejected').textContent = d.rejected_today + ' 条';
    $('#exposure').textContent = (d.exposure || 0).toFixed(1) + '%';
    $('#exposure').className = 'stat-value ' + (d.exposure > 50 ? 'red' : d.exposure > 30 ? 'yellow' : '');
    $('#total-pnl').textContent = fmtPct(d.total_pnl_pct);
    $('#total-pnl').className = 'stat-sm ' + pnlClass(d.total_pnl_pct);
    $('#max-dd').textContent = (d.max_drawdown || 0).toFixed(2) + '%';
    $('#max-dd').className = 'stat-sm ' + ((d.max_drawdown||0) < -5 ? 'red' : (d.max_drawdown||0) < -2 ? 'yellow' : '');
    $('#total-closed').innerHTML = '<span class="green">' + d.total_wins + '</span>胜/<span>' + d.total_closed + '</span>笔';
    $('#rejected').textContent = d.rejected_today + ' 条';
  } catch(e) { console.error('overview', e); }
}

// 持仓
async function loadPositions() {
  try {
    const r = await fetch('/api/trading');
    const d = await r.json();
    const positions = (d.positions || []).filter(p => (p.status === 'OPEN' || p.status === 'PARTIAL') && p.strategy === 'agent');
    const tbody = $('#positions-body');
    const empty = $('#positions-empty');
    if (!positions.length) {
      tbody.innerHTML = '';
      empty.style.display = '';
      return;
    }
    empty.style.display = 'none';
    tbody.innerHTML = positions.map(p => {
      const pnl = p.pnl_pct != null ? Number(p.pnl_pct) : null;
      const closedQty = Number(p.closed_qty || 0);
      const totalQty = Number(p.quantity || 0);
      const lockedPct = totalQty > 0 ? (closedQty / totalQty * 100) : 0;
      let hold = '—';
      if (p.created_at) {
        try { const d = new Date(p.created_at.replace(' ','T') + 'Z'); const h = Math.floor((Date.now()-d)/3600000); const m = Math.floor((Date.now()-d)/60000)%60; hold = (h ? h+'h' : '') + m + 'm'; } catch(e) {}
      }
      const entryConf = p.entry_confidence != null ? Number(p.entry_confidence) : null;
      const entryTrend = p.entry_trend ? '<span class="badge badge-action">'+esc(p.entry_trend)+(entryConf!=null?' / '+entryConf:'')+'</span>' : '—';
      const latestConf = p.latest_confidence != null ? Number(p.latest_confidence) : null;
      const latestTrend = p.latest_trend ? '<span class="badge badge-action">'+esc(p.latest_trend)+(latestConf!=null?' / '+latestConf:'')+'</span>' : '—';
      const posSize = '$' + Number(p.margin_amount || 0).toFixed(0) + ' ×' + Number(p.leverage || 0);
      const pnlDollar = p.unrealized_pnl != null ? Number(p.unrealized_pnl) : null;
      let pnlDisplay = '<span class="' + pnlClass(pnl) + '">' + fmtPct(pnl) + '</span>';
      if (pnlDollar !== null) {
        pnlDisplay += ' <span class="muted" style="font-size:10px">(' + (pnlDollar>=0?'+':'') + '$' + pnlDollar.toFixed(2) + ')</span>';
      }
      let openTime = '—';
      if (p.created_at) { try { const d = new Date(p.created_at.replace(' ','T')+'Z'); openTime = d.toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).replace(/\//g,'-'); } catch(e) {} }
      return '<tr>' +
        '<td style="font-weight:bold;color:var(--accent)">' + esc(p.token) + '</td>' +
        '<td>' + esc(p.side) + '</td>' +
        '<td style="font-size:11px;color:var(--green)">' + (lockedPct > 0 ? '🔒'+lockedPct.toFixed(0)+'%' : '—') + '</td>' +
        '<td>' + fmtPrice(p.entry_price) + '</td>' +
        '<td>' + fmtPrice(p.current_price) + '</td>' +
        '<td style="font-size:11px">' + entryTrend + '</td>' +
        '<td style="font-size:11px">' + latestTrend + '</td>' +
        '<td style="font-size:11px;white-space:nowrap">' + posSize + '</td>' +
        '<td style="font-size:11px">' + pnlDisplay + '</td>' +
        '<td>' + fmtPrice(p.stop_loss_price) + '</td>' +
        '<td>' + fmtPrice(p.tp1_price) + '</td>' +
        '<td>' + fmtPrice(p.tp2_price) + '</td>' +
        '<td style="font-size:11px;white-space:nowrap">' + esc(hold) + '</td>' +
        '<td style="font-size:11px">' + (p.order_type === 'limit' ? '挂单' : '市价') + '</td>' +
        '<td style="font-size:11px;white-space:nowrap">' + openTime + '</td>' +
        '</tr>';
    }).join('');
  } catch(e) { console.error('positions', e); }
}

async function loadPending() {
  try {
    const r = await fetch('/api/trading' + location.search);
    const d = await r.json();
    const pending = (d.positions || []).filter(p => p.status === 'PENDING' && p.strategy === 'agent');
    const tbody = document.getElementById('pending-body');
    const empty = document.getElementById('pending-empty');
    const panel = document.getElementById('pending-panel');
    const maxAge = Number((d.account && d.account.settings && d.account.settings.limit_order_timeout_seconds)) || 600;
    if (!pending.length) {
      tbody.innerHTML = '';
      empty.style.display = '';
      return;
    }
    empty.style.display = 'none';
    tbody.innerHTML = pending.map(p => {
      const limitPrice = Number(p.limit_price || 0);
      const currentPrice = Number(p.current_price || 0);
      const gapPct = limitPrice > 0 ? ((currentPrice - limitPrice) / limitPrice * 100).toFixed(1) : '-';
      let remain = '—';
      if (p.created_at) {
        try { const dd = new Date(p.created_at.replace(' ','T') + 'Z'); const elapsed = (Date.now()-dd)/1000; const left = Math.max(0, maxAge - elapsed); const m = Math.floor(left/60); const s = Math.floor(left%60); remain = m + 'm' + s + 's'; } catch(e) {}
      }
      const entryConf = p.entry_confidence != null ? Number(p.entry_confidence) : null;
      const entryTrend = p.entry_trend ? '<span class="badge badge-action">'+esc(p.entry_trend)+(entryConf!=null?' / '+entryConf:'')+'</span>' : '—';
      const latestConf = p.latest_confidence != null ? Number(p.latest_confidence) : null;
      const latestTrend = p.latest_trend ? '<span class="badge badge-action">'+esc(p.latest_trend)+(latestConf!=null?' / '+latestConf:'')+'</span>' : '—';
      const posSize = '$' + Number(p.margin_amount || 0).toFixed(0) + ' ×' + Number(p.leverage || 0);
      let openTime = '—';
      if (p.created_at) { try { const d = new Date(p.created_at.replace(' ','T')+'Z'); openTime = d.toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).replace(/\//g,'-'); } catch(e) {} }
      return '<tr>' +
        '<td style="font-weight:bold;color:var(--accent)">' + esc(p.token) + '</td>' +
        '<td>' + esc(p.side) + '</td>' +
        '<td>' + fmtPrice(limitPrice) + '</td>' +
        '<td>' + fmtPrice(currentPrice) + ' <span class="muted" style="font-size:10px">(' + (gapPct >= 0 ? '+' : '') + gapPct + '%)</span></td>' +
        '<td style="font-size:11px">' + entryTrend + '</td>' +
        '<td style="font-size:11px">' + latestTrend + '</td>' +
        '<td style="font-size:11px;white-space:nowrap">' + posSize + '</td>' +
        '<td style="font-size:11px">' + esc(remain) + '</td>' +
        '<td style="font-size:11px;white-space:nowrap">' + openTime + '</td>' +
        '<td><button onclick="cancelPending(' + p.id + ')" style="font-size:11px;padding:2px 6px">取消</button></td>' +
        '</tr>';
    }).join('');
  } catch(e) { console.error('pending', e); }
}

async function cancelPending(id) {
  if (!confirm('确定取消该挂单？')) return;
  try {
    const r = await fetch('/api/trading/limit-order/' + id + '/cancel', {method:'POST'});
    if (!r.ok) { const e = await r.text(); alert('取消失败: ' + e); return; }
    loadPending();
    loadPositions();
  } catch(e) { alert('取消失败: ' + e.message); }
}

// 教训库
let _lsOffset = 0;
let _lsHasMore = true;
let _lsShowAll = true;
const _LS_PAGE = 5;

async function loadLessons(reset = false) {
  if (reset) { _lsOffset = 0; _lsHasMore = true; }
  if (!_lsHasMore) return;
  try {
    const url = '/api/agent/lessons?limit=' + _LS_PAGE + '&offset=' + _lsOffset + '&all=' + (_lsShowAll ? 1 : 0);
    const r = await fetch(url);
    const d = await r.json();
    const s = d.stats || {};
    _lsOffset += (d.active || []).length;
    _lsHasMore = d.has_more;

    if (reset) {
      $('#lesson-stats').innerHTML =
        '<div class="item"><span class="label">活跃</span> <strong>' + (s.active || 0) + '</strong></div>' +
        '<div class="item"><span class="label">critical</span> <strong class="red">' + ((s.severity_dist || {}).critical || 0) + '</strong></div>' +
        '<div class="item"><span class="label">warning</span> <strong class="yellow">' + ((s.severity_dist || {}).warning || 0) + '</strong></div>' +
        '<div class="item"><span class="label">medium</span> <strong class="blue">' + ((s.severity_dist || {}).medium || 0) + '</strong></div>' +
        '<div class="item"><span onclick="toggleLessonsAll()" style="cursor:pointer;color:var(--accent);font-size:11px">[' + (_lsShowAll ? '仅活跃' : '全部') + ']</span></div>';
    }

    const lessons = d.active || [];
    const list = $('#lessons-list');
    if (reset && !lessons.length) {
      list.innerHTML = '<div class="empty">暂无教训</div>';
      return;
    }

    let html = '';
    const now = new Date();
    lessons.forEach(l => {
      const sev = l.severity || 'medium';
      const isNew = l.created_at ? (now - new Date(l.created_at.replace(' ','T') + 'Z')) / 3600000 < 24 : false;
      const learned = l.learned ? '无效' : '有效';
      const learnedCls = l.learned ? 'red' : 'green';
      html += '<div class="lesson-item ' + sev + '">' +
        '<div class="lesson-head">' +
          '<span style="color:var(--muted);font-size:10px;margin-right:6px">#' + (l.id || '?') + '</span>' +
          '<span class="lesson-token">' + esc(l.token) + '</span>' +
          (isNew ? '<span style="background:#3a1f5f;color:#c4a0ff;padding:1px 5px;border-radius:2px;font-size:10px;margin-left:4px">NEW</span>' : '') +
          '<span class="lesson-sev ' + sev + '">' + sev + '</span>' +
          '<span style="font-size:10px;color:var(--muted);margin-left:auto">' + fmtDateTime(l.created_at) + '</span>' +
          '<span style="font-size:11px;color:var(--' + learnedCls + ');margin-left:8px;cursor:pointer" onclick="toggleLearned(' + (l.id || 0) + ')" title="点击切换">' + learned + '</span>' +
        '</div>' +
        '<div class="lesson-body">' + truncText(l.lesson, 120) + '</div>' +
        (l.rule_update ? '<div class="lesson-rule">规则: ' + truncText(l.rule_update, 80) + '</div>' : '') +
        (l.root_cause ? '<div class="tl-meta">根因: ' + truncText(l.root_cause, 60) + '</div>' : '') +
      '</div>';
    });
    if (reset) { list.innerHTML = html; } else { list.innerHTML += html; }

    if (_lsHasMore && lessons.length) {
      const btn = document.getElementById('ls-more');
      if (!btn) {
        const b = document.createElement('div');
        b.id = 'ls-more';
        b.style.cssText = 'text-align:center;padding:10px;cursor:pointer;color:var(--accent);font-size:12px';
        b.textContent = '▼ 加载更多';
        b.onclick = () => loadLessons(false);
        list.parentElement.appendChild(b);
      }
    } else {
      const btn = document.getElementById('ls-more');
      if (btn) btn.remove();
    }
  } catch(e) { console.error('lessons', e); }
}

// 时间线
let _expandedDets = new Set();
let _tlOffset = 0;
let _tlHasMore = true;
let _tlDetIdx = 0;
const _TL_PAGE = 10;

async function loadTimeline(reset = false) {
  if (reset) { _tlOffset = 0; _tlHasMore = true; }
  if (!_tlHasMore) return;

  document.querySelectorAll('.tl-detail.show').forEach(el => {
    _expandedDets.add(el.id);
  });
  try {
    const r = await fetch('/api/agent/timeline?limit=' + _TL_PAGE + '&offset=' + _tlOffset);
    const d = await r.json();
    const items = d.timeline || [];
    _tlHasMore = d.has_more;
    _tlOffset += items.length;

    const tl = $('#timeline');
    if (reset) {
      if (!items.length) { tl.innerHTML = '<div class="empty">暂无操作记录</div>'; _expandedDets.clear(); return; }
      tl.innerHTML = '';
    }

    let html = '';
    items.forEach((item) => {
      const di = ++_tlDetIdx;
      const src = item.source;
      let action, token, badgeClass, time, reason, meta;

      if (src === 'journal') {
        action = item.action;
        token = item.token;
        badgeClass = action;
        time = fmtDateTime(item.created_at).replace(/^\\d{4}-/, '');
        reason = item.reason || '';
        meta = [];
        if (item.pnl_pct != null) meta.push('PnL: ' + fmtPct(item.pnl_pct));
        if (item.close_reason) meta.push(item.close_reason);
        if (item.hold_duration) meta.push('持仓: ' + item.hold_duration);
        if (item.tier) meta.push('tier: ' + item.tier);
        if (item.social_score) meta.push('热度: ' + item.social_score);
      } else {
        action = item.action;
        token = item.token;
        time = fmtDateTime(item.created_at);
        reason = item.reason || '';
        if (item.status === 'pending') { badgeClass = 'pending'; }
        else if (item.status === 'rejected') { badgeClass = 'rejected'; reason = '拒绝: ' + (item.reject_reason || '') + ' | ' + reason; }
        else if (item.status === 'expired') { badgeClass = 'expired'; }
        else { badgeClass = 'open'; }
        meta = [];
        if (item.status) meta.push('状态: ' + item.status);
        if (item.market_read) meta.push(item.market_read.length > 50 ? item.market_read.substr(0, 50) + '...' : item.market_read);
      }

      let detail = '';
      if (src === 'journal' && item.dimension_data) {
        try {
          const dd = typeof item.dimension_data === 'string' ? JSON.parse(item.dimension_data) : item.dimension_data;
          let fields = Object.entries(dd).map(([k,v]) => [k, typeof v === 'object' ? JSON.stringify(v) : v]);
          detail = '<div class="tl-detail" id="det-' + di + '">'
            + fields.map(([k,v]) => '<b>' + esc(String(k)) + ':</b> ' + esc(v != null ? String(v) : '-')).join(' &nbsp;|&nbsp; ')
            + '</div>';
        } catch(e) {}
      }

      html += '<div class="tl-item ' + badgeClass + '">' +
        '<div class="tl-head">' +
          '<div><span class="tl-time">' + time + '</span> ' +
          '<span class="tl-badge ' + badgeClass + '">' + esc(action) + '</span> ' +
          '<span class="tl-token">' + esc(token) + '</span></div>' +
          (detail ? '<span class="tl-toggle" onclick="toggleDet(' + di + ')">展开</span>' : '') +
        '</div>' +
        '<div class="tl-reason">' + truncText(reason, 100) + '</div>' +
        (meta.length ? '<div class="tl-meta">' + meta.join(' · ') + '</div>' : '') +
        detail +
      '</div>';
    });

    if (reset) { tl.innerHTML = html; } else { tl.innerHTML += html; }
    if (_tlHasMore && items.length) {
      const btn = document.getElementById('tl-more');
      if (!btn) {
        const b = document.createElement('div');
        b.id = 'tl-more';
        b.style.cssText = 'text-align:center;padding:10px;cursor:pointer;color:var(--accent);font-size:12px';
        b.textContent = '▼ 加载更多';
        b.onclick = () => loadTimeline(false);
        tl.parentElement.appendChild(b);
      }
    } else {
      const btn = document.getElementById('tl-more');
      if (btn) btn.remove();
    }

    _expandedDets.forEach(id => {
      const el = document.getElementById(id);
      if (el) el.classList.add('show');
    });
  } catch(e) { console.error('timeline', e); }
}


function toggleLessonsAll() {
  _lsShowAll = !_lsShowAll;
  loadLessons(true);
}

async function toggleLearned(id) {
  try {
    const r = await fetch('/api/agent/lessons/toggle?id=' + id, {method:'POST'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    loadLessons(true);
  } catch(e) { console.error('toggleLearned', e); }
}

function toggleDet(i) {
  const el = document.getElementById('det-' + i);
  if (!el) return;
  el.classList.toggle('show');
  if (el.classList.contains('show')) {
    _expandedDets.add('det-' + i);
  } else {
    _expandedDets.delete('det-' + i);
  }
}

// 时钟
function updateClock() {
  const now = new Date();
  $('#clock').textContent = now.toLocaleString('zh-CN', {hour12: false, timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit'}).replace(/\\//g, '-') + ' UTC+8';
}

let _jOffset = 0;
let _jHasMore = true;
const _J_PAGE = 5;

async function loadJournal(reset = false) {
  if (reset) { _jOffset = 0; _jHasMore = true; }
  if (!_jHasMore) return;
  try {
    const r = await fetch('/api/agent/journal?limit=' + _J_PAGE + '&offset=' + _jOffset);
    const d = await r.json();
    const items = d.journal || [];
    _jHasMore = d.has_more;
    _jOffset += items.length;

    const tbody = $('#journal-body');
    const empty = $('#journal-empty');
    if (reset && !items.length) {
      tbody.innerHTML = ''; empty.style.display = ''; return;
    }
    empty.style.display = 'none';

    let html = '';
    items.forEach(j => {
      const actionLabel = j.action === 'open' ? '开仓' : j.action === 'close' ? '平仓' : j.action;
      const actionCls = j.action === 'open' ? 'green' : j.action === 'close' ? 'red' : '';
      const pnl = j.pnl_pct != null ? Number(j.pnl_pct) : null;
      const pnlStr = pnl != null ? fmtPct(pnl) : '—';
      const pnlCls = pnl != null ? (pnl > 0 ? 'green' : 'red') : 'muted';
      const fullReason = j.reason || '';
      const reason = fullReason ? fullReason.substr(0, 60) + (fullReason.length > 60 ? '...' : '') : '—';
      const hold = j.hold_duration || '';
      const reasonWithHold = (reason + (hold ? ' (' + hold + ')' : ''));
      const fullReasonWithHold = (fullReason + (hold ? ' (' + hold + ')' : ''));
      html += '<tr>' +
        '<td style="font-size:11px;white-space:nowrap">' + fmtDateTime(j.created_at).replace(' ', '<br>') + '</td>' +
        '<td><span style="font-size:11px;padding:1px 6px;border-radius:3px' + (actionCls ? ';color:' + (j.action==='open'?'var(--green)':'var(--red)') + ';background:' + (j.action==='open'?'#1a3a1a':'#3a1a1a') : '') + '">' + esc(actionLabel) + '</span></td>' +
        '<td style="font-weight:bold;color:var(--accent)">' + esc(j.token) + '</td>' +
        '<td class="right">' + fmtPrice(j.price) + '</td>' +
        '<td>' + esc(j.tier || '—') + '</td>' +
        '<td class="right ' + pnlCls + '">' + pnlStr + '</td>' +
        '<td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" class="muted" title="' + esc(fullReasonWithHold) + '">' + esc(reasonWithHold) + '</td>' +
        '</tr>';
    });
    if (reset) { tbody.innerHTML = html; } else { tbody.innerHTML += html; }
    if (_jHasMore && items.length) {
      const btn = document.getElementById('j-more');
      if (!btn) {
        const b = document.createElement('div');
        b.id = 'j-more'; b.style.cssText = 'text-align:center;padding:8px;cursor:pointer;color:var(--accent);font-size:12px';
        b.textContent = '▼ 加载更多'; b.onclick = () => loadJournal(false);
        $('#journal-empty').parentElement.appendChild(b);
      }
    } else { const btn = document.getElementById('j-more'); if (btn) btn.remove(); }
  } catch(e) { console.error('journal', e); }
}

async function refreshAll() {
  await Promise.all([loadDailyCard(), loadOverview(), loadPositions(), loadPending(), loadLessons(true), loadTimeline(true), loadJournal(true)]);
}

updateClock();
setInterval(updateClock, 1000);
refreshAll();
setInterval(loadDailyCard, 300000);
setInterval(loadOverview, 15000);
setInterval(loadPositions, 5000);
setInterval(loadPending, 30000);
</script>
</body>
</html>
"""

# 热度 Agent 面板 —— 从 Agent 面板复制，strategy 替换为 heat_agent
HEAT_AGENT_HTML = AGENT_HTML.replace(
    "<title>Agent-合约扫描</title>", "<title>Agent-热度榜单</title>"
).replace(
    "<h1>Agent-合约扫描</h1>", "<h1>Agent-热度榜单</h1>"
).replace(
    "strategy_initial_agent", "strategy_initial_heat_agent"
).replace(
    "p.strategy === 'agent'", "p.strategy === 'heat_agent'"
).replace(
    "const AGENT_STRATEGY = 'agent'", "const AGENT_STRATEGY = 'heat_agent'"
).replace(
    "'/api/agent/overview'", "'/api/agent/overview?strategy='+AGENT_STRATEGY"
).replace(
    "'/api/agent/journal?limit='", "'/api/agent/journal?strategy='+AGENT_STRATEGY+'&limit='"
).replace(
    "'/api/agent/timeline?limit='", "'/api/agent/timeline?strategy='+AGENT_STRATEGY+'&limit='"
).replace(
    "'/api/agent/lessons?limit='", "'/api/agent/lessons?strategy='+AGENT_STRATEGY+'&limit='"
).replace(
    '<a href="/agent" class="active">', '<a href="/agent">'
).replace(
    '<a href="/heat-agent">', '<a href="/heat-agent" class="active">'
)

HEAT_LESSONS_HTML = AGENT_HTML.replace(
    "<title>Agent-合约扫描</title>", "<title>Agent-热度有教训</title>"
).replace(
    "<h1>Agent-合约扫描</h1>", "<h1>Agent-热度有教训</h1>"
).replace(
    "strategy_initial_agent", "strategy_initial_heat_agent_lessons"
).replace(
    "p.strategy === 'agent'", "p.strategy === 'heat_agent_lessons'"
).replace(
    "const AGENT_STRATEGY = 'agent'", "const AGENT_STRATEGY = 'heat_agent_lessons'"
).replace(
    "'/api/agent/overview'", "'/api/agent/overview?strategy='+AGENT_STRATEGY"
).replace(
    "'/api/agent/journal?limit='", "'/api/agent/journal?strategy='+AGENT_STRATEGY+'&limit='"
).replace(
    "'/api/agent/timeline?limit='", "'/api/agent/timeline?strategy='+AGENT_STRATEGY+'&limit='"
).replace(
    "'/api/agent/lessons?limit='", "'/api/agent/lessons?strategy='+AGENT_STRATEGY+'&limit='"
).replace(
    '<a href="/agent" class="active">', '<a href="/agent">'
).replace(
    '<a href="/agent-heat-lessons">', '<a href="/agent-heat-lessons" class="active">'
)

NL_AGENT_HTML = AGENT_HTML.replace(
    "<title>Agent-合约扫描</title>", "<title>Agent-无教训对照</title>"
).replace(
    "<h1>Agent-合约扫描</h1>", "<h1>Agent-无教训对照</h1>"
).replace(
    "strategy_initial_agent", "strategy_initial_agent_no_lessons"
).replace(
    "p.strategy === 'agent'", "p.strategy === 'agent_no_lessons'"
).replace(
    "const AGENT_STRATEGY = 'agent'", "const AGENT_STRATEGY = 'agent_no_lessons'"
).replace(
    "'/api/agent/overview'", "'/api/agent/overview?strategy='+AGENT_STRATEGY"
).replace(
    "'/api/agent/journal?limit='", "'/api/agent/journal?strategy='+AGENT_STRATEGY+'&limit='"
).replace(
    "'/api/agent/timeline?limit='", "'/api/agent/timeline?strategy='+AGENT_STRATEGY+'&limit='"
).replace(
    "'/api/agent/lessons?limit='", "'/api/agent/lessons?strategy='+AGENT_STRATEGY+'&limit='"
).replace(
    '<a href="/agent" class="active">', '<a href="/agent">'
).replace(
    '<a href="/agent-nl">', '<a href="/agent-nl" class="active">'
)

AGENT_SHORT_HTML = AGENT_HTML.replace(
    "<title>Agent-合约扫描</title>", "<title>Agent-做空版</title>"
).replace(
    "<h1>Agent-合约扫描</h1>", "<h1>Agent-做空版</h1>"
).replace(
    "strategy_initial_agent", "strategy_initial_agent_short"
).replace(
    "p.strategy === 'agent'", "p.strategy === 'agent_short'"
).replace(
    "const AGENT_STRATEGY = 'agent'", "const AGENT_STRATEGY = 'agent_short'"
).replace(
    "'/api/agent/overview'", "'/api/agent/overview?strategy='+AGENT_STRATEGY"
).replace(
    "'/api/agent/journal?limit='", "'/api/agent/journal?strategy='+AGENT_STRATEGY+'&limit='"
).replace(
    "'/api/agent/timeline?limit='", "'/api/agent/timeline?strategy='+AGENT_STRATEGY+'&limit='"
).replace(
    "'/api/agent/lessons?limit='", "'/api/agent/lessons?strategy='+AGENT_STRATEGY+'&limit='"
).replace(
    '<a href="/agent" class="active">', '<a href="/agent">'
).replace(
    '<a href="/agent-short">', '<a href="/agent-short" class="active">'
)


SETTINGS_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>系统设置 - BSM Agent</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Inter:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0d1117; --panel: #161b22; --text: #c9d1d9; --muted: #8b949e;
  --accent: #58a6ff; --border: #30363d; --red: #f85149; --green: #3fb950;
  --danger: #da3633;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: 'Inter',-apple-system,BlinkMacSystemFont,sans-serif; padding: 20px; max-width: 700px; margin: 0 auto; }
h1 { font-size: 20px; margin-bottom: 20px; }
h2 { font-size: 15px; margin: 0 0 12px; color: var(--accent); }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 16px; }
label { color: var(--muted); font-size: 12px; display: block; margin-bottom: 4px; }
input, select { width: 100%; box-sizing: border-box; background: #0a0e15; color: var(--text); border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; font-size: 14px; height: 40px; margin-bottom: 14px; vertical-align: middle; }
select { -webkit-appearance: none; appearance: none; background-image: url(\"data:image/svg+xml,%3csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3e%3cpath fill='none' stroke='%238b949e' stroke-linecap='round' stroke-linejoin='round' stroke-width='2' d='m2 5 6 6 6-6'/%3e%3c/svg%3e\"); background-repeat: no-repeat; background-position: right 8px center; background-size: 16px 12px; padding-right: 30px; }
input[type=\"number\"] { -moz-appearance: textfield; }
input[type=\"number\"]::-webkit-outer-spin-button,
input[type=\"number\"]::-webkit-inner-spin-button { -webkit-appearance: none; margin: 0; }
input:focus, select:focus { outline: none; border-color: var(--accent); }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.grid3 { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 14px; }
.btn { padding: 10px 20px; border: none; border-radius: 8px; cursor: pointer; font-size: 14px; font-weight: 500; }
.btn-save { background: var(--accent); color: #000; width: 100%; }
.btn-danger { background: var(--danger); color: #fff; }
.btn-warn { background: #da3633; color: #fff; }
.btn-row { display: flex; gap: 10px; margin-top: 16px; justify-content: center; flex-wrap: wrap; }
.toast { position: fixed; top: 20px; right: 20px; padding: 10px 18px; border-radius: 6px; color: #fff; font-size: 13px; z-index: 999; opacity: 0; transition: opacity 0.3s; }
.toast.show { opacity: 1; }
.toast.ok { background: var(--green); }
.toast.err { background: var(--red); }
.nav-bar {
  display: flex; justify-content: center; gap: 0; margin-bottom: 20px;
  border-bottom: 1px solid var(--border); padding-bottom: 0; flex-wrap: wrap;
}
.nav-bar a {
  padding: 8px 16px; color: var(--muted); text-decoration: none;
  font-size: 12px; font-weight: 500; border-bottom: 2px solid transparent;
  transition: all 0.2s; white-space: nowrap;
}
.nav-bar a.active, .nav-bar a:hover { color: var(--accent); border-bottom-color: var(--accent); }
.nav-dropdown { position: relative; }
.nav-dropdown > a { display: inline-block; padding: 8px 16px; color: var(--muted); text-decoration: none; font-size: 12px; font-weight: 500; border-bottom: 2px solid transparent; transition: all 0.2s; white-space: nowrap; }
.nav-dropdown > a:hover { color: var(--accent); border-bottom-color: var(--accent); }
.nav-dropdown-content { display: none; position: absolute; top: 100%; left: 0; background: var(--panel); border: 1px solid var(--border); border-radius: 6px; min-width: 140px; z-index: 100; padding: 4px 0; }
.nav-dropdown-content a { display: block; padding: 6px 14px; color: var(--text); border-bottom: none !important; font-size: 12px; white-space: nowrap; }
.nav-dropdown-content a:hover { background: #1a1f2e; color: var(--accent) !important; }
.nav-dropdown:hover .nav-dropdown-content { display: block; }
.strategy-block {
  border-bottom: 1px solid var(--border); padding: 12px 0;
}
.strategy-block:first-of-type { padding-top: 0; }
.strategy-block:last-of-type { border-bottom: none; padding-bottom: 0; }
.strategy-name {
  color: var(--muted); font-size: 12px; font-weight: 500;
  margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.5px;
}
</style>
</head>
<body>
<div class="nav-bar">
  <a href="/">📊 市场监控</a>
  <a href="/strategies">📈 运行策略</a>
  <div class="nav-dropdown">
  <a href="#">🤖 Agent 面板 ▾</a>
  <div class="nav-dropdown-content">
    <a href="/agent">Agent-合约扫描</a>
    <a href="/heat-agent">Agent-热度榜单</a>
    <a href="/agent-heat-lessons">Agent-热度有教训</a>
    <a href="/agent-nl">Agent-无教训</a>
                <a href="/agent-short">Agent-做空</a>
    <a href="/agent-kol">Agent-KOL 监控</a>
              <a href="/agent-kol-snapshot">Agent-KOL 快照监控</a>
  </div>
</div>
  <a href="/dashboard">📊 市场全景</a>
  <a href="/onchain">🔍 链上监控</a>
  <a href="/settings" class="active">⚙ 系统设置</a>
</div>

<h1>⚙ 系统设置</h1>

<div class="panel">
  <h2>系统参数</h2>
  <div class="grid3">
    <div>
      <label>自动交易</label>
      <select id="trade-enabled">
        <option value="false">关闭</option>
        <option value="true">开启</option>
      </select>
    </div>
    <div>
      <label>交易模式</label>
      <select id="trade-mode">
        <option value="paper">模拟</option>
        <option value="live">实盘（暂未启用）</option>
      </select>
    </div>
    <div>
      <label>日交易限制（每策略15次）</label>
      <select id="trade-daily-limit-switch">
        <option value="true">启用</option>
        <option value="false">关闭</option>
      </select>
    </div>
  </div>
  <div class="grid3">
    <div>
      <label>账户初始金额 USDT</label>
      <input id="trade-initial" type="number" min="1" step="1">
    </div>
    <div>
      <label>交易倍数</label>
      <input id="trade-leverage" type="number" min="1" max="125" step="1">
    </div>
    <div>
      <label>挂单超时（秒）</label>
      <input id="trade-limit-timeout" type="number" min="60" max="7200" step="60">
    </div>
  </div>
</div>

<div class="panel">
  <h2>数据源</h2>
  <div class="grid3">
    <div>
      <label>数据来源</label>
      <select id="trade-data-source">
        <option value="false">币安广场</option>
        <option value="true">OKX</option>
      </select>
    </div>
  </div>
  <div style="color:var(--muted);font-size:11px;margin-top:4px">切换数据源后需重启系统生效</div>
</div>

<div class="panel">
  <h2>链上监控</h2>
  <div class="grid3">
    <div>
      <label>扫描间隔（小时）</label>
      <input id="trade-onchain-interval" type="number" min="0.5" max="24" step="0.5">
    </div>
    <div>
      <label>背离阈值（资金同源%）</label>
      <input id="trade-onchain-fund-min" type="number" min="10" max="80" step="5">
    </div>
    <div>
      <label>表层集中度上限（%）</label>
      <input id="trade-onchain-top10-max" type="number" min="10" max="80" step="5">
    </div>
    <div>
      <label>观察池最长天数</label>
      <input id="trade-onchain-max-days" type="number" min="3" max="30" step="1">
    </div>
  </div>
  <div style="color:var(--muted);font-size:11px;margin-top:4px">修改后点击「保存设置」立即生效，无需重启</div>
</div>

<div class="panel">
  <h2>集群监控</h2>
  <div class="grid3">
    <div>
      <label>数据采集开关</label>
      <select id="trade-cluster-enabled">
        <option value="true">启用</option>
        <option value="false">关闭</option>
      </select>
    </div>
    <div>
      <label>标签计算开关</label>
      <select id="trade-cluster-analysis-switch">
        <option value="true">启用</option>
        <option value="false">关闭</option>
      </select>
    </div>
    <div>
      <label>分析间隔（轮）</label>
      <input id="trade-cluster-interval" type="number" min="1" max="48" step="1">
    </div>
    <div>
      <label>每集群分析地址数</label>
      <input id="trade-cluster-top-addresses" type="number" min="1" max="20" step="1">
    </div>
    <div>
      <label>最小集群持仓%</label>
      <input id="trade-cluster-min-holding" type="number" min="0.1" max="10" step="0.1">
    </div>
    <div>
      <label>数据保留轮数</label>
      <input id="trade-cluster-keep-rounds" type="number" min="50" max="5000" step="50">
    </div>
  </div>
  <div style="color:var(--muted);font-size:11px;margin-top:4px">修改后点击「保存设置」立即生效，无需重启</div>
</div>

<div class="panel">
  <h2>策略配置</h2>

  <div class="strategy-block">
    <div class="strategy-name">Agent-合约扫描</div>
    <div class="grid3">
      <div>
        <label>开关</label>
        <select id="trade-agent-switch">
          <option value="true">启用</option>
          <option value="false">关闭</option>
        </select>
      </div>
      <div>
        <label>初始金额 USDT</label>
        <input id="trade-strategy-agent" type="number" min="1" step="1">
      </div>
      <div>
        <label>收集间隔（分钟）</label>
        <input id="trade-collect-interval" type="number" min="5" max="60" step="1">
      </div>
    </div>
  </div>

  <div class="strategy-block">
    <div class="strategy-name">Agent-热度榜单</div>
    <div class="grid3">
      <div>
        <label>开关</label>
        <select id="trade-heat-switch">
          <option value="true">启用</option>
          <option value="false">关闭</option>
        </select>
      </div>
      <div>
        <label>初始金额 USDT</label>
        <input id="trade-strategy-heat" type="number" min="1" step="1">
      </div>
      <div>
        <label>触发间隔（轮）</label>
        <input id="trade-trigger-interval" type="number" min="1" max="10" step="1">
      </div>
    </div>
  </div>

  <div class="strategy-block">
    <div class="strategy-name">Agent-热度有教训</div>
    <div class="grid3">
      <div>
        <label>开关</label>
        <select id="trade-heat-lessons-switch">
          <option value="true">启用</option>
          <option value="false">关闭</option>
        </select>
      </div>
      <div>
        <label>初始金额 USDT</label>
        <input id="trade-strategy-heat-lessons" type="number" min="1" step="1">
      </div>
      <div>
        <label>触发间隔（轮）</label>
        <input id="trade-heat-lessons-interval" type="number" min="1" max="20" step="1">
      </div>
    </div>
  </div>

  <div class="strategy-block">
    <div class="strategy-name">Agent-无教训</div>
    <div class="grid3">
      <div>
        <label>开关</label>
        <select id="trade-nl-switch">
          <option value="true">启用</option>
          <option value="false">关闭</option>
        </select>
      </div>
      <div>
        <label>初始金额 USDT</label>
        <input id="trade-strategy-nl" type="number" min="1" step="1">
      </div>
      <div>
        <label>触发间隔（分钟）</label>
        <input id="trade-nl-interval" type="number" min="10" max="480" step="5">
      </div>
    </div>
  </div>

  <div class="strategy-block">
    <div class="strategy-name">Agent-KOL</div>
    <div class="grid3">
      <div>
        <label>开关</label>
        <select id="trade-kol-switch">
          <option value="true">启用</option>
          <option value="false">关闭</option>
        </select>
      </div>
      <div>
        <label>初始金额 USDT</label>
        <input id="trade-strategy-kol" type="number" min="1" step="1">
      </div>
      <div>
        <label>触发间隔（分钟）</label>
        <input id="trade-kol-interval" type="number" min="10" max="480" step="5">
      </div>
    </div>
    <div class="grid3">
      <div>
        <label>LLM 提供商</label>
        <select id="trade-kol-provider">
          <option value="deepseek">DeepSeek</option>
          <option value="nvidia">NVIDIA</option>
        </select>
      </div>
      <div>
        <label>最低信心分</label>
        <input id="trade-kol-min-conf" type="number" min="50" max="95" step="5">
      </div>
      <div>
        <label>Token 冷却时间（分钟）</label>
        <input id="trade-kol-cooldown" type="number" min="1" max="1440" step="5">
      </div>
    </div>
  </div>

  <div class="strategy-block">
    <div class="strategy-name">Agent-KOL认知快照</div>
    <div class="grid3">
      <div>
        <label>开关</label>
        <select id="trade-kol-snapshot-switch">
          <option value="true">启用</option>
          <option value="false">关闭</option>
        </select>
      </div>
      <div>
        <label>初始金额 USDT</label>
        <input id="trade-strategy-kol-snapshot" type="number" min="1" step="1">
      </div>
      <div>
        <label>触发间隔（分钟）</label>
        <input id="trade-kol-snapshot-interval" type="number" min="10" max="480" step="5">
      </div>
    </div>
    <div>
      <label>LLM 提供商</label>
      <select id="trade-kol-snapshot-provider">
        <option value="deepseek">DeepSeek</option>
        <option value="nvidia">NVIDIA</option>
      </select>
    </div>
  </div>

  <div class="strategy-block">
    <div class="strategy-name">Agent-做空版</div>
    <div class="grid3">
      <div>
        <label>开关</label>
        <select id="trade-short-switch">
          <option value="true">启用</option>
          <option value="false">关闭</option>
        </select>
      </div>
      <div>
        <label>初始金额 USDT</label>
        <input id="trade-strategy-short" type="number" min="1" step="1">
      </div>
      <div>
        <label>触发间隔（分钟）</label>
        <input id="trade-short-interval" type="number" min="5" max="480" step="5">
      </div>
    </div>
  </div>

  <div class="strategy-block">
    <div class="strategy-name">链上监控策略</div>
    <div class="grid3">
      <div>
        <label>自动开仓</label>
        <select id="trade-onchain-strategy-switch">
          <option value="true">启用</option>
          <option value="false">关闭</option>
        </select>
      </div>
      <div>
        <label>扫描开关</label>
        <select id="trade-onchain-scan-switch">
          <option value="true">启用</option>
          <option value="false">关闭</option>
        </select>
      </div>
      <div>
        <label>初始金额 USDT</label>
        <input id="trade-strategy-onchain" type="number" min="1" step="1">
      </div>
      <div>
        <label>交易模式</label>
        <select id="trade-onchain-live-mode">
          <option value="paper">模拟 (Paper)</option>
          <option value="live">实盘 (Live)</option>
        </select>
      </div>
    </div>
  </div>

  <div class="strategy-block">
    <div class="strategy-name">系统策略</div>
    <div class="grid3">
      <div>
        <label>自动开仓</label>
        <select id="trade-system-auto-switch">
          <option value="true">启用</option>
          <option value="false">关闭</option>
        </select>
      </div>
      <div>
        <label>初始金额 USDT</label>
        <input id="trade-strategy-system" type="number" min="1" step="1">
      </div>
      <div></div>
    </div>
  </div>

  <div class="strategy-block">
    <div class="strategy-name">手动策略</div>
    <div>
      <label>初始金额 USDT（手动触发，无自动开关）</label>
      <input id="trade-strategy-manual" type="number" min="1" step="1">
    </div>
  </div>
</div>

<div class="panel">
  <h2>系统操作</h2>
  <div>
    <label>AI 研判间隔（分钟）</label>
    <input id="trade-ai-regime-interval" type="number" min="10" max="240" step="5">
  </div>
  <div class="grid2" style="margin-top:14px">
    <div>
      <label>策略重置</label>
      <select id="trade-reset-strategy">
        <option value="agent">Agent-合约扫描</option>
        <option value="heat_agent">Agent-热度榜单</option>
        <option value="heat_agent_lessons">Agent-热度有教训</option>
        <option value="agent_no_lessons">Agent-无教训</option>
        <option value="kol_agent">Agent-KOL</option>
        <option value="kol_snapshot">Agent-KOL认知快照</option>
        <option value="system">系统自动</option>
        <option value="manual">手动</option>
        <option value="agent_short">Agent-做空版</option>
        <option value="onchain">链上监控</option>
      </select>
    </div>
    <div>
      <label>重置后初始金额（留空不修改）</label>
      <input id="trade-reset-balance" type="number" min="1" placeholder="留空不修改">
    </div>
  </div>
  <div class="btn-row">
    <button class="btn btn-danger" onclick="resetStrategyAccount()">重置该策略</button>
    <button class="btn btn-danger" onclick="resetTradingAccount()">重置全部账户</button>
  </div>
  <div class="btn-row" style="margin-top:8px">
    <button class="btn btn-warn" onclick="restartSystem()">重启系统</button>
  </div>
  <div style="color:var(--muted);font-size:11px;margin-top:12px;text-align:center;line-height:1.5">
    策略重置：只清该策略的持仓/决策/候选池，保留教训和日志<br>
    全部重置：清除所有策略数据<br>
    重启系统：强制终止所有进程，等 11 分钟后自动重启
  </div>
</div>

<div class="btn-row">
  <button class="btn btn-save" onclick="saveTradingSettings()">保存设置</button>
</div>

<div class="toast" id="toast"></div>

<script>
const escHtml = s => s != null ? String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;') : '';
function showToast(msg, kind = 'ok', d = 2500) {
  const el = document.getElementById('toast');
  el.className = 'toast ' + kind; el.textContent = msg;
  requestAnimationFrame(() => el.classList.add('show'));
  clearTimeout(el._t); el._t = setTimeout(() => el.classList.remove('show'), d);
}
async function loadSettings() {
  try {
    const r = await fetch('/api/settings');
    const s = await r.json();
    document.getElementById('trade-enabled').value = s.enabled ? 'true' : 'false';
    document.getElementById('trade-mode').value = s.mode || 'paper';
    document.getElementById('trade-initial').value = s.initial_balance ?? '';
    document.getElementById('trade-leverage').value = s.leverage ?? '';
    document.getElementById('trade-collect-interval').value = s.agent_collect_interval_minutes ?? 15;
    document.getElementById('trade-trigger-interval').value = s.agent_trigger_interval ?? 3;
    document.getElementById('trade-kol-interval').value = s.kol_agent_interval_minutes ?? 15;
    document.getElementById('trade-nl-interval').value = s.nl_agent_interval_minutes ?? 30;
    document.getElementById('trade-heat-lessons-interval').value = s.heat_agent_lessons_trigger_interval ?? 3;
    document.getElementById('trade-ai-regime-interval').value = s.ai_regime_interval_minutes ?? 30;
    document.getElementById('trade-limit-timeout').value = s.limit_order_timeout_seconds ?? 600;
    document.getElementById('trade-kol-provider').value = s.kol_llm_provider || 'deepseek';
    document.getElementById('trade-kol-snapshot-provider').value = s.kol_snapshot_llm_provider || 'nvidia';
    document.getElementById('trade-agent-switch').value = s.agent_trade_enabled ?? true;
    document.getElementById('trade-heat-switch').value = s.heat_agent_enabled ?? true;
    document.getElementById('trade-kol-switch').value = s.kol_agent_enabled ?? true;
    document.getElementById('trade-kol-snapshot-switch').value = s.kol_snapshot_enabled ?? true;
    document.getElementById('trade-kol-snapshot-interval').value = s.kol_snapshot_interval_minutes ?? 8;
    document.getElementById('trade-strategy-kol-snapshot').value = s.strategy_initial_kol_snapshot ?? 1000;
    document.getElementById('trade-nl-switch').value = s.nl_agent_enabled ?? true;
    document.getElementById('trade-heat-lessons-switch').value = s.heat_agent_lessons_enabled ?? true;
    document.getElementById('trade-short-switch').value = s.agent_short_enabled ?? true;
    document.getElementById('trade-short-interval').value = s.agent_short_interval_minutes ?? 16;
    document.getElementById('trade-strategy-short').value = s.strategy_initial_agent_short ?? 1000;
    document.getElementById('trade-onchain-strategy-switch').value = s.onchain_strategy_enabled ?? true;
    document.getElementById('trade-onchain-scan-switch').value = s.onchain_scan_enabled ?? true;
    document.getElementById('trade-strategy-onchain').value = s.strategy_initial_onchain ?? 1000;
    document.getElementById('trade-kol-min-conf').value = s.kol_agent_min_confidence ?? 70;
    document.getElementById('trade-kol-cooldown').value = s.kol_token_cooldown_minutes ?? 30;
    document.getElementById('trade-daily-limit-switch').value = s.trading_daily_limit_enabled ?? true;
    document.getElementById('trade-system-auto-switch').value = s.system_auto_trade_enabled ?? true;
    document.getElementById('trade-data-source').value = s.okx_data_source_enabled ?? false;
    document.getElementById('trade-data-source').setAttribute('data-original', (s.okx_data_source_enabled ?? false) ? 'true' : 'false');
    document.getElementById('trade-onchain-interval').value = s.onchain_scan_interval_hours ?? 4;
    document.getElementById('trade-onchain-fund-min').value = s.onchain_divergence_fund_source_min ?? 30;
    document.getElementById('trade-onchain-top10-max').value = s.onchain_surface_top10_max ?? 40;
    document.getElementById('trade-onchain-max-days').value = s.onchain_observation_max_days ?? 7;
    document.getElementById('trade-cluster-enabled').value = s.onchain_cluster_enabled ?? true;
    document.getElementById('trade-cluster-analysis-switch').value = s.onchain_cluster_analysis_enabled ?? true;
    document.getElementById('trade-cluster-interval').value = s.onchain_cluster_analysis_interval ?? 1;
    document.getElementById('trade-cluster-top-addresses').value = s.onchain_cluster_top_addresses ?? 5;
    document.getElementById('trade-cluster-min-holding').value = s.onchain_cluster_min_holding_pct ?? 1.0;
    document.getElementById('trade-cluster-keep-rounds').value = s.onchain_cluster_keep_rounds ?? 1500;
    document.getElementById('trade-onchain-live-mode').value = s.onchain_live_mode || 'paper';
    document.getElementById('trade-strategy-agent').value = s.strategy_initial_agent ?? 1000;
    document.getElementById('trade-strategy-heat').value = s.strategy_initial_heat_agent ?? 1000;
    document.getElementById('trade-strategy-system').value = s.strategy_initial_system ?? 1000;
    document.getElementById('trade-strategy-manual').value = s.strategy_initial_manual ?? 1000;
    document.getElementById('trade-strategy-kol').value = s.strategy_initial_kol_agent ?? 1000;
    document.getElementById('trade-strategy-nl').value = s.strategy_initial_agent_no_lessons ?? 1000;
    document.getElementById('trade-strategy-heat-lessons').value = s.strategy_initial_heat_agent_lessons ?? 1000;
  } catch(e) { console.error(e); }
}
async function saveTradingSettings() {
  const body = {
    enabled: document.getElementById('trade-enabled').value === 'true',
    mode: document.getElementById('trade-mode').value,
    initial_balance: Number(document.getElementById('trade-initial').value),
    leverage: Number(document.getElementById('trade-leverage').value),
    strategy_initial_agent: Number(document.getElementById('trade-strategy-agent').value),
    strategy_initial_heat_agent: Number(document.getElementById('trade-strategy-heat').value),
    strategy_initial_system: Number(document.getElementById('trade-strategy-system').value),
    strategy_initial_manual: Number(document.getElementById('trade-strategy-manual').value),
    strategy_initial_kol_agent: Number(document.getElementById('trade-strategy-kol').value),
    strategy_initial_kol_snapshot: Number(document.getElementById('trade-strategy-kol-snapshot').value),
    strategy_initial_agent_no_lessons: Number(document.getElementById('trade-strategy-nl').value),
    strategy_initial_heat_agent_lessons: Number(document.getElementById('trade-strategy-heat-lessons').value),
    agent_collect_interval_minutes: Number(document.getElementById('trade-collect-interval').value),
    agent_trigger_interval: Number(document.getElementById('trade-trigger-interval').value),
    kol_agent_interval_minutes: Number(document.getElementById('trade-kol-interval').value),
    kol_snapshot_interval_minutes: Number(document.getElementById('trade-kol-snapshot-interval').value),
    nl_agent_interval_minutes: Number(document.getElementById('trade-nl-interval').value),
    heat_agent_lessons_trigger_interval: Number(document.getElementById('trade-heat-lessons-interval').value),
    ai_regime_interval_minutes: Number(document.getElementById('trade-ai-regime-interval').value),
    limit_order_timeout_seconds: Number(document.getElementById('trade-limit-timeout').value),
    kol_llm_provider: document.getElementById('trade-kol-provider').value,
    kol_snapshot_llm_provider: document.getElementById('trade-kol-snapshot-provider').value,
    agent_trade_enabled: document.getElementById('trade-agent-switch').value === 'true',
    heat_agent_enabled: document.getElementById('trade-heat-switch').value === 'true',
    kol_agent_enabled: document.getElementById('trade-kol-switch').value === 'true',
    kol_snapshot_enabled: document.getElementById('trade-kol-snapshot-switch').value === 'true',
    nl_agent_enabled: document.getElementById('trade-nl-switch').value === 'true',
    heat_agent_lessons_enabled: document.getElementById('trade-heat-lessons-switch').value === 'true',
    agent_short_enabled: document.getElementById('trade-short-switch').value === 'true',
    strategy_initial_agent_short: Number(document.getElementById('trade-strategy-short').value),
    agent_short_interval_minutes: Number(document.getElementById('trade-short-interval').value),
    onchain_strategy_enabled: document.getElementById('trade-onchain-strategy-switch').value === 'true',
    onchain_scan_enabled: document.getElementById('trade-onchain-scan-switch').value === 'true',
    strategy_initial_onchain: Number(document.getElementById('trade-strategy-onchain').value),
    kol_agent_min_confidence: parseInt(document.getElementById('trade-kol-min-conf').value) || 70,
    kol_token_cooldown_minutes: parseInt(document.getElementById('trade-kol-cooldown').value) || 30,
    trading_daily_limit_enabled: document.getElementById('trade-daily-limit-switch').value === 'true',
    system_auto_trade_enabled: document.getElementById('trade-system-auto-switch').value === 'true',
    okx_data_source_enabled: document.getElementById('trade-data-source').value === 'true',
    onchain_scan_interval_hours: Number(document.getElementById('trade-onchain-interval').value),
    onchain_divergence_fund_source_min: Number(document.getElementById('trade-onchain-fund-min').value),
    onchain_surface_top10_max: Number(document.getElementById('trade-onchain-top10-max').value),
    onchain_observation_max_days: Number(document.getElementById('trade-onchain-max-days').value),
    onchain_cluster_enabled: document.getElementById('trade-cluster-enabled').value === 'true',
    onchain_cluster_analysis_enabled: document.getElementById('trade-cluster-analysis-switch').value === 'true',
    onchain_cluster_analysis_interval: Number(document.getElementById('trade-cluster-interval').value),
    onchain_cluster_top_addresses: Number(document.getElementById('trade-cluster-top-addresses').value),
    onchain_cluster_min_holding_pct: Number(document.getElementById('trade-cluster-min-holding').value),
    onchain_cluster_keep_rounds: Number(document.getElementById('trade-cluster-keep-rounds').value),
    onchain_live_mode: document.getElementById('trade-onchain-live-mode').value,
  };
  const oldSource = document.getElementById('trade-data-source').getAttribute('data-original') || '';
  const newSource = body.okx_data_source_enabled ? 'true' : 'false';
  try {
    const resp = await fetch('/api/trading/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    if (!resp.ok) { const err = await resp.text(); throw new Error(err); }
    showToast('设置已保存', 'ok');
    // 数据源变了 → 提示重启
    if (oldSource && newSource !== oldSource) {
      if (confirm('数据源已切换，需要重启系统生效。是否立即重启？')) {
        await fetch('/api/system/restart', {method:'POST'});
        showToast('系统重启中，约 11 分钟后恢复...', 'ok', 5000);
      } else {
        showToast('设置已保存，手动重启后生效', 'ok');
      }
    }
    // 更新标记，下次保存用新值对比
    document.getElementById('trade-data-source').setAttribute('data-original', newSource);
  } catch(e) { showToast('保存失败：'+e.message, 'err'); }
}
async function resetStrategyAccount() {
  const sel = document.getElementById('trade-reset-strategy');
  const strategy = sel.value;
  const name = sel.options[sel.selectedIndex].text;
  if (!confirm('确定要重置「' + name + '」吗？\\n\\n将清空该策略的持仓、决策、候选池，保留教训和日志。\\n\\n此操作不可撤销！')) return;
  const balInput = document.getElementById('trade-reset-balance');
  const bal = balInput.value ? parseFloat(balInput.value) : null;
  if (bal !== null && (isNaN(bal) || bal <= 0)) { showToast('金额必须为正数', 'err'); return; }
  try {
    const resp = await fetch('/api/trading/reset-strategy', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({strategy: strategy, confirm: true, new_initial_balance: bal}),
    });
    if (!resp.ok) { const err = await resp.text(); throw new Error(err); }
    const data = await resp.json();
    showToast(
      '「' + name + '」已重置：清除 ' + data.positions_deleted + ' 条持仓 / ' +
      data.locks_deleted + ' 条锁 / ' + data.decisions_deleted + ' 条决策 / ' +
      data.candidates_deleted + ' 条候选',
      'ok', 3000
    );
    loadSettings();
  } catch(e) { showToast('重置失败：'+e.message, 'err'); }
}
async function resetTradingAccount() {
  if (!confirm('确定要重置账户吗？\\n\\n将清空所有持仓和历史记录，保留教训和操作日志。')) return;
  const cur = document.getElementById('trade-initial').value || 1000;
  const input = prompt('请输入重置后的账户初始金额 USDT (回车保持 '+cur+'):', cur);
  if (input === null) return;
  const v = parseFloat(input);
  if (!v || v <= 0) { showToast('金额必须为正数', 'err'); return; }
  try {
    const resp = await fetch('/api/trading/reset', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({confirm:true, new_initial_balance:v})});
    if (!resp.ok) { const err = await resp.text(); throw new Error(err); }
    document.getElementById('trade-initial').value = v;
    showToast('账户已重置', 'ok');
  } catch(e) { showToast('重置失败：'+e.message, 'err'); }
}
async function restartSystem() {
  if (!confirm('确定要重启系统吗？\\n\\n所有进程将被强制终止，等待 60 秒后自动重启。\\n\\n重启期间约 90 秒不可用。')) return;
  try {
    showToast('正在重启...', 'ok');
    const resp = await fetch('/api/system/restart', {method:'POST'});
    const d = await resp.json();
    showToast(d.msg || '已触发重启', 'ok');
  } catch(e) { showToast('重启指令发送失败', 'err'); }
}
loadSettings();
</script>
</body>
</html>
"""

@app.get("/settings", response_class=HTMLResponse)
def settings_page():
    return HTMLResponse(
        content=SETTINGS_HTML,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


@app.get("/api/strategy/stats")
def api_strategy_stats():
    with storage.get_conn() as conn:
        strategies = [dict(r) for r in conn.execute("""
            SELECT strategy,
                   COUNT(CASE WHEN status='CLOSED' THEN 1 END) as total,
                   SUM(CASE WHEN status='CLOSED' AND realized_pnl>0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN status='CLOSED' AND realized_pnl<=0 THEN 1 ELSE 0 END) as losses,
                   ROUND(SUM(CASE WHEN status='CLOSED' THEN realized_pnl ELSE 0 END),2) as total_pnl,
                   ROUND(AVG(CASE WHEN status='CLOSED' THEN pnl_pct END),2) as avg_pnl,
                   SUM(CASE WHEN status IN('OPEN','PARTIAL') THEN 1 ELSE 0 END) as open_count,
                   COUNT(CASE WHEN status='CLOSED' AND closed_at >= datetime('now', '-7 days') THEN 1 END) as total_7d,
                   SUM(CASE WHEN status='CLOSED' AND closed_at >= datetime('now', '-7 days') AND realized_pnl>0 THEN 1 ELSE 0 END) as wins_7d
            FROM trade_positions WHERE strategy IS NOT NULL
            GROUP BY strategy ORDER BY strategy
        """)]
        # 止损失败标签按策略分组
        import json as _json
        loss_tags = {}
        for r in conn.execute("""
            SELECT tp.strategy, la.reason_tags
            FROM trade_loss_archive la
            JOIN trade_positions tp ON la.position_id = tp.id
            WHERE la.reason_tags IS NOT NULL
        """):
            s = r["strategy"] or "unknown"
            if s not in loss_tags:
                loss_tags[s] = {}
            for t in _json.loads(r["reason_tags"]):
                loss_tags[s][t] = loss_tags[s].get(t, 0) + 1
        # 各策略当前持仓 + 最近平仓
        positions = [dict(r) for r in conn.execute(
            "SELECT strategy, token, side, status, entry_price, current_price, "
            "pnl_pct, realized_pnl, unrealized_pnl, stop_loss_price, tp1_price, tp2_price, "
            "margin_amount, leverage, closed_qty, quantity, order_type, created_at, closed_at "
            "FROM trade_positions WHERE strategy IS NOT NULL "
            "ORDER BY id DESC LIMIT 500"
        )]
        # 为 KOL 策略持仓补 trend + confidence（kol_analyses 在 agent DB）
        with storage.get_conn(config.KOL_DB) as kol_conn, \
             storage.get_conn(config.SNAPSHOT_DB) as snap_conn:
            for p in positions:
                if p.get("strategy") not in ("kol_agent", "kol_snapshot"):
                    continue
                c = snap_conn if p.get("strategy") == "kol_snapshot" else kol_conn
                row = c.execute(
                    "SELECT trend, confidence FROM kol_analyses WHERE token=? AND created_at <= ? ORDER BY id DESC LIMIT 1",
                    (p["token"], p["created_at"]),
                ).fetchone()
                if row:
                    p["entry_trend"] = row["trend"]
                    p["entry_confidence"] = row["confidence"]
                row = c.execute(
                    "SELECT trend, confidence FROM kol_analyses WHERE token=? ORDER BY id DESC LIMIT 1",
                    (p["token"],),
                ).fetchone()
                if row:
                    p["latest_trend"] = row["trend"]
                    p["latest_confidence"] = row["confidence"]
        for s in strategies:
            s["loss_tags"] = loss_tags.get(s["strategy"], {})
            s["account"] = trade_logic.strategy_account_summary(conn, s["strategy"])
            st = s["strategy"]
            s["open_positions"] = [p for p in positions if p["strategy"] == st and p["status"] in ("OPEN","PARTIAL")]
            s["closed_positions"] = [p for p in positions if p["strategy"] == st and p["status"] == "CLOSED"][:10]
        # 各策略运行状态（从 trading_settings 读取开关）
        ts = storage.trading_settings_get(conn)
        enabled_map = {
            "agent": ts.get("agent_trade_enabled", True),
            "heat_agent": ts.get("heat_agent_enabled", True),
            "heat_agent_lessons": ts.get("heat_agent_lessons_enabled", True),
            "agent_no_lessons": ts.get("nl_agent_enabled", True),
            "agent_short": ts.get("agent_short_enabled", True),
            "kol_agent": ts.get("kol_agent_enabled", True),
            "kol_snapshot": ts.get("kol_snapshot_enabled", True),
            "system": ts.get("system_auto_trade_enabled", True),
            "onchain": ts.get("onchain_strategy_enabled", True),
        }
        for s in strategies:
            s["running"] = enabled_map.get(s["strategy"], True)
            s["live_mode"] = ts.get("onchain_live_mode", "paper")
    return {"strategies": strategies}


# === KOL Agent API ===

# === 市场全景 Dashboard API ===

@app.get("/api/risk-metrics")
def api_risk_metrics():
    import dashboard
    return dashboard.get_risk_metrics()


@app.get("/api/macro-events")
def api_macro_events():
    import dashboard
    return dashboard.get_macro_events()


@app.get("/api/ai-regime")
def api_ai_regime():
    import dashboard
    return dashboard.get_ai_regime()


@app.post("/api/ai-regime/refresh")
def api_ai_regime_refresh():
    """强制刷新 AI 研判 — 清缓存立即触发后台 LLM"""
    import dashboard
    dashboard._ai_cache = {"ts": 0, "data": {}}
    dashboard._ai_loading = False
    alpha = dashboard.get_alpha_breadth()
    dashboard.refresh_ai_regime(alpha)
    return dashboard.get_ai_regime()


@app.get("/api/kol/llm-logs")
def api_kol_llm_logs(limit: int = 30, strategy: str = ""):
    db = config.SNAPSHOT_DB if strategy == "kol_snapshot" else config.KOL_DB
    with storage.get_conn(db) as conn:
        return storage.kol_llm_logs_recent(conn, limit, strategy=strategy)


@app.get("/api/kol/analyses")
def api_kol_analyses(symbol: str = "", strategy: str = ""):
    db = config.SNAPSHOT_DB if strategy == "kol_snapshot" else config.KOL_DB
    with storage.get_conn(db) as conn:
        rows = storage.kol_analyses_latest(conn, symbol=symbol, strategy=strategy)
    tokens = list(dict.fromkeys(r["token"] for r in rows))
    by_token = {}
    for r in rows:
        by_token.setdefault(r["token"], []).append(r)
    for t in by_token:
        by_token[t] = by_token[t][:8]
    return {"tokens": tokens, "by_token": by_token}


KOL_AGENT_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI 判断流详细版 - BSM Agent</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Inter:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0d1117; --panel: #161b22; --text: #c9d1d9; --muted: #8b949e;
  --accent: #58a6ff; --border: #30363d; --red: #f85149; --green: #3fb950;
  --yellow: #d29922; --purple: #a371f7; --orange: #d97706;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: 'Inter','JetBrains Mono',-apple-system,BlinkMacSystemFont,"Microsoft YaHei",sans-serif; padding: 20px; max-width: 1200px; margin: 0 auto; font-size: 13px; }
h1 { font-size: 20px; margin-bottom: 4px; }
h2 { font-size: 15px; margin: 0 0 12px; color: var(--accent); }
.nav-bar {
  display: flex; justify-content: center; gap: 0; margin-bottom: 16px;
  border-bottom: 1px solid var(--border); padding-bottom: 0; flex-wrap: wrap;
}
.nav-bar a {
  padding: 8px 16px; color: var(--muted); text-decoration: none;
  font-size: 12px; font-weight: 500; border-bottom: 2px solid transparent;
  transition: all 0.2s; white-space: nowrap;
}
.nav-bar a.active, .nav-bar a:hover { color: var(--accent); border-bottom-color: var(--accent); }
.nav-dropdown { position: relative; }
.nav-dropdown > a { display: inline-block; padding: 8px 16px; color: var(--muted); text-decoration: none; font-size: 12px; font-weight: 500; border-bottom: 2px solid transparent; transition: all 0.2s; white-space: nowrap; }
.nav-dropdown > a:hover { color: var(--accent); border-bottom-color: var(--accent); }
.nav-dropdown-content { display: none; position: absolute; top: 100%; left: 0; background: var(--panel); border: 1px solid var(--border); border-radius: 6px; min-width: 140px; z-index: 100; padding: 4px 0; }
.nav-dropdown-content a { display: block; padding: 6px 14px; color: var(--text); border-bottom: none !important; font-size: 12px; white-space: nowrap; }
.nav-dropdown-content a:hover { background: #1a1f2e; color: var(--accent) !important; }
.nav-dropdown:hover .nav-dropdown-content { display: block; }
.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.header-time { color: var(--muted); font-size: 12px; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 16px; margin-bottom: 14px; }

/* Zone 1: Metadata row */
.zone1 { margin-bottom: 8px; }
.zone1-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 4px; }
.card-time { color: var(--muted); font-size: 11px; white-space: nowrap; }
.badge { padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; white-space: nowrap; }
.badge-long { background: #1a3a1a; color: var(--green); }
.badge-short { background: #3a1a1a; color: var(--red); }
.badge-none { background: #1a1a2a; color: var(--muted); }
.badge-enter { background: #1a3a1a; color: var(--green); }
.badge-wait { background: #3a3a1a; color: var(--yellow); }
.badge-action { background: #2a1a3a; color: var(--purple); }
.badge-high { background: #1a3a1a; color: var(--green); }
.badge-medium { background: #3a3a1a; color: var(--yellow); }
.badge-low { background: #3a1a1a; color: var(--red); }
.context-tag-inline { font-size: 11px; color: #a371f7; margin-left: 4px; }
.zone1-summary { font-size: 12px; color: var(--text); line-height: 1.6; margin-top: 2px; }

/* Zone 2: Price grid */
.price-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 6px; margin-bottom: 10px; }
.price-box { background: #0f0f23; border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; text-align: center; }
.price-box .plabel { font-size: 10px; color: var(--muted); margin-bottom: 2px; }
.price-box .pvalue { font-size: 12px; font-weight: 600; color: var(--text); }

/* Zone 3: Reasoning rows */
.zone3 { margin: 8px 0; padding: 8px 10px; background: #0d1117; border-radius: 6px; }
.reasoning-row { display: flex; gap: 6px; padding: 3px 0; font-size: 12px; line-height: 1.5; align-items: flex-start; }
.reasoning-row .rlabel { color: var(--accent); font-weight: 600; min-width: 36px; flex-shrink: 0; }
.reasoning-row .rtext { color: var(--text); }

/* Zone 4: Evidence tags */
.zone4 { display: flex; gap: 5px; margin-top: 8px; flex-wrap: wrap; }
.evidence-tag { font-size: 10px; padding: 2px 7px; border-radius: 3px; border: 1px solid; }
.et-neutral { color: #94a3b8; background: #1a1a2a; border-color: #2a2a3a; }
.et-bull { color: var(--green); background: #0d1a0d; border-color: #1a3a1a; }
.et-bear { color: var(--red); background: #1a0d0d; border-color: #3a1a1a; }

/* Token tabs */
.token-tab { padding: 6px 16px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; font-weight: 600; cursor: pointer; transition: all 0.15s; font-family: inherit; background: var(--panel); color: var(--text); }
.token-tab:hover { border-color: var(--accent); }
.token-tab.active { background: var(--accent); color: #fff; border-color: var(--accent); }

/* Timeline header */
.timeline-header { padding: 6px 0 10px; font-size: 13px; color: var(--text); border-bottom: 1px solid var(--border); margin-bottom: 12px; }
.timeline-header .th-token { font-weight: 600; color: var(--accent); }
.timeline-header .th-count { background: var(--accent); color: #fff; padding: 1px 8px; border-radius: 10px; font-size: 11px; margin-left: 6px; }

/* Period selector */
.period-selector { display: flex; gap: 4px; padding: 8px; border-bottom: 1px solid var(--border); }
.period-btn { padding: 3px 12px; border: 1px solid var(--border); border-radius: 4px; font-size: 11px; font-weight: 600; cursor: pointer; font-family: inherit; background: transparent; color: var(--muted); transition: all 0.15s; }
.period-btn:hover { border-color: var(--accent); color: var(--text); }
.period-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }

/* Layout */
#main-layout { display: flex; gap: 16px; height: 600px; }
#chart-panel { width: 380px; min-width: 320px; flex-shrink: 0; background: var(--panel); border: 1px solid var(--border); border-radius: 10px; overflow: hidden; display: flex; flex-direction: column; }
#chart-iframe-wrap { flex: 1; }
#cards-panel { flex: 1; overflow-y: auto; padding-right: 4px; }
.empty { text-align: center; color: var(--muted); padding: 40px; }
@media (max-width: 900px) { #main-layout { flex-direction: column; height: auto; } #chart-panel { width: 100%; height: 300px; } #cards-panel { max-height: 600px; overflow-y: auto; } }
</style>
</head>
<body>

<div class="nav-bar">
  <a href="/">📊 市场监控</a>
  <a href="/strategies">📈 运行策略</a>
  <div class="nav-dropdown">
    <a href="#">🤖 Agent 面板 ▾</a>
    <div class="nav-dropdown-content">
      <a href="/agent">Agent-合约扫描</a>
      <a href="/heat-agent">Agent-热度榜单</a>
    <a href="/agent-heat-lessons">Agent-热度有教训</a>
    <a href="/agent-nl">Agent-无教训</a>
                <a href="/agent-short">Agent-做空</a>
      <a href="/agent-kol">Agent-KOL 监控</a>
              <a href="/agent-kol-snapshot">Agent-KOL 快照监控</a>
          </div>
  </div>
  <a href="/dashboard">📊 市场全景</a>
  <a href="/onchain">🔍 链上监控</a>
  <a href="/alpha-cluster">🔬 Alpha 集群</a>
  <a href="/live-onchain">💹 实盘监控</a>
  <a href="/settings">⚙ 系统设置</a>
</div>

<div class="header">
  <div></div>
  <span class="header-time" id="clock"></span>
</div>

<div id="token-tabs"></div>

<div id="main-layout">
  <div id="chart-panel">
    <div class="period-selector" id="period-selector">
      <button class="period-btn active" data-interval="15">15m</button>
      <button class="period-btn" data-interval="60">1H</button>
      <button class="period-btn" data-interval="240">4H</button>
    </div>
    <div id="chart-iframe-wrap">
      <iframe id="tv-chart" src="about:blank" style="width:100%;height:100%;border:none" allowfullscreen></iframe>
    </div>
  </div>
  <div id="cards-panel">
    <div id="timeline-header" class="timeline-header" style="display:none"></div>
    <div id="cards-list"><div class="empty">加载中...</div></div>
  </div>
</div>

<script>
const fmtPrice = v => (v==null||isNaN(Number(v)))?'—':Number(v).toFixed(6);
const esc = s => s ? String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;') : '';

let _byToken = {};
let _activeToken = '';
let _currentInterval = '15';
let _autoRefreshTimer = null;

function evTagClass(text) {
  var s = String(text);
  if (/\\b-\\d/.test(s)) return 'et-bear';
  if (/跌|空|卖|砸|阻|缩|dump|short|bear/i.test(s)) return 'et-bear';
  if (/涨|多|买|拉|放|支|弹|增|pump|long|bull|break/i.test(s)) return 'et-bull';
  return 'et-neutral';
}

function setChart(token, interval) {
  var sym = 'BINANCE%3A' + token + 'USDT.P';
  var iv = interval || _currentInterval;
  document.getElementById('tv-chart').src = 'https://s.tradingview.com/widgetembed/?symbol=' + sym + '&interval=' + iv + '&theme=dark&style=1&width=100%25&height=100%25&hide_side_toolbar=1&withdateranges=1&save_image=0&studies=';
}

function timeStr(ts) {
  if (!ts) return '';
  try { var d = new Date(ts.replace(' ','T')+'Z'); return d.toLocaleString('zh-CN', {hour12:false, timeZone:'Asia/Shanghai', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'}).replace(/\\//g,'-'); } catch(e) { return ts.substr(5,11); }
}

function renderCard(a) {
  // --- Zone 1: time + badges + context_tag + summary ---
  var dirBadge = a.direction === 'long' ? '<span class="badge badge-long">LONG</span>'
    : a.direction === 'short' ? '<span class="badge badge-short">SHORT</span>'
    : '<span class="badge badge-none">NONE</span>';
  var confScore = Number(a.confidence) || 0;
  var statusBadge = a.status === 'ENTER'
    ? '<span class="badge badge-enter">ENTER</span>'
    : '<span class="badge badge-wait">' + esc(a.status||'WAIT') + '</span>';
  var trendConfBadge = '<span class="badge badge-action">' + esc(a.trend || '') + ' / ' + confScore + '</span>';
  var ctxTag = a.context_tag ? ' <span class="context-tag-inline">'+esc(a.context_tag)+'</span>' : '';

  var summary = a.summary || '';

  var zone1 = '<div class="zone1">'
    + '<div class="zone1-row"><span class="card-time">'+timeStr(a.created_at)+'</span>'
    + dirBadge + trendConfBadge + statusBadge + ctxTag + '</div>'
    + (summary ? '<div class="zone1-summary">'+esc(summary)+'</div>' : '')
    + '</div>';

  // --- Zone 2: Price grid ---
  var pl = a.price_levels || {};
  var priceKeys = [
    ['现价','current'],['支撑','support'],['阻力','resistance'],
    ['入场','entry'],['止损','stop_loss'],['止盈','take_profit']
  ];
  var priceHtml = '';
  for (var i = 0; i < priceKeys.length; i++) {
    var v = pl[priceKeys[i][1]];
    priceHtml += '<div class="price-box"><div class="plabel">'+priceKeys[i][0]+'</div><div class="pvalue">'+(v!=null?fmtPrice(v):'—')+'</div></div>';
  }
  var zone2 = '<div class="price-grid">'+priceHtml+'</div>';

  // --- Zone 3: Reasoning rows (key:value) ---
  var rs = a.reasoning || {};
  var reasoningHtml = '';
  if (rs.wz || rs.sj || rs.fk) {
    reasoningHtml = '<div class="zone3">';
    if (rs.wz) reasoningHtml += '<div class="reasoning-row"><span class="rlabel">位置</span><span class="rtext">'+esc(rs.wz)+'</span></div>';
    if (rs.sj) reasoningHtml += '<div class="reasoning-row"><span class="rlabel">时机</span><span class="rtext">'+esc(rs.sj)+'</span></div>';
    if (rs.fk) reasoningHtml += '<div class="reasoning-row"><span class="rlabel">风控</span><span class="rtext">'+esc(rs.fk)+'</span></div>';
    reasoningHtml += '</div>';
  }

  // --- Zone 4: Evidence tags (color-coded) ---
  var evTags = a.evidence_tags || [];
  var tagsHtml = '';
  if (evTags.length) {
    tagsHtml = '<div class="zone4">';
    for (var j = 0; j < evTags.length; j++) {
      tagsHtml += '<span class="evidence-tag '+evTagClass(evTags[j])+'">'+esc(evTags[j])+'</span>';
    }
    tagsHtml += '</div>';
  }

  return '<div class="card">' + zone1 + zone2 + reasoningHtml + tagsHtml + '</div>';
}

function renderTabs(tokens) {
  var html = '<div style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap">';
  for (var i = 0; i < tokens.length; i++) {
    var t = tokens[i];
    var cls = t === _activeToken ? ' token-tab active' : 'token-tab';
    html += '<button class="'+cls+'" data-token="'+esc(t)+'">'+esc(t)+'</button>';
  }
  html += '</div>';
  document.getElementById('token-tabs').innerHTML = html;
  var btns = document.querySelectorAll('.token-tab');
  for (var j = 0; j < btns.length; j++) {
    btns[j].addEventListener('click', function() { selectToken(this.dataset.token); });
  }
}

function renderTimelineHeader(token, count) {
  var hdr = document.getElementById('timeline-header');
  hdr.style.display = '';
  hdr.innerHTML = '<span class="th-token">'+esc(token)+'</span> 判断时间线 · 最近12小时<span class="th-count">'+count+'条</span>';
}

async function loadTimeline(token) {
  try {
    var r = await fetch('/api/kol/analyses?strategy=' + (_strategy || '') + '&symbol=' + encodeURIComponent(token));
    var d = await r.json();
    var list = (d.by_token && d.by_token[token]) ? d.by_token[token] : [];
    document.getElementById('cards-list').innerHTML = list.length
      ? list.map(function(a) { return renderCard(a); }).join('')
      : '<div class="empty">暂无该币种分析数据</div>';
    renderTimelineHeader(token, list.length);
  } catch(e) {
    document.getElementById('cards-list').innerHTML = '<div class="empty" style="color:var(--red)">加载失败: '+esc(e.message)+'</div>';
  }
}

var _strategy = new URLSearchParams(window.location.search).get("strategy") || "";

async function loadTokens() {
  try {
    var r = await fetch('/api/kol/analyses?strategy=' + (_strategy || ''));
    var d = await r.json();
    var tokens = d.tokens || [];
    _byToken = d.by_token || {};
    if (!tokens.length) {
      document.getElementById('cards-list').innerHTML = '<div class="empty">暂无KOL分析数据<br><span style="font-size:11px;color:var(--muted)">等待 collector 触发分析后刷新</span></div>';
      document.getElementById('timeline-header').style.display = 'none';
      return;
    }
    _activeToken = tokens[0];
    renderTabs(tokens);
    selectToken(_activeToken);
  } catch(e) {
    document.getElementById('cards-list').innerHTML = '<div class="empty" style="color:var(--red)">加载失败: '+esc(e.message)+'</div>';
  }
}

function selectToken(token) {
  _activeToken = token;
  renderTabs(Object.keys(_byToken));
  setChart(token);
  loadTimeline(token);
  // restart auto-refresh for current token
  if (_autoRefreshTimer) clearInterval(_autoRefreshTimer);
  _autoRefreshTimer = setInterval(function() { loadTimeline(_activeToken); }, 60000);
}

// Period selector
document.getElementById('period-selector').addEventListener('click', function(e) {
  var btn = e.target.closest('.period-btn');
  if (!btn) return;
  _currentInterval = btn.dataset.interval;
  var btns = document.querySelectorAll('.period-btn');
  for (var i = 0; i < btns.length; i++) { btns[i].classList.remove('active'); }
  btn.classList.add('active');
  if (_activeToken) setChart(_activeToken);
});

function updateClock() {
  try { document.getElementById('clock').textContent = new Date().toLocaleString('zh-CN', {hour12:false, timeZone:'Asia/Shanghai'}); } catch(e) {}
}
setInterval(updateClock, 1000);
updateClock();
loadTokens();
</script>
</body>
</html>
"""

STRATEGIES_HTML = """
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>运行策略 - BSM Agent</title>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600&family=Inter:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0f1419; --panel: #1a1f2e; --border: #2a3142;
  --text: #e6e8eb; --muted: #8b92a5; --accent: #58a6ff;
  --green: #3fb950; --red: #f85149; --yellow: #d29922;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: 'JetBrains Mono','Inter',-apple-system,BlinkMacSystemFont,"Microsoft YaHei",monospace; padding: 20px; max-width: 1100px; margin: 0 auto; font-size: 13px; }
h1 { font-size: 20px; margin-bottom: 8px; }
h2 { font-size: 15px; margin: 20px 0 12px; color: var(--accent); }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 18px; margin-bottom: 16px; }
.nav-bar { display: flex; justify-content: center; gap: 0; margin-bottom: 20px; border-bottom: 1px solid var(--border); padding-bottom: 0; }
.nav-bar a { padding: 10px 24px; color: var(--muted); text-decoration: none; font-size: 13px; font-weight: 500; border-bottom: 2px solid transparent; transition: all 0.2s; }
.nav-bar a.active, .nav-bar a:hover { color: var(--accent); border-bottom-color: var(--accent); }
.nav-dropdown { position: relative; }
.nav-dropdown > a { display: inline-block; padding: 10px 24px; color: var(--muted); text-decoration: none; font-size: 13px; font-weight: 500; border-bottom: 2px solid transparent; transition: all 0.2s; }
.nav-dropdown > a:hover { color: var(--accent); border-bottom-color: var(--accent); }
.nav-dropdown-content { display: none; position: absolute; top: 100%; left: 0; background: var(--panel); border: 1px solid var(--border); border-radius: 6px; min-width: 160px; z-index: 100; padding: 4px 0; }
.nav-dropdown-content a { display: block; padding: 8px 16px; color: var(--text); border-bottom: none !important; font-size: 13px; white-space: nowrap; }
.nav-dropdown-content a:hover { background: #1a1f2e; color: var(--accent) !important; }
.nav-dropdown:hover .nav-dropdown-content { display: block; }
.summary { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-bottom: 12px; }
.metric { background: #0a0e15; padding: 10px 12px; border-radius: 8px; }
.metric .label { color: var(--muted); font-size: 11px; }
.metric .value { font-size: 18px; font-weight: 600; margin-top: 2px; }
.green { color: var(--green); }
.red { color: var(--red); }
table { width: 100%; border-collapse: collapse; font-size: 12px; border-radius: 8px; overflow: hidden; margin-top: 8px; }
th { text-align: left; padding: 8px 10px; background: #0a0e15; color: var(--muted); font-weight: 500; border-bottom: 1px solid var(--border); }
td { padding: 7px 10px; border-bottom: 1px solid var(--border); }
.right { text-align: right; }
.strategy-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.strategy-badge { padding: 4px 12px; border-radius: 4px; font-size: 12px; font-weight: 500; }
.badge-agent { background: #1a2a4a; color: #79c0ff; }
.badge-system { background: #1a3a2a; color: #3fb950; }
.badge-manual { background: #3a2a1a; color: #d29922; }
.toggle-section { border-top: 1px solid var(--border); padding-top: 6px; margin-top: 8px; }
.toggle-header { cursor: pointer; color: var(--muted); font-size: 12px; padding: 6px 0; user-select: none; }
.toggle-header:hover { color: var(--text); }
.toggle-body { display: none; }
.toggle-body.show { display: block; }
</style>
</head>
<body>
<div class="nav-bar">
  <a href="/">📊 市场监控</a>
  <a href="/strategies" class="active">📈 运行策略</a>
  <div class="nav-dropdown">
    <a href="#">🤖 Agent 面板 ▾</a>
    <div class="nav-dropdown-content">
      <a href="/agent">Agent-合约扫描</a>
      <a href="/heat-agent">Agent-热度榜单</a>
    <a href="/agent-heat-lessons">Agent-热度有教训</a>
    <a href="/agent-nl">Agent-无教训</a>
                <a href="/agent-short">Agent-做空</a>
      <a href="/agent-kol">Agent-KOL 监控</a>
              <a href="/agent-kol-snapshot">Agent-KOL 快照监控</a>
              </div>
  </div>
  <a href="/dashboard">📊 市场全景</a>
  <a href="/onchain">🔍 链上监控</a>
  <a href="/alpha-cluster">🔬 Alpha 集群</a>
  <a href="/live-onchain">💹 实盘监控</a>
  <a href="/settings">⚙ 系统设置</a>
</div>

<h1>📈 运行策略</h1>

<div id="content"><div style="color:var(--muted);padding:20px">加载中...</div></div>

<script>
const fmtUsd = v => (v!=null&&!isNaN(Number(v)))?'$'+Number(v).toFixed(2):'-';
const fmtPct = v => (v!=null&&!isNaN(Number(v)))?(v>=0?'+':'')+Number(v).toFixed(2)+'%':'-';
const fmtPrice = v => (v==null||isNaN(Number(v)))?'—':(Number(v)>=1?Number(v).toFixed(4):Number(v).toFixed(6));
const fmtTime = ts => { if (!ts) return '—'; try { const d = new Date(ts.replace(' ','T')+'Z'); return d.toLocaleString('zh-CN', {hour12:false, timeZone:'Asia/Shanghai', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'}).replace(/\//g,'-'); } catch { return ts; } };

async function doClose(token) {
  if (!confirm(`确认市价平仓 ${token}？`)) return;
  try {
    const r = await fetch('/api/trade/market-close', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token})
    });
    const d = await r.json();
    if (d.ok) { showToast(`${token} 已平仓`, 'ok'); load(); }
    else { showToast(`${token} 平仓失败：${d.reason || '未知'}`, 'err'); }
  } catch(e) { showToast('操作失败：'+e.message, 'err'); }
}

async function load() {
  try {
    const r = await fetch('/api/strategy/stats');
    const d = await r.json();
    const strategies = d.strategies || [];
    if (!strategies.length) {
      document.getElementById('content').innerHTML = '<div class="panel"><div style="color:var(--muted);text-align:center;padding:40px">暂无策略数据</div></div>';
      return;
    }
    const badge = {
      agent: '<span class="strategy-badge badge-agent">🤖 Agent 策略</span>',
      heat_agent: '<span class="strategy-badge" style="background:#3a1f5f;color:#c4a0ff">📡 热度Agent</span>',
      system: '<span class="strategy-badge badge-system">⚙ 系统策略</span>',
      manual: '<span class="strategy-badge badge-manual">👆 手动策略</span>',
      kol_snapshot: '<span class="strategy-badge" style="background:#2a1a3a;color:#c4a0ff">KOL快照</span>',
      kol_agent: '<span class="strategy-badge" style="background:#1a2a4a;color:#58a6ff">🧠 KOL策略</span>',
      heat_agent_lessons: '<span class="strategy-badge" style="background:#3a2a1a;color:#f0a040">🔥 热度有教训</span>',
      agent_no_lessons: '<span class="strategy-badge" style="background:#2a2a1a;color:#d29922">📋 无教训对照</span>',
        agent_short: '<span class="strategy-badge" style="background:#4a1a1a;color:#f85149">🔻 做空Agent</span>',
        onchain: '<span class="strategy-badge" style="background:#1a3a2a;color:#0ECB81">🔗 链上监控</span>',
    };
    let html = '';
    for (const s of strategies) {
      const wr = s.total > 0 ? (s.wins / s.total * 100) : 0;
      const pnlCls = (s.total_pnl||0) >= 0 ? 'green' : 'red';
      const acc = s.account || {};
      const accPnlCls = (acc.realized_pnl||0) >= 0 ? 'green' : 'red';
      html += `<div class="panel">
        <div class="strategy-header">
          <div>${badge[s.strategy] || s.strategy}${s.strategy === 'onchain' ? (s.live_mode === 'live' ? ' <span style="font-size:11px;background:rgba(14,203,129,0.15);color:#0ECB81;padding:2px 6px;border-radius:6px">💹 实盘</span>' : ' <span style="font-size:11px;background:rgba(139,148,158,0.1);color:#8b949e;padding:2px 6px;border-radius:6px">📝 纸盘</span>') : ''}</div>
          <div style="display:flex;align-items:center;gap:8px">
            ${s.running ? '<span style="font-size:11px;background:rgba(63,185,80,0.15);color:#3fb950;padding:2px 8px;border-radius:10px">● 运行中</span>' : '<span style="font-size:11px;background:rgba(139,148,158,0.15);color:#8b949e;padding:2px 8px;border-radius:10px">○ 已暂停</span>'}
            <span style="color:var(--muted);font-size:12px">共 ${s.total} 笔</span>
          </div>
        </div>
        <div class="summary" style="grid-template-columns:repeat(6,1fr);margin-bottom:6px">
          <div class="metric"><div class="label">初始金额</div><div class="value">${fmtUsd(acc.initial_balance)}</div></div>
          <div class="metric"><div class="label">账户权益</div><div class="value">${fmtUsd(acc.equity)}</div></div>
          <div class="metric"><div class="label">可用余额</div><div class="value">${fmtUsd(acc.available_balance)}</div></div>
          <div class="metric"><div class="label">占用保证金</div><div class="value">${fmtUsd(acc.locked_margin)}</div></div>
          <div class="metric"><div class="label">资金利用率</div><div class="value">${(acc.equity>0?(acc.locked_margin/acc.equity*100).toFixed(1):0)}%</div></div>
          <div class="metric"><div class="label">已实现盈亏</div><div class="value ${accPnlCls}">${fmtUsd(acc.realized_pnl)}</div></div>
        </div>
        <div class="summary" style="grid-template-columns:repeat(6,1fr)">
          <div class="metric"><div class="label">浮动盈亏</div><div class="value">${fmtUsd(acc.unrealized_pnl)}</div></div>
          <div class="metric"><div class="label">胜率</div><div class="value">${wr.toFixed(1)}%</div></div>
          <div class="metric"><div class="label">胜/负</div><div class="value">${s.wins}/${s.losses}</div></div>
          <div class="metric"><div class="label">总盈亏</div><div class="value ${pnlCls}">${fmtUsd(s.total_pnl)}</div></div>
          <div class="metric"><div class="label">平均盈亏</div><div class="value ${pnlCls}">${fmtPct(s.avg_pnl)}</div></div>
          <div class="metric"><div class="label">最近7天胜率</div><div class="value">${s.total_7d>0 ? (s.wins_7d/s.total_7d*100).toFixed(1)+'%' : '—'}</div><div style="font-size:10px;color:var(--muted);margin-top:1px">${s.wins_7d||0}胜/${s.total_7d||0}笔</div></div>
        </div>`;
      // 止损失败标签
      const lossTags = s.loss_tags || {};
      const tags = Object.entries(lossTags).sort((a,b) => b[1]-a[1]);
      if (tags.length) {
        const tagHtml = tags.map(([k,v]) =>
          `<span style="display:inline-block;background:#1a1f2e;padding:3px 8px;border-radius:4px;margin:2px;font-size:11px">${k.replace(/_/g,' ')} <b style="color:var(--red)">${v}</b></span>`
        ).join('');
        html += `<div style="margin-top:4px;padding:8px 0;border-top:1px solid var(--border)">
          <div style="color:var(--muted);font-size:11px;margin-bottom:4px">⚠ 止损失败归档</div>
          ${tagHtml}
        </div>`;
      }
      // 当前持仓 / 已平仓（折叠展开）
      const openList = s.open_positions || [];
      const closedList = s.closed_positions || [];
      const uid = s.strategy + '-' + Math.random().toString(36).slice(2,6);
      if (openList.length) {
        html += `<div class="toggle-section">
          <div class="toggle-header" onclick="this.nextElementSibling.classList.toggle('show')">📌 当前持仓 (${openList.length}) ▾</div>
          <div class="toggle-body" style="overflow-x:auto"><table><thead><tr><th>代币</th><th>方向</th><th>锁利</th><th class="right">入场</th><th class="right">现价</th><th>入场结构</th><th>最新结构</th><th class="right">仓位</th><th class="right">PnL%</th><th class="right">PnL$</th><th class="right">止损</th><th class="right">TP1</th><th class="right">TP2</th><th>持仓</th><th>类型</th><th>开仓时间</th><th>操作</th></tr></thead><tbody>`;
        for (const p of openList) {
          const pnlCls = (p.pnl_pct||0) >= 0 ? 'green' : 'red';
          const cq = Number(p.closed_qty||0), tq = Number(p.quantity||0);
          const lockedPct = tq > 0 ? (cq/tq*100) : 0;
          let hold = '—';
          if (p.created_at) { try { const d = new Date(p.created_at.replace(' ','T')+'Z'); const h = Math.floor((Date.now()-d)/3600000); const m = Math.floor((Date.now()-d)/60000)%60; hold = (h?h+'h':'')+m+'m'; } catch(e) {} }
          const entryConf = p.entry_confidence != null ? Number(p.entry_confidence) : null;
          const entryTrend = p.entry_trend ? '<span style="display:inline-block;background:#2a1a3a;color:#a371f7;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:600">'+p.entry_trend+(entryConf!=null?' / '+entryConf:'')+'</span>' : '—';
          const latestConf = p.latest_confidence != null ? Number(p.latest_confidence) : null;
          const latestTrend = p.latest_trend ? '<span style="display:inline-block;background:#2a1a3a;color:#a371f7;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:600">'+p.latest_trend+(latestConf!=null?' / '+latestConf:'')+'</span>' : '—';
          const posSize = '$' + Number(p.margin_amount || 0).toFixed(0) + ' ×' + Number(p.leverage || 0);
          let openTime = '—';
          if (p.created_at) { try { const d = new Date(p.created_at.replace(' ','T')+'Z'); openTime = d.toLocaleString('zh-CN',{hour12:false,timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).replace(/\//g,'-'); } catch(e) {} }
          const orderTypeLabel = p.order_type === 'limit' ? '挂单' : '市价';
          html += `<tr><td style="font-weight:bold;color:var(--accent)">${p.token}</td><td>${p.side}</td><td style="color:var(--green);font-size:11px">${lockedPct>0?'🔒'+lockedPct.toFixed(0)+'%':'—'}</td><td class="right">${fmtPrice(p.entry_price)}</td><td class="right">${fmtPrice(p.current_price)}</td><td style="font-size:11px">${entryTrend}</td><td style="font-size:11px">${latestTrend}</td><td style="font-size:11px;white-space:nowrap">${posSize}</td><td class="right ${pnlCls}">${fmtPct(p.pnl_pct)}</td><td class="right ${pnlCls}">${fmtUsd(p.unrealized_pnl)}</td><td class="right">${fmtPrice(p.stop_loss_price)}</td><td class="right">${fmtPrice(p.tp1_price)}</td><td class="right">${fmtPrice(p.tp2_price)}</td><td style="font-size:11px;white-space:nowrap">${hold}</td><td style="font-size:11px">${orderTypeLabel}</td><td style="font-size:11px;white-space:nowrap">${openTime}</td><td><button onclick="doClose('${p.token}')" style="background:#f85149;color:#fff;border:none;padding:3px 10px;border-radius:4px;cursor:pointer;font-size:11px;font-weight:600">平仓</button></td></tr>`;
        }
        html += `</tbody></table></div></div>`;
      }
      if (closedList.length) {
        html += `<div class="toggle-section">
          <div class="toggle-header" onclick="this.nextElementSibling.classList.toggle('show')">📋 最近平仓 (${closedList.length}) ▾</div>
          <div class="toggle-body" style="overflow-x:auto"><table><thead><tr><th>代币</th><th>方向</th><th class="right">入场</th><th class="right">平仓</th><th class="right">PnL%</th><th class="right">盈亏</th><th>类型</th><th>平仓时间</th><th>开仓时间</th></tr></thead><tbody>`;
        for (const p of closedList) {
          const pnlCls = (p.pnl_pct||0) >= 0 ? 'green' : 'red';
          html += `<tr><td style="font-weight:bold;color:var(--accent)">${p.token}</td><td>${p.side}</td><td class="right">${fmtPrice(p.entry_price)}</td><td class="right">${fmtPrice(p.current_price)}</td><td class="right ${pnlCls}">${fmtPct(p.pnl_pct)}</td><td class="right ${pnlCls}">${fmtUsd(p.realized_pnl)}</td><td style="font-size:11px">${p.order_type === 'limit' ? '挂单' : '市价'}</td><td style="font-size:11px;white-space:nowrap">${fmtTime(p.closed_at)}</td><td style="font-size:11px;white-space:nowrap">${fmtTime(p.created_at)}</td></tr>`;
        }
        html += `</tbody></table></div></div>`;
      }
      html += `</div>`;
    }
    document.getElementById('content').innerHTML = html;
  } catch(e) { document.getElementById('content').innerHTML = '<div class="panel"><div style="color:var(--red)">加载失败: '+e.message+'</div></div>'; }
}
load();
</script>
</body>
</html>
"""


KOL_SNAPSHOT_MONITOR_HTML = AGENT_HTML.replace(
    "<title>Agent-合约扫描</title>", "<title>Agent-KOL 认知快照</title>"
).replace(
    "<h1>Agent-合约扫描</h1>", "<h1>Agent-KOL 认知快照</h1>"
).replace(
    "合约扫描 · 自主决策开仓", "KOL认知快照 · 决策时间线 · LLM日志 · <a href='/kol?strategy=kol_snapshot' style='color:var(--accent);text-decoration:none'>📊 查看K线分析 →</a>"
).replace(
    "strategy_initial_agent", "strategy_initial_kol_snapshot"
).replace(
    "loadLessons(true), ", "").replace(
    "  loadLessons(true);\n", ""
).replace(
    "p.strategy === 'agent'", "p.strategy === 'kol_snapshot'"
).replace(
    "const AGENT_STRATEGY = 'agent'", "const AGENT_STRATEGY = 'kol_snapshot'"
).replace(
    "'/api/agent/overview'", "'/api/agent/overview?strategy='+AGENT_STRATEGY"
).replace(
    "'/api/agent/journal?limit='", "'/api/agent/journal?strategy='+AGENT_STRATEGY+'&limit='"
).replace(
    "'/api/agent/timeline?limit='", "'/api/agent/timeline?strategy='+AGENT_STRATEGY+'&limit='"
).replace(
    "'/api/agent/lessons?limit='", "'/api/agent/lessons?strategy='+AGENT_STRATEGY+'&limit='"
).replace(
    '<a href="/agent" class="active">', '<a href="/agent">'
).replace(
    '<a href="/agent-kol-snapshot">', '<a href="/agent-kol-snapshot" class="active">'
).replace(
    '''<!-- 底部：教训库 + 决策时间线 -->
<div class="grid-bottom">
  <div class="panel">
    <h2>教训库</h2>
    <div class="stats-bar" id="lesson-stats"></div>
    <div id="lessons-list"></div>
  </div>

  <div class="panel">
    <h2>决策时间线</h2>''',
    '''<!-- 底部：LLM日志 + 决策时间线 -->
<div class="grid-bottom">
  <div class="panel">
    <h2>LLM 调用日志</h2>
    <div id="llm-logs"></div>
  </div>
  <div class="panel">
    <h2>决策时间线</h2>'''
).replace(
    '</body>', '''<script>
var _strategy = "kol_snapshot";
async function loadLLMLogs(){
  try{
    const r=await fetch("/api/kol/llm-logs?limit=30&strategy=" + _strategy);
    const logs=await r.json();
    if(!Array.isArray(logs)||!logs.length){document.getElementById("llm-logs").innerHTML="<div class=empty>暂无</div>";return;}
    let h="<table><tr><th>时间</th><th>提供商</th><th>候选</th><th>耗时</th><th>状态</th></tr>";
    for(const l of logs){
      let t="";if(l.created_at){const d=new Date(l.created_at+"Z");d.setHours(d.getHours()+8);t=d.toISOString().replace("T"," ").substring(5,16);}
      const ok=l.success?"<span class=green>OK("+l.analyses_count+"条)</span>":"<span class=red>FAIL</span>";
      h+="<tr><td class=muted>"+t+"</td><td>"+(l.provider||"")+"</td><td>"+l.candidate_count+"</td><td>"+(l.duration_ms/1000).toFixed(1)+"s</td><td>"+ok+"</td></tr>";
    }
    document.getElementById("llm-logs").innerHTML=h+"</table>";
  }catch(e){}
}
loadLLMLogs();
setInterval(loadLLMLogs,60000);
</script>
</body>'''
)

KOL_MONITOR_HTML = AGENT_HTML.replace(
    "<title>Agent-合约扫描</title>", "<title>Agent-KOL 监控</title>"
).replace(
    "<h1>Agent-合约扫描</h1>", "<h1>Agent-KOL 监控</h1>"
).replace(
    "合约扫描 · 自主决策开仓", "合约扫描 · 决策时间线 · LLM日志 · <a href='/kol?strategy=kol_agent' style='color:var(--accent);text-decoration:none'>📊 查看K线分析 →</a>"
).replace(
    "strategy_initial_agent", "strategy_initial_kol_agent"
).replace(
    "loadLessons(true), ", "").replace(
    "  loadLessons(true);\n", ""
).replace(
    "p.strategy === 'agent'", "p.strategy === 'kol_agent'"
).replace(
    "const AGENT_STRATEGY = 'agent'", "const AGENT_STRATEGY = 'kol_agent'"
).replace(
    "'/api/agent/overview'", "'/api/agent/overview?strategy='+AGENT_STRATEGY"
).replace(
    "'/api/agent/journal?limit='", "'/api/agent/journal?strategy='+AGENT_STRATEGY+'&limit='"
).replace(
    "'/api/agent/timeline?limit='", "'/api/agent/timeline?strategy='+AGENT_STRATEGY+'&limit='"
).replace(
    "'/api/agent/lessons?limit='", "'/api/agent/lessons?strategy='+AGENT_STRATEGY+'&limit='"
).replace(
    '<a href="/agent" class="active">', '<a href="/agent">'
).replace(
    '<a href="/agent-kol">', '<a href="/agent-kol" class="active">'
).replace(
    '''<!-- 底部：教训库 + 决策时间线 -->
<div class="grid-bottom">
  <div class="panel">
    <h2>教训库</h2>
    <div class="stats-bar" id="lesson-stats"></div>
    <div id="lessons-list"></div>
  </div>

  <div class="panel">
    <h2>决策时间线</h2>''',
    '''<!-- 底部：LLM日志 + 决策时间线 -->
<div class="grid-bottom">
  <div class="panel">
    <h2>LLM 调用日志</h2>
    <div id="llm-logs"></div>
  </div>
  <div class="panel">
    <h2>决策时间线</h2>'''
).replace(
    '</body>', '''<script>
var _strategy = "kol_agent";
async function loadLLMLogs(){
  try{
    const r=await fetch("/api/kol/llm-logs?limit=30&strategy=" + _strategy);
    const logs=await r.json();
    if(!Array.isArray(logs)||!logs.length){document.getElementById("llm-logs").innerHTML="<div class=empty>暂无</div>";return;}
    let h="<table><tr><th>时间</th><th>提供商</th><th>候选</th><th>耗时</th><th>状态</th></tr>";
    for(const l of logs){
      let t="";if(l.created_at){const d=new Date(l.created_at+"Z");d.setHours(d.getHours()+8);t=d.toISOString().replace("T"," ").substring(5,16);}
      const ok=l.success?"<span class=green>OK("+l.analyses_count+"条)</span>":"<span class=red>FAIL</span>";
      h+="<tr><td class=muted>"+t+"</td><td>"+(l.provider||"")+"</td><td>"+l.candidate_count+"</td><td>"+(l.duration_ms/1000).toFixed(1)+"s</td><td>"+ok+"</td></tr>";
    }
    document.getElementById("llm-logs").innerHTML=h+"</table>";
  }catch(e){}
}
loadLLMLogs();
setInterval(loadLLMLogs,60000);
</script>
</body>'''
)



@app.get("/heat-agent", response_class=HTMLResponse)
def heat_agent_page():
    return HTMLResponse(
        content=HEAT_AGENT_HTML,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )

@app.get("/agent-nl", response_class=HTMLResponse)
def agent_nl_page():
    return HTMLResponse(
        content=NL_AGENT_HTML,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


@app.get("/agent-short", response_class=HTMLResponse)
def agent_short_page():
    return HTMLResponse(
        content=AGENT_SHORT_HTML,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )

@app.get("/agent-kol-snapshot", response_class=HTMLResponse)
def agent_kol_snapshot_page():
    return HTMLResponse(
        content=KOL_SNAPSHOT_MONITOR_HTML,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


@app.get("/agent-kol", response_class=HTMLResponse)
def agent_kol_page():
    return HTMLResponse(
        content=KOL_MONITOR_HTML,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )

@app.get("/agent-heat-lessons", response_class=HTMLResponse)
def agent_heat_lessons_page():
    return HTMLResponse(
        content=HEAT_LESSONS_HTML,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )

@app.get("/kol", response_class=HTMLResponse)
def kol_agent_page():
    return HTMLResponse(
        content=KOL_AGENT_HTML,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )

@app.get("/dashboard", response_class=HTMLResponse)
def dashboard_page():
    return HTMLResponse(
        content=DASHBOARD_HTML,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="zh-CN"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>市场全景分析 — BSM Agent</title>
<style>
:root{--bg:#0a0e14;--panel:#11161d;--border:#1e2530;--text:#c9d1d9;--muted:#6e7681;--accent:#58a6ff;--green:#3fb950;--yellow:#d29922;--red:#f85149;--orange:#f0883e}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Inter',-apple-system,"Microsoft YaHei",sans-serif;padding:16px;max-width:1400px;margin:0 auto;font-size:13px}
h1{font-size:18px;margin-bottom:2px}h2{font-size:14px;color:var(--accent);margin-bottom:6px}
.nav-bar{display:flex;justify-content:center;gap:0;margin-bottom:12px;border-bottom:1px solid var(--border);padding-bottom:0;flex-wrap:wrap}
.nav-bar a{padding:8px 14px;color:var(--muted);text-decoration:none;font-size:12px;font-weight:500;border-bottom:2px solid transparent;transition:all .2s;white-space:nowrap}
.nav-bar a.active,.nav-bar a:hover{color:var(--accent);border-bottom-color:var(--accent)}
.header-row{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.status-badge{padding:4px 10px;border-radius:4px;font-size:11px;font-weight:600}
.status-warning{background:#3a2a0a;color:var(--yellow);border:1px solid var(--yellow);animation:breathe 2s ease-in-out infinite}
.status-normal{background:#1a3a1a;color:var(--green);border:1px solid var(--green)}
@keyframes breathe{0%,100%{opacity:1}50%{opacity:.6}}
.main-grid{display:grid;grid-template-columns:1fr;gap:12px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px}
.card-title{font-size:13px;color:var(--muted);margin-bottom:2px}
.metrics-row{display:grid;grid-template-columns:repeat(6,1fr);gap:8px;margin-bottom:10px}
.mini-card{background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:10px;text-align:center}
.mini-card .label{font-size:10px;color:var(--muted);margin-bottom:4px}
.mini-card .value{font-size:22px;font-weight:700}
.mini-card .desc{font-size:10px;color:var(--muted);margin-top:2px}
.macro-inner{background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:10px;margin:8px 0}
.macro-4col{display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:8px}
.macro-col{padding:6px}
.macro-col .m-label{font-size:10px;color:var(--muted);margin-bottom:2px}
.macro-col .m-value{font-size:13px;font-weight:600}
.events-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.event-item{background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:10px}
.event-name{font-weight:700;font-size:13px;margin-bottom:4px}
.event-meta{font-size:11px;color:var(--muted)}
.event-tag{padding:2px 6px;border-radius:3px;font-size:10px;margin-left:6px}
.tag-observe{border:1px solid var(--accent);color:var(--accent);background:#0a1a2a}
.tag-core{border:1px solid var(--red);color:var(--red);background:#2a0a0a}
.anomaly-bar{background:#2a1a0a;border:1px solid var(--yellow);border-radius:6px;padding:10px;margin-top:8px;font-size:12px}
.anomaly-bar .a-title{font-weight:700;color:var(--yellow);margin-bottom:2px}
.anomaly-bar .a-desc{color:var(--text)}
.anomaly-bar .a-tag{margin-top:4px;font-size:10px;color:var(--muted)}
.evidence-left{border-left:3px solid var(--green);padding-left:12px;margin:8px 0;background:#0a1a0a;padding:10px;border-radius:0 4px 4px 0}
.evidence-right{border-left:3px solid var(--red);padding-left:12px;margin:8px 0;background:#1a0a0a;padding:10px;border-radius:0 4px 4px 0}
.evidence-left ul{margin:0;padding-left:16px;font-size:12px}
.evidence-right p{font-size:12px;line-height:1.5}
.pills{display:flex;gap:6px;flex-wrap:wrap;margin:8px 0}
.pill{background:#1a1f2e;color:var(--muted);padding:3px 10px;border-radius:12px;font-size:10px}
.timeline-bar{display:flex;gap:2px;margin:8px 0}
.timeline-block{width:18px;height:18px;border-radius:2px}
.legend{display:flex;gap:12px;font-size:10px;color:var(--muted)}
.legend span{display:flex;align-items:center;gap:4px}
.legend-dot{width:10px;height:10px;border-radius:2px;display:inline-block}
button:disabled{opacity:.5;cursor:not-allowed}
.refresh-info{font-size:11px;color:var(--muted)}
.empty{text-align:center;color:var(--muted);padding:20px}
@media(max-width:768px){.metrics-row{grid-template-columns:repeat(3,1fr)}.macro-4col{grid-template-columns:1fr 1fr}.events-grid{grid-template-columns:1fr}}
.nav-dropdown{position:relative}.nav-dropdown>a{display:inline-block;padding:8px 14px;color:var(--muted);text-decoration:none;font-size:12px;font-weight:500;border-bottom:2px solid transparent;transition:all .2s;white-space:nowrap}.nav-dropdown>a:hover{color:var(--accent);border-bottom-color:var(--accent)}.nav-dropdown-content{display:none;position:absolute;top:100%;left:0;background:var(--panel);border:1px solid var(--border);border-radius:6px;min-width:140px;z-index:100;padding:4px 0}.nav-dropdown-content a{display:block;padding:6px 14px;color:var(--text);border-bottom:none!important;font-size:12px;white-space:nowrap}.nav-dropdown-content a:hover{background:#1a1f2e;color:var(--accent)!important}.nav-dropdown:hover .nav-dropdown-content{display:block}
</style></head><body>

<div class="nav-bar">
  <a href="/">📊 市场监控</a>
  <a href="/strategies">📈 运行策略</a>
  <div class="nav-dropdown"><a href="#">🤖 Agent 面板 ▾</a>
    <div class="nav-dropdown-content">
      <a href="/agent">Agent-合约搜索</a>
      <a href="/heat-agent">Agent-热度榜单</a>
      <a href="/agent-heat-lessons">Agent-热度有教训</a>
      <a href="/agent-nl">Agent-无教训</a>
                <a href="/agent-short">Agent-做空</a>
      <a href="/agent-kol">Agent-KOL 监控</a>
              <a href="/agent-kol-snapshot">Agent-KOL 快照监控</a>
    </div>
  </div>
  <a href="/dashboard" class="active">📊 市场全景</a>
  <a href="/onchain">🔍 链上监控</a>
  <a href="/alpha-cluster">🔬 Alpha 集群</a>
  <a href="/live-onchain">💹 实盘监控</a>
  <a href="/settings">⚙ 系统设置</a>
</div>

<div class="header-row">
  <div><h1>📊 市场全景分析</h1></div>
  <div id="status-badge"></div>
</div>

<div class="main-grid">
  <div class="card">
    <div class="header-row">
      <div><h2>风控 / 异常窗口</h2><span class="card-title">流动性门禁、宏观事件窗口、Macro AI 标记</span></div>
      <span class="refresh-info">每15s刷新</span>
    </div>
    <div class="metrics-row" id="metrics"></div>
    <div class="macro-inner">
      <div class="header-row" style="margin-bottom:6px"><span style="font-weight:600">宏观事件窗口</span><span class="status-badge status-normal" id="macro-status">正常</span></div>
      <div class="macro-4col" id="macro-next"></div>
    </div>
    <div class="events-grid" id="recent-events"></div>
    <div id="anomaly" style="margin-top:6px"></div>
  </div>

  <div class="card">
    <div class="header-row">
      <div><h2 style="display:inline">V3 AI Regime</h2> <span id="regime-tag" style="font-size:12px;margin-left:8px"></span> <span id="model-tag" style="font-size:10px;color:var(--muted)"></span> <button onclick="refreshAI(event)" style="background:var(--accent);color:#000;border:none;border-radius:4px;padding:3px 10px;font-size:11px;cursor:pointer;margin-left:6px">⟳ 刷新</button></div>
      <span class="refresh-info" id="ai-updated">更新中...</span>
    </div>
    <div id="judgment" style="font-size:13px;line-height:1.6;margin:10px 0;padding:10px;background:var(--bg);border-radius:6px">加载中...</div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
      <div class="evidence-left"><b style="color:var(--green);font-size:12px">✓ 支持证据</b><div id="evidence-support"></div></div>
      <div class="evidence-right"><b style="color:var(--red);font-size:12px">✗ 反面证据</b><div id="evidence-counter"></div></div>
    </div>
    <div class="pills" id="data-sources"></div>
    <div class="timeline-bar" id="timeline-bar"></div>
    <div class="legend">
      <span><span class="legend-dot" style="background:#3fb950"></span>山寨行情</span>
      <span><span class="legend-dot" style="background:#d29922"></span>回调兑现</span>
      <span><span class="legend-dot" style="background:#f85149"></span>趋势空</span>
      <span><span class="legend-dot" style="background:#8b949e"></span>震荡死水</span>
      <span><span class="legend-dot" style="background:#6e7681"></span>崩盘</span>
    </div>
  </div>
</div>

<script>
const R={"alt_season":"#3fb950","alt_pullback":"#d29922","chop":"#8b949e","risk_off":"#f85149","crash":"#6e7681"};
const RN={"alt_season":"山寨行情","alt_pullback":"回调兑现","chop":"震荡死水","risk_off":"趋势空","crash":"崩盘"};
const MLAB={"liquidity":"流动性状态","z_depth":"当前 z_depth","macro_1h":"1h 大局","macro_3h":"3h 大局","data_age":"数据年龄","fear_greed":"恐惧贪婪"};
async function refreshAI(e){
  const btn=e.target;btn.disabled=true;btn.textContent='⟳ 刷新中...';
  try{
    const[rm,me,ai]=await Promise.all([fetch("/api/risk-metrics"),fetch("/api/macro-events"),fetch("/api/ai-regime/refresh",{method:'POST'})]);
    const r=await rm.json(),m=await me.json(),a=await ai.json();
    updateDashboard(r,m,a);
  }catch(e){console.error(e)}
  btn.disabled=false;btn.textContent='⟳ 刷新';
}
function updateDashboard(r,m,a){
  const hasWarning=r.global_status==="warning"||m.current_risk==="high"||m.current_risk==="medium";
  document.getElementById("status-badge").innerHTML=hasWarning?'<span class="status-badge status-warning">⚠ 有警告</span>':'<span class="status-badge status-normal">✓ 正常</span>';
  const mt=r.metrics||{};let mh="";
  for(const[k,v]of Object.entries(mt)){
    const c=v.color||"var(--text)";
    mh+=`<div class="mini-card"><div class="label">${MLAB[k]||k}</div><div class="value" style="color:${c}">${v.value??v.status}</div><div class="desc">${v.desc||""}</div></div>`;
  }
  document.getElementById("metrics").innerHTML=mh;
  document.getElementById("macro-status").textContent=m.current_risk==="high"?"⚠ 高风险":m.current_risk==="medium"?"● 注意":"✓ 正常";
  document.getElementById("macro-status").className="status-badge "+(m.current_risk==="high"?"status-warning":"status-normal");
  if(m.next_event){
    document.getElementById("macro-next").innerHTML=
      `<div class="macro-col"><div class="m-label">当前宏观风险</div><div class="m-value" style="color:${m.current_risk==="high"?"var(--red)":"var(--green)"}">${m.current_risk==="high"?"高风险":m.current_risk==="medium"?"注意":"正常"}</div></div>`+
      `<div class="macro-col"><div class="m-label">下个事件</div><div class="m-value">${m.next_event.name||""}</div><div style="font-size:10px;color:var(--muted)">${(m.next_event.time||"").substring(0,16)}</div></div>`+
      `<div class="macro-col"><div class="m-label">倒计时</div><div class="m-value" style="color:var(--yellow)">${m.countdown_str||"即将"}</div><div style="font-size:10px;color:var(--muted)">预测:${m.next_event.forecast||"N/A"} 前值:${m.next_event.previous||"N/A"}</div></div>`+
      `<div class="macro-col"><div class="m-label">面板动作</div><div class="m-value" style="color:var(--accent)">仅提示</div></div>`;
  }else{
    document.getElementById("macro-next").innerHTML=`<div class="macro-col" style="grid-column:1/-1;text-align:center;color:var(--muted)">暂无未来宏观事件</div>`;
  }
  const evts=m.recent_events||[];let eh="";
  for(const e of evts){
    const tag=e.tag==="核心"?"tag-core":"tag-observe";
    eh+=`<div class="event-item"><div class="event-name">${e.name}${e.tag?`<span class="event-tag ${tag}">${e.tag}</span>`:""}</div><div class="event-meta">${(e.time||"").substring(0,16)} · ${e.region||""}</div></div>`;
  }
  document.getElementById("recent-events").innerHTML=eh||"<div class='empty'>暂无宏观事件</div>";
  let anomalyHtml="";
  const liq=r.metrics?.liquidity;
  if(liq&&liq.color!=="green"){
    anomalyHtml+=`<div class="anomaly-bar"><div class="a-title">⚠ 流动性警戒</div><div class="a-desc">${liq.status}: ${liq.desc}</div><div class="a-tag">rolling_liquidity</div></div>`;
  }
  if(r.global_status==="warning"&&m.current_risk==="high"){
    anomalyHtml+=`<div class="anomaly-bar"><div class="a-title">⚠ 宏观风险 + 流动性双重警戒</div><div class="a-desc">当前宏观风险评级为高，同时流动性出现异常</div><div class="a-tag">macro_risk + liquidity</div></div>`;
  }
  document.getElementById("anomaly").innerHTML=anomalyHtml;
  const tag=a.current_regime||"";
  document.getElementById("regime-tag").innerHTML=`<span style="color:${R[tag]||'var(--muted)'}">●</span> ${RN[tag]||tag} <span style="color:var(--muted);font-size:11px">conf ${a.confidence||0}</span>`;
  document.getElementById("model-tag").textContent=a.model||"deepseek";
  document.getElementById("ai-updated").textContent=`每${a.refresh_interval_min||32}分钟 · ${a.cache_age_display||""}` + (a.cache_age_display!=="等待首次刷新" ? ` (${a.updated_at||""})` : "");
  document.getElementById("judgment").innerHTML=(a.judgment_text||"AI研判加载中...").replace(/\\n/g,"<br>");
  let sl="";for(const s of(a.evidence_support||[]))sl+=`<li>${s}</li>`;
  document.getElementById("evidence-support").innerHTML=`<ul>${sl}</ul>`;
  document.getElementById("evidence-counter").innerHTML=`<p>${a.evidence_counter||"暂无反面证据"}</p>`;
  let pl="";for(const s of(a.data_sources||[]))pl+=`<span class="pill">${s}</span>`;
  document.getElementById("data-sources").innerHTML=pl;
  const tl=a.timeline_24h||[];
  let th="";for(const t of tl)th+=`<span class="timeline-block" style="background:${R[t]||"#30363d"}" title="${RN[t]||t}"></span>`;
  for(let i=tl.length;i<24;i++)th+=`<span class="timeline-block" style="background:#1a1f2e" title="无数据"></span>`;
  document.getElementById("timeline-bar").innerHTML=th;
}
async function loadAll(){
  try{
    const[rm,me,ai]=await Promise.all([fetch("/api/risk-metrics"),fetch("/api/macro-events"),fetch("/api/ai-regime")]);
    const r=await rm.json(),m=await me.json(),a=await ai.json();
    updateDashboard(r,m,a);
  }catch(e){console.error(e);document.getElementById("judgment").innerHTML='<span style="color:var(--red)">数据加载失败，15s后重试...</span>';document.getElementById("status-badge").innerHTML='<span class="status-badge status-warning">⚠ 数据异常</span>'}
}
loadAll();setInterval(loadAll,15000);
</script></body></html>"""


CLUSTER_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>集群监控</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:opsz@14..32&family=JetBrains+Mono&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0d1117; --panel: #161b22; --text: #c9d1d9; --muted: #8b949e;
  --accent: #58a6ff; --border: #30363d; --red: #f85149; --green: #3fb950;
  --yellow: #d2991d; --orange: #db6d28; --purple: #a371f7;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: 'Inter', sans-serif; padding: 20px; max-width: 1400px; margin: 0 auto; }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 16px; }
h1 { font-size: 20px; margin-bottom: 20px; }
h2 { font-size: 16px; margin-bottom: 12px; color: var(--accent); }
.nav-bar { display: flex; justify-content: center; gap: 0; margin-bottom: 20px; border-bottom: 1px solid var(--border); padding-bottom: 0; flex-wrap: wrap; }
.nav-bar a { padding: 8px 16px; color: var(--muted); text-decoration: none; font-size: 12px; font-weight: 500; border-bottom: 2px solid transparent; transition: all 0.2s; white-space: nowrap; }
.nav-bar a:hover { color: var(--accent); border-bottom-color: var(--accent); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--muted); font-weight: 500; padding: 8px 10px; border-bottom: 1px solid var(--border); font-size: 11px; text-transform: uppercase; }
td { padding: 8px 10px; border-bottom: 1px solid var(--border); }
tr:hover { background: rgba(88,166,255,0.04); }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 500; margin-right: 4px; }
.badge-green { background: rgba(63,185,80,0.15); color: var(--green); }
.badge-yellow { background: rgba(210,153,29,0.15); color: var(--yellow); }
.badge-red { background: rgba(248,81,73,0.15); color: var(--red); }
.badge-blue { background: rgba(88,166,255,0.15); color: var(--accent); }
.badge-purple { background: rgba(163,113,247,0.15); color: var(--purple); }
.metric-row { display: flex; gap: 24px; flex-wrap: wrap; margin-bottom: 8px; }
.metric { display: flex; gap: 6px; align-items: baseline; }
.metric .lbl { color: var(--muted); font-size: 12px; }
.metric .val { font-size: 15px; font-weight: 600; font-family: 'JetBrains Mono', monospace; }
.cluster-card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin-bottom: 12px; }
.cluster-card.real { border-left: 3px solid var(--green); }
.cluster-card.filtered { border-left: 3px solid var(--muted); opacity: 0.7; }
.cluster-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; cursor: pointer; }
.cluster-header .title { font-size: 15px; font-weight: 600; }
.cluster-body { display: none; }
.cluster-body.open { display: block; }
.address-table td { font-family: 'JetBrains Mono', monospace; font-size: 12px; }
.tag-row { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
.collapse-icon { font-size: 12px; color: var(--muted); transition: transform 0.2s; }
.collapse-icon.open { transform: rotate(90deg); }
</style>
</head>
<body>

<div class="nav-bar">
  <a href="/onchain">🔍 链上监控</a>
  <a href="/onchain/token" class="active">📋 集群监控</a>
  <a href="/alpha-cluster">🔬 Alpha 集群</a>
  <a href="/live-onchain">💹 实盘监控</a>
</div>

<h1 id="page-title">集群监控</h1>

<div class="panel" id="overview-panel">
  <div class="metric-row" id="overview-metrics"></div>
</div>

<div id="clusters-container"><div class="panel"><span style="color:var(--muted)">加载中...</span></div></div>

<script>
const url = new URL(window.location.href);
const token = url.pathname.split('/').pop().toUpperCase();
document.getElementById('page-title').textContent = '集群监控 — ' + token;

async function loadCluster() {
  try {
    const [clResp, poolResp, anaResp] = await Promise.all([
      fetch('/api/onchain/clusters?token=' + token),
      fetch('/api/onchain/pool'),
      fetch('/api/onchain/cluster/analysis?token=' + token)
    ]);
    const clusters = await clResp.json();
    const pool = await poolResp.json();
    const analysis = await anaResp.json();
    const analysisMap = {};
    for (const a of analysis) { analysisMap[a.address] = a; }
    const info = (Array.isArray(pool) ? pool : []).find(p => p.token === token);
    renderOverview(info);
    renderClusters(clusters, analysisMap);
  } catch(e) {
    console.error('loadCluster:', e);
    document.getElementById('clusters-container').innerHTML = '<div class="panel"><span style="color:var(--red)">加载失败</span></div>';
  }
}

function renderOverview(info) {
  const m = document.getElementById('overview-metrics');
  if (!info) { m.innerHTML = '<span style="color:var(--muted)">Token 不在观察池中</span>'; return; }
  const stageBadge = info.pool_stage === 'actionable'
    ? '<span class="badge badge-purple">🎯可建仓</span>'
    : '<span class="badge badge-green">●观察池</span>';
  m.innerHTML =
    '<div class="metric"><span class="lbl">代币</span><span class="val">' + esc(token) + '</span></div>' +
    '<div class="metric"><span class="lbl">链</span><span class="val">' + esc(info.chain||'bsc') + '</span></div>' +
    '<div class="metric"><span class="lbl">状态</span><span class="val">' + stageBadge + '</span></div>' +
    '<div class="metric"><span class="lbl">入池</span><span class="val">' + (info.days_in_pool||0).toFixed(1) + 'd</span></div>' +
    '<div class="metric"><span class="lbl">表层Top10</span><span class="val">' + (info.surface_top10_pct||0).toFixed(1) + '%</span></div>' +
    '<div class="metric"><span class="lbl">资金同源</span><span class="val">' + (info.fund_source_same_pct||0).toFixed(1) + '%</span></div>' +
    '<div class="metric"><span class="lbl">有效集中度</span><span class="val">' + ((info.effective_concentration||0)).toFixed(1) + '%</span></div>';
}

function renderClusters(clusters, analysisMap) {
  const container = document.getElementById('clusters-container');
  if (!clusters || !clusters.length) {
    container.innerHTML = '<div class="panel"><span style="color:var(--muted)">暂无集群数据（等待扫描器采集）</span></div>';
    return;
  }
  const visible = clusters.filter(c => (c.holding_percent || 0) > 0);
  if (!visible.length) {
    container.innerHTML = '<div class="panel"><span style="color:var(--muted)">暂无有效集群（持仓比例均为 0）</span></div>';
    return;
  }
  let html = '<h2>集群列表 (' + visible.length + ')</h2>';
  for (const cl of visible) {
    const isReal = cl.is_real_cluster === 1;
    const cls = isReal ? 'real' : 'filtered';
    const label = isReal ? '● 真人' : (cl.addresses_json||'').indexOf('"is_exchange":true') > -1 ? '交易所' : '合约';
    const openClass = isReal ? ' open' : '';
    const iconClass = isReal ? ' open' : '';

    // Tags
    const tags = cl.tags || [];
    let tagsHtml = '';
    for (const t of tags) {
      let cls2 = 'badge-blue';
      if (t.tag_type === 'holding_change') cls2 = t.tag_label.indexOf('增持') >= 0 ? 'badge-green' : (t.tag_label.indexOf('减持') >= 0 ? 'badge-red' : 'badge-yellow');
      else if (t.tag_type === 'common_holdings') cls2 = 'badge-purple';
      else if (t.tag_type === 'trade_behavior') cls2 = t.tag_label.indexOf('无卖出') >= 0 ? 'badge-green' : 'badge-yellow';
      tagsHtml += '<span class="badge ' + cls2 + '" title="' + esc(t.tag_detail||'') + '">' + esc(t.tag_label) + '</span> ';
    }

    html += '<div class="cluster-card ' + cls + '">' +
      '<div class="cluster-header" onclick="toggleCluster(this)">' +
        '<div>' +
          '<span class="title">集群 #' + (cl.cluster_index+1) + ' [' + label + ']</span>' +
          '<span style="margin-left:12px;font-size:13px;color:var(--muted)">控盘 ' + (cl.holding_percent||0).toFixed(1) + '%</span>' +
          '<span style="margin-left:8px;font-size:13px;color:var(--muted)">' + (cl.pnl_percent != null ? '盈亏 ' + (cl.pnl_percent>=0?'+':'') + cl.pnl_percent.toFixed(1) + '%' : '') + '</span>' +
          '<span style="margin-left:8px;font-size:13px;color:var(--muted)">' + cl.address_count + ' 地址</span>' +
        '</div>' +
        '<span class="collapse-icon ' + iconClass + '">▶</span>' +
      '</div>' +
      '<div class="tag-row">' + tagsHtml + '</div>' +
      '<div class="cluster-body ' + openClass + '">' +
        renderAddressTable(cl.addresses || [], analysisMap) +
      '</div>' +
    '</div>';
  }
  container.innerHTML = html;
}

function renderAddressTable(addresses, analysisMap) {
  if (!addresses || !addresses.length) return '<span style="color:var(--muted);font-size:12px">无地址数据</span>';
  let h = '<table class="address-table"><tr><th>地址</th><th>排名</th><th>持仓%</th><th>总资产</th><th>总买入/均价</th><th>总卖出/均价</th><th>最后交易</th><th>标签</th></tr>';
  for (const a of addresses) {
    const addr = (a.address||'').substring(0, 10) + '...' + (a.address||'').substring((a.address||'').length - 6);
    const wa = analysisMap[a.address] || {};
    const tv = wa.total_value_usd ? '$' + (wa.total_value_usd/1000).toFixed(0) + 'K' : '--';
    const buy = wa.buy_count_30d != null ? wa.buy_count_30d + '笔/$' + (wa.avg_buy_value||0).toFixed(0) : '--';
    const sell = wa.sell_count_30d != null ? wa.sell_count_30d + '笔/$' + (wa.avg_sell_value||0).toFixed(0) : '--';
    const lastTrade = wa.last_trade_time ? wa.last_trade_time.substring(0, 10) : '--';
    const tags = [];
    if (a.is_exchange) tags.push('<span class="badge badge-yellow">EX</span>');
    if (a.is_contract) tags.push('<span class="badge badge-yellow">CT</span>');
    if (a.is_kol) tags.push('<span class="badge badge-purple">KOL</span>');
    if (wa.buy_count_30d > 0 && wa.sell_count_30d === 0) tags.push('<span class="badge badge-green">纯买入</span>');
    if (wa.realized_pnl > 0) tags.push('<span class="badge badge-green">盈利</span>');
    h += '<tr>' +
      '<td title="' + esc(a.address||'') + '">' + esc(addr) + '</td>' +
      '<td>#' + (a.address_rank||'--') + '</td>' +
      '<td>' + ((a.holding_percent||0)*100).toFixed(2) + '%</td>' +
      '<td>' + tv + '</td>' +
      '<td>' + buy + '</td>' +
      '<td>' + sell + '</td>' +
      '<td>' + lastTrade + '</td>' +
      '<td>' + tags.join(' ') + '</td>' +
    '</tr>';
  }
  h += '</table>';
  return h;
}

function toggleCluster(el) {
  const card = el.parentElement;
  const body = card.querySelector('.cluster-body');
  const icon = card.querySelector('.collapse-icon');
  body.classList.toggle('open');
  icon.classList.toggle('open');
}

function esc(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

loadCluster();
setInterval(loadCluster, 60000);
</script>
</body>
</html>
"""


ONCHAIN_HTML = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>链上监控</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:opsz@14..32&family=JetBrains+Mono&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0d1117; --panel: #161b22; --text: #c9d1d9; --muted: #8b949e;
  --accent: #58a6ff; --border: #30363d; --red: #f85149; --green: #3fb950;
  --yellow: #d2991d; --orange: #db6d28; --purple: #a371f7;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font-family: 'Inter', sans-serif; padding: 20px; max-width: 1400px; margin: 0 auto; }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 20px; margin-bottom: 16px; }
h1 { font-size: 20px; margin-bottom: 20px; }
h2 { font-size: 16px; margin-bottom: 12px; color: var(--accent); }
.nav-bar { display: flex; justify-content: center; gap: 0; margin-bottom: 20px; border-bottom: 1px solid var(--border); padding-bottom: 0; flex-wrap: wrap; }
.nav-bar a { padding: 8px 16px; color: var(--muted); text-decoration: none; font-size: 12px; font-weight: 500; border-bottom: 2px solid transparent; transition: all 0.2s; white-space: nowrap; }
.nav-bar a.active, .nav-bar a:hover { color: var(--accent); border-bottom-color: var(--accent); }
.nav-dropdown { position: relative; display: inline-block; }
.nav-dropdown-content { display: none; position: absolute; background: var(--panel); border: 1px solid var(--border); border-radius: 6px; min-width: 200px; z-index: 100; }
.nav-dropdown:hover .nav-dropdown-content { display: block; }
.nav-dropdown-content a { display: block; padding: 8px 16px; color: var(--muted); text-decoration: none; font-size: 12px; }
.nav-dropdown-content a:hover { color: var(--accent); background: rgba(88,166,255,0.06); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--muted); font-weight: 500; padding: 8px 10px; border-bottom: 1px solid var(--border); font-size: 11px; text-transform: uppercase; }
td { padding: 8px 10px; border-bottom: 1px solid var(--border); }
tr:hover { background: rgba(88,166,255,0.04); }
.status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
.status-dot.running { background: var(--green); }
.status-dot.idle { background: var(--yellow); }
.status-dot.error { background: var(--red); }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 500; }
.badge-green { background: rgba(63,185,80,0.15); color: var(--green); }
.badge-yellow { background: rgba(210,153,29,0.15); color: var(--yellow); }
.badge-red { background: rgba(248,81,73,0.15); color: var(--red); }
.badge-blue { background: rgba(88,166,255,0.15); color: var(--accent); }
.badge-purple { background: rgba(163,113,247,0.15); color: var(--purple); }
.stat-row { display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 12px; }
.stat-item { display: flex; gap: 6px; align-items: baseline; }
.stat-label { color: var(--muted); font-size: 12px; }
.stat-value { font-size: 16px; font-weight: 600; font-family: 'JetBrains Mono', monospace; }
.empty { text-align: center; color: var(--muted); padding: 30px; font-size: 14px; }
.trend-up { color: var(--green); }
.trend-down { color: var(--red); }
.trend-flat { color: var(--yellow); }
.signal-strong { color: var(--green); font-weight: 600; }
.signal-weak { color: var(--yellow); }
.signal-none { color: var(--muted); }
.card-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 14px; }
.card:hover { border-color: var(--accent); }
.card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.card-token { font-size: 16px; font-weight: 700; }
.card-meta { font-size: 11px; color: var(--muted); display: flex; gap: 8px; align-items: center; }
.card-kline { width: 100%; height: 280px; border: 0; border-radius: 4px; margin-bottom: 8px; }
.card-metrics { display: flex; gap: 12px; flex-wrap: wrap; font-size: 12px; margin-bottom: 4px; }
.card-metrics .metric { display: flex; flex-direction: column; }
.card-metrics .metric .label { color: var(--muted); font-size: 10px; }
.card-metrics .metric .value { font-family: 'JetBrains Mono', monospace; font-weight: 600; }
.card-stage2 { border-top: 1px solid var(--border); padding-top: 6px; margin-top: 6px; font-size: 11px; display: flex; gap: 12px; }
.pagination { display: flex; justify-content: center; gap: 8px; margin-top: 12px; align-items: center; }
.pagination button { background: var(--panel); border: 1px solid var(--border); color: var(--text); padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 12px; }
.pagination button:hover { background: var(--accent); color: #fff; }
.pagination button:disabled { opacity: 0.3; cursor: default; }
.pagination span { color: var(--muted); font-size: 12px; }
</style>
</head>
<body>

<div class="nav-bar">
  <a href="/">📊 市场监控</a>
  <a href="/strategies">📈 运行策略</a>
  <div class="nav-dropdown">
    <a href="#">🤖 Agent 面板 ▾</a>
    <div class="nav-dropdown-content">
      <a href="/agent">Agent-合约扫描</a>
      <a href="/heat-agent">Agent-热度榜单</a>
      <a href="/agent-heat-lessons">Agent-热度有教训</a>
      <a href="/agent-nl">Agent-无教训</a>
      <a href="/agent-short">Agent-做空</a>
      <a href="/agent-kol">Agent-KOL 监控</a>
      <a href="/agent-kol-snapshot">Agent-KOL 快照监控</a>
    </div>
  </div>
  <a href="/dashboard">📊 市场全景</a>
  <a href="/onchain" class="active">🔍 链上监控</a>
  <a href="/alpha-cluster">🔬 Alpha 集群</a>
  <a href="/live-onchain">💹 实盘监控</a>
  <a href="/settings">⚙ 系统设置</a>
</div>

<h1>链上监控 — Stage 1 吸筹 & Stage 2 启动准备</h1>

<!-- Panel 1: Scanner Status -->
<div class="panel" id="status-panel">
  <h2>扫描状态</h2>
  <div class="stat-row">
    <div class="stat-item"><span class="stat-label">状态</span><span class="stat-value" id="st-stage">--</span></div>
    <div class="stat-item"><span class="stat-label">轮次</span><span class="stat-value" id="st-round">--</span></div>
    <div class="stat-item"><span class="stat-label">已扫描</span><span class="stat-value" id="st-scanned">--</span></div>
    <div class="stat-item"><span class="stat-label">观察池</span><span class="stat-value" id="st-pool">--</span></div>
    <div class="stat-item"><span class="stat-label">可建仓</span><span class="stat-value" id="st-action">--</span></div>
  </div>
  <div style="color:var(--muted);font-size:12px" id="st-detail">--</div>
</div>

<!-- Panel 2: Observation Pool -->
<div class="panel">
  <h2>观察池 (Stage 1 — 吸筹检测)</h2>
  <div id="obs-grid"><div class="empty">加载中...</div></div>
  <div class="pagination" id="obs-pager"></div>
</div>

<!-- Panel 3: Actionable -->
<div class="panel">
  <h2>可建仓 (Stage 2 — 启动准备)</h2>
  <div id="act-grid"><div class="empty">加载中...</div></div>
  <div class="pagination" id="act-pager"></div>
</div>

<!-- Blacklist Management -->
<div class="panel">
  <h2 style="cursor:pointer" onclick="document.getElementById('blacklist-body').classList.toggle('hidden')">🚫 黑名单管理 ▾</h2>
  <div id="blacklist-body" class="hidden">
    <div id="blacklist-table"><span style="color:var(--muted)">加载中...</span></div>
  </div>
</div>

<!-- Removed List -->
<div class="panel">
  <h2 style="cursor:pointer" onclick="document.getElementById('removed-body').classList.toggle('hidden')">📋 移除名单 ▾</h2>
  <div id="removed-body" class="hidden">
    <div id="removed-table"><span style="color:var(--muted)">加载中...</span></div>
  </div>
</div>

<style>
.hidden { display: none; }
</style>

<script>
async function loadOnchain() {
  try {
    const [stResp, obsResp, actResp] = await Promise.all([
      fetch('/api/onchain/status'),
      fetch('/api/onchain/pool?stage=observation'),
      fetch('/api/onchain/pool?stage=actionable')
    ]);
    const st = await stResp.json();
    const obs = await obsResp.json();
    const act = await actResp.json();
    renderStatus(st);
    renderPool('obs-grid', 'obs-pager', obs, 'observation');
    renderPool('act-grid', 'act-pager', act, 'actionable');
    loadBlacklist();
    loadRemoved();
  } catch(e) {
    console.error('loadOnchain:', e);
  }
}

async function loadBlacklist() {
  try {
    const r = await fetch('/api/onchain/blacklist');
    const list = await r.json();
    const el = document.getElementById('blacklist-table');
    if (!list || !list.length) {
      el.innerHTML = '<span style="color:var(--muted)">黑名单为空</span>';
      return;
    }
    let h = '<table><thead><tr><th>Token</th><th>添加时间</th><th>操作</th></tr></thead><tbody>';
    for (const b of list) {
      h += '<tr><td style="font-weight:bold">' + esc(b.token) + '</td><td>' + (b.added_at || '') + '</td>' +
        '<td><button data-token="' + b.token + '" onclick="removeFromBlacklist(this.dataset.token)" style="background:var(--green);color:#fff;border:none;padding:3px 10px;border-radius:4px;cursor:pointer;font-size:11px">恢复</button></td></tr>';
    }
    h += '</tbody></table>';
    el.innerHTML = h;
  } catch(e) {
    console.error('loadBlacklist:', e);
  }
}

async function addToBlacklist(token) {
  if (!confirm('将 ' + token + ' 移入黑名单？移入后不会再进入观察池。')) return;
  try {
    await fetch('/api/onchain/blacklist/add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token})
    });
    loadOnchain();
  } catch(e) {
    console.error('addToBlacklist:', e);
  }
}

async function removeFromBlacklist(token) {
  if (!confirm('将 ' + token + ' 移出黑名单？移出后可重新进入观察池。')) return;
  try {
    await fetch('/api/onchain/blacklist/remove', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token})
    });
    loadOnchain();
  } catch(e) {
    console.error('removeFromBlacklist:', e);
  }
}

async function removeFromActionable(token) {
  if (!confirm('将 ' + token + ' 从可建仓池移除？')) return;
  try {
    await fetch('/api/onchain/removed/add', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token, reason: 'manual'})
    });
    loadOnchain();
  } catch(e) {
    console.error('removeFromActionable:', e);
  }
}

async function loadRemoved() {
  try {
    const r = await fetch('/api/onchain/removed');
    const list = await r.json();
    const el = document.getElementById('removed-table');
    if (!list || !list.length) {
      el.innerHTML = '<span style="color:var(--muted)">移除名单为空</span>';
      return;
    }
    let h = '<table><thead><tr><th>Token</th><th>原因</th><th>时间</th></tr></thead><tbody>';
    for (const b of list) {
      const reason = b.reason === 'closed' ? '平仓' : '手动';
      h += '<tr><td style="font-weight:bold">' + esc(b.token) + '</td><td>' + reason + '</td><td>' + (b.added_at || '') + '</td></tr>';
    }
    h += '</tbody></table>';
    el.innerHTML = h;
  } catch(e) {
    console.error('loadRemoved:', e);
  }
}

function renderStatus(st) {
  if (!st) return;
  const stageEl = document.getElementById('st-stage');
  stageEl.textContent = st.stage || '--';
  const dot = (st.stage === 'idle') ? 'idle' : (st.stage === 'error' ? 'error' : 'running');
  stageEl.innerHTML = '<span class="status-dot '+dot+'"></span>' + stageEl.textContent;
  document.getElementById('st-round').textContent = '#' + (st.round_number || 0);
  document.getElementById('st-scanned').textContent = st.tokens_scanned || 0;
  document.getElementById('st-pool').textContent = st.pool_size || 0;
  document.getElementById('st-action').textContent = st.actionable_size || 0;
  document.getElementById('st-detail').textContent = st.detail || '';
}

function renderPool(gridId, pagerId, data, stage) {
  const grid = document.getElementById(gridId);
  const pager = document.getElementById(pagerId);
  if (!data || !data.length) {
    grid.innerHTML = '<div class="empty">暂无' + (stage === 'observation' ? '观察池' : '可建仓') + '候选</div>';
    pager.innerHTML = '';
    return;
  }

  const perPage = 4;
  const totalPages = Math.ceil(data.length / perPage);
  let page = parseInt(grid.getAttribute('data-page') || '0');
  if (page >= totalPages) page = 0;
  grid.setAttribute('data-page', page);

  const items = data.slice(page * perPage, page * perPage + perPage);
  const parts = ['<div class="card-grid">'];
  for (const p of items) {
    const sym = 'BINANCE%3A' + p.token + 'USDT.P';
    const eff = p.effective_concentration ? p.effective_concentration.toFixed(1) : '--';
    const top10 = p.surface_top10_pct ? p.surface_top10_pct.toFixed(1) : '--';
    const fund = p.fund_source_same_pct ? p.fund_source_same_pct.toFixed(1) : '--';
    const days = p.days_in_pool ? p.days_in_pool.toFixed(1) : '0.0';
    const spark = sparklineSVG(p.sparkline || []);

    let stageRow = '';
    if (stage === 'actionable') {
      const lp = p.lp_reserve_trend || '--';
      const gas = p.gas_seeding_signal >= 2 ? '<span class="signal-strong">强</span>' : (p.gas_seeding_signal >= 1 ? '<span class="signal-weak">弱</span>' : '<span class="signal-none">--</span>');
      const cex = p.cex_staging_signal >= 2 ? '<span class="signal-strong">强</span>' : (p.cex_staging_signal >= 1 ? '<span class="signal-weak">弱</span>' : '<span class="signal-none">--</span>');
      const conf = p.confidence_score ? p.confidence_score.toFixed(0) + '%' : '--';
      stageRow = '<div class="card-stage2"><span>LP: ' + lp + '</span><span>Gas: ' + gas + '</span><span>CEX: ' + cex + '</span><span>置信度: <b>' + conf + '</b></span></div>';
    }

    const badge = stage === 'actionable'
      ? '<span class="badge badge-purple">🎯可建仓</span>'
      : (p.divergence_detected ? '<span class="badge badge-green">●观察</span>' : '<span class="badge badge-yellow">--</span>');

    parts.push('<div class="card">' +
      '<div class="card-header">' +
        '<span class="card-token"><a href="/onchain/token/' + encodeURIComponent(p.token) + '" style="color:inherit;text-decoration:none" title="查看集群详情">' + esc(p.token) + '</a></span>' +
        '<div class="card-meta">' + esc(p.chain) + ' ' + badge + ' ' + days + 'd' +
          (stage === 'observation' ? ' <button data-token="' + p.token + '" onclick="addToBlacklist(this.dataset.token)" title="移入黑名单" style="background:none;border:none;cursor:pointer;font-size:12px;margin-left:4px">🚫</button>' : '') +
          (stage === 'actionable' ? ' <button data-token="' + p.token + '" onclick="removeFromActionable(this.dataset.token)" title="从可建仓移除" style="background:none;border:none;cursor:pointer;font-size:12px;margin-left:4px">🗑</button>' : '') +
        '</div>' +
      '</div>' +
      '<iframe class="card-kline" src="https://s.tradingview.com/widgetembed/?symbol=' + sym + '&interval=60&theme=dark&style=1&width=100%25&height=100%25&hide_side_toolbar=1&withdateranges=0&save_image=0&studies="></iframe>' +
      '<div class="card-metrics">' +
        '<div class="metric"><div class="label">表层Top10</div><div class="value">' + top10 + '%</div></div>' +
        '<div class="metric"><div class="label">资金同源</div><div class="value">' + fund + '%</div></div>' +
        '<div class="metric"><div class="label">有效集中度</div><div class="value">' + eff + '%</div></div>' +
        '<div class="metric"><div class="label">趋势</div><div class="value">' + spark + '</div></div>' +
      '</div>' +
      stageRow +
    '</div>');
  }
  // Fill empty slots to maintain grid
  for (let i = items.length; i < perPage; i++) {
    parts.push('<div class="card" style="visibility:hidden"></div>');
  }
  parts.push('</div>');
  grid.innerHTML = parts.join('');

  // Pagination
  let pHtml = '';
  pHtml += '<button ' + (page === 0 ? 'disabled' : '') + ' onclick="goPage(' + "'" + gridId + "','" + pagerId + "','" + stage + "'," + (page - 1) + ')">◀</button>';
  pHtml += '<span>' + (page + 1) + ' / ' + totalPages + '</span>';
  pHtml += '<button ' + (page >= totalPages - 1 ? 'disabled' : '') + ' onclick="goPage(' + "'" + gridId + "','" + pagerId + "','" + stage + "'," + (page + 1) + ')">▶</button>';
  pager.innerHTML = pHtml;
}

function goPage(gridId, pagerId, stage, page) {
  document.getElementById(gridId).setAttribute('data-page', page);
  loadOnchain();
}

function sparklineSVG(values) {
  if (!values || values.length < 2) return '--';
  const valid = values.filter(v => v != null);
  if (valid.length < 2) return '--';
  const min = Math.min(...valid);
  const max = Math.max(...valid);
  const range = max - min || 1;
  const h = 20, w = valid.length * 8;
  let points = '';
  for (let i = 0; i < valid.length; i++) {
    const x = (i / (valid.length - 1)) * w;
    const y = h - ((valid[i] - min) / range) * h;
    points += x.toFixed(1) + ',' + y.toFixed(1) + ' ';
  }
  const trend = valid[valid.length-1] > valid[0] ? '↑' : (valid[valid.length-1] < valid[0] ? '↓' : '→');
  const color = trend === '↑' ? '#3fb950' : (trend === '↓' ? '#f85149' : '#d2991d');
  return '<svg width="' + w + '" height="' + h + '" style="vertical-align:middle"><polyline points="' + points.trim() + '" fill="none" stroke="' + color + '" stroke-width="1.5"/></svg> ' + trend + ' ' + valid[valid.length-1].toFixed(1) + '%';
}

function esc(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

loadOnchain();
setInterval(loadOnchain, 60000);
</script>
</body>
</html>
"""


@app.get("/onchain", response_class=HTMLResponse)
def onchain_page():
    return HTMLResponse(
        content=ONCHAIN_HTML,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


@app.get("/onchain/token/{token}", response_class=HTMLResponse)
def cluster_page(token: str):
    return HTMLResponse(
        content=CLUSTER_HTML,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


ALPHA_CLUSTER_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Alpha 集群查询</title>
<style>
:root { --bg: #0d1117; --panel: #161b22; --border: #30363d; --text: #e6edf3; --muted: #8b949e; --accent: #1f6feb; --green: #3fb950; --red: #f85149; --purple: #a371f7; --yellow: #d2991d; }
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: var(--bg); color: var(--text); font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; padding: 20px; }
.nav-bar { display: flex; gap: 16px; align-items: center; margin-bottom: 20px; flex-wrap: wrap; padding: 12px 16px; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; }
.nav-bar a { color: var(--muted); text-decoration: none; font-size: 14px; }
.nav-bar a:hover { color: var(--text); }
.nav-bar a.active { color: var(--accent); font-weight: 600; }
.nav-dropdown { position: relative; display: inline-block; }
.nav-dropdown > a { cursor: pointer; }
.nav-dropdown-content { display: none; position: absolute; top: 100%; left: 0; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; min-width: 200px; z-index: 10; padding: 8px 0; }
.nav-dropdown:hover .nav-dropdown-content { display: block; }
.nav-dropdown-content a { display: block; padding: 6px 16px; color: var(--muted); font-size: 13px; white-space: nowrap; }
.nav-dropdown-content a:hover { color: var(--text); background: rgba(88,166,255,0.08); }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 16px; }
.metric-row { display: flex; gap: 20px; flex-wrap: wrap; }
.metric { display: flex; flex-direction: column; gap: 2px; }
.metric .lbl { font-size: 11px; color: var(--muted); }
.metric .val { font-size: 18px; font-weight: 700; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; margin-right: 4px; }
.badge-green { background: #1a3a2a; color: var(--green); }
.badge-red { background: #3a1a1a; color: var(--red); }
.badge-purple { background: #2a1a3a; color: var(--purple); }
.badge-blue { background: #1a2a3a; color: #58a6ff; }
.badge-yellow { background: #3a2a1a; color: var(--yellow); }
.cluster-card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 14px; margin-bottom: 12px; }
.cluster-card.real { border-left: 3px solid var(--green); }
.cluster-card.filtered { border-left: 3px solid var(--muted); opacity: 0.7; }
.cluster-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; cursor: pointer; }
.cluster-header .title { font-size: 15px; font-weight: 600; }
.cluster-body { display: none; }
.cluster-body.open { display: block; }
.tag-row { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
.address-table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 8px; }
.address-table th { background: var(--bg); padding: 6px 8px; text-align: left; color: var(--muted); font-weight: 600; border-bottom: 1px solid var(--border); }
.address-table td { padding: 6px 8px; border-bottom: 1px solid var(--border); font-family: 'JetBrains Mono', monospace; }
.collapse-icon { transition: transform 0.2s; font-size: 12px; color: var(--muted); }
.collapse-icon.open { transform: rotate(90deg); }
.input-row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.input-row input { background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 8px 14px; border-radius: 6px; font-size: 14px; width: 180px; }
.input-row select { background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 8px 14px; border-radius: 6px; font-size: 14px; }
.input-row button { background: var(--accent); border: none; color: #fff; padding: 8px 18px; border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: 600; }
.input-row button:hover { opacity: 0.85; }
.input-row button:disabled { opacity: 0.5; cursor: not-allowed; }
.loading { color: var(--muted); font-size: 13px; }
.error-msg { color: var(--red); font-size: 13px; }
</style>
</head>
<body>

<div class="nav-bar">
  <a href="/">📊 市场监控</a>
  <a href="/strategies">📈 运行策略</a>
  <div class="nav-dropdown">
    <a href="#">🤖 Agent 面板 ▾</a>
    <div class="nav-dropdown-content">
      <a href="/agent">Agent-合约扫描</a>
      <a href="/heat-agent">Agent-热度榜单</a>
      <a href="/agent-heat-lessons">Agent-热度有教训</a>
      <a href="/agent-nl">Agent-无教训</a>
      <a href="/agent-short">Agent-做空</a>
      <a href="/agent-kol">Agent-KOL 监控</a>
      <a href="/agent-kol-snapshot">Agent-KOL 快照监控</a>
    </div>
  </div>
  <a href="/dashboard">📊 市场全景</a>
  <a href="/onchain">🔍 链上监控</a>
  <a href="/alpha-cluster" class="active">🔬 Alpha 集群查询</a>
  <a href="/live-onchain">💹 实盘监控</a>
  <a href="/settings">⚙ 系统设置</a>
</div>

<h1>🔬 Alpha 集群查询</h1>
<p style="color:var(--muted);font-size:13px;margin-bottom:16px">输入任意 Token 符号，实时拉取链上集群数据</p>

<div class="panel">
  <div class="input-row">
    <input id="token-input" type="text" placeholder="Token 符号, 如 PEPE" value="">
    <select id="chain-select">
      <option value="bsc" selected>BSC</option>
      <option value="ethereum">Ethereum</option>
      <option value="solana">Solana</option>
      <option value="arbitrum">Arbitrum</option>
      <option value="base">Base</option>
      <option value="polygon">Polygon</option>
    </select>
    <button id="fetch-btn" onclick="doFetch()">🔍 查询</button>
    <button id="refresh-btn" onclick="doRefresh()" disabled>🔄 刷新</button>
    <span id="status-msg"></span>
  </div>
</div>

<div class="panel" id="overview-panel" style="display:none">
  <div class="metric-row" id="overview-metrics"></div>
</div>

<div id="clusters-container"></div>

<script>
const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');

let _lastToken = '';
let _lastChain = 'bsc';
let _lastData = null;

async function doFetch() {
  const token = document.getElementById('token-input').value.trim();
  const chain = document.getElementById('chain-select').value;
  if (!token) return;
  _lastToken = token;
  _lastChain = chain;
  await _fetch(token, chain);
}

async function doRefresh() {
  if (!_lastToken) return;
  await _fetch(_lastToken, _lastChain);
}

async function _fetch(token, chain) {
  const btn = document.getElementById('fetch-btn');
  const refreshBtn = document.getElementById('refresh-btn');
  const status = document.getElementById('status-msg');
  btn.disabled = true;
  refreshBtn.disabled = true;
  status.innerHTML = '<span class="loading">⏳ 查询中...</span>';
  document.getElementById('clusters-container').innerHTML = '';

  try {
    const resp = await fetch('/api/alpha/cluster/fetch', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({token, chain})
    });
    if (!resp.ok) {
      const err = await resp.json();
      status.innerHTML = '<span class="error-msg">❌ ' + esc(err.detail||'查询失败') + '</span>';
      btn.disabled = false;
      return;
    }
    const data = await resp.json();
    _lastData = data;
    status.innerHTML = '<span style="color:var(--green)">✅ 查询完成</span>';
    refreshBtn.disabled = false;
    renderOverview(data.overview);
    renderClusters(data.clusters, data.analysis || {});
  } catch(e) {
    status.innerHTML = '<span class="error-msg">❌ 网络错误: ' + esc(e.message) + '</span>';
  }
  btn.disabled = false;
}

function renderOverview(info) {
  const panel = document.getElementById('overview-panel');
  if (!info) { panel.style.display = 'none'; return; }
  panel.style.display = '';
  const m = document.getElementById('overview-metrics');
  m.innerHTML =
    '<div class="metric"><span class="lbl">代币</span><span class="val">' + esc(info.token||'') + '</span></div>' +
    '<div class="metric"><span class="lbl">链</span><span class="val">' + esc(info.chain||'') + '</span></div>' +
    '<div class="metric"><span class="lbl">MCap</span><span class="val">' + (info.market_cap_usd ? '$' + (info.market_cap_usd/1000000).toFixed(1) + 'M' : '--') + '</span></div>' +
    '<div class="metric"><span class="lbl">表层Top10</span><span class="val">' + (info.top10_hold_pct||0).toFixed(1) + '%</span></div>' +
    '<div class="metric"><span class="lbl">资金同源</span><span class="val">' + (info.fund_source_same_pct||0).toFixed(1) + '%</span></div>' +
    '<div class="metric"><span class="lbl">有效集中度</span><span class="val">' + ((info.effective_concentration||0)).toFixed(1) + '%</span></div>' +
    '<div class="metric"><span class="lbl">地址</span><span class="val" style="font-size:13px">' + esc((info.token_address||'').substring(0,10)+'...') + '</span></div>';
}

function renderClusters(clusters, analysisMap) {
  const container = document.getElementById('clusters-container');
  if (!clusters || !clusters.length) {
    container.innerHTML = '<div class="panel"><span style="color:var(--muted)">无集群数据</span></div>';
    return;
  }
  const visible = clusters.filter(c => (c.holding_percent || 0) > 0);
  if (!visible.length) {
    container.innerHTML = '<div class="panel"><span style="color:var(--muted)">暂无有效集群（持仓比例均为 0）</span></div>';
    return;
  }
  let html = '<h2>集群列表 (' + visible.length + ')</h2>';
  for (const cl of visible) {
    const isReal = cl.is_real_cluster === 1;
    const cls = isReal ? 'real' : 'filtered';
    const label = isReal ? '● 真人' : ((cl.addresses||[]).some(a => a.is_exchange) ? '交易所' : '合约');
    const openClass = isReal ? ' open' : '';
    const iconClass = isReal ? ' open' : '';

    html += '<div class="cluster-card ' + cls + '">' +
      '<div class="cluster-header" onclick="toggleCluster(this)">' +
        '<div>' +
          '<span class="title">集群 #' + (cl.cluster_index+1) + ' [' + label + ']</span>' +
          '<span style="margin-left:12px;font-size:13px;color:var(--muted)">控盘 ' + (cl.holding_percent||0).toFixed(1) + '%</span>' +
          '<span style="margin-left:8px;font-size:13px;color:var(--muted)">' + (cl.pnl_percent != null ? '盈亏 ' + (cl.pnl_percent>=0?'+':'') + cl.pnl_percent.toFixed(1) + '%' : '') + '</span>' +
          '<span style="margin-left:8px;font-size:13px;color:var(--muted)">' + (cl.address_count||0) + ' 地址</span>' +
        '</div>' +
        '<span class="collapse-icon ' + iconClass + '">▶</span>' +
      '</div>' +
      '<div class="tag-row">' + renderTags(cl.tags || []) + '</div>' +
      '<div class="cluster-body ' + openClass + '">' +
        renderAddressTable(cl.addresses || [], analysisMap) +
      '</div>' +
    '</div>';
  }
  container.innerHTML = html;
}

function renderAddressTable(addresses, analysisMap) {
  if (!addresses || !addresses.length) return '<span style="color:var(--muted);font-size:12px">无地址数据</span>';
  const chain = _lastChain || 'bsc';
  let h = '<table class="address-table"><tr><th>地址</th><th>排名</th><th>持仓%</th><th>总资产</th><th>总买入/均价</th><th>总卖出/均价</th><th>最后交易</th><th>标签</th></tr>';
  for (const a of addresses) {
    const fullAddr = a.address || '';
    const addr = fullAddr.substring(0, 10) + '...' + fullAddr.substring(fullAddr.length - 6);
    const addrLink = 'https://debot.ai/address/' + chain + '/' + fullAddr;
    const wa = analysisMap[a.address] || {};
    const tv = wa.total_value_usd ? '$' + (wa.total_value_usd/1000).toFixed(0) + 'K' : '--';
    const buy = wa.buy_count_30d != null ? wa.buy_count_30d + '笔/$' + (wa.avg_buy_value||0).toFixed(0) : '--';
    const sell = wa.sell_count_30d != null ? wa.sell_count_30d + '笔/$' + (wa.avg_sell_value||0).toFixed(0) : '--';
    const lastTrade = wa.last_trade_time ? wa.last_trade_time.substring(0, 10) : '--';
    const tags = [];
    if (a.is_exchange) tags.push('<span class="badge badge-yellow">EX</span>');
    if (a.is_contract) tags.push('<span class="badge badge-yellow">CT</span>');
    if (a.is_kol) tags.push('<span class="badge badge-purple">KOL</span>');
    if (wa.buy_count_30d > 0 && wa.sell_count_30d === 0) tags.push('<span class="badge badge-green">纯买入</span>');
    if (wa.realized_pnl > 0) tags.push('<span class="badge badge-green">盈利</span>');
    h += '<tr>' +
      '<td><a href="' + addrLink + '" target="_blank" title="' + esc(fullAddr) + '" style="color:var(--accent);text-decoration:none">' + esc(addr) + '</a></td>' +
      '<td>#' + (a.address_rank||'--') + '</td>' +
      '<td>' + ((a.holding_percent||0)*100).toFixed(2) + '%</td>' +
      '<td>' + tv + '</td>' +
      '<td>' + buy + '</td>' +
      '<td>' + sell + '</td>' +
      '<td>' + lastTrade + '</td>' +
      '<td>' + tags.join(' ') + '</td>' +
    '</tr>';
  }
  h += '</table>';
  return h;
}

function renderTags(tags) {
  if (!tags || !tags.length) return '';
  let h = '';
  for (const t of tags) {
    let cls = 'badge-blue';
    if (t.tag_type === 'holding_change') cls = t.tag_label.indexOf('增持') >= 0 ? 'badge-green' : (t.tag_label.indexOf('减持') >= 0 ? 'badge-red' : 'badge-yellow');
    else if (t.tag_type === 'common_holdings') cls = 'badge-purple';
    else if (t.tag_type === 'trade_behavior') cls = t.tag_label.indexOf('无卖出') >= 0 ? 'badge-green' : 'badge-yellow';
    h += '<span class="badge ' + cls + '" title="' + esc(t.tag_detail||'') + '">' + esc(t.tag_label) + '</span> ';
  }
  return h;
}

function toggleCluster(el) {
  const card = el.closest('.cluster-card');
  const body = card.querySelector('.cluster-body');
  const icon = card.querySelector('.collapse-icon');
  body.classList.toggle('open');
  icon.classList.toggle('open');
}

// 输入框回车触发查询
document.getElementById('token-input').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') doFetch();
});
</script>
</body>
</html>
"""


@app.get("/alpha-cluster", response_class=HTMLResponse)
def alpha_cluster_page():
    return HTMLResponse(
        content=ALPHA_CLUSTER_HTML,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


@app.get("/strategies", response_class=HTMLResponse)
def strategies_page():
    return HTMLResponse(
        content=STRATEGIES_HTML,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


@app.get("/agent", response_class=HTMLResponse)
def agent_monitor():
    return HTMLResponse(
        content=AGENT_HTML,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


LIVE_ONCHAIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>链上实盘监控</title>
<style>
:root { --bg: #0d1117; --panel: #161b22; --border: #30363d; --text: #e6edf3; --muted: #8b949e; --accent: #1f6feb; --green: #3fb950; --red: #f85149; }
* { margin: 0; padding: 0; box-sizing: border-box; }
body { background: var(--bg); color: var(--text); font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; padding: 20px; }
.nav-bar { display: flex; gap: 16px; align-items: center; margin-bottom: 20px; flex-wrap: wrap; padding: 12px 16px; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; }
.nav-bar a { color: var(--muted); text-decoration: none; font-size: 14px; }
.nav-bar a:hover { color: var(--text); }
.nav-bar a.active { color: var(--accent); font-weight: 600; }
.nav-dropdown { position: relative; display: inline-block; }
.nav-dropdown > a { cursor: pointer; }
.nav-dropdown-content { display: none; position: absolute; top: 100%; left: 0; background: var(--panel); border: 1px solid var(--border); border-radius: 8px; min-width: 200px; z-index: 10; padding: 8px 0; }
.nav-dropdown:hover .nav-dropdown-content { display: block; }
.nav-dropdown-content a { display: block; padding: 6px 16px; color: var(--muted); font-size: 13px; white-space: nowrap; }
.nav-dropdown-content a:hover { color: var(--text); background: rgba(88,166,255,0.08); }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 16px; }
h1 { font-size: 20px; margin-bottom: 20px; }
h2 { font-size: 16px; margin-bottom: 12px; color: var(--accent); }
.metric-row { display: flex; gap: 24px; flex-wrap: wrap; }
.metric { display: flex; flex-direction: column; gap: 2px; }
.metric .lbl { font-size: 11px; color: var(--muted); }
.metric .val { font-size: 20px; font-weight: 700; }
.green { color: var(--green); }
.red { color: var(--red); }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--muted); font-weight: 500; padding: 8px 10px; border-bottom: 1px solid var(--border); font-size: 11px; }
td { padding: 8px 10px; border-bottom: 1px solid var(--border); }
tr:hover { background: rgba(88,166,255,0.04); }
.right { text-align: right; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
.badge-green { background: #1a3a2a; color: var(--green); }
.badge-red { background: #3a1a1a; color: var(--red); }
</style>
</head>
<body>

<div class="nav-bar">
  <a href="/">📊 市场监控</a>
  <a href="/strategies">📈 运行策略</a>
  <div class="nav-dropdown">
    <a href="#">🤖 Agent 面板 ▾</a>
    <div class="nav-dropdown-content">
      <a href="/agent">Agent-合约扫描</a>
      <a href="/heat-agent">Agent-热度榜单</a>
      <a href="/agent-heat-lessons">Agent-热度有教训</a>
      <a href="/agent-nl">Agent-无教训</a>
      <a href="/agent-short">Agent-做空</a>
      <a href="/agent-kol">Agent-KOL 监控</a>
      <a href="/agent-kol-snapshot">Agent-KOL 快照监控</a>
    </div>
  </div>
  <a href="/dashboard">📊 市场全景</a>
  <a href="/onchain">🔍 链上监控</a>
  <a href="/alpha-cluster">🔬 Alpha 集群</a>
  <a href="/live-onchain" class="active">💹 实盘监控</a>
  <a href="/settings">⚙ 系统设置</a>
</div>

<h1>💹 链上实盘监控</h1>

<div class="panel" id="overview-panel">
  <h2>账户概览</h2>
  <div class="metric-row" id="overview-metrics">
    <div class="metric"><div class="lbl">账户权益</div><div class="val" id="ov-equity">--</div></div>
    <div class="metric"><div class="lbl">可用余额</div><div class="val" id="ov-available">--</div></div>
    <div class="metric"><div class="lbl">占用保证金</div><div class="val" id="ov-margin">--</div></div>
    <div class="metric"><div class="lbl">未实现盈亏</div><div class="val" id="ov-uniq">--</div></div>
    <div class="metric"><div class="lbl">当前持仓</div><div class="val" id="ov-count">--</div></div>
  </div>
</div>

<div class="panel">
  <h2>当前持仓</h2>
  <div id="positions-table"><span style="color:var(--muted)">加载中...</span></div>
</div>

<div class="panel">
  <h2>交易日志</h2>
  <div id="trades-table"><span style="color:var(--muted)">加载中...</span></div>
</div>

<script>
const fmtUsd = v => (v!=null && !isNaN(Number(v))) ? '$'+Number(v).toFixed(2) : '--';
const fmtPct = v => (v!=null && !isNaN(Number(v))) ? (v>=0?'+':'')+Number(v).toFixed(2)+'%' : '--';
const fmtPrice = v => (v==null||isNaN(Number(v)))?'—':(Number(v)>=1?Number(v).toFixed(4):Number(v).toFixed(6));

async function load() {
  try {
    const [ov, pos, trades] = await Promise.all([
      fetch('/api/live-onchain/overview'),
      fetch('/api/live-onchain/positions'),
      fetch('/api/live-onchain/trades')
    ]);
    const o = await ov.json();
    document.getElementById('ov-equity').textContent = fmtUsd(o.equity);
    document.getElementById('ov-available').textContent = fmtUsd(o.available_balance);
    document.getElementById('ov-margin').textContent = fmtUsd(o.margin_used);
    const pnl = o.unrealized_pnl || 0;
    const pnlEl = document.getElementById('ov-uniq');
    pnlEl.textContent = fmtUsd(pnl);
    pnlEl.className = 'val ' + (pnl >= 0 ? 'green' : 'red');
    document.getElementById('ov-count').textContent = o.open_count || 0;

    const positions = await pos.json();
    renderPositions(positions);

    const tradeList = await trades.json();
    renderTrades(tradeList);
  } catch(e) {
    console.error('load:', e);
  }
}

function renderPositions(list) {
  const el = document.getElementById('positions-table');
  if (!list || !list.length) {
    el.innerHTML = '<span style="color:var(--muted)">无活跃持仓</span>';
    return;
  }
  let h = '<table><thead><tr><th>Token</th><th>方向</th><th class="right">入场价</th><th class="right">现价</th><th class="right">数量</th><th class="right">PnL%</th><th class="right">未实现</th><th>状态</th><th>开仓时间</th></tr></thead><tbody>';
  for (const p of list) {
    const pnlCls = (p.ex_unrealized_pnl||0) >= 0 ? 'green' : 'red';
    h += '<tr>' +
      '<td style="font-weight:bold;color:var(--accent)">' + (p.token||'') + '</td>' +
      '<td>' + (p.side||'') + '</td>' +
      '<td class="right">' + fmtPrice(p.entry_price) + '</td>' +
      '<td class="right">' + fmtPrice(p.mark_price) + '</td>' +
      '<td class="right">' + (p.ex_quantity||p.quantity||'--') + '</td>' +
      '<td class="right ' + pnlCls + '">' + fmtPct((p.ex_unrealized_pnl||0)/(p.margin_usd||1)*100) + '</td>' +
      '<td class="right ' + pnlCls + '">' + fmtUsd(p.ex_unrealized_pnl) + '</td>' +
      '<td>' + p.status + '</td>' +
      '<td>' + (p.created_at||'').substring(0,16) + '</td>' +
    '</tr>';
  }
  h += '</tbody></table>';
  el.innerHTML = h;
}

function renderTrades(list) {
  const el = document.getElementById('trades-table');
  if (!list || !list.length) {
    el.innerHTML = '<span style="color:var(--muted)">无交易记录</span>';
    return;
  }
  let h = '<table><thead><tr><th>Token</th><th>操作</th><th class="right">价格</th><th class="right">数量</th><th>时间</th></tr></thead><tbody>';
  for (const t of list.slice(0,20)) {
    const actionLabel = t.action === 'open' ? '🔵开仓' : t.action === 'close' ? '🔴平仓' : t.action;
    h += '<tr>' +
      '<td style="font-weight:bold">' + (t.token||'') + '</td>' +
      '<td>' + actionLabel + '</td>' +
      '<td class="right">' + fmtPrice(t.price) + '</td>' +
      '<td class="right">' + (t.quantity||'--') + '</td>' +
      '<td>' + (t.created_at||'').substring(0,16) + '</td>' +
    '</tr>';
  }
  h += '</tbody></table>';
  el.innerHTML = h;
}

load();
setInterval(load, 30000);
</script>
</body>
</html>
"""


@app.get("/live-onchain", response_class=HTMLResponse)
def live_onchain_page():
    return HTMLResponse(
        content=LIVE_ONCHAIN_HTML,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(
        content=HTML,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


if __name__ == "__main__":
    storage.init_db()  # 保证表存在，即使 worker 没先跑
    print(f"=> Web 仪表盘启动：http://{config.WEB_HOST}:{config.WEB_PORT}")
    print(f"=> 记得另开一个终端运行 python worker.py 采数据")
    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT, log_level="warning")
