import subprocess, sqlite3, sys, os, json

# 1. Test API save directly
print("=== 1. Testing API save ===")
result = subprocess.run(
    ['curl', '-s', '-X', 'POST', 'http://127.0.0.1:8000/api/trading/settings',
     '-H', 'Content-Type: application/json',
     '-d', '{"okx_data_source_enabled": true}'],
    capture_output=True, text=True, timeout=10
)
print('stdout:', result.stdout.strip())
print('stderr:', result.stderr.strip())
print('returncode:', result.returncode)

# 2. Check DB
print("\n=== 2. DB after save ===")
db = '/root/binance-monitor/bsm-agent/db/binance_square.db'
conn = sqlite3.connect(db)
rows = conn.execute(
    "SELECT key, value FROM trading_settings WHERE key='okx_data_source_enabled'"
).fetchall()
for r in rows:
    print('key=%s value=%s' % (r[0], r[1]))
if not rows:
    print('(not found in DB)')

# 3. Test trading_settings_get
print("\n=== 3. trading_settings_get ===")
sys.path.insert(0, '/root/binance-monitor/bsm-agent')
import storage
c = storage.get_conn()
s = storage.trading_settings_get(c)
print('okx_data_source_enabled =', s.get('okx_data_source_enabled'))
c.close()

# 4. Also verify config default
print("\n=== 4. Config default ===")
import config
print('config.OKX_DATA_SOURCE_ENABLED =', getattr(config, 'OKX_DATA_SOURCE_ENABLED', 'NOT_FOUND'))

# 5. Check web server port
print("\n=== 5. Web process ===")
result2 = subprocess.run(
    ['ps', 'aux'],
    capture_output=True, text=True, timeout=5
)
for line in result2.stdout.split('\n'):
    if 'web.py' in line:
        print(line)
