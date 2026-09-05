# v5.2 链上监控优化 + 黑名单/移除名单 + Alpha集群查询

## 链上监控扫描器修复 (onchain_scanner.py)

### 观察池数据断层修复
- **Step B2**：新增观察池 token 补扫逻辑，每轮对已在观察池的 token 重新拉 CLI 数据
- **Step C2**：新增池数据刷新逻辑，已入池但本轮未背离的 token 同步更新 pool 表字段
- **效果**：sparkline 不再断层、集群页面数据保持新鲜、rounds_in_pool 正常递增
- **Step A**：过滤已晋级的 actionable token，避免重复扫描

### 集群监控边界
- **Stage 3**：仅对 observation 池做标签分析，actionable 不参与

## 集群监控前端 (web.py)

### K线显示优化
- 卡片 K 线高度 160px → 280px
- TradingView URL 加 `.P` 永续合约后缀（参考 KOL 页面的修复），小币种 K 线正常显示

### 0% 持仓过滤
- 前端 `renderClusters()` 过滤 holding_percent <= 0 的集群
- 标题计数同步为过滤后数量

## Alpha 集群查询 (/alpha-cluster)

### 新功能
- 输入 Token 符号+链 → 实时拉取 OnchainOS 集群数据
- 独立 DB `db/alpha_cluster.db`，与扫描器隔离
- 四类标签完整：api / holding_change / common_holdings / trade_behavior
- 地址列可点击跳转 debot.ai
- 手动刷新，不自动

### 后端
- `okx_source.py`：新增 `search_token_address()` 通过 hot-tokens API 查找合约地址
- `storage.py`：新增 `init_alpha_db()` + `alpha_clear_token()`
- `web.py`：3 个 API（fetch/GET clusters/GET analysis）

## 黑名单与移除名单

### 黑名单 (/onchain)
- 观察池卡片 🚫 按钮 → 移入黑名单 → Token 不再进入观察池
- 黑名单管理面板：查看 + 恢复 → Token 可重新入池
- Scanner Step C + Step B2 双重过滤

### 移除名单 (/onchain)
- 策略页平仓 onchain 持仓 → 自动记录 "closed"
- 可建仓卡片 🗑 按钮 → 手动移除 → 记录 "manual"
- 仅记录查看，不做过滤拦截
- DB: `onchain_removed` 表

## 策略页面优化 (web.py)

### 手动平仓
- 当前持仓表格每行新增"平仓"按钮 → 调用 `POST /api/trade/market-close`
- 平仓后自动清理 onchain 可建仓池

### 最近平仓
- 新增"开仓时间"列

## 存储变更 (storage.py)

- `onchain_blacklist` 表 + 3 CRUD + 迁移
- `onchain_removed` 表 + 2 CRUD + 迁移
- `init_alpha_db()` + `alpha_clear_token()`
