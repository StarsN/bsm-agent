"""
Mem0 记忆系统：一笔交易一条记忆。

写入：平仓后一次写入完整记录（数据+决策+结果合并）
搜索：Agent 开仓前查类似场景
"""
from __future__ import annotations

import config
import logging

_log = logging.getLogger("sync_memory")
_log.setLevel(logging.INFO)
if not _log.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[mem] %(asctime)s %(message)s", datefmt="%H:%M:%S"))
    _log.addHandler(h)


def _enabled():
    return getattr(config, "MEM0_ENABLED", False) and getattr(config, "MEM0_API_KEY", "")


def _client():
    from mem0 import MemoryClient
    return MemoryClient(api_key=config.MEM0_API_KEY)


_USER_ID = getattr(config, "MEM0_USER_ID", "agent-trade")


def record_trade(token: str, round_num: int, tier: str,
                 reason: str, dimension_data: str, market_overview: str,
                 pnl: float, close_reason: str, hold_duration: str):
    """
    平仓时写入一条完整记忆。

    调用方需传入开仓信息（来自 journal 或 decision）和平仓结果。
    Mem0 只存一条消息，含完整上下文。
    """
    if not _enabled():
        return
    try:
        is_win = pnl >= 0
        result = "win" if is_win else "loss"
        if is_win and close_reason == "sl_hit":
            outcome = "尾仓sl_hit（整体盈利）"
        elif is_win:
            outcome = f"{close_reason}（盈利）"
        else:
            outcome = f"{close_reason}（亏损）"

        content = (
            f"Round={round_num} {token} tier={tier} "
            f"入场理由：{reason}。"
            f"市场：{market_overview}。"
            f"结果：{outcome} PnL={pnl:+.2f}% 持仓={hold_duration}。"
            f"入场数据：{dimension_data}"
        )

        cl = _client()
        cl.add(
            messages=[{"role": "user", "content": content}],
            user_id=_USER_ID,
            metadata={"token": token, "pnl": pnl, "round": round_num, "result": result, "tier": tier},
            infer=False,
        )
        _log.info(f"record {token} tier={tier} pnl={pnl:+.2f}% {result}")
    except Exception as e:
        _log.warning(f"record {token} 失败: {e}")


def record_trade_from_journal(conn, token: str, pnl: float,
                               close_reason: str, hold_duration: str,
                               round_num: int = 0):
    """
    从 journal 表查开仓信息，组合平仓结果，写一条完整记忆。
    """
    if not _enabled():
        return
    try:
        # 查最近一条 open 日志
        row = conn.execute(
            "SELECT tier, reason, dimension_data, market_overview, source_round "
            "FROM journal WHERE token=? AND action='open' ORDER BY id DESC LIMIT 1",
            (token.upper(),)
        ).fetchone()
        if not row:
            return

        record_trade(
            token=token,
            round_num=row["source_round"] or round_num,
            tier=row["tier"] or "?",
            reason=row["reason"] or "",
            dimension_data=row["dimension_data"] or "{}",
            market_overview=row["market_overview"] or "",
            pnl=pnl,
            close_reason=close_reason,
            hold_duration=hold_duration,
        )
    except Exception as e:
        _log.warning(f"record_from_journal {token} 失败: {e}")


def search_similar(query: str, token: str = None, limit: int = 5) -> list[dict]:
    """
    搜索历史类似场景。

    query: 场景描述，如 "OI涨 taker弱 新币"
    token: 可选，限定币种
    返回: [{memory, metadata, score}, ...]
    """
    if not _enabled():
        return []
    try:
        cl = _client()
        filters = {"user_id": _USER_ID}
        if token:
            filters["metadata"] = {"token": token}

        result = cl.search(query, filters=filters, top_k=limit)
        items = result.get("results", []) if isinstance(result, dict) else []
        memories = [
            {
                "memory": r.get("memory", ""),
                "metadata": r.get("metadata", {}),
                "score": r.get("score", 0),
            }
            for r in items if isinstance(r, dict)
        ]
        _log.info(f"search '{query}' → {len(memories)} 条")
        return memories
    except Exception as e:
        _log.warning(f"search 失败: {e}")
        return []
