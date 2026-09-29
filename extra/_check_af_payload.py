"""验证跟单页面载荷（_af_build_payload 的币种聚合）对脏数据健壮。

web.py 不能 import（模块级会起探测线程/连外网）→ 按 `_check_exempt_recorder.py`
的既有套路：从源码抽取真实代码块执行。

覆盖：上游 dna 的历史记录里 pnl 是字符串/None、openTime 类型混杂、元素不是对象时，
聚合不能抛异常——抛了页面版本号永不递增 = 页面静默定格（无任何报错）。
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)

sys.path.insert(0, os.path.join(root, "extra"))
from _mut_src import read_module_source       # 变异注入的源码优先（否则读磁盘原文）

src = read_module_source("web")
fails = 0


def check(name, cond, extra=""):
    global fails
    ok = bool(cond)
    print(("[PASS] " if ok else "[FAIL] ") + name + ("" if ok else f"  {extra}"))
    if not ok:
        fails += 1


# ---- 1. 抽取真实的 _af_num ----
m = re.search(r"\ndef _af_num\(v, default: float = 0\.0\) -> float:.*?\n\n\n", src, re.S)
if not m:
    print("[FAIL] 未能从 web.py 定位 _af_num")
    sys.exit(1)
ns = {}
exec(compile(m.group(0), "web.py:_af_num", "exec"), ns)
_af_num = ns["_af_num"]
check("字符串数字可转", _af_num("12.5") == 12.5, str(_af_num("12.5")))
check("脏字符串→默认 0", _af_num("N/A") == 0.0, str(_af_num("N/A")))
check("None→默认 0", _af_num(None) == 0.0, str(_af_num(None)))
check("dict→默认 0", _af_num({"a": 1}) == 0.0, str(_af_num({"a": 1})))

# ---- 2. 抽取真实的聚合代码块 ----
_start_mark = "\n    symbol_stats = []\n"
_end_mark = '        symbol_stats.sort(key=lambda x: -x["pnl"])'
_i = src.find(_start_mark)
_j = src.find(_end_mark, _i if _i >= 0 else 0)
if _i < 0 or _j < 0:
    print("[FAIL] 未能从 web.py 定位 symbol_stats 聚合块")
    sys.exit(1)
block = src[_i + 1:_j + len(_end_mark)]
block = "\n".join((l[4:] if l.startswith("    ") else l) for l in block.splitlines())

slim_dna = {"cycleItems": [
    {"symbol": "BTC", "pnl": 10.0, "openTime": 100},
    {"symbol": "BTC", "pnl": "5.5", "openTime": "200"},     # 字符串 pnl/时间
    {"symbol": "BTC", "pnl": None, "openTime": None},
    {"symbol": "BTC", "pnl": -3.0},                          # 缺 openTime
    "junk-not-dict",                                         # 元素不是对象
    {"symbol": "ETH", "pnl": -8.0, "openTime": 50},
]}
stats = {"slim_dna": slim_dna, "_af_num": _af_num}
try:
    exec(compile(block, "web.py:symbol_stats", "exec"), stats)
    ok, err = True, ""
except Exception as e:      # noqa: BLE001
    ok, err = False, f"{type(e).__name__}: {e}"
check("脏数据下聚合不抛异常（页面不会定格）", ok, err)

if ok:
    out = {s["symbol"]: s for s in stats["symbol_stats"]}
    check("BTC 聚合 = 10 + 5.5 + 0 - 3 = 12.5",
          abs(out["BTC"]["pnl"] - 12.5) < 1e-9, str(out["BTC"]["pnl"]))
    check("计数含非对象元素之外的全部条目（4 条）",
          out["BTC"]["count"] == 4, str(out["BTC"]["count"]))
    # 10 胜 / "5.5" 也计入胜（证明字符串被正确转成数字）/ -3 负 → 2/3 = 66.7%
    check("胜率正确（字符串 pnl 被计入：2 胜 1 负 = 66.7%）",
          abs(out["BTC"]["win_rate"] - 66.7) < 0.05, str(out["BTC"]["win_rate"]))
    check("时间排序未因类型混杂抛错（字符串 200 排最前）",
          out["BTC"]["items"][0].get("openTime") == "200",
          str([i.get("openTime") for i in out["BTC"]["items"]]))

print(f"\n结果: {'全部通过' if not fails else str(fails) + ' 项失败'}")
sys.exit(1 if fails else 0)
