import subprocess, glob, os

# Search for short-collect in ALL log files
result = subprocess.run(
    ['grep', '-r', '-i', 'short-collect', '/root/binance-monitor/bsm-agent/logs/'],
    capture_output=True, text=True, timeout=10
)
print('=== grep -r short-collect logs/ ===')
for line in result.stdout.strip().split('\n'):
    print(line)
if not result.stdout.strip():
    print('  (empty - no matches)')

# All log files
print()
print('=== All log files ===')
for f in sorted(glob.glob('/root/binance-monitor/bsm-agent/logs/**', recursive=True)):
    print(' ', f)

# worker.log for short
result2 = subprocess.run(
    ['grep', '-i', 'short', '/root/binance-monitor/bsm-agent/logs/worker.log'],
    capture_output=True, text=True, timeout=10
)
print('\n=== worker.log: short entries ===')
lines = result2.stdout.strip().split('\n')
for line in lines[-20:]:
    print(line)

# Check processes
result3 = subprocess.run(
    ['ps', 'aux'],
    capture_output=True, text=True, timeout=5
)
print('\n=== web.py / hermes processes ===')
for line in result3.stdout.strip().split('\n'):
    if 'web.py' in line or 'hermes' in line:
        print(line)

# Check the market_data_short.json content
print('\n=== /tmp/market_data_short.json content ===')
try:
    with open('/tmp/market_data_short.json', 'r') as f:
        content = f.read()
        print(content[:2000])
except Exception as e:
    print('Error: %s' % e)

# Search ALL logs for short-collect with zgrep (for rotated)
result4 = subprocess.run(
    ['grep', '-r', 'short-collect', '/root/binance-monitor/'],
    capture_output=True, text=True, timeout=15
)
print('\n=== grep -r short-collect /root/binance-monitor/ (all) ===')
out = result4.stdout.strip()
if out:
    for line in out.split('\n')[:50]:
        print(line)
else:
    print('  (empty)')

# Also check systemd journal for web service
result5 = subprocess.run(
    ['journalctl', '-u', 'web', '--no-pager', '-n', '50'],
    capture_output=True, text=True, timeout=10
)
print('\n=== journalctl -u web (last 50) ===')
for line in result5.stdout.strip().split('\n'):
    print(line)
if not result5.stdout.strip():
    print('  (no web service or no output)')

# Check if web.py output goes to a different place
result6 = subprocess.run(
    ['journalctl', '--no-pager', '-n', '100', '|', 'grep', '-i', 'short-collect'],
    capture_output=True, text=True, timeout=10, shell=True
)
print('\n=== journalctl | grep short-collect ===')
for line in result6.stdout.strip().split('\n'):
    print(line)
if not result6.stdout.strip():
    print('  (empty)')
