"""验证 web.py「添加监控币」的真实 SQL（从源码抽取执行，非手抄）。

用户语义：重新加入此前被移除的币 → 重置为纸盘 + 暂停。
"""
import io
import os
import re
import sqlite3
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

web_src = open(os.path.join(root, "web.py"), encoding="utf-8").read()
# 定位「添加监控币」的 INSERT 语句（跨多个相邻字符串字面量），拼成真实 SQL
m = re.search(
    r'conn\.execute\(\s*((?:"(?:[^"\\]|\\.)*"\s*)+),\s*\(token, now, now\)',
    web_src)
if not m:
    print("[FAIL] 未能从 web.py 定位添加币的插入 SQL")
    sys.exit(1)
sql = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)))
if "INSERT INTO supertrend_tokens" not in sql:
    print("[FAIL] 抽取到的不是预期 SQL:", sql[:120])
    sys.exit(1)
print("抽取 SQL:")
print(sql)
print()

db = os.path.join(tempfile.mkdtemp(prefix="st_add_"), "st.db")
conn = sqlite3.connect(db)
conn.execute("""CREATE TABLE supertrend_tokens (
    token TEXT PRIMARY KEY, status TEXT DEFAULT 'ACTIVE', added_at INTEGER,
    removed_at INTEGER, live_mode INTEGER DEFAULT 0, paused INTEGER DEFAULT 0)""")

now = 1700000000
PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def add(token):
    conn.execute(sql, (token, now, now))
    conn.commit()


def row(token):
    r = conn.execute("SELECT * FROM supertrend_tokens WHERE token=?", (token,)).fetchone()
    return r


# 1) 全新币：ACTIVE + 暂停 + 纸盘
add("BTC")
r = row("BTC")
check("全新币：ACTIVE/paused=1/live_mode=0",
      r and r[1] == "ACTIVE" and r[5] == 1 and (r[4] or 0) == 0, str(r))

# 2) 曾实盘被移除的币 → 重加后重置为纸盘 + 暂停
conn.execute("UPDATE supertrend_tokens SET status='REMOVED', live_mode=1, paused=1 "
             "WHERE token='BTC'")
conn.commit()
add("BTC")
r = row("BTC")
check("重加曾实盘币：live_mode 重置为 0", r and (r[4] or 0) == 0, str(r))
check("重加曾实盘币：paused=1（暂停开始）", r and r[5] == 1, str(r))
check("重加曾实盘币：status=ACTIVE", r and r[1] == "ACTIVE", str(r))

# 3) 已 ACTIVE 且实盘的币（重复调用）→ 不重置其模式
conn.execute("UPDATE supertrend_tokens SET live_mode=1, paused=0 WHERE token='BTC'")
conn.commit()
add("BTC")
r = row("BTC")
check("已实盘中重复添加：保留 live_mode=1", r and (r[4] or 0) == 1, str(r))

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
