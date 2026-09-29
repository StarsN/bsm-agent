import sqlite3
import json

conn = sqlite3.connect(r'C:\Users\Administrator\Desktop\小龙虾学习手册\Hermes\bsm-agent\extra\binance_square_202605261530.db')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

out = []
def p(line):
    out.append(line)
    print(line)

p("=" * 80)
p("强制12h平仓模拟：如果所有 KOL 持仓到 12h 就平掉")
p("=" * 80)

cur.execute("""
    SELECT id, token, side, pnl_pct, entry_price, current_price,
           stop_loss_price, tp1_price, tp2_price, highest_price, lowest_price,
           created_at, closed_at,
           ROUND((julianday(closed_at) - julianday(created_at)) * 24, 2) as hold_hours
    FROM trade_positions
    WHERE strategy = 'kol_agent' AND status = 'CLOSED' AND closed_at IS NOT NULL
    ORDER BY hold_hours
""")
rows = [dict(r) for r in cur.fetchall()]

# --- 分组 ---
le12 = [r for r in rows if r['hold_hours'] <= 12]
gt12 = [r for r in rows if r['hold_hours'] > 12]

p(f"\n<=12h 持仓: {len(le12)} 笔 (实际结果不变)")
p(f">12h 持仓:  {len(gt12)} 笔 (需要估算12h的价格)")

# --- <=12h 统计 ---
wins_le = sum(1 for r in le12 if r['pnl_pct'] > 0)
pnl_le = sum(r['pnl_pct'] for r in le12)
p(f"\n<=12h 实际: WR={wins_le}/{len(le12)}={wins_le/len(le12)*100:.1f}%, SumPnL={pnl_le:.2f}%")

# --- >12h 用三种方法估算 ---
p("\n\n=== 三种估算方法对比 ===")
p("\n方法A: 线性插值 (P&L at 12h = final P&L * 12/hold_hours)")
p("方法B: 偏保守 (盈利折半，亏损不变)")
p("方法C: 保守 (盈利折1/3，亏损不变)")

for method, desc in [("A", "线性插值"), ("B", "盈利折半"), ("C", "盈利折1/3")]:
    estimated_pnls = []
    for r in gt12:
        actual_pnl = r['pnl_pct']
        h = r['hold_hours']

        if method == "A":
            # Linear: PnL at 12h proportional to time
            est = actual_pnl * (12.0 / h) if h > 0 else actual_pnl
        elif method == "B":
            # Conservative: if winning, halve it; if losing, keep same
            if actual_pnl > 0:
                est = actual_pnl * 0.5
            else:
                est = actual_pnl  # loss stays the same
        elif method == "C":
            if actual_pnl > 0:
                est = actual_pnl * 0.33
            else:
                est = actual_pnl

        estimated_pnls.append(est)

    # Combine with <=12h actuals
    all_pnls = [r['pnl_pct'] for r in le12] + estimated_pnls
    total = len(all_pnls)
    wins = sum(1 for p in all_pnls if p > 0)
    wr = wins / total * 100
    sum_pnl = sum(all_pnls)
    avg_pnl = sum_pnl / total

    p(f"\n  方法{method} ({desc}):")
    p(f"    总胜率: {wins}/{total} = {wr:.1f}%")
    p(f"    累计盈亏: {sum_pnl:.2f}%")
    p(f"    均盈亏: {avg_pnl:.2f}%")

# --- 详细列出 >12h 每笔在12h的线性估算 ---
p("\n\n--- >12h 每笔交易在12h的线性估算 ---")
p(f"{'Token':<12} {'Side':>6} {'HoldH':>7} {'Actual%':>8} {'Est@12h%':>9} {'HiPrice%':>8} {'LoPrice%':>8}")
p("-" * 65)
for r in gt12:
    h = r['hold_hours']
    actual = r['pnl_pct']
    est = actual * (12.0 / h) if h > 0 else actual

    # Also compute highest/lowest price PnL
    hi_pnl = None
    lo_pnl = None
    if r['highest_price'] and r['entry_price']:
        if r['side'] == 'LONG':
            hi_pnl = (r['highest_price'] - r['entry_price']) / r['entry_price'] * 100
        else:
            hi_pnl = (r['entry_price'] - r['highest_price']) / r['entry_price'] * 100
    if r['lowest_price'] and r['entry_price']:
        if r['side'] == 'LONG':
            lo_pnl = (r['lowest_price'] - r['entry_price']) / r['entry_price'] * 100
        else:
            lo_pnl = (r['entry_price'] - r['lowest_price']) / r['entry_price'] * 100

    hi_str = f"{hi_pnl:>7.1f}%" if hi_pnl is not None else "     N/A"
    lo_str = f"{lo_pnl:>7.1f}%" if lo_pnl is not None else "     N/A"
    p(f"{r['token']:<12} {r['side']:>6} {h:>6.1f}h {actual:>7.2f}% {est:>8.2f}% {hi_str} {lo_str}")

# --- 如果直接禁用 >12h 交易（不持有到12h以上的交易就不做）---
p("\n\n--- 激进方案：不做持仓会超过12h的交易（只看<=12h实际结果）---")
p(f"  <=12h 笔数: {len(le12)}, 实际WR: {wins_le/len(le12)*100:.1f}%, 累计PnL: {pnl_le:.2f}%")
p(f"  被过滤掉的 >12h 笔数: {len(gt12)}")

# 看看 >12h 的交易在 12h 的时候如果平掉到底是什么情况
p("\n\n--- >12h 交易的 12h 时刻实际盈亏估算（线性插值详细）---")
gt12_wins_at_12 = sum(1 for r in gt12 if r['pnl_pct'] * (12.0 / r['hold_hours']) > 0)
gt12_linear_pnl = sum(r['pnl_pct'] * (12.0 / r['hold_hours']) for r in gt12)
gt12_actual_wins = sum(1 for r in gt12 if r['pnl_pct'] > 0)
gt12_actual_pnl = sum(r['pnl_pct'] for r in gt12)

p(f"  >12h 实际: {len(gt12)}笔, WR={gt12_actual_wins}/{len(gt12)}={gt12_actual_wins/len(gt12)*100:.1f}%, SumPnL={gt12_actual_pnl:.2f}%")
p(f"  >12h 估计@12h: {len(gt12)}笔, WR={gt12_wins_at_12}/{len(gt12)}={gt12_wins_at_12/len(gt12)*100:.1f}%, SumPnL={gt12_linear_pnl:.2f}%")

p(f"\n  在12h平掉会改变其中 {sum(1 for r in gt12 if r['pnl_pct'] * (12.0 / r['hold_hours']) > 0) - gt12_actual_wins} 笔的结果")
for r in gt12:
    h = r['hold_hours']
    est = r['pnl_pct'] * (12.0 / h)
    actual_win = r['pnl_pct'] > 0
    est_win = est > 0
    if actual_win != est_win:
        p(f"    {r['token']} {r['side']}: 实际{r['pnl_pct']:+.2f}%({h:.1f}h) -> 12h估计{est:+.2f}%, {'亏转盈' if est_win else '盈转亏'}")

# --- 最终对比总结 ---
p("\n\n" + "=" * 80)
p("最终对比总结")
p("=" * 80)

# Current actual
cur_actual_wr = sum(1 for r in rows if r['pnl_pct'] > 0) / len(rows) * 100
cur_actual_pnl = sum(r['pnl_pct'] for r in rows)

# 12h rule (linear estimate for >12h)
all_12h_pnls = [r['pnl_pct'] for r in le12]
for r in gt12:
    all_12h_pnls.append(r['pnl_pct'] * (12.0 / r['hold_hours']))
rule_wr = sum(1 for p in all_12h_pnls if p > 0) / len(all_12h_pnls) * 100
rule_pnl = sum(all_12h_pnls)

p(f"\n{'':<25} {'胜率':>10} {'累计盈亏':>12} {'均盈亏':>10}")
p("-" * 55)
p(f"{'当前实际（不限时）':<25} {cur_actual_wr:>9.1f}% {cur_actual_pnl:>11.2f}% {cur_actual_pnl/len(rows):>9.2f}%")
p(f"{'强制12h平仓（线性估算）':<25} {rule_wr:>9.1f}% {rule_pnl:>11.2f}% {rule_pnl/len(rows):>9.2f}%")

# delta
p(f"\n胜率变化: {rule_wr - cur_actual_wr:+.1f}%")
p(f"累计盈亏变化: {rule_pnl - cur_actual_pnl:+.2f}%")

with open(r'C:\Users\Administrator\Desktop\小龙虾学习手册\Hermes\bsm-agent\extra\_kol_12h_sim.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(out))

conn.close()
print("\nDone!")
