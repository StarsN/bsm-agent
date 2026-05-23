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


# ------------------------------------------------------------
# 1. 加载 KOL 知识文件
# ------------------------------------------------------------

def _load_kol_files(kol_dir: str) -> list[dict]:
    """扫描 kol_dir 下的 Theory/ TradeFramework/ DataSource/ 子目录。

    命名约定：{KOL名称}__Theory.md, {KOL名称}__TradeFramework.md, {KOL名称}__DataSource.md
    返回 [{name, theory, framework, datasource}, ...]
    """
    subdirs = {
        "Theory": "theory",
        "TradeFramework": "framework",
        "DataSource": "datasource",
    }

    kol_map: dict[str, dict] = {}

    for dirname, key in subdirs.items():
        d = Path(kol_dir) / dirname
        if not d.is_dir():
            continue
        for fpath in sorted(d.glob("*.md")):
            name = fpath.stem  # e.g. "BTC_Alert__Theory"
            kol_name = name.split("__")[0].strip()
            if not kol_name:
                continue
            text = fpath.read_text(encoding="utf-8").strip()
            if not text:
                continue
            kol_map.setdefault(kol_name, {"name": kol_name})[key] = text

    # theory + framework 必备，datasource 可选
    return [v for v in kol_map.values() if v.get("theory") and v.get("framework")]


# ------------------------------------------------------------
# 2. 拼接 Prompt
# ------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = """\
你是一位加密货币合约交易分析师，经过多位专业交易员（KOL）交易体系的深度训练。

以下是 {kol_count} 位KOL的交易理论体系和交易框架。请仔细阅读并内化这些方法论，
然后用这些框架分析候选币的盘面结构、交易机会和方向。

{kol_sections}

## 输出格式

返回严格JSON，不要额外文字：
{{
  "analyses": [
    {{
      "token": "币种简称（如 FLUID、ORDI）",
      "trend": "偏多 / 偏空 / 震荡",
      "price_levels": {{
        "current": 数字或null, "support": 数字或null, "resistance": 数字或null,
        "entry": 数字或null, "stop_loss": 数字或null, "take_profit": 数字或null
      }},
      "position_analysis": "当前价格位置、支撑阻力、多空力量分析",
      "timing": "什么条件下入场，什么条件下等待",
      "risk_control": {{
        "stop_loss_rule": "止损位和逻辑",
        "tp1": "第一止盈目标",
        "tp2": "第二止盈目标"
      }},
      "direction": "long / short / none",
      "confidence": 75,
      "reason": "综合KOL框架的决策依据，1-3句话"
    }}
  ]
}}

## 字段说明
- token: 币种简称，大写
- trend: 当前盘面整体方向倾向，"偏多" / "偏空" / "震荡"
- price_levels: 关键价格位，无依据填 null
  - current: 当前标记价
  - support: 最近有效支撑位
  - resistance: 最近有效阻力位
  - entry: 建议入场价（无交易机会时填 null）
  - stop_loss: 建议止损价（无交易机会时填 null）
  - take_profit: 建议止盈目标（无交易机会时填 null）
- position_analysis: 对当前价格位置、支撑阻力、多空力量的综合分析（100-200字）
- timing: 什么条件下入场、什么条件下等待的具体说明（50-100字）
- risk_control: 风控方案（direction=none时可填占位文字"未入场"）
  - stop_loss_rule: 止损位设置逻辑
  - tp1: 第一止盈目标及依据
  - tp2: 第二止盈目标及依据
- direction: 交易方向，long(做多) / short(做空) / none(无机会)
- confidence: 对当前判断的信心评分，0-100整数。≥80=高把握，50-79=中等，<50=低把握
- reason: 综合KOL框架的决策依据，1-3句话

## 规则
- price_levels 中无明确依据的字段填 null
- direction 为 none 时 price_levels 的 entry/stop_loss/take_profit 填 null
- direction 为 none 时 risk_control 的三项填占位文字即可
- 始终基于 KOL 的交易框架进行分析，用他们的视角看盘面
"""


def _build_kol_section(kol: dict) -> str:
    """把单个 KOL 的知识拼成一段 markdown"""
    name = kol["name"]
    theory = kol["theory"]
    framework = kol["framework"]
    datasource = kol.get("datasource", "")
    title = f"## {name} 的交易体系\n"
    theory_clean = re.sub(r"^# .+\n", "", theory, count=1).strip()
    framework_clean = re.sub(r"^# .+\n", "", framework, count=1).strip()
    parts = [f"{title}\n### 核心理论\n{theory_clean}\n\n### 交易框架\n{framework_clean}"]
    if datasource:
        ds_clean = re.sub(r"^# .+\n", "", datasource, count=1).strip()
        parts.append(f"\n### 数据源与工具\n{ds_clean}")
    parts.append("")  # trailing newline
    return "\n".join(parts)


def build_system_prompt(kol_data: list[dict]) -> str:
    kol_sections = "\n---\n".join(_build_kol_section(k) for k in kol_data)
    return _SYSTEM_PROMPT_TEMPLATE.format(
        kol_count=len(kol_data),
        kol_sections=kol_sections,
    )


def build_user_prompt(candidates: list[dict]) -> str:
    """把候选币数据格式化为 user prompt"""
    lines = ["## 候选币市场数据\n"]
    for c in candidates:
        lines.append(f"### {c['token']}")
        fields = [
            ("标记价", c.get("price")),
            ("15m涨跌", f"{c.get('15m')}%"),
            ("1h涨跌", f"{c.get('1h')}%"),
            ("4h涨跌", f"{c.get('4h')}%"),
            ("24h涨跌", f"{c.get('24h')}%"),
            ("OI 15m变化", f"{c.get('oi_15m')}%"),
            ("OI 1h变化", f"{c.get('oi_1h')}%"),
            ("OI 4h变化", f"{c.get('oi_4h')}%"),
            ("资金费率", f"{c.get('funding')}%/8h"),
            ("主动买卖比", c.get("taker")),
            ("主动买入占比", f"{c.get('taker_pct')}%"),
            ("Taker趋势", f"{c.get('taker_trend')}%"),
            ("盘口价差", f"{c.get('spread')}%"),
            ("买盘深度(USD)", c.get("depth_bid")),
            ("卖盘深度(USD)", c.get("depth_ask")),
            ("盘口失衡度", f"{c.get('imbalance')}%"),
            ("散户多空比", c.get("lsr")),
            ("大户多空比", c.get("top_lsr")),
            ("24h成交额", c.get("vol_24h")),
            ("OI(USD)", c.get("oi_usd")),
            ("上币时长", c.get("age")),
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

def call_deepseek(system: str, user: str, max_tokens: int = 4096) -> Optional[str]:
    """返回 LLM 回复文本，失败返回 None"""
    provider = getattr(config, "KOL_LLM_PROVIDER", "deepseek")
    if provider == "nvidia":
        api_key = config.NVIDIA_API_KEY
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

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"]["content"]
            return content
    except Exception as e:
        print(f"[kol_agent] API 调用失败: {e}")
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

    # 拼接 prompt
    system = build_system_prompt(kol_data)
    user = build_user_prompt(candidates)

    print(f"[kol_agent] 分析 {len(candidates)} 个候选币（{len(kol_data)} 位KOL）")

    # 调 API
    import time as _time
    _t0 = _time.time()
    provider = getattr(config, "KOL_LLM_PROVIDER", "deepseek")
    model = config.NVIDIA_MODEL if provider == "nvidia" else config.DEEPSEEK_MODEL
    raw = call_deepseek(system, user, max_tokens=32768)
    _elapsed = int((_time.time() - _t0) * 1000)
    analyses = _parse_response(raw) if raw else []

    # 写 LLM 调用日志
    storage.kol_llm_log_insert(conn, {
        "provider": provider,
        "model": model,
        "candidate_count": len(candidates),
        "prompt_chars": len(system) + len(user),
        "response_chars": len(raw) if raw else 0,
        "duration_ms": _elapsed,
        "success": 1 if (raw and analyses) else 0,
        "error": "" if raw else "API调用失败" if not analyses else "解析失败",
        "analyses_count": len(analyses),
    })

    if not raw:
        return []

    if not analyses:
        return []

    # 写入 DB
    written = 0
    for a in analyses:
        token = a.get("token", "").upper()
        if not token:
            continue
        a["raw_response"] = raw
        a["system_prompt"] = system
        a["user_prompt"] = user
        storage.kol_analysis_insert(conn, a)
        written += 1

    conn.commit()
    print(f"[kol_agent] 写入 {written} 条分析")

    # 接入系统下单：confidence>=70 且 direction=long
    if analyses:
        opened = _execute_kol_trades(conn, analyses, candidates)
        if opened:
            conn.commit()
            print(f"[kol_agent] 下单 {opened} 笔")

    return analyses


def _execute_kol_trades(conn, analyses, candidates):
    """对 KOL 分析结果中满足条件的，接入系统下单（策略隔离: kol_agent）"""
    from trade_logic import open_paper_position
    candidate_map = {c["token"]: c for c in candidates}
    settings = storage.trading_settings_get(conn)
    min_conf = getattr(config, "KOL_AGENT_MIN_CONFIDENCE", 70)
    opened = 0
    for a in analyses:
        direction = a.get("direction", "")
        confidence = int(a.get("confidence", 0) or 0)
        if direction not in ("long", "short") or confidence < min_conf:
            continue
        token = a.get("token", "").upper()
        if not token:
            continue
        original = candidate_map.get(token)
        if not original:
            print(f"[kol_agent] 下单跳过 {token}: 无原始行情数据")
            continue
        side = "LONG" if direction == "long" else "SHORT"
        candidate = {
            "token": token,
            "side": side,
            "passed": True,
            "has_active_position": False,
            "tier": "full" if confidence >= 80 else "half",
            "price": original.get("price"),
            "signal_key": original.get("signal_key", ""),
            "analysis_score": confidence,
            "pass_count": confidence,
        }
        action = "open_long" if side == "LONG" else "open_short"
        reason = a.get("reason", "") or f"KOL {direction} conf={confidence}"
        ok = open_paper_position(conn, candidate, settings, strategy="kol_agent", side=side)
        # 写入 pending_decisions，决策时间线可查
        status = "consumed" if ok else "rejected"
        reject_reason = "" if ok else "系统拒绝"
        conn.execute(
            "INSERT INTO pending_decisions "
            "(action, token, tier, reason, status, source, reject_reason, social_score, mentions) "
            "VALUES (?, ?, ?, ?, ?, 'kol_agent', ?, ?, ?)",
            (action, token, candidate["tier"], reason, status,
             reject_reason, original.get("social_score", 0), original.get("mentions", 0)),
        )
        if ok:
            print(f"[kol_agent] 下单成功 {token} {side} conf={confidence} tier={candidate['tier']}")
            opened += 1
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
