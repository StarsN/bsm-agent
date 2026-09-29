#!/usr/bin/env python3
"""排查 market_realtime_cache 为何无数据"""
import sqlite3, json, subprocess, os

DB = "db/binance_square.db"

# 1. 进程
print("=== 1. market_realtime 进程 ===")
try:
    r = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5)
    for line in r.stdout.split("\n"):
        if "market_realtime" in line and "grep" not in line:
            print(f"  ✅ {line.strip()}")
            break
    else:
        print("  ❌ 未找到 market_realtime 进程")
except Exception as e:
    print(f"  ps 失败: {e}")

# 2. 表数据
print("\n=== 2. market_realtime_cache 表 ===")
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

cnt = conn.execute("SELECT COUNT(*) as n FROM market_realtime_cache").fetchone()["n"]
print(f"  总行数: {cnt}")

if cnt > 0:
    rows = conn.execute("SELECT token FROM market_realtime_cache LIMIT 5").fetchall()
    print(f"  前5个代币: {[r['token'] for r in rows]}")

    # 看一个样本的数据结构
    row = conn.execute("SELECT token, snapshot FROM market_realtime_cache LIMIT 1").fetchone()
    snap = json.loads(row["snapshot"]) if isinstance(row["snapshot"], str) else row["snapshot"]
    print(f"  {row['token']} 的字段: {list(snap.keys())[:10]}")
    print(f"  mark_price: {snap.get('mark_price')}")

# 3. 热度榜代币
print("\n=== 3. 热度榜代币 vs realtime 覆盖 ===")
tokens = [r["token"] for r in conn.execute(
    "SELECT token FROM market_snapshots ORDER BY updated_at DESC LIMIT 10"
).fetchall()]
hit = 0
for t in tokens:
    rt = conn.execute("SELECT 1 FROM market_realtime_cache WHERE token=?", (t,)).fetchone()
    if rt:
        hit += 1
    else:
        print(f"  ❌ {t}: 快照有数据，realtime 无数据")
print(f"  覆盖: {hit}/{len(tokens)}")

conn.close()
print("\n如果 realtime 行为 0，需要检查 market_realtime.py 是否正常运行及其订阅逻辑。")
