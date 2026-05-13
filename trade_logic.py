"""Trading signal and paper-position helpers.

Default behavior is paper trading. Live order placement is intentionally not
implemented here; it should be added behind an explicit live switch later.

架构说明：
- 风控决策（仓位 sizing / 熔断 / 冷却 / 集中度）统一由 risk.py 提供
- 本模块只负责：组装数据 -> 调 risk 做决策 -> 落库
- 这样实盘接入时，风控逻辑可以 100% 复用，只需替换 open_paper_position
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import config
import storage
import risk
from analyzer import compute_short_scores, compute_composite_scores
from market import get_mark_price, get_klines_1h, get_market_snapshot
from signals import analyze as analyze_signals


MAX_ENTRY_CHANGE_15M = 5.0
MAX_ENTRY_CHANGE_1H = 20.0
MIN_ENTRY_TAKER_RATIO = 1.15
ARCHIVE_FUNDING_HOT_PCT = 0.05
ARCHIVE_LONG_SHORT_HOT = 2.0
ARCHIVE_TAKER_WEAK = 1.15
REALTIME_PRICE_MAX_AGE_SECONDS = 5
VERDICT_ORDER = {
    "✅ 看起来健康": 0,
    "🎯 值得留意": 1,
    "⚠️ 过热预警": 2,
    "📉 信号偏弱": 3,
    "⚪ 中性": 4,
    "数据不足": 5,
}


def _loads(raw, default=None):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _load_market(conn, token: str) -> dict:
    row = storage.snapshot_get(conn, token)
    if not row:
        return {"snapshot": {}, "analysis": {}, "updated_at": None}
    return {
        "snapshot": _loads(row.get("snapshot"), {}),
        "analysis": _loads(row.get("analysis"), {}),
        "updated_at": row.get("updated_at"),
    }


def _load_realtime(conn, token: str) -> dict:
    row = storage.realtime_get(conn, token)
    if not row:
        return {}
    data = _loads(row.get("snapshot"), {})
    data["cache_updated_at"] = row.get("updated_at")
    return data


def _current_price(market: dict, realtime: dict) -> float | None:
    snap = market.get("snapshot") or {}
    for key in ("last_trade_price", "mark_price", "best_ask", "best_bid"):
        val = realtime.get(key)
        if val:
            return float(val)
    val = snap.get("mark_price")
    return float(val) if val else None


def _timestamp_age_seconds(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return None



def _calc_hold(created_at: str | None) -> str | None:
    if not created_at:
        return None
    try:
        dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        hours, rem = divmod(int(delta.total_seconds()), 3600)
        mins = rem // 60
        return f"{hours}h{mins}m" if hours else f"{mins}m"
    except Exception:
        return None


# 关仓快照中文字段映射：snapshot 原始 key → 中文 key，与开仓 dimension_data 一致
_CLOSE_SNAP_MAP = [
    ("mark_price",           "标记价"),
    ("change_15m_pct",       "15分钟涨跌"),
    ("change_1h_pct",        "1小时涨跌"),
    ("change_4h_pct",        "4小时涨跌"),
    ("change_24h_pct",       "24小时涨跌"),
    ("change_48h_pct",       "48小时涨跌"),
    ("funding_rate_pct",     "资金费率(%/8h)"),
    ("oi_usd",               "未平仓(USD)"),
    ("oi_change_15m_pct",    "OI15分钟变化"),
    ("oi_change_1h_pct",     "OI1小时变化"),
    ("oi_change_4h_pct",     "OI4小时变化"),
    ("oi_change_48h_pct",    "OI48小时变化"),
    ("taker_buy_sell_ratio", "主动买卖比"),
    ("taker_buy_pct",        "主动买入占比"),
    ("taker_trend_pct",      "Taker趋势"),
    ("bid_ask_spread_pct",   "盘口价差"),
    ("depth_bid_1pct_usd",   "买盘深度(USD)"),
    ("depth_ask_1pct_usd",   "卖盘深度(USD)"),
    ("depth_imbalance_pct",  "盘口失衡度"),
    ("volume_24h_usd",       "24小时成交额"),
    ("long_short_ratio",     "散户多空比"),
    ("top_trader_ls_ratio",  "大户多空比"),
]


def _build_close_snap(close_price: float, snap: dict, analysis: dict) -> str:
    """构建与开仓同格式的中文平铺关仓快照 JSON。"""
    result = {"平仓价": close_price}
    for en_key, cn_key in _CLOSE_SNAP_MAP:
        result[cn_key] = snap.get(en_key)
    result["信号判定"] = analysis.get("verdict")
    result["走向"] = analysis.get("direction")
    result["信号标签"] = analysis.get("tags", [])
    result["分析备注"] = analysis.get("notes", [])
    result["OI背离"] = analysis.get("oi_divergence")
    return json.dumps(result, default=str, ensure_ascii=False)


def _position_price(token: str, market: dict, realtime: dict) -> float | None:
    age = _timestamp_age_seconds(realtime.get("cache_updated_at"))
    if age is not None and age <= REALTIME_PRICE_MAX_AGE_SECONDS:
        price = _current_price(market, realtime)
        if price:
            return price

    fresh_price = get_mark_price(token)
    if fresh_price:
        return fresh_price
    return _current_price(market, realtime)


def _entry_limit_price(realtime: dict, fallback_price: float) -> float:
    bid = realtime.get("best_bid")
    ask = realtime.get("best_ask")
    if bid and ask:
        return (float(bid) + float(ask)) / 2
    return fallback_price


def _pct(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_pct(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2f}%"


def _fmt_num(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _margin_pnl_pct(realized: float, unrealized: float, margin: float) -> float:
    return ((realized + unrealized) / (margin or 1)) * 100


def evaluate_candidate(score_row: dict, rank: int, market: dict, realtime: dict) -> dict:
    """
    评估一个候选币是否可以开仓。

    新架构（v2）：调用 risk.evaluate_entry_quality 得到 tier（full/half/skip），
    而不是所有条件硬 AND。这样轻度不满足的信号可以半仓进场，同时保留追高硬否决。

    返回字典新增字段：
      tier:         "full" / "half" / "skip"
      pass_count:   7 项核心条件通过数
      hard_block:   硬否决原因列表（非空则必 skip）
    原有 passed 字段继续保留，语义 = tier != "skip"，用于兼容旧代码。
    """
    token = score_row["token"]
    snap = market.get("snapshot") or {}
    analysis = market.get("analysis") or {}
    verdict = analysis.get("verdict") or ""
    signal_score = analysis.get("score")

    quality = risk.evaluate_entry_quality(snap, realtime, signal_score, verdict)

    tier = quality["tier"]
    passed = tier != "skip"

    # 把 reasons 组装成旧格式，方便 UI 继续展示
    reasons = []
    for r in quality["reasons_pass"]:
        reasons.append("OK " + r)
    for r in quality["reasons_fail"]:
        reasons.append("NO " + r)
    for r in quality["hard_block"]:
        reasons.insert(0, "⛔ " + r)

    price = _current_price(market, realtime)
    if price is None or price <= 0:
        passed = False
        tier = "skip"
        reasons.append("NO 缺少可用价格")

    suggestion = {
        "full": "可开多（满仓）",
        "half": "可开多（半仓）",
        "skip": "观察",
    }[tier]
    if quality["hard_block"]:
        suggestion = "不追高"

    return {
        "token": token,
        "rank": rank,
        "passed": passed,
        "tier": tier,
        "pass_count": quality["pass_count"],
        "hard_block": quality["hard_block"],
        "suggestion": suggestion,
        "reasons": reasons,
        "price": price,
        "limit_price": _entry_limit_price(realtime, price) if price else None,
        "market": market,
        "realtime": realtime,
        "analysis_score": signal_score,
    }


def build_trade_candidates_from_leaderboard(
        conn, leaderboard_items: list[dict], limit: int | None = None,
        passed_only: bool = False) -> list[dict]:
    candidates = []
    items = leaderboard_items[:limit] if limit else leaderboard_items
    signal_key = storage.leaderboard_signal_key(conn)
    for rank, item in enumerate(items, 1):
        token = item["token"]
        market = item.get("market") or _load_market(conn, token)
        if not (market.get("snapshot") or {}).get("mark_price"):
            continue
        realtime = _load_realtime(conn, token)
        score_row = item.get("score_row") or item
        result = evaluate_candidate(score_row, rank, market, realtime)
        result["score"] = score_row
        result["signal_key"] = signal_key
        result["has_active_position"] = storage.trade_has_active(conn, token)
        if passed_only and not result.get("passed"):
            continue
        candidates.append(result)
    return candidates


def build_trade_candidates(conn, limit: int = 20, passed_only: bool = False) -> list[dict]:
    raw_scores = compute_short_scores(conn)
    scores = compute_composite_scores(conn, raw_scores, config.COMPOSITE_HISTORY_WINDOW)
    signal_key = storage.leaderboard_signal_key(conn)
    sortable = []
    for score_row in scores:
        market = _load_market(conn, score_row["token"])
        if not (market.get("snapshot") or {}).get("mark_price"):
            continue
        verdict = (market.get("analysis") or {}).get("verdict", "")
        sortable.append((score_row, market, VERDICT_ORDER.get(verdict, 99)))
    sortable.sort(key=lambda item: (
        item[2],
        -(item[0].get("composite_score") or 0),
        -(item[0].get("score") or 0),
    ))
    candidates = []
    for rank, (score_row, market, _) in enumerate(sortable[:limit], 1):
        realtime = _load_realtime(conn, score_row["token"])
        result = evaluate_candidate(score_row, rank, market, realtime)
        result["score"] = score_row
        result["signal_key"] = signal_key
        result["has_active_position"] = storage.trade_has_active(conn, score_row["token"])
        if passed_only and not result.get("passed"):
            continue
        candidates.append(result)
    return candidates


def account_summary(conn) -> dict:
    settings = storage.trading_settings_get(conn)
    positions = storage.trade_positions_all(conn, limit=500)
    initial = float(settings.get("initial_balance") or 0)
    realized = sum(float(p.get("realized_pnl") or 0) for p in positions)
    unrealized = sum(float(p.get("unrealized_pnl") or 0)
                     for p in positions if p.get("status") in {"OPEN", "PARTIAL"})
    locked = sum(float(p.get("margin_amount") or 0)
                 for p in positions if p.get("status") in {"PENDING", "OPEN", "PARTIAL"})
    equity = initial + realized + unrealized
    available = initial + realized - locked
    return {
        "settings": settings,
        "initial_balance": round(initial, 4),
        "equity": round(equity, 4),
        "available_balance": round(available, 4),
        "locked_margin": round(locked, 4),
        "realized_pnl": round(realized, 4),
        "unrealized_pnl": round(unrealized, 4),
    }


def _build_account_context(conn) -> risk.AccountContext:
    """组装风控决策需要的账户上下文。只读，不修改数据库。"""
    summary = account_summary(conn)
    open_positions = storage.trade_open_positions(conn)

    # 按板块聚合
    by_sector = {}
    for pos in open_positions:
        sec = risk.sector_of(pos["token"])
        by_sector[sec] = by_sector.get(sec, 0) + 1

    return risk.AccountContext(
        equity=summary["equity"],
        available_balance=summary["available_balance"],
        realized_pnl_today=storage.trade_realized_pnl_today(conn),
        unrealized_pnl=summary["unrealized_pnl"],
        open_positions_count=len(open_positions),
        open_positions_by_sector=by_sector,
        trades_opened_today=storage.trade_count_today_opened(conn),
        last_stop_loss_by_token=storage.trade_last_stop_loss_map(
            conn, hours=max(2, config.TRADING_COOLDOWN_MINUTES_AFTER_LOSS // 30 + 1)),
    )


def _debug_reject(token: str, reason: str, candidate: dict = None):
    """TRADING_DEBUG 为 True 时打印开仓拒绝原因到 stderr，便于诊断"""
    if not getattr(config, "TRADING_DEBUG", False):
        return
    import sys
    extra = ""
    if candidate is not None:
        tier = candidate.get("tier", "?")
        score = candidate.get("analysis_score", "?")
        extra = f" | tier={tier} signal_score={score}"
    print(f"[trade-debug] REJECT {token}: {reason}{extra}", file=sys.stderr, flush=True)


def open_paper_position(conn, candidate: dict, settings: dict) -> bool | dict:
    """
    v2：接入风控中枢的开仓。

    决策流程：
      1. 基础去重（是否已有持仓 / signal lock）
      2. 账户级风控（日亏损熔断 / 持仓上限 / 冷却期 / 板块集中度）
      3. 计算 ATR 自适应止损
      4. 按 tier（full/half）和风险反推仓位
      5. 落库

    返回：True 成功 / False 失败。
    失败原因在 TRADING_DEBUG=True 时会打印到 stderr。
    """
    token = (candidate.get("token") or "").upper() or "?"

    if not candidate.get("passed"):
        _debug_reject(token, "candidate.passed=False（信号评估不通过）", candidate)
        return False
    if candidate.get("has_active_position"):
        _debug_reject(token, "已有活跃持仓", candidate)
        return False

    tier = candidate.get("tier", "full")
    if tier == "skip":
        _debug_reject(token, "tier=skip", candidate)
        return False

    if storage.trade_has_active(conn, token):
        _debug_reject(token, "DB 中已有活跃仓位（并发保护）", candidate)
        return False

    # 账户级风控
    account = _build_account_context(conn)
    risk_decision = risk.check_account_risk(account, token)
    if not risk_decision.allowed:
        _debug_reject(token, f"账户风控: {risk_decision.reason}", candidate)
        return False

    # 获取当前价（带滑点模拟）
    raw_price = candidate.get("price")
    if not raw_price or raw_price <= 0:
        _debug_reject(token, f"价格无效 ({raw_price})", candidate)
        return False
    entry_price = raw_price * (1 + config.TRADING_ASSUMED_SLIPPAGE_PCT / 100)

    # 计算 ATR 止损
    klines = get_klines_1h(token, limit=max(30, config.TRADING_ATR_PERIOD + 2))
    stop_pct, stop_mode = risk.compute_stop_distance_pct(klines)
    stop_loss_price = entry_price * (1 + stop_pct / 100)

    # 抢 signal lock
    signal_key = candidate.get("signal_key") or storage.leaderboard_signal_key(conn)
    if not storage.trade_signal_lock_acquire(conn, token, signal_key):
        _debug_reject(token, f"signal_lock 已占用 (signal_key={signal_key})", candidate)
        return False

    # 计算仓位
    leverage = float(settings.get("leverage") or config.TRADING_LEVERAGE)
    sizing = risk.compute_position_size(account, entry_price, stop_loss_price, leverage, tier)
    if sizing.get("quantity", 0) <= 0:
        _debug_reject(token, f"仓位计算: {sizing.get('note')}", candidate)
        return False

    quantity = sizing["quantity"]
    margin = sizing["margin"]
    notional = sizing["notional"]
    risk_amount = sizing["risk_amount"]

    # 止盈：基于 R 值
    risk_per_unit = entry_price - stop_loss_price
    tp1_price = entry_price + risk_per_unit * config.TRADING_TP1_R
    tp2_price = entry_price + risk_per_unit * config.TRADING_TP2_R

    snapshot = {
        **candidate,
        "_risk_meta": {
            "tier": tier,
            "stop_mode": stop_mode,
            "stop_distance_pct": sizing.get("stop_distance_pct"),
            "risk_amount": risk_amount,
            "risk_pct_of_equity": (risk_amount / account.equity * 100) if account.equity else None,
            "sector": risk.sector_of(token),
            "account_equity_at_open": account.equity,
            "assumed_slippage_pct": config.TRADING_ASSUMED_SLIPPAGE_PCT,
        },
    }

    position = {
        "token": token,
        "symbol": f"{token}USDT",
        "side": "LONG",
        "status": "OPEN",
        "mode": settings.get("mode") or "paper",
        "margin_amount": margin,
        "leverage": leverage,
        "notional": notional,
        "quantity": quantity,
        "entry_price": entry_price,
        "limit_price": entry_price,
        "current_price": entry_price,
        "stop_loss_price": stop_loss_price,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "highest_price": entry_price,
        "trailing_stop_price": None,
        "signal_snapshot": json.dumps(snapshot, default=str, ensure_ascii=False),
        "open_reason": (
            f"自动开仓 tier={tier} | "
            f"信号分={candidate.get('analysis_score')} | "
            f"通过 {candidate.get('pass_count')}/7 | "
            f"止损 {stop_mode} {sizing.get('stop_distance_pct', 0):.2f}% | "
            f"风险 ${risk_amount:.2f} ({(risk_amount/account.equity*100) if account.equity else 0:.2f}% equity)"
        ),
        "advice": (
            f"{'满仓' if tier == 'full' else '半仓'}持有：等待 +{config.TRADING_TP1_R}R 止盈 / "
            f"{sizing.get('stop_distance_pct', 0):.2f}% 止损"
        ),
    }
    ok = storage.trade_position_insert(conn, position)
    if not ok:
        _debug_reject(token, "DB insert 失败（唯一索引冲突？）", candidate)
    return ok


def manual_open_on_watch(conn, token: str, settings: dict) -> dict:
    """
    收藏时按设置金额和倍数模拟市价开多。

    v2 改动：
    - 仓位用风险反推（和自动交易一致）
    - 止损用 ATR 自适应
    - 收藏是用户强意愿，豁免部分账户级风控（持仓上限/板块集中度），
      但保留最关键的熔断和止损冷却（通过 config 开关控制）
    - 返回详细 reason，前端 toast 能直接展示
    """
    token = token.upper()
    if storage.trade_has_active(conn, token):
        return {"ok": False, "reason": f"{token} 已有持仓或挂单"}

    mode = settings.get("mode") or "paper"
    if mode != "paper":
        return {"ok": False, "reason": f"当前模式 {mode}，实盘下单未启用"}

    market = _load_market(conn, token)
    realtime = _load_realtime(conn, token)
    raw_price = _current_price(market, realtime)

    # 如果本地缓存没有价格，尝试实时拉一次
    if not raw_price or raw_price <= 0:
        raw_price = get_mark_price(token)
    if not raw_price or raw_price <= 0:
        return {"ok": False, "reason": f"{token} 缺少可用市价（可能没有永续合约或接口超时）"}

    # 账户级风控（收藏豁免部分）
    account = _build_account_context(conn)
    risk_decision = risk.check_account_risk(
        account, token,
        bypass_max_concurrent=config.MANUAL_BYPASS_MAX_CONCURRENT,
        bypass_sector_limit=config.MANUAL_BYPASS_SECTOR_LIMIT,
        bypass_cooldown=config.MANUAL_BYPASS_COOLDOWN,
    )
    if not risk_decision.allowed:
        return {"ok": False, "reason": f"风控拦截：{risk_decision.reason}"}

    entry_price = raw_price * (1 + config.TRADING_ASSUMED_SLIPPAGE_PCT / 100)
    klines = get_klines_1h(token, limit=max(30, config.TRADING_ATR_PERIOD + 2))
    stop_pct, stop_mode = risk.compute_stop_distance_pct(klines)
    stop_loss_price = entry_price * (1 + stop_pct / 100)

    leverage = float(settings.get("leverage") or config.TRADING_LEVERAGE)
    # 手动开仓默认满仓档
    sizing = risk.compute_position_size(account, entry_price, stop_loss_price, leverage, "full")
    if sizing.get("quantity", 0) <= 0:
        note = sizing.get("note", "未知")
        # 常见情况友好提示
        if "余额" in note:
            return {"ok": False, "reason": (
                f"可用余额不足：{note}。"
                f" 当前 equity=${account.equity:.2f}，已锁定保证金=${account.equity - account.available_balance:.2f}"
            )}
        if "名义" in note:
            return {"ok": False, "reason": (
                f"按风险反推的仓位太小：{note}。"
                f" 可尝试：1) 增大账户余额 2) 把 TRADING_SIZING_MODE 改成 'fixed_margin'"
            )}
        return {"ok": False, "reason": f"仓位计算失败：{note}"}

    quantity = sizing["quantity"]
    margin = sizing["margin"]
    notional = sizing["notional"]
    risk_amount = sizing["risk_amount"]

    risk_per_unit = entry_price - stop_loss_price
    tp1_price = entry_price + risk_per_unit * config.TRADING_TP1_R
    tp2_price = entry_price + risk_per_unit * config.TRADING_TP2_R

    snapshot = {
        "manual": True,
        "trigger": "watchlist_add",
        "market": market,
        "realtime": realtime,
        "settings": settings,
        "_risk_meta": {
            "tier": "manual_full",
            "stop_mode": stop_mode,
            "stop_distance_pct": sizing.get("stop_distance_pct"),
            "risk_amount": risk_amount,
            "sector": risk.sector_of(token),
        },
    }
    position = {
        "token": token,
        "symbol": f"{token}USDT",
        "side": "LONG",
        "status": "OPEN",
        "mode": "paper",
        "margin_amount": margin,
        "leverage": leverage,
        "notional": notional,
        "quantity": quantity,
        "entry_price": entry_price,
        "limit_price": entry_price,
        "current_price": entry_price,
        "stop_loss_price": stop_loss_price,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "highest_price": entry_price,
        "trailing_stop_price": None,
        "signal_snapshot": json.dumps(snapshot, default=str, ensure_ascii=False),
        "open_reason": (
            f"手动收藏开仓 | 止损 {stop_mode} {sizing.get('stop_distance_pct', 0):.2f}% | "
            f"风险 ${risk_amount:.2f}"
        ),
        "advice": f"手动开仓持有：等待止盈或 {sizing.get('stop_distance_pct', 0):.2f}% 止损",
    }
    if not storage.trade_position_insert(conn, position):
        return {"ok": False, "reason": "DB 写入失败（可能并发冲突）"}

    # 写开仓 journal
    pos_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    snap_json = json.dumps({
        "price": entry_price, "stop_loss": stop_loss_price,
        "tp1": tp1_price, "tp2": tp2_price,
        "margin": margin, "notional": notional, "leverage": leverage,
        "stop_mode": stop_mode,
        "stop_distance_pct": sizing.get("stop_distance_pct"),
        "risk_amount": risk_amount, "trigger": "manual_watch",
    }, ensure_ascii=False)
    storage.journal_add_open(
        conn, position_id=pos_id, token=token, price=entry_price, tier="full",
        stop_loss=stop_loss_price, tp1_price=tp1_price, tp2_price=tp2_price,
        reason=f"收藏开仓 | 止损 {stop_mode} {sizing.get('stop_distance_pct', 0):.2f}% | 风险 ${risk_amount:.2f}",
        dimension_data=snap_json,
    )

    return {
        "ok": True, "token": token,
        "entry_price": entry_price,
        "quantity": quantity,
        "stop_loss_price": stop_loss_price,
        "risk_amount": risk_amount,
        "note": f"按风险 ${risk_amount:.2f} 开仓，止损 @ ${stop_loss_price:.6g}",
    }


def manual_close_on_unwatch(conn, token: str) -> dict:
    """取消收藏时按当前价格模拟市价平仓；未成交挂单直接取消。"""
    token = token.upper()
    market = _load_market(conn, token)
    realtime = _load_realtime(conn, token)
    price = _position_price(token, market, realtime)
    positions = [p for p in storage.trade_open_positions(conn) if p["token"].upper() == token]
    closed = 0
    canceled = 0
    realized_delta = 0.0

    for pos in positions:
        qty = float(pos.get("quantity") or 0)
        closed_qty = float(pos.get("closed_qty") or 0)
        open_qty = max(qty - closed_qty, 0)
        realized = float(pos.get("realized_pnl") or 0)

        if pos["status"] == "PENDING":
            storage.trade_position_update(conn, pos["id"], {
                "status": "CANCELED",
                "advice": "取消收藏触发：未成交挂单已取消",
                "closed_at": "__CURRENT_TIMESTAMP__",
            })
            canceled += 1
            continue

        if not price or price <= 0:
            continue
        entry = float(pos.get("entry_price") or pos.get("limit_price") or price)
        pnl = (price - entry) * open_qty
        realized += pnl
        realized_delta += pnl
        storage.trade_position_update(conn, pos["id"], {
            "status": "CLOSED",
            "current_price": price,
            "closed_qty": qty,
            "realized_pnl": realized,
            "unrealized_pnl": 0,
            "pnl_pct": (realized / float(pos.get("margin_amount") or 1)) * 100,
            "advice": "取消收藏触发：按市价平仓",
            "closed_at": "__CURRENT_TIMESTAMP__",
        })
        closed += 1

        # 写平仓 journal
        margin = float(pos.get("margin_amount") or 1)
        pnl_pct_val = (realized / margin) * 100
        _cs_snap = get_market_snapshot(token, heavy=True)
        _cs_analysis = analyze_signals(_cs_snap, social_score=0) if _cs_snap else {}
        close_snap = _build_close_snap(price, _cs_snap or {}, _cs_analysis)
        storage.journal_add_close(
            conn, order_id=pos["id"], token=token, price=price,
            reason="取消收藏触发：按市价平仓",
            pnl_pct=pnl_pct_val, close_reason="manual",
            dimension_data=close_snap,
            hold_duration=_calc_hold(pos.get("created_at")),
        )
        try:
            import sync_memory
            sync_memory.record_trade_from_journal(
                conn, token=token,
                pnl=pnl_pct_val,
                close_reason="manual",
                hold_duration=_calc_hold(pos.get("created_at")) or "",
            )
        except Exception:
            pass

    return {
        "ok": closed > 0 or canceled > 0,
        "token": token,
        "closed": closed,
        "canceled": canceled,
        "price": price,
        "realized_pnl": realized_delta,
        "reason": None if (closed or canceled) else "没有可平仓位或缺少市价",
    }


def _failure_tags(entry_snapshot: dict, exit_market: dict, exit_realtime: dict) -> list[str]:
    tags = []
    market = (entry_snapshot or {}).get("market") or {}
    snap = market.get("snapshot") or {}
    analysis = market.get("analysis") or {}
    exit_snap = (exit_market or {}).get("snapshot") or {}

    if "健康" not in (analysis.get("verdict") or ""):
        tags.append("entry_not_healthy")

    ch15 = _pct(snap.get("change_15m_pct"))
    ch1h = _pct(snap.get("change_1h_pct"))
    funding = _pct(snap.get("funding_rate_pct"))
    lsr = _pct(snap.get("long_short_ratio"))
    if ch15 is not None and ch15 > MAX_ENTRY_CHANGE_15M:
        tags.append("entry_15m_hot")
    if ch1h is not None and ch1h > MAX_ENTRY_CHANGE_1H:
        tags.append("entry_1h_hot")
    if funding is not None and funding >= ARCHIVE_FUNDING_HOT_PCT:
        tags.append("entry_funding_hot")
    if lsr is not None and lsr >= ARCHIVE_LONG_SHORT_HOT:
        tags.append("entry_lsr_hot")

    if (_pct(exit_snap.get("oi_change_15m_pct")) or 0) <= 0:
        tags.append("oi15_reversed")
    if (_pct(exit_snap.get("oi_change_1h_pct")) or 0) <= 0:
        tags.append("oi1h_reversed")
    if (_pct(exit_snap.get("oi_change_4h_pct")) or 0) <= 0:
        tags.append("oi4h_reversed")

    taker_exit = _pct((exit_realtime or {}).get("trade_buy_sell_ratio_60s")
                      or exit_snap.get("taker_buy_sell_ratio"))
    if taker_exit is not None and taker_exit < ARCHIVE_TAKER_WEAK:
        tags.append("buy_pressure_faded")

    return tags or ["price_hit_stop"]


def _archive_stop_loss(conn, pos: dict, exit_price: float, realized: float,
                       market: dict, realtime: dict):
    entry_snapshot = _loads(pos.get("signal_snapshot"), {})
    tags = _failure_tags(entry_snapshot, market, realtime)
    storage.trade_loss_archive_add(conn, {
        "position_id": pos.get("id"),
        "token": pos.get("token"),
        "symbol": pos.get("symbol"),
        "entry_price": pos.get("entry_price") or pos.get("limit_price"),
        "exit_price": exit_price,
        "realized_pnl": realized,
        "pnl_pct": (realized / float(pos.get("margin_amount") or 1)) * 100,
        "failed_reason": "-2% stop loss hit",
        "reason_tags": json.dumps(tags, ensure_ascii=False),
        "entry_snapshot": pos.get("signal_snapshot"),
        "exit_snapshot": json.dumps({
            "market": market,
            "realtime": realtime,
            "exit_price": exit_price,
            "archived_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }, default=str, ensure_ascii=False),
    })


def update_paper_positions(conn):
    positions = storage.trade_open_positions(conn)
    settings = storage.trading_settings_get(conn)
    mode = settings.get("mode") or "paper"

    # === 实盘：从 exchange 同步持仓状态 ===
    if mode == "live" and positions:
        import exchange
        live_positions = exchange.get_positions()
        live_map = {lp["symbol"][:-4]: lp for lp in live_positions}

        for pos in positions:
            token = pos["token"]
            lp = live_map.get(token.upper())
            if not lp:
                # 交易所已平仓
                qty = float(pos.get("quantity") or 0)
                realized = float(pos.get("realized_pnl") or 0)
                margin = float(pos.get("margin_amount") or 1)
                # 未从交易所拿到数据时保持原样
                if any(t.upper() in live_map for t in [p["token"] for p in positions]):
                    fields = {
                        "status": "CLOSED",
                        "unrealized_pnl": 0,
                        "pnl_pct": (realized / margin) * 100 if margin else 0,
                        "advice": "实盘已平仓（交易所无此持仓）",
                        "closed_at": "__CURRENT_TIMESTAMP__",
                    }
                    storage.trade_position_update(conn, pos["id"], fields)
                continue

            # 同步交易所数据
            fields = {
                "current_price": lp["mark_price"],
                "unrealized_pnl": lp["unrealized_pnl"],
                "pnl_pct": (float(pos.get("realized_pnl") or 0) + lp["unrealized_pnl"])
                           / float(pos.get("margin_amount") or 1) * 100,
                "highest_price": max(
                    float(pos.get("highest_price") or lp["mark_price"]),
                    lp["mark_price"],
                ),
            }
            storage.trade_position_update(conn, pos["id"], fields)
        return  # 实盘不跑本地止盈止损逻辑

    for pos in positions:
        market = _load_market(conn, pos["token"])
        realtime = _load_realtime(conn, pos["token"])
        price = _position_price(pos["token"], market, realtime)
        if not price:
            continue

        status = pos["status"]
        qty = float(pos.get("quantity") or 0)
        closed_qty = float(pos.get("closed_qty") or 0)
        open_qty = max(qty - closed_qty, 0)
        realized = float(pos.get("realized_pnl") or 0)
        fields = {"current_price": price}

        if status == "PENDING":
            limit_price = float(pos.get("limit_price") or 0)
            created_at = datetime.fromisoformat(str(pos["created_at"]).replace("Z", "+00:00"))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - created_at).total_seconds()
            if price <= limit_price:
                fields.update({
                    "status": "OPEN",
                    "entry_price": limit_price,
                    "current_price": price,
                    "highest_price": price,
                    "advice": "持仓中：等待止盈或止损",
                })
            elif age >= config.TRADING_LIMIT_ORDER_TIMEOUT_SECONDS:
                fields.update({
                    "status": "CANCELED",
                    "advice": "限价单超时未成交，已取消",
                    "closed_at": "__CURRENT_TIMESTAMP__",
                })
            storage.trade_position_update(conn, pos["id"], fields)
            continue

        entry = float(pos.get("entry_price") or pos.get("limit_price") or 0)
        if entry <= 0 or open_qty <= 0:
            continue

        highest = max(float(pos.get("highest_price") or entry), price)
        fields["highest_price"] = highest
        tp1 = float(pos.get("tp1_price") or 0)
        tp2 = float(pos.get("tp2_price") or 0)
        stop = float(pos.get("stop_loss_price") or 0)

        # ---- 止损：考虑滑点（比 stop 价更差的价格成交）----
        if stop > 0 and price <= stop:
            # 真实场景止损触发时常有滑点；paper 交易模拟更保守的成交价
            slip_factor = 1 - config.TRADING_STOP_SLIPPAGE_PCT / 100
            fill_price = min(price, stop * slip_factor)
            realized += (fill_price - entry) * open_qty
            if realized < 0:
                _archive_stop_loss(conn, pos, fill_price, realized, market, realtime)
            fields.update({
                "status": "CLOSED",
                "current_price": fill_price,
                "closed_qty": qty,
                "realized_pnl": realized,
                "unrealized_pnl": 0,
                "pnl_pct": _margin_pnl_pct(realized, 0, float(pos.get("margin_amount") or 1)),
                "advice": f"止损触发 @ ${fill_price:.6g}（含假设滑点），已平仓",
                "closed_at": "__CURRENT_TIMESTAMP__",
            })
            storage.trade_position_update(conn, pos["id"], fields)
            # 写平仓 journal
            _cs_snap = get_market_snapshot(pos["token"], heavy=True)
            _cs_analysis = analyze_signals(_cs_snap, social_score=0) if _cs_snap else {}
            close_snap = _build_close_snap(fill_price, _cs_snap or {}, _cs_analysis)
            storage.journal_add_close(
                conn, order_id=pos["id"], token=pos["token"], price=fill_price,
                reason=f"止损触发 @ ${fill_price:.6g}（含假设滑点）",
                pnl_pct=_margin_pnl_pct(realized, 0, float(pos.get("margin_amount") or 1)),
                close_reason="sl_hit", dimension_data=close_snap,
                hold_duration=_calc_hold(pos.get("created_at")),
            )
            try:
                import sync_memory
                sync_memory.record_trade_from_journal(
                    conn, token=pos["token"],
                    pnl=_margin_pnl_pct(realized, 0, float(pos.get("margin_amount") or 1)),
                    close_reason="sl_hit",
                    hold_duration=_calc_hold(pos.get("created_at")) or "",
                )
            except Exception:
                pass
            continue

        # ---- 止盈 TP1：达到 +1R，平 TP1_CLOSE_PCT%，止损移到保本 ----
        tp1_pct = config.TRADING_TP1_CLOSE_PCT / 100
        tp2_pct = config.TRADING_TP2_CLOSE_PCT / 100
        # 用"是否已触发过某档"而不是脆弱的数量比较
        closed_ratio = closed_qty / qty if qty > 0 else 0
        tp1_done = closed_ratio >= tp1_pct - 1e-6
        tp2_done = closed_ratio >= (tp1_pct + tp2_pct) - 1e-6

        if not tp1_done and tp1 > 0 and price >= tp1:
            close_qty = qty * tp1_pct
            realized += (tp1 - entry) * close_qty
            closed_qty += close_qty
            open_qty = qty - closed_qty
            fields.update({
                "status": "PARTIAL",
                "closed_qty": closed_qty,
                "realized_pnl": realized,
                "stop_loss_price": entry,  # 保本
                "advice": f"+{config.TRADING_TP1_R}R 已平 {config.TRADING_TP1_CLOSE_PCT:.0f}%，止损移到保本",
            })
            tp1_done = True

        # ---- 止盈 TP2：只在 TP1 已触发且 TP2 未触发时考虑 ----
        if tp1_done and not tp2_done and tp2 > 0 and price >= tp2:
            close_qty = qty * tp2_pct
            realized += (tp2 - entry) * close_qty
            closed_qty += close_qty
            open_qty = qty - closed_qty
            trailing = highest * (1 - config.TRADING_TRAIL_CALLBACK_PCT / 100)
            fields.update({
                "status": "PARTIAL",
                "closed_qty": closed_qty,
                "realized_pnl": realized,
                "trailing_stop_price": trailing,
                "advice": f"+{config.TRADING_TP2_R}R 已再平 {config.TRADING_TP2_CLOSE_PCT:.0f}%，剩余跟踪止盈",
            })
            tp2_done = True

        # ---- 剩余仓位：跟踪止盈 ----
        if tp2_done and open_qty > 0:
            trailing = max(float(pos.get("trailing_stop_price") or 0),
                           highest * (1 - config.TRADING_TRAIL_CALLBACK_PCT / 100))
            fields["trailing_stop_price"] = trailing
            if price <= trailing:
                # 跟踪止盈触发，也假设一点滑点
                slip_factor = 1 - config.TRADING_STOP_SLIPPAGE_PCT / 100
                fill_price = min(price, trailing * slip_factor)
                realized += (fill_price - entry) * open_qty
                fields.update({
                    "status": "CLOSED",
                    "current_price": fill_price,
                    "closed_qty": qty,
                    "realized_pnl": realized,
                    "unrealized_pnl": 0,
                    "pnl_pct": _margin_pnl_pct(realized, 0, float(pos.get("margin_amount") or 1)),
                    "advice": f"跟踪止盈触发 @ ${fill_price:.6g}，已平仓",
                    "closed_at": "__CURRENT_TIMESTAMP__",
                })
                storage.trade_position_update(conn, pos["id"], fields)
                # 写平仓 journal
                _cs_snap = get_market_snapshot(pos["token"], heavy=True)
                _cs_analysis = analyze_signals(_cs_snap, social_score=0) if _cs_snap else {}
                close_snap = _build_close_snap(fill_price, _cs_snap or {}, _cs_analysis)
                storage.journal_add_close(
                    conn, order_id=pos["id"], token=pos["token"], price=fill_price,
                    reason=f"跟踪止盈触发 @ ${fill_price:.6g}",
                    pnl_pct=_margin_pnl_pct(realized, 0, float(pos.get("margin_amount") or 1)),
                    close_reason="tp_hit", dimension_data=close_snap,
                    hold_duration=_calc_hold(pos.get("created_at")),
                )
                try:
                    import sync_memory
                    sync_memory.record_trade_from_journal(
                        conn, token=pos["token"],
                        pnl=_margin_pnl_pct(realized, 0, float(pos.get("margin_amount") or 1)),
                        close_reason="tp_hit",
                        hold_duration=_calc_hold(pos.get("created_at")) or "",
                    )
                except Exception:
                    pass
                continue

        unrealized = (price - entry) * open_qty
        fields.update({
            "unrealized_pnl": unrealized,
            "pnl_pct": _margin_pnl_pct(realized, unrealized, float(pos.get("margin_amount") or 1)),
        })
        storage.trade_position_update(conn, pos["id"], fields)
