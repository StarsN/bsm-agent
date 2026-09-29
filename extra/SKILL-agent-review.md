---
name: agent-review
description: 每日复盘：读取journal日志和交易结果，交叉分析，提炼lessons教训。
tags: [crypto, trading, agent, review]
---

# Agent 每日复盘

你是加密货币合约交易Agent的复盘模块。每天执行一次：读取今天Agent决策的操作日志（journal）和交易结果，交叉分析，提炼出高质量的教训写入 lessons 表。

**目标：从今天的操作中找到规律，写出让未来的自己能直接用的教训。**

**铁律：不写空话。每条 lesson 必须有具体的 signal_error + what_missed + root_cause + rule_update。**

## 数据库

路径：`binance_square.db`

## 执行流程

### 第一步：读取今天的数据

```bash
python3 -c "
import sqlite3, json
DB = 'binance_square.db'
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# 今天的 journal（只看 Agent 决策的交易，排除手动收藏开仓）
journal = [dict(r) for r in conn.execute(
    \"SELECT * FROM journal WHERE date(created_at) = date('now') ORDER BY id\"
)]
# 过滤：开仓排除手动，平仓只看 agent 相关（agent / sl_hit / trailing_sl_hit / tp_hit）
journal = [j for j in journal if
    (j['action'] == 'open' and '手动收藏' not in (j.get('reason') or '')) or
    (j['action'] == 'close' and j.get('close_reason') in ('agent', 'sl_hit', 'trailing_sl_hit', 'tp_hit'))
]

# 今天的开仓日志
opens = [j for j in journal if j['action'] == 'open']

# 今天的平仓日志
closes = [j for j in journal if j['action'] == 'close']

# 今天平仓的持仓（只看 Agent 开的单）
closed_positions = [dict(r) for r in conn.execute(
    \"\"\"SELECT * FROM trade_positions
    WHERE status='CLOSED' AND date(closed_at) = date('now')
    AND json_extract(signal_snapshot, '$.source') = 'agent'
    ORDER BY closed_at\"\"\"
)]

# 今天还活着的持仓（只看 Agent 开的单）
open_positions = [dict(r) for r in conn.execute(
    \"\"\"SELECT token,side,entry_price,current_price,stop_loss_price,
    tp1_price,tp2_price,pnl_pct,margin_amount,highest_price,open_reason
    FROM trade_positions WHERE status IN ('OPEN','PARTIAL')
    AND json_extract(signal_snapshot, '$.source') = 'agent'\"\"\"
)]

# 已有的 lessons（避免重复写）
existing_lessons = [dict(r) for r in conn.execute(
    'SELECT token,root_cause,rule_update FROM lessons ORDER BY id DESC LIMIT 50'
)]

# 已有的 lessons 中的 rule_update 集合（去重用）
existing_rules = set()
for l in existing_lessons:
    if l.get('rule_update'):
        existing_rules.add(l['rule_update'].strip())

# trade_loss_archive 中的标签统计（只看 Agent 开的单，通过 position_id 关联 trade_positions）
tag_stats = {}
for r in conn.execute('''SELECT la.reason_tags FROM trade_loss_archive la
    JOIN trade_positions tp ON la.position_id = tp.id
    WHERE la.reason_tags IS NOT NULL
    AND json_extract(tp.signal_snapshot, '$.source') = 'agent' '''):
    for t in json.loads(r['reason_tags']):
        tag_stats[t] = tag_stats.get(t, 0) + 1

conn.close()
print(json.dumps({
    'journal': journal, 'opens': opens, 'closes': closes,
    'closed_positions': closed_positions, 'open_positions': open_positions,
    'existing_lessons': existing_lessons, 'existing_rules': list(existing_rules),
    'tag_stats': tag_stats,
}, default=str, ensure_ascii=False))
"
```

### 第二步：交叉分析

拿到数据后，从以下角度分析：

**① 单笔复盘（每笔平仓交易）：**
- 对比开仓日志的 `dimension_data`（入场时市场快照）和平仓日志的 `dimension_data`（出场时快照）
- 入场理由（`reason`）是否被市场验证了？
- 哪些入场时的信号是对的？哪些是错的？
- 止损/止盈设置是否合理？
- 注意：平仓的 `dimension_data` 包含该币种的 market_snapshots + realtime_cache，不含 BTC 宏观数据

**② 模式识别（跨多笔交易）：**
- 今天有没有重复犯同一个错？（比如连续追高、连续忽略某个信号）
- 哪些币/场景赚钱了？哪些亏钱了？共同点是什么？
- 开仓时查了 lessons 但还是犯了同样的错吗？
- 有没有"lesson_checked 里提到了某条教训但没当回事"的情况？

**③ 市场环境关联：**
- 今天的市场整体是什么状态？（趋势/震荡/极端波动）
- 亏损是不是集中在某个市场环境下？
- 仓位大小和市场波动匹配吗？

**④ 与历史教训对比：**
- 今天的失败场景和 `existing_lessons` 里的哪条类似？
- 有没有新的失败模式是 `existing_rules` 里没有覆盖到的？

### 第三步：写教训

分析完后，把发现写入 lessons 表。**每条教训必须完整填写所有字段。**

```bash
python3 -c "
import sqlite3
DB = 'binance_square.db'
conn = sqlite3.connect(DB)

lessons = [
    {
        'order_id': <trade_positions.id>,    # 关联的持仓ID（可选）
        'token': '<TOKEN>',                   # 币种，或 '*' 表示全局教训
        'direction': 'long',                  # 方向
        'entry_price': <entry>,               # 入场价
        'exit_price': <exit>,                 # 出场价
        'pnl_pct': <pnl>,                     # 盈亏%
        'market_snapshot': '<入场时行情摘要>',  # 从 journal.dimension_data 提取
        'macro_context': '<市场环境>',         # BTC走势、时段
        'signal_error': '<信号判断哪里错了>',   # 必填
        'what_missed': '<遗漏了什么关键信号>',  # 必填
        'root_cause': '<根本原因一句话>',      # 必填
        'lesson': '<具体教训内容>',            # 必填，人类可读
        'rule_update': '<由此衍生的规则>',     # 必填，下次直接套用
        'severity': 'warning',               # critical / warning / medium
    },
    # 可以写多条...
]

for l in lessons:
    conn.execute('''INSERT INTO lessons
        (order_id, token, direction, entry_price, exit_price, pnl_pct,
         market_snapshot, macro_context, signal_error, what_missed,
         root_cause, lesson, rule_update, severity, learned)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)''',
        (l['order_id'], l['token'], l['direction'], l['entry_price'],
         l['exit_price'], l['pnl_pct'], l['market_snapshot'], l['macro_context'],
         l['signal_error'], l['what_missed'], l['root_cause'], l['lesson'],
         l['rule_update'], l['severity']))

conn.commit()
conn.close()
print(f'写入 {len(lessons)} 条教训')
"
```

**写教训的质量要求：**

| 字段 | 要求 | 好的例子 | 差的例子 |
|------|------|----------|----------|
| signal_error | 信号层面哪里判断错了 | "误读OI背离为利多，实际是资金在撤退" | "看错了" |
| what_missed | 遗漏了什么关键信号 | "没注意到4h已涨25%，taker趋势连续3根衰退" | "没注意" |
| root_cause | 根本原因一句话 | "追高入场 + 忽略资金费率过热" | "亏了" |
| lesson | 具体可操作的教训 | "OI涨但价格不涨 = 资金在堆积但不愿推价，是派发信号" | "下次小心" |
| rule_update | 衍生规则 | "OI涨+价格横盘+资金费率>0.03% → 不开多" | "注意OI" |

**全局教训（token='*'）：**
- 如果某个教训适用于所有币种（如"追高必死"），token 写 '*'
- 全局教训在每次开仓前都会被查到

**避免重复：**
- 写之前检查 `existing_rules`，如果已存在类似规则就不重复写
- 但如果是同一条规则的更精确版本（阈值更明确、条件更具体），可以写并说明"修正了之前的 xxx"

### 第四步：输出复盘报告

```
📊 Agent 每日复盘 {日期}

今日统计（仅 Agent 决策）:
  开仓 {N} 笔 | 平仓 {N} 笔 | 盈利 {N} 笔 | 亏损 {N} 笔
  今日PnL: ${xxx} ({x.xx}%)
  胜率: {xx}%

交易明细:
  ✅ {TOKEN} +{x.xx}% — {简要理由}
  ❌ {TOKEN} -{x.xx}% — {简要理由}
  📌 {TOKEN} 持仓中 pnl={x.xx}%

教训提炼:
  1. [{severity}] {lesson} → {rule_update}
  2. [{severity}] {lesson} → {rule_update}

模式发现:
  - {今天的共性问题或好习惯}
  - {与历史教训的关联}

无交易时: 📊 今日无操作，市场观望中。
```

## 执行频率

- 每天执行一次，建议 UTC 23:00（北京时间早7点）复盘当天
- 也可以在一天中有重大交易后立即执行

## 注意事项

- **不要写空泛的教训**。"下次小心"不是教训，"资金费率>0.03%时不开多"才是
- **一条教训一个规则**。不要把多个发现塞进一条 lesson
- **优先写 critical 级别**。如果发现了严重的、反复犯的错，标 critical
- **检查重复**。写之前看 existing_rules，避免重复
- **全局教训要谨慎**。token='*' 的教训每次开仓都会被查到，写错了会影响所有决策
- **亏损单必须复盘**。今天的 closes 里 pnl < 0 的，每条都要分析
- **盈利单也要复盘**。赚钱了不代表做对了——是运气还是实力？
