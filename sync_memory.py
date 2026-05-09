"""
Mem0 记忆系统：交易场景存储和模糊搜索

写入：auto_trader 执行开仓/平仓时同步记录
搜索：Agent 开仓前查类似场景
部署：pip install mem0ai，config.py 填 MEM0_API_KEY + MEM0_ENABLED=True
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


def record_open(token: str, round_num: int, tier: str,
                reason: str, dimension_data: str, market_overview: str):
    """
    记录开仓决策到记忆。

    token: 币种
    round_num: 来自 latest_round
    tier: full/half/quarter
    reason: Agent 决策理由
    dimension_data: 30个中文字段的 JSON 字符串
    market_overview: BTC走势+恐惧贪婪+时段
    """
    if not _enabled():
        return
    try:
        cl = _client()
        cl.add(
            messages=[
                {"role": "user",
                 "content": f"Round={round_num} {token} 市场数据：{dimension_data}"},
                {"role": "assistant",
                 "content": f"开仓决策：{token} tier={tier}。理由：{reason}。市场环境：{market_overview}"},
            ],
            user_id=_USER_ID,
            metadata={"token": token, "pnl": None, "round": round_num},
            infer=False,
        )
        _log.info(f"record_open {token} tier={tier} round={round_num}")
    except Exception as e:
        _log.warning(f"record_open {token} 失败: {e}")


def record_close(token: str, round_num: int, pnl: float,
                 close_reason: str, hold_duration: str):
    """
    追加平仓结果到记忆。

    token: 币种
    round_num: 来自 latest_round
    pnl: 盈亏百分比（正=盈利，负=亏损）
    close_reason: sl_hit / tp_hit / manual
    hold_duration: "2h30m" 格式
    """
    if not _enabled():
        return
    try:
        is_win = pnl >= 0
        result = "win" if is_win else "loss"
        if is_win and close_reason == "sl_hit":
            desc = f"尾仓sl_hit（整体盈利）"
        elif is_win:
            desc = f"{close_reason}（盈利）"
        else:
            desc = f"{close_reason}（亏损）"

        cl = _client()
        cl.add(
            messages=[
                {"role": "user",
                 "content": f"Round={round_num} {token} 平仓结果：{desc}, "
                            f"PnL={pnl:+.2f}%, 持仓={hold_duration}"},
            ],
            user_id=_USER_ID,
            metadata={"token": token, "pnl": pnl, "round": round_num, "result": result},
            infer=False,
        )
        _log.info(f"record_close {token} pnl={pnl:+.2f}% result={result}")
    except Exception as e:
        _log.warning(f"record_close {token} 失败: {e}")


def search_similar(query: str, token: str = None, limit: int = 5) -> list[dict]:
    """
    搜索历史类似场景。

    query: 场景描述，如 "OI涨 taker弱 新币"
    token: 可选，限定币种
    返回: [{memory, metadata, score}, ...]，score 越高越相似
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
