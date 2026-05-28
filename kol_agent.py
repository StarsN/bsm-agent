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

# ------------------------------------------------------------
# 1. 加载 KOL 知识文件
# ------------------------------------------------------------

def _load_kol_files(kol_dir: str) -> list[dict]:
    """扫描 kol_dir/TradeFramework/ 下的 KOL 交易框架 .md 文件。
    返回 [{name, framework}, ...]
    """
    subdirs = {
        "TradeFramework": "framework",
    }

    kol_map: dict[str, dict] = {}

    for dirname, key in subdirs.items():
        d = Path(kol_dir) / dirname
        if not d.is_dir():
            continue
        for fpath in sorted(d.glob("*.md")):
            name = fpath.stem  # e.g. "BTC_Alert__TradeFramework"
            if "__" not in name:
                continue       # 跳过非 KOL 文件（如 #交易框架.md）
            kol_name = name.split("__")[0].strip()
            if not kol_name:
                continue
            text = fpath.read_text(encoding="utf-8").strip()
            if not text:
                continue
            kol_map.setdefault(kol_name, {"name": kol_name})[key] = text

    return [v for v in kol_map.values() if v.get("framework")]


# ------------------------------------------------------------
# 2. 拼接 Prompt
# ------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = """\
你是一位加密货币合约交易分析师，经过多位专业交易员（KOL）交易体系的深度训练。

以下是 {kol_count} 位KOL的交易框架。请仔细阅读并内化这些方法论，
然后用这些框架分析候选币的盘面结构、交易机会和方向。

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
      "confidence": 75
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
  - entry: 建议入场价（无交易机会时填 null）
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

## 规则
- price_levels 中无明确依据的字段填 null
- direction 为 none 时 price_levels 的 entry/stop_loss/take_profit 填 null
- evidence_tags 必须引用用户 prompt 中的具体数据，不要笼统
- 始终基于 KOL 的交易框架进行分析，用他们的视角看盘面
- **全景优先**：用户 prompt 中"全景指引"的规则和方向偏好，优先于 KOL 框架信号
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


def _fmt_klines(klines):
    """格式化 K 线: O/H/L/C/Vol/额, ..."""
    if not klines:
        return None
    parts = []
    for k in klines:
        if len(k) >= 6:
            parts.append(f"{k[0]:.4f}/{k[1]:.4f}/{k[2]:.4f}/{k[3]:.4f}/{k[4]:.0f}/{k[5]:.0f}")
    return ", ".join(parts) if parts else None


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
            ("OI 15m变化", _fmt_pct(c.get("oi_15m"))),
            ("OI 1h变化", _fmt_pct(c.get("oi_1h"))),
            ("OI 4h变化", _fmt_pct(c.get("oi_4h"))),
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
            # 市值 + 乖离 + 聪明钱
            ("市值", _fmt_usd(c.get("market_cap_usd")) if c.get("market_cap_usd") else None),
            ("市值排名", f"#{int(c.get('market_cap_rank'))}" if c.get("market_cap_rank") else None),
            ("MA20乖离(5h)", _fmt_pct(c.get("ma20_deviation"))),
            ("聪明钱多头占比", _fmt_pct(c.get("sm_long_ratio"))),
            ("聪明钱净头寸(USD)", _fmt_usd(c.get("sm_net_notional_usdt")) if c.get("sm_net_notional_usdt") else None),
            ("聪明钱多头胜率", _fmt_pct(c.get("sm_avg_long_win_rate"))),
            ("聪明钱关注人数", c.get("sm_traders_with_position")),
            ("OI/市值", _fmt_pct(c.get("oi_marketcap"))),
            ("1H K线(开/高/低/收/成交量/成交额)", _fmt_klines(c.get("klines_1h"))),
            ("4H K线(开/高/低/收/成交量/成交额)", _fmt_klines(c.get("klines_4h"))),
            ("日K (开/高/低/收/成交量/成交额)", _fmt_klines(c.get("klines_1d"))),
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

def call_deepseek(system: str, user: str, max_tokens: int = 4096, provider: str = "", api_key_override: str = "") -> Optional[str]:
    """返回 LLM 回复文本，失败返回 None。api_key_override 用于多 key 分批。"""
    provider = provider or getattr(config, "KOL_LLM_PROVIDER", "deepseek")
    if provider == "nvidia":
        api_key = api_key_override or config.NVIDIA_API_KEY
        model = config.NVIDIA_MODEL
        api_base = config.NVIDIA_API_BASE
    else:
        api_key = config.DEEPSEEK_API_KEY
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


def kol_is_interesting(candidate: dict) -> bool:
    """KOL 策略代币异常度筛选。OI异常 + 费率异常 + 价格乖离 ≥2 → 入选 | vol/OI > 20x → 排除"""
    snap = candidate.get("market", {}).get("snapshot", {})
    if not snap or not snap.get("mark_price"):
        return False
    vol_oi = snap.get("vol_oi_ratio")
    if vol_oi is not None and float(vol_oi) > getattr(config, "KOL_MAX_VOL_OI", 20):
        return False
    score = 0
    if abs(snap.get("oi_change_1h_pct") or 0) > getattr(config, "KOL_MIN_OI_CHANGE_1H_PCT", 3) \
       or abs(snap.get("oi_change_4h_pct") or 0) > getattr(config, "KOL_MIN_OI_CHANGE_4H_PCT", 8):
        score += 1
    if abs(snap.get("funding_rate_pct") or 0) > getattr(config, "KOL_MIN_FUNDING_ABS_PCT", 0.03):
        score += 1
    if abs(snap.get("ma20_deviation_pct") or 0) > getattr(config, "KOL_MIN_MA20_DEVIATION_PCT", 5):
        score += 1
    return score >= getattr(config, "KOL_MIN_ANOMALY_SCORE", 2)


def get_kol_candidates(conn: sqlite3.Connection) -> list[dict]:
    """从 kol_candidates 读取候选币（按时间窗口，跟主 Agent 一致）"""
    import json as _json
    ts = storage.trading_settings_get(conn)
    inter_min = int(ts.get("kol_agent_interval_minutes",
                    getattr(config, "KOL_AGENT_INTERVAL_MINUTES", 15)))
    rows = conn.execute(
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
        "**全景指引：以下宏观数据调整你的交易偏好。**",
        "- BTC 3h 微跌或下跌 → 优先考虑做空或 direction=none",
        "- BTC 3h 微涨          → 可以正常评估做多机会",
        "- BTC 3h 大涨（>0.5%） → 做多优于做空",
        "- 恐惧贪婪 < 30         → 市场恐慌中等待 BTC 企稳信号，不急于开仓",
        "- AI 市场研判           → 参考下方研判描述，理解当前市场阶段和资金情绪",
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
                lines.append(f"- 恐惧贪婪: {fng.get('value')} ({fng.get('value_classification', '')})")
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
        lines.append(f"- BTC 1h: {m1h['value']}% ({m1h['desc']}) | BTC 3h: {m3h['value']}% ({m3h['desc']})")
        lines.append(f"- z_depth: ${zd['value']}M ({zd['desc']})")
        lines.append(f"- 流动性: {liq['status']} (价差{liq['desc']})")
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
                lines.append(f"- AI 市场研判: {regime_cn} (conf {regime.get('confidence', 0)})")
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
    candidates = get_kol_candidates(conn)
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
            c["klines_1d"] = get_daily_klines(c.get("token", ""))
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

    all_analyses = []  # [(analysis_dict, log_id), ...]
    cooldown_tokens: set[str] = set()  # 收集成功分析的 token，commit 后统一更新冷却

    for i, key in enumerate(api_keys):
        start = i * batch_size
        if i == len(api_keys) - 1:
            batch = candidates[start:]  # 最后一份：全部剩余
        else:
            batch = candidates[start:start + batch_size]
        if not batch:
            break

        user = panorama + "\n" + build_user_prompt(batch)
        _t0 = _time.time()
        raw = call_deepseek(system, user, max_tokens=32768, provider=provider, api_key_override=key)
        _elapsed = int((_time.time() - _t0) * 1000)

        batch_analyses = []
        if raw:
            batch_analyses = _parse_response(raw) or []
            if batch_analyses:
                for c in batch:
                    cooldown_tokens.add(c.get("token", "").upper())

        model = config.NVIDIA_MODEL if provider == "nvidia" else config.DEEPSEEK_MODEL
        storage.kol_llm_log_insert(conn, {
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
        })
        log_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
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
        storage.kol_analysis_insert(conn, a)
        written += 1

    conn.commit()
    for t in cooldown_tokens:
        _kol_token_last_sent[t] = _time.time()
    print(f"[kol_agent] 写入 {written} 条分析")

    # 接入系统下单：confidence>=70 且 direction=long/short
    if all_analyses:
        analyses = [a for a, _ in all_analyses]
        opened = _execute_kol_trades(conn, analyses, candidates)
        if opened:
            conn.commit()
            print(f"[kol_agent] 下单 {opened} 笔")

    return analyses


def _execute_kol_trades(conn, analyses, candidates):
    """对 KOL 分析结果中满足条件的，接入系统下单（策略隔离: kol_agent）

    status=ENTER → 市价单；status=WAIT → 挂单（需 entry）；
    direction=long/short + confidence≥min_conf。
    """
    from trade_logic import open_limit_position, open_paper_position, _last_reject_reason
    candidate_map = {c["token"]: c for c in candidates}
    settings = storage.trading_settings_get(conn)
    min_conf = int(settings.get("kol_agent_min_confidence", 70) or 70)
    margin = float(settings.get("kol_agent_margin") or getattr(config, "KOL_AGENT_MARGIN", 50))
    opened = 0
    for a in analyses:
        direction = a.get("direction", "")
        confidence = int(a.get("confidence", 0) or 0)
        if direction not in ("long", "short") or confidence < min_conf:
            continue
        llm_status = a.get("status", "")
        if llm_status not in ("ENTER", "WAIT"):
            continue
        token = a.get("token", "").upper()
        if not token:
            continue
        original = candidate_map.get(token)
        if not original:
            print(f"[kol_agent] 下单跳过 {token}: 无原始行情数据")
            continue

        side = "LONG" if direction == "long" else "SHORT"
        tier = "half"
        action = "open_long" if side == "LONG" else "open_short"
        reason = a.get("summary", "") or f"KOL {direction} conf={confidence}"

        if llm_status == "WAIT":
            # 挂单：取 LLM 给的 entry 价格
            price_levels = a.get("price_levels", {}) or {}
            entry_price = price_levels.get("entry")
            try:
                entry_price_f = float(entry_price) if entry_price is not None else 0
            except (ValueError, TypeError):
                entry_price_f = 0
            if entry_price_f <= 0:
                print(f"[kol_agent] 挂单跳过 {token}: WAIT 但无有效 entry 价格")
                continue

            conn.execute(
                "INSERT INTO pending_decisions "
                "(action, token, tier, entry_price, reason, status, source, social_score, mentions) "
                "VALUES (?, ?, ?, ?, ?, 'pending', 'kol_agent', ?, ?)",
                (action, token, tier, entry_price_f, reason,
                 original.get("social_score", 0), original.get("mentions", 0)),
            )
            pd_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            result = open_limit_position(
                conn, token, side, entry_price_f,
                margin_amount=margin, tier=tier, strategy="kol_agent",
                pending_decision_id=pd_id,
            )
            ok = result.get("ok", False)
            if ok:
                print(f"[kol_agent] 挂单 {token} {side} @ {entry_price_f} conf={confidence} tier={tier}")
                opened += 1
            else:
                reject_reason = result.get("reason", "")
                print(f"[kol_agent] 挂单被拒 {token} {side}: {reject_reason}")
                conn.execute(
                    "UPDATE pending_decisions SET status = 'rejected', reject_reason = ? WHERE id = ?",
                    (reject_reason, pd_id),
                )
        else:
            # ENTER → 市价单：先写决策记录，再开仓，按结果更新
            conn.execute(
                "INSERT INTO pending_decisions "
                "(action, token, tier, reason, status, source, social_score, mentions) "
                "VALUES (?, ?, ?, ?, 'pending', 'kol_agent', ?, ?)",
                (action, token, tier, reason,
                 original.get("social_score", 0), original.get("mentions", 0)),
            )
            pd_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            candidate = {
                "token": token,
                "side": side,
                "passed": True,
                "has_active_position": False,
                "tier": tier,
                "price": original.get("price"),
                "signal_key": original.get("signal_key", ""),
                "analysis_score": confidence,
                "pass_count": confidence,
            }
            ok = open_paper_position(conn, candidate, settings, strategy="kol_agent", side=side)
            if ok:
                conn.execute(
                    "UPDATE pending_decisions SET status = 'consumed', consumed_at = datetime('now') WHERE id = ?",
                    (pd_id,),
                )
                pos_id = conn.execute(
                    "SELECT id FROM trade_positions WHERE token=? AND strategy='kol_agent' ORDER BY id DESC LIMIT 1",
                    (token,),
                ).fetchone()[0]
                conn.execute(
                    "UPDATE journal SET pending_decision_id = ? WHERE order_id = ?",
                    (pd_id, pos_id),
                )
                print(f"[kol_agent] 市价开仓 {token} {side} conf={confidence} tier={tier}")
                opened += 1
            else:
                reject_reason = _last_reject_reason.get(token, "系统拒绝")
                conn.execute(
                    "UPDATE pending_decisions SET status = 'rejected', reject_reason = ? WHERE id = ?",
                    (reject_reason, pd_id),
                )
    return opened


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
