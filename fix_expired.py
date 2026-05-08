#!/usr/bin/env python3
"""清理因 auto_trader bug 而错误执行的过期决策"""
import sqlite3, sys, os

DB = sys.argv[1] if len(sys.argv) > 1 else "extra/binance_square05081004.db"
conn = sqlite3.connect(DB)

# 只清 bug 期间的：2026-05-07 16:00 之后的过期决策
rows = conn.execute("""
    SELECT id, token, status, created_at FROM pending_decisions
    WHERE created_at > '2026-05-07 16:00:00'
    AND created_at < datetime('now', '-10 minutes')
""").fetchall()

if not rows:
    print("无过期决策")
    conn.close(); sys.exit(0)

print("=== 过期决策 ===")
expired_ids = []
for r in rows:
    print(f"  id={r[0]} {r[1]} {r[2]} {r[3]}")
    expired_ids.append(r[0])

# 找关联持仓
ph = ",".join("?" * len(expired_ids))
pos_rows = conn.execute(
    f"SELECT id, token FROM trade_positions WHERE id IN ("
    f"SELECT json_extract(signal_snapshot, '$.agent_decision.id') FROM trade_positions"
    f") AND json_extract(signal_snapshot, '$.agent_decision.id') IN ({ph})",
    expired_ids
).fetchall()

print(f"\n=== 关联持仓: {len(pos_rows)} 个 ===")
pos_ids = []
for r in pos_rows:
    print(f"  id={r[0]} {r[1]}")
    pos_ids.append(r[0])

if not pos_ids:
    print("无关联持仓，只清 pending_decisions")
    conn.execute(f"DELETE FROM pending_decisions WHERE id IN ({ph})", expired_ids)
    conn.commit(); conn.close()
    sys.exit(0)

print(f"\n将删除:")
print(f"  {len(expired_ids)} 条 pending_decisions")
print(f"  {len(pos_ids)} 条 trade_positions + journal + 归档 + signal_lock")

confirm = input("确认? (y/N): ").strip().lower()
if confirm != 'y':
    print("取消"); conn.close(); sys.exit(0)

ph2 = ",".join("?" * len(pos_ids))
conn.execute(f"DELETE FROM journal WHERE order_id IN ({ph2})", pos_ids)
conn.execute(f"DELETE FROM trade_loss_archive WHERE position_id IN ({ph2})", pos_ids)
conn.execute(f"DELETE FROM trade_signal_locks WHERE token IN (SELECT token FROM trade_positions WHERE id IN ({ph2}))", pos_ids)
conn.execute(f"DELETE FROM trade_positions WHERE id IN ({ph2})", pos_ids)
conn.execute(f"DELETE FROM pending_decisions WHERE id IN ({ph})", expired_ids)
conn.commit()
print("完成")
conn.close()
