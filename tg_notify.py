"""Telegram 通知：链上监控每轮摘要推送到指定 Chat"""
import requests
import config

TG_API = "https://api.telegram.org/bot{token}/sendMessage"


def _send(msg: str, disable_preview: bool = True):
    """发送纯文本到配置的 TG Chat"""
    token = getattr(config, "TG_BOT_TOKEN", "")
    chat_id = getattr(config, "TG_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        requests.post(
            TG_API.format(token=token),
            json={
                "chat_id": chat_id,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": disable_preview,
            },
            timeout=10,
        )
    except Exception:
        pass


def _fmt_mc(mc) -> str:
    """市值格式化：500000 → 500K, 1500000 → 1.5M"""
    if not mc:
        return ""
    mc = float(mc)
    if mc >= 1_000_000:
        return f"${mc/1_000_000:.1f}M"
    return f"${mc/1_000:.0f}K"


def send_onchain_round(round_number: int, snapshots_count: int,
                       divergence_tokens: list[dict],
                       obs_count: int, act_count: int,
                       promoted_tokens: list[str]):
    """每轮链上扫描结束后调用，发送人类可读的分级摘要"""
    if not getattr(config, "TG_ONCHAIN_ENABLED", True):
        return

    parts = []

    # 标题行：一轮一句话
    parts.append(
        f"<b>🔍 链上扫描 第 {round_number} 轮</b>\n"
        f"扫描 {snapshots_count} 个代币"
        f"  |  观察池 {obs_count}  ·  待建仓 {act_count}"
    )

    # 背离入池：用中文标签替代技术缩写
    if divergence_tokens:
        lines = ["\n<b>⚡ 吸筹信号</b>（隐藏关联 &gt;30% + 表层持仓 &lt;40%）:"]
        for s in divergence_tokens[:5]:
            fund = s.get("holder_same_fund_pct") or 0
            top10 = s.get("top10_hold_pct") or 0
            eff = s.get("effective_concentration") or 0
            chain = s.get("chain", "")
            mc = _fmt_mc(s.get("market_cap_usd"))
            chain_tag = f" @{chain.upper()}" if chain else ""
            mc_tag = f"  {mc}" if mc else ""
            lines.append(
                f"  • <b>{s['token']}</b>{chain_tag}{mc_tag}\n"
                f"    隐藏关联资金 {fund:.0f}%  |  表层持仓 {top10:.0f}%  |  暗仓 {eff:.1f}%"
            )
        parts.append("\n".join(lines))

    # 晋级 actionable
    if promoted_tokens:
        lines = ["\n<b>🎯 晋级待建仓:</b>"]
        for t in promoted_tokens[:5]:
            lines.append(f"  • <b>{t}</b>")
        lines.append("\n已自动开仓，请关注持仓。")
        parts.append("\n".join(lines))

    _send("\n".join(parts))
