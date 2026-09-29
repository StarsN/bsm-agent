import subprocess, sqlite3, re

# Get short-collect log entries
result = subprocess.run(
    ['grep', '-i', 'short-collect', '/root/binance-monitor/bsm-agent/logs/web.log'],
    capture_output=True, text=True, timeout=10
)
print('=== [short-collect] log entries ===')
for line in result.stdout.strip().split('\n'):
    print(line)

# DB schema
db = '/root/binance-monitor/bsm-agent/db/agent_short.db'
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
cols = conn.execute("PRAGMA table_info(agent_short_candidates)").fetchall()
print('\n=== agent_short_candidates schema ===')
for c in cols:
    print('  %s (%s)' % (c[1], c[2]))

# All entries with round_number
rows = conn.execute(
    'SELECT id, token, round_number, created_at FROM agent_short_candidates ORDER BY id'
).fetchall()
print('\n=== All entries with round_number ===')
for r in rows:
    print('  id=%s  token=%s  round=%s  created_at=%s' % (r['id'], r['token'], r['round_number'], r['created_at']))

# Check if extract was actually run by hermes
result2 = subprocess.run(
    ['grep', '-i', 'extract_market_data', '/root/binance-monitor/bsm-agent/logs/web.log'],
    capture_output=True, text=True, timeout=10
)
print('\n=== extract_market_data references in web.log ===')
for line in result2.stdout.strip().split('\n'):
    print(line)

# Check hermes logs if any
import glob
for f in sorted(glob.glob('/root/.hermes/logs/*.log')):
    print('\n=== hermes log: %s ===' % f)
    try:
        with open(f, 'r') as fh:
            content = fh.read()[-5000:]
            print(content[-3000:])
    except:
        pass

# Also check /tmp for extract output
result3 = subprocess.run(
    ['ls', '-la', '/tmp/market_data_short.json'],
    capture_output=True, text=True, timeout=5
)
print('\n=== /tmp/market_data_short.json ===')
print(result3.stdout.strip())

conn.close()
