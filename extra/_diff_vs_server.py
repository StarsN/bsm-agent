"""对比本地优化版与服务器版的差异（逐文件、按函数归类出改了什么）。

用法：先把服务器文件下到 _srv/，再跑本脚本。
"""
import difflib
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

FILES = ["alphafox_trade.py", "alphafox_live.py", "supertrend_strategy.py",
         "supertrend_live.py", "web.py", "storage.py", "trade_logic.py",
         "tg_notify.py", "dashboard.py", "exchange.py"]


def enclosing_func(lines, idx):
    """idx 行（0基）往上找最近的 def/class"""
    for i in range(idx, -1, -1):
        m = re.match(r"^(def |class |async def )(\w+)", lines[i])
        if m:
            return m.group(2)
    return "(模块级)"


for f in FILES:
    a = f"_srv/{f}"
    if not os.path.exists(a):
        print(f"=== {f}: 未下载，跳过 ===")
        continue
    srv = open(a, encoding="utf-8").read().splitlines()
    loc = open(f, encoding="utf-8").read().splitlines()
    sm = difflib.SequenceMatcher(None, srv, loc, autojunk=False)
    hunks = [h for h in sm.get_opcodes() if h[0] != "equal"]
    add = sum(h[2] - h[1] for h in hunks)
    rm = sum(h[4] - h[3] for h in hunks)
    print(f"\n{'='*78}\n=== {f}   服务器 {len(srv)} 行 → 本地 {len(loc)} 行 "
          f"（改 {len(hunks)} 处，+{add} / -{rm}）\n{'='*78}")
    if not hunks:
        print("  完全一致 ✓")
        continue
    for tag, i1, i2, j1, j2 in hunks:
        fn = enclosing_func(loc, j1)
        note = f"  服务器 {i1+1}-{i2} → 本地 {j1+1}-{j2}"
        print(f"\n  [{fn}] {note}")
        # 打印改动行（新增的为主，截断）
        added = [l.strip() for l in loc[j1:j2]]
        removed = [l.strip() for l in srv[i1:i2]]
        for l in removed[:3]:
            print(f"      - {l[:110]}")
        if len(removed) > 3:
            print(f"      - ...(共 {len(removed)} 行)")
        for l in added[:5]:
            print(f"      + {l[:110]}")
        if len(added) > 5:
            print(f"      + ...(共 {len(added)} 行)")
