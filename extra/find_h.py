import subprocess, json, sys

def run_cmd(cmd, timeout=30):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip(), result.stderr.strip()

# Search for token H on multiple chains
for chain in ['solana', 'bsc', 'ethereum']:
    print('=== Search H on %s ===' % chain)
    out, err = run_cmd(['onchainos', 'token', 'search', '--query', 'H', '--chains', chain, '--limit', '5'])
    print(out[:2000])
    if err:
        print('STDERR:', err[:500])
    print()

# Also try the token "H" from the agent_short_candidates DB
# The DB shows token H from agent_short. Let's check if we have a contract address
import sqlite3
db = '/root/binance-monitor/bsm-agent/db/agent_short.db'
conn = sqlite3.connect(db)
rows = conn.execute(
    "SELECT token, data FROM agent_short_candidates WHERE token='H' ORDER BY id DESC LIMIT 1"
).fetchall()
print('=== agent_short_candidates for H ===')
for r in rows:
    try:
        d = json.loads(r[1])
        addr = d.get('token_address') or d.get('contract_address') or d.get('address', 'N/A')
        chain = d.get('chain', 'N/A')
        print('token=%s chain=%s address=%s' % (r[0], chain, addr))
        print('full data keys:', list(d.keys())[:20])
    except:
        print('token=%s raw=%s' % (r[0], r[1][:200]))
conn.close()
