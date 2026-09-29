import sqlite3, json, os, sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_DIR = Path("/root/binance-monitor/bsm-agent")
sys.path.insert(0, str(PROJECT_DIR))

# ---- config ----
try:
    import config
    inter_min = getattr(config, "AGENT_COLLECT_INTERVAL_MINUTES", 30)
    AGENT_SHORT_DB = getattr(config, "AGENT_SHORT_DB", "db/agent_short.db")
    print(f"config OK: INTERVAL={inter_min}, AGENT_SHORT_DB={AGENT_SHORT_DB}")
except Exception as e:
    inter_min = 30
    AGENT_SHORT_DB = "db/agent_short.db"
    print(f"config FAILED: {e}")

_agent_db = str(PROJECT_DIR / AGENT_SHORT_DB)
print(f"agent_short DB: {_agent_db}")
print(f"exists: {os.path.exists(_agent_db)}")

conn = sqlite3.connect(_agent_db)
conn.row_factory = sqlite3.Row

# ---- server time ----
now_utc = datetime.now(timezone.utc)
print(f"\nServer UTC: {now_utc}")
print(f"Server CST: {now_utc + timedelta(hours=8)}")

# ---- total + latest ----
total = conn.execute("SELECT COUNT(*) as cnt FROM agent_short_candidates").fetchone()
print(f"\nTotal rows: {total['cnt']}")

latest = conn.execute(
    "SELECT id, token, passed, created_at FROM agent_short_candidates ORDER BY id DESC LIMIT 10"
).fetchall()
print("Latest 10:")
for r in latest:
    print(f"  id={r['id']} token={r['token']:8s} passed={r['passed']} created_at={r['created_at']}")

# ---- time window test ----
for window_min in [32, 60, 120, 180, 300, 600]:
    query = (
        "SELECT COUNT(*) as cnt FROM agent_short_candidates "
        "WHERE created_at >= datetime('now', '-{} minutes')".format(window_min)
    )
    rows = conn.execute(query).fetchone()
    print(f"Window {window_min:3d}min: {rows['cnt']} rows")

# ---- exact extract query ----
window = inter_min + 2
print(f"\n=== Exact extract query (window={window}min) ===")

# Part 1: inner query - tokens in window
inner = conn.execute(
    "SELECT token, MAX(id) AS max_id FROM agent_short_candidates "
    "WHERE created_at >= datetime('now', '-{} minutes') ".format(window) +
    "GROUP BY token"
).fetchall()
print(f"Inner (tokens in window): {len(inner)}")
for r in inner:
    print(f"  token={r['token']} max_id={r['max_id']}")

# Part 2: full query
query = (
    "SELECT a.id, a.token, a.passed, a.created_at FROM agent_short_candidates a "
    "INNER JOIN ("
    "  SELECT token, MAX(id) AS max_id FROM agent_short_candidates "
    "  WHERE created_at >= datetime('now', '-{} minutes') ".format(window) +
    "  GROUP BY token"
    ") b ON a.id = b.max_id "
    "ORDER BY a.id"
)
print(f"\nFull query:")
ac_rows = conn.execute(query).fetchall()
print(f"Result: {len(ac_rows)} rows")
for r in ac_rows:
    print(f"  id={r['id']} token={r['token']:8s} created_at={r['created_at']}")

# ---- without time filter ----
print("\n=== Without time filter (same dedup logic) ===")
all_rows = conn.execute(
    "SELECT a.id, a.token, a.passed, a.created_at FROM agent_short_candidates a "
    "INNER JOIN ("
    "  SELECT token, MAX(id) AS max_id FROM agent_short_candidates "
    "  GROUP BY token"
    ") b ON a.id = b.max_id "
    "ORDER BY a.id"
).fetchall()
print(f"Result: {len(all_rows)} unique tokens")
for r in all_rows:
    print(f"  id={r['id']} token={r['token']:8s} created_at={r['created_at']}")

# ---- window relative to latest data time ----
print("\n=== Time gap analysis ===")
latest_time_str = conn.execute("SELECT MAX(created_at) FROM agent_short_candidates").fetchone()[0]
print(f"Latest data created_at: {latest_time_str}")

# SQLite datetime math
gap = conn.execute(
    "SELECT ROUND((julianday('now') - julianday(?)) * 24 * 60) as minutes_gap",
    [latest_time_str]
).fetchone()[0]
print(f"Gap from now: {gap} minutes")

conn.close()
print("\nDONE")
