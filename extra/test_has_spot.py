"""
Test has_spot logic — standalone, no project deps.
Run on server: python /tmp/test_has_spot.py
"""
import json
import time
import urllib.request
from urllib.error import HTTPError

SPOT_URL = "https://api.binance.com/api/v3/exchangeInfo"
_CACHE = {"ts": 0.0, "symbols": set()}
_CACHE_TTL = 3600


def get_spot_symbols() -> set[str]:
    now = time.time()
    if now - _CACHE["ts"] < _CACHE_TTL and _CACHE["symbols"]:
        return _CACHE["symbols"]

    try:
        req = urllib.request.Request(SPOT_URL)
        req.add_header("User-Agent", "Mozilla/5.0")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[WARN] API failed: {e}, using stale cache ({len(_CACHE['symbols'])} symbols)")
        return _CACHE["symbols"]

    symbols = set()
    for s in data.get("symbols", []):
        if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING":
            symbols.add(s.get("baseAsset", "").upper())

    _CACHE["ts"] = now
    _CACHE["symbols"] = symbols
    return symbols


def has_spot(token: str) -> bool:
    return token.upper() in get_spot_symbols()


# ── Tests ──
errors = 0

# Test 1: Cold load
print("=== 1. Cold load ===")
t0 = time.time()
spots = get_spot_symbols()
dt = time.time() - t0
print(f"Loaded {len(spots)} spot symbols in {dt:.2f}s")

# Test 2: Cache hit
print("\n=== 2. Cache hit ===")
t0 = time.time()
spots2 = get_spot_symbols()
dt = time.time() - t0
print(f"Cache returned {len(spots2)} symbols in {dt:.4f}s (should be <0.001s)")

# Test 3: Known tokens
print("\n=== 3. has_spot() checks ===")
tests = [
    ("BTC", True, "major coin"),
    ("ETH", True, "major coin"),
    ("SOL", True, "major coin"),
    ("USDC", True, "stablecoin"),
    ("USDT", True, "stablecoin"),
    ("BNB", True, "major coin"),
    ("DOGE", True, "major coin"),
    ("XRP", True, "major coin"),
    ("PEPE", True, "popular memecoin"),
    ("WIF", True, "popular memecoin"),
]
for token, expect, note in tests:
    result = has_spot(token)
    ok = "OK" if result == expect else "FAIL"
    if result != expect:
        errors += 1
    print(f"  has_spot({token:6s}) = {result}  expect={expect}  [{ok}] ({note})")

# Test 4: Check onchain-relevant tokens
print("\n=== 4. Onchain candidate tokens ===")
onchain_tokens = ["XPL", "BASED", "GUA", "IN", "ASTER", "JTO", "USELESS",
                  "HYPE", "TRUMP", "FF", "EDGE", "IDOL", "ARX", "VVV", "LAB",
                  "VELVET", "POWER", "XAN", "BAS", "BTR", "PIEVERSE", "COLLECT",
                  "BTW", "ZEC", "AERO", "SLX", "COAI", "OPG", "PRL", "RAVE",
                  "POPCAT", "MET", "BILL", "BSB", "BLESS", "STABLE", "ROBO",
                  "LIGHT", "SENT"]
for token in onchain_tokens:
    result = has_spot(token)
    label = "HAS SPOT" if result else "no spot"
    print(f"  {token:10s} -> {label}")

# Test 5: Futures-only count (rough overlap)
print("\n=== 5. Summary ===")
print(f"Total spot symbols: {len(spots)}")
onchain_no_spot = sum(1 for t in onchain_tokens if not has_spot(t))
onchain_has_spot = sum(1 for t in onchain_tokens if has_spot(t))
print(f"Onchain tokens with NO spot: {onchain_no_spot}/{len(onchain_tokens)}")
print(f"Onchain tokens WITH spot:    {onchain_has_spot}/{len(onchain_tokens)}")
print(f"Errors: {errors}")
print("Done.")
