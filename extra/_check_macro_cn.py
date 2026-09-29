"""验证市场全景「宏观事件窗口」的事件名中文化（离线，不连网）。

关键不变式：
1. 常见美区事件名 → 中文（按 OKX 真实命名核对）
2. 表里没有的 → 原样英文（宁可露英文也不要显示错的中文）
3. 「核心/观察」判定必须用**英文原名**——翻译后判定会全部失效
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)

import dashboard as ds

fails = 0


def check(name, cond, extra=""):
    global fails
    ok = bool(cond)
    print(("[PASS] " if ok else "[FAIL] ") + name + ("" if ok else f"  {extra}"))
    if not ok:
        fails += 1


def _has_cjk(s: str) -> bool:
    return any("一" <= c <= "鿿" for c in (s or ""))


# ---- 1. OKX 真实事件名（2026-09-11 实拉核对）→ 中文 ----
REAL_NAMES = [
    "Core Inflation Rate YoY", "Core Inflation Rate MoM",
    "Inflation Rate YoY", "Inflation Rate MoM",
    "Fed Interest Rate Decision", "FOMC Economic Projections",
    "FOMC Minutes", "Fed Press Conference",
    "Non Farm Payrolls", "Unemployment Rate",
    "Initial Jobless Claims", "JOLTs Job Openings",
    "Retail Sales MoM", "Michigan Consumer Sentiment Prel",
    "Michigan Consumer Sentiment Final", "Building Permits Prel",
    "Housing Starts", "Existing Home Sales", "New Home Sales",
    "Core PCE Price Index MoM", "PCE Price Index YoY",
    "ISM Manufacturing PMI", "ISM Services PMI",
    "CB Consumer Confidence", "ADP Employment Change",
    "GDP Growth Rate QoQ Final", "PPI MoM", "Durable Goods Orders MoM",
    "EIA Crude Oil Stocks Change", "S&P/Case-Shiller Home Price YoY",
]
bad = [n for n in REAL_NAMES if not _has_cjk(ds._event_cn(n))]
check(f"{len(REAL_NAMES)} 个真实事件名都译出中文", not bad, str(bad))

# ---- 2. 全表自查：不能有空值/纯英文值 ----
bad_vals = [k for k, v in ds._MACRO_EVENT_CN.items()
            if not _has_cjk(v) or v.startswith("MUT")]
check("词表内所有译名都是中文（无空值/漏译）", not bad_vals, str(bad_vals[:5]))

# ---- 3. 兜底：未知事件名原样返回英文 ----
UNKNOWN = "Some Brand New Event 2050"
check("未知事件名原样返回英文（不显示空白）",
      ds._event_cn(UNKNOWN) == UNKNOWN, ds._event_cn(UNKNOWN))
check("空值不炸", ds._event_cn("") == "" and ds._event_cn(None) is None)

# ---- 4. 规则兜底（发言人姓名无法穷举）----
check("Fed xxx Speech 规则命中",
      ds._event_cn("Fed Barkin Speech") == "美联储Barkin讲话",
      ds._event_cn("Fed Barkin Speech"))

# ---- 5. 关键不变式：核心判定必须用英文原名 ----
check("核心判定用英文原名（FOMC 命中）",
      ds._is_core_event("Fed Interest Rate Decision") is True)
check("核心判定不吃中文译名（防止先翻译后判定）",
      ds._is_core_event(ds._event_cn("Fed Interest Rate Decision")) is False,
      ds._event_cn("Fed Interest Rate Decision"))

# ---- 6. 地区中文化 ----
check("United States → 美国", ds._region_cn("United States") == "美国",
      ds._region_cn("United States"))
check("未收录地区原样返回", ds._region_cn("Mars") == "Mars")

# ---- 7. 端到端：注入假 OKX 返回，跑真 get_macro_events ----
FAKE_ROWS = [
    {"date": "1789581600000", "event": "Fed Interest Rate Decision",
     "category": "Interest Rate", "forecast": "4%", "previous": "3.75%",
     "region": "United States"},
    {"date": "1789648200000", "event": "Building Permits Prel",
     "category": "Building Permits", "forecast": "", "previous": "1.433M",
     "region": "United States"},
]
_real_okx = ds._okx
ds._okx = lambda args, live=False: FAKE_ROWS
try:
    r = ds.get_macro_events()
finally:
    ds._okx = _real_okx
evs = r.get("recent_events", [])
check("端到端：事件名已中文化", evs and evs[0]["name"] == "美联储利率决议",
      str([e.get("name") for e in evs]))
check("端到端：英文原名仍保留在 name_en",
      evs and evs[0].get("name_en") == "Fed Interest Rate Decision",
      str([e.get("name_en") for e in evs]))
check("端到端：地区已中文化", evs and evs[0]["region"] == "美国",
      str([e.get("region") for e in evs]))
check("端到端：核心标记仍按英文原名给出（FOMC=核心）",
      evs and evs[0]["tag"] == "核心", str([e.get("tag") for e in evs]))

print(f"\n结果: {'全部通过' if not fails else str(fails) + ' 项失败'}")
sys.exit(1 if fails else 0)
