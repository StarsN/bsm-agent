"""MA20 乖离率分布统计"""
import sqlite3, json, sys, os

db = "binance_square.db"
root = os.environ.get("AGENT_DB_ROOT", "/root/binance-monitor/bsm-agent")
if os.path.exists(os.path.join(root, db)): db = os.path.join(root, db)

conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("SELECT snapshot FROM market_snapshots ORDER BY rowid DESC LIMIT 1000")
rows = cur.fetchall()
conn.close()

vals = []
for r in rows:
    snap = json.loads(r["snapshot"])
    v = snap.get("ma20_deviation_pct")
    if v is not None:
        vals.append(v)

if not vals:
    print("无 MA20 乖离率数据")
    sys.exit(1)

vals.sort()
n = len(vals)
avg = sum(vals) / n
lo = vals[0]
hi = vals[-1]
p50 = vals[n // 2]
p90 = vals[int(n * 0.9)]
p95 = vals[int(n * 0.95)]
p99 = vals[int(n * 0.99)]

print(f"样本数: {n}")
print(f"均值: {avg:+.2f}%  中位: {p50:+.2f}%")
print(f"范围: {lo:+.2f}% ~ {hi:+.2f}%")
print()
print(f"分位数:")
print(f"  P90:  {p90:+.2f}%")
print(f"  P95:  {p95:+.2f}%")
print(f"  P99:  {p99:+.2f}%")
print()

# 分段统计
bins = [(-99, -10), (-10, -5), (-5, -3), (-3, -1), (-1, 1), (1, 3), (3, 5), (5, 10), (10, 99)]
print(f"{'区间':>12}  {'数量':>6}  {'占比':>7}")
print("-" * 28)
for lo, hi in bins:
    cnt = sum(1 for v in vals if lo <= v < hi)
    pct = cnt / n * 100
    bar = "█" * int(pct / 2)
    print(f"  {lo:>+5} ~ {hi:>+5}%  {cnt:>6}  {pct:>5.1f}%  {bar}")

# >5% 的 token
extreme = [(json.loads(r["snapshot"]).get("token", "?"), json.loads(r["snapshot"]).get("ma20_deviation_pct", 0))
           for r in rows if json.loads(r["snapshot"]).get("ma20_deviation_pct", 0) != None
           and abs(json.loads(r["snapshot"]).get("ma20_deviation_pct", 0)) > 5]
if extreme:
    print(f"\n|MA20|>5% 的样本 ({len(extreme)} 个):")
    for t, v in sorted(set(extreme), key=lambda x: -abs(x[1]))[:10]:
        print(f"  {t}: {v:+.2f}%")
