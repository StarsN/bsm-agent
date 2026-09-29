#!/usr/bin/env python3
"""诊断 agent_short extract 查询 — 模拟完整路径，在服务器上运行"""
import sys, os, json, sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(PROJECT_DIR))

print(f"SCRIPT_DIR:  {SCRIPT_DIR}")
print(f"PROJECT_DIR: {PROJECT_DIR}")
print()

# --- Step 1: config ---
try:
    import config
    db_root = getattr(config, "AGENT_DB_ROOT", "")
    DB_NAME = getattr(config, "DB_PATH", "db/binance_square.db")
    AGENT_SHORT_DB = getattr(config, "AGENT_SHORT_DB", "db/agent_short.db")
    inter_min_cfg = getattr(config, "AGENT_COLLECT_INTERVAL_MINUTES", 15)
    print(f"[config] OK")
    print(f"  AGENT_DB_ROOT = {db_root!r}")
    print(f"  DB_PATH       = {DB_NAME!r}")
    print(f"  AGENT_SHORT_DB = {AGENT_SHORT_DB!r}")
    print(f"  AGENT_COLLECT_INTERVAL_MINUTES = {inter_min_cfg}")
except Exception as e:
    print(f"[config] FAILED: {e}")
    db_root = ""
    DB_NAME = "db/binance_square.db"
    AGENT_SHORT_DB = "db/agent_short.db"
    inter_min_cfg = 15

# --- Step 2: 主DB ---
if db_root:
    MAIN_DB = str(Path(os.path.expanduser(db_root)) / DB_NAME)
else:
    MAIN_DB = str(PROJECT_DIR / DB_NAME)
print(f"\n[主DB] {MAIN_DB}")
print(f"  exists = {os.path.exists(MAIN_DB)}")

# --- Step 3: agent_short DB ---
_agent_db = str(PROJECT_DIR / AGENT_SHORT_DB)
print(f"\n[agent_short DB] {_agent_db}")
print(f"  exists = {os.path.exists(_agent_db)}")

if not os.path.exists(_agent_db):
    # 尝试 db_root 版本
    if db_root:
        _agent_db2 = str(Path(db_root) / AGENT_SHORT_DB)
        print(f"  db_root版: {_agent_db2}  exists={os.path.exists(_agent_db2)}")
    print("\n>>> FAIL: agent_short.db 不存在！extract 返回 0 candidates")
    sys.exit(1)

# --- Step 4: 表结构 ---
ac = sqlite3.connect(_agent_db)
ac.row_factory = sqlite3.Row

tables = ac.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print(f"\n[表] {[t['name'] for t in tables]}")

total = ac.execute("SELECT COUNT(*) as cnt FROM agent_short_candidates").fetchone()
print(f"[总行数] {total['cnt']}")

# --- Step 5: 时间窗口查询 ---
main_conn = sqlite3.connect(MAIN_DB)
main_conn.row_factory = sqlite3.Row

inter_min = inter_min_cfg
try:
    ts = {r["key"]: r["value"] for r in main_conn.execute("SELECT * FROM trading_settings").fetchall()}
    inter_min = int(ts.get("agent_collect_interval_minutes", inter_min_cfg))
    print(f"\n[trading_settings] agent_collect_interval_minutes = {inter_min}")
except Exception as e:
    print(f"\n[trading_settings] 读取失败: {e}")

window_min = inter_min + 2
print(f"[时间窗口] {window_min} 分钟")

# 模拟 extract 的查询
now_utc = datetime.now(timezone.utc)
print(f"[当前UTC] {now_utc.strftime('%Y-%m-%d %H:%M:%S')}")
print(f"[当前CST] {(now_utc + timedelta(hours=8)).strftime('%Y-%m-%d %H:%M:%S')}")

query = (
    f"SELECT a.data FROM agent_short_candidates a "
    "INNER JOIN ("
    "  SELECT token, MAX(id) AS max_id FROM agent_short_candidates "
    f"  WHERE created_at >= datetime('now', '-{window_min} minutes') "
    "  GROUP BY token"
    ") b ON a.id = b.max_id "
    "ORDER BY a.id"
)
print(f"\n[SQL] {query}")

ac_rows = ac.execute(query).fetchall()
print(f"[结果] {len(ac_rows)} 条（窗口内去重后）")

if ac_rows:
    for r in ac_rows:
        d = json.loads(r["data"])
        print(f"  token={d.get('token','?')} passed={d.get('passed','?')}")
else:
    print("  >>> 窗口内无数据！")

# --- Step 6: 最近数据 & 时间分布 ---
print("\n--- 最近10条 ---")
recent = ac.execute(
    "SELECT id, token, passed, created_at FROM agent_short_candidates ORDER BY id DESC LIMIT 10"
).fetchall()
for r in recent:
    print(f"  id={r['id']} token={r['token']:8s} passed={r['passed']} created_at={r['created_at']}")

print("\n--- 按小时分布（最近48h）---")
dist = ac.execute(
    "SELECT strftime('%Y-%m-%d %H:00', created_at) as hr, COUNT(*) as cnt "
    "FROM agent_short_candidates "
    "WHERE created_at >= datetime('now', '-48 hours') "
    "GROUP BY hr ORDER BY hr DESC LIMIT 48"
).fetchall()
for d in dist:
    bar = "#" * min(d['cnt'], 50)
    print(f"  {d['hr']}  {d['cnt']:3d}  {bar}")

# --- Step 7: 模拟特定时间点查询 ---
print("\n--- 模拟 17:30 CST 执行 extract ---")
target_cst = "2026-06-13 17:30:00"
target_utc = "2026-06-13 09:30:00"
print(f"  目标时间 CST={target_cst}  UTC={target_utc}")

# 用 datetime('2026-06-13 09:30:00', '-18 minutes') = 09:12 UTC = 17:12 CST
window_rows = ac.execute(
    "SELECT id, token, passed, created_at FROM agent_short_candidates "
    "WHERE created_at >= datetime(?, '-18 minutes') "
    "AND created_at <= ? "
    "ORDER BY id",
    [target_utc, target_utc]
).fetchall()
print(f"  17:12-17:30 CST 窗口内: {len(window_rows)} 条")
for r in window_rows:
    print(f"    id={r['id']} token={r['token']:8s} passed={r['passed']} created_at={r['created_at']}")

# 看看 17:30 之前最新数据是什么时候
before_rows = ac.execute(
    "SELECT id, token, passed, created_at FROM agent_short_candidates "
    "WHERE created_at <= ? "
    "ORDER BY id DESC LIMIT 5",
    [target_utc]
).fetchall()
print(f"  17:30 CST 之前最新5条:")
for r in before_rows:
    print(f"    id={r['id']} token={r['token']:8s} passed={r['passed']} created_at={r['created_at']}")

main_conn.close()
ac.close()
print("\nDONE")
