import subprocess, json

def run(cmd, timeout=30):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate(timeout=timeout)
    return out.strip(), err.strip()

# Step 1: Search STRAX on BSC
print("=== Search STRAX on BSC ===")
out, err = run(['onchainos', 'token', 'search', '--query', 'STRAX', '--chains', 'bsc', '--limit', '5'])
try:
    data = json.loads(out)
    if data.get('ok'):
        for t in data['data']:
            print("%s | %s | %s" % (t.get('tokenSymbol'), t.get('tokenName'), t.get('tokenContractAddress')))
            print("  holders=%s mc=%s liq=%s comm=%s" % (
                t.get('holders'), t.get('marketCap'), t.get('liquidity'),
                t.get('tagList', {}).get('communityRecognized', False)))
except:
    print(out[:1000])

# Use the first STRAX result from BSC
# Stratis [Old] on BSC: 0xabd3279afe8b627e64a1b257d38c269990b6afb1 or similar
# Let's find the main one
addr = None
try:
    data = json.loads(out)
    for t in data.get('data', []):
        if 'STRAX' in t.get('tokenSymbol', '').upper():
            addr = t.get('tokenContractAddress')
            print("\nSelected: %s (%s)" % (t.get('tokenSymbol'), addr))
            break
except:
    pass

if not addr:
    # Try known STRAX address on BSC
    addr = '0xabd3279afe8b627e64a1b257d38c269990b6afb1'
    print('\nUsing known address: %s' % addr)

chain = 'bsc'
# ========================================
print("=" * 60)
print("STRAX on BSC: %s" % addr)
print("=" * 60)

# --- 1. cluster-overview ---
print("\n=== 1. cluster-overview ===")
out, err = run(['onchainos', 'token', 'cluster-overview', '--address', addr, '--chain', chain])
try:
    data = json.loads(out)
    if data.get('ok'):
        d = data['data']
        for k, v in d.items():
            print("  %s: %s" % (k, v))
except:
    print(out[:1500])
if err:
    print("ERR:", err[:300])

# --- 2. cluster-list ---
print("\n=== 2. cluster-list ===")
out, err = run(['onchainos', 'token', 'cluster-list', '--address', addr, '--chain', chain])
try:
    data = json.loads(out)
    if data.get('ok'):
        clusters = data['data'].get('clusterList', [])
        total_holding = 0
        exchange_count = 0
        contract_count = 0
        buy_clusters = 0
        sell_clusters = 0
        multi_addr_clusters = 0

        print("  Top clusters:")
        for i, c in enumerate(clusters):
            pct = float(c.get('holdingPercent', 0))
            total_holding += pct
            addrs = c.get('clusterAddressList', [])
            has_ex = any(a.get('isExchange') for a in addrs)
            has_ct = any(a.get('isContract') for a in addrs)
            trend = c.get('trendType', [])
            pnl = c.get('pnlPercent', '0')[:10]

            if has_ex: exchange_count += 1
            if has_ct: contract_count += 1
            if 'buy' in str(trend).lower(): buy_clusters += 1
            if 'sell' in str(trend).lower(): sell_clusters += 1
            if len(addrs) > 1: multi_addr_clusters += 1

            if i < 10:
                flags = []
                if has_ex: flags.append('EX')
                if has_ct: flags.append('CT')
                flag = ' [' + ','.join(flags) + ']' if flags else ''
                print("  C%d: %.4f%% | %d addr | trend=%s | pnl=%s%%%s" % (
                    i+1, pct, len(addrs), trend, pnl, flag))
                for a in addrs[:3]:
                    print("       rank=%s %.4f%% ex=%s ct=%s kol=%s" % (
                        a.get('addressRank'), float(a.get('holdingPercent', 0)),
                        a.get('isExchange'), a.get('isContract'), a.get('isKol')))
                if len(addrs) > 3:
                    print("       ... +%d more" % (len(addrs) - 3))

        print("\n  --- Summary ---")
        print("  Total clusters: %d" % len(clusters))
        print("  Sum holdings: %.2f%%" % (total_holding * 100 if total_holding < 1 else total_holding))
        print("  Exchange clusters: %d" % exchange_count)
        print("  Contract clusters: %d" % contract_count)
        print("  Buy-trend clusters: %d" % buy_clusters)
        print("  Sell-trend clusters: %d" % sell_clusters)
        print("  Multi-address clusters: %d" % multi_addr_clusters)
except Exception as e:
    print("Parse error: %s" % e)
    print(out[:2000])

# --- 3. advanced-info ---
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

# --- 4. holders top 10 ---
print("\n=== 4. top-10 holders ===")
out, err = run(['onchainos', 'token', 'holders', '--address', addr, '--chain', chain, '--limit', '10'])
try:
    data = json.loads(out)
    if data.get('ok'):
        for h in data['data']:
            fs = h.get('fundingSource', '') or '(empty)'
            if len(fs) > 30: fs = fs[:30] + '...'
            pnl = h.get('totalPnlUsd', '') or '0'
            print("  %s %.4f%% | fund=%s | pnl=%s" % (
                h.get('holderWalletAddress', '')[:12],
                float(h.get('holdPercent', 0)),
                fs, pnl[:18]))
except:
    print(out[:1000])

# --- 5. cluster-top-holders (aggregate behavior) ---
print("\n=== 5. cluster-top-holders (top 100 aggregate) ===")
out, err = run(['onchainos', 'token', 'cluster-top-holders', '--address', addr, '--chain', chain, '--range-filter', '3'])
try:
    data = json.loads(out)
    if data.get('ok'):
        d = data['data']
        for k, v in d.items():
            print("  %s: %s" % (k, v))
except:
    print(out[:1000])
