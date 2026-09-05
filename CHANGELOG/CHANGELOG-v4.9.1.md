# CHANGELOG v4.9.1 — CG/GMGN 全链路重构 + 设置页面分类整理

## CG 搜索优化 (market.py)

### Bug 修复
- **精确 symbol 匹配，不取兜底**：`_coingecko_search` 删除 `coins[0]` 兜底逻辑，无精确匹配直接返回 None。避免 "PUMP" 匹配到 "PumpFun"、"D" 匹配到 USDT 等错误关联
- **失败不缓存**：搜索失败/元数据失败不再缓存 None 整整 1 小时，下轮自动重试。修复因 CG 限流导致代币被黑 1 小时的问题
- **URL 编码**：`quote(token)` 修复中文代币名（如"币安人生"）URL 非法
- **原生币识别**：`detail_platforms` 中 `""` key 表示原生链（无合约地址），直接跳过，避免 BNB 误取 Ethereum WBNB 的持仓数据
- **asset_platform_id 优先**：代币原生属链替代固定优先级猜测。仅当 CG 明确标注原生链且在 GMGN 支持列表中才取合约地址
- **市值与链信息解耦**：`return result if "chain" in result else None` → `return result if result else None`，原生币仍能获取市值/FDV

## GMGN 链映射修复 (market.py)

- **_CG_TO_GMGN_CHAIN** 从 9→4 条，删除 arbitrum-one/polygon-pos/avalanche/optimistic-ethereum/sui 五个 GMGN CLI 不支持的映射
- **新增 `_GMGN_SUPPORTED_CHAINS`** 白名单：`{sol, bsc, base, eth, monad}`
- **_gmgn_chain()** 返回类型改为 `str | None`，不支持链提前返回 None
- **get_gmgn_holders / get_gmgn_token_info** 开头加 `if chain is None: return None`，无效链不启动 CLI 进程

## 设置页面重构 (web.py)

- 6 panel → 3 panel：**系统参数** / **策略配置** / **系统操作**
- 每策略参数聚合在一个 block 内（开关 + 初始金额 + 触发间隔 + 专有参数）
- 零 DOM ID 变更，零 JS 改动

## KOL Agent 改进 (kol_agent.py)

- LLM 持仓展示从 TOP5 改为 TOP10（`_fmt_holders` 删除 `[:5]` 切片）

## 影响范围
- 修改文件：web.py, market.py, kol_agent.py
- 部署：git pull → restart worker.py + web.py
