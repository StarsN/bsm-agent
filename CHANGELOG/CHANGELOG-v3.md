# CHANGELOG v3.0

## v4.1 — Agent 候选池同源面板 + 噪音过滤 + 无罪推论（2026-05-14）

- **候选池同源面板**：web.py 后台 collector 线程每 3s 复现"合约扫描与操作建议"，passed 币种存入 `agent_candidates`，每隔 15min 入库触发 Agent。extract 脚本改读时间窗口（17min 含 2min 缓冲），与面板零差异。
- **Agent 触发可配置**：`AGENT_COLLECT_POLL_SECONDS`/`AGENT_COLLECT_INTERVAL_MINUTES`/`AGENT_COLLECT_CACHE_TTL` 三个 config 参数，面板+collector 共享 `_cached` 缓存。
- **噪音止损过滤**：extract_review_data.py 新增 5 维度加权评分（OI 动量/OI 加速度/taker/跌幅/盘口），≥5 且无单维 ≤-2 自动标记 reviewed=1 不推 Agent。模糊地带推给 Agent 自主判断。
- **复盘无罪推论**：SKILL 新增"逻辑研判错误"vs"市场随机噪音"分类。噪音单 `root_cause="市场随机噪音"`，`rule_update` 留空，禁止为随机噪音编造规则。
- **verdict/direction 屏蔽**：FIELD_CN 删 verdict/direction 映射、`_build_close_snap` 删 信号判定/走向、extract strip 列表新增——Agent 不再看到系统判断字段，完全自主决策。
- **Mem0 记忆系统全移除**：sync_memory.py 删除，web API/面板/前端/search/requirements 全部清理。
- **关仓快照中文平铺**：`_build_close_snap()` 输出中文平铺 JSON，与开仓格式一致。复盘可对比入场/出场。
- **复盘 Skill 重造**：第二步新增全维 Delta 分析（OI/Taker/大户多空/深度）、宏观共振、成功固化；去重/孤儿过滤/噪音过滤前置处理。
- **交易 Skill 改造**：冷启动引导、lessons 非空过滤、字段声明补全（fear_greed/account/today_journal/positions）、数据库表结构文档更新。
- **Worker 浏览器卡死防护**：SingletonLock 清理 + `launch_persistent_context` 120s 超时 + 整轮 420s 超时三级防护。
- **重置账户优化**：保留 lessons 和 journal 数据，只标记 reviewed=1/learned=1，不物理删除。
- **CPU 优化**：pollWatchlistRealtime 1s→5s；collector 与面板共享 `candidates_scan` 缓存。
- **面板交互优化**：Agent 交易频率下拉框、保存/重置按钮居中、旧开仓规则描述删除、已平仓滚动位置保持。

---

## v3.4 — Skill 模式化 + 止损优化 + 面板增强（2026-05-11）

- **教训模式化**：复盘和交易 skill 从"硬规则"改为三段式模式（情境+倾向+策略）。Agent 不再写"X>Y就不开仓"，而是描述市场条件、概率判断和行动建议。两个 skill 的 lessons字段说明、SKILL.md、常见陷阱模式全部对齐。
- **写教训去重移除**：`write_lessons.py` 去重逻辑去掉，由 Agent 自查 `existing_lessons` 判断重复。
- **复盘精简**：去掉活跃持仓查询，只复盘已平仓数据。
- **止损优化**：`TRADING_STOP_LOSS_MIN_PCT` 从 -1.2% 放宽到 -2.0%，减少正常波动扫到止损。
- **面板增强**：教训面板加 #ID、NEW 标签、全部/活跃切换、learned 状态按钮；持仓表加 TP2 列；今日 PnL 修正为美元显示。
- **Bug 修复**：跟踪止盈和取消收藏平仓的 `record_trade_from_journal` 调用补齐。

---

## v3.3 — 记忆搜索接入（2026-05-11）

- **记忆结构优化**：`sync_memory.py` 重写，一笔交易一条完整记忆（数据+决策+结果），`record_open/record_close` 合并为 `record_trade`。平仓时从 journal 查开仓信息，合并写入。
- **Agent 查记忆**：交易 SKILL 加第四步"查记忆"，Agent 认真考虑开仓时调用 `search_memory.py` 搜类似市场特征的历史结果。配合 SQLite 教训做双轨决策：规则说"不能做什么"，记忆说"这样做的结果"。
- **搜索脚本**：新增 `scripts/search_memory.py` 和 `references/查记忆字段说明.md`。
- **复盘 SKILL 完善**：JSON 格式明确包含 `deprecate_ids`，支持标记旧规则废弃。

---

## v3.2 — CPU 优化 + 止损分析（2026-05-10）

- **Agent 面板**：账户重排 2×2+3×2、最大回撤 1h 缓存、持仓加时间列、全部分页 5 条/页、时间线年月日。
- **CPU 优化**：去掉时间线/教训/日志定时刷新，overview 缓存 15s，WAL checkpoint 保持默认。
- **止损分析**：20 笔样本，TRADING_STOP_LOSS_MIN_PCT=-1.2% 过紧，5 笔被扫到。建议提到 -2.0%，待 50 笔后复查。

---

## v3.1 — Mem0 记忆 + Agent 面板增强（2026-05-09）

### Mem0 记忆系统

- 新增 `sync_memory.py`：`record_open/record_close/search_similar`。交易全生命周期写入记忆。
- `auto_trader.py` + `trade_logic.py`：开仓/平仓后同步写 Mem0，覆盖 Agent 平仓和系统平仓（止损/止盈/取消收藏）。
- `config.py` 加 `MEM0_ENABLED/API_KEY/USER_ID`，`requirements.txt` 加 `mem0ai`。
- 存中文原文（`infer=False`）+ metadata（token/pnl/result），搜索支持 metadata 过滤（按币种/盈亏）。
- Agent 面板新增记忆搜索栏（`/api/agent/memory/search`），卡片式展示结果。

### Agent 面板优化

- 账户重排：上 4 格 2×2（总资产/可用、今日 PnL、胜率、资金利用率），下 6 格 3×2（今日开/平、待处理、今日被拒、总盈亏、最大回撤、胜/总）。
- 最大回撤 1 小时缓存，避免扫全表。
- 当前持仓加"持仓时间"列，面板溢出滑动。
- 时间线/教训库/操作日志全部改分页：5 条/页 + 加载更多按钮，去掉定时刷新。
- 时间线时间加年月日、展开状态保持、关仓 detail 格式优化。
- Agent 触发由 worker 控制：每轮 market 后调 `hermes cron run`，配置 `AGENT_TRIGGER_INTERVAL` 控制频率。

### 数据优化

- 废弃 `round_candidates`，Agent 改读 `token_heat_history`。
- worker 重启自动从 `token_heat_history` 续轮次。
- `auto_trader.py` 过期清理移到决策扫描之前，避免积压的旧决策被错误执行。
- 候选币加 `age` 字段（上币时长）、提取脚本加 BTC 走势 + 恐惧贪婪指数 + 交易时段。

---

## v3.0 — Agent 自主交易 + 全栈重构（2026-05-08）

### Skill 重构

- **agent-trade**：从 700 行拆成 SKILL.md（~110 行）+ 6 个 references + 2 个 scripts + 2 个 assets。去掉硬编码评分规则，Agent 自主决策。定价权移交系统。
- **agent-review**：同上拆成 SKILL.md（~75 行）+ 2 个 references + 2 个 scripts。去掉日期限制，改用 `reviewed=0` 标记。
- **渐进式披露**：SKILL.md 只写原则和流程，细节全在 references/，脚本全在 scripts/，模板全在 assets/。

### IP 封禁加固

- **market.py**：加 rate limiter（4 req/s）、熔断退避（429/403 暂停 30-90s）、指数退避。删现货 API，合约 K 线算 24h 涨跌。
- **scraper.py**：随机滚动距离/间隔/burst-idle 模式、随机鼠标移动、user_data >200MB 自动清。
- **market_realtime.py**：断连不刷屏、ping 从 150s 降到 20s、轻量流订阅开关。

### 数据表优化

- 废弃 `round_candidates`，Agent 改读 `token_heat_history`。
- `journal` 加 `reviewed` 字段，复盘不再依赖日期。
- `pending_decisions` 加 `source_round/social_score/mentions`，去 `raw_response/market_read`。
- `watchlist_followups` 每 50 轮清 3 天前数据。
- worker 重启自动从 `token_heat_history` 续轮次。

### 定价权移交

- Agent 只决定 token + tier，`entry_price/stop_loss/tp` 由 `auto_trader` 用实时价 + ATR 计算。
- 开仓后回填 actual 价格到 `pending_decisions`。
- Web 收藏拆开：★ 只收藏、[开仓] 按钮市价下单、持仓面板 [平仓] 按钮市价平仓。

### 实盘接入（paper/live 双模式）

- 新增 `exchange.py`：合约 API 封装（市价开仓、挂止损止盈、查余额持仓、撤单）。
- `auto_trader.execute_open()`：paper/live 分流，live 下单 + 挂 SL/TP。
- `trade_logic.update_paper_positions()`：live 模式查 exchange 同步持仓，不跑本地止盈止损。
- `config.py` 加 `BINANCE_API_KEY/SECRET`，`TRADING_MODE=paper/live` 切换。

### Web 面板优化

- Agent 面板决策时间线 + 教训库：分页加载（30/20 条/页），"加载更多"按钮。
- 时间线展开保持（刷新不丢失）、关仓 detail 格式化、操作日志悬停完整理由。
- 15 分钟热度榜 + 交易面板：横向滚动防溢出、字体缩小。
- 平仓价改为加权均价（TP1+TP2 多次成交场景）。

### 新字段

- 候选币加 `age`（上币时长，"0d10h" 格式，K 线根数估算）。
- 提取脚本加 BTC 走势 + 恐惧贪婪指数 + 交易时段。
- `dimension_data` 入库自动翻译为中文（`FIELD_CN` 字典）。

### 部署适配

- `manage_processes.py`：Linux 启动 `--no-browser` + 日志写入 `logs/`。
- Chromium 低内存模式（`LOW_MEMORY_MODE`）：加 7 个限内 flag。
- 2GB Debian 部署指南：swap 2GB + vm.swappiness=10。

---

## v2.2 — 入场筛选优化（数据反哺）

基于 30 笔止损数据的标签统计：`buy_pressure_faded=73 / entry_lsr_hot=38 / entry_funding_hot=27`。

- **硬门槛新增**：资金费率 ≥ 0.05%/8h、散户多空比 ≥ 1.7、主动买卖比 ≥ 1.8 全部拒绝开仓。
- **Taker 趋势过滤**：最近 20m taker 趋势衰退 > -5% 不开仓（针对 buy_pressure_faded）。
- **入场回调允许**：15m 允许回调到 -1.5%，不再要求必须 > 0。
- **聪明钱分歧加分**：大户 LSR > 1.5 且散户 LSR < 0.7 升档（skip→half, half→full）。

---

## v2.1 — 调试增强 + 性能优化

- **`TRADING_DEBUG`**：打印每个拒绝点的具体原因（余额不足、板块满、信号不过等）。
- **收藏开仓豁免**：手动收藏豁免持仓上限和板块限制（`MANUAL_BYPASS_*`）。
- **worker 大事务拆分**：从 1 个长事务拆成 5 个独立短事务，web 不再卡 30 秒。
- **清理延迟**：`purge_old` 从每轮改为每 20 轮，`heat_history_purge` 每 100 轮。
- **web.py TTL 缓存**：`/api/leaderboard` 和 `/api/trading` 加 2 秒缓存。

---

## v2.0 — 风控体系重构

- **仓位 sizing**：从"先定保证金"改为"先定风险"（risk_based），风险金额 = equity × 1%，仓位 = 风险 / |entry - stop|。
- **ATR 自适应止损**：用 1h K 线算 ATR，止损 = 1.5 × ATR，夹在 [-1.2%, -5%]。
- **入场分档**：FULL（7/7 信号 + score≥65）、HALF（5/7 + score≥55）、SKIP。
- **追高保护**：4h 涨 > 25% / 24h 涨 > 50% 硬否决。
- **板块集中度**：同板块最多 2 个仓位（Meme/L2/AI/DeFi 等粗分类）。
- **日内熔断**：日亏损 > 5% 停开新仓、日交易 ≤ 15 次、止损冷却 30 分钟。
- **TP/SL 阶梯止盈**：TP1 +1.5R 平 30%、TP2 +3R 平 30%、剩余 40% 跟踪止盈。
- **止损失败归档**：自动打标签（`entry_funding_hot`、`buy_pressure_faded` 等），累计统计。
