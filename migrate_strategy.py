#!/usr/bin/env python3
"""一次性脚本：给 trade_positions 存量 NULL strategy 回填为 'agent'"""
import sqlite3, sys, os

DB = "binance_square.db"
if len(sys.argv) > 1:
    DB = sys.argv[1]

if not os.path.exists(DB):
    print(f"找不到: {DB}")
    sys.exit(1)

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# 检查列是否存在
cols = [r[1] for r in conn.execute("PRAGMA table_info(trade_positions)")]
if "strategy" not in cols:
    conn.execute("ALTER TABLE trade_positions ADD COLUMN strategy TEXT DEFAULT 'agent'")
    conn.commit()
    print("已添加 strategy 列")

updated = conn.execute(
    "UPDATE trade_positions SET strategy = 'agent' WHERE strategy IS NULL"
).rowcount
conn.commit()
conn.close()

print(f"已回填 {updated} 条旧数据 strategy='agent'")
print("完成")
