# KOL Agent 全流程（采集 → 分析 → 入库 → 下单）

```
═══════════════════════════════════════════════════════════════════════════════
阶段 0: 开关配置
═══════════════════════════════════════════════════════════════════════════════

  /settings 页:
  ┌─ Agent 开关 面板 ────────────────────────┐
  │  Agent-合约扫描  [启用 v]                  │
  │  Agent-热度榜单  [启用 v]                  │
  │  Agent-KOL       [启用 v]  ← kol_agent_enabled  │
  ├─ 策略初始资金 ────────────────────────────┤
  │  KOL 策略初始 USDT  [1000]                 │
  │  ↑ strategy_initial_kol_agent             │
  └──────────────────────────────────────────┘
                    │
    保存 → POST /api/trading/settings
    → TradingSettingsBody (pydantic: kol_agent_enabled: bool, strategy_initial_kol_agent: float)
    → storage.trading_settings_update (allowed whitelist ✓)
    → trading_settings 表 (str(False) → "False")
    → 读取时 bool 解析 ✓

═══════════════════════════════════════════════════════════════════════════════
阶段 1: 采集候选币
═══════════════════════════════════════════════════════════════════════════════

  web.py _collect_loop() [daemon 线程，每 3s 循环]

  ┌─────────────────────────────────────────────────────────────────┐
  │ 1. sleep(3s)                                                    │
  │ 2. _cached("candidates_scan", TTL=2s, _scan_candidates)        │
  │    └─ _build_leaderboard_items(conn)                            │
  │    └─ build_trade_candidates_from_leaderboard(items,            │
  │         passed_only=True)                                       │
  │       └─ evaluate_candidate() × N                               │
  │          └─ 返回 {token, price, 15m, 1h, ..., tier, passed,     │
  │                   hard_block, signal_key, market, score}         │
  └──────────────────────────┬──────────────────────────────────────┘
                             │
                             ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │ 3. 双路累积:                                                     │
  │                                                                  │
  │ 主累积 _collected[token] = {                                     │
  │   data: _build_data_blob(c) 展平 JSON,  ← 26 个扁平字段          │
  │   tier, passed, signal_key ...                                  │
  │ }                                                                │
  │                                                                  │
  │ KOL累积 (if kol_agent_enabled && passed):                        │
  │ _kol_collected[token] = {                                        │
  │   data: _build_data_blob(c) 展平 JSON,  ← 同样的 26 字段         │
  │   tier, passed, signal_key ...                                  │
  │ }                                                                │
  │ 开关 OFF → 跳过累积，_kol_collected 不长                         │
  └──────────────────────────┬──────────────────────────────────────┘
                             │
                             ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │ 4. 每 60s 心跳:                                                  │
  │    ts_settings = storage.trading_settings_get(conn)              │
  │    → 刷新 kol_agent_interval_minutes, kol_agent_enabled          │
  │    → 刷新 agent_collect_interval_minutes                         │
  └─────────────────────────────────────────────────────────────────┘

═══════════════════════════════════════════════════════════════════════════════
阶段 2: KOL 独立触发
═══════════════════════════════════════════════════════════════════════════════

  ┌─────────────────────────────────────────────────────────────────┐
  │ 5. KOL flush 检查 (独立于主 flush，无锁):                         │
  │    条件: kol_agent_enabled AND _kol_collected 非空               │
  │          AND not _kol_running (防重叠)                           │
  │          AND now-_kol_last_flush >= kol_interval_seconds         │
  └──────────────────────────┬──────────────────────────────────────┘
                             │ 满足
                             ▼
  ┌─────────────────────────────────────────────────────────────────┐
  │ 6. KOL flush:                                                    │
  │    batch = list(_kol_collected.values())                         │
  │    → kol_candidates_insert_batch(conn, _kol_round, batch)        │
  │    → kol_candidates_purge_old(conn, keep=30)                     │
  │    → _kol_collected.clear()                                      │
  │    → _kol_running = True                                         │
  │    → threading.Thread(target=_run_kol_agent, daemon=True)        │
  └──────────────────────────┬──────────────────────────────────────┘
                             │
                             ▼
═══════════════════════════════════════════════════════════════════════════════
阶段 3: KOL Agent 分析 [后台线程]
═══════════════════════════════════════════════════════════════════════════════

  _run_kol_agent()
  ┌─────────────────────────────────────────────────────────────────┐
  │ → storage.get_conn()  (独立连接)                                 │
  │ → kol_agent.analyze_candidates(conn)                             │
  │                                                                  │
  │   3a. load_kol_knowledge(dir)     ← KOL 文件缓存 5min            │
  │       └─ _load_kol_files():                                       │
  │          扫描 Theory/TradeFramework/DataSource/                   │
  │          BTC_Alert__Theory.md 等                                  │
  │          theory+framework 必备                                   │
  │                                                                  │
  │   3b. get_kol_candidates(conn)                                   │
  │       └─ kol_candidates_latest_round(conn)                       │
  │            SELECT MAX(round_number) → WHERE round_number=?       │
  │       └─ json.loads(row["data"]) × N                             │
  │          还原 26 个扁平字段 (token, price, 15m, funding, ...)    │
  │                                                                  │
  │   3c. build_system_prompt(kol_data)                              │
  │       └─ 拼接 KOL 理论+框架+数据源 + 输出格式 + 字段说明          │
  │          confidence: "0-100整数，>=80高把握，50-79中等，<50低"   │
  │                                                                  │
  │   3d. build_user_prompt(candidates)                              │
  │       └─ 拼接候选币市场数据 (23 项，去掉社交分+提及+持仓)         │
  │          price, 15m, 1h, 4h, 24h, oi_*, funding, taker, ...     │
  │                                                                  │
  │   3e. call_deepseek(system, user)   ← urllib, 120s timeout       │
  │       └─ model: deepseek-v4-pro, temperature: 0.3                │
  │       └─ max_tokens: 4096                                        │
  │       └─ 返回 JSON: {"analyses": [...]}                          │
  │                                                                  │
  │   3f. _parse_response(raw)                                      │
  │       └─ 直接解析 → 失败则正则提取 JSON 块                        │
  └──────────────────────────┬──────────────────────────────────────┘
                             │
                             ▼
═══════════════════════════════════════════════════════════════════════════════
阶段 4: 写入分析结果
═══════════════════════════════════════════════════════════════════════════════

  ┌─────────────────────────────────────────────────────────────────┐
  │   3g. 写入 kol_analyses 表:                                      │
  │       for a in analyses:                                         │
  │         a["raw_response"] = raw                                  │
  │         a["system_prompt"] = system   ← 存 prompt                 │
  │         a["user_prompt"] = user                                  │
  │         storage.kol_analysis_insert(conn, a)                     │
  │       conn.commit()                                              │
  └──────────────────────────┬──────────────────────────────────────┘
                             │
                             ▼
═══════════════════════════════════════════════════════════════════════════════
阶段 5: 接入系统下单
═══════════════════════════════════════════════════════════════════════════════

  ┌─────────────────────────────────────────────────────────────────┐
  │   3h. _execute_kol_trades(conn, analyses, candidates):           │
  │                                                                  │
  │   candidate_map = {token: 原始行情数据}                          │
  │   for a in analyses:                                             │
  │     direction != "long"  → skip  (如 ORDI=none)                   │
  │     confidence < 70      → skip  (如 ORDI=45)                     │
  │     token not in map     → skip                                  │
  │                                                                  │
  │     direction=long && confidence>=70:                             │
  │     ┌─────────────────────────────────────────┐                  │
  │     │ 构建 candidate dict:                     │                  │
  │     │   passed=True                            │                  │
  │     │   has_active_position=False              │                  │
  │     │   tier = full(>=80) / half(70-79)       │                  │
  │     │   price = original["price"]             │                  │
  │     │   signal_key = original["signal_key"]   │                  │
  │     │   analysis_score = confidence           │                  │
  │     └──────────────┬──────────────────────────┘                  │
  │                    │                                              │
  │                    ▼                                              │
  │     open_paper_position(conn, candidate, settings,                │
  │                         strategy="kol_agent")                     │
  │     ┌─────────────────────────────────────────────────┐         │
  │     │ ① 去重: trade_has_active(token, "kol_agent")    │         │
  │     │ ② 风控: _build_account_context("kol_agent")     │         │
  │     │    → strategy_initial_kol_agent (初始资金)      │         │
  │     │    → WHERE strategy='kol_agent' (日亏损熔断)    │         │
  │     │    → WHERE strategy='kol_agent' (持仓上限)      │         │
  │     │    → WHERE strategy='kol_agent' (冷却期)        │         │
  │     │ ③ ATR 自适应止损                               │         │
  │     │ ④ 信号锁: trade_signal_lock_acquire(           │         │
  │     │      token, signal_key, "kol_agent")           │         │
  │     │ ⑤ 仓位反推: risk.compute_position_size()       │         │
  │     │    → equity = initial + realized + unrealized   │         │
  │     │    → margin = risk / |entry - stop|             │         │
  │     │ ⑥ 止盈: TP1=+1R关80%, TP2=+2R关10%             │         │
  │     │ ⑦ 落库: trade_positions 表                     │         │
  │     │    strategy='kol_agent'                        │         │
  │     │    open_reason 含 confidence 分                │         │
  │     └─────────────────────────────────────────────────┘         │
  │                                                                  │
  │   if opened > 0: conn.commit()                                   │
  └──────────────────────────┬──────────────────────────────────────┘
                             │
                             ▼
                    finally: _kol_running = False

═══════════════════════════════════════════════════════════════════════════════
阶段 6: 页面展示
═══════════════════════════════════════════════════════════════════════════════

  /kol ──────────────────────────────────────────────────────┐
  │  GET /api/kol/analyses?limit=30                           │
  │  └─ kol_analyses_latest(conn)                             │
  │     └─ WHERE created_at >= now-(interval+2) minutes       │
  │  renderCard:                                               │
  │    direction: LONG(绿) / SHORT(红) / NONE(灰)              │
  │    confidence: 82 → ≥80绿 / 78→≥50黄 / <50红              │
  │    price_levels 表格, timeline, position_analysis,         │
  │    timing, risk_control, reason                           │
  └──────────────────────────────────────────────────────────┘

  /strategies ───────────────────────────────────────────────┐
  │  GET /api/strategy/stats                                  │
  │  └─ GROUP BY strategy → 自动发现 kol_agent               │
  │  badge: "🧠 KOL策略"                                      │
  │  account: strategy_initial_kol_agent                      │
  │  open_positions: WHERE strategy='kol_agent'               │
  │  closed_positions: WHERE strategy='kol_agent'             │
  └──────────────────────────────────────────────────────────┘

  /settings ─────────────────────────────────────────────────┐
  │  Agent 开关: kol_agent_enabled [启用 v / 关闭 v]           │
  │  策略初始资金: strategy_initial_kol_agent [1000]           │
  │  Agent 参数: kol_agent_interval_minutes [15]               │
  └──────────────────────────────────────────────────────────┘


═══════════════════════════════════════════════════════════════════════════════
                    下单条件 & 分档逻辑
═══════════════════════════════════════════════════════════════════════════════

  direction  │  confidence  │  行为
  ───────────┼──────────────┼──────────────────────────
  none       │  任意        │  skip，不分析也不下单
  long       │  < 70        │  skip，分析入库但不下单
  long       │  70 - 79     │  tier=half → 半仓下单
  long       │  >= 80       │  tier=full → 满仓下单
  short      │  任意        │  skip (当前仅支持做多)


═══════════════════════════════════════════════════════════════════════════════
                    数据隔离确认
═══════════════════════════════════════════════════════════════════════════════

  隔离项           │ 机制
  ─────────────────┼─────────────────────────────────────────
  初始资金          │ strategy_initial_kol_agent (settings)
  持仓查询          │ WHERE strategy = 'kol_agent'
  账户权益          │ initial + SUM(realized_pnl WHERE strategy='kol_agent')
  日亏损熔断        │ SUM(realized_pnl WHERE strategy='kol_agent' AND today)
  持仓上限          │ COUNT(OPEN|PARTIAL WHERE strategy='kol_agent')
  冷却期            │ WHERE strategy='kol_agent' AND status='CLOSED' AND pnl<0
  信号锁            │ UNIQUE(token, signal_key, strategy)
  策略页面          │ GROUP BY strategy → 自动发现
  其他策略          │ agent / heat_agent / system / manual 互不影响
