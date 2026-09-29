import subprocess, json

# First, get the token H contract address from agent_short DB
import sqlite3
db = '/root/binance-monitor/bsm-agent/db/agent_short.db'
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT id, token, data, created_at FROM agent_short_candidates WHERE token='H' ORDER BY id DESC LIMIT 3"
).fetchall()
print('=== agent_short_candidates for H ===')
for r in rows:
    d = json.loads(r['data'])
    print('id=%s token=%s created_at=%s' % (r['id'], r['token'], r['created_at']))
    # Print relevant address fields
    for k in ['token_address', 'contract_address', 'address', 'chain', 'mark_price', 'symbol']:
        if k in d:
            print('  %s: %s' % (k, d[k]))
conn.close()

# Try a known interesting token for cluster demo
# Let's use the BSC H token: 0x24ff2555210ad72ea26246f0b8c363401cc34444
# Or 0x44f161ae29361e332dea039dfa2f404e0bc5b5cc (Humanity)
addr = '0x44f161ae29361e332dea039dfa2f404e0bc5b5cc'
chain = 'bsc'

print('\n=== Step 1: cluster-overview for %s ===' % addr)
out, err = subprocess.Popen(
    ['onchainos', 'token', 'cluster-overview', '--address', addr, '--chain', chain],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
).communicate(timeout=30)
print(out[:3000])
if err:
    print('ERR:', err[:500])

print('\n=== Step 1b: cluster-list (first 2 clusters) ===')
out2, err2 = subprocess.Popen(
    ['onchainos', 'token', 'cluster-list', '--address', addr, '--chain', chain],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
).communicate(timeout=30)
# Only show first part
lines = out2.split('\n')
for line in lines[:80]:
    print(line)

print('\n=== Step 1c: top-10 holders ===')
out3, err3 = subprocess.Popen(
    ['onchainos', 'token', 'holders', '--address', addr, '--chain', chain, '--limit', '10'],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
).communicate(timeout=30)
print(out3[:3000])
if err3:
    print('ERR:', err3[:500])

print('\n=== Step 1d: advanced-info ===')
out4, err4 = subprocess.Popen(
    ['onchainos', 'token', 'advanced-info', '--address', addr, '--chain', chain],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
).communicate(timeout=30)
print(out4[:2000])
if err4:
    print('ERR:', err4[:500])
