# 无教训版 Agent (agent_no_lessons) 全链路审核

```
═══════════════════════════════════════════════════════════════════════════
① 采集 & 累积
═══════════════════════════════════════════════════════════════════════════

  collector 每 3s: _scan_candidates()
    → _build_leaderboard_items()
    → build_trade_candidates_from_leaderboard(items, passed_only=True)
    → 返回 passed 候选列表

  累积:
    for c in candidates:
      主 Agent:   _collected[token] = _build_data_blob(c)    无条件
      无教训版:   _nl_collected[token] = _build_data_blob(c)  if c.passed

═══════════════════════════════════════════════════════════════════════════
② 定时 Flush (每 30min)
═══════════════════════════════════════════════════════════════════════════

  if _nl_collected and not _nl_running and now-_nl_last_flush >= 30min:
    _nl_round += 1
    batch = list(_nl_collected.values())
    → nl_candidates_insert_batch(conn, _nl_round, batch)      存 DB
    → nl_candidates_purge_old(conn, keep=30)                   清理旧轮
    → _nl_collected.clear()                                    清内存
    → threading.Thread(target=_run_nl_agent)                   异步触发

  重启安全: _nl_round 从 DB 恢复 MAX(round_number)

═══════════════════════════════════════════════════════════════════════════
③ Agent 执行 (hermes cron)
═══════════════════════════════════════════════════════════════════════════

  _run_nl_agent()
    → hermes cron run NL_AGENT_HERMES_JOB_ID (需配置)

  SKILL.md → extract_market_data.py:
    ┌─────────────────────────────────────────────────────────┐
    │ candidates:      nl_candidates 时间窗口 ✓              │
    │ positions:       strategy='agent_no_lessons' ✓         │
    │ account:         strategy_initial_agent_no_lessons ✓   │
    │ agent_lessons:   strategy='agent_no_lessons' (空) ✓    │
    │ archive_lessons: strategy='agent_no_lessons' ✓         │
    │ tag_stats:       strategy='agent_no_lessons' ✓         │
    │ today_journal:   strategy='agent_no_lessons' ✓         │
    └─────────────────────────────────────────────────────────┘

  SKILL.md → Agent 查教训 → 做决策 (agent_lessons 天然为空)

═══════════════════════════════════════════════════════════════════════════
④ 写决策
═══════════════════════════════════════════════════════════════════════════

  write_decisions.py --source nl_candidates
    → pending_decisions(source='nl_candidates', status='pending')

═══════════════════════════════════════════════════════════════════════════
⑤ 系统执行
═══════════════════════════════════════════════════════════════════════════

  auto_trader.one_scan()
    → source_map:
        'nl_candidates' → 'agent_no_lessons' ✓
    → execute_open(dec, settings, strategy='agent_no_lessons')
      → trade_has_active(token, 'agent_no_lessons')    去重
      → check_account_risk(account, token)              风控
      → trade_signal_lock_acquire(token, key, 'agent_no_lessons')
      → trade_position_insert(strategy='agent_no_lessons')

═══════════════════════════════════════════════════════════════════════════
⑥ 平仓管理
═══════════════════════════════════════════════════════════════════════════

  update_paper_positions(conn)
    → 自动按 trade_positions.strategy 隔离
    → strategy='agent_no_lessons' 独立核算

═══════════════════════════════════════════════════════════════════════════
⑦ 页面展示
═══════════════════════════════════════════════════════════════════════════

  /agent-nl → AGENT_HTML.replace('agent' → 'agent_no_lessons')
    → AGENT_STRATEGY = 'agent_no_lessons'
    → p.strategy === 'agent_no_lessons'
    → API 全部带 ?strategy='agent_no_lessons'
    → 策略页 badge: 无教训对照 ✓
    → 设置页初始金额: strategy_initial_agent_no_lessons ✓


═══════════════════════════════════════════════════════════════════════════
                      审查结论
═══════════════════════════════════════════════════════════════════════════

  ✓ 候选池独立: nl_candidates 表，时间窗口读取
  ✓ 初始资金独立: strategy_initial_agent_no_lessons (刚修复)
  ✓ 持仓隔离: WHERE strategy='agent_no_lessons' (刚修复)
  ✓ 教训隔离: WHERE strategy='agent_no_lessons' (刚修复)
  ✓ 止损记录隔离: WHERE strategy='agent_no_lessons' (刚修复)
  ✓ auto_trader 映射: nl_candidates → agent_no_lessons ✓
  ✓ 页面隔离: AGENT_STRATEGY='agent_no_lessons' ✓
  ✓ 索引隔离: UNIQUE(token, side, strategy) ✓

  ⚠ NL_AGENT_HERMES_JOB_ID 为空，需创建 hermes cron job
