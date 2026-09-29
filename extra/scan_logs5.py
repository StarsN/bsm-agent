import subprocess, glob, os, datetime

log_dir = '/root/binance-monitor/bsm-agent/logs'

# Find log files from last 5 days
all_files = []
for root, dirs, files in os.walk(log_dir):
    for f in files:
        if f.endswith('.log'):
            path = os.path.join(root, f)
            mtime = os.path.getmtime(path)
            all_files.append((mtime, path))
all_files.sort(reverse=True)
print('=== Log files by modification time (last 5 days) ===')
cutoff = datetime.datetime.now().timestamp() - 5*86400
for mtime, path in all_files:
    if mtime > cutoff:
        dt = datetime.datetime.fromtimestamp(mtime).strftime('%m-%d %H:%M')
        size = os.path.getsize(path)
        print('  %s  %s bytes  %s' % (dt, size, path))

print()

# Search for errors/warnings across all logs from last 5 days
recent_files = []
for mtime, path in all_files:
    if mtime > cutoff:
        recent_files.append(path)

if not recent_files:
    print('No recent log files found')
else:
    patterns = [
        ('ERROR|error|Error|exception|Exception|Traceback', 'Errors/Exceptions'),
        ('WARNING|warning|Warning', 'Warnings'),
        ('database is locked', 'DB Locked'),
        ('timeout|Timeout|timed out', 'Timeouts'),
        ('failed|Failed|FAIL', 'Failures'),
        ('504|502|503|500', 'HTTP 5xx'),
        ('trap int3|TargetClosedError|Target closed', 'Chromium crashes'),
        ('Invalid HTTP request', 'Invalid HTTP'),
        ('入库失败', 'Insert CN failed'),
    ]

    for pattern, label in patterns:
        result = subprocess.run(
            ['grep', '-c', '-E', pattern] + recent_files,
            capture_output=True, text=True, timeout=30
        )
        total = 0
        details = []
        for line in result.stdout.strip().split('\n'):
            if ':' in line:
                fname, count = line.rsplit(':', 1)
                c = int(count)
                total += c
                if c > 0:
                    short = fname.replace(log_dir + '/', '')
                    details.append('%s: %s' % (short, c))
        if total > 0:
            print('[%s] Total: %d' % (label, total))
            for d in details[:10]:
                print('  %s' % d)
            print()

    # Also show the actual error lines (last 40 unique)
    print('=== Last 40 error/warning lines (unique) ===')
    result2 = subprocess.run(
        ['grep', '-h', '-E', 'ERROR|error|Error|exception|Exception|WARNING|warning|database is locked|入库失败|timeout|Timeout|failed|Failed|504|502|503|trap int3|TargetClosedError|Invalid HTTP'] + recent_files,
        capture_output=True, text=True, timeout=30
    )
    lines = result2.stdout.strip().split('\n')
    seen = set()
    unique = []
    for line in lines:
        key = line[:120]
        if key not in seen:
            seen.add(key)
            unique.append(line)
    for line in unique[-40:]:
        print(line.rstrip()[:250])

# Also check per-day breakdown
print()
print('=== Per-day summary ===')
for mtime, path in all_files:
    if mtime > cutoff:
        dt = datetime.datetime.fromtimestamp(mtime).strftime('%m-%d')
        size = os.path.getsize(path)
        short = path.replace(log_dir + '/', '')
        # Count lines
        try:
            with open(path, 'r') as f:
                lines = sum(1 for _ in f)
        except:
            lines = 0
        # Count errors
        result = subprocess.run(
            ['grep', '-c', '-E', 'ERROR|error|Error|exception|database is locked|入库失败|failed|timeout|504|502|trap int3|TargetClosedError|Invalid HTTP'],
            capture_output=True, text=True, timeout=15
        )
        err_count = result.stdout.strip()
        if err_count and err_count.isdigit():
            err_count = int(err_count)
        else:
            err_count = 0
        print('  %s  %s  %s lines  %s errors' % (dt, short, lines, err_count))
