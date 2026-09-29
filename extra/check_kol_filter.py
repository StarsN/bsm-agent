"""
诊断 KOL 筛选：看最近每轮入库了多少 token，及每个 token 的三维触发情况
用法: python3 extra/check_kol_filter.py
"""
import sqlite3, json, sys, os

db = "binance_square.db"
root = os.environ.get("AGENT_DB_ROOT", "/root/binance-monitor/bsm-agent")
if os.path.exists(os.path.join(root, db)): db = os.path.join(root, db)

conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kol_agent import kol_is_interesting

# 1. 最近 10 轮入库统计
print("=" * 60)
print("1. 最近 10 轮 KOL 候选入库统计")
print("=" * 60)
cur.execute("""
    SELECT round_number, COUNT(*) as cnt, GROUP_CONCAT(token, ', ') as tokens
    FROM kol_candidates
    GROUP BY round_number
    ORDER BY round_number DESC LIMIT 10
""")
rows = cur.fetchall()
for r in reversed(rows):
    print(f"  第{r['round_number']}轮: {r['cnt']} 个 — {r['tokens']}")

# 2. 查最新一轮每个 token 的三维触发情况
print(f"\n{'=' * 60}")
print("2. 最新一轮候选的三维触发详情")
print("=" * 60)
cur.execute("""
    SELECT data FROM kol_candidates
    WHERE round_number = (SELECT MAX(round_number) FROM kol_candidates)
""")
latest = cur.fetchall()
print(f"最新一轮: {len(latest)} 个 token\n")

# 需要快照数据来模拟 kol_is_interesting 的输入
for r in latest:
    d = json.loads(r["data"])
    token = d.get("token", "?")
    # 从快照查对应数据
    snap_row = cur.execute(
        "SELECT snapshot FROM market_snapshots WHERE token = ?", (token,)
    ).fetchone()
    if not snap_row:
        print(f"  {token}: 快照不存在")
        continue
    snap = json.loads(snap_row["snapshot"])

    oi_1h = abs(snap.get("oi_change_1h_pct") or 0)
    oi_4h = abs(snap.get("oi_change_4h_pct") or 0)
    fr = abs(snap.get("funding_rate_pct") or 0)
    ma = abs(snap.get("ma20_deviation_pct") or 0)
    vo = snap.get("vol_oi_ratio")

    s = 0
    flags = []
    if oi_1h > 3 or oi_4h > 8: s += 1; flags.append(f"OI(1h={oi_1h:.1f}%/4h={oi_4h:.1f}%)")
    if fr > 0.03: s += 1; flags.append(f"费率({fr:.4f}%)")
    if ma > 2.5: s += 1; flags.append(f"MA20({ma:.2f}%)")

    print(f"  {token}: score={s} {'入选' if s>=2 else '落选'} | {' | '.join(flags) if flags else '无触发'}")
    if vo and float(vo) > 20:
        print(f"         vol/OI={vo:.1f} > 20 → 应被排除!")

# 3. 验证代码是否生效
print(f"\n{'=' * 60}")
print("3. 代码验证")
print("=" * 60)
print(f"  kol_is_interesting 函数存在: {callable(kol_is_interesting)}")
print(f"  web.py KOL 循环: 检查是否用了 kol_is_interesting")

conn.close()
