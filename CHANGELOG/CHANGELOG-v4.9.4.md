# v4.9.4 CHANGELOG

## 做空执行 + agent_short 全链路 + CG/Ave 双路兜底 + GMGN 持仓增强

### auto_trader.py
- `execute_open` 支持 `side="SHORT"` 参数：入场价滑点反向计算、止损在入场价上方、止盈用 `risk_per_unit` 反向
- `one_scan` 中 `open_short` action 调用 `execute_open(..., side="SHORT")`，不再拒绝
- `source_map` 新增 `agent_candidates_short` → `agent_short`
- 持仓记录：做空方向存 `lowest_price`（替代 `highest_price`）

### storage.py
- 新增 `agent_short_candidates` 表 DDL（`agent_short.db`）
- 新增 `agent_short_candidates_insert_batch/get_latest/purge_old` CRUD
- `init_agent_dbs` 注册 `agent_short.db`
- `trading_settings_defaults/get/update` 新增 `agent_short_enabled`、`strategy_initial_agent_short`、`agent_short_interval_minutes`
- `trade_reset_all` / `trade_reset_strategy` 覆盖 `agent_short` 策略

### market.py
- 快照新增 `basis_pct`：标记价 vs 现货指数偏离（基差），用于 KOL 策略全景字段
- CG 搜索/元数据：移除 3 次重试循环，超时直接 `return None`，减少阻塞
- 新增 `_ave_token_info()`：Ave.ai 代币搜索 → `{chain, contract_address, market_cap_usd, fdv_usd}`，CG 失败时兜底
- `get_coingecko_metrics`：CG 失败不缓存，Ave 兜底结果也不缓存（允许下轮重试）
- GMGN holders 缓存：`_gmgn_cache` 30 分钟 TTL，减少 CLI 调用
- GMGN holders 字段增强：`cost/profit/realized/unrealized/bought/sold/netflow` + `top10_avg_cost/top10_total_profit/top10_in_profit`
- 新增 `get_ave_holders()`：Ave.ai Top100 持仓，GMGN 失败时兜底，同格式返回

### agent-review SKILL
- 入场动能分析新增 OI 绝对水平风险曲线（第6类模式）
- 新增交叉矩阵分析：OI × Taker趋势 四象限定位系统性危险区
- Taker 伪强势陷阱：Taker趋势不降≠安全，需交叉 OI Delta 综合判断
- 跨币种归纳新增 OI 积累水平共性分析

### agent-trade SKILL
- DB 锁冲突处理：遇到 `database is locked` 时递增间隔重试（10s→20s→30s）

### CLAUDE.md
- 页面列表从 3 个更新为 ~10 个（agent-heat、agent-kol、dashboard 等）
