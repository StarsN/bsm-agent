import sqlite3, glob, os, json

# 1. All agent_short_candidates
db = '/root/binance-monitor/bsm-agent/db/agent_short.db'
conn = sqlite3.connect(db)
rows = conn.execute('SELECT id, token, passed, created_at FROM agent_short_candidates ORDER BY id').fetchall()
print('=== agent_short_candidates (入库时间) ===')
for r in rows:
    print('  id=%s  %s  passed=%s  created_at=%s' % (r[0], r[1], r[2], r[3]))
conn.close()

# 2. Journal schema
main_db = '/root/binance-monitor/bsm-agent/db/binance_square.db'
conn2 = sqlite3.connect(main_db)
conn2.row_factory = sqlite3.Row
cols = conn2.execute('PRAGMA table_info(journal)').fetchall()
print('\n=== journal table columns ===')
for c in cols:
    print('  %s (%s)' % (c[1], c[2]))

# 3. Journal for agent_short (find by order_id linking to trade_positions)
journals = conn2.execute(
    "SELECT j.* FROM journal j"
    " WHERE j.id IN (SELECT id FROM journal ORDER BY id DESC LIMIT 20)"
).fetchall()
print('\n=== journal latest 20 ===')
for r in journals:
    d = dict(r)
    print('  %s' % json.dumps(d, default=str))

# 4. worker_status
conn2.row_factory = sqlite3.Row
rows3 = conn2.execute('SELECT * FROM worker_status').fetchall()
print('\n=== worker_status ===')
for r in rows3:
    d = dict(r)
    print('  %s' % json.dumps(d, default=str))
conn2.close()

# 5. Log files
print('\n=== Log files ===')
for f in sorted(glob.glob('/root/binance-monitor/bsm-agent/logs/*.log')):
    size = os.path.getsize(f)
    print('  %s  (%d bytes)' % (f, size))

# 6. Search for collect/trigger in web.log
print('\n=== web.log: collect entries (last 20) ===')
try:
    with open('/root/binance-monitor/bsm-agent/logs/web.log', 'r') as f:
        for line in f:
            l = line.lower()
            if 'short-collect' in l or 'short_collect' in l or 'collect' in l:
                print('  %s' % line.rstrip()[-250:])
except Exception as e:
    print('  Error: %s' % e)

# 7. agent trigger entries
print('\n=== web.log: agent/trigger entries (last 20) ===')
try:
    with open('/root/binance-monitor/bsm-agent/logs/web.log', 'r') as f:
        for line in f:
            l = line.lower()
            if 'agent' in l and ('trigger' in l or 'flush' in l or 'execute' in l):
                print('  %s' % line.rstrip()[-250:])
except Exception as e:
    print('  Error: %s' % e)

# 8. trade_positions for agent_short
conn4 = sqlite3.connect(main_db)
conn4.row_factory = sqlite3.Row
positions = conn4.execute(
    "SELECT id, token, side, status, created_at FROM trade_positions WHERE strategy='agent_short' ORDER BY id DESC LIMIT 10"
).fetchall()
print('\n=== agent_short positions ===')
for p in positions:
    print('  %s' % json.dumps(dict(p), default=str))
conn4.close()

print('\nDONE')
