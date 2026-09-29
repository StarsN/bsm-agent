# 热度有教训 Agent (heat_agent_lessons) 全链路审核

```
═══════════════════════════════════════════════════════════════════════════
① 数据源（与无教训版共享 token_heat_history）
═══════════════════════════════════════════════════════════════════════════

  worker 每轮写 token_heat_history（热度榜）
    → 两个热度 Agent 读同一个表，数据源完全相同

═══════════════════════════════════════════════════════════════════════════
② 触发（独立间隔，刚修复嵌套bug）
═══════════════════════════════════════════════════════════════════════════

  worker.py:
    ┌─ 热 度 Agent (原 版): _ROUND_NUMBER % trigger_interval == 0
    │   → hermes cron HEAT_AGENT_HERMES_JOB_ID
    │
    └─ 热度有教训版(新):  _ROUND_NUMBER % lessons_trigger == 0    ← 独立块
        → hermes cron HEAT_AGENT_LESSONS_HERMES_JOB_ID
        (需填入 job ID 才生效)

═══════════════════════════════════════════════════════════════════════════
③ Agent 读取数据
═══════════════════════════════════════════════════════════════════════════

  extract_market_data.py:
    ┌─────────────────────────────────────────────────────────┐
    │ candidates:      token_heat_history + market_snapshots  │
    │ positions:       strategy='heat_agent_lessons' ✓       │
    │ account:         strategy_initial_heat_agent_lessons ✓ │
    │ agent_lessons:   strategy='heat_agent_lessons'         │
    │                  AND learned=0 ✓                        │
    │ archive_lessons: strategy='heat_agent_lessons' ✓       │
    │ tag_stats:       strategy='heat_agent_lessons' ✓       │
    │ today_journal:   strategy='heat_agent_lessons' ✓       │
    └─────────────────────────────────────────────────────────┘

  SKILL.md → Agent 查教训 → 做决策
    → agent_lessons: 有教训（learned=0）← 跟无教训版唯一区别
    → archive_lessons + tag_stats: 系统标签

═══════════════════════════════════════════════════════════════════════════
④ 写决策
═══════════════════════════════════════════════════════════════════════════

  write_decisions.py --source token_heat_history_lessons
    → pending_decisions(source='token_heat_history_lessons')

═══════════════════════════════════════════════════════════════════════════
⑤ 系统执行
═══════════════════════════════════════════════════════════════════════════

  auto_trader:
    source_map['token_heat_history_lessons'] = 'heat_agent_lessons' ✓
    → execute_open(strategy='heat_agent_lessons')
      → 去重: trade_has_active(token, 'heat_agent_lessons')
      → 风控: check_account_risk(account, token)
      → 锁: trade_signal_lock_acquire(token, key, 'heat_agent_lessons')
      → 入库: trade_position_insert(strategy='heat_agent_lessons')

═══════════════════════════════════════════════════════════════════════════
⑥ 平仓管理
═══════════════════════════════════════════════════════════════════════════

  update_paper_positions → 自动按 strategy 隔离 ✓

═══════════════════════════════════════════════════════════════════════════
⑦ 页面展示
═══════════════════════════════════════════════════════════════════════════

  /agent-heat-lessons
    → AGENT_STRATEGY = 'heat_agent_lessons'
    → p.strategy === 'heat_agent_lessons'
    → 策略页 badge: 🔥 热度有教训 ✓
    → 设置页: strategy_initial_heat_agent_lessons ✓

═══════════════════════════════════════════════════════════════════════════
                      审查结论
═══════════════════════════════════════════════════════════════════════════

  ✓ 数据源: token_heat_history，跟无教训版同源
  ✓ 触发: 独立间隔块（刚修复，不再嵌套）
  ✓ 初始资金: strategy_initial_heat_agent_lessons ✓
  ✓ 持仓隔离: strategy='heat_agent_lessons' ✓
  ✓ 教训: learned=0 + strategy='heat_agent_lessons' ✓
  ✓ 系统标签: archive_lessons + tag_stats 独立 ✓
  ✓ auto_trader: source_map ✓
  ✓ 页面: /agent-heat-lessons ✓
  ✓ 索引: UNIQUE(token, side, strategy) ✓

  ⚠ HEAT_AGENT_LESSONS_HERMES_JOB_ID 为空，需创建 hermes cron job
