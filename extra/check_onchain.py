import sqlite3

db = '/root/binance-monitor/bsm-agent/db/binance_square.db'
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

# Check scanner status
print("=== onchain_scanner_status ===")
rows = conn.execute("SELECT * FROM onchain_scanner_status").fetchall()
for r in rows:
    print(dict(r))

# Check pool entries
print("\n=== onchain_pool ===")
rows = conn.execute("SELECT * FROM onchain_pool ORDER BY last_updated_at DESC LIMIT 10").fetchall()
for r in rows:
    print(dict(r))

# Check snapshots count and latest
print("\n=== onchain_snapshots stats ===")
total = conn.execute("SELECT COUNT(*) FROM onchain_snapshots").fetchone()[0]
print("Total snapshots: %d" % total)
distinct_rounds = conn.execute("SELECT COUNT(DISTINCT round_number) FROM onchain_snapshots").fetchone()[0]
print("Distinct rounds: %d" % distinct_rounds)
rows = conn.execute("SELECT token, round_number, effective_concentration, holder_same_fund_pct, divergence_score, snapshot_at FROM onchain_snapshots ORDER BY id DESC LIMIT 10").fetchall()
for r in rows:
    print(dict(r))

# Check LP snapshots
print("\n=== onchain_lp_snapshots (last 5) ===")
rows = conn.execute("SELECT * FROM onchain_lp_snapshots ORDER BY id DESC LIMIT 5").fetchall()
for r in rows:
    print(dict(r))

# Search for odd tokens
print("\n=== Search for non-alphanumeric tokens ===")
for table in ['onchain_pool', 'onchain_snapshots']:
    try:
        rows = conn.execute(
            "SELECT DISTINCT token FROM %s WHERE token NOT GLOB '[A-Z0-9]*' OR length(token) > 15" % table
        ).fetchall()
        if rows:
            print("%s odd tokens: %s" % (table, [r[0] for r in rows]))
        else:
            print("%s: all tokens look normal" % table)
    except Exception as e:
        print("%s: error %s" % (table, e))

# Show all unique tokens in pool and snapshots
print("\n=== All unique tokens in onchain_pool ===")
rows = conn.execute("SELECT token, pool_stage FROM onchain_pool").fetchall()
for r in rows:
    print("  %s (%s)" % (r["token"], r["pool_stage"]))

conn.close()
