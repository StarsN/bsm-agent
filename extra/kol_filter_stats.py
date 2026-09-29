"""KOL 异常度三个维度的触发概率统计"""
import sqlite3, json, sys, os

db = "binance_square.db"
root = os.environ.get("AGENT_DB_ROOT", "/root/binance-monitor/bsm-agent")
if os.path.exists(os.path.join(root, db)): db = os.path.join(root, db)

conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("SELECT snapshot FROM market_snapshots ORDER BY rowid DESC LIMIT 2000")
rows = cur.fetchall()
conn.close()

snaps = []
for r in rows:
    snap = json.loads(r["snapshot"])
    if snap.get("mark_price"):  # 有价格才算有效
        snaps.append(snap)

n = len(snaps)
print(f"有效快照: {n}\n")

# 维度1: OI 异常
oi_hits = 0
oi_vals_1h = []
oi_vals_4h = []
for s in snaps:
    o1 = abs(s.get("oi_change_1h_pct") or 0)
    o4 = abs(s.get("oi_change_4h_pct") or 0)
    oi_vals_1h.append(o1)
    oi_vals_4h.append(o4)
    if o1 > 3 or o4 > 8:
        oi_hits += 1

oi_vals_1h.sort()
oi_vals_4h.sort()

print("=" * 50)
print("维度1: OI 异常 (|1h| > 3% 或 |4h| > 8%)")
print("=" * 50)
print(f"  触发次数: {oi_hits}/{n} = {oi_hits/n*100:.1f}%")
print(f"  OI 1h: 均值={sum(oi_vals_1h)/n:.2f}%  中位={oi_vals_1h[n//2]:.2f}%  P90={oi_vals_1h[int(n*0.9)]:.2f}%  P95={oi_vals_1h[int(n*0.95)]:.2f}%")
print(f"  OI 4h: 均值={sum(oi_vals_4h)/n:.2f}%  中位={oi_vals_4h[n//2]:.2f}%  P90={oi_vals_4h[int(n*0.9)]:.2f}%  P95={oi_vals_4h[int(n*0.95)]:.2f}%")

# 维度2: 费率异常
fr_hits = 0
fr_vals = []
for s in snaps:
    f = abs(s.get("funding_rate_pct") or 0)
    fr_vals.append(f)
    if f > 0.03:
        fr_hits += 1

fr_vals.sort()
print(f"\n{'=' * 50}")
print("维度2: 费率异常 (|funding| > 0.03%/8h)")
print("=" * 50)
print(f"  触发次数: {fr_hits}/{n} = {fr_hits/n*100:.1f}%")
print(f"  费率: 均值={sum(fr_vals)/n:.4f}%  中位={fr_vals[n//2]:.4f}%  P90={fr_vals[int(n*0.9)]:.4f}%  P95={fr_vals[int(n*0.95)]:.4f}%")
print(f"  费率分布:")
bins = [(0, 0.005), (0.005, 0.01), (0.01, 0.02), (0.02, 0.03), (0.03, 0.05), (0.05, 0.1), (0.1, 99)]
for lo, hi in bins:
    cnt = sum(1 for v in fr_vals if lo <= v < hi)
    if cnt > 0:
        print(f"    {lo:.3f}~{hi:.3f}%: {cnt} ({cnt/n*100:.1f}%)")

# 维度3: 价格乖离 (如果有数据)
ma_hits = 0
ma_vals = []
for s in snaps:
    m = abs(s.get("ma20_deviation_pct") or 0)
    ma_vals.append(m)
    if m > 2.5:
        ma_hits += 1

ma_vals.sort()
print(f"\n{'=' * 50}")
print("维度3: 价格乖离 (|MA20| > 2.5%)")
print("=" * 50)
print(f"  触发次数: {ma_hits}/{n} = {ma_hits/n*100:.1f}%")
if len(ma_vals) > 0:
    print(f"  MA20乖离: 均值={sum(ma_vals)/len(ma_vals):.2f}%  中位={ma_vals[len(ma_vals)//2]:.2f}%")
    non_zero = [v for v in ma_vals if v > 0]
    if non_zero:
        print(f"  非零值的: P90={non_zero[int(len(non_zero)*0.9)]:.2f}%  P95={non_zero[int(len(non_zero)*0.95)]:.2f}%")

# 综合: 至少2维触发
print(f"\n{'=' * 50}")
print("综合: ≥2 维度触发 (kol_is_interesting)")
print("=" * 50)
scores = []
for s in snaps:
    score = 0
    o1 = abs(s.get("oi_change_1h_pct") or 0)
    o4 = abs(s.get("oi_change_4h_pct") or 0)
    if o1 > 3 or o4 > 8: score += 1
    if abs(s.get("funding_rate_pct") or 0) > 0.03: score += 1
    if abs(s.get("ma20_deviation_pct") or 0) > 2.5: score += 1
    scores.append(score)

for level in [0, 1, 2, 3]:
    cnt = scores.count(level)
    print(f"  {level}维触发: {cnt}/{n} = {cnt/n*100:.1f}%")
print(f"  ≥2维 = kol入选: {sum(1 for s in scores if s >= 2)}/{n} = {sum(1 for s in scores if s >= 2)/n*100:.1f}%")

# vol/OI 排除
vol_exclude = 0
for s in snaps:
    vo = s.get("vol_oi_ratio")
    if vo is not None and float(vo) > 20:
        vol_exclude += 1
print(f"\n  vol/OI > 20 排除: {vol_exclude}/{n} = {vol_exclude/n*100:.1f}%")
