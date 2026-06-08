# v4.9 CHANGELOG — CoinGecko + GMGN 数据集成 + 多项优化

## 一、CoinGecko 数据源（链 + 市值 + FDV + 合约地址）

- `market.py` 新增 `get_coingecko_metrics()`、`_coingecko_search()`、`_coingecko_meta()`
- 批量获取代币的链、合约地址、市值、FDV，内置 1 小时缓存
- 链选择优先级：solana > bsc > base > ethereum，避免桥接链（如 BNB 不再取 Eth WBNB）
- 替代 `extra/token_tags.json` 静态文件作为链来源，替代 OKX 作为市值来源
- worker.py 和 web.py `_refresh_watchlist_tokens` 均集成了 CG 批量调用

## 二、GMGN 数据源（持仓分布 + 链上聪明钱）

- `market.py` 新增 `get_gmgn_holders()` — TOP10 持仓分布
- `market.py` 新增 `get_gmgn_token_info()` — 聪明钱/巨鲸/KOL/狙击手 数量 + 安全指标
- CoinGecko→GMGN chain 映射（`_gmgn_chain`），自动转换链名格式
- rate 字段统一转百分比（`_pct` 函数），修复前端显示和阈值判断
- worker.py 和 web.py 均集成了 GMGN 调用 + 日志（正常/异常/跳过）

## 三、前端展示

- 深度解读卡片新增"持仓分布（前10）"板块：TOP10 明细 + 前10合计
- 深度解读卡片新增"链上聪明钱 (GMGN)"板块：聪明钱/巨鲸/KOL 数量 + 团队持币/前10占比
- 无数据时显示 `-` 而非隐藏面板，与其他字段一致
- `renderKolCandidates` 展示 reasons（OI异动/费率异常/MA20乖离）
- 实时价格：`_build_leaderboard_items` 用 `market_realtime_cache` 覆盖 snap 旧价
- 前端文字："合约扫描"→"做多建议"、"做空候选"→"做空建议"、"KOL关注"→"综合建议"

## 四、KOL Agent 优化

- KOL prompt 新增 20+ 字段：FDV、持仓分布、聪明钱统计、48h 涨跌、OI 48h、费率趋势等
- 新增 `_fmt_holders()`、`_fmt_funding_hist()` 格式化函数
- `kol_is_interesting` 返回 `(bool, reasons_list)`，前端可展示触发原因
- `KOL_CANDIDATES_PER_BATCH` 改为 1（单次分析一个代币）
- NVIDIA_API_KEYS 合并 KOL 策略 5 个 + 快照 4 个 = 9 个 key

## 五、Bug 修复

- `_build_leaderboard_items` 实时价格覆盖（快照 5 分钟 → 实时 1 秒）
- `_refresh_watchlist_tokens` 补上 `oi_marketcap_ratio` 计算
- GMGN 百分比字段源头转百分比，修复前端 `.toFixed()` 和阈值判断
- 前端 `Array.isArray` 守卫 `top10`，防止数据异常时崩溃
- worker.py pkill chromium 移出 except 块，每轮都执行
- web.py 删除 `api_watchlist_refresh` 死代码 ~30 行
