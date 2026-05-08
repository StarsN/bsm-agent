# CHANGELOG v3.0

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
