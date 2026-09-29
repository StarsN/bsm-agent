import subprocess, os

log_dir = '/root/binance-monitor/bsm-agent/logs/worker'

# Check what files exist
print('=== Worker log files ===')
for f in sorted(os.listdir(log_dir)):
    path = os.path.join(log_dir, f)
    size = os.path.getsize(path)
    print('  %s: %s bytes' % (f, size))

print()

for date in ['20260612', '20260613']:
    path = os.path.join(log_dir, date + '.log')
    if not os.path.exists(path):
        print('=== %s: FILE NOT FOUND ===' % date)
        continue

    # Count total lines
    with open(path, 'r') as f:
        lines = f.readlines()
    total_lines = len(lines)
    print('=== %s: %d total lines ===' % (date, total_lines))

    # Count by category
    categories = {
        'Page.goto timeout': ['Page.goto.*[Tt]imeout', 'Page.goto: Timeout'],
        'TargetClosedError': ['TargetClosedError', 'Target closed'],
        'trap int3': ['trap int3'],
        'database is locked': ['database is locked'],
        'scraper error': ['scraper.*出错', 'scraper.*error', 'scraper.*Error'],
        'round timeout >420s': ['本轮超时'],
        'fetch failed': ['fetch.*fail', 'Failed.*fetch', 'HTTP.*fail'],
        '入库失败': ['入库失败'],
        'hermes timeout': ['hermes.*timed out', 'TimeoutExpired.*hermes'],
        'HTTP 5xx': ['HTTP 50[0-9]', 'HTTP 5..'],
        '0 posts captured': ['本轮捕获 0 条'],
        '0 tokens heat': ['候选币池: 0', '15 分钟榜代币数: 0'],
        'Snapshot failed': ['snapshot.*fail', '快照.*失败'],
    }

    for label, patterns in categories.items():
        count = 0
        for line in lines:
            for p in patterns:
                import re
                if re.search(p, line):
                    count += 1
                    break
        if count > 0:
            print('  [%s]: %d' % (label, count))

    # Round stats
    round_starts = 0
    round_ends = 0
    total_time = 0
    timeouts = 0
    round_times = []
    import re
    for line in lines:
        if '开始抓取轮' in line:
            round_starts += 1
        if '总耗时' in line:
            m = re.search(r'总耗时 (\d+)s', line)
            if m:
                t = int(m.group(1))
                round_times.append(t)
                total_time += t
            round_ends += 1
        if '本轮超时' in line:
            timeouts += 1

    if round_times:
        avg_time = sum(round_times) / len(round_times)
        max_time = max(round_times)
        min_time = min(round_times)
        print()
        print('  Rounds: %d started, %d completed, %d timeouts' % (round_starts, round_ends, timeouts))
        print('  Round time: avg=%ds, min=%ds, max=%ds' % (int(avg_time), min_time, max_time))

    # Show all unique scraper errors
    print()
    scraper_errors = set()
    for line in lines:
        if 'scraper' in line.lower() and ('出错' in line or 'error' in line.lower() or 'timeout' in line.lower()):
            scraper_errors.add(line.strip()[:200])
    if scraper_errors:
        print('  Scraper errors:')
        for e in sorted(scraper_errors):
            print('    %s' % e)

    # Show all database locked lines
    db_locked = []
    for line in lines:
        if 'database is locked' in line:
            db_locked.append(line.strip()[:200])
    if db_locked:
        print()
        print('  Database locked: %d occurrences' % len(db_locked))
        # Show unique tokens
        tokens = set()
        for l in db_locked:
            m = re.search(r'(\w+) 入库失败', l)
            if m:
                tokens.add(m.group(1))
        if tokens:
            print('    Affected tokens: %s' % ', '.join(sorted(tokens)[:20]))

    print()
