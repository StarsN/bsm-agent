"""
用全量快照模拟：不同阈值下每30个token中入选几个
用法: python3 extra/calibrate_kol.py
"""
import sqlite3, json, sys, os

db = "binance_square.db"
root = os.environ.get("AGENT_DB_ROOT", "/root/binance-monitor/bsm-agent")
if os.path.exists(os.path.join(root, db)): db = os.path.join(root, db)

conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("SELECT token, snapshot FROM market_snapshots")
rows = cur.fetchall()
conn.close()

snaps = []
for r in rows:
    snap = json.loads(r["snapshot"])
    if snap.get("mark_price"):
        snap["_token"] = r["token"]
        snaps.append(snap)

n = len(snaps)
print(f"全量快照: {n} 个 token\n")
print(f"估算: 全量 521 token 通过率 → 按每轮50 token估算入选数\n")

configs = [
    (3.0, 8.0,  0.03, 2.5, "当前 (OI3/8 FR0.03 MA2.5)"),
    (4.0, 10.0, 0.03, 2.5, "OI收紧 (OI4/10)"),
    (4.0, 10.0, 0.05, 2.5, "OI+费率 (OI4/10 FR0.05)"),
    (4.0, 12.0, 0.03, 2.5, "OI再收 (OI4/12)"),
    (4.0, 10.0, 0.05, 3.0, "全收紧 (OI4/10 FR0.05 MA3)"),
    (5.0, 12.0, 0.05, 3.0, "极限 (OI5/12 FR0.05 MA3)"),
]

print(f"{'配置':<30} {'通过%':>7} {'每轮≈':>7}")
print("-" * 60)

for oi1, oi4, fr, ma, desc in configs:
    passed = 0
    vol_excluded = 0
    for s in snaps:
        vo = s.get("vol_oi_ratio")
        if vo is not None and float(vo) > 20:
            vol_excluded += 1
            continue

        score = 0
        if abs(s.get("oi_change_1h_pct") or 0) > oi1 or abs(s.get("oi_change_4h_pct") or 0) > oi4:
            score += 1
        if abs(s.get("funding_rate_pct") or 0) > fr:
            score += 1
        if abs(s.get("ma20_deviation_pct") or 0) > ma:
            score += 1
        if score >= 2:
            passed += 1

    pct = passed / n * 100
    # 每轮约 50 token（根据 kol_candidates 最近轮次均值）
    per_round = pct / 100 * 50

    print(f"{desc:<30} {pct:>6.1f}% {per_round:>6.1f}")
    if desc.startswith("当前"):
        print(f"  vol/OI>20排除: {vol_excluded}")
        # 各维度单独
        oi_only = sum(1 for s in snaps if abs(s.get("oi_change_1h_pct") or 0) > oi1 or abs(s.get("oi_change_4h_pct") or 0) > oi4)
        fr_only = sum(1 for s in snaps if abs(s.get("funding_rate_pct") or 0) > fr)
        ma_only = sum(1 for s in snaps if abs(s.get("ma20_deviation_pct") or 0) > ma)
        print(f"  各维: OI={oi_only}({oi_only/n*100:.1f}%) FR={fr_only}({fr_only/n*100:.1f}%) MA={ma_only}({ma_only/n*100:.1f}%)")
