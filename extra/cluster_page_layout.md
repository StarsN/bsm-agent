# 集群监控页面 — 布局 · 字段 · 数据流

## 一、页面布局

```
┌─ 导航栏 ────────────────────────────────────────────────────┐
│  🔍 链上监控    📋 集群监控                                   │
├─ Token 概览卡片 ────────────────────────────────────────────┤
│  代币: FF    链: bsc    状态: ●观察池    入池: 2.3d           │
│  表层Top10: 24.4%    资金同源: 47.7%    有效集中度: 24.6%    │
├─ 集群 #1 [● 真人] 控盘 18.5%  盈亏 +25.3%  9 地址 ── [▶] ──┤
│  [增持+1.2%] [吸筹中] [集群盈利+25.3%] [共持12币] [近期无卖出]│
│  ┌──────────┬──────┬───────┬────────┬────────────┬──────────┬──────────┬──────┐
│  │ 地址      │ 排名  │ 持仓% │ 总资产  │ 总买入/均价 │ 总卖出/均价│ 最后交易  │ 标签  │
│  ├──────────┼──────┼───────┼────────┼────────────┼──────────┼──────────┼──────┤
│  │ 0x35aa...│ #2   │ 23.58%│ $4672K │ 0笔       │ 0笔      │ --       │      │
│  │ 0xbd1d...│ #3   │ 17.00%│ $3382K │ 34笔/$10489│ 0笔     │ 2026-06-17│ 纯买入 盈利│
│  └──────────┴──────┴───────┴────────┴────────────┴──────────┴──────────┴──────┘
├─ 集群 #2 [交易所] 控盘 24.6% [折叠] ─────────────────────────┤
├─ 集群 #3 [合约] 控盘 5.0% [折叠] ───────────────────────────┤
└──────────────────────────────────────────────────────────────┘
```

## 二、Token 概览栏 — 字段取值

| 显示字段 | API 字段 | 数据来源 | 取值逻辑 |
|---------|---------|---------|---------|
| 代币 | `token` | URL pathname.split('/').pop() | 从 `/onchain/token/{token}` 提取 |
| 链 | `chain` | `/api/onchain/pool` → find by token | DB `onchain_pool.chain` |
| 状态 | `pool_stage` | 同上 | `observation` → ●观察池 / `actionable` → 🎯可建仓 |
| 入池 | `days_in_pool` | 同上 | API 实时计算: `(now - first_seen_at) / 86400` |
| 表层Top10 | `surface_top10_pct` | 同上 | DB `onchain_pool.surface_top10_pct` |
| 资金同源 | `fund_source_same_pct` | 同上 | DB `onchain_pool.fund_source_same_pct` |
| 有效集中度 | `effective_concentration` | 同上 | DB `onchain_pool.effective_concentration` |

## 三、集群卡片 — 字段取值

| 显示字段 | API 字段 | 数据来源 | 取值逻辑 |
|---------|---------|---------|---------|
| 集群编号 | `cluster_index` | `/api/onchain/clusters` | 0-based → 显示 +1 |
| 类型标签 | `is_real_cluster` | 同上 | 1 → ●真人; 0+含交易所→交易所; 0+含合约→合约 |
| 控盘% | `holding_percent` | 同上 | `onchain_clusters.holding_percent`（Step B 已 ×100） |
| 盈亏% | `pnl_percent` | 同上 | `onchain_clusters.pnl_percent`（cluster-list API 返回） |
| 地址数 | `address_count` | 同上 | `onchain_clusters.address_count` |

### 集群标签 — 取值逻辑

| 标签 | tag_type | 生成条件 | 数据来源 |
|------|----------|---------|---------|
| 增持 +X% | `holding_change` | 本轮 holding% - 上轮 > 0.5 | Step B 两轮对比 |
| 减持 -X% | `holding_change` | 上轮 - 本轮 > 0.5 | 同上 |
| 持仓稳定 | `holding_change` | abs(变化) ≤ 0.5 | 同上 |
| 吸筹中 | `api` | cluster-list trendType 含 "accumulating" | API 直接返回 |
| 派发中 | `api` | cluster-list trendType 含 "distributing" | API 直接返回 |
| 集群盈利 +X% | `api` | pnl_percent ≥ 0 | API 直接返回 |
| 集群亏损 X% | `api` | pnl_percent < 0 | API 直接返回 |
| 共持 N 个币 | `common_holdings` | ≥2 地址的持仓交集 ≥ 3 个币 | wallet-analysis balances 计算 |
| 近期无卖出 | `trade_behavior` | trades 中 sell 数量 = 0 | token trades API |
| 少量卖出 (N笔) | `trade_behavior` | 0 < sell ≤ 2 | 同上 |
| 活跃卖出 (N笔) | `trade_behavior` | sell > 2 | 同上 |
| 近期买入 (N笔) | `trade_behavior` | trades 中 buy 数量 > 0 | 同上 |

## 四、地址表格 — 字段取值

| 显示字段 | API 字段 | 数据来源 | 取值逻辑 |
|---------|---------|---------|---------|
| 地址 | `address` | `/api/onchain/clusters` → addresses_json | 前10后6省略显示，title 全量 |
| 排名 | `address_rank` | 同上 | 集群内排名 |
| 持仓% | `holding_percent` | 同上 | 原始小数 ×100 → 2位小数% |
| 总资产 | `total_value_usd` | `/api/onchain/cluster/analysis` | `$ + (值/1000).toFixed(0) + K` |
| 总买入/均价 | `buy_count_30d` + `avg_buy_value` | 同上 | `N笔/$M`，null → `--` |
| 总卖出/均价 | `sell_count_30d` + `avg_sell_value` | 同上 | `N笔/$M`，null → `--` |
| 最后交易 | `last_trade_time` | 同上 | ISO时间戳 → `substring(0,10)`，null → `--` |
| 标签 | 多个字段 | 同上 + addresses | EX/CT/KOL/纯买入/盈利 |

### 地址标签逻辑

| 标签 | 颜色 | 条件 |
|------|------|------|
| EX | 黄 | `a.is_exchange === true` |
| CT | 黄 | `a.is_contract === true` |
| KOL | 紫 | `a.is_kol === true` |
| 纯买入 | 绿 | `buy_count_30d > 0 AND sell_count_30d === 0` |
| 盈利 | 绿 | `realized_pnl > 0` |

## 五、完整数据流

```
┌─────────────────────────────────────────────────────────────┐
│                       数据采集层                              │
│                                                             │
│  Step B (每轮, 0.5-1h)                                      │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ onchainos token cluster-list                        │    │
│  │   ├→ cluster_list (解析) → effective_conc 计算       │    │
│  │   ├→ cluster_records → onchain_clusters 表           │    │
│  │   └→ cluster_raw → onchain_snapshots.cluster_raw_json│    │
│  └─────────────────────────────────────────────────────┘    │
│                          │                                   │
│                          ▼                                   │
│  Stage 3 (每 4 轮, 2-4h)                                    │
│  ┌─────────────────────────────────────────────────────┐    │
│  │ 对每个观察池/建仓池 token:                            │    │
│  │                                                     │    │
│  │ 1. 加载本轮+上轮 onchain_clusters → 对比 holding%    │    │
│  │    └→ holding_change 标签                            │    │
│  │                                                     │    │
│  │ 2. 读 trend_type/pnl_percent                       │    │
│  │    └→ api 标签                                       │    │
│  │                                                     │    │
│  │ 3. 每个真人地址(≥MIN_HOLDING_PCT):                    │    │
│  │    onchainos workflow wallet-analysis                │    │
│  │    ├→ wa_records → onchain_wallet_analysis 表        │    │
│  │    └→ balances 提取持仓 → 取交集 → common_holdings 标签│    │
│  │                                                     │    │
│  │ 4. 批量 onchainos token trades --wallet-filter       │    │
│  │    ├→ onchain_cluster_trades 表                      │    │
│  │    └→ 买卖分析 → trade_behavior 标签                  │    │
│  │                                                     │    │
│  │ 所有标签 → onchain_cluster_tags 表                    │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                        API 服务层                             │
│                                                             │
│  GET /api/onchain/clusters?token=X                           │
│    → onchain_clusters (最新轮) + onchain_cluster_tags (最新轮)│
│    → addresses_json 反序列化                                  │
│                                                             │
│  GET /api/onchain/cluster/analysis?token=X                    │
│    → onchain_wallet_analysis (最新轮)                         │
│                                                             │
│  GET /api/onchain/pool                                        │
│    → onchain_pool (全部 token，前端 find 筛选)                 │
│                                                             │
│  GET /api/onchain/cluster/portfolio?token=X   (备用)          │
│  GET /api/onchain/cluster/trades?token=X      (备用)          │
└─────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────┐
│                        前端渲染层                             │
│                                                             │
│  /onchain/token/{token}                                     │
│                                                             │
│  loadCluster() — 每 60s                                     │
│    ├→ Promise.all([clusters, pool, analysis])               │
│    ├→ analysisMap = {address → analysis_data}               │
│    ├→ renderOverview(pool_info)                             │
│    └→ renderClusters(clusters, analysisMap)                 │
│         ├→ 真人集群默认展开，交易所/合约折叠                   │
│         ├→ 标签颜色: 绿(增持/买入) 红(减持) 紫(共持) 蓝(API)  │
│         └→ renderAddressTable(addresses, analysisMap)        │
│              └→ 按地址查 analysisMap，无数据 → --             │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│                        数据清理                               │
│                                                             │
│  每轮 Step B 后: _purge_by_round(1500轮) × 5张表              │
│    onchain_clusters / address_portfolio / wallet_analysis    │
│    cluster_trades / cluster_tags                             │
│                                                             │
│  升级迁移: init_onchain_db → _onchain_migrate 补列            │
└─────────────────────────────────────────────────────────────┘
```

## 六、存储表关系

```
onchain_snapshots  ← cluster_raw_json（原始JSON，1份/token/轮）
onchain_clusters   ← 解析后字段 + addresses_json（N行/token/轮）
onchain_cluster_tags ← 标签（N行/token/Stage3轮）
onchain_wallet_analysis ← wallet-analysis 提取字段 + 原始JSON
onchain_address_portfolio ← 已废弃（不再写入，保留历史数据）
onchain_cluster_trades ← trades 原始JSON
```
