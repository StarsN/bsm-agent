import subprocess, json

def run(cmd, timeout=30):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate(timeout=timeout)
    return out.strip(), err.strip()

addr = '0xcce5f304fd043d6a4e8ccb5376a4a4fb583b98d5'
chain = 'bsc'

print("ALLO (Allora) on BSC: %s" % addr)
print("holders=3337 mc=$7.7M")
print("=" * 60)

# 1. cluster-overview
print("\n=== 1. cluster-overview ===")
out, err = run(['onchainos', 'token', 'cluster-overview', '--address', addr, '--chain', chain])
try:
    data = json.loads(out)
    if data.get('ok'):
        for k, v in data['data'].items():
            print("  %s: %s" % (k, v))
except:
    print(out[:1500])

# 2. cluster-list with analysis
print("\n=== 2. cluster-list (top + filtering) ===")
out, err = run(['onchainos', 'token', 'cluster-list', '--address', addr, '--chain', chain])
try:
    data = json.loads(out)
    if data.get('ok'):
        clusters = data['data'].get('clusterList', [])
        total_holding = 0
        real_clusters = 0
        exchange_clusters = 0
        buy_trend = 0
        multi_addr = 0
        large_clusters = 0  # >2%

        for c in clusters:
            pct = float(c.get('holdingPercent', 0))
            total_holding += pct
            addrs = c.get('clusterAddressList', [])
            has_ex = any(a.get('isExchange') for a in addrs)
            trend = str(c.get('trendType', []))

            if has_ex: exchange_clusters += 1
            if not has_ex: real_clusters += 1
            if 'buy' in trend.lower(): buy_trend += 1
            if len(addrs) > 1: multi_addr += 1
            if pct > 2: large_clusters += 1

        print("  Top 10 clusters:")
        for i, c in enumerate(clusters[:10]):
            pct = float(c.get('holdingPercent', 0))
            addrs = c.get('clusterAddressList', [])
            has_ex = any(a.get('isExchange') for a in addrs)
            has_ct = any(a.get('isContract') for a in addrs)
            trend = c.get('trendType', [])
            pnl = str(c.get('pnlPercent', '0'))[:10]

            flags = []
            if has_ex: flags.append('EX')
            if has_ct: flags.append('CT')
            flag = ' [' + ','.join(flags) + ']' if flags else ''
            print("  C%d: %.2f%% | %d addr | trend=%s | pnl=%s%%%s" % (
                i+1, pct, len(addrs), trend, pnl, flag))
            for a in addrs[:3]:
                print("       rank=%s %.3f%% ex=%s ct=%s kol=%s | %s" % (
                    a.get('addressRank'), float(a.get('holdingPercent', 0)),
                    a.get('isExchange'), a.get('isContract'), a.get('isKol'),
                    a.get('address', '')[:14]))
            if len(addrs) > 3:
                print("       ... +%d more" % (len(addrs) - 3))

        print("\n  --- Summary ---")
        print("  Total clusters: %d" % len(clusters))
        print("  Sum holdings: %.1f%%" % (total_holding * 100 if total_holding < 1 else total_holding))
        print("  Non-exchange clusters: %d" % real_clusters)
        print("  Exchange-tagged clusters: %d" % exchange_clusters)
        print("  Buy-trend clusters: %d" % buy_trend)
        print("  Multi-address (>1): %d" % multi_addr)
        print("  Large clusters (>2%% each): %d" % large_clusters)
except Exception as e:
    print("Parse error: %s" % e)
    print(out[:2000])

# 3. advanced-info
print("\n=== 3. advanced-info ===")
out, err = run(['onchainos', 'token', 'advanced-info', '--address', addr, '--chain', chain])
try:
    data = json.loads(out)
    if data.get('ok'):
        d = data['data']
        for k in ['top10HoldPercent', 'devHoldingPercent', 'bundleHoldingPercent',
                   'suspiciousHoldingPercent', 'sniperHoldingPercent',
                   'riskControlLevel', 'tokenTags', 'lpBurnedPercent']:
            print("  %s: %s" % (k, d.get(k, '')))
except:
    print(out[:1000])

# 4. holders top 10 with funding source
print("\n=== 4. top-10 holders (funding source) ===")
out, err = run(['onchainos', 'token', 'holders', '--address', addr, '--chain', chain, '--limit', '10'])
try:
    data = json.loads(out)
    if data.get('ok'):
        for h in data['data']:
            fs = h.get('fundingSource', '') or '(empty)'
            if len(fs) > 34: fs = fs[:34] + '...'
            print("  %s %.3f%% | fund=%s" % (
                h.get('holderWalletAddress', '')[:14],
                float(h.get('holdPercent', 0)),
                fs))
except:
    print(out[:800])

# 5. cluster-top-holders aggregate
print("\n=== 5. top-100 aggregate behavior ===")
out, err = run(['onchainos', 'token', 'cluster-top-holders', '--address', addr, '--chain', chain, '--range-filter', '3'])
try:
    data = json.loads(out)
    if data.get('ok'):
        d = data['data']
        for k, v in d.items():
            print("  %s: %s" % (k, v))
except:
    print(out[:1000])
