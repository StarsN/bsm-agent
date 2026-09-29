"""验证 web.py 里"实盘开关 0→1 记录豁免清单"的真实代码块（从源码抽取原文执行）。

覆盖用户语义：刚开启跟单实盘时，之前的纸盘存量仓要进豁免清单（不进实盘）。
"""
import io
import json
import os
import re
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)

import config

_TMP = tempfile.mkdtemp(prefix="exempt_")
config.DB_PATH = os.path.join(_TMP, "main.db")
config.SUPERTREND_DB = os.path.join(_TMP, "supertrend.db")
config.LIVE_DB_PATH = os.path.join(_TMP, "live.db")
config.ALPHAFOX_DB = os.path.join(_TMP, "alphafox.db")

import storage

with storage.get_conn() as conn:
    conn.executescript(storage.SCHEMA)
storage.init_onchain_live_db()
for token, side, status in [("BTC", "LONG", "OPEN"), ("ZEC", "SHORT", "OPEN"),
                            ("ETH", "LONG", "CLOSED")]:
    with storage.get_conn() as conn:
        conn.execute(
            "INSERT INTO trade_positions (token, symbol, side, status, mode, "
            "margin_amount, leverage, notional, quantity, entry_price, "
            "current_price, strategy, closed_qty) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (token, f"{token}USDT", side, status, "paper", 100.0, 10.0, 1000.0,
             1.0, 100.0, 100.0, "alphafox", 0.0))
# ETH 是已平仓的（不入清单）；另外造一笔**已有实盘镜像**的活跃仓 → 也不该进豁免
# （豁免=实盘侧完全不处理 → 有镜像的仓进豁免会让"开关关一下再开"后加仓不再跟）
with storage.get_conn() as conn:
    cur = conn.execute(
        "INSERT INTO trade_positions (token, symbol, side, status, mode, "
        "margin_amount, leverage, notional, quantity, entry_price, "
        "current_price, strategy, closed_qty) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ADA", "ADAUSDT", "LONG", "OPEN", "paper", 100.0, 10.0, 1000.0,
         1.0, 100.0, 100.0, "alphafox", 0.0))
    _pid_ada = cur.lastrowid

with storage.get_conn(config.LIVE_DB_PATH) as _lc:
    storage.live_position_insert(_lc, {
        "paper_position_id": _pid_ada, "token": "ADA", "symbol": "ADAUSDT",
        "side": "LONG", "strategy": "alphafox", "entry_price": 100.0,
        "quantity": 1.0, "leverage": 10, "margin_usd": 10.0})

# 从 web.py 抽取真实代码块（注释起 → prev_live = live_enabled 止）
sys.path.insert(0, os.path.join(root, "extra"))
from _mut_src import read_module_source       # 变异注入的源码优先（否则读磁盘原文）

web_src = read_module_source("web")
m = re.search(
    r"(\n        if prev_live is not None and live_enabled and not prev_live:"
    r".*?\n        prev_live = live_enabled)", web_src, re.S)
if not m:
    print("[FAIL] 未能从 web.py 定位豁免记录代码块")
    sys.exit(1)
block = m.group(1)
import textwrap
block = textwrap.dedent(block)
print("抽取到的代码块行数:", block.count("\n"))

prev_live = False
live_enabled = True
exec(compile(block, "web.py:exempt_block", "exec"), globals())

with storage.get_conn() as conn:
    row = conn.execute("SELECT value FROM trading_settings "
                       "WHERE key='alphafox_live_exempt_paper_ids'").fetchone()
val = json.loads(row["value"]) if row and row["value"] else []
ok = (len(val) == 2
      and all(v.startswith("BTC|LONG#") or v.startswith("ZEC|SHORT#") for v in val)
      and not any(v.startswith("ADA|LONG#") for v in val))
print(("[PASS] " if ok else "[FAIL] ")
      + f"豁免清单 = {val}（期望 BTC|LONG#id / ZEC|SHORT#id；"
        f"已平仓的不入；**已有实盘镜像的不入**）")

# 关闭→再开启：清单应被刷新（此前的仓仍豁免）
prev_live = True
live_enabled = False
exec(compile(block, "web.py:exempt_block", "exec"), globals())
prev_live = False
live_enabled = True
exec(compile(block, "web.py:exempt_block", "exec"), globals())
with storage.get_conn() as conn:
    row = conn.execute("SELECT value FROM trading_settings "
                       "WHERE key='alphafox_live_exempt_paper_ids'").fetchone()
val2 = json.loads(row["value"]) if row and row["value"] else []
ok2 = (len(val2) == 2
       and all(v.startswith("BTC|LONG#") or v.startswith("ZEC|SHORT#") for v in val2)
       and not any(v.startswith("ADA|LONG#") for v in val2))
print(("[PASS] " if ok2 else "[FAIL] ") + f"关→开后再记录 = {val2}")

# 探测线程必须把**单笔上限**（设置页 alphafox_live_max_notional）读出来并传给镜像层。
# 同类坑（2026-09-14 的 rebase_lots 死代码）：这类参数只写在 except 兜底分支里 =
# 生产恒拿不到 = 功能静默失效，所以这里同时钉住"读哪来的"和"传下去了"
ok6 = bool(re.search(
    r'live_notional = float\(ts\.get\("alphafox_live_max_notional"\) or 0\)',
    web_src))
ok6 = ok6 and bool(re.search(r"sync_alphafox_live\(\s*live_enabled,\s*live_notional",
                             web_src))
print(("[PASS] " if ok6 else "[FAIL] ")
      + "探测线程从设置读「单笔上限」（alphafox_live_max_notional）并传给对账")

# web.py 顶层 import alphafox_live：镜像层的"进程启动时刻"必须定在 web 启动瞬间，
# 否则首个 poll 里 process() 先写出的新仓可能被判成"存量仓"而不镜像
top_level = web_src.split("app = FastAPI")[0]
ok3 = bool(re.search(r"^import alphafox_live", top_level, re.M))
print(("[PASS] " if ok3 else "[FAIL] ")
      + "web.py 顶层 import alphafox_live（_STARTED_AT 定在启动瞬间）")

# 探测轮次准备段（init_db/锁/following_ids）必须有异常兜底：SQLite 抖一下就会
# 把探测线程整个打死 → 跟单、SSE、实盘对账全停，交易员平仓不再触发退出
ok4 = ("探测轮次准备失败" in web_src
       and re.search(r"if not _inited:\s*\n\s*alphafox_trade\.init_db\(\)", web_src)
       is not None
       # 失败必须把 _inited 置回 False：否则 alphafox.db 被删/换/损坏后
       # init_db 再不被调用 → 跟单/SSE/TG/实盘对账全停。
       # 必须卡在**这个 except 分支里**（M116 就是删掉它）：只查 "_inited = False"
       # 会被线程开头那行初始化赋值蒙过 —— 变异删掉兜底它照样命中
       and re.search(r"except Exception as e:\s*\n(?:\s*#[^\n]*\n)*"
                     r"\s*_inited = False", web_src) is not None)
print(("[PASS] " if ok4 else "[FAIL] ")
      + "探测轮次准备段有兜底 + 失败后下轮重建（线程不会静默死掉）")

# polledAt 门必须用独立的 _af_polled：借道 _af_latest 时，只跟单不看页面的
# 交易员 prev_polled 恒为 None → 门失效 → 每 15s 重复跑 process
ok5 = ("_af_polled.get(sid)" in web_src and "_af_polled[sid] = cur_polled" in web_src
       and "_af_polled = {}" in web_src)
print(("[PASS] " if ok5 else "[FAIL] ")
      + "polledAt 门独立记账（无页面订阅也跟着生效）")
sys.exit(0 if (ok and ok2 and ok3 and ok4 and ok5 and ok6) else 1)
