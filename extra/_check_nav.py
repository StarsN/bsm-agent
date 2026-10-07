"""导航结构检查：三大 TAG（市场/策略/实盘）+ 系统设置，且**每个页面的 key 都在导航里**。

为什么要单列一个检查：导航由 `_nav_html()` 一处生成、14+ 个页面共用 ——
改了列表漏掉某个页面（历史教训：`/kol` 一度没有任何入口），页面照样能打开、只是点不到。
所以钉两条不变量：
  ① web.py 里所有 `_nav_html("xxx")` 调用用到的 key，都必须能在导航（TAG/下拉/尾部）里找到
  ② 两个**钻取页**（`/kol?strategy=`、`/onchain/token/{token}`）必须仍从父页面链接着

本地没有 fastapi，不能 import web → 抽源码 exec（同 _check_pages_js 的做法）。
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "extra"))

from _mut_src import read_module_source  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


src = read_module_source("web")

# —— 抽出导航那一段源码（从"统一导航栏"注释到下一个大节）并 exec ——
m = re.search(r"# 统一导航栏.*?(?=\n# =+\n# 轻量内存缓存)", src, re.S)
assert m, "未找到导航代码段"
ns: dict = {}
exec("from __future__ import annotations\n" + m.group(0), ns)
nav_html = ns["_nav_html"]
NAV_DROPDOWNS = ns["_NAV_DROPDOWNS"]
NAV_TAIL = ns["_NAV_TAIL"]

print("== 1. 结构：3 个 TAG（市场/策略/实盘）+ 尾部系统设置 ==")
titles = [t for t, _l, _e in NAV_DROPDOWNS]
check("TAG 顺序 = 市场 / 策略 / 实盘", titles == ["🌐 市场", "🎯 策略", "💹 实盘"], str(titles))
check("没有顶级直达（高频入口并入 TAG）", ns["_NAV_ITEMS"] == [])
check("尾部是系统设置", NAV_TAIL[0] == "/settings")
check("TAG 标签链接：市场→/、策略→/strategies、实盘不设链接",
      NAV_DROPDOWNS[0][1] == "/" and NAV_DROPDOWNS[1][1] == "/strategies"
      and NAV_DROPDOWNS[2][1] is None)

print("\n== 2. 覆盖：所有页面的 active key 都能在导航里找到 ==")
used = set(re.findall(r'_nav_html\("([^"]+)"\)', src))
nav_keys = set()
for _t, _l, entries in NAV_DROPDOWNS:
    nav_keys.update(k for _h, _lb, k in entries if k)
nav_keys.add(NAV_TAIL[2])
missing = sorted(used - nav_keys)
check(f"{len(used)} 个页面 key 全部在导航里", not missing, f"找不到入口的 key: {missing}")
# 反向：导航里有 key、但压根没有页面用它 → 多半是改名后的残留
stale = sorted(nav_keys - used)
check("导航里的 key 都有对应页面（无残留项）", not stale, f"无页面对应的导航 key: {stale}")

print("\n== 3. 钻取页仍从父页面可达（不占导航，但不能成孤儿）==")
check("KOL 两个监控页都有「查看K线分析 →」链到 /kol?strategy=",
      src.count("/kol?strategy=") >= 2, str(src.count("/kol?strategy=")))
check("链上监控页有链到 /onchain/token/", "/onchain/token/" in src)

print("\n== 4. 渲染：分组小标题不可点、TAG 高亮跟着页面走 ==")
h_strat = nav_html("strategies")
h_home = nav_html("home")
h_kol = nav_html("agent-kol")
check("策略下拉有分组小标题（渲染成 div，不是 <a>）",
      "总览</div>" in h_strat and "Agent 面板</div>" in h_strat
      and '<a href="/strategies"' in h_strat)
check("active=home → 市场 TAG 高亮",
      re.search(r'<a href="/" class="active">🌐 市场', h_home) is not None, h_home[:200])
check("active=agent-kol → 策略 TAG 高亮（最深的 Agent 页也能亮到 TAG）",
      re.search(r'<a href="/strategies" class="active">🎯 策略', h_kol) is not None)
check("实盘 TAG 渲染成纯悬停（href=#）",
      '<a href="#" >💹 实盘' in nav_html("live-onchain")
      or '<a href="#"' in nav_html("live-onchain"))
check("下拉有滚动护栏（矮屏上 20 行的策略菜单不被裁掉）",
      h_strat.count("max-height:70vh;overflow-y:auto") == len(NAV_DROPDOWNS),
      str(h_strat.count("max-height:70vh;overflow-y:auto")))
check("导航 HTML 标签配平（div/a 开=闭）",
      h_strat.count("<div") == h_strat.count("</div>")
      and h_strat.count("<a ") == h_strat.count("</a>"),
      f"div {h_strat.count('<div')}/{h_strat.count('</div>')} "
      f"a {h_strat.count('<a ')}/{h_strat.count('</a>')}")

print("\n== 5. 每个用 {NAV} 的页面都带导航样式（否则导航会裸奔）==")
# 历史教训：事件页最初漏了 .nav-bar/.nav-dropdown 这几条 → 导航没样式。
# 页面里引用 {EV_CSS} 这类占位符时，样式在对应常量（_EV_CSS）里 → 解析后一起查。
_consts = {n: b for n, b in re.findall(r'^([A-Za-z_0-9]+) = """(.*?)"""', src, re.S | re.M)}
_need_css = (".nav-bar", ".nav-dropdown", ".nav-dropdown-content",
             ".nav-dropdown-content a")
_bad_css = []
for _n, _b in _consts.items():
    if "{NAV}" not in _b:
        continue
    _eff = _b + "".join(_consts.get(_ph, "") + _consts.get("_" + _ph, "")
                        for _ph in re.findall(r"\{([A-Za-z_0-9]+)\}", _b))
    _miss = [c for c in _need_css if c not in _eff]
    if _miss:
        _bad_css.append((_n, _miss))
check(f"{sum(1 for b in _consts.values() if '{NAV}' in b)} 个页面的导航样式齐备",
      not _bad_css, str(_bad_css))

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
