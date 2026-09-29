# 集群监控 — 完整流程详解

> 生成时间: 2026-06-18

---

## 一、数据获取总览

```
                     OnchainOS CLI
                          │
          ┌───────────────┼────────────────┐
          │               │                │
   cluster-list    wallet-analysis   token-trades
          │               │                │
          ▼               ▼                ▼
   ┌─────────────────────────────────────────────┐
   │           onchain_scanner.py                │
   │                                             │
   │  Step B  ─→ 新候选 token 的集群数据          │
   │  Step B2 ─→ 观察池 token 的集群数据（补扫）   │
   │  Step F  ─→ 标签计算（Stage 3）              │
   └──────────────┬──────────────────────────────┘
                  │
                  ▼
   ┌──────────────────────┐
   │     onchain.db       │
   │                      │
   │  onchain_clusters    │ ← 每轮每集群 1 行
   │  onchain_wallet_analysis │ ← 每地址分析数据
   │  onchain_cluster_trades  │ ← 批量交易记录
   │  onchain_cluster_tags    │ ← 计算标签
   └──────┬───────────────┘
          │
          ▼
   ┌──────────────────────┐
   │    web.py API        │
   │                      │
   │  /api/onchain/clusters        │
   │  /api/onchain/cluster/analysis│
   │  /api/onchain/cluster/trades  │
   │  /api/onchain/pool            │
   └──────┬───────────────┘
          │
          ▼
   ┌──────────────────────┐
   │   前端 /onchain/token │
   │   集群卡片 + 地址表    │
   │   60s 自动刷新        │
   └──────────────────────┘
```

---

## 二、Step B / B2 — 集群原始数据采集

每轮对候选 + 观察池 token，调用 OnchainOS CLI 获取原始集群数据。

### 2.1 API 调用

```
for each token (候选 + 观察池去重):
  │
  ├─ fetch_cluster_list(token_address, chain)
  │     └─ onchainos token cluster-list --address X --chain bsc
  │
  │  返回结构:
  │    data.clusterList[]:
  │      holdingPercent: 0.0523        ← 集群持有占比 (0-1)
  │      trendType: ["accumulating"]   ← 吸筹/派发
  │      pnlPercent: 15.2              ← 集群盈亏%
  │      clusterAddressList[]:
  │        address: "0x..."
  │        addressRank: 3
  │        holdingPercent: 0.012
  │        isExchange: false           ← 交易所标记
  │        isContract: false           ← 合约标记
  │        isKol: false                ← KOL 标记
  │
  └─ 返回 (parsed_list, raw_json_string)
```

### 2.2 有效集中度计算

```
effective_conc = 0.0
for each cluster in cluster_list:
    has_exchange = any(address.is_exchange)
    has_contract = any(address.is_contract)
    if NOT has_exchange AND NOT has_contract:
        effective_conc += cluster.holding_percent

effective_concentration = effective_conc * 100   ← 转为百分比
```

**含义**: 去掉交易所/合约地址后，真人集群的总持仓占比。这才是真正意义上的"暗仓集中度"。

### 2.3 集群记录写入

```
if onchain_cluster_enabled and cluster_list:
    for each cluster:
        cluster_records.append({
            token, token_address, chain, cluster_index,
            holding_percent:     cl.holding_percent * 100,     ← 百分比
            trend_type:          json.dumps(cl.trend_type),    ← JSON 数组
            pnl_percent:         cl.pnl_percent,               ← 盈亏%
            address_count:       len(cl.addresses),            ← 地址数
            is_real_cluster:     0 if (ex or ct) else 1,       ← 真人/过滤
            addresses_json:      json.dumps(cl.addresses),     ← 完整地址 JSON
        })

→ 批量 INSERT INTO onchain_clusters
→ purge: 保留最近 onchain_cluster_keep_rounds 轮 (默认 1500)
```

### 2.4 集群数据也存入快照

```
snapshot["cluster_raw_json"] = cluster_raw  ← 原始 JSON，防丢字段
→ INSERT INTO onchain_snapshots
```

---

## 三、Step F — Stage 3 集群标签分析

每 `onchain_cluster_analysis_interval` 轮执行一次 (默认 1，即每轮)。

### 3.1 入口

```
if onchain_cluster_enabled AND onchain_cluster_analysis_enabled:
    if round_number % interval == 0:
        _analyze_clusters(round_number)
```

### 3.2 详细流程

```
_analyze_clusters(round_number):
  │
  ├─ 加载所有池 token: onchain_pool_get_all(conn)
  │     observation + actionable 全覆盖
  │
  └─ for each pool token (串行):
        │
        ├─ ① 读 DB 集群数据
        │     clusters_now  = onchain_clusters_for_token(token, limit_rounds=1)  ← 最新轮
        │     clusters_prev = onchain_clusters_for_token(token, limit_rounds=2)  ← 最近 2 轮
        │     │
        │     └─ prev_map 构建:
        │         if len(clusters_prev) > len(clusters_now):
        │           (有上一轮数据时才对比)
        │           for c in clusters_prev:
        │               if c.round_number != 当前轮:
        │                 prev_map[c.cluster_index] = c
        │
        └─ for each cluster in clusters_now:
              │
              ├─ 过滤: is_real_cluster == 0 → 跳过 (交易所/合约)
              ├─ 过滤: holding_percent < min_holding_pct → 跳过 (默认 <1%)
              │
              ├─ ② 持仓变化标签 (holding_change)
              │     prev = prev_map.get(cluster_index)
              │     if prev:
              │       hp_change = hp_now - hp_prev
              │       > +0.5%  → "增持 +2.1%"
              │       < -0.5%  → "减持 -1.3%"
              │       ≤ 0.5%   → "持仓稳定"
              │     标签写入 onchain_cluster_tags
              │
              ├─ ③ API 标签 (api)
              │     "accumulating" in trend_type → "吸筹中"
              │     "distributing" in trend_type → "派发中"
              │     pnl_percent != None → "集群盈利 +15.2%" / "集群亏损 8.3%"
              │     标签写入 onchain_cluster_tags
              │
              ├─ ④ 钱包分析 (wallet-analysis)
              │     real_addrs = 集群中非交易所/非合约地址 (取 top_n 个)
              │     默认: onchain_cluster_top_addresses = 5
              │
              │     for each address:
              │       fetch_wallet_analysis(address, chain)
              │         └─ onchainos workflow wallet-analysis --address X --chain bsc
              │
              │       提取字段 → INSERT onchain_wallet_analysis:
              │         total_value_usd, buy_count_30d, sell_count_30d,
              │         buy_volume_30d, sell_volume_30d, avg_buy_value,
              │         avg_sell_value, realized_pnl, win_rate,
              │         last_trade_time, analysis_json (原始 JSON)
              │
              │       提取持仓 → addr_holdings[address] = {token, token, ...}
              │       sleep 0.3s
              │
              ├─ ⑤ 交叉持仓标签 (common_holdings)
              │     if 地址数 >= 2:
              │       common = ∩(所有地址的持仓) - {本币}
              │       if len(common) >= 3:
              │         标签: "共持 5 个币" (列出前 8 个)
              │         → onchain_cluster_tags
              │
              └─ ⑥ 交易行为标签 (trade_behavior)
                    addr_list = [所有真人地址]
                    fetch_token_trades(token_addr, chain, wallet_filter=addr_list)
                      └─ onchainos token trades --address X --chain bsc --wallet-filter A,B,C

                    写入 onchain_cluster_trades:
                      {round_number, token, cluster_index,
                       addresses_batch, trades_json}

                    分析:
                      sells = filter(tradeType == "2")
                      buys  = filter(tradeType == "1")
                      无卖出      → "近期无卖出"
                      少量卖出    → "少量卖出 (N笔)"
                      活跃卖出    → "活跃卖出 (N笔)"
                      有买入      → "近期买入 (N笔)"
                      → onchain_cluster_tags

              sleep 0.2s (between clusters)
```

### 3.3 标签类型汇总

| tag_type | 数据来源 | 示例 |
|----------|---------|------|
| `holding_change` | DB 环比 | 增持 +2.5% / 减持 -1.3% / 持仓稳定 |
| `api` | cluster-list 原始字段 | 吸筹中 / 派发中 / 集群盈利 +15.2% |
| `common_holdings` | wallet-analysis 交叉对比 | 共持 5 个币 (BTC,ETH,BNB,SOL,MATIC) |
| `trade_behavior` | token trades 批量查询 | 近期无卖出 / 活跃卖出 (5笔) / 近期买入 (3笔) |

---

## 四、DB 存储结构

### 4.1 onchain_clusters

每轮每个集群一行，包含完整地址明细 JSON。

```
id, round_number, token, token_address, chain,
cluster_index, holding_percent, trend_type, pnl_percent,
address_count, is_real_cluster, addresses_json, snapshot_at

索引: (token, round_number), (round_number)
保留: onchain_cluster_keep_rounds (默认 1500)
```

### 4.2 onchain_wallet_analysis

Stage 3 对每个真人地址调用 wallet-analysis，提取关键字段。

```
id, round_number, token, cluster_index, address,
total_value_usd, buy_count_30d, sell_count_30d,
buy_volume_30d, sell_volume_30d, avg_buy_value,
avg_sell_value, realized_pnl, win_rate,
last_trade_time, analysis_json
```

### 4.3 onchain_cluster_trades

Stage 3 对每个真人集群调用 token trades（批量查询集群地址）。

```
id, round_number, token, cluster_index,
addresses_batch (逗号分隔), trades_json
```

### 4.4 onchain_cluster_tags

Stage 3 生成的 4 类标签。

```
id, round_number, token, cluster_index,
tag_type, tag_label, tag_detail
```

---

## 五、前端数据流

### 5.1 集群详情页 `/onchain/token/{token}`

```
浏览器加载
  │
  ├─ loadCluster() 每 60s 执行
  │
  ├─ Promise.all([
  │     GET /api/onchain/clusters?token=X
  │       → onchain_clusters_for_token(token, limit_rounds=1)
  │       → onchain_cluster_tags_for_token(token, limit_rounds=1)
  │       → 合并: cluster + tags[] + addresses (JSON.parse)
  │       → 返回 [{...cluster, addresses: [...], tags: [...]}]
  │
  │     GET /api/onchain/pool
  │       → onchain_pool_get_all()
  │       → 找到当前 token 的 pool 记录
  │       → 渲染顶部概览面板 (7 个指标)
  │
  │     GET /api/onchain/cluster/analysis?token=X
  │       → onchain_wallet_analysis_for_token(token)
  │       → 按 address 建立 HashMap
  │       → 渲染地址详情（每行附加钱包分析数据）
  │   ])
  │
  └─ 渲染:
      ┌──────────────────────────────────────────┐
      │ Token 概览                                │
      │ 代币 | 链 | 状态 | 入池 | 表层Top10 |     │
      │ 资金同源 | 有效集中度                       │
      ├──────────────────────────────────────────┤
      │ 集群 #1 ● 真人 控盘 3.2%  8 地址          │
      │ [增持+2.1%] [吸筹中] [共持5币] [近期无卖出] │
      │ ▸ 地址明细:                                │
      │   地址        排名  持仓%  总资产  买入均价..│
      ├──────────────────────────────────────────┤
      │ 集群 #2 交易所 控盘 15.3%  (折叠, 半透明)   │
      │ 集群 #3 合约   控盘 8.7%   (折叠, 半透明)   │
      │ 集群 #4 ● 真人 控盘 1.5%  3 地址           │
      │ ...                                       │
      └──────────────────────────────────────────┘
```

### 5.2 卡片样式

```
真人集群 (is_real_cluster=1):
  → cluster-card real    (绿色左边框, 不透明)
  → 默认展开 (class="cluster-body open")

过滤集群 (is_real_cluster=0):
  → cluster-card filtered (灰色左边框, opacity:0.7)
  → 默认折叠
  → 标记: "交易所" / "合约" (根据 addresses_json 中的 is_exchange/is_contract)
```

### 5.3 标签颜色

```
holding_change:  增持=绿  减持=红  稳定=黄
api:             =蓝
common_holdings: 紫色
trade_behavior:  无卖出=绿  其他=黄
```

---

## 六、完整端到端数据流

```
┌─────────────────────────────────────────────────────────────────┐
│                    OnchainOS CLI                                 │
│                                                                 │
│  token cluster-list       → clusterList[] + clusterAddressList  │
│  workflow wallet-analysis → balances + performance + activities │
│  token trades             → DEX 交易记录列表                     │
└──────────────┬──────────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────────┐
│  okx_source.py                                                   │
│                                                                  │
│  fetch_cluster_list()      → (parsed_list, raw_json)             │
│  fetch_wallet_analysis()   → dict (balances + perf + activities) │
│  fetch_token_trades()      → list[dict] (DEX trades)             │
└──────────────┬───────────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────────┐
│  onchain_scanner.py                                              │
│                                                                  │
│  Step B (每轮):                                                  │
│    for 新候选 token:                                              │
│      cluster_list, cluster_raw = fetch_cluster_list(addr)        │
│      → cluster_records[] (新候选的集群数据)                       │
│      → snapshots[].cluster_raw_json                              │
│                                                                  │
│  Step B2 (每轮):                                                 │
│    for 观察池 token (不在候选中的):                                │
│      同上 → cluster_records[] (观察池的集群数据)                  │
│      → snapshots[].cluster_raw_json                              │
│                                                                  │
│  DB 写入:                                                        │
│    onchain_snapshot_insert_batch(snapshots)                      │
│    onchain_cluster_insert_batch(cluster_records)                 │
│    _purge_by_round("onchain_clusters", keep)                     │
│                                                                  │
│  Step F (每 interval 轮):                                        │
│    _analyze_clusters():                                          │
│      for 所有池 token:                                           │
│        读 onchain_clusters (最新+上一轮)                          │
│        → holding_change 标签                                     │
│        → api 标签 (trend_type, pnl)                              │
│        fetch_wallet_analysis(每地址)                              │
│          → onchain_wallet_analysis                               │
│          → common_holdings 标签                                  │
│        fetch_token_trades(每集群)                                 │
│          → onchain_cluster_trades                                │
│          → trade_behavior 标签                                   │
│        写 onchain_cluster_tags                                   │
│    _purge_by_round(各表, keep)                                   │
└──────────────┬───────────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────────┐
│  onchain.db                                                      │
│                                                                  │
│  onchain_clusters         ← 每轮集群明细 (1500 轮)               │
│  onchain_wallet_analysis  ← 地址分析 (1500 轮)                   │
│  onchain_cluster_trades   ← 交易记录 (1500 轮)                   │
│  onchain_cluster_tags     ← 4 类标签 (1500 轮)                   │
│  onchain_snapshots        ← 含 cluster_raw_json (50 轮)          │
└──────────────┬───────────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────────┐
│  web.py                                                          │
│                                                                  │
│  GET /api/onchain/clusters?token=X                               │
│    → clusters + tags + addresses (JSON.parse)                    │
│                                                                  │
│  GET /api/onchain/cluster/analysis?token=X                       │
│    → wallet_analysis_by_address                                  │
│                                                                  │
│  GET /api/onchain/pool                                           │
│    → pool 全量 (含 sparkline)                                    │
└──────────────┬───────────────────────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────────────────────┐
│  前端 /onchain/token/{token}                                     │
│                                                                  │
│  loadCluster() {                                                 │
│    Promise.all([clusters, pool, analysis])                       │
│    → analysisMap[address] = wallet_data                          │
│    → renderOverview(pool.find token)                             │
│    → renderClusters(all_clusters, analysisMap)                   │
│      for each cluster:                                           │
│        卡片: 集群 #, 标签, 控盘%, 地址数                          │
│        展开: 地址表 (8 列, 含 wallet 数据)                       │
│  }                                                               │
│  setInterval(loadCluster, 60000)                                 │
└──────────────────────────────────────────────────────────────────┘
```

---

## 七、关键配置项

| 配置 | 默认值 | 作用 |
|------|--------|------|
| `onchain_cluster_enabled` | true | 集群监控总开关 |
| `onchain_cluster_analysis_enabled` | true | Stage 3 标签计算开关 |
| `onchain_cluster_analysis_interval` | 1 | Stage 3 执行间隔（轮） |
| `onchain_cluster_top_addresses` | 5 | 每集群取 Top N 地址做 wallet-analysis |
| `onchain_cluster_min_holding_pct` | 1.0 | 最小集群持仓%（低于此值跳过分析） |
| `onchain_cluster_keep_rounds` | 1500 | 集群相关表保留轮数 |
