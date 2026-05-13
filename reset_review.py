#!/usr/bin/env python3
"""重置复盘状态：旧教训全部标记失效，journal 全部标记未复盘"""
import sqlite3, sys

DB = sys.argv[1] if len(sys.argv) > 1 else "extra/binance_square_05111634.db"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# 当前状态
old_lessons = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
old_active = conn.execute("SELECT COUNT(*) FROM lessons WHERE learned=0").fetchone()[0]
old_reviewed = conn.execute("SELECT COUNT(*) FROM journal WHERE reviewed=1").fetchone()[0]
old_total = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]

print(f"当前状态:")
print(f"  lessons: {old_lessons} 条 (活跃 {old_active})")
print(f"  journal: {old_reviewed}/{old_total} 条已复盘")
print()

print("重置中...")

# 1. 全部旧教训标记失效
conn.execute("UPDATE lessons SET learned=1")
new_active = 0

# 2. 全部 journal 标记未复盘
conn.execute("UPDATE journal SET reviewed=0")
new_unreviewed = conn.execute("SELECT COUNT(*) FROM journal WHERE reviewed=0").fetchone()[0]

conn.commit()

print(f"重置完成:")
print(f"  lessons: {old_active} → {new_active} 条活跃")
print(f"  journal: {new_unreviewed} 条未复盘")

conn.close()
print(f"\n下一步：运行复盘 skill")
