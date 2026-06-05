"""修复 heat_agent 复盘教训的串位数据：strategy='0' + learned='heat_agent' → strategy='heat_agent' + learned=0"""
import os
import sqlite3
import sys

import config
import storage

DB = config.DB_PATH
os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# 查受影响的行
rows = conn.execute(
    "SELECT id, strategy, learned FROM lessons WHERE strategy='0' AND learned='heat_agent'"
).fetchall()

if not rows:
    print("没有需要修复的数据")
    conn.close()
    sys.exit(0)

print(f"找到 {len(rows)} 条需要修复的记录:")
for r in rows:
    print(f"  id={r['id']} strategy={r['strategy']!r} learned={r['learned']!r}")

# 修复
conn.execute(
    "UPDATE lessons SET strategy='heat_agent', learned=0 WHERE strategy='0' AND learned='heat_agent'"
)
conn.commit()

# 验证
rows2 = conn.execute(
    "SELECT id, strategy, learned FROM lessons WHERE strategy='0' AND learned='heat_agent'"
).fetchall()
print(f"\n修复后残留: {len(rows2)} 条")

# 确认 heat_agent 正确的数据
correct = conn.execute(
    "SELECT COUNT(*) as cnt FROM lessons WHERE strategy='heat_agent'"
).fetchone()["cnt"]
print(f"heat_agent 策略共 {correct} 条教训")

conn.close()
print("修复完成")
