#!/usr/bin/env python3
"""一次性脚本：存量 NULL 回填（DDL 由 init_db() 启动时自动处理，脚本只管数据）"""
import sqlite3, sys, os

DB = "db/binance_square.db"
if len(sys.argv) > 1:
    DB = sys.argv[1]

if not os.path.exists(DB):
    print(f"找不到: {DB}")
    sys.exit(1)

conn = sqlite3.connect(DB)

tables = [
    ("trade_positions", "strategy", "agent"),
    ("lessons", "strategy", "agent"),
    ("pending_decisions", "source", "agent_candidates"),
]

for tbl, col, default in tables:
    updated = conn.execute(f"UPDATE {tbl} SET {col} = '{default}' WHERE {col} IS NULL").rowcount
    conn.commit()
    print(f"[{tbl}] 回填 {updated} 条 {col}='{default}'")

conn.close()
print("完成")
