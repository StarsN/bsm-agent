#!/usr/bin/env python3
"""检查实时价格覆盖是否生效。对比 market_snapshots 和 market_realtime_cache 的 mark_price。"""
import sqlite3, json

DB = "db/binance_square.db"
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# 取最近 5 个有快照的代币
tokens = [r["token"] for r in conn.execute(
    "SELECT token FROM market_snapshots ORDER BY updated_at DESC LIMIT 5"
).fetchall()]

if not tokens:
    print("无快照数据")
    conn.close()
    exit(1)

print(f"对比 {len(tokens)} 个代币的快照 vs 实时价格:\n")

for token in tokens:
    # 快照
    snap_row = conn.execute(
        "SELECT snapshot FROM market_snapshots WHERE token=?", (token,)
    ).fetchone()
    snap = json.loads(snap_row["snapshot"]) if snap_row else {}
    snap_price = snap.get("mark_price")

    # 实时缓存
    rt_row = conn.execute(
        "SELECT snapshot FROM market_realtime_cache WHERE token=?", (token,)
    ).fetchone()
    rt = {}
    if rt_row:
        rt = json.loads(rt_row["snapshot"]) if isinstance(rt_row["snapshot"], str) else rt_row["snapshot"]
    rt_price = rt.get("mark_price")

    if snap_price and rt_price:
        diff = abs(snap_price - rt_price)
        diff_pct = diff / snap_price * 100
        flag = "" if diff_pct < 0.5 else " ⚠ 差价>" if diff_pct < 2 else " ❌ 差价大"
        print(f"  {token}: 快照={snap_price:.4f}  实时={rt_price:.4f}  差{diff_pct:.2f}%{flag}")
    elif snap_price:
        print(f"  {token}: 快照={snap_price:.4f}  实时=无数据 ⚠")
    else:
        print(f"  {token}: 无价格")

conn.close()
print("\n如果差价 < 0.5%，说明 worker 刷新及时。")
print("如果差价 > 1% 但页面价格接近实时价，说明 _build_leaderboard_items 覆盖逻辑生效。")
