"""
Clean up onchain pool: blacklist all tokens that have Binance spot.
Standalone — no project deps, runs directly on server.

Run: python /tmp/cleanup_spot_blacklist.py
"""
import json
import time
import sqlite3
import urllib.request
from urllib.error import HTTPError

SPOT_URL = "https://api.binance.com/api/v3/exchangeInfo"
ONCHAIN_DB = "/root/binance-monitor/bsm-agent/db/onchain.db"
CACHE_TTL = 3600

_spot_cache = {"ts": 0.0, "symbols": set()}


def get_spot_symbols():
    now = time.time()
    if now - _spot_cache["ts"] < CACHE_TTL and _spot_cache["symbols"]:
        return _spot_cache["symbols"]
    try:
        req = urllib.request.Request(SPOT_URL)
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[WARN] API failed: {e}")
        return _spot_cache["symbols"]
    symbols = set()
    for s in data.get("symbols", []):
        if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING":
            symbols.add(s.get("baseAsset", "").upper())
    _spot_cache["ts"] = now
    _spot_cache["symbols"] = symbols
    return symbols


def has_spot(token):
    return token.upper() in get_spot_symbols()


print("=== Loading spot symbols ===")
t0 = time.time()
spots = get_spot_symbols()
print(f"Loaded {len(spots)} spot symbols in {time.time()-t0:.2f}s")

# ── Read pool ──
conn = sqlite3.connect(ONCHAIN_DB)
conn.row_factory = sqlite3.Row

pool = conn.execute("SELECT token, pool_stage FROM onchain_pool").fetchall()
print(f"\n=== Pool: {len(pool)} tokens ===")
for row in pool:
    print(f"  {row['token']:12s} [{row['pool_stage']}]")

# ── Find spot tokens ──
spot_tokens = [row["token"] for row in pool if has_spot(row["token"])]
print(f"\n=== Tokens WITH spot (will blacklist): {len(spot_tokens)} ===")
for t in spot_tokens:
    print(f"  {t}")

if not spot_tokens:
    print("  (none) - pool already clean")
    conn.close()
    exit()

# ── Blacklist + archive ──
for token in spot_tokens:
    # Archive from pool
    conn.execute("""
        INSERT INTO onchain_pool_archive
        SELECT *, 'spot_cleanup', datetime('now') FROM onchain_pool WHERE token=?
    """, (token,))
    # Remove from pool
    conn.execute("DELETE FROM onchain_pool WHERE token=?", (token,))
    # Add to blacklist
    conn.execute("INSERT OR REPLACE INTO onchain_blacklist (token) VALUES (?)", (token,))
    # Add to removed
    conn.execute("INSERT OR REPLACE INTO onchain_removed (token, reason) VALUES (?, 'spot_cleanup')", (token,))
    print(f"  {token}: archived + blacklisted + removed")

conn.commit()

# ── Verify ──
remaining = conn.execute("SELECT token FROM onchain_pool").fetchall()
blacklist = conn.execute("SELECT token FROM onchain_blacklist").fetchall()
print(f"\n=== Done ===")
print(f"Pool remaining: {len(remaining)}")
print(f"Blacklist total: {len(blacklist)}")
conn.close()
