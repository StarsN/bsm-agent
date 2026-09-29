import subprocess, json

def run(cmd, timeout=30):
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = p.communicate(timeout=timeout)
    return out.strip(), err.strip()

# Step 1: Search SYN
print("=== Search SYN ===")
for chain in ['bsc', 'ethereum', 'solana']:
    out, err = run(['onchainos', 'token', 'search', '--query', 'SYN', '--chains', chain, '--limit', '3'])
    data = json.loads(out) if out else {}
    if data.get('ok') and data.get('data'):
        for t in data['data'][:3]:
            print("%s | %s | %s | holders=%s | mc=%s | liq=%s | comm=%s" % (
                t.get('tokenSymbol'), t.get('tokenName'), t.get('tokenContractAddress'),
                t.get('holders'), t.get('marketCap'), t.get('liquidity'),
                t.get('tagList', {}).get('communityRecognized', False)
            ))
            print("  chain=%s" % chain)
    print()

# Pick the most established one and run cluster queries
# SYN on ethereum: 0x0f2d719407fdbeff09d87557abb7232601fd9f29 (Synapse)
addr = '0x0f2d719407fdbeff09d87557abb7232601fd9f29'
chain = 'ethereum'

print("=" * 60)
print("Using: SYN (Synapse) on Ethereum")
print("Address: %s" % addr)
print("=" * 60)

# 1. cluster-overview
print("\n=== 1. cluster-overview ===")
out, err = run(['onchainos', 'token', 'cluster-overview', '--address', addr, '--chain', chain])
try:
    data = json.loads(out)
    if data.get('ok'):
        d = data['data']
        for k, v in d.items():
            print("  %s: %s" % (k, v))
except:
    print(out[:2000])

# 2. cluster-list (show first 5 clusters)
print("\n=== 2. cluster-list (top clusters with filtering) ===")
out, err = run(['onchainos', 'token', 'cluster-list', '--address', addr, '--chain', chain])
try:
    data = json.loads(out)
    if data.get('ok'):
        clusters = data['data'].get('clusterList', [])
        total_clusters = len(clusters)
        total_holding = 0
        exchange_related = 0
        contract_related = 0

        for i, c in enumerate(clusters):
            cluster_pct = float(c.get('holdingPercent', 0))
            total_holding += cluster_pct

            # Check for exchange/contract addresses in cluster
            addrs = c.get('clusterAddressList', [])
            has_exchange = any(a.get('isExchange') for a in addrs)
            has_contract = any(a.get('isContract') for a in addrs)
            has_kol = any(a.get('isKol') for a in addrs)
            trend = c.get('trendType', [])

            if has_exchange:
                exchange_related += 1
            if has_contract:
                contract_related += 1

            if i < 8:  # Show first 8 clusters
                flags = []
                if has_exchange: flags.append('EXCHANGE')
                if has_contract: flags.append('CONTRACT')
                if has_kol: flags.append('KOL')
                flag_str = ' [' + ','.join(flags) + ']' if flags else ''

                # Check funding source diversity within cluster
                sources = set()
                for a in addrs[:5]:
                    pass  # fundingSource not in cluster-list items

                print("  Cluster %d: %.2f%% | %d addrs | trend=%s | pnl=%s%%%s" % (
                    i+1, cluster_pct, len(addrs), trend,
                    c.get('pnlPercent', '0')[:8], flag_str
                ))
                for a in addrs[:3]:
                    print("    rank=%s %.4f%% exch=%s contract=%s kol=%s" % (
                        a.get('addressRank'), float(a.get('holdingPercent', 0)),
                        a.get('isExchange'), a.get('isContract'), a.get('isKol')
                    ))
                if len(addrs) > 3:
                    print("    ... +%d more addresses" % (len(addrs) - 3))

        print("\n  --- Summary ---")
        print("  Total clusters: %d" % total_clusters)
        print("  Sum of cluster holdings: %.2f%%" % total_holding)
        print("  Clusters with exchange addr: %d" % exchange_related)
        print("  Clusters with contract addr: %d" % contract_related)
except Exception as e:
    print("Parse error: %s" % e)
    print(out[:1500])

# 3. advanced-info for surface comparison
print("\n=== 3. advanced-info (surface metrics) ===")
out, err = run(['onchainos', 'token', 'advanced-info', '--address', addr, '--chain', chain])
try:
    data = json.loads(out)
    if data.get('ok'):
        d = data['data']
        print("  top10HoldPercent: %s" % d.get('top10HoldPercent'))
        print("  devHoldingPercent: %s" % d.get('devHoldingPercent'))
        print("  bundleHoldingPercent: %s" % d.get('bundleHoldingPercent'))
        print("  suspiciousHoldingPercent: %s" % d.get('suspiciousHoldingPercent'))
        print("  sniperHoldingPercent: %s" % d.get('sniperHoldingPercent'))
        print("  riskControlLevel: %s" % d.get('riskControlLevel'))
        print("  tokenTags: %s" % d.get('tokenTags'))
        print("  lpBurnedPercent: %s" % d.get('lpBurnedPercent'))
except:
    print(out[:1000])

# 4. holders top 10 with funding source
print("\n=== 4. top-10 holders (funding source check) ===")
out, err = run(['onchainos', 'token', 'holders', '--address', addr, '--chain', chain, '--limit', '10'])
try:
    data = json.loads(out)
    if data.get('ok'):
        for h in data['data'][:10]:
            fs = h.get('fundingSource', '')
            fs_str = fs[:20] + '...' if len(fs) > 20 else (fs or '(empty)')
            print("  rank=%s %.4f%% | fundSource=%s | pnl=%s" % (
                h.get('cursor', '?'), float(h.get('holdPercent', 0)),
                fs_str, h.get('totalPnlUsd', '0')[:15]
            ))
except:
    print(out[:1000])
