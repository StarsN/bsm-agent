# KOL Agent 数据流审核

```
═══════════════════════════════════════════════════════════════════════════
                          采集 & 累积
═══════════════════════════════════════════════════════════════════════════

  每 3s: _scan_candidates()
    → _build_leaderboard_items()          读热度榜
    → build_trade_candidates_from_leaderboard(items, passed_only=True)
      → evaluate_candidate() × N
        → if market.snapshot.mark_price 为空 → skip
        → if not passed → skip
      → 返回 passed 候选列表

  累积:
    for c in candidates:
      主:  _collected[token] = {data, tier, ...}    无条件
      KOL: if kol_agent_enabled && c.passed:
             _kol_collected[token] = {data: _build_data_blob(c), ...}

═══════════════════════════════════════════════════════════════════════════
                          KOL 触发 & 入库
═══════════════════════════════════════════════════════════════════════════

  每轮检查 (独立于主 flush):
    if kol_agent_enabled
       and _kol_collected 非空
       and not _kol_running
       and now - _kol_last_flush >= kol_interval_seconds:

      _kol_round += 1
      batch = list(_kol_collected.values())
      → kol_candidates_insert_batch(conn, _kol_round, batch)
      → kol_candidates_purge_old(conn, keep_last_rounds=30)   ← 用 round_number
      → _kol_collected.clear()
      → threading.Thread(target=_run_kol_agent)

───────────────────────────────────────────────────────────────────────────
  启动恢复:
    SELECT COALESCE(MAX(round_number), 0) FROM kol_candidates
    → _kol_round = 恢复值
    防止重启后 flush 的 round_number 跟已有数据冲突
───────────────────────────────────────────────────────────────────────────

═══════════════════════════════════════════════════════════════════════════
                          LLM 分析 (后台线程)
═══════════════════════════════════════════════════════════════════════════

  _run_kol_agent()
    → analyze_candidates(conn)

  1. load_kol_knowledge(dir)           缓存 5min，扫描 Theory/TradeFramework/

  2. get_kol_candidates(conn)           ★ 时间窗口 ★
     SELECT data FROM kol_candidates
     WHERE created_at >= datetime('now', '-{interval + 2} minutes')
     ORDER BY id
     → json.loads(r["data"]) × N       还原 26 个扁平字段

  3. build_system_prompt(kol_data)     拼接 KOL 知识 + 输出格式

  4. build_user_prompt(candidates)     23 个市场数据字段 (无 social_score/mentions)

  5. call_deepseek(system, user)       reasoning_effort=max

  6. _parse_response(raw)              JSON 容错提取

  7. kol_analysis_insert(conn, a) × N  写入 kol_analyses (含 prompt)

  8. _execute_kol_trades(conn, analyses, candidates)
     direction in ("long","short") && confidence >= 70
     → open_paper_position(..., strategy="kol_agent", side=side)
     → conn.commit()

═══════════════════════════════════════════════════════════════════════════
                        页面展示
═══════════════════════════════════════════════════════════════════════════

  GET /api/kol/analyses
    → kol_analyses_latest(conn)         12 小时窗口
    → 按 token 分组，每个最多 8 条
    → 前端 token 标签 + TradingView K线 + 卡片列表


═══════════════════════════════════════════════════════════════════════════
                    与主 Agent 对比
═══════════════════════════════════════════════════════════════════════════

                  主 Agent                     KOL Agent
  ─────────────── ──────────────────────────   ──────────────────────────
  入库表           agent_candidates             kol_candidates
  读数据方式       时间窗口 (interval+2min)      时间窗口 (interval+2min)  ✓
  LLM              Claude (hermes)              DeepSeek (直接)
  下单              write_decisions → auto_trader   _execute_kol_trades → open_paper_position
  数据累积          _collected (共享)             _kol_collected (独立)
  触发间隔          agent_collect_interval       kol_agent_interval


═══════════════════════════════════════════════════════════════════════════
                    审查结论
═══════════════════════════════════════════════════════════════════════════

  ✓ 采集合约扫描 passed 候选，无 market price 的跳过
  ✓ 累积去重（同 token 覆盖）
  ✓ 开关关闭不累积
  ✓ 时间窗口读数据（17 分钟），跟主 Agent 一致
  ✓ 重启后 _kol_round 从 DB 恢复，编号连续
  ✓ purge 用 round_number 截断，独立于读路径
  ✓ LLM 拿到的是最近 interval+2 分钟内的候选
  ✓ 无遗漏
