"""
Web 仪表盘服务
运行：python web.py
访问：http://localhost:8000
"""
import json
import time
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import uvicorn

import config
import storage
import trade_logic
from analyzer import compute_short_scores, compute_composite_scores
from market import has_perpetual, get_market_snapshot, get_futures_symbols
from signals import analyze as analyze_signals


app = FastAPI(title="Binance Square Monitor")


# ============================================================
# 轻量内存缓存：给高频只读 API 加 2 秒 TTL
# 目的：worker 正在跑重活时，前端刷新不会每次都挤进 SQLite 排队
# ============================================================
_cache = {}
_cache_lock = threading.Lock()
_max_dd_cache = {"value": 0.0, "time": 0}


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


class TokenBody(BaseModel):
    token: str


class TradingSettingsBody(BaseModel):
    enabled: bool | None = None
    mode: str | None = None
    initial_balance: float | None = None
    leverage: int | None = None
    order_amount: float | None = None


class TradingResetBody(BaseModel):
    confirm: bool = False                    # 必须为 True 才执行，防误触
    new_initial_balance: float | None = None  # 可选：顺便改初始金额


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
        short_scores = compute_short_scores(conn)
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
    raw_scores = compute_short_scores(conn)
    # 综合热度增强：加 composite_score, trend, prev_score 等
    scored = compute_composite_scores(conn, raw_scores, config.COMPOSITE_HISTORY_WINDOW)

    watchlist = set(storage.watchlist_get_all(conn))
    pool = []
    skipped_no_contract = 0
    for s in scored:
        snap_row = _load_snapshot(conn, s["token"])
        if not snap_row or not (snap_row.get("snapshot") or {}).get("mark_price"):
            skipped_no_contract += 1
            continue
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
            "updated_at": datetime.now().isoformat(timespec="seconds"),
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
        scores = compute_short_scores(conn)
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
        result = trade_logic.manual_close_on_unwatch(conn, token)
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
        short_scores = compute_short_scores(conn)
        social_map = {s["token"]: s["score"] for s in short_scores}

    if not tokens:
        return {"ok": True, "refreshed": 0, "skipped_no_contract": 0, "tokens": []}

    try:
        result = _refresh_watchlist_tokens(tokens)
    except Exception as e:
        raise HTTPException(503, f"刷新观察列表合约数据失败: {e}")
    return {"ok": True, **result}

    try:
        futures_set = get_futures_symbols()
    except Exception as e:
        raise HTTPException(503, f"获取合约列表失败: {e}")

    refreshed = 0
    skipped = 0
    for token in tokens:
        if token.upper() not in futures_set:
            skipped += 1
            continue
        try:
            snap = get_market_snapshot(token)
        except Exception:
            continue
        if not snap:
            continue
        social_score = social_map.get(token, 0.0)
        analysis = analyze_signals(snap, social_score)
        with storage.get_conn() as conn:
            storage.snapshot_upsert(
                conn, token,
                json.dumps(snap, default=str, ensure_ascii=False),
                json.dumps(analysis, default=str, ensure_ascii=False),
            )
        refreshed += 1
        time.sleep(0.3)  # 节流

    return {"ok": True, "refreshed": refreshed, "skipped_no_contract": skipped,
            "tokens": tokens}


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
            positions = storage.trade_positions_all(conn, limit=30)
            leaderboard_items, _ = _build_leaderboard_items(conn)
            candidates = trade_logic.build_trade_candidates_from_leaderboard(
                conn, leaderboard_items, passed_only=True)
            loss_archive = storage.trade_loss_archive_stats(conn)
        return {
            "account": account,
            "positions": positions,
            "candidates": candidates,
            "loss_archive": loss_archive,
        }
    return _cached("trading", 2.0, compute)


@app.post("/api/trading/settings")
def api_trading_settings(body: TradingSettingsBody):
    fields = {}
    for key in ("enabled", "mode", "initial_balance", "leverage", "order_amount"):
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


# === Agent 监控 API ===

@app.get("/api/agent/overview")
def api_agent_overview():
    """Agent 账户概览 + 今日统计"""
    def compute():
        with storage.get_conn() as conn:
            # 账户
            settings = storage.trading_settings_get(conn)
            initial = float(settings.get("initial_balance", 1000))
            realized = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl),0) FROM trade_positions"
            ).fetchone()[0]
            unrealized = conn.execute(
                "SELECT COALESCE(SUM(unrealized_pnl),0) FROM trade_positions "
                "WHERE status IN ('OPEN','PARTIAL')"
            ).fetchone()[0]
            locked = conn.execute(
                "SELECT COALESCE(SUM(margin_amount),0) FROM trade_positions "
                "WHERE status IN ('OPEN','PARTIAL')"
            ).fetchone()[0]
            equity = round(initial + realized + unrealized, 2)
            available = round(initial + realized - locked, 2)

            # 今日统计（从 trade_positions 读，覆盖收藏/auto/止损止盈所有来源）
            today_opens = conn.execute(
                "SELECT COUNT(*) FROM trade_positions "
                "WHERE date(created_at, '+8 hours')=date('now', '+8 hours')"
            ).fetchone()[0]
            today_closes = conn.execute(
                "SELECT COUNT(*) FROM trade_positions "
                "WHERE status='CLOSED' AND closed_at IS NOT NULL "
                "AND date(closed_at, '+8 hours')=date('now', '+8 hours')"
            ).fetchone()[0]
            today_wins = conn.execute(
                "SELECT COUNT(*) FROM trade_positions "
                "WHERE status='CLOSED' AND realized_pnl > 0 AND closed_at IS NOT NULL "
                "AND date(closed_at, '+8 hours')=date('now', '+8 hours')"
            ).fetchone()[0]
            today_losses = conn.execute(
                "SELECT COUNT(*) FROM trade_positions "
                "WHERE status='CLOSED' AND realized_pnl <= 0 AND closed_at IS NOT NULL "
                "AND date(closed_at, '+8 hours')=date('now', '+8 hours')"
            ).fetchone()[0]
            today_pnl = conn.execute(
                "SELECT COALESCE(SUM(realized_pnl),0) FROM trade_positions "
                "WHERE status='CLOSED' AND closed_at IS NOT NULL "
                "AND date(closed_at, '+8 hours')=date('now', '+8 hours')"
            ).fetchone()[0]

            # 持仓数
            open_count = conn.execute(
                "SELECT COUNT(*) FROM trade_positions "
                "WHERE status IN ('OPEN','PARTIAL')"
            ).fetchone()[0]

            # 待处理决策
            pending = conn.execute(
                "SELECT COUNT(*) FROM pending_decisions WHERE status='pending'"
            ).fetchone()[0]
            rejected_today = conn.execute(
                "SELECT COUNT(*) FROM pending_decisions "
                "WHERE status='rejected' AND date(created_at, '+8 hours')=date('now', '+8 hours')"
            ).fetchone()[0]

            # 总体胜率
            total_closed = conn.execute(
                "SELECT COUNT(*) FROM trade_positions WHERE status='CLOSED'"
            ).fetchone()[0]
            total_wins = conn.execute(
                "SELECT COUNT(*) FROM trade_positions "
                "WHERE status='CLOSED' AND realized_pnl > 0"
            ).fetchone()[0]

            # 最大回撤（1h 缓存）
            global _max_dd_cache
            now = time.time()
            if _max_dd_cache["time"] and now - _max_dd_cache["time"] < 3600:
                max_dd = _max_dd_cache["value"]
            else:
                running = float(initial or 0)
                peak = running
                max_dd = 0.0
                for r in conn.execute("SELECT COALESCE(realized_pnl,0) FROM trade_positions WHERE status='CLOSED' ORDER BY closed_at").fetchall():
                    running += float(r[0])
                    if running > peak: peak = running
                    if peak > 0:
                        dd = (running - peak) / peak * 100
                        if dd < max_dd: max_dd = dd
                dd = (running + unrealized - peak) / peak * 100 if peak > 0 else 0
                if dd < max_dd: max_dd = dd
                _max_dd_cache = {"value": round(max_dd, 1), "time": time.time()}

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
    return _cached("agent_overview", 15.0, compute)


@app.get("/api/agent/journal")
def api_agent_journal(limit: int = 5, offset: int = 0):
    """操作日志：仅 journal 表（已执行的交易），按时间倒排，分页"""
    with storage.get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
        rows = conn.execute(
            "SELECT token, action, price, tier, pnl_pct, close_reason, "
            "hold_duration, reason, created_at "
            "FROM journal ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
    return {
        "journal": [dict(r) for r in rows],
        "has_more": (offset + limit) < total,
    }


@app.get("/api/agent/timeline")
def api_agent_timeline(limit: int = 30, offset: int = 0):
    """Agent 决策时间线：journal + pending_decisions 合并，按时间倒排，分页"""
    with storage.get_conn() as conn:
        journal_total = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
        journal_rows = conn.execute(
            "SELECT id, token, action, price, tier, stop_loss, tp1_price, tp2_price, "
            "reason, dimension_data, market_overview, lesson_checked, "
            "pnl_pct, close_reason, hold_duration, created_at "
            "FROM journal ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()

        decision_total = conn.execute("SELECT COUNT(*) FROM pending_decisions").fetchone()[0]
        decision_rows = conn.execute(
            "SELECT id, action, token, tier, entry_price, stop_loss, "
            "tp1_price, tp2_price, close_reason, reason, market_overview AS market_read, "
            "status, reject_reason, consumed_at, created_at "
            "FROM pending_decisions ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()

    timeline = []
    for r in journal_rows:
        r = dict(r)
        r["source"] = "journal"
        timeline.append(r)
    for r in decision_rows:
        r = dict(r)
        # consumed 的决策已反映在 journal 里，跳过避免重复
        if r.get("status") == "consumed":
            continue
        r["source"] = "decision"
        timeline.append(r)

    timeline.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    has_more = (offset + limit) < max(journal_total, decision_total)
    return {"timeline": timeline[:limit], "has_more": has_more}


@app.get("/api/agent/memory/search")
def api_agent_memory_search(q: str = "", token: str = "", limit: int = 10):
    """记忆搜索：调 Mem0 搜历史类似场景"""
    if not q.strip():
        return {"results": []}
    try:
        import sync_memory
        results = sync_memory.search_similar(q.strip(), token=token.strip() or None, limit=limit)
        return {"results": results, "query": q.strip()}
    except Exception:
        return {"results": [], "query": q.strip()}


@app.get("/api/agent/lessons")
def api_agent_lessons(limit: int = 20, offset: int = 0):
    """Agent 教训库 + 统计，分页"""
    with storage.get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM lessons WHERE learned=0").fetchone()[0]
        active = conn.execute(
            "SELECT * FROM lessons WHERE learned=0 ORDER BY severity DESC, created_at DESC LIMIT ? OFFSET ?",
            (limit, offset)
        ).fetchall()
        active = [dict(r) for r in active]
        stats = storage.lessons_stats(conn)
    return {"active": active, "stats": stats, "has_more": (offset + limit) < total}


# === 前端页面 ===

HTML = """
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>Binance Square Monitor</title>
<style>
:root {
  --bg: #0f1419;
  --panel: #1a1f2e;
  --border: #2a3142;
  --text: #e6e8eb;
  --muted: #8b92a5;
  --accent: #f0b90b;
  --green: #52c41a;
  --red: #ff4d4f;
  --yellow: #faad14;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 20px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
  background: var(--bg); color: var(--text);
}
h1, h2 { margin: 0 0 12px; }
h1 { font-size: 22px; color: var(--accent); }
h2 { font-size: 16px; color: var(--accent); margin-top: 24px; }
.updated { color: var(--muted); font-size: 12px; margin-bottom: 16px; }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 16px; margin-bottom: 16px; }
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
  background: #3a1f1f; border: 1px solid #5a2f2f; color: #ffb4b4;
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
  background: #c0392b; color: #fff;
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
.worker-stage-badge.market   { background: #5f1e3a; color: #ff7eb3; }
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
  background: #5a1f1f; color: #ffb4b4;
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
.open-btn {
  background: var(--green); color: #000; border: none;
  padding: 2px 8px; border-radius: 3px; cursor: pointer;
  font-size: 11px; font-weight: 500; margin-left: 6px;
}
.open-btn:hover { opacity: 0.8; }
.open-btn:disabled { background: #2a3142; color: var(--muted); cursor: not-allowed; }
.close-btn {
  background: #c0392b; color: #fff; border: none;
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
</style>
</head>
<body>

<div class="progress-bar" id="progress-bar"><div class="fill" id="progress-fill" style="width:0%"></div></div>
<div class="toast" id="toast"></div>

<div style="display:flex;align-items:center;gap:16px;margin-bottom:4px;">
<h1 style="margin:0">🔥 币安广场热度监控</h1>
<a href="/agent" style="font-size:13px;color:var(--accent);text-decoration:none;padding:4px 12px;border:1px solid var(--border);border-radius:4px;">Agent 监控 →</a>
</div>
<div class="updated" id="updated">加载中...</div>

<div class="disclaimer">
⚠ 本页所有数据和标签仅为客观数据呈现，<strong>不是投资建议</strong>。综合分高 ≠ 一定上涨，
市场永远可能反向走，加密货币合约是高风险产品，请独立判断并谨慎决策。
</div>

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
  <h2 style="margin-top:0">自动交易面板</h2>
  <div class="muted" style="font-size:12px;margin-bottom:10px;">
    默认模拟交易。自动开仓规则：判断栏为看起来健康，15m 涨幅 0%-5%，1h 涨幅 0%-20%，OI 15m/1h/4h 都增加，主动买卖比 > 1.15，有可用价格后按市价开多。同一代币同一轮榜单只开一次。止损固定 -2%，止盈为 +1R 平 50%、+2R 平 30%、剩余跟踪。
  </div>
  <div class="trade-controls">
    <div>
      <label>自动交易</label>
      <select id="trade-enabled">
        <option value="false">关闭</option>
        <option value="true">开启</option>
      </select>
    </div>
    <div>
      <label>模式</label>
      <select id="trade-mode">
        <option value="paper">模拟</option>
        <option value="live">实盘（暂未启用）</option>
      </select>
    </div>
    <div>
      <label>账户初始金额 USDT</label>
      <input id="trade-initial" type="number" min="1" step="1">
    </div>
    <div>
      <label>交易倍数</label>
      <input id="trade-leverage" type="number" min="1" max="125" step="1">
    </div>
    <div style="display:flex;align-items:end;gap:8px;">
      <button class="refresh-btn" onclick="saveTradingSettings()">保存交易设置</button>
      <button class="refresh-btn danger-btn" onclick="resetTradingAccount()"
              title="清空所有持仓和历史记录，把账户恢复到初始金额">重置账户</button>
    </div>
  </div>
  <div class="trade-summary" id="trade-summary"></div>
  <div class="trade-position-grid">
    <div class="trade-window" style="overflow-x:auto">
      <h3>持仓代币</h3>
      <div id="trade-positions"><div class="empty">暂无持仓</div></div>
    </div>
    <div class="trade-window" style="overflow-x:auto">
      <h3>已平仓代币</h3>
      <div id="trade-closed-positions"><div class="empty">暂无已平仓记录</div></div>
    </div>
  </div>
  <h2>合约扫描与操作建议</h2>
  <div id="trade-candidates"><div class="empty">等待扫描数据...</div></div>
  <h2>止损失败归档</h2>
  <div id="trade-loss-archive"><div class="empty">暂无止损样本</div></div>
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
            更新于 ${m.updated_at || '-'}
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
  document.getElementById('updated').textContent = '最后刷新: ' + data.updated_at;
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
const fmtUsdGlobal = (v) => {
  if (v === null || v === undefined || isNaN(Number(v))) return '-';
  return '$' + Number(v).toFixed(2);
};

function renderTradingPanel(data) {
  const acc = data.account || {};
  const settings = acc.settings || {};
  const active = document.activeElement;
  const editingSettings = active && active.closest && active.closest('.trade-controls');
  if (!editingSettings) {
    document.getElementById('trade-enabled').value = settings.enabled ? 'true' : 'false';
    document.getElementById('trade-mode').value = settings.mode || 'paper';
    document.getElementById('trade-initial').value = settings.initial_balance ?? '';
    document.getElementById('trade-leverage').value = settings.leverage ?? '';
  }

  document.getElementById('trade-summary').innerHTML = `
    <div class="metric"><div class="label">初始金额</div><div class="value">${fmtUsdGlobal(acc.initial_balance)}</div></div>
    <div class="metric"><div class="label">账户权益</div><div class="value">${fmtUsdGlobal(acc.equity)}</div></div>
    <div class="metric"><div class="label">剩余金额</div><div class="value">${fmtUsdGlobal(acc.available_balance)}</div></div>
    <div class="metric"><div class="label">占用保证金</div><div class="value">${fmtUsdGlobal(acc.locked_margin)}</div></div>
    <div class="metric"><div class="label">已实现盈亏</div><div class="value">${fmtUsdGlobal(acc.realized_pnl)}</div></div>
    <div class="metric"><div class="label">浮动盈亏</div><div class="value">${fmtUsdGlobal(acc.unrealized_pnl)}</div></div>
  `;

  renderTradePositions(data.positions || []);
  renderTradeCandidates(data.candidates || []);
  renderTradeLossArchive(data.loss_archive || {});
}

function renderTradePositions(items) {
  const activeStatuses = new Set(['PENDING', 'OPEN', 'PARTIAL']);
  const activeItems = items.filter(p => activeStatuses.has(p.status));
  const closedItems = items.filter(p => !activeStatuses.has(p.status));
  renderOpenPositions(activeItems);
  renderClosedPositions(closedItems);
}

function renderOpenPositions(items) {
  const el = document.getElementById('trade-positions');
  if (!items.length) {
    el.innerHTML = '<div class="empty">暂无持仓</div>';
    return;
  }
  el.innerHTML = '<table class="compact-table"><thead><tr>' +
    '<th>代币</th><th>状态</th><th class="right">倍数</th><th class="right">金额</th>' +
    '<th class="right">入场</th><th class="right">现价</th><th class="right">止损</th>' +
    '<th class="right">盈亏</th><th>操作建议</th>' +
    '</tr></thead><tbody>' +
    items.map(p => {
      const pnl = Number(p.pnl_pct || 0);
      const pnlCls = pnl >= 0 ? 'green' : 'red';
      return `<tr data-token="${p.token}">
        <td class="token">${p.token}<button class="close-btn" onclick="closeTrade('${p.token}')">平仓</button></td>
        <td>${p.status}</td>
        <td class="right">${Number(p.leverage || 0).toFixed(0)}x</td>
        <td class="right">${fmtUsdGlobal(p.margin_amount)}</td>
        <td class="right">${fmtPrice(p.entry_price || p.limit_price)}</td>
        <td class="right">${fmtPrice(p.current_price)}</td>
        <td class="right">${fmtPrice(p.stop_loss_price)}</td>
        <td class="right ${pnlCls}">${pnl.toFixed(2)}%</td>
        <td><span class="cell-advice" title="${escHtml(p.advice || '-')}">${escHtml(p.advice || '-')}</span></td>
      </tr>`;
    }).join('') + '</tbody></table>';
}

function renderClosedPositions(items) {
  const el = document.getElementById('trade-closed-positions');
  if (!items.length) {
    el.innerHTML = '<div class="empty">暂无已平仓记录</div>';
    return;
  }
  const closed = items.filter(p => p.status === 'CLOSED');
  const totalPnl = closed.reduce((sum, p) => sum + Number(p.realized_pnl || 0), 0);
  const wins = closed.filter(p => Number(p.realized_pnl || 0) > 0).length;
  const losses = closed.filter(p => Number(p.realized_pnl || 0) < 0).length;
  const winRate = closed.length ? (wins / closed.length * 100) : 0;
  const totalCls = totalPnl >= 0 ? 'green' : 'red';

  el.innerHTML = `
    <div class="closed-summary">
      <div class="metric"><div class="label">已平仓</div><div class="value">${closed.length}</div></div>
      <div class="metric"><div class="label">总盈亏</div><div class="value ${totalCls}">${fmtUsdGlobal(totalPnl)}</div></div>
      <div class="metric"><div class="label">胜率</div><div class="value">${winRate.toFixed(1)}%</div></div>
    </div>
    <div class="closed-positions-scroll">
      <table class="compact-table"><thead><tr>
        <th>代币</th><th>状态</th><th class="right">入场</th><th class="right">平仓价</th>
        <th class="right">盈亏</th><th class="right">盈亏率</th><th>操作建议</th>
      </tr></thead><tbody>
        ${items.map(p => {
          const realized = Number(p.realized_pnl || 0);
          let pnl = Number(p.pnl_pct || 0);
          if (!pnl && realized && Number(p.margin_amount || 0)) {
            pnl = realized / Number(p.margin_amount) * 100;
          }
          const pnlCls = realized >= 0 ? 'green' : 'red';
          const qty = Number(p.quantity || 0);
          const entry = Number(p.entry_price || p.limit_price || 0);
          const avgExit = qty && entry ? entry + realized / qty : Number(p.current_price || 0);
          return `<tr data-token="${p.token}">
            <td class="token">${p.token}</td>
            <td>${p.status}</td>
            <td class="right">${fmtPrice(entry || p.limit_price)}</td>
            <td class="right">${fmtPrice(avgExit)}</td>
            <td class="right ${pnlCls}">${fmtUsdGlobal(realized)}</td>
            <td class="right ${pnlCls}">${pnl.toFixed(2)}%</td>
            <td><span class="cell-advice" title="${escHtml(p.advice || '-')}">${escHtml(p.advice || '-')}</span></td>
          </tr>`;
        }).join('')}
      </tbody></table>
    </div>
  `;
}

function renderTradeCandidates(items) {
  const el = document.getElementById('trade-candidates');
  if (!items.length) {
    el.innerHTML = '<div class="empty">暂无符合自动开仓要求的代币</div>';
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

function renderTradeLossArchive(archive) {
  const el = document.getElementById('trade-loss-archive');
  if (!archive.count) {
    el.innerHTML = '<div class="empty">暂无止损样本</div>';
    return;
  }
  const tags = Object.entries(archive.tag_counts || {})
    .sort((a, b) => b[1] - a[1])
    .map(([k, v]) => `<span class="tag-chip">${k}: ${v}</span>`)
    .join('');
  const recent = (archive.recent || []).slice(0, 5).map(r => {
    const pnl = Number(r.pnl_pct || 0);
    return `<tr>
      <td class="token">${r.token}</td>
      <td>${r.failed_reason || '-'}</td>
      <td class="right red">${pnl.toFixed(2)}%</td>
      <td>${r.reason_tags || '[]'}</td>
    </tr>`;
  }).join('');
  el.innerHTML = `
    <div class="deep-tags">${tags}</div>
    <table><thead><tr><th>代币</th><th>失败原因</th><th class="right">亏损</th><th>标签</th></tr></thead>
    <tbody>${recent}</tbody></table>
  `;
}

async function loadTradingPanel() {
  try {
    const resp = await fetch('/api/trading');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    renderTradingPanel(await resp.json());
  } catch (e) {
    document.getElementById('trade-summary').innerHTML =
      `<div class="empty">交易面板加载失败：${e.message}</div>`;
  }
}

async function saveTradingSettings() {
  const body = {
    enabled: document.getElementById('trade-enabled').value === 'true',
    mode: document.getElementById('trade-mode').value,
    initial_balance: Number(document.getElementById('trade-initial').value),
    leverage: Number(document.getElementById('trade-leverage').value),
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
  };
  stageEl.textContent = stageLabels[stage] || stage;

  detailEl.textContent = s.detail || '—';

  // 进度条：抓取阶段按 round_start 算，其他阶段满格
  let pct = 100;
  if (stage === 'scraping' && s.round_start && s.round_duration_seconds) {
    try {
      const startMs = new Date(s.round_start).getTime();
      const elapsed = (Date.now() - startMs) / 1000;
      pct = Math.min(100, (elapsed / s.round_duration_seconds) * 100);
    } catch (e) { pct = 50; }
  } else if (stage === 'idle') {
    pct = 100;
  } else if (stage === 'saving') {
    pct = 100;
  }
  fillEl.style.width = pct.toFixed(0) + '%';

  // 轮次
  if (s.round_number) {
    roundEl.textContent = `第 ${s.round_number} 轮`
      + (s.heartbeat_age_seconds !== undefined ? ` · ${s.heartbeat_age_seconds}s 前更新` : '');
  } else {
    roundEl.textContent = '—';
  }

  // 统计
  const stats = [];
  if (s.posts_this_round !== undefined)
    stats.push(`<span>本轮抓到 <strong>${s.posts_this_round}</strong> 条</span>`);
  if (s.saved_this_round !== undefined)
    stats.push(`<span>入库 <strong>${s.saved_this_round}</strong> 条</span>`);
  if (s.total_posts !== undefined)
    stats.push(`<span>累计帖子 <strong>${s.total_posts}</strong></span>`);
  if (s.total_authors !== undefined)
    stats.push(`<span>累计作者 <strong>${s.total_authors}</strong></span>`);
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
setInterval(pollWatchlistRealtime, 1000);
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
<title>Agent 监控面板</title>
<style>
:root {
  --bg: #0f1419; --panel: #1a1f2e; --border: #2a3142;
  --text: #e6e8eb; --muted: #8b92a5; --accent: #f0b90b;
  --green: #52c41a; --red: #ff4d4f; --yellow: #faad14; --blue: #1890ff;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 16px;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
  background: var(--bg); color: var(--text); font-size: 13px;
}
h1 { font-size: 20px; color: var(--accent); margin: 0 0 4px; }
h2 { font-size: 14px; color: var(--accent); margin: 0 0 10px; }
.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }
.header-time { color: var(--muted); font-size: 12px; }
.nav-link { color: var(--accent); text-decoration: none; font-size: 12px; }
.nav-link:hover { text-decoration: underline; }

/* 布局 */
.grid-top { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 12px; }
.grid-bottom { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
@media (max-width: 900px) {
  .grid-top, .grid-bottom { grid-template-columns: 1fr; }
}

/* 面板 */
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 14px; }
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

<div class="header">
  <div>
    <h1>Agent 监控面板</h1>
    <span class="header-time" id="clock"></span>
  </div>
  <a href="/" class="nav-link">市场监控 →</a>
</div>

<!-- 记忆搜索 -->
<div class="panel">
  <div style="display:flex;align-items:center;gap:8px">
    <span style="font-size:13px;font-weight:500;white-space:nowrap">🔍 记忆搜索</span>
    <input id="mem-query" type="text" placeholder="OI涨 taker弱 sl_hit" style="flex:1;background:#0a0e15;color:var(--text);border:1px solid var(--border);border-radius:4px;padding:6px 10px;font-size:13px" onkeydown="if(event.key==='Enter')searchMemory()">
    <input id="mem-token" type="text" placeholder="限定币种(可选)" style="width:120px;background:#0a0e15;color:var(--text);border:1px solid var(--border);border-radius:4px;padding:6px 10px;font-size:13px">
    <button onclick="searchMemory()" style="background:var(--accent);color:#000;border:none;padding:6px 14px;border-radius:4px;cursor:pointer;font-size:13px;font-weight:500">搜索</button>
    <button onclick="closeMemorySearch()" style="background:transparent;color:var(--muted);border:none;cursor:pointer;font-size:18px" title="关闭">✕</button>
  </div>
  <div id="mem-results" style="display:none;margin-top:10px"></div>
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

  <div class="panel" style="overflow:auto;max-height:380px">
    <h2>当前持仓</h2>
    <table>
      <thead><tr>
        <th>币种</th><th>方向</th><th>入场</th><th>现价</th><th>PnL%</th><th>止损</th><th>止盈</th><th>持仓</th>
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
async function loadOverview() {
  try {
    const r = await fetch('/api/agent/overview');
    const d = await r.json();
    $('#equity-avail').innerHTML = '$' + d.equity + '<br><span style="font-size:13px;color:var(--muted)">$' + d.available + '</span>';
    $('#equity-avail').className = 'stat-value ' + (d.unrealized >= 0 ? 'green' : 'red');
    $('#today-pnl').textContent = fmtPct(d.today_pnl);
    $('#today-pnl').className = 'stat-value ' + pnlClass(d.today_pnl);
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
    const positions = (d.positions || []).filter(p => p.status === 'OPEN' || p.status === 'PARTIAL');
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
      let hold = '—';
      if (p.created_at) {
        try { const d = new Date(p.created_at.replace(' ','T') + 'Z'); const h = Math.floor((Date.now()-d)/3600000); const m = Math.floor((Date.now()-d)/60000)%60; hold = (h ? h+'h' : '') + m + 'm'; } catch(e) {}
      }
      return '<tr>' +
        '<td style="font-weight:bold;color:var(--accent)">' + esc(p.token) + '</td>' +
        '<td>' + esc(p.side) + '</td>' +
        '<td>' + fmtPrice(p.entry_price) + '</td>' +
        '<td>' + fmtPrice(p.current_price) + '</td>' +
        '<td class="' + pnlClass(pnl) + '">' + fmtPct(pnl) + '</td>' +
        '<td>' + fmtPrice(p.stop_loss_price) + '</td>' +
        '<td>' + fmtPrice(p.tp1_price) + '</td>' +
        '<td style="font-size:11px;white-space:nowrap">' + esc(hold) + '</td>' +
        '</tr>';
    }).join('');
  } catch(e) { console.error('positions', e); }
}

// 教训库
let _lsOffset = 0;
let _lsHasMore = true;
const _LS_PAGE = 5;

async function loadLessons(reset = false) {
  if (reset) { _lsOffset = 0; _lsHasMore = true; }
  if (!_lsHasMore) return;
  try {
    const r = await fetch('/api/agent/lessons?limit=' + _LS_PAGE + '&offset=' + _lsOffset);
    const d = await r.json();
    const s = d.stats || {};
    _lsOffset += (d.active || []).length;
    _lsHasMore = d.has_more;

    if (reset) {
      $('#lesson-stats').innerHTML =
        '<div class="item"><span class="label">活跃</span> <strong>' + (s.active || 0) + '</strong></div>' +
        '<div class="item"><span class="label">critical</span> <strong class="red">' + ((s.severity_dist || {}).critical || 0) + '</strong></div>' +
        '<div class="item"><span class="label">warning</span> <strong class="yellow">' + ((s.severity_dist || {}).warning || 0) + '</strong></div>' +
        '<div class="item"><span class="label">medium</span> <strong class="blue">' + ((s.severity_dist || {}).medium || 0) + '</strong></div>';
    }

    const lessons = d.active || [];
    const list = $('#lessons-list');
    if (reset && !lessons.length) {
      list.innerHTML = '<div class="empty">暂无教训</div>';
      return;
    }

    let html = '';
    lessons.forEach(l => {
      const sev = l.severity || 'medium';
      html += '<div class="lesson-item ' + sev + '">' +
        '<div class="lesson-head">' +
          '<span class="lesson-token">' + esc(l.token) + '</span>' +
          '<span class="lesson-sev ' + sev + '">' + sev + '</span>' +
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
          let fields = [];
          if (dd.market && dd.market.snapshot && typeof dd.market.snapshot === 'object') {
            const snap = dd.market.snapshot;
            fields = [
              ['价格', dd.price], ['15m涨跌', snap.change_15m_pct], ['1h涨跌', snap.change_1h_pct],
              ['4h涨跌', snap.change_4h_pct], ['OI 1h', snap.oi_change_1h_pct], ['OI 4h', snap.oi_change_4h_pct],
              ['taker', snap.taker_buy_sell_ratio], ['资金费率', snap.funding_rate_pct],
              ['散户多空', snap.long_short_ratio], ['大户多空', snap.top_trader_ls_ratio],
            ];
          } else {
            fields = Object.entries(dd).map(([k,v]) => [k, typeof v === 'object' ? JSON.stringify(v) : v]);
          }
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

async function searchMemory() {
  const q = $('#mem-query').value.trim();
  if (!q) return;
  const token = $('#mem-token').value.trim();
  const el = $('#mem-results');
  el.style.display = 'block';
  el.innerHTML = '<div class="muted" style="padding:10px">搜索中...</div>';
  try {
    const url = '/api/agent/memory/search?q=' + encodeURIComponent(q) + (token ? '&token=' + encodeURIComponent(token) : '');
    const r = await fetch(url);
    const d = await r.json();
    const items = d.results || [];
    if (!items.length) {
      el.innerHTML = '<div class="muted" style="padding:10px">无结果</div>';
      return;
    }
    el.innerHTML = items.map((m, i) => {
      const meta = m.metadata || {};
      const pnl = meta.pnl != null ? (meta.pnl >= 0 ? '<span class="green">+' + meta.pnl.toFixed(2) + '%</span>' : '<span class="red">' + meta.pnl.toFixed(2) + '%</span>') : '—';
      const result = meta.result === 'win' ? '🟢' : meta.result === 'loss' ? '🔴' : '';
      return '<div style="padding:8px 10px;margin-bottom:6px;background:#151a26;border-radius:4px;border-left:3px solid var(--accent)">' +
        '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">' +
          '<span><span style="color:var(--accent);font-weight:500">[' + (i+1) + ']</span> ' +
          '<span style="font-size:12px;color:var(--muted)">score=' + (m.score || 0).toFixed(3) + '</span> ' +
          '<b>' + esc(meta.token || '?') + '</b> ' + result + '</span>' +
          '<span style="font-size:12px">PnL: ' + pnl + '</span>' +
        '</div>' +
        '<div style="font-size:12px;color:var(--text);line-height:1.5">' + esc(m.memory || '') + '</div>' +
      '</div>';
    }).join('');
  } catch(e) {
    el.innerHTML = '<div class="muted" style="padding:10px">搜索失败: ' + esc(e.message) + '</div>';
  }
}

function closeMemorySearch() {
  $('#mem-results').style.display = 'none';
  $('#mem-results').innerHTML = '';
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
  await Promise.all([loadOverview(), loadPositions(), loadLessons(true), loadTimeline(true), loadJournal(true)]);
}

updateClock();
setInterval(updateClock, 1000);
refreshAll();
setInterval(loadOverview, 15000);
setInterval(loadPositions, 5000);
</script>
</body>
</html>
"""


@app.get("/agent", response_class=HTMLResponse)
def agent_monitor():
    return HTMLResponse(
        content=AGENT_HTML,
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
