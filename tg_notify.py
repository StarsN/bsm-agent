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


def send_alphafox_signals(signals: list[dict]):
    """跟单雷达信号推送（独立 bot，config 留空则跳过）。

    signals: alphafox_trade.process 返回的信号列表（开/加/减/平/跳过）
    """
    token = getattr(config, "ALPHAFOX_TG_BOT_TOKEN", "")
    chat_id = getattr(config, "ALPHAFOX_TG_CHAT_ID", "")
    if not token or not chat_id or not signals:
        return

    def esc_html(s):
        return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))

    act_icon = {"OPEN": "🚀", "ADD": "➕", "REDUCE": "➖", "CLOSE": "✅", "SKIP": "⏭️"}
    act_name = {"OPEN": "开", "ADD": "加", "REDUCE": "减", "CLOSE": "平"}
    lines = ["<b>📡 跟单信号</b>"]
    pushed = 0
    for s in signals:
        if s.get("action") not in act_name:
            continue  # SKIP/LEV 等不推
        if s.get("retry"):
            continue  # 执行失败待重试的信号不推（避免误导）
        side_txt = "多" if s.get("side") == "LONG" else "空"
        price = s.get("price")
        price_txt = f" @{float(price):.6g}" if price else ""
        notional = s.get("trader_notional")
        # alphofox 对 SHORT 仓的 notional 为负值，展示取绝对值
        notional_txt = f"$ {abs(float(notional)):,.0f}" if notional else ""
        lines.append(
            f"{act_icon.get(s['action'], '')} {esc_html(s.get('label'))} "
            f"{act_name.get(s['action'], s['action'])}{side_txt} "
            f"{esc_html(s.get('symbol'))}{price_txt}\n  名义 {notional_txt}")
        pushed += 1
    if pushed == 0:
        return
    try:
        requests.post(
            TG_API.format(token=token),
            json={"chat_id": chat_id, "text": "\n".join(lines),
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception:
        pass


def send_supertrend_reversal(token: str, old_trend: str, new_trend: str,
                             upper: float, lower: float, price: float,
                             candle_ts: int):
    """主流币趋势 15m 趋势反转通知（独立 bot，config 留空则跳过）。"""
    bot = getattr(config, "SUPERTREND_TG_BOT_TOKEN", "")
    chat_id = getattr(config, "SUPERTREND_TG_CHAT_ID", "")
    if not bot or not chat_id:
        return
    arrow = {"UP": "▲", "DOWN": "▼"}.get(new_trend, new_trend)
    old_arrow = {"UP": "▲", "DOWN": "▼"}.get(old_trend, old_trend)
    import time as _t
    # gmtime：不加服务器本地时区偏移，手动 +8h 即 UTC+8
    kt = _t.strftime("%m-%d %H:%M", _t.gmtime(candle_ts / 1000 + 8 * 3600))
    text = (f"<b>📈 主流币趋势反转</b>\n"
            f"{token} {old_arrow} {old_trend} → {arrow} {new_trend}\n"
            f"上轨道 {upper:.6g} ｜ 下轨道 {lower:.6g} ｜ 现价 {price:.6g}\n"
            f"K线时间 {kt}（UTC+8）")
    try:
        requests.post(
            TG_API.format(token=bot),
            json={"chat_id": chat_id, "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception:
        pass


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
