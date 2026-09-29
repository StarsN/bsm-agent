import sys, json, subprocess
sys.path.insert(0, "/root/binance-monitor/bsm-agent")
from dashboard import get_macro_events, _okx

# Test 1: okx CLI accessible
r = subprocess.run(["okx", "--version"], capture_output=True, text=True)
print("okx version:", r.stdout.strip()[:100])

# Test 2: _okx with news
r = _okx(["news", "economic-calendar", "--region", "united_states", "--importance", "3", "--limit", "3"], live=True)
print("_okx result type:", type(r))
if isinstance(r, list):
    print("_okx result len:", len(r))
    if r: print("first event:", r[0].get("event", "N/A"))
else:
    print("_okx result:", r)

# Test 3: full get_macro_events
r = get_macro_events()
print("events count:", len(r.get("all_events", [])))
print("next_event:", r.get("next_event"))
