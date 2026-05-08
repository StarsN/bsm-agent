import sqlite3
conn = sqlite3.connect("extra/binance_square05071831.db")
conn.row_factory = sqlite3.Row

# SIREN 在 round_candidates 中吗
rc = conn.execute("SELECT * FROM round_candidates WHERE token='SIREN' ORDER BY source_round DESC LIMIT 5").fetchall()
print(f"round_candidates: {len(rc)} 条")
for r in rc:
    print(f"  round={r['source_round']} score={r['score']} delivered={r['delivered']}")

# SIREN 在 token_heat_history 中吗
hh = conn.execute("SELECT * FROM token_heat_history WHERE token='SIREN' ORDER BY round_number DESC LIMIT 5").fetchall()
print(f"token_heat_history: {len(hh)} 条")
for h in hh:
    print(f"  round={h['round_number']} score={h['score']} mentions={h['mentions']}")

# SIREN 在 market_snapshots 中吗
ms = conn.execute("SELECT * FROM market_snapshots WHERE token='SIREN'").fetchone()
print(f"market_snapshots: {'有' if ms else '无'}")

# 最近一轮热度榜全部
lr = conn.execute("SELECT MAX(round_number) FROM token_heat_history").fetchone()[0]
print(f"\n最近一轮 (round {lr}) 全部 token:")
top = conn.execute("SELECT token, score, mentions, unique_posts FROM token_heat_history WHERE round_number=? ORDER BY score DESC", (lr,)).fetchall()
for t in top:
    print(f"  {t['token']:12s} score={t['score']:6.1f} m={t['mentions']:3d} p={t['unique_posts']:2d}")

# SIREN 在 mentions 中有吗
mc = conn.execute("SELECT COUNT(*) as n FROM mentions WHERE token='SIREN'").fetchone()
print(f"\nmentions: {mc['n']} 条")

# SIREN 对应的 posts
if mc['n']:
    posts = conn.execute("SELECT p.content, p.posted_at FROM posts p JOIN mentions m ON m.post_id=p.post_id WHERE m.token='SIREN' ORDER BY p.posted_at DESC LIMIT 5").fetchall()
    for p in posts:
        print(f"  posted_at={p['posted_at']} content={p['content'][:80]}")

conn.close()
