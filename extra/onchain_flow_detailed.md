# 链上监控 & 集群监控 — 全流程详解

> 生成时间: 2026-06-18  
> 涉及文件: `onchain_scanner.py`, `okx_source.py`, `storage.py`, `web.py`, `worker.py`, `trade_logic.py`, `tg_notify.py`, `config.py`

---

## 一、系统总览

```
┌─────────────────────────────────────────────────────────────────────────┐
│                          worker.py 启动                                  │
│                              │                                           │
│                    storage.init_onchain_db()                             │
│                    (创建 onchain.db + 9 张表)                             │
│                              │                                           │
│                    scanner_loop()  ← daemon 线程                         │
│                              │                                           │
│              每 N 小时一轮 (onchain_scan_interval_hours)                  │
└─────────────────────────────────────────────────────────────────────────┘
```

```
┌──────────────────────────────────────────────────────────────────────────┐
│                        数据源：OnchainOS CLI                              │
│                                                                          │
│  onchainos token hot-tokens        → 热门代币列表 (Step A)               │
│  onchainos token cluster-overview  → 聚类概览（含同源资金/新地址%）(Step B)│
│  onchainos token cluster-list      → 每个集群的地址明细 (Step B)          │
│  onchainos token liquidity         → LP 流动性储备 (Stage 2)             │
│  onchainos workflow wallet-analysis → 单个地址持仓+交易行为 (Stage 3)     │
│  onchainos token trades            → 集群地址的批量交易记录 (Stage 3)     │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 二、程序启动流程

```
worker.py main()
  │
  ├─ storage.init_db()              → 主 DB (binance_square.db)
  ├─ storage.init_onchain_db()      → 链上 DB (onchain.db)
  │     │
  │     ├─ 创建 9 张表:
  │     │   onchain_scanner_status   扫描器心跳
  │     │   onchain_snapshots        每轮快照（背离检测输入）
  │     │   onchain_lp_snapshots     LP 储备时序
  │     │   onchain_pool             观察池/建仓池（每个 token 一行）
  │     │   onchain_clusters         每轮集群详情（地址 JSON）
  │     │   onchain_address_portfolio  (legacy, 已不再写入)
  │     │   onchain_wallet_analysis  钱包分析提取字段
  │     │   onchain_cluster_trades   集群交易记录
  │     │   onchain_cluster_tags     集群标签
  │     │
  │     └─ _onchain_migrate()       ALTER TABLE 补历史列
  │
  ├─ 启动 scanner 线程 (daemon)
  │     │
  │     └─ scanner_loop()
  │           │
  │           ├─ 恢复上次 round_number (from onchain_scanner_status)
  │           ├─ while True:
  │           │     │
  │           │     ├─ 检查 ONCHAIN_SCAN_ENABLED
  │           │     ├─ run_round()            ← 核心
  │           │     ├─ 从 DB 重读 scan_interval (支持设置页动态调)
  │           │     └─ sleep(N 小时)
  │           │
  │           └─ 异常 → _write_status("error") → sleep 5min 重试
  │
  └─ 启动其他线程 (web server, collector, etc.)
```

---

## 三、Stage 1 — 发现流水线 (Step A~D)

每轮都执行，核心目标：从热门代币中找出"吸筹背离"信号。

```
┌─────────────────────────────────────────────────────────────────────┐
│                      run_round() 入口                                │
│                                                                     │
│  从 DB 读取动态配置:                                                  │
│    _load_setting("onchain_divergence_fund_source_min", default=30)   │
│    _load_setting("onchain_surface_top10_max", default=40)            │
│    _load_setting("onchain_observation_max_days", default=7)          │
│    _load_setting("onchain_scan_interval_hours", default=4)           │
└─────────────────────────────────────────────────────────────────────┘
```

### Step A: 候选筛选

```
fetch_hot_tokens(chain, mc_min=500k, limit=50, price_min=-5, price_max=5)
  │
  └─ onchainos token hot-tokens
       --rank-by 5         ← 综合热度排名
       --time-frame 4      ← 4h 时间窗
       --limit 50
       --market-cap-min 500000
       --price-change-min -5  ← 横盘过滤下限
       --price-change-max 5   ← 横盘过滤上限
       --risk-filter true
       [--chain bsc]

  输出: [{token, token_address, chain, top10_hold_pct, holders_count, market_cap_usd}, ...]

  过滤:
    ├─ 无 token_address → 跳过
    ├─ has_perpetual(token) == False → 跳过 (只保留有币安合约的)
    └─ 最终: ~15-30 个候选进入 Step B
```

### Step B: 逐币聚类分析

```
for each candidate (串行, 每个 sleep 0.6s):
  │
  ├─ ① fetch_cluster_overview(addr, chain)
  │     └─ onchainos token cluster-overview --address X --chain bsc
  │     返回: {
  │       cluster_concentration,     ← 集群集中度描述
  │       top100_holdings_pct,       ← top100 持仓占比%
  │       holder_same_fund_pct,      ← 同资金源占比% ★ 背离关键
  │       holder_new_addr_pct,       ← 新地址占比%
  │       holder_same_creation_pct,  ← 同时创建%
  │       rug_pull_pct,              ← rug 风险%
  │     }
  │
  ├─ ② fetch_cluster_list(addr, chain) → (parsed_list, raw_json)
  │     └─ onchainos token cluster-list --address X --chain bsc
  │     返回 tuple:
  │       parsed_list = [{              ← 用于计算有效集中度
  │         holding_percent,            ← 集群持有占比 (0-1)
  │         trend_type,                 ← ["accumulating"|"distributing"]
  │         pnl_percent,                ← 集群盈亏%
  │         addresses: [{
  │           address,
  │           holding_percent,
  │           is_exchange,              ← 交易所地址标记
  │           is_contract,              ← 合约地址标记
  │           is_kol,                   ← KOL 地址标记
  │         }]
  │       }]
  │       raw_json = 原始 JSON 字符串    ← 直接存 DB, 不丢字段
  │
  ├─ ③ 计算 effective_concentration (有效集中度):
  │     for each cluster:
  │       if 集群中没有 is_exchange 且 没有 is_contract:
  │         effective_conc += cluster.holding_percent
  │     effective_conc *= 100    ← 转为百分比
  │     含义: 去除了交易所/合约地址后的"真人集群"持仓之和
  │
  ├─ ④ 背离检测:
  │     if holder_same_fund_pct > fund_source_min (默认 30%)
  │        AND top10_hold_pct < surface_top10_max (默认 40%):
  │       → divergence_detected = True   ← 进入观察池
  │       → divergence_score = (fund - 30) * (40 - top10) / 100
  │     else:
  │       → divergence_detected = False
  │
  ├─ ⑤ 组装 snapshot dict (写入 onchain_snapshots)
  │
  └─ ⑥ 如果 onchain_cluster_enabled:
         for each cluster:
           组装 cluster_record dict (写入 onchain_clusters)
           is_real_cluster = 0 (交易所/合约) / 1 (真人集群)
```

### Step C: 观察池更新

```
for each divergence_tokens (背离检测通过的):
  │
  ├─ 查 onchain_pool 是否已存在
  │   ├─ 已存在 → rounds_in_pool += 1, 保留原 pool_stage (不降级)
  │   └─ 新入池 → pool_stage = "observation", first_seen_at = now
  │
  └─ onchain_pool_upsert() → 写入/更新 onchain_pool
```

### Step D: 超期清理

```
for each pool token with pool_stage == "observation":
  if first_seen_at < (now - max_days):
    onchain_pool_remove()  ← 移除超期未晋级的 token
```

### 每轮收尾: DB 清理

```
写入 onchain_snapshots (批量 insert)
写入 onchain_clusters (批量 insert)

清理旧数据:
  onchain_snapshots  → 保留最近 50 轮
  onchain_clusters   → 保留 onchain_cluster_keep_rounds (默认 1500) 轮
  onchain_address_portfolio → 同上
  onchain_wallet_analysis    → 同上
  onchain_cluster_trades     → 同上
  onchain_cluster_tags       → 同上
```

---

## 四、Stage 2 — 晋级流水线 (Step E)

每 `ONCHAIN_STAGE2_CHECK_INTERVAL` 轮执行一次 (默认 1，即每轮)。  
对观察池内每个 token 进行 3 项检查 → 综合置信度 ≥ 50% → 晋级 `actionable`。

```
if round_number % stage2_interval == 0:
  for each pool token with pool_stage == "observation":
    │
    ├─ ❶ LP 储备检查 (_check_lp_reserve)
    │   │
    │   ├─ fetch_liquidity(addr, chain)
    │   │     └─ onchainos token liquidity --address X --chain bsc
    │   │
    │   ├─ 写入 onchain_lp_snapshots
    │   │
    │   ├─ 读最近 4 轮 LP 快照 (含本轮 = 新 4 轮)
    │   │
    │   ├─ 取最新 3 个有效值，计算两轮变化率:
    │   │     changes[i] = (amount[i+1] - amount[i]) / amount[i+1] * 100
    │   │     (LP 减少时 changes > 0, 增加时 < 0)
    │   │
    │   └─ 判定:
    │         latest_change > 0 AND prev_change > 0:
    │           latest > prev*1.5 AND latest > 3% → "↓↓ 加速减少" score=80
    │           latest > prev*1.2 AND latest > 2% → "↓ 加速减少(弱)" score=50
    │           latest > 0                        → "↓ 轻微减少" score=25
    │         latest > 0 AND prev <= 0            → "↓ 开始减少" score=20
    │         latest <= 0 AND prev > 0            → "增加（反向）" score=0
    │         else                                → "稳定" score=10
    │
    ├─ ❷ Gas Seeding 检查 (_check_gas_seeding)
    │   │
    │   ├─ fetch_cluster_overview(addr, chain)
    │   │
    │   └─ 4 项评分 (每满足 1 项 +1):
    │         new_addr_pct > 2%              → +1
    │         fund_same_pct > 35%            → +1
    │         same_creation_pct > 5%         → +1
    │         new_addr_pct > prev_new + 1%   → +1 (在上升)
    │         ─────────────────────────────────────
    │         score >= 3 → signal=2 (强)
    │         score >= 2 → signal=1 (弱)
    │         else       → signal=0
    │
    ├─ ❸ CEX Staging 检查 (_check_cex_staging)
    │   │
    │   ├─ fetch_cluster_list(addr, chain)
    │   │
    │   └─ 统计交易所集群:
    │         交易所集群数 >= 3 AND 总占比 > 5% → signal=2 (强)
    │         交易所集群数 >= 1 AND 总占比 > 2% → signal=1 (弱)
    │         else                             → signal=0
    │
    └─ ❹ 综合置信度 (_compute_confidence)
        │
        │  权重分配:
        │    LP 储备:  50%
        │    Gas 播种: 35%
        │    CEX 上架: 15%  (方向不可判, 降权)
        │
        │  公式: lp_score*0.50 + gas_signal*17.5 + cex_signal*7.5
        │
        └─ confidence >= 50:
              ├─ onchain_pool_upsert(pool_stage="actionable", ...)
              │
              └─ 自动开仓 (如果 onchain_strategy_enabled):
                    _open_onchain_position(token, pool_entry)
                      │
                      ├─ get_market_snapshot(token) ← 取实时价格
                      ├─ 构建 candidate dict: {token, passed=True, tier="half",
                      │    direction="long", price=mark_price}
                      │
                      └─ trade_logic.open_paper_position(main_conn, candidate,
                             settings, strategy="onchain", side="LONG")
                           │
                           ├─ evaluate_candidate()  ← 风控校验
                           ├─ evaluate_entry_quality() ← 7 条件 + hard blocks
                           ├─ 计算仓位大小
                           ├─ 设置 TP/SL/移动止盈
                           └─ 写入 positions 表 → 出现在交易面板
```

---

## 五、Stage 3 — 集群标签分析 (Step F)

每 `onchain_cluster_analysis_interval` 轮执行一次 (默认 1，即每轮)。  
对观察池+建仓池内每个 token，分析其集群标签。

```
if onchain_cluster_enabled AND onchain_cluster_analysis_enabled:
  if round_number % analysis_interval == 0:
    _analyze_clusters(round_number)
```

### 详细流程

```
_analyze_clusters(round_number):
  │
  ├─ 加载 pool 中所有 token (observation + actionable)
  │
  └─ for each pool token (串行):
        │
        ├─ ① 加载集群数据
        │     clusters_now  = onchain_clusters_for_token(token, limit_rounds=1)
        │     clusters_prev = onchain_clusters_for_token(token, limit_rounds=2)
        │     (仅取非当前轮的 → prev_map[cluster_index] = prev_record)
        │
        └─ for each cluster in clusters_now:
              │
              ├─ 过滤: 跳过 !is_real_cluster (交易所/合约)
              ├─ 过滤: 跳过 holding_percent < min_holding_pct (默认 1%)
              │
              ├─ ② 持仓变化标签 (holding_change)
              │     对比上轮同 index 集群的 holding_percent:
              │       change > +0.5% → "增持 +X%"
              │       change < -0.5% → "减持 X%"
              │       |change| ≤ 0.5% → "持仓稳定"
              │
              ├─ ③ API 标签 (trend_type / pnl)
              │     "accumulating" in trend_type → "吸筹中"
              │     "distributing" in trend_type → "派发中"
              │     pnl_percent 有值 → "集群盈利 +X%" / "集群亏损 X%"
              │
              ├─ ④ 钱包分析 (wallet-analysis)
              │     取前 top_n 个真人地址 (默认 5):
              │     for each address:
              │       fetch_wallet_analysis(address, chain)
              │         └─ onchainos workflow wallet-analysis --address X --chain bsc
              │       │
              │       ├─ 存储到 onchain_wallet_analysis:
              │       │   {round_number, token, cluster_index, address,
              │       │    total_value_usd, buy_count_30d, sell_count_30d,
              │       │    buy_volume_30d, sell_volume_30d, avg_buy_value,
              │       │    avg_sell_value, realized_pnl, win_rate,
              │       │    last_trade_time, analysis_json}
              │       │
              │       └─ 提取持仓: balances[0].tokenAssets[].symbol → addr_holdings[addr]
              │
              ├─ ⑤ 交叉持仓标签 (common_holdings)
              │     if len(addr_holdings) >= 2:
              │       common = ∩(all addr_holdings) - {本币}
              │       if len(common) >= 3:
              │         → "共持 N 个币" (列出前 8 个)
              │
              ├─ ⑥ 交易行为标签 (trade_behavior)
              │     fetch_token_trades(addr, chain, wallet_filter=[cluster地址列表])
              │       └─ onchainos token trades --address X --chain bsc --wallet-filter A,B,C
              │     │
              │     ├─ 存储到 onchain_cluster_trades:
              │     │   {round_number, token, cluster_index,
              │     │    addresses_batch, trades_json}
              │     │
              │     └─ 分析:
              │         sells = tradeType=="2" 的记录
              │         buys  = tradeType=="1" 的记录
              │         sells=0      → "近期无卖出"
              │         sells <= 2   → "少量卖出 (N笔)"
              │         sells > 2    → "活跃卖出 (N笔)"
              │         buys > 0     → "近期买入 (N笔)"
              │
              └─ sleep 0.2s (between clusters)
```

### 标签类型汇总

| tag_type | 来源 | 示例值 |
|----------|------|--------|
| `holding_change` | DB 对比上轮 | 增持 +2.5% / 减持 -1.3% / 持仓稳定 |
| `api` | cluster-list API | 吸筹中 / 派发中 / 集群盈利 +15.2% |
| `common_holdings` | wallet-analysis 交叉 | 共持 5 个币 |
| `trade_behavior` | token trades API | 近期无卖出 / 活跃卖出 (5笔) / 近期买入 (3笔) |

---

## 六、数据库架构

### 6.1 存储架构

```
┌─────────────────────────┐     ┌──────────────────────────┐
│   binance_square.db     │     │      onchain.db          │
│   (主 DB, SQLite WAL)   │     │   (链上独立 DB, WAL)      │
│                         │     │                          │
│  trading_settings       │←────│ _load_setting() 读配置    │
│  positions              │     │                          │
│  token_heat_history     │     │ onchain_scanner_status   │
│  market_snapshots       │     │ onchain_snapshots        │
│  ...                    │     │ onchain_lp_snapshots     │
│                         │     │ onchain_pool             │
│  开仓/风控/持仓 写入 →   │     │ onchain_clusters         │
│                         │     │ onchain_wallet_analysis  │
│                         │     │ onchain_cluster_trades   │
│                         │     │ onchain_cluster_tags     │
│                         │     │ onchain_address_portfolio│
└─────────────────────────┘     └──────────────────────────┘
```

### 6.2 各表详情

#### onchain_scanner_status (1 行, key="scanner")
```
key, stage, detail, round_number, tokens_scanned, candidates_found,
pool_size, actionable_size, last_scan_start, last_scan_end, last_heartbeat
```
用途: 前端 `/onchain` 状态面板 + 轮次恢复

#### onchain_snapshots (每轮每币 1 行)
```
id, round_number, token, token_address, chain,
hot_rank, top10_hold_pct, holders_count, market_cap_usd,
cluster_concentration, top100_holdings_pct,
holder_same_fund_pct, holder_new_addr_pct,
holder_same_creation_pct, rug_pull_pct,
effective_concentration, divergence_score,
cluster_raw_json (原始 cluster-list JSON),
snapshot_at
```
索引: (token, round_number), (round_number)  
保留: 最近 50 轮

#### onchain_pool (每 token 1 行)
```
token (PK), token_address, chain,
pool_stage (observation|actionable),
surface_top10_pct, effective_concentration, fund_source_same_pct,
divergence_detected, days_in_pool, rounds_in_pool,
lp_reserve_trend, lp_trend_score,
gas_seeding_signal, cex_staging_signal,
confidence_score, prev_new_addr_pct,
phases_seen, first_seen_at, last_updated_at
```

#### onchain_clusters (每轮每集群 1 行)
```
id, round_number, token, token_address, chain,
cluster_index, holding_percent, trend_type, pnl_percent,
address_count, is_real_cluster, addresses_json, snapshot_at
```
索引: (token, round_number), (round_number)  
保留: onchain_cluster_keep_rounds (默认 1500)

#### onchain_wallet_analysis (每地址每轮 1 行)
```
id, round_number, token, cluster_index, address,
total_value_usd, buy_count_30d, sell_count_30d,
buy_volume_30d, sell_volume_30d, avg_buy_value, avg_sell_value,
realized_pnl, win_rate, last_trade_time, analysis_json
```

#### onchain_cluster_trades (每集群每轮 1 行)
```
id, round_number, token, cluster_index,
addresses_batch (逗号分隔), trades_json
```

#### onchain_cluster_tags (每集群每轮 N 行)
```
id, round_number, token, cluster_index,
tag_type, tag_label, tag_detail
```

#### onchain_lp_snapshots (流动性时序)
```
id, token, token_address, chain, round_number, token_amount, snapshot_at
```

---

## 七、前端数据流

### 7.1 /onchain 页面 (链上监控总览)

```
浏览器加载 /onchain
  │
  ├─ JS: loadOnchain() 每 60s 自动刷新
  │     │
  │     ├─ GET /api/onchain/status
  │     │     → onchain_status_get("scanner")
  │     │     → 返回: {stage, detail, round_number, tokens_scanned,
  │     │              candidates_found, pool_size, actionable_size,
  │     │              last_scan_start, last_scan_end}
  │     │     → 渲染 Panel 1: 扫描状态 + 下次倒计时
  │     │
  │     ├─ GET /api/onchain/pool?stage=observation
  │     │     → onchain_pool_get_all("observation")
  │     │     → 每个 token 附带最近 6 轮 snapshots 用于 sparkline
  │     │     → 渲染 Panel 2: 观察池表格
  │     │
  │     └─ GET /api/onchain/pool?stage=actionable
  │           → onchain_pool_get_all("actionable")
  │           → 渲染 Panel 3: 可建仓表格
  │
  └─ 页面结构:
      ┌────────────────────────────────────────┐
      │ Panel 1: 扫描器状态 (指示灯+统计+倒计时)  │
      ├────────────────────────────────────────┤
      │ Panel 2: 观察池 (Stage 1)              │
      │  列: 代币 | 链 | 表层Top10% | 有效集中度% │
      │      资金同源% | 趋同 | 入池轮数 | 趋势    │
      ├────────────────────────────────────────┤
      │ Panel 3: 可建仓 (Stage 2)              │
      │  列: 代币 | 链 | LP储备趋势 | Gas播种   │
      │      CEX上架 | 置信度% | 操作            │
      └────────────────────────────────────────┘
```

### 7.2 /onchain/token/{token} 页面 (集群详情)

```
浏览器加载 /onchain/token/{token}
  │
  ├─ JS: loadTokenClusters() 每 60s 自动刷新
  │     │
  │     ├─ GET /api/onchain/clusters?token=X
  │     │     → onchain_clusters_for_token(token, limit_rounds=1)
  │     │     → onchain_cluster_tags_for_token(token, limit_rounds=1)
  │     │     → 合并 clusters + tags, 附加地址明细 JSON
  │     │     → 渲染集群卡片列表
  │     │
  │     ├─ GET /api/onchain/pool
  │     │     → 找当前 token 的 pool 记录
  │     │     → 渲染 token 概览面板 (7 指标)
  │     │
  │     └─ GET /api/onchain/cluster/analysis?token=X
  │           → onchain_wallet_analysis_for_token(token)
  │           → 渲染地址详情表格 (展开集群卡片后显示)
  │
  │
  └─ 页面结构:
      ┌──────────────────────────────────────────┐
      │ Token 概览面板 (7 个指标卡片)              │
      │  表层Top10% | 有效集中度 | 资金同源% ...    │
      ├──────────────────────────────────────────┤
      │ 集群 #1 [真人集群] ▸ (可展开)              │
      │   ├─ 标签: 吸筹中 | 增持+2.1% | 近期无卖出  │
      │   ├─ 持仓: 5.23%  |  地址数: 8            │
      │   └─ 展开后: 地址明细表 (8 列)             │
      │      地址 | 排名 | 持仓% | 总资产 | 买入均价│
      │      卖出均价 | 最后交易 | 标签              │
      ├──────────────────────────────────────────┤
      │ 集群 #2 [交易所] (折叠, 半透明)             │
      │ 集群 #3 [真人集群] ▸                       │
      │ ...                                       │
      └──────────────────────────────────────────┘
```

### 7.3 设置页面 (/settings)

```
设置页 → 链上监控 + 集群监控 panel
  │
  ├─ 链上监控:
  │   trade-onchain-strategy-switch   策略开关 (enable/disable)
  │   trade-strategy-onchain          初始资金
  │   trade-onchain-interval          扫描间隔 (0.5-24h)
  │   trade-onchain-fund-min          资金同源阈值 (10-80%)
  │   trade-onchain-top10-max         表面集中度上限 (10-80%)
  │   trade-onchain-max-days          观察池最大天数 (3-30)
  │
  ├─ 集群监控:
  │   trade-cluster-enabled           集群监控总开关 (enable/disable)
  │   trade-cluster-analysis-switch   标签分析开关 (enable/disable)
  │   trade-cluster-interval          分析间隔 轮 (1-48)
  │   trade-cluster-top-addresses     每集群分析地址数 (1-20)
  │   trade-cluster-min-holding       最小持仓%过滤 (0.1-10)
  │   trade-cluster-keep-rounds       数据保留轮数 (50-5000)
  │
  └─ JS save: POST /api/settings
       → trading_settings_update()  写入 trading_settings 表
       → scanner 下轮 _load_setting() 读取新值
```

---

## 八、Telegram 通知流

```
run_round() 结尾:
  │
  if TG_ONCHAIN_ENABLED:
    │
    └─ send_onchain_round(round_number, snapshot_count,
         divergence_tokens, obs_count, act_count, promoted_names)
         │
         └─ 拼装中文消息:
             📊 链上扫描 第N轮
             扫描 X 个代币 | Y 个背离 | 观察池 Z | 可建仓 W
             ─────────────────────
             新背离:
             🔹 BTC (BSC) | MC $1.2M | 隐藏关联资金 45% | 表层持仓 25% | 暗仓 18%
             ⚡ 晋级 actionable: TOKEN1, TOKEN2
```

---

## 九、动态配置流

```
设置页面保存
  │
  POST /api/settings  {onchain_cluster_enabled: true, ...}
  │
  ├─ Pydantic 校验 (TradingSettingsBody)
  ├─ API key whitelist 检查 (web.py 元组)
  └─ storage.trading_settings_update(conn, settings)
       │
       ├─ allowed set 白名单检查 ★ (6 个 cluster key 必须在此)
       ├─ 写入 trading_settings 表 (key-value)
       └─ commit

下次 scanner 调用 _load_setting("onchain_cluster_enabled", default):
  │
  ├─ 打开 binance_square.db (注意: 不是 onchain.db)
  ├─ trading_settings_get(conn) → 返回全量 key-value dict
  ├─ dict.get("onchain_cluster_enabled") → 找到 → 返回
  └─ 找不到 → 返回 config.py 默认值
```

**涉及的 5 处白名单** (必须全部包含新增 key):

| 位置 | 用途 | 漏 = 后果 |
|------|------|-----------|
| `web.py` API key 元组 | API 接收 | API 拒绝该字段 |
| `web.py` Pydantic model | 类型校验 | 400 错误 |
| `storage.py` defaults | 默认值提供 | 返回 None |
| `storage.py` boolean/numeric whitelist | 读取类型转换 | 类型错误 |
| `storage.py` `allowed` set | **写入** | 静默丢弃, 设置不生效 ★ |

---

## 十、完整数据流图 (端到端)

```
                        OnchainOS CLI
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
   hot-tokens      cluster-overview        cluster-list
        │           cluster-list           wallet-analysis
        │           liquidity              token-trades
        │                    │                    │
        ▼                    ▼                    ▼
   ┌──────────────────────────────────────────────────┐
   │              okx_source.py                       │
   │  fetch_hot_tokens()           → list[dict]       │
   │  fetch_cluster_overview()     → dict|None        │
   │  fetch_cluster_list()         → (list, raw_json) │
   │  fetch_liquidity()            → dict|None        │
   │  fetch_wallet_analysis()      → dict|None        │
   │  fetch_token_trades()         → list[dict]|None  │
   │  (CLI 调用层: subprocess + JSON parse)           │
   └──────────────┬───────────────────────────────────┘
                  │
                  ▼
   ┌──────────────────────────────────────────────────┐
   │           onchain_scanner.py                     │
   │                                                  │
   │  scanner_loop()  ← daemon 线程                   │
   │    │                                             │
   │    run_round():                                  │
   │    │                                             │
   │    ├─ Step A: hot-tokens → 候选过滤              │
   │    │     filter: has_perpetual + addrs 非空       │
   │    │                                             │
   │    ├─ Step B: 每币串行                            │
   │    │     overview + cluster-list                 │
   │    │     → effective_concentration               │
   │    │     → divergence_detected                   │
   │    │     → snapshots[] + cluster_records[]        │
   │    │                                             │
   │    ├─ Step C: 观察池 upsert                       │
   │    │     divergence → onchain_pool               │
   │    │                                             │
   │    ├─ Step D: 超期清理                            │
   │    │     first_seen > max_days → remove          │
   │    │                                             │
   │    ├─ Step E: Stage 2 (每 interval 轮)            │
   │    │     3 检查 → 置信度 → actionable             │
   │    │                │                            │
   │    │                └─ _open_onchain_position()   │
   │    │                      │                      │
   │    │                      ▼                      │
   │    │               trade_logic.py                │
   │    │               open_paper_position()         │
   │    │                      │                      │
   │    │                      ▼                      │
   │    │               binance_square.db             │
   │    │               → positions 表                │
   │    │                                             │
   │    ├─ Step F: Stage 3 (每 interval 轮)            │
   │    │     wallet-analysis + trades + tags         │
   │    │                                             │
   │    ├─ DB 写入 (onchain.db):                       │
   │    │     snapshots + clusters + pool +           │
   │    │     wallet_analysis + trades + tags         │
   │    │                                             │
   │    └─ TG 通知 (if enabled)                       │
   │                                             │
   │  _load_setting() ←── 读 binance_square.db        │
   │                      trading_settings             │
   └──────────────┬───────────────────────────────────┘
                  │
        ┌─────────┴──────────┐
        │                    │
        ▼                    ▼
   ┌──────────────┐   ┌──────────────────┐
   │  onchain.db  │   │ binance_square.db │
   │              │   │                  │
   │ 9 张表       │   │ positions        │
   │ (链上数据)   │   │ trading_settings │
   └──────┬───────┘   └──────────────────┘
          │
          ▼
   ┌──────────────────────────────────────────────────┐
   │                 web.py (FastAPI)                  │
   │                                                  │
   │  /onchain                → 总览页面               │
   │  /onchain/token/{token}  → 集群详情页             │
   │                                                  │
   │  GET /api/onchain/status                          │
   │  GET /api/onchain/pool?stage=observation          │
   │  GET /api/onchain/pool?stage=actionable           │
   │  GET /api/onchain/clusters?token=X                │
   │  GET /api/onchain/cluster/analysis?token=X        │
   │  GET /api/onchain/cluster/portfolio?token=X       │
   │  GET /api/onchain/cluster/trades?token=X          │
   │  GET /api/onchain/snapshots?token=X&limit=10      │
   │                                                  │
   │  POST /api/settings  ← 保存配置 (含 6 集群 key)   │
   └──────────────────────┬───────────────────────────┘
                          │
                          ▼
                    浏览器前端
                    /onchain        (总览面板)
                    /onchain/token  (集群卡片)
                    /settings       (参数调节)
```

---

## 十一、关键设计决策与注意事项

### 背离信号公式
```
divergence_score = (fund_source_pct - 30) * (40 - top10_pct) / 100
```
- fundSource > 30 AND top10 < 40 → 越极端分越高
- 物理含义：隐藏关联多 + 表面分散 = 有人在暗中吸筹

### 有效集中度 vs 表层集中度
- **表层 top10%**: 前 10 地址持仓之和 → 可能包含交易所/合约
- **有效集中度**: 去掉交易所+合约后的真人集群持仓之和 → 更准

### CEX Staging 为什么降权 (15%)
- cluster-list 只能标记 `isExchange=true`，无法判断方向
- CEX 地址充币可能是"布弹药准备拉盘"也可能是"出货跑路"
- 所以权重只有 15%，仅作辅助参考

### LP 加速减少的判定
- 要求连续两轮都在减少（排除偶发波动）
- 最新减幅 > 前一轮的 1.5x 且 > 3% 才判"加速"
- 逻辑：项目方撤 LP = 要跑路或要拉盘（需结合其他信号）

### Stage 3 为何不每轮全量跑
- wallet-analysis 对每个地址调一次 API (每集群 top 5 地址)
- token trades 对每个集群调一次 API
- 假设 20 个 pool token × 平均 3 个有效集群 × 5 地址 = 300 次 wallet-analysis
- 串行 + sleep 0.3s = ~90s 仅 wallet-analysis 部分
- 所以通过 `onchain_cluster_analysis_interval` 控制频率

### 数据保留策略
| 表 | 保留轮数 | 理由 |
|----|---------|------|
| onchain_snapshots | 50 | 仅用于 sparkline 趋势图, 50 轮足够 |
| onchain_clusters | 1500 | 用于 Stage 3 环比, 需历史对比 |
| onchain_wallet_analysis | 1500 | 同上 |
| onchain_cluster_trades | 1500 | 同上 |
| onchain_cluster_tags | 1500 | 同上 |

1500 轮 × 1h = 约 62 天历史, 对 20 个 pool token 约 ~30K 行集群数据。
