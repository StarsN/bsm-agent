"""
KOL Agent：加载蒸馏的 KOL 交易知识，调 DeepSeek 分析候选币盘面结构。

独立模块，不修改原有自动交易逻辑。分析结果写入 kol_analyses 表，仅供展示参考。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.request
from pathlib import Path
from typing import Optional

import config
import storage

# token 冷却缓存：记录每个 token 上次发给 LLM 的时间戳（epoch 秒）
_kol_token_last_sent: dict[str, float] = {}
_kol_snapshot_token_last_sent: dict[str, float] = {}

# ------------------------------------------------------------
# 1. 加载 KOL 知识文件
# ------------------------------------------------------------

def _load_kol_files(kol_dir: str) -> list[dict]:
    """扫描 kol_dir/TradeSnapshot/ 下的 _hermes_short_v1.md 文件。
    返回 [{name, framework}, ...]
    """
    d = Path(kol_dir) / "TradeSnapshot"
    if not d.is_dir():
        return []

    kols = []
    for fpath in sorted(d.glob("*_hermes_short_v1.md")):
        name = fpath.stem.replace("_hermes_short_v1", "").strip()
        if not name:
            continue
        text = fpath.read_text(encoding="utf-8").strip()
        if not text:
            continue
        kols.append({"name": name, "framework": text})
    return kols


# ------------------------------------------------------------
# 2. 拼接 Prompt
# ------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = """\
你是一位加密货币合约交易分析师，经过 {kol_count} 位专业交易员（KOL）交易体系的深度训练。

## 分析方法：KOL框架筛选

逐一用KOL的交易框架审视候选币：
- 盘面数据与至少一位KOL框架高度共振，且入场时机成熟 → 给出 ENTER + 对应方向
- 盘面数据不匹配任何KOL框架 → direction=none
- 禁止自行创造KOL框架之外的分析逻辑

以下是 {kol_count} 位KOL的交易框架，请以这些框架为唯一筛选标准：

{kol_sections}

## 输出格式

返回严格JSON，不要额外文字：
{{
  "analyses": [
    {{
      "token": "币种简称（如 FLUID、ORDI）",
      "trend": "多头建仓结构 / 空头出货结构 / 区间盘整",
      "price_levels": {{
        "current": 数字或null, "support": 数字或null, "resistance": 数字或null,
        "entry": 数字或null, "stop_loss": 数字或null, "take_profit": 数字或null
      }},
      "summary": "盘面综合摘要，包含价格位置、支撑阻力、多空力量，100-200字",
      "reasoning": {{
        "wz": "位置: 当前价格在支撑阻力中的位置、多空力量分析",
        "sj": "时机: 什么条件下入场、什么条件下等待",
        "fk": "风控: 止损/止盈设置逻辑（direction=none时可填'未入场'）"
      }},
      "status": "ENTER / WAIT",
      "context_tag": "range_low · support_holding",
      "evidence_tags": ["taker 0.74", "24h -5.5%", "支撑未破", "BTC横盘"],
      "direction": "long / short / none",
      "confidence": 75,
      "position_size": "full / half"
    }}
  ]
}}

## 字段说明
- token: 币种简称，大写
- trend: 盘面结构类型。三选一：
  "多头建仓结构" — 支撑位确认、量价配合，适合做多
  "空头出货结构" — 阻力位受阻、抛压信号，适合做空
  "区间盘整" — 无明显方向，等待突破
- price_levels: 关键价格位，无依据填 null
  - current: 当前标记价
  - support: 最近有效支撑位
  - resistance: 最近有效阻力位
  - entry: 建议入场价。status=ENTER 时必须填有效价格；direction=none 时填 null
  - stop_loss: 建议止损价（无交易机会时填 null）
  - take_profit: 建议止盈目标（无交易机会时填 null）
- summary: 盘面综合摘要，包含价格位置、支撑阻力、多空力量（100-200字）
- reasoning: 逻辑推演（键值对结构）
  - wz (位置): 当前价格在支撑阻力中的位置分析
  - sj (时机): 什么条件下入场、等待的具体说明
  - fk (风控): 止损/止盈设置逻辑
- status: 执行时机
  ENTER: 现在可以入场, 条件已满足
  WAIT: 时机未到, 等待条件触发
  direction=none时填WAIT
- context_tag: 盘面场景标签，用" · "连接多个标签，概括当前市场结构
  示例: "range_low · support_holding", "near_resistance", "breakout_confirmed", "overextended_pullback"
- evidence_tags: 做出判断引用的具体数据证据，每条约5-15字
  示例: ["24h -5.5%", "taker 0.74", "OI增长+价格持平", "BTC横盘"]
  direction=none或证据不足时填["无明显信号"]
- direction: 交易方向，long(做多) / short(做空) / none(无机会)
- confidence: 对当前判断的信心评分，0-100整数。≥80=高把握，50-79=中等，<50=低把握
- position_size: 仓位档位。full(满仓) / half(半仓)
  full: 结构明确 + 信号共振、把握较高的机会
  half: 信号部分共振、或盘面有不确定因素
  direction=none时填half

## 规则
- **KOL框架为唯一标准**：只在KOL框架明确支持时给出ENTER。不匹配任何KOL框架的币 → direction=none
- price_levels 中无明确依据的字段填 null
- direction 为 none 时 price_levels 的 entry/stop_loss/take_profit 填 null
- evidence_tags 必须引用用户 prompt 中的具体数据，不要笼统
"""


_SNAPSHOT_SYSTEM_PROMPT_TEMPLATE = """# Role
你是一个集成了多位专业交易员认知快照的 AI 交易分析引擎。你现在同时拥有 {kol_count} 位交易员的【离线认知大脑快照】，并且能够实时审视当前市场的微观盘面数据矩阵。

# Goal
你需要通过"矩阵交叉推理（Matrix Cross-Reasoning）"，站在客观的系统整体风险回报比（R:R）的角度，将交易员的认知与实时盘面进行多维对齐，最终输出精准、可执行的交易建议。

# Reasoning Methodology (四步交叉推演法)
在给出最终决策前，你必须按以下顺序完成推演：
0. **全景定向**：先读取用户 prompt 中的"市场全景环境"。AI市场研判是方向核心依据。
   - BTC与AI同向 → 该方向，可full
   - BTC与AI分歧 → 以AI为准，half（典型：山寨季BTC横盘AI看多）
   - AI震荡死水（chop） → 参考BTC方向，双向half
   - BTC 1h >3%闪崩且与AI相悖 → 以BTC为准
1. **情境共振校验**：在全景确定的方向内，逐一将实时的盘面数据矩阵投入到各位交易员的核心交易情境中。计算当前盘面究竟高度共振了谁的框架？触发了谁的警觉陷阱？
2. **多维冲突仲裁**：如果盘面数据导致交易员们的策略产生了冲突（例如：交易员 A 认为当前 OI 暴增符合他的右侧追多框架；但交易员 B 的盘口模型提示当前深度单薄，属于薄盘口滑点黑洞），你必须根据当前的系统性流动性（Beta 环境）进行硬核仲裁，决定谁的权重在当前情境下更高。
3. **启发式执行映射**：根据当前价格在支撑阻力位的实际微观表现（如是否放量突破、是否假跌破收回），动态判断入场时机和执行价格。

# Output Control
1. 保持分析简洁、专业、高信息密度，避免冗余描述。
2. 严禁带有任何情绪化色彩。
3. 如果盘面指标不足以支撑任何交易员的框架，必须果断给出 WAIT + direction=none（观望，无交易方向）。

---

以下是 {kol_count} 位交易员的离线认知快照。请仔细阅读并内化这些方法论，然后用他们的视角分析候选币的盘面结构、交易机会和方向。

{kol_sections}

## 输出格式

返回严格JSON，不要额外文字：
{{
  "analyses": [
    {{
      "token": "币种简称（如 FLUID、ORDI）",
      "trend": "多头建仓结构 / 空头出货结构 / 区间盘整",
      "price_levels": {{
        "current": 数字或null, "support": 数字或null, "resistance": 数字或null,
        "entry": 数字或null, "stop_loss": 数字或null, "take_profit": 数字或null
      }},
      "summary": "盘面综合摘要，包含价格位置、支撑阻力、多空力量，100-200字",
      "reasoning": {{
        "wz": "位置: 当前价格在支撑阻力中的位置、多空力量分析",
        "sj": "时机: 什么条件下入场、什么条件下等待",
        "fk": "风控: 止损/止盈设置逻辑（direction=none时可填'未入场'）"
      }},
      "status": "ENTER / WAIT",
      "context_tag": "range_low · support_holding",
      "evidence_tags": ["taker 0.74", "24h -5.5%", "支撑未破", "BTC横盘"],
      "direction": "long / short / none",
      "confidence": 75,
      "position_size": "full / half"
    }}
  ]
}}

## 字段说明
- token: 币种简称，大写
- trend: 盘面结构类型。三选一：
  "多头建仓结构" — 支撑位确认、量价配合，适合做多
  "空头出货结构" — 阻力位受阻、抛压信号，适合做空
  "区间盘整" — 无明显方向，等待突破
- price_levels: 关键价格位，无依据填 null
  - entry: status=ENTER 时必须填有效价格，direction=none 时填 null
- summary: 盘面综合摘要，100-200字
- reasoning: 逻辑推演（键值对结构）
  - wz (位置): 当前价格在支撑阻力中的位置分析
  - sj (时机): 什么条件下入场、等待的具体说明
  - fk (风控): 止损/止盈设置逻辑
- status: 执行时机
  ENTER: 条件已满足，可以入场
  WAIT: 时机未到或框架不支持，等待条件触发
  direction=none时填WAIT
- context_tag: 盘面场景标签，用" · "连接
- evidence_tags: 做出判断引用的具体数据证据，每条约5-15字。direction=none或证据不足时填["无明显信号"]
- direction: 交易方向 long/short/none
- confidence: 0-100整数。>=80=高把握，50-79=中等，<50=低把握
- position_size: 仓位档位。full(满仓) / half(半仓)
  full: BTC与AI同向 + 结构明确 + 信号共振、把握较高的机会
  half: BTC与AI分歧、或AI震荡、或信号部分共振、或盘面有不确定因素
  direction=none时填half

## 规则
- **全景定向**：先判断宏观方向，在此方向内筛选交易员信号，不逆宏观
- **交易员框架为唯一标准**：不匹配任何交易员框架的币 → direction=none
- price_levels 中无明确依据的字段填 null
- direction 为 none 时 price_levels 的 entry/stop_loss/take_profit 填 null
- evidence_tags 必须引用用户 prompt 中的具体数据，不要笼统
"""


def _build_kol_section(kol: dict) -> str:
    """把单个 KOL 的交易框架拼成一段 markdown"""
    name = kol["name"]
    framework = kol["framework"]
    framework_clean = re.sub(r"^# .+\n", "", framework, count=1).strip()
    return f"## {name} 的交易框架\n\n{framework_clean}\n"


def build_system_prompt(kol_data: list[dict]) -> str:
    kol_sections = "\n---\n".join(_build_kol_section(k) for k in kol_data)
    return _SYSTEM_PROMPT_TEMPLATE.format(
        kol_count=len(kol_data),
        kol_sections=kol_sections,
    )


def _fmt_pct(v):
    """百分比：自适应精度 — 大值 2 位、小值保留更多小数，0 值不退化"""
    if v is None:
        return None
    try:
        v = float(v)
        if v == 0:
            return "0.00%"
        if abs(v) >= 0.01:
            return f"{v:.2f}%"
        if abs(v) >= 0.0001:
            return f"{v:.4f}%"
        return f"{v:.6f}%"
    except (ValueError, TypeError):
        return str(v)


def _fmt_ratio(v):
    """比率：自适应精度，None 安全，0 值不退化"""
    if v is None:
        return None
    try:
        v = float(v)
        if v == 0:
            return "0.00"
        if abs(v) >= 0.01:
            return f"{v:.2f}"
        if abs(v) >= 0.0001:
            return f"{v:.4f}"
        return f"{v:.6f}"
    except (ValueError, TypeError):
        return str(v)


def _fmt_usd(v):
    """USD 金额：自动 K/M 后缀，None 安全"""
    if v is None:
        return None
    try:
        v = float(v)
        if abs(v) >= 1_000_000:
            return f"{v / 1_000_000:.2f}M"
        if abs(v) >= 1_000:
            return f"{v / 1_000:.1f}K"
        return f"{v:.0f}"
    except (ValueError, TypeError):
        return str(v)


def _fmt_price(v):
    """价格：按量级自适应精度（abs 防负值），None 安全"""
    if v is None:
        return None
    try:
        v = float(v)
        if abs(v) >= 1000:
            return f"{v:.2f}"
        if abs(v) >= 1:
            return f"{v:.4f}"
        if abs(v) >= 0.01:
            return f"{v:.6f}"
        return f"{v:.8f}"
    except (ValueError, TypeError):
        return str(v)


def _fmt_holders(hd: dict) -> str | None:
    """持仓分布 → 简洁文本，如 TOP1: whale 12.3% | TOP2: Binance 8.1%"""
    top = hd.get("top10", [])
    if not top:
        return None
    parts = []
    for h in top[:5]:
        tag = h.get("tag") or h.get("address_short", "?")
        pct = h.get("pct", 0)
        parts.append(f"#{h['rank']} {tag} {pct:.1f}%")
    return " | ".join(parts)


def _fmt_funding_hist(hist: list) -> str | None:
    """OKX 费率历史 → 趋势文本，如 +0.0050% → -0.0020%"""
    if not hist or not isinstance(hist, list):
        return None
    return " → ".join(
        f"{h.get('r', 0):+.4f}%" for h in hist[-4:]
    )


def _fmt_klines(klines, limit: int = 0):
    """格式化 K 线: O/H/L/C/Vol/额, ... limit=0 表示全部"""
    if not klines:
        return None
    k = klines[-limit:] if limit > 0 else klines
    parts = []
    for k in k:
        if len(k) >= 6:
            parts.append(f"{k[0]:.4f}/{k[1]:.4f}/{k[2]:.4f}/{k[3]:.4f}/{k[4]:.0f}/{k[5]:.0f}")
    return ", ".join(parts) if parts else None


def _build_kline_summary(klines, label: str = "", limit: int = 0):
    """从 K 线提取结构化摘要：趋势 + 区间 + 位置 + 量能。limit>0 时只取最近 N 根"""
    if not klines or len(klines) < 3:
        return None
    if limit > 0 and len(klines) > limit:
        klines = klines[-limit:]
    try:
        highs = [float(k[1]) for k in klines]
        lows  = [float(k[2]) for k in klines]
        closes = [float(k[3]) for k in klines]
        vols = [float(k[4]) for k in klines]

        hi, lo = max(highs), min(lows)
        cur = closes[-1]
        rng = hi - lo
        pos_pct = (cur - lo) / rng * 100 if rng > 0 else 50

        # 前后半段均价比
        mid = len(closes) // 2
        first_avg = sum(closes[:mid]) / mid
        second_avg = sum(closes[mid:]) / (len(closes) - mid)
        if second_avg > first_avg * 1.005:
            trend = "上升"
        elif second_avg < first_avg * 0.995:
            trend = "下降"
        else:
            trend = "横盘"

        # 量能
        avg_vol = sum(vols) / len(vols) if vols else 1
        last_vol = vols[-1]
        if last_vol > avg_vol * 1.5:
            vol_label = "放量"
        elif last_vol < avg_vol * 0.7:
            vol_label = "缩量"
        else:
            vol_label = "量平"

        prefix = f"{label} " if label else ""
        return f"{prefix}{trend} | 区间 ${lo:.4f}-${hi:.4f} | 当前在{pos_pct:.0f}%位 | {vol_label}"
    except Exception:
        return None


def build_user_prompt(candidates: list[dict]) -> str:
    """把候选币数据格式化为 user prompt"""
    lines = ["## 候选币市场数据\n"]
    for c in candidates:
        lines.append(f"### {c['token']}")
        fields = [
            ("标记价", _fmt_price(c.get("price"))),
            ("15m涨跌", _fmt_pct(c.get("15m"))),
            ("1h涨跌", _fmt_pct(c.get("1h"))),
            ("4h涨跌", _fmt_pct(c.get("4h"))),
            ("24h涨跌", _fmt_pct(c.get("24h"))),
            ("48h涨跌", _fmt_pct(c.get("chg_48h"))),
            ("OI 15m变化", _fmt_pct(c.get("oi_15m"))),
            ("OI 1h变化", _fmt_pct(c.get("oi_1h"))),
            ("OI 4h变化", _fmt_pct(c.get("oi_4h"))),
            ("OI 48h变化", _fmt_pct(c.get("oi_48h"))),
            ("资金费率", f"{_fmt_ratio(c.get('funding'))}%/8h" if c.get('funding') is not None else None),
            ("主动买卖比", _fmt_ratio(c.get("taker"))),
            ("主动买入占比", _fmt_pct(c.get("taker_pct"))),
            ("Taker趋势", _fmt_pct(c.get("taker_trend"))),
            ("盘口价差", _fmt_pct(c.get("spread"))),
            ("买盘深度(USD)", _fmt_usd(c.get("depth_bid"))),
            ("卖盘深度(USD)", _fmt_usd(c.get("depth_ask"))),
            ("盘口失衡度", _fmt_pct(c.get("imbalance"))),
            ("散户多空比", _fmt_ratio(c.get("lsr"))),
            ("大户多空比", _fmt_ratio(c.get("top_lsr"))),
            ("24h成交额", _fmt_usd(c.get("vol_24h"))),
            ("OI(USD)", _fmt_usd(c.get("oi_usd"))),
            ("上币时长", c.get("age")),
            # 全景字段
            ("vol/OI", f"{c.get('vol_oi'):.2f}" if c.get("vol_oi") is not None else None),
            ("上线天数", f"{c.get('listing_days'):.0f}d" if c.get("listing_days") is not None else None),
            ("品类", c.get("sector")),
            ("链", c.get("chain")),
            ("基差", _fmt_pct(c.get("basis")) if c.get("basis") is not None else None),
            ("OKX 资金费率", _fmt_pct(c.get("okx_funding")) if c.get("okx_funding") is not None else None),
            ("OKX 费率趋势(最近4条)", _fmt_funding_hist(c.get("funding_hist"))),
            # 市值 + 乖离 + 聪明钱
            ("市值", _fmt_usd(c.get("market_cap_usd")) if c.get("market_cap_usd") else None),
            ("市值排名", f"#{int(c.get('market_cap_rank'))}" if c.get("market_cap_rank") else None),
            ("MA20乖离(5h)", _fmt_pct(c.get("ma20_deviation"))),
            ("聪明钱多头占比", _fmt_pct(c.get("sm_long_ratio"))),
            ("聪明钱净头寸(USD)", _fmt_usd(c.get("sm_net_notional_usdt")) if c.get("sm_net_notional_usdt") else None),
            ("聪明钱多头胜率", _fmt_pct(c.get("sm_avg_long_win_rate"))),
            ("聪明钱关注人数", c.get("sm_traders_with_position")),
            ("聪明钱多头均价", _fmt_price(c.get("sm_long_avg_entry")) if c.get("sm_long_avg_entry") else None),
            ("聪明钱空头均价", _fmt_price(c.get("sm_short_avg_entry")) if c.get("sm_short_avg_entry") else None),
            ("聪明钱空头胜率", _fmt_pct(c.get("sm_avg_short_win_rate"))),
            ("OI/市值", _fmt_pct(c.get("oi_marketcap"))),
            # 链上数据 (CoinGecko + GMGN)
            ("FDV", _fmt_usd(c.get("fdv_usd")) if c.get("fdv_usd") else None),
            ("持币地址总数", f"{int(c['gmgn_holder_count']):,}" if c.get("gmgn_holder_count") else None),
            ("前10持仓占比", _fmt_pct(c.get("gmgn_top_10_holder_rate")) if c.get("gmgn_top_10_holder_rate") is not None else None),
            ("团队持币占比", _fmt_pct(c.get("gmgn_dev_hold_rate")) if c.get("gmgn_dev_hold_rate") is not None else None),
            ("狙击手持币占比", _fmt_pct(c.get("gmgn_sniper_hold_rate")) if c.get("gmgn_sniper_hold_rate") is not None else None),
            ("持仓分布(前10合计)", _fmt_pct(c.get("holder_distribution", {}).get("top10_pct")) if c.get("holder_distribution") else None),
            ("持仓TOP5", _fmt_holders(c.get("holder_distribution")) if c.get("holder_distribution") else None),
            ("链上聪明钱", f"{c['gmgn_smart_wallets']}个" if c.get("gmgn_smart_wallets") else None),
            ("链上巨鲸", f"{c['gmgn_whale_wallets']}个" if c.get("gmgn_whale_wallets") else None),
            ("链上KOL", f"{c['gmgn_renowned_wallets']}个" if c.get("gmgn_renowned_wallets") else None),
            ("链上狙击手", f"{c['gmgn_sniper_wallets']}个" if c.get("gmgn_sniper_wallets") else None),
            ("链上新钱包", f"{c['gmgn_fresh_wallets']}个" if c.get("gmgn_fresh_wallets") else None),
            ("链上捆绑包", f"{c['gmgn_bundler_wallets']}个" if c.get("gmgn_bundler_wallets") else None),
            ("链上老鼠仓", f"{c['gmgn_rat_trader_wallets']}个" if c.get("gmgn_rat_trader_wallets") else None),
            # K线结构
            ("1h结构", _build_kline_summary(c.get("klines_1h"), limit=6)),
            ("1H K线(开/高/低/收/成交量/成交额)", _fmt_klines(c.get("klines_1h"), 6)),
            ("4h结构", _build_kline_summary(c.get("klines_4h"))),
            ("4H K线(开/高/低/收/成交量/成交额)", _fmt_klines(c.get("klines_4h"), 6)),
            ("日线位置", _build_kline_summary(c.get("klines_1d"), "日线")),
            ("日K (开/高/低/收/成交量/成交额)", _fmt_klines(c.get("klines_1d"), 7)),
            ("信号标签", c.get("tags")),
        ]
        for label, val in fields:
            if val is not None:
                lines.append(f"- {label}: {val}")
        lines.append("")

    return "\n".join(lines)


# ------------------------------------------------------------
# 3. 调用 DeepSeek API
# ------------------------------------------------------------

def call_deepseek(system: str, user: str, max_tokens: int = 4096, provider: str = "", api_key_override: str = "", deepseek_api_key: str = "", nvidia_api_key: str = "") -> Optional[str]:
    """返回 LLM 回复文本，失败返回 None。deepseek_api_key/nvidia_api_key 用于策略独立 key。"""
    provider = provider or getattr(config, "KOL_LLM_PROVIDER", "deepseek")
    if provider == "nvidia":
        api_key = api_key_override or nvidia_api_key or config.NVIDIA_API_KEY
        model = config.NVIDIA_MODEL
        api_base = config.NVIDIA_API_BASE
    else:
        api_key = deepseek_api_key or config.DEEPSEEK_API_KEY
        model = config.DEEPSEEK_MODEL
        api_base = config.DEEPSEEK_API_BASE
    if not api_key:
        print(f"[kol_agent] {provider} API Key 未配置，跳过")
        return None

    body_data = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    body_data["reasoning_effort"] = "max"
    body = json.dumps(body_data).encode("utf-8")

    url = f"{api_base.rstrip('/')}/chat/completions"
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    })

    raw_body = b""
    try:
        with urllib.request.urlopen(req, timeout=getattr(config, "KOL_LLM_TIMEOUT", 600)) as resp:
            raw_body = resp.read()
            data = json.loads(raw_body.decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            return content
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")[:500]
        print(f"[kol_agent] HTTP {e.code}: {err_body}")
        return None
    except Exception as e:
        detail = str(e)
        if raw_body:
            detail += f" | body={raw_body.decode('utf-8', errors='replace')[:300]}"
        print(f"[kol_agent] API 调用失败: {detail}")
        return None


def _parse_response(raw: str) -> list[dict]:
    """从 LLM 返回中提取 analyses 列表，容错处理"""
    # 尝试直接解析
    try:
        data = json.loads(raw)
        return data.get("analyses", [])
    except json.JSONDecodeError:
        pass

    # 尝试提取 JSON 块
    m = re.search(r"\{[\s\S]*\"analyses\"[\s\S]*\}", raw)
    if m:
        try:
            return json.loads(m.group()).get("analyses", [])
        except json.JSONDecodeError:
            pass

    print(f"[kol_agent] 无法解析 LLM 返回: {raw[:300]}")
    return []


def kol_is_interesting(candidate: dict):
    """KOL 策略代币异常度筛选。OI异常 + 费率异常 + 价格乖离 ≥2 → 入选 | vol/OI > 20x → 排除
    返回 (是否关注, 异常原因列表)"""
    reasons = []
    snap = candidate.get("market", {}).get("snapshot", {})
    if not snap or not snap.get("mark_price"):
        return False, reasons
    vol_oi = snap.get("vol_oi_ratio")
    if vol_oi is not None and float(vol_oi) > getattr(config, "KOL_MAX_VOL_OI", 20):
        return False, reasons
    score = 0
    if abs(snap.get("oi_change_1h_pct") or 0) > getattr(config, "KOL_MIN_OI_CHANGE_1H_PCT", 4) \
       or abs(snap.get("oi_change_4h_pct") or 0) > getattr(config, "KOL_MIN_OI_CHANGE_4H_PCT", 10):
        score += 1
        reasons.append("OI异动")
    if abs(snap.get("funding_rate_pct") or 0) > getattr(config, "KOL_MIN_FUNDING_ABS_PCT", 0.03):
        score += 1
        reasons.append("费率异常")
    if abs(snap.get("ma20_deviation_pct") or 0) > getattr(config, "KOL_MIN_MA20_DEVIATION_PCT", 2.5):
        score += 1
        reasons.append("MA20乖离")
    return score >= getattr(config, "KOL_MIN_ANOMALY_SCORE", 2), reasons


def get_kol_candidates(conn: sqlite3.Connection, strategy: str = "kol_agent") -> list[dict]:
    """从 kol_candidates 读取候选币（strategy 决定读哪个 agent DB）"""
    import json as _json
    ts = storage.trading_settings_get(conn)
    key = "kol_agent_interval_minutes" if strategy == "kol_agent" else "kol_snapshot_interval_minutes"
    default = getattr(config, "KOL_AGENT_INTERVAL_MINUTES", 15) if strategy == "kol_agent" else getattr(config, "KOL_SNAPSHOT_INTERVAL_MINUTES", 8)
    inter_min = int(ts.get(key, default))
    db_path = config.KOL_DB if strategy == "kol_agent" else config.SNAPSHOT_DB
    with storage.get_conn(db_path) as agent_conn:
        rows = agent_conn.execute(
            f"SELECT data FROM kol_candidates "
            f"WHERE created_at >= datetime('now', '-{inter_min + 2} minutes') "
            "ORDER BY id"
        ).fetchall()
    candidates = []
    for r in rows:
        try:
            c = _json.loads(r["data"])
            candidates.append(c)
        except (json.JSONDecodeError, TypeError):
            pass
    return candidates


# ------------------------------------------------------------
# 4. 主入口：分析候选币 → 写入 DB
# ------------------------------------------------------------

def _build_panorama_context() -> str:
    """获取市场全景数据（除 AI Regime），格式化为 KOL prompt header"""
    lines = [
        "## 市场全景环境",
        "",
        "**市场全景环境：AI市场研判是方向判断的核心依据。请结合以下数据判断趋势。**",
        "",
        "---",
        "",
    ]
    # 恐惧贪婪
    try:
        import json as _json
        import urllib.request as _req
        url = "https://api.alternative.me/fng/?limit=1"
        r = _req.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _req.urlopen(r, timeout=5) as resp:
            d = _json.loads(resp.read().decode("utf-8"))
            if d.get("data"):
                fng = d["data"][0]
                fng_cn = {"Extreme Fear": "极度恐惧", "Fear": "恐惧", "Neutral": "中性",
                          "Greed": "贪婪", "Extreme Greed": "极度贪婪"}
                class_en = fng.get("value_classification", "")
                class_cn = fng_cn.get(class_en, class_en)
                lines.append(f"- 恐惧贪婪指数: {fng.get('value')} ({class_cn})")
    except Exception:
        pass
    # BTC / 流动性 / 宏观
    try:
        from dashboard import get_risk_metrics, get_macro_events
        rm = get_risk_metrics()
        m1h = rm["metrics"]["macro_1h"]
        m3h = rm["metrics"]["macro_3h"]
        zd = rm["metrics"]["z_depth"]
        liq = rm["metrics"]["liquidity"]
        def _btc_label(v):
            if v > 0.5: return "大涨"
            if v > 0.0: return "微涨"
            if v > -0.1: return "横盘"
            if v > -0.5: return "微跌"
            return "下跌"
        v1, v3 = m1h["value"], m3h["value"]
        lines.append(f"- BTC 1h: {v1:+.2f}% ({_btc_label(v1)}) | BTC 3h: {v3:+.2f}% ({_btc_label(v3)})")
        zd_desc = zd.get("desc", "")
        zd_note = "偏薄" if "RISK_OFF" in zd_desc else ""
        lines.append(f"- 订单簿深度: ${zd['value']}M{' (' + zd_note + ')' if zd_note else ''}")
        lines.append(f"- 流动性: {liq['status']} ({liq['desc']})")
        # V3 AI Regime（只有缓存命中才附加，loading 中不喂）
        try:
            from dashboard import get_ai_regime
            regime = get_ai_regime()
            jt = regime.get("judgment_text", "")
            if "生成中" in jt or not jt:
                pass  # loading 或空，不喂给 KOL
            else:
                regime_cn = {"alt_season": "山寨行情", "alt_pullback": "回调兑现",
                             "chop": "震荡死水", "risk_off": "趋势空"}.get(regime.get("current_regime", ""), regime.get("current_regime", ""))
                regime_dir = {"alt_season": "偏多", "alt_pullback": "中性偏空", "chop": "无方向", "risk_off": "偏空"}
                dir_tag = regime_dir.get(regime.get("current_regime", ""), "")
                lines.append(f"- AI 市场研判: {regime_cn} (conf {regime.get('confidence', 0)}) → {dir_tag}")
                lines.append(f"  {jt[:200]}")
        except Exception:
            pass
        me = get_macro_events()
        risk_label = {"high": "高风险", "medium": "注意", "normal": "正常"}.get(me["current_risk"], me["current_risk"])
        lines.append(f"- 宏观风险评级: {risk_label}")
        if me.get("next_event"):
            ne = me["next_event"]
            lines.append(f"- 即将: {ne['name']} ({me.get('countdown_str', '')})")
        # 完整事件列表
        events = me.get("all_events", me.get("recent_events", []))
        if events:
            lines.append("- 未来一周宏观事件:")
            for e in events:
                tag = e.get("tag", "")
                forecast = e.get("forecast", "")
                previous = e.get("previous", "")
                extra = ""
                if forecast or previous:
                    extra = f" 预测:{forecast or 'N/A'} 前值:{previous or 'N/A'}"
                lines.append(f"  [{tag}] {e['name']} @ {e['time'][:16]}{extra}")
    except Exception:
        pass
    lines.append("")
    return "\n".join(lines)




def _load_snapshot_knowledge(snapshot_dir: str) -> list[dict]:
    """扫描 snapshot_dir/ 下的 *_KnowledgeSnapshot_short.md 文件。返回 [{name, framework}, ...]"""
    d = Path(snapshot_dir)
    if not d.is_dir():
        return []
    kols = []
    for fpath in sorted(d.glob("*_KnowledgeSnapshot_short.md")):
        name = fpath.stem.replace("_KnowledgeSnapshot_short", "").replace("__", "_").strip()
        if not name:
            continue
        text = fpath.read_text(encoding="utf-8").strip()
        if not text:
            continue
        kols.append({"name": name, "framework": text})
    return kols


def build_snapshot_system_prompt(kol_data: list[dict]) -> str:
    """用认知快照拼接 system prompt。使用独立的 _SNAPSHOT_SYSTEM_PROMPT_TEMPLATE。"""
    kol_sections = "\n---\n".join(_build_kol_section(k) for k in kol_data)
    return _SNAPSHOT_SYSTEM_PROMPT_TEMPLATE.format(
        kol_count=len(kol_data),
        kol_sections=kol_sections,
    )


def analyze_candidates_snapshot(conn: sqlite3.Connection) -> list[dict]:
    """与 analyze_candidates 完全相同的流程，仅 KOL 知识来源不同（_short.md 认知快照）"""
    snapshot_dir = getattr(config, "KOL_SNAPSHOT_KNOWLEDGE_DIR", "") or getattr(config, "KOL_KNOWLEDGE_DIR", "")
    if not snapshot_dir or not os.path.isdir(snapshot_dir):
        print("[kol_snapshot] KOL_KNOWLEDGE_DIR 不存在或未配置")
        return []

    trade_snapshot_dir = str(Path(snapshot_dir) / "TradeSnapshot")
    if not os.path.isdir(trade_snapshot_dir):
        trade_snapshot_dir = snapshot_dir

    kol_data = _load_snapshot_knowledge(trade_snapshot_dir)
    if not kol_data:
        print("[kol_snapshot] 未找到有效的 KOL 认知快照文件")
        return []

    candidates = get_kol_candidates(conn, "kol_snapshot")
    if not candidates:
        print("[kol_snapshot] kol_candidates 无数据")
        return []

    import time as _time
    ts = storage.trading_settings_get(conn)
    cooldown_min = int(ts.get("kol_token_cooldown_minutes",
                        getattr(config, "KOL_TOKEN_COOLDOWN_MINUTES", 30)) or 30)
    now = _time.time()
    fresh = []
    skipped = 0
    for c in candidates:
        token = c.get("token", "").upper()
        last = _kol_snapshot_token_last_sent.get(token, 0)
        if now - last < cooldown_min * 60:
            skipped += 1
            continue
        fresh.append(c)
    if skipped:
        print(f"[kol_snapshot] 冷却过滤: 跳过 {skipped} 个 token")
    if not fresh:
        print("[kol_snapshot] 全部候选币在冷却期内，跳过本轮")
        return []
    candidates = fresh

    try:
        from market import get_daily_klines
        for c in candidates:
            c["klines_1d"] = get_daily_klines(c.get("token", ""), 90)
    except Exception:
        pass

    system = build_snapshot_system_prompt(kol_data)
    panorama = _build_panorama_context()

    ts = storage.trading_settings_get(conn)
    provider = ts.get("kol_snapshot_llm_provider", getattr(config, "KOL_SNAPSHOT_LLM_PROVIDER", "deepseek"))

    api_keys = getattr(config, "KOL_SNAPSHOT_NVIDIA_API_KEYS", []) if provider == "nvidia" else []
    if not api_keys:
        api_keys = [""]
    batch_size = getattr(config, "KOL_CANDIDATES_PER_BATCH", 2)

    print(f"[kol_snapshot] 分析 {len(candidates)} 个候选币（{len(kol_data)} 位KOL快照）")

    # 预构建所有批次的 user prompt
    batches = []
    for i, key in enumerate(api_keys):
        start = i * batch_size
        if i == len(api_keys) - 1:
            batch = candidates[start:]
        else:
            batch = candidates[start:start + batch_size]
        if not batch:
            break
        user = panorama + "\n" + build_user_prompt(batch)
        batches.append((i, key, batch, user))

    # 并发调用 LLM
    all_analyses = []
    cooldown_tokens: set[str] = set()
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=len(batches)) as executor:
        future_map = {}
        for i, key, batch, user in batches:
            future_map[executor.submit(
                call_deepseek, system, user, 32768, provider, key,
                getattr(config, "KOL_SNAPSHOT_DEEPSEEK_API_KEY", ""),
                getattr(config, "KOL_SNAPSHOT_NVIDIA_API_KEY", "")
            )] = (i, key, batch, user, _time.time())
        for future in as_completed(future_map):
            i, key, batch, user, t0 = future_map[future]
            try:
                raw = future.result()
                _elapsed = int((_time.time() - t0) * 1000)
            except Exception as e:
                raw = None
                _elapsed = int((_time.time() - t0) * 1000)
                print(f"[kol_snapshot] key {i+1}/{len(api_keys)} 调用失败: {e}")
            model = config.NVIDIA_MODEL if provider == "nvidia" else config.DEEPSEEK_MODEL
            batch_analyses = _parse_response(raw) if raw else []
            if batch_analyses:
                for c in batch:
                    cooldown_tokens.add(c.get("token", "").upper())
            log_id = storage.kol_llm_log_insert(conn, {
                "provider": provider, "model": model,
                "candidate_count": len(batch),
                "prompt_chars": len(system) + len(user),
                "response_chars": len(raw) if raw else 0,
                "duration_ms": _elapsed,
                "success": 1 if (raw and batch_analyses) else 0,
                "error": "" if (raw and batch_analyses) else ("解析失败" if raw else "API调用失败"),
                "analyses_count": len(batch_analyses),
                "system_prompt": system, "user_prompt": user, "raw_response": raw,
            }, agent_db=config.SNAPSHOT_DB)
            for a in batch_analyses:
                all_analyses.append((a, log_id))
            print(f"[kol_snapshot] key {i+1}/{len(api_keys)}: {len(batch)}候选 -> {len(batch_analyses)}条 ({_elapsed}ms)")

    if not all_analyses:
        return []

    written = 0
    for a, log_id in all_analyses:
        token = a.get("token", "").upper()
        if not token:
            continue
        a["token"] = token
        a["llm_log_id"] = log_id
        a["strategy"] = "kol_snapshot"
        storage.kol_analysis_insert(conn, a, agent_db=config.SNAPSHOT_DB)
        written += 1

    conn.commit()
    for t in cooldown_tokens:
        _kol_snapshot_token_last_sent[t] = _time.time()
    print(f"[kol_snapshot] 写入 {written} 条分析")

    if all_analyses:
        analyses = [a for a, _ in all_analyses]
        _execute_kol_trades(conn, analyses, candidates, strategy="kol_snapshot")
        conn.commit()
        print("[kol_snapshot] 决策已入库，等待 auto_trader 执行")

    return analyses

def analyze_candidates(conn: sqlite3.Connection) -> list[dict]:
    """读候选币 → 加载KOL知识 → 调DeepSeek分析 → 写kol_analyses → 返回结果列表"""
    # 加载 KOL 知识（缓存 5 分钟，KOL 文件不会频繁变）
    kol_dir = getattr(config, "KOL_KNOWLEDGE_DIR", "")
    if not kol_dir or not os.path.isdir(kol_dir):
        print("[kol_agent] KOL_KNOWLEDGE_DIR 不存在或未配置")
        return []

    kol_data = load_kol_knowledge(kol_dir)
    if not kol_data:
        print("[kol_agent] 未找到有效的 KOL 知识文件")
        return []

    # 从 KOL 专属累积表读候选币（passed 的才入库）
    candidates = get_kol_candidates(conn, "kol_agent")
    if not candidates:
        print("[kol_agent] kol_candidates 无数据")
        return []

    # ---- token 冷却过滤：同一 token 在冷却期内不重复发给 LLM ----
    import time as _time
    ts = storage.trading_settings_get(conn)
    cooldown_min = int(ts.get("kol_token_cooldown_minutes",
                        getattr(config, "KOL_TOKEN_COOLDOWN_MINUTES", 30)) or 30)
    now = _time.time()
    fresh = []
    skipped = 0
    for c in candidates:
        token = c.get("token", "").upper()
        last = _kol_token_last_sent.get(token, 0)
        if now - last < cooldown_min * 60:
            skipped += 1
            continue
        fresh.append(c)
    if skipped:
        print(f"[kol_agent] 冷却过滤: 跳过 {skipped} 个 token（{cooldown_min}min 内已分析过）")
    if not fresh:
        print("[kol_agent] 全部候选币在冷却期内，跳过本轮")
        return []
    candidates = fresh
    # ---- 冷却过滤结束 ----

    # 为候选币拉日线数据（仅少数 token，每轮 ≤3 次 API）
    try:
        from market import get_daily_klines
        for c in candidates:
            c["klines_1d"] = get_daily_klines(c.get("token", ""), 90)
    except Exception:
        pass

    # 拼接 prompt（全景 header 共用，候选币按批次拆分）
    system = build_system_prompt(kol_data)
    panorama = _build_panorama_context()

    ts = storage.trading_settings_get(conn)
    provider = ts.get("kol_llm_provider", getattr(config, "KOL_LLM_PROVIDER", "deepseek"))

    # 多 key 分批：按 API key 数量拆分候选币
    api_keys = getattr(config, "NVIDIA_API_KEYS", []) if provider == "nvidia" else []
    if not api_keys:
        api_keys = [""]  # 单 key fallback（call_deepseek 会用 NVIDIA_API_KEY 或 DEEPSEEK_API_KEY）
    batch_size = getattr(config, "KOL_CANDIDATES_PER_BATCH", 2)

    print(f"[kol_agent] 分析 {len(candidates)} 个候选币（{len(kol_data)} 位KOL, {len(api_keys)} 个key, 每批{batch_size}个）")

    # 预构建所有批次的 user prompt
    batches = []
    for i, key in enumerate(api_keys):
        start = i * batch_size
        if i == len(api_keys) - 1:
            batch = candidates[start:]
        else:
            batch = candidates[start:start + batch_size]
        if not batch:
            break
        user = panorama + "\n" + build_user_prompt(batch)
        batches.append((i, key, batch, user))

    # 并发调用 LLM
    all_analyses = []
    cooldown_tokens: set[str] = set()
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=len(batches)) as executor:
        future_map = {}
        for i, key, batch, user in batches:
            future_map[executor.submit(
                call_deepseek, system, user, 32768, provider, key
            )] = (i, key, batch, user, _time.time())
        for future in as_completed(future_map):
            i, key, batch, user, t0 = future_map[future]
            try:
                raw = future.result()
                _elapsed = int((_time.time() - t0) * 1000)
            except Exception as e:
                raw = None
                _elapsed = int((_time.time() - t0) * 1000)
                print(f"[kol_agent] key {i+1}/{len(api_keys)} 调用失败: {e}")
            model = config.NVIDIA_MODEL if provider == "nvidia" else config.DEEPSEEK_MODEL
            batch_analyses = _parse_response(raw) if raw else []
            if batch_analyses:
                for c in batch:
                    cooldown_tokens.add(c.get("token", "").upper())
            log_id = storage.kol_llm_log_insert(conn, {
                "provider": provider,
                "model": model,
                "candidate_count": len(batch),
                "prompt_chars": len(system) + len(user),
                "response_chars": len(raw) if raw else 0,
                "duration_ms": _elapsed,
                "success": 1 if (raw and batch_analyses) else 0,
                "error": "" if (raw and batch_analyses) else ("解析失败" if raw else "API调用失败"),
                "analyses_count": len(batch_analyses),
                "system_prompt": system,
                "user_prompt": user,
                "raw_response": raw,
            }, agent_db=config.KOL_DB)
            for a in batch_analyses:
                all_analyses.append((a, log_id))
            print(f"[kol_agent]   key {i+1}/{len(api_keys)}: {len(batch)}个候选 → {len(batch_analyses)}条分析 ({_elapsed}ms)")

    if not all_analyses:
        return []

    # 写入 DB
    written = 0
    for a, log_id in all_analyses:
        token = a.get("token", "").upper()
        if not token:
            continue
        a["token"] = token
        a["llm_log_id"] = log_id
        a["strategy"] = "kol_agent"
        storage.kol_analysis_insert(conn, a, agent_db=config.KOL_DB)
        written += 1

    conn.commit()
    for t in cooldown_tokens:
        _kol_token_last_sent[t] = _time.time()
    print(f"[kol_agent] 写入 {written} 条分析")

    # 接入系统下单：confidence>=70 且 direction=long/short
    if all_analyses:
        analyses = [a for a, _ in all_analyses]
        _execute_kol_trades(conn, analyses, candidates)
        conn.commit()
        print("[kol_agent] 决策已入库，等待 auto_trader 执行")

    return analyses


def _execute_kol_trades(conn, analyses, candidates, strategy="kol_agent"):
    """对 KOL 分析结果中满足条件的，挂单接入（策略隔离）

    status=ENTER + direction=long/short + confidence>=min_conf → 挂单。
    auto_trader 统一执行挂单，避免多进程抢 DB 锁。"""
    candidate_map = {c["token"]: c for c in candidates}
    settings = storage.trading_settings_get(conn)
    prefix = "kol_snapshot" if strategy == "kol_snapshot" else "kol_agent"
    min_conf = int(settings.get(f"{prefix}_min_confidence", 70) or 70)
    inserted = 0
    for a in analyses:
        direction = a.get("direction", "")
        confidence = int(a.get("confidence", 0) or 0)
        if direction not in ("long", "short") or confidence < min_conf:
            continue
        llm_status = a.get("status", "")
        if llm_status != "ENTER":
            continue
        token = a.get("token", "").upper()
        if not token:
            continue
        original = candidate_map.get(token)
        if not original:
            print(f"[{strategy}] 下单跳过 {token}: 无原始行情数据")
            continue

        side = "LONG" if direction == "long" else "SHORT"
        tier = a.get("position_size", "half")
        if tier not in ("full", "half"):
            tier = "half"
        action = "open_long" if side == "LONG" else "open_short"
        reason = a.get("summary", "") or f"KOL {direction} conf={confidence}"

        # 挂单：必须 LLM 给出有效 entry 价格
        price_levels = a.get("price_levels", {}) or {}
        entry_price = price_levels.get("entry")
        try:
            entry_price_f = float(entry_price) if entry_price is not None else 0
        except (ValueError, TypeError):
            entry_price_f = 0
        if entry_price_f <= 0:
            print(f"[{strategy}] 挂单跳过 {token}: ENTER 但无有效 entry 价格")
            continue

        conn.execute(
            "INSERT INTO pending_decisions "
            "(action, token, tier, entry_price, reason, status, source, social_score, mentions) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (action, token, tier, entry_price_f, reason, strategy,
             original.get("social_score", 0), original.get("mentions", 0)),
        )
        print(f"[{strategy}] 决策入库 {token} {side} @ {entry_price_f} conf={confidence} tier={tier}")
        inserted += 1
    return inserted


# ------------------------------------------------------------
# 5. 缓存（避免频繁读文件）
# ------------------------------------------------------------

_cache: dict = {}
_cache_time: float = 0.0
_CACHE_TTL = 300  # 5 分钟


def load_kol_knowledge(kol_dir: str) -> list[dict]:
    global _cache, _cache_time
    now = time.time()
    if _cache and (now - _cache_time) < _CACHE_TTL:
        return _cache
    data = _load_kol_files(kol_dir)
    _cache = data
    _cache_time = now
    return data
