# v5.1 链上监控全链路 + 挂单成交修复 + DB独立化

**日期**: 2026-06-17

## 新增功能

### 链上监控系统 (On-Chain Monitor)
- **onchain_scanner.py**: 后台扫描器，Stage 1 吸筹检测 + Stage 2 启动准备
  - Step A: 从 OnchainOS hot-tokens 拉取横盘候选 (-5%~+5%, mc≥$500K)
  - Step B: 逐币 cluster-overview + cluster-list 聚类分析，检测资金同源 vs 表层持仓背离
  - Step C: 背离代币入观察池，保留已有 stage 不降级
  - Step D: 超期清理 (默认7天未晋级自动移出)
  - Step E: 每2轮 Stage 2 检查 (LP储备加速减少 + Gas seeding + CEX staging)
  - 置信度≥50% 晋级 actionable，自动市价开仓
- **/onchain 页面**: 扫描状态面板 + 观察池/可建仓卡片 (TradingView K线 + sparkline趋势)
- **tg_notify.py**: 每轮扫描 TG 推送，中文人类可读标签替代技术缩写
- **独立 DB**: onchain 4表迁移至 `db/onchain.db`，与主库隔离

### 挂单成交修复
- entry_price 改用 `actual_entry = min(limit, price)` (LONG) / `max(limit, price)` (SHORT)
- TP/SL/risk_unit 全部从 `actual_entry` 推导，不再用 `limit_price`
- journal 入场价、advice 提示同步使用 actual_entry
- 挂单在"错误侧"时 (做多限价>市价 / 做空限价<市价) 自动以更优市价成交

## Bug 修复 (12项)
1. `_chain` 赋值移到使用之前 (UnboundLocalError)
2. onchain 策略缺失重置入口 (API白名单+前端下拉框)
3. `_load_setting` 连错库 → 设置页阈值不生效
4. `_open_onchain_position` 仓位错写进 onchain.db
5. Python `\'` 转义吞反斜杠 → JS 语法错误
6. renderPool `+=` 拼接触发 V8 ConsString 异常 → 改用 Array push+join
7. TG 通知字段名技术缩写 → 中文标签+链名+市值
8. `days_in_pool` 计算不一致 → API 实时计算覆盖
9. 重置策略下拉框缺少 onchain 选项
10. `entry_price` 只用 `limit_price` 忽略更优市价
11. TP/SL 基于 `limit_price` 而非实际入场价 → 开仓即爆
12. journal 入场价记录为 `limit_price` 而非实际成交价

## 文件变更
| 文件 | 变更 |
|------|------|
| onchain_scanner.py | **新建** (~470行) |
| tg_notify.py | **新建** (~80行) |
| okx_source.py | +172行 OnchainOS CLI wrappers |
| storage.py | +297行 onchain 4表DDL + 12个CRUD + init_onchain_db |
| web.py | +599行 ONCHAIN_HTML + 4 API + 设置面板 |
| worker.py | +18行 init_onchain_db + scanner 线程 |
| trade_logic.py | 挂单成交 entry_price/TP/SL/journal 全链路修复 |
