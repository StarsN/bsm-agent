import sqlite3, json, re

conn = sqlite3.connect(r'C:\Users\Administrator\Desktop\小龙虾学习手册\Hermes\bsm-agent\extra\binance_square_202605261530.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()
out = []
def p(line):
    out.append(str(line))
    print(".", end="", flush=True)

p("=" * 80)
p("AI=山寨行情 时 SHORT 交易详细审查")
p("=" * 80)

# Get all SHORT trades with their panorama
cur.execute("""
    SELECT ka.id, ka.token, ka.direction, ka.confidence, ka.user_prompt,
           ka.created_at as ka_time, ka.reason,
           tp.id as trade_id, tp.side, tp.pnl_pct, tp.entry_price, tp.current_price,
           tp.created_at as open_time, tp.closed_at,
           ROUND((julianday(tp.closed_at) - julianday(tp.created_at)) * 24, 2) as hold_hours
    FROM kol_analyses ka
    JOIN trade_positions tp ON ka.token = tp.token
        AND tp.strategy = 'kol_agent'
        AND tp.created_at >= ka.created_at
        AND tp.created_at <= datetime(ka.created_at, '+30 minutes')
    WHERE ka.user_prompt IS NOT NULL
      AND ka.direction = 'short'
    ORDER BY ka.created_at
""")
short_rows = [dict(r) for r in cur.fetchall()]
p(f"\nTotal SHORT trades: {len(short_rows)}")

# Parse AI regime for each
for r in short_rows:
    up = r['user_prompt'] or ''
    m = re.search(r'AI 市场研判:\s*(\S+)\s*\(conf\s*(\d+)\)', up)
    r['ai_regime'] = m.group(1) if m else 'unknown'
    m = re.search(r'恐惧贪婪[:\s]*(\d+)', up)
    r['fng'] = int(m.group(1)) if m else None
    m = re.search(r'BTC 3h:\s*([-\d.]+)%', up)
    r['btc_3h'] = float(m.group(1)) if m else None
    m = re.search(r'宏观风险评级:\s*(\S+)', up)
    r['macro'] = m.group(1) if m else None

# 1. AI regime breakdown for SHORT
p("\n--- 1. SHORT by AI Regime ---")
regimes = {}
for r in short_rows:
    reg = r['ai_regime']
    regimes.setdefault(reg, []).append(r)
for reg in sorted(regimes, key=lambda x: len(regimes[x]), reverse=True):
    sub = regimes[reg]
    wins = sum(1 for r in sub if r['pnl_pct'] > 0)
    wr = wins / len(sub) * 100
    sum_p = sum(r['pnl_pct'] for r in sub)
    avg_h = sum((r['hold_hours'] or 0) for r in sub) / len(sub)
    p(f"  {reg}: {len(sub)}笔 WR={wr:.1f}% SumPnL={sum_p:.2f}% AvgHold={avg_h:.1f}h")

# 2. Show every SHORT when AI=山寨行情
p("\n\n--- 2. AI=山寨行情 时每笔 SHORT 详情 ---")
ai_shan = [r for r in short_rows if r['ai_regime'] == '山寨行情']
p(f"共 {len(ai_shan)} 笔:")
p(f"{'Token':<12} {'PnL%':>8} {'HoldH':>7} {'Conf':>5} {'F&G':>5} {'BTC3h%':>8} {'Macro':<8} {'Reason'}")
p("-" * 100)
for r in ai_shan:
    reason = (r['reason'] or '')[:60]
    p(f"{r['token']:<12} {r['pnl_pct']:>7.2f}% {(r['hold_hours'] or 0):>6.1f}h {r['confidence']:>5} {r['fng'] or '?':>5} {r['btc_3h'] or '?':>8} {r['macro'] or '?':<8} {reason}")

# 3. Token concentration
p("\n\n--- 3. AI=山寨行情 SHORT 的 Token 集中度 ---")
token_counts = {}
for r in ai_shan:
    t = r['token']
    token_counts[t] = token_counts.get(t, 0) + 1
for t, cnt in sorted(token_counts.items(), key=lambda x: x[1], reverse=True):
    sub = [r for r in ai_shan if r['token'] == t]
    wins = sum(1 for r in sub if r['pnl_pct'] > 0)
    sum_p = sum(r['pnl_pct'] for r in sub)
    p(f"  {t}: {cnt}笔 WR={wins/cnt*100:.0f}% SumPnL={sum_p:.2f}%")

# 4. BTC 3h context when SHORT in AI=山寨行情
p("\n\n--- 4. AI=山寨行情 SHORT 时的 BTC 3h 环境 ---")
for r in ai_shan:
    btc3 = r['btc_3h']
    if btc3 is None:
        btc_bucket = 'unknown'
    elif btc3 < -0.5:
        btc_bucket = 'BTC3h跌<-0.5%'
    elif btc3 < 0:
        btc_bucket = 'BTC3h微跌'
    elif btc3 < 0.5:
        btc_bucket = 'BTC3h微涨'
    else:
        btc_bucket = 'BTC3h涨>0.5%'
    r['btc_bucket'] = btc_bucket

btc_groups = {}
for r in ai_shan:
    btc_groups.setdefault(r['btc_bucket'], []).append(r)
for bg in sorted(btc_groups.keys()):
    sub = btc_groups[bg]
    wins = sum(1 for r in sub if r['pnl_pct'] > 0)
    p(f"  {bg}: {len(sub)}笔 WR={wins/len(sub)*100:.0f}% SumPnL={sum(r['pnl_pct'] for r in sub):.2f}%")

# 5. Compare: ALL SHORT vs AI=山寨行情 SHORT
p("\n\n--- 5. 对比：全部 SHORT vs AI=山寨行情 SHORT ---")
all_short_wr = sum(1 for r in short_rows if r['pnl_pct'] > 0) / len(short_rows) * 100
all_short_pnl = sum(r['pnl_pct'] for r in short_rows)
ai_short_wr = sum(1 for r in ai_shan if r['pnl_pct'] > 0) / len(ai_shan) * 100 if ai_shan else 0
ai_short_pnl = sum(r['pnl_pct'] for r in ai_shan) if ai_shan else 0
p(f"  全部 SHORT: {len(short_rows)}笔 WR={all_short_wr:.1f}% SumPnL={all_short_pnl:.2f}%")
p(f"  AI=山寨 SHORT: {len(ai_shan)}笔 WR={ai_short_wr:.1f}% SumPnL={ai_short_pnl:.2f}%")

# 6. LONG vs SHORT in AI=山寨行情 — full breakdown by BTC context
p("\n\n--- 6. AI=山寨行情 时 多空对比（按BTC环境分层）---")
cur.execute("""
    SELECT ka.id, ka.token, ka.direction, ka.confidence, ka.user_prompt,
           tp.side, tp.pnl_pct,
           ROUND((julianday(tp.closed_at) - julianday(tp.created_at)) * 24, 2) as hold_hours
    FROM kol_analyses ka
    JOIN trade_positions tp ON ka.token = tp.token
        AND tp.strategy = 'kol_agent'
        AND tp.created_at >= ka.created_at
        AND tp.created_at <= datetime(ka.created_at, '+30 minutes')
    WHERE ka.user_prompt IS NOT NULL
      AND ka.direction IN ('long', 'short')
    ORDER BY ka.created_at
""")
all_rows = [dict(r) for r in cur.fetchall()]
for r in all_rows:
    up = r['user_prompt'] or ''
    m = re.search(r'AI 市场研判:\s*(\S+)\s*\(conf\s*(\d+)\)', up)
    r['ai_regime'] = m.group(1) if m else 'unknown'
    m = re.search(r'BTC 3h:\s*([-\d.]+)%', up)
    r['btc_3h'] = float(m.group(1)) if m else None

# Filter to AI=山寨行情
ai_all = [r for r in all_rows if r['ai_regime'] == '山寨行情']
p(f"AI=山寨行情 总交易: {len(ai_all)}笔")

# By BTC context
for btc_lo, btc_hi, btc_label in [(-99, -0.5, 'BTC3h跌<-0.5%'), (-0.5, 0, 'BTC3h微跌'),
                                    (0, 0.5, 'BTC3h微涨'), (0.5, 99, 'BTC3h涨>0.5%')]:
    sub = [r for r in ai_all if r['btc_3h'] is not None and btc_lo <= r['btc_3h'] < btc_hi]
    if not sub:
        continue
    longs = [r for r in sub if r['side'] == 'LONG']
    shorts = [r for r in sub if r['side'] == 'SHORT']
    l_wr = sum(1 for r in longs if r['pnl_pct'] > 0) / len(longs) * 100 if longs else 0
    s_wr = sum(1 for r in shorts if r['pnl_pct'] > 0) / len(shorts) * 100 if shorts else 0
    l_pnl = sum(r['pnl_pct'] for r in longs) if longs else 0
    s_pnl = sum(r['pnl_pct'] for r in shorts) if shorts else 0
    p(f"  {btc_label}: LONG {len(longs)}笔 WR={l_wr:.0f}% PnL={l_pnl:.1f}% | SHORT {len(shorts)}笔 WR={s_wr:.0f}% PnL={s_pnl:.1f}%")

with open(r'C:\Users\Administrator\Desktop\小龙虾学习手册\Hermes\bsm-agent\extra\_shan_short_audit.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))
conn.close()
print("\nDone!")
