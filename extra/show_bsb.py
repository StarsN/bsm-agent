import subprocess, json

def run(cmd, timeout=30):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate(timeout=timeout)
    return out.strip(), err.strip()

addr = '0x595deaad1eb5476ff1e649fdb7efc36f1e4679cc'
chain = 'bsc'

print("BSB (Block Street) on BSC: %s" % addr)
print("holders=58273 mc=$25.4M +75% communityRecognized")
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

# 2. cluster-list
print("\n=== 2. cluster-list ===")
out, err = run(['onchainos', 'token', 'cluster-list', '--address', addr, '--chain', chain])
try:
    data = json.loads(out)
    if data.get('ok'):
        clusters = data['data'].get('clusterList', [])
        total_holding = 0
        real = 0; ex = 0; ct = 0; buy = 0; multi = 0; large = 0

        for c in clusters:
            pct = float(c.get('holdingPercent', 0))
            total_holding += pct
            addrs = c.get('clusterAddressList', [])
            has_ex = any(a.get('isExchange') for a in addrs)
            has_ct = any(a.get('isContract') for a in addrs)
            trend = str(c.get('trendType', []))
            if not has_ex: real += 1
            if has_ex: ex += 1
            if has_ct: ct += 1
            if 'buy' in trend.lower(): buy += 1
            if len(addrs) > 1: multi += 1
            if pct > 1: large += 1

        print("  Top 10:")
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
            print("  C%d: %.4f%% | %d addr | trend=%s | pnl=%s%%%s" % (
                i+1, pct, len(addrs), trend, pnl, flag))
            for a in addrs[:3]:
                print("       rank=%s %.4f%% ex=%s ct=%s kol=%s | %s" % (
                    a.get('addressRank'), float(a.get('holdingPercent', 0)),
                    a.get('isExchange'), a.get('isContract'), a.get('isKol'),
                    a.get('address', '')[:14]))
            if len(addrs) > 3:
                print("       ... +%d" % (len(addrs) - 3))

        print("\n  --- Summary ---")
        print("  Total clusters: %d | Sum: %.1f%%" % (len(clusters), total_holding))
        print("  Non-exchange: %d | Exchange: %d | Contract: %d" % (real, ex, ct))
        print("  Buy-trend: %d | Multi-addr: %d | >1%%: %d" % (buy, multi, large))
except Exception as e:
    print("Error: %s" % e)
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

# 4. top-10 holders
print("\n=== 4. top-10 holders ===")
out, err = run(['onchainos', 'token', 'holders', '--address', addr, '--chain', chain, '--limit', '10'])
try:
    data = json.loads(out)
    if data.get('ok'):
        for h in data['data']:
            fs = h.get('fundingSource', '') or '(empty)'
            if len(fs) > 34: fs = fs[:34] + '...'
            print("  %s %.3f%% | fund=%s | pnl=%s" % (
                h.get('holderWalletAddress', '')[:14],
                float(h.get('holdPercent', 0)),
                fs, h.get('totalPnlUsd', '')[:16]))
except:
    print(out[:800])

# 5. aggregate behavior
print("\n=== 5. top-100 aggregate ===")
out, err = run(['onchainos', 'token', 'cluster-top-holders', '--address', addr, '--chain', chain, '--range-filter', '3'])
try:
    data = json.loads(out)
    if data.get('ok'):
        for k, v in data['data'].items():
            print("  %s: %s" % (k, v))
except:
    print(out[:1000])
