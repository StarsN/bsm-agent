# 链上监控 — 观察池 (Stage 1 吸筹检测) 全链路分析

## 数据流概览

```
OnchainOS CLI (3个接口)
    │
    ├─ hot-tokens      → Step A 候选池（横盘 + 市值 + 成交量）
    ├─ cluster-overview → Step B 聚类概览（资金同源判断）
    └─ cluster-list    → Step B 聚类详情（有效集中度计算）
            │
            ▼
      onchain_scanner.py
            │
    ┌───────┴───────┐
    │  背离检测      │
    │  条件1: has_perpetual       (币安有合约)
    │  条件2: fund_source > 30%   (隐藏关联)
    │  条件3: top10_hold < 40%    (表面分散)
    │  全部满足 → 入观察池        │
    └───────┬───────┘
            │
            ▼
      onchain_pool 表 (onchain.db)
            │
            ▼
      /api/onchain/pool → 前端卡片渲染
```

---

## 一、Step A：候选池 — `onchainos token hot-tokens`

### CLI 命令

```bash
onchainos token hot-tokens \
  --rank-by 5 \             # 按成交量排序
  --time-frame 4 \           # 24h 时间窗口
  --limit 50 \               # 最多 50 个候选
  --market-cap-min 500000 \  # 市值 >= $500K
  --risk-filter true \       # 过滤风险代币
  --price-change-min -5 \    # 24h 最低涨幅（防暴跌币）
  --price-change-max 5       # 24h 最高涨幅（防暴拉币，锁定横盘）
```

### 原始返回字段 → 代码映射

| 代码字段 | 原始 JSON key | 类型 | 备注 |
|----------|-------------|------|------|
| `token` | `tokenSymbol` | str | 转大写 |
| `token_address` | `tokenContractAddress` | str | 后续 cluster 查询的入参 |
| `token_name` | `tokenName` | str | 未使用 |
| `chain` | `chainIndex` → `_chain_name()` | str | 数字→链名映射 (56→bsc, 1→ethereum...) |
| `hot_rank` | `cursor` | int | 排名游标 |
| `top10_hold_pct` | `top10HoldPercent` | float | **百分比**，无需转换 |
| `holders_count` | `holders` | int | 持币人数 |
| `market_cap_usd` | `marketCap` | float | 美元市值 |

### 关键判断：`top10HoldPercent` 格式

已验证：OnchainOS `hot-tokens` 的 `top10HoldPercent` 返回**百分比**（如 54.4），不需要 `_pct()` 转换。与 `cluster-overview` 不同（后者返回小数 0-1，需要 `_pct()` 转百分比）。

验证数据：
```json
{"tokenSymbol": "quq", "top10HoldPercent": "54.3981"}   // 54.4%，百分比
{"tokenSymbol": "EVAA", "top10HoldPercent": "67.4668"}  // 67.5%，百分比
```

---

## 二、Step B：聚类分析 — `onchainos token cluster-overview`

### CLI 命令

```bash
onchainos token cluster-overview --address <合约地址> --chain <链名>
```

### 原始返回字段 → 代码映射

| 代码字段 | 原始 JSON key | 转换 | 说明 |
|----------|-------------|------|------|
| `cluster_concentration` | `clusterConcentration` | 无 | 聚类集中度等级（文本） |
| `top100_holdings_pct` | `top100HoldingsPercent` | **`_pct()`** | 小数→百分比 |
| `holder_same_fund_pct` | `holderSameFundSourcePercent` | **`_pct()`** | **核心背离指标** |
| `holder_new_addr_pct` | `holderNewAddressPercent` | **`_pct()`** | Stage 2 Gas 检测用 |
| `holder_same_creation_pct` | `holderSameCreationTimePercent` | **`_pct()`** | Stage 2 Gas 检测用 |
| `rug_pull_pct` | `rugPullPercent` | **`_pct()`** | 风险参考 |

### `_pct()` 函数

```python
def _pct(val) -> float | None:
    """小数 (0-1) → 百分比 (0-100)"""
    v = _safe_float(val)
    if v is None:
        return None
    return round(v * 100, 2)
```

cluster-overview 的所有百分比字段返回**小数**（0.42 = 42%），必须乘 100 才能和阈值比较。

---

## 三、Step B：聚类详情 — `onchainos token cluster-list`

### CLI 命令

```bash
onchainos token cluster-list --address <合约地址> --chain <链名>
```

### 原始返回结构

```json
{
  "clusterList": [
    {
      "holdingPercent": 0.12,      // 该集群总持仓占比（小数）
      "trendType": ["accumulating"],
      "pnlPercent": 15.5,
      "clusterAddressList": [
        {
          "address": "0x...",
          "addressRank": 1,
          "holdingPercent": 0.08,  // 该地址持仓占比（小数）
          "isExchange": false,
          "isContract": false,
          "isKol": false
        }
      ]
    }
  ]
}
```

### 有效集中度 (effective_concentration) 计算

```python
effective_conc = 0.0
if cluster_list:
    for cl in cluster_list:
        # 检查集群内是否有交易所/合约地址
        has_exchange = any(a["is_exchange"] for a in cl["addresses"])
        has_contract = any(a["is_contract"] for a in cl["addresses"])
        # 排除交易所和合约 → 只计"真人钱包"集群
        if not has_exchange and not has_contract:
            effective_conc += cl["holding_percent"] or 0.0

# 小数累加 ×100 → 百分比显示
effective_concentration = round(effective_conc * 100, 2)
```

### 计算示例

```
cluster-list 返回 6 个集群：

集群A: holding=18%, addresses=[交易所: 0xbinance]        → has_exchange=True  → 跳过
集群B: holding=12%, addresses=[合约: 0xpancake]          → has_contract=True  → 跳过
集群C: holding=10%, addresses=[EOA①, EOA②, EOA③]       → 纯钱包 → 计入 +10%
集群D: holding= 7%, addresses=[EOA④, EOA⑤]             → 纯钱包 → 计入 +7%
集群E: holding= 5%, addresses=[EOA⑥, 合约: 0xuniswap]   → has_contract=True  → 跳过
集群F: holding= 3%, addresses=[EOA⑦]                    → 纯钱包 → 计入 +3%

effective_concentration = 10 + 7 + 3 = 20%
```

### 为什么这样算

- **交易所地址**：币安、OKX 等的热钱包，大量用户资金汇集，不代表庄家行为
- **合约地址**：Uniswap/PancakeSwap 的 LP 合约、协议金库，是正常 DeFi 活动
- **纯 EOA**：排除上述两类后，剩下的集群更可能反映"真实控盘方"的持仓

有效集中度越接近 `fund_source_same_pct`（资金同源比例），背离信号越可靠。

---

## 四、背离检测 — 进入观察池的门槛

### 三条件（必须同时满足）

```
条件 1: has_perpetual(token) = True
        ↓
        币安 USDT 永续合约已上市
        （调用 Binance fapi/v1/exchangeInfo，有 1h 缓存）

条件 2: holder_same_fund_pct > 30%
        ↓
        Top1000 持有者中，>30% 地址有共同资金来源
        （通过 cluster-overview 获取，已转为百分比）

条件 3: top10_hold_pct < 40%
        ↓
        表层 Top10 持仓 <40%，看起来分散
        （通过 hot-tokens 获取，已是百分比）
```

### 背离分数的物理含义

```python
divergence_score = (fund_pct - fund_min) * (surface_max - top10_pct) / 100
```

| 场景 | fund=50, top10=15 | fund=35, top10=35 | fund=60, top10=10 |
|------|-------------------|-------------------|-------------------|
| 公式 | (50-30)×(40-15)/100 | (35-30)×(40-35)/100 | (60-30)×(40-10)/100 |
| 得分 | 5.0（强信号） | 0.25（弱信号） | 9.0（极强信号） |

分数越大，背离越显著：一方面资金高度同源，另一方面表层极度分散。

---

## 五、观察池卡片显示的字段

### API 返回字段 → 前端渲染

| 卡片位置 | API 字段 | 数据来源 | 备注 |
|----------|---------|---------|------|
| 代币名 | `token` | onchain_pool 表 | |
| 链 | `chain` | onchain_pool 表 | bsc/ethereum/polygon... |
| 入池天数 | `days_in_pool` | API **实时计算** | `(now - first_seen_at) / 86400` |
| 状态徽章 | `divergence_detected` | onchain_pool 表 | ●观察 / 🎯可建仓 |
| K 线图 | — | 前端拼接 TradingView | `BINANCE:{token}USDT` 1h |
| 表层 Top10 | `surface_top10_pct` | onchain_pool 表 | 百分比 |
| 资金同源 | `fund_source_same_pct` | onchain_pool 表 | 百分比 |
| 有效集中度 | `effective_concentration` | onchain_pool 表 | 百分比（自算） |
| 趋势图 | `sparkline` | onchain_snapshots 表 | 最近 6 轮 effective_concentration 折线 |

### sparkline 数据来源

```python
# web.py api_onchain_pool
snaps = storage.onchain_snapshots_for_token(conn, r["token"], limit=6)
snaps.reverse()  # 时间升序
r["sparkline"] = [s.get("effective_concentration") for s in snaps]
```

---

## 六、数据流转总结

```
┌──────────────────────────────────────────────────────────┐
│                   OnchainOS CLI（远程）                    │
│                                                          │
│  hot-tokens ──→ 50 候选（横盘 + 市值 + 成交量）            │
│       ↓                                                  │
│  has_perpetual 过滤（Binance API，1h 缓存）                │
│       ↓                                                  │
│  cluster-overview ──→ fund_pct, new_addr, creation...    │
│  cluster-list     ──→ 各集群持仓 + 地址类型标注             │
│       ↓                                                  │
│  有效集中度 = Σ(非交易所+非合约集群的 holding_percent)×100  │
│       ↓                                                  │
│  背离检测: fund>30 且 top10<40 → divergence_detected=1    │
└──────────────────────┬───────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────┐
│                   onchain.db（本地 SQLite）               │
│                                                          │
│  onchain_snapshots  ← 每轮全量快照（时序）                 │
│  onchain_pool       ← 观察池/可建仓（每 token 一行）       │
│  onchain_lp_snapshots ← LP 储备时序（Stage 2 用）         │
│  onchain_scanner_status ← 扫描器心跳                      │
└──────────────────────┬───────────────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────────────┐
│                    前端 /onchain                          │
│                                                          │
│  Panel 1: 扫描状态（轮次、已扫描、观察池/可建仓数量）       │
│  Panel 2: 观察池卡片（token/链/天数/K线/4项指标/趋势图）   │
│  Panel 3: 可建仓卡片（同上 + Stage 2 信号：LP/Gas/CEX）    │
│                                                          │
│  每 60 秒自动刷新                                         │
│  每页 4 张卡片，分页                                       │
└──────────────────────────────────────────────────────────┘
```
