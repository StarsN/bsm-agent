#!/usr/bin/env python3
"""Trap detection helper for agent-trade candidates.

Reads /tmp/market_data.json, prints each candidate with auto-flagged traps.
Importable: `detect_traps(candidate)` returns list of flag strings.
"""

import json

def sf(v, fmt=".2f"):
    if v is None: return "-"
    if isinstance(v, str):
        try: v = float(v)
        except ValueError: return v
    return f"{v:{fmt}}"

def detect_traps(c):
    """Return list of trap flag strings for a candidate dict."""
    flags = []
    taker = c.get('taker')
    taker_pct = c.get('taker_pct')
    chg_1h = c.get('1h')
    chg_24h = c.get('24h')
    chg_48h = c.get('chg_48h')
    chg_15m = c.get('15m')
    chg_4h = c.get('4h')
    oi_15m = c.get('oi_15m')
    oi_1h = c.get('oi_1h')
    oi_4h = c.get('oi_4h')
    oi_48h = c.get('oi_48h')
    funding = c.get('funding')
    depth_bid = c.get('depth_bid')
    depth_ask = c.get('depth_ask')
    spread = c.get('spread')

    # Trap 1: Strong taker + price not moving (buy_pressure_faded)
    if all(v is not None for v in [taker, taker_pct, chg_1h, chg_24h]):
        if taker > 1.5 and taker_pct > 60 and (chg_1h is None or chg_1h < 1) and (chg_24h is None or chg_24h < 5):
            flags.append("T1:buy_pressure_faded")

    # Trap 2: OI rising + price falling (OI reversed)
    if oi_15m is not None and chg_15m is not None and oi_15m > 2 and chg_15m <= 0:
        flags.append("T2:oi15_reversed")
    if oi_1h is not None and chg_1h is not None and oi_1h > 2 and chg_1h <= 0:
        flags.append("T2:oi1h_reversed")
    if oi_4h is not None and chg_4h is not None and oi_4h > 2 and chg_4h <= 0:
        flags.append("T2:oi4h_reversed")

    # Trap 3: High funding + high price
    if funding is not None and chg_48h is not None:
        if funding > 0.1 and chg_48h > 10:
            flags.append("T3:high_funding_chase")
        if funding < -0.5:
            flags.append("T3:extreme_neg_funding")

    # Trap 4: Thin book / wide spread
    if depth_bid is not None and depth_ask is not None:
        if depth_bid < 100000 or depth_ask < 100000:
            flags.append("T4:thin_book")
    if spread is not None and spread > 0.05:
        flags.append("T4:wide_spread")

    # Agent lesson: chase threshold
    if chg_48h is not None and chg_48h > 18:
        flags.append("lesson:48h>18%_chase")
    if chg_24h is not None and chg_24h > 15:
        flags.append("lesson:24h>15%_chase")

    # Agent lesson: OI growth >> price growth (distribution pattern, from HYPE)
    if oi_48h is not None and chg_48h is not None and chg_48h > 0:
        if oi_48h / max(chg_48h, 0.1) > 2.5:
            flags.append("lesson:OI_>>_price_(distribution)")

    return flags


def main():
    with open("/tmp/market_data.json") as f:
        d = json.load(f)

    # Identify positions
    positions = d.get("positions", [])
    journal = d.get("today_journal", [])
    print("=== POSITION MAPPING ===")
    token_map = {}
    for i, p in enumerate(positions):
        entry = p.get('entry_price')
        found = False
        for j in journal:
            jp = j.get('price')
            if jp and abs(entry - jp) < 0.001:
                token_map[i] = j.get('token')
                print(f"  pos[{i}]: *** @ {entry} -> {j.get('token')}")
                found = True
                break
        if not found:
            print(f"  pos[{i}]: *** @ {entry} -> UNKNOWN")

    # Detect traps per candidate
    candidates = d.get("candidates", [])
    print(f"\n=== {len(candidates)} CANDIDATES ===")
    for c in candidates:
        token = c.get('token', '***')
        price = c.get('price')
        analysis = c.get('verdict', '-')
        flags = detect_traps(c)
        
        print(f"\n  [{token:6s}] ${sf(price, '.4f'):>10s} | analysis: {analysis}")
        print(f"    chg: 15m={sf(c.get('15m'))}% 1h={sf(c.get('1h'))}% 4h={sf(c.get('4h'))}% 24h={sf(c.get('24h'))}% 48h={sf(c.get('chg_48h'))}%")
        print(f"    OI:  15m={sf(c.get('oi_15m'))}% 1h={sf(c.get('oi_1h'))}% 4h={sf(c.get('oi_4h'))}% 48h={sf(c.get('oi_48h'))}%")
        print(f"    Taker={sf(c.get('taker'))} pct={sf(c.get('taker_pct'))}% trend={c.get('taker_trend')} | Funding={sf(c.get('funding'), '.4f')}%")
        print(f"    Depth: bid={sf(c.get('depth_bid'), '.0f')} ask={sf(c.get('depth_ask'), '.0f')} spread={sf(c.get('spread'), '.4f')}%")
        if flags:
            print(f"    🚩 FLAGS: {' | '.join(flags)}")
        else:
            print(f"    ✅ NO TRAPS")


if __name__ == "__main__":
    main()
