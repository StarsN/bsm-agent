"""
Test market cap + smart money fetch and merge logic.
Usage: python extra/test_market_cap_sm.py
"""
import sys, json, os, subprocess

# Fix Windows: Python subprocess can't find okx.ps1 directly
def _okx_wrapper(args, live=False):
    cmd = "okx"
    if live:
        cmd += " --profile live"
    cmd += " " + " ".join(args) + " --json"
    try:
        r = subprocess.run(["powershell", "-Command", cmd],
                           capture_output=True, text=True, timeout=20)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        out = r.stdout.strip()
        for i, ch in enumerate(out):
            if ch in ('{', '['):
                return json.loads(out[i:])
        return None
    except Exception:
        return None

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pass_count = 0
fail_count = 0

def check(name, ok, detail=""):
    global pass_count, fail_count
    if ok:
        pass_count += 1
        print(f"  [PASS] {name}")
    else:
        fail_count += 1
        print(f"  [FAIL] {name}  {detail}")


# ============================================================
# Test 1: Market cap CLI structure
# ============================================================
print("=" * 60)
print("Test 1: Market cap CLI structure")
print("=" * 60)

mc_data = _okx_wrapper(["market", "filter", "--instType", "SPOT",
    "--sortBy", "marketCapUsd", "--sortOrder", "desc",
    "--limit", "10", "--quoteCcy", "USDT"])
check("returns list", isinstance(mc_data, list), f"type={type(mc_data)}")
if mc_data and isinstance(mc_data, list):
    check("mc_data[0] exists", len(mc_data) > 0)
    rows = mc_data[0].get("rows", [])
    check("rows field exists", len(rows) >= 10, f"got {len(rows)}")
    if rows:
        row = rows[0]
        check("baseCcy is str", isinstance(row.get("baseCcy"), str))
        check("marketCapUsd > 0", float(row.get("marketCapUsd", 0)) > 0,
              f"value={row.get('marketCapUsd')}")
        check("rank > 0", int(row.get("rank", 0)) > 0)


# ============================================================
# Test 2: Smart money CLI structure
# ============================================================
print(f"\n{'=' * 60}")
print("Test 2: Smart money CLI structure")
print("=" * 60)

sm_data = _okx_wrapper(["smartmoney", "signal-overview-by-filter",
    "--topInstruments", "10", "--period", "7"], live=True)
check("returns dict", isinstance(sm_data, dict), f"type={type(sm_data)}")
if sm_data and isinstance(sm_data, dict):
    items = sm_data.get("data", [])
    check("data is list", isinstance(items, list))
    check("data not empty", len(items) > 0, f"got {len(items)}")
    if items:
        item = items[0]
        lsr = item.get("longShortRatio", {})
        nt = item.get("notional", {})
        wr = item.get("winRate", {})
        check("ccy exists", isinstance(item.get("ccy"), str))
        check("longRatio 0-1", 0 <= float(lsr.get("longRatio", -1)) <= 1)
        check("netNotionalUsdt exists", "netNotionalUsdt" in nt)
        check("avgLongWinRate 0-1", 0 <= float(wr.get("avgLongWinRate", -1)) <= 1)
        check("tradersWithPosition > 0", int(item.get("tradersWithPosition", 0)) > 0)


# ============================================================
# Test 3: Logic test — simulate the merge code from get_multi_token_okx_metrics
# ============================================================
print(f"\n{'=' * 60}")
print("Test 3: Merge logic simulation (market cap + smart money)")
print("=" * 60)

# Use Test 1+2 raw data to simulate the merge logic that runs inside the function
test_tokens = ["BTC", "ETH"]
result = {t: {} for t in test_tokens}

# --- Market cap merge (simulated from Test 1 data) ---
if mc_data and isinstance(mc_data, list):
    for row in mc_data[0].get("rows", []):
        t = row.get("baseCcy", "").upper()
        if t in result:
            result[t]["market_cap_usd"] = float(row.get("marketCapUsd", 0))
            result[t]["market_cap_rank"] = int(row.get("rank", 0))
check("BTC market_cap_usd merged", result["BTC"].get("market_cap_usd", 0) > 0)
check("ETH market_cap_usd merged", result["ETH"].get("market_cap_usd", 0) > 0)

# --- Smart money merge (simulated from Test 2 data) ---
if sm_data and isinstance(sm_data, dict):
    sm_items = sm_data.get("data", [])
    if isinstance(sm_items, list):
        for item in sm_items:
            t = item.get("ccy", "").upper()
            if t in result:
                lsr = item.get("longShortRatio", {})
                nt = item.get("notional", {})
                wr = item.get("winRate", {})
                result[t].update({
                    "sm_long_ratio": float(lsr.get("longRatio", 0)),
                    "sm_net_notional_usdt": float(nt.get("netNotionalUsdt", 0)),
                    "sm_long_avg_entry": float(nt.get("smartMoneyLongAvgEntry", 0)),
                    "sm_short_avg_entry": float(nt.get("smartMoneyShortAvgEntry", 0)),
                    "sm_avg_long_win_rate": float(wr.get("avgLongWinRate", 0)),
                    "sm_avg_short_win_rate": float(wr.get("avgShortWinRate", 0)),
                    "sm_traders_with_position": int(item.get("tradersWithPosition", 0)),
                })
sm_tokens = [t for t in result if result[t].get("sm_long_ratio") is not None]
check(f"smart money merged to {len(sm_tokens)} tokens", len(sm_tokens) > 0,
      f"tokens={list(result.keys())}")

check("returns dict", isinstance(result, dict))
check("has 2 tokens", len(result) == 2, f"got {len(result)}")

for token in test_tokens:
    print(f"\n  [{token}]")
    data = result.get(token, {})

    # Market cap
    check(f"  market_cap_usd > 0", data.get("market_cap_usd", 0) > 0,
          f"value={data.get('market_cap_usd')}")
    check(f"  market_cap_rank > 0", data.get("market_cap_rank", 0) > 0,
          f"value={data.get('market_cap_rank')}")

    # Smart money
    check(f"  sm_long_ratio 0-1", 0 <= data.get("sm_long_ratio", -1) <= 1,
          f"value={data.get('sm_long_ratio')}")
    check(f"  sm_net_notional_usdt exists", "sm_net_notional_usdt" in data)
    check(f"  sm_traders_with_position > 0",
          data.get("sm_traders_with_position", 0) > 0,
          f"value={data.get('sm_traders_with_position')}")
    check(f"  sm_avg_long_win_rate 0-1",
          0 <= data.get("sm_avg_long_win_rate", -1) <= 1)


# ============================================================
# Test 4: Simulate worker.py merge logic
# ============================================================
print(f"\n{'=' * 60}")
print("Test 4: Simulate worker.py merge")
print("=" * 60)

btc_data = result.get("BTC", {})
snap = {"token": "BTC", "mark_price": 75000.0, "funding_rate_pct": None}
check("BTC has sm data", btc_data.get("sm_long_ratio") is not None)
extra = btc_data
for k, v in extra.items():
    if k not in snap or snap.get(k) is None:
        snap[k] = v

check("market_cap_usd merged", snap.get("market_cap_usd", 0) > 0)
check("sm_long_ratio merged", isinstance(snap.get("sm_long_ratio"), float))
check("mark_price preserved", snap["mark_price"] == 75000.0)


# ============================================================
# Test 5: Error tolerance
# ============================================================
print(f"\n{'=' * 60}")
print("Test 5: Error tolerance")
print("=" * 60)

bad = _okx_wrapper(["market", "filter", "--instType", "INVALID", "--json"])
check("invalid args not crash", bad is None or isinstance(bad, (dict, list)),
      f"type={type(bad)}")

# Simulate: non-existent tokens not in market cap / smart money results
fake_result = {"NOEXIST123": {}, "FAKETOKEN456": {}}
for row in (mc_data[0].get("rows", []) if mc_data and isinstance(mc_data, list) else []):
    t = row.get("baseCcy", "").upper()
    if t in fake_result:
        fake_result[t]["market_cap_usd"] = float(row.get("marketCapUsd", 0))
if sm_data and isinstance(sm_data, dict):
    for item in sm_data.get("data", []):
        t = item.get("ccy", "").upper()
        if t in fake_result:
            fake_result[t]["sm_long_ratio"] = 0.5
check("non-existent tokens not crash", isinstance(fake_result, dict) and len(fake_result) == 2)
for t, d in fake_result.items():
    check(f"  {t} no market_cap_usd", "market_cap_usd" not in d)
    check(f"  {t} no sm_long_ratio", "sm_long_ratio" not in d)


# ============================================================
print(f"\n{'=' * 60}")
print(f"Result: {pass_count} PASS / {fail_count} FAIL  (total {pass_count + fail_count})")
print("=" * 60)

sys.exit(0 if fail_count == 0 else 1)
