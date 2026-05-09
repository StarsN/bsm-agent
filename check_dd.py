#!/usr/bin/env python3
"""直接查询数据库计算最大回撤，绕过缓存"""
import sqlite3, sys

DB = sys.argv[1] if len(sys.argv) > 1 else "binance_square.db"

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# 账户初始
s = conn.execute("SELECT * FROM trading_settings").fetchall()
settings = {r["key"]: r["value"] for r in s}
initial = float(settings.get("initial_balance", 1000))
print(f"初始金额: ${initial:.2f}")

# 所有已平仓盈亏（按时间排序）
closed = conn.execute(
    "SELECT token, COALESCE(realized_pnl,0) as pnl, closed_at "
    "FROM trade_positions WHERE status='CLOSED' ORDER BY closed_at"
).fetchall()
print(f"已平仓: {len(closed)} 笔")

# 计算回撤
running = initial
peak = running
max_dd = 0.0
worst_at = ""
for r in closed:
    running += r["pnl"]
    if running > peak:
        peak = running
    if peak > 0:
        dd = (running - peak) / peak * 100
        if dd < max_dd:
            max_dd = dd
            worst_at = r["closed_at"]
    if r["pnl"] < -50:
        print(f"  {r['token']} pnl={r['pnl']:.2f} running={running:.2f} peak={peak:.2f} dd={dd:.2f}%  {r['closed_at']}")

# 加上当前浮动
unrealized = conn.execute(
    "SELECT COALESCE(SUM(unrealized_pnl),0) FROM trade_positions "
    "WHERE status IN ('OPEN','PARTIAL')"
).fetchone()[0] or 0
final = running + unrealized
dd = (final - peak) / peak * 100 if peak > 0 else 0
if dd < max_dd:
    max_dd = dd

print(f"\nrunning={running:.2f} peak={peak:.2f} unrealized={unrealized:.2f} final={final:.2f}")
print(f"最大回撤: {max_dd:.2f}% (发生在 {worst_at})")

conn.close()
