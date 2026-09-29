import sqlite3, json, re

conn = sqlite3.connect(r'C:\Users\Administrator\Desktop\小龙虾学习手册\Hermes\bsm-agent\extra\binance_square_202605261530.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

out = []
def p(line):
    out.append(str(line))
    print(".", end="", flush=True)

p("=" * 80)
p("KOL 市场全景数据 vs 交易结果 深度关联分析")
p("=" * 80)

# 1. Extract panorama fields from user_prompt and match to trades
cur.execute("""
    SELECT ka.id, ka.token, ka.direction, ka.confidence, ka.user_prompt,
           ka.created_at as ka_time,
           tp.id as trade_id, tp.side, tp.pnl_pct,
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
rows = [dict(r) for r in cur.fetchall()]
p(f"\nTotal matched trades with panorama: {len(rows)}")

# 2. Parse panorama fields from user_prompt
for r in rows:
    up = r['user_prompt'] or ''

    # 恐惧贪婪
    m = re.search(r'恐惧贪婪[:\s]*(\d+)\s*\((\w+)?\)?', up)
    r['fng_value'] = int(m.group(1)) if m else None
    r['fng_label'] = m.group(2) if m and m.group(2) else None

    # BTC 1h
    m = re.search(r'BTC 1h:\s*([-\d.]+)%\s*\((.+?)\)', up)
    r['btc_1h_val'] = float(m.group(1)) if m else None
    r['btc_1h_desc'] = m.group(2) if m else None

    # BTC 3h
    m = re.search(r'BTC 3h:\s*([-\d.]+)%\s*\((.+?)\)', up)
    r['btc_3h_val'] = float(m.group(1)) if m else None
    r['btc_3h_desc'] = m.group(2) if m else None

    # z_depth
    m = re.search(r'z_depth:\s*\$?([-\d.]+)M?\s*\((.+?)\)', up)
    r['zdepth_val'] = float(m.group(1)) if m else None
    r['zdepth_desc'] = m.group(2) if m else None

    # 流动性
    m = re.search(r'流动性:\s*(\S+)\s*\((.+?)\)', up)
    r['liquidity_status'] = m.group(1) if m else None
    r['liquidity_desc'] = m.group(2) if m else None

    # AI 市场研判
    m = re.search(r'AI 市场研判:\s*(\S+)\s*\(conf\s*(\d+)\)', up)
    r['ai_regime'] = m.group(1) if m else None
    r['ai_conf'] = int(m.group(2)) if m else None

    # 宏观风险评级
    m = re.search(r'宏观风险评级:\s*(\S+)', up)
    r['macro_risk'] = m.group(1) if m else None

# ============================================================================
# 3. 恐惧贪婪指数分析
# ============================================================================
p("\n\n--- 3. 恐惧贪婪指数 vs 胜率 ---")
fng_segs = [('极度恐惧(0-25)', 0, 25), ('恐惧(25-45)', 25, 45),
            ('中性(45-55)', 45, 55), ('贪婪(55-75)', 55, 75),
            ('极度贪婪(75-100)', 75, 100)]
p(f"{'F&G':<20} {'N':>5} {'Win':>5} {'Loss':>5} {'WR%':>7} {'SumPnL%':>10} {'AvgPnL%':>9} {'AvgHoldH':>9}")
p("-" * 70)
for label, lo, hi in fng_segs:
    sub = [r for r in rows if r['fng_value'] is not None and lo <= r['fng_value'] < hi]
    if not sub:
        continue
    n = len(sub)
    wins = sum(1 for r in sub if r['pnl_pct'] > 0)
    wr = wins / n * 100
    sum_p = sum(r['pnl_pct'] for r in sub)
    avg_p = sum_p / n
    holds = [(r['hold_hours'] or 0) for r in sub]
    avg_h = sum(holds) / len(holds)
    p(f"{label:<20} {n:>5} {wins:>5} {n-wins:>5} {wr:>6.1f}% {sum_p:>9.2f}% {avg_p:>8.2f}% {avg_h:>8.1f}h")

# LONG only by F&G
p("\n  LONG only:")
for label, lo, hi in fng_segs:
    sub = [r for r in rows if r['fng_value'] is not None and lo <= r['fng_value'] < hi and r['side'] == 'LONG']
    if not sub:
        continue
    n = len(sub)
    wins = sum(1 for r in sub if r['pnl_pct'] > 0)
    wr = wins / n * 100
    sum_p = sum(r['pnl_pct'] for r in sub)
    p(f"    {label:<18} {n:>4}笔  WR={wr:.1f}%  SumPnL={sum_p:.2f}%")

# SHORT only by F&G
p("  SHORT only:")
for label, lo, hi in fng_segs:
    sub = [r for r in rows if r['fng_value'] is not None and lo <= r['fng_value'] < hi and r['side'] == 'SHORT']
    if not sub:
        continue
    n = len(sub)
    wins = sum(1 for r in sub if r['pnl_pct'] > 0)
    wr = wins / n * 100
    sum_p = sum(r['pnl_pct'] for r in sub)
    p(f"    {label:<18} {n:>4}笔  WR={wr:.1f}%  SumPnL={sum_p:.2f}%")

# ============================================================================
# 4. BTC 走势分析
# ============================================================================
p("\n\n--- 4. BTC 1h 走势 vs 胜率 ---")
btc_segs = [('BTC跌(<-0.5%)', -99, -0.5), ('BTC微跌(-0.5~0)', -0.5, 0),
            ('BTC微涨(0~0.5%)', 0, 0.5), ('BTC涨(>0.5%)', 0.5, 99)]
p(f"{'BTC 1h':<22} {'N':>5} {'Win':>5} {'WR%':>7} {'SumPnL%':>10} {'AvgPnL%':>9} {'LONG':>6} {'SHRT':>6} {'L_WR%':>7} {'S_WR%':>7}")
p("-" * 88)
for label, lo, hi in btc_segs:
    sub = [r for r in rows if r['btc_1h_val'] is not None and lo <= r['btc_1h_val'] < hi]
    if not sub:
        continue
    n = len(sub)
    wins = sum(1 for r in sub if r['pnl_pct'] > 0)
    wr = wins / n * 100
    sum_p = sum(r['pnl_pct'] for r in sub)
    avg_p = sum_p / n
    l_sub = [r for r in sub if r['side'] == 'LONG']
    s_sub = [r for r in sub if r['side'] == 'SHORT']
    l_wr = sum(1 for r in l_sub if r['pnl_pct'] > 0) / len(l_sub) * 100 if l_sub else 0
    s_wr = sum(1 for r in s_sub if r['pnl_pct'] > 0) / len(s_sub) * 100 if s_sub else 0
    p(f"{label:<22} {n:>5} {wins:>5} {wr:>6.1f}% {sum_p:>9.2f}% {avg_p:>8.2f}% {len(l_sub):>5} {len(s_sub):>5} {l_wr:>6.1f}% {s_wr:>6.1f}%")

# BTC 3h
p("\n--- 4B. BTC 3h 走势 vs 胜率 ---")
for label, lo, hi in btc_segs:
    sub = [r for r in rows if r['btc_3h_val'] is not None and lo <= r['btc_3h_val'] < hi]
    if not sub:
        continue
    n = len(sub)
    wins = sum(1 for r in sub if r['pnl_pct'] > 0)
    wr = wins / n * 100
    sum_p = sum(r['pnl_pct'] for r in sub)
    l_sub = [r for r in sub if r['side'] == 'LONG']
    s_sub = [r for r in sub if r['side'] == 'SHORT']
    l_wr = sum(1 for r in l_sub if r['pnl_pct'] > 0) / len(l_sub) * 100 if l_sub else 0
    s_wr = sum(1 for r in s_sub if r['pnl_pct'] > 0) / len(s_sub) * 100 if s_sub else 0
    p(f"  {label:<20} {n:>4}笔  WR={wr:.1f}%  SumPnL={sum_p:.2f}%  LONG({len(l_sub)}笔 WR={l_wr:.1f}%)  SHORT({len(s_sub)}笔 WR={s_wr:.1f}%)")

# ============================================================================
# 5. AI 市场研判分析
# ============================================================================
p("\n\n--- 5. AI 市场研判 vs 胜率 ---")
regimes = {}
for r in rows:
    reg = r['ai_regime'] or 'unknown'
    if reg not in regimes:
        regimes[reg] = []
    regimes[reg].append(r)

p(f"{'Regime':<16} {'N':>5} {'Win':>5} {'WR%':>7} {'SumPnL%':>10} {'AvgPnL%':>9} {'LONG':>6} {'L_WR%':>7} {'SHORT':>6} {'S_WR%':>7}")
p("-" * 85)
for reg in sorted(regimes.keys(), key=lambda x: len(regimes[x]), reverse=True):
    sub = regimes[reg]
    n = len(sub)
    wins = sum(1 for r in sub if r['pnl_pct'] > 0)
    wr = wins / n * 100
    sum_p = sum(r['pnl_pct'] for r in sub)
    avg_p = sum_p / n
    l_sub = [r for r in sub if r['side'] == 'LONG']
    s_sub = [r for r in sub if r['side'] == 'SHORT']
    l_wr = sum(1 for r in l_sub if r['pnl_pct'] > 0) / len(l_sub) * 100 if l_sub else 0
    s_wr = sum(1 for r in s_sub if r['pnl_pct'] > 0) / len(s_sub) * 100 if s_sub else 0
    p(f"{reg:<16} {n:>5} {wins:>5} {wr:>6.1f}% {sum_p:>9.2f}% {avg_p:>8.2f}% {len(l_sub):>5} {l_wr:>6.1f}% {len(s_sub):>5} {s_wr:>6.1f}%")

# ============================================================================
# 6. 宏观风险评级分析
# ============================================================================
p("\n\n--- 6. 宏观风险评级 vs 胜率 ---")
macro_segs = {}
for r in rows:
    mr = r['macro_risk'] or 'unknown'
    if mr not in macro_segs:
        macro_segs[mr] = []
    macro_segs[mr].append(r)

p(f"{'MacroRisk':<16} {'N':>5} {'Win':>5} {'WR%':>7} {'SumPnL%':>10} {'AvgPnL%':>9} {'LONG':>6} {'L_WR%':>7} {'SHORT':>6} {'S_WR%':>7}")
p("-" * 85)
for mr in sorted(macro_segs.keys(), key=lambda x: len(macro_segs[x]), reverse=True):
    sub = macro_segs[mr]
    n = len(sub)
    wins = sum(1 for r in sub if r['pnl_pct'] > 0)
    wr = wins / n * 100
    sum_p = sum(r['pnl_pct'] for r in sub)
    avg_p = sum_p / n
    l_sub = [r for r in sub if r['side'] == 'LONG']
    s_sub = [r for r in sub if r['side'] == 'SHORT']
    l_wr = sum(1 for r in l_sub if r['pnl_pct'] > 0) / len(l_sub) * 100 if l_sub else 0
    s_wr = sum(1 for r in s_sub if r['pnl_pct'] > 0) / len(s_sub) * 100 if s_sub else 0
    p(f"{mr:<16} {n:>5} {wins:>5} {wr:>6.1f}% {sum_p:>9.2f}% {avg_p:>8.2f}% {len(l_sub):>5} {l_wr:>6.1f}% {len(s_sub):>5} {s_wr:>6.1f}%")

# ============================================================================
# 7. 流动性状态分析
# ============================================================================
p("\n\n--- 7. 流动性状态 vs 胜率 ---")
liq_segs = {}
for r in rows:
    ls = r['liquidity_status'] or 'unknown'
    if ls not in liq_segs:
        liq_segs[ls] = []
    liq_segs[ls].append(r)

for ls in sorted(liq_segs.keys(), key=lambda x: len(liq_segs[x]), reverse=True):
    sub = liq_segs[ls]
    n = len(sub)
    wins = sum(1 for r in sub if r['pnl_pct'] > 0)
    wr = wins / n * 100
    sum_p = sum(r['pnl_pct'] for r in sub)
    p(f"  {ls}: {n}笔, WR={wr:.1f}%, SumPnL={sum_p:.2f}%")

# ============================================================================
# 8. 组合条件：寻找最优/最劣全景组合
# ============================================================================
p("\n\n--- 8. 全景条件组合分析 ---")

# Define panorama "regimes"
def classify_pano(r):
    fng = r['fng_value']
    btc1 = r['btc_1h_val']
    ai = r['ai_regime']
    macro = r['macro_risk']

    # Danger signals
    dangers = []
    if fng is not None and fng >= 65:
        dangers.append('F&G贪婪')
    if btc1 is not None and btc1 < -0.3:
        dangers.append('BTC走弱')
    if ai and ai in ('回调兑现', '趋势空'):
        dangers.append(f'AI={ai}')
    if macro and macro in ('高风险', '注意'):
        dangers.append(f'宏观={macro}')

    # Opportunity signals
    opps = []
    if fng is not None and fng <= 35:
        opps.append('F&G恐惧')
    if btc1 is not None and btc1 > 0.2:
        opps.append('BTC走强')
    if ai and ai == '山寨行情':
        opps.append('AI=山寨行情')
    if macro and macro == '正常':
        opps.append('宏观正常')

    return dangers, opps

# Test danger combinations
p("\n危险信号叠加效果（应有更高的 direction=none 或 SHORT 倾向）：")
danger_levels = {}
for r in rows:
    dangers, opps = classify_pano(r)
    level = len(dangers)
    key = f"{level}个危险信号"
    if key not in danger_levels:
        danger_levels[key] = []
    danger_levels[key].append(r)

for key in sorted(danger_levels.keys()):
    sub = danger_levels[key]
    n = len(sub)
    wins = sum(1 for r in sub if r['pnl_pct'] > 0)
    wr = wins / n * 100
    sum_p = sum(r['pnl_pct'] for r in sub)
    l_sub = [r for r in sub if r['side'] == 'LONG']
    s_sub = [r for r in sub if r['side'] == 'SHORT']
    l_wr = sum(1 for r in l_sub if r['pnl_pct'] > 0) / len(l_sub) * 100 if l_sub else 0
    s_wr = sum(1 for r in s_sub if r['pnl_pct'] > 0) / len(s_sub) * 100 if s_sub else 0
    p(f"  {key}: {n}笔 WR={wr:.1f}% SumPnL={sum_p:.2f}% LONG({len(l_sub)} WR={l_wr:.1f}%) SHORT({len(s_sub)} WR={s_wr:.1f}%)")

# Opportunity combinations
p("\n机会信号叠加效果：")
opp_levels = {}
for r in rows:
    dangers, opps = classify_pano(r)
    level = len(opps)
    key = f"{level}个机会信号"
    if key not in opp_levels:
        opp_levels[key] = []
    opp_levels[key].append(r)

for key in sorted(opp_levels.keys()):
    sub = opp_levels[key]
    n = len(sub)
    wins = sum(1 for r in sub if r['pnl_pct'] > 0)
    wr = wins / n * 100
    sum_p = sum(r['pnl_pct'] for r in sub)
    p(f"  {key}: {n}笔 WR={wr:.1f}% SumPnL={sum_p:.2f}%")

# ============================================================================
# 9. 具体全景组合
# ============================================================================
p("\n\n--- 9. 具体全景组合 Top/Bottom ---")
combo_stats = {}
for r in rows:
    dangers, opps = classify_pano(r)
    d_tags = ','.join(dangers) if dangers else '无危险'
    o_tags = ','.join(opps) if opps else '无机会'
    key = f"D[{d_tags}]_O[{o_tags}]"
    if key not in combo_stats:
        combo_stats[key] = []
    combo_stats[key].append(r)

# Top 10 by count
p(f"{'PanoramaCombo':<55} {'N':>5} {'WR%':>7} {'SumPnL%':>10}")
p("-" * 80)
for key, sub in sorted(combo_stats.items(), key=lambda x: len(x[1]), reverse=True)[:15]:
    n = len(sub)
    if n < 3:
        continue
    wins = sum(1 for r in sub if r['pnl_pct'] > 0)
    wr = wins / n * 100
    sum_p = sum(r['pnl_pct'] for r in sub)
    short_key = key[:54]
    p(f"{short_key:<55} {n:>5} {wr:>6.1f}% {sum_p:>9.2f}%")

# ============================================================================
# 10. z_depth vs 结果
# ============================================================================
p("\n\n--- 10. z_depth vs 胜率 ---")
z_segs = [('<100M (薄)', 0, 100), ('100-200M (正常)', 100, 200),
          ('200-400M (厚)', 200, 400), ('400M+ (极厚)', 400, 9999)]
p(f"{'z_depth':<22} {'N':>5} {'Win':>5} {'WR%':>7} {'SumPnL%':>10}")
p("-" * 50)
for label, lo, hi in z_segs:
    sub = [r for r in rows if r['zdepth_val'] is not None and lo <= r['zdepth_val'] < hi]
    if not sub:
        continue
    n = len(sub)
    wins = sum(1 for r in sub if r['pnl_pct'] > 0)
    wr = wins / n * 100
    sum_p = sum(r['pnl_pct'] for r in sub)
    p(f"  {label:<20} {n:>5} {wins:>5} {wr:>6.1f}% {sum_p:>9.2f}%")

# ============================================================================
# 11. 多空方向 vs 全景环境
# ============================================================================
p("\n\n--- 11. 关键发现：什么环境该做多/做空/不开 ---")

# Find environments where LONG is terrible
p("\nLONG最差的环境（应该不开多）:")
long_envs = []
for r in rows:
    if r['side'] == 'LONG':
        dangers, opps = classify_pano(r)
        long_envs.append((len(dangers), r['pnl_pct'], dangers, opps))

# Group by danger level for LONG
for level in [2, 3]:
    sub = [(pnl, d, o) for lv, pnl, d, o in long_envs if lv == level]
    if sub:
        n = len(sub)
        wins = sum(1 for pnl, d, o in sub if pnl > 0)
        avg = sum(pnl for pnl, d, o in sub) / n
        p(f"  {level}个危险信号 LONG: {n}笔, WR={wins/n*100:.1f}%, AvgPnL={avg:.2f}%")

# Find environments where SHORT is best
p("\nSHORT最好/最差的环境:")
short_envs = []
for r in rows:
    if r['side'] == 'SHORT':
        dangers, opps = classify_pano(r)
        short_envs.append((len(dangers), r['pnl_pct'], dangers, opps))

for level in [0, 1, 2]:
    sub = [(pnl, d, o) for lv, pnl, d, o in short_envs if lv == level]
    if sub:
        n = len(sub)
        wins = sum(1 for pnl, d, o in sub if pnl > 0)
        avg = sum(pnl for pnl, d, o in sub) / n
        p(f"  {level}个危险信号 SHORT: {n}笔, WR={wins/n*100:.1f}%, AvgPnL={avg:.2f}%")

with open(r'C:\Users\Administrator\Desktop\小龙虾学习手册\Hermes\bsm-agent\extra\_panorama_analysis.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))

conn.close()
print("\nDone!")
