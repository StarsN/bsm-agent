import sqlite3, subprocess

db = '/root/binance-monitor/bsm-agent/db/binance_square.db'
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

# 1. Check trading_settings for okx
rows = conn.execute("SELECT * FROM trading_settings WHERE key LIKE '%okx%' OR key LIKE '%data_source%'").fetchall()
print("=== trading_settings (okx/data_source keys) ===")
for r in rows:
    print(dict(r))

# 2. Check worker_status
rows2 = conn.execute("SELECT key, data_source, stage FROM worker_status").fetchall()
print("\n=== worker_status ===")
for r in rows2:
    print(dict(r))

# 3. Check config.py for OKX settings
print("\n=== grep config.py for OKX ===")
r = subprocess.run(['grep', '-i', 'okx', '/root/binance-monitor/bsm-agent/config.py'], capture_output=True, text=True)
print(r.stdout.strip())

# 4. Check if config.py has OKX_DATA_SOURCE_ENABLED
print("\n=== grep config.py for OKX_DATA_SOURCE ===")
r2 = subprocess.run(['grep', 'OKX_DATA_SOURCE', '/root/binance-monitor/bsm-agent/config.py'], capture_output=True, text=True)
print(r2.stdout.strip())

conn.close()
