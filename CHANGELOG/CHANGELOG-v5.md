# v5.0 CHANGELOG — OKX 数据源全链路 + 系统设置动态切换

## OKX 数据源（新增）

**okx_source.py（新文件，~230行）**
- `fetch_top_swap_tokens(50)`：okx market filter → USDT 永续成交量 Top50，过滤非加密资产
- `fetch_sentiment_batch(tokens)`：onchainos sentiment-symbol 批量获取社交情绪（20个/批）
- `build_heat_scores(vol, sentiment)`：成交量排名×0.4 + 社交情绪×0.6 → 热度榜
- 输出格式与 `compute_short_scores` 完全一致，下游无感

**analyzer.py**
- `get_current_heat_scores(conn)`：统一入口，读取 `worker_status.data_source` 决定路由
- `_load_scores_from_heat_history(conn)`：OKX 模式读 `token_heat_history` 最新轮
- OKX=false 时直接调用原 `compute_short_scores`，币安路径零污染

**worker.py**
- `_one_round_okx()`：OKX 模式专用轮次（Pipeline → 读榜 → 快照 → Agent → 收尾）
- `_OKX_MODE` 模块变量 + `worker_status.data_source`：启动时缓存，切换需重启
- `main()` 启动时写 data_source 到 worker_status，全系统（worker/web/analyzer）一致

## 设置页数据源切换

**web.py**
- 新增数据源面板（币安广场 / OKX 下拉选择）
- 保存后检测变化 → 自动弹窗 "是否立即重启？"
- 重启消息更新（3min → 11min）

**storage.py**
- `trading_settings` 加入 `okx_data_source_enabled`（defaults/get/update）
- `worker_status` 新增 `data_source`、`okx_tokens_count`、`okx_last_round` 列

## Bug 修复

- **dashboard.py**：`_okx()` pop 环境变量 OKX_API_KEY，防止误读 OnchainOS key 导致 401
- **analyzer.py**：`get_current_heat_scores` SQL 值加 `str()` 包裹，防止整数类型 `.lower()` 崩溃

## 优化

- **manage_processes.py**：重启顺序改为 等10min → 清僵尸 → 等1min → 启动
- **scraper.py**：`context.close()` 去掉 10s asyncio 超时保护
- **worker.py**：轮间休息从 45-75s 改为 30-60s
- **config.py**：PAGE_GOTO_TIMEOUT 从 30s 改回 60s
