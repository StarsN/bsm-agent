# 跟单数据源：alphafox → 币安 order-history 可行性方案

> 2026-09-12 整理。本文所有"实测"结论都是当轮在**服务器**上真实调用币安接口得到的（probe 脚本用完即删，未落库）。
> 标注：**【实测】**= 有真实返回；**【推断】**= 由代码/数据推出但未直接验证；**【未验证】**= 需要再打接口确认。

---

## 一、现状：数据从哪来、用到哪

### 1.1 现在唯一的跟单数据源
`web.py::_af_request()` → `https://www.alphafox.app/api/radar/{traders, snapshot?sourceId=, dna}`

快照 `data` 字段（实测 dump）：

```
id / portfolioId / polledAt / traderName / avatarUrl / marginBalance / aumAmount /
currentCopyCount / maxCopyCount / sharpRatio / winRate / totalPnl / totalCopierPnl /
maxDrawdown / symbolCount / totalNotional / totalUnrealizedPnl / effectiveLeverage /
positions[] / closedPositions[] / latestOrders[] / performanceCurve[]
```

### 1.2 四个 TAG 各自吃什么（`web.py::ALPHAFOX_HTML::renderTabBody`）

| TAG | 数据来源 | 逐字段 |
|---|---|---|
| **持有仓位** | `snap.positions[]` | baseAsset/symbol、side、absQty、avgPrice、**markPrice**、notional、**unrealizedPnl**、**leverage**、**estimatedLiqPrice**、lastOrderTime |
| **成交流水** | `snap.latestOrders[]` | time、symbol、**canonicalAction**、side、avgPrice、executedQty、notional、realizedPnl |
| **交易点位** | `snap.tradePoints`（按币分组） | timeMs、action(OPEN/CLOSE)、side、price、quantity、notional |
| **交易分析** | `curData.dna` + `curData.symbol_stats` | dna.profile、dna.profitSharingRate、dna.charts(pnl)、symbol_stats[{symbol,count,pnl,win_rate,items[{openTime,closeTime,side,entryPrice,exitPrice,size,**leverage**,pnl}]}] |

### 1.3 跟单信号链路吃哪几个字段（这决定"换源"的真正代价）

`alphafox_trade.process()` 的输入 = **持仓快照**（不是流水），逐字段用途：

| 字段 | 用途 | 缺了会怎样 |
|---|---|---|
| `symbol` / `side` | key（经 `_pos_key`/`_token_of` 归一，防拼写变化） | 平仓重开（第 1 轮修过） |
| `absQty` | 三阶段 diff 的核心（加/减/平判定） | 见第 5 轮"快照降级"保护 |
| `avgPrice` / `markPrice` / `notional` | 开仓/对齐计价 | 计价错 |
| **`leverage`** | 纸盘仓 `leverage` / `margin_amount` / `pnl_pct`；LEV 信号触发 | 纸盘杠杆与收益率口径错（**实盘不受影响**，见 3.4） |
| **`marginBalance`** | `k_qty = 预算 / marginBalance`（等比缩放基准） | 开仓比例无从计算 |

---

## 二、币安侧可用的接口（实测清单）

| 用途 | 接口 | 实测结果 |
|---|---|---|
| **订单/成交流水** | `POST /bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/order-history`<br>body `{portfolioId, startTime, endTime, pageSize, indexValue}` | ✅ **私域+公域都能查**。熬鹰资本窗口内 `total=47`；`pageSize=50` 返回 37 条；`indexValue` 可翻页（10→10→10 连续三页）；公域交易员也返回 15 条 |
| 当前持仓 | `GET .../lead-data/positions?portfolioId=` | ⚠️ **仅公域**：公域返回 759 行符号表（`positionAmount` 带符号、含 `leverage`）；**私域（熬鹰资本）返回 0 行** |
| 带单员列表/排行 | `POST .../home-page/query-list` | ✅ leadPortfolioId/nickname/roi/pnl/aum/mdd/winRate/copierPnl/currentCopyCount/maxCopyCount/portfolioType/startTime/aiSummary |
| 组合资料 | `GET .../lead-portfolio/detail?portfolioId=` | ✅ nickname/status/currentCopyCount/maxCopyCount/description |
| 业绩曲线 | `GET .../lead-portfolio/chart-data?dataType=ROI&portfolioId=&timeRange=7D` | ✅ `{value, dataType, dateTime}` |
| 币种业绩 | `GET .../lead-portfolio/performance/coin?portfolioId=&timeRange=7D` | ✅ |
| 逐笔（含手续费） | `POST .../lead-portfolio/trade-history` | ✅ 有 `fee`，但**不能按时间窗口查**（`startTime/endTime/pageNumber/indexValue` 实测都无效，只回最新一批） |

`order-history` 单条字段（实测样例）：

```json
{"symbol":"SNDKUSDT","baseAsset":"SNDK","quoteAsset":"USDT","side":"BUY","type":"MARKET",
 "positionSide":"LONG","executedQty":66.67,"avgPrice":1720.3534273,
 "totalPnl":0.0,"orderUpdateTime":1789046825444,"orderTime":1789046825444}
```

- `totalPnl`：**开仓腿为 0，平仓腿为真实已实现盈亏**（BTC 平仓腿实测 -10662.08 / +1787.34）
- `side` + `positionSide` 可推动作：`BUY+LONG`=开多、`SELL+LONG`=平多、`SELL+SHORT`=开空、`BUY+SHORT`=平空
- 分页：返回 `{indexValue, total, list}`，`indexValue` 作游标回传即取更早一页

---

## 三、字段能力对照：四个 TAG 能不能撑住

### 3.1 汇总

| TAG | 结论 | 说明 |
|---|---|---|
| **持有仓位** | ⚠️ **能显示，缺 2 列** | 方向/数量/开仓均价/浮盈/名义/最后下单时间 **都能重建**（已用真实数据验算一致，见 3.2）；**杠杆、预估强平价拿不到** |
| **成交流水** | ✅ **完整支持** | 全部字段可映射，且比 alphafox 多"时间窗口 + total + 翻页"（alphafox 只有最近 46 条） |
| **交易点位** | ✅ **完整支持** | 与成交流水同源，本地按币分组 + 标开/平即可 |
| **交易分析** | ✅ **基本支持**（缺 2 项） | 币种汇总/胜率/开平价/笔数可聚合；曲线用 `chart-data`；自述用 `detail.description`。**缺**：每笔明细的 `leverage` 列、`profitSharingRate`（未验证） |

### 3.2 持仓重建的正确性（用真实数据验算过）

熬鹰资本 09-12 的两条开空成交：

```
556.377 ETH @ 2599.9958  → 1,446,577.9 USDT
555.201 ETH @ 2606.8388  → 1,447,319.5 USDT
合计 1111.578 ETH，加权均价 2603.4137
```
对照 alphafox 持仓：`ETHUSDT SHORT, absQty=1111.578, avgPrice=2603.4137` —— **数量、均价、方向完全一致** ✅

→ 结论：**"完整流水 → 当前持仓（数量/均价/方向/已实现）"在数学上成立**，前提是流水覆盖该仓从开仓起的全部变动。

### 3.3 推不出来的字段（结构性缺失，不是"漏"）

| 字段 | 为什么推不出 |
|---|---|
| **杠杆** | 成交记录不含杠杆（10x 与 25x 的成交完全同形），只有 `lead-data/positions` 带（**仅公域**） |
| **保证金余额 marginBalance** | 成交记录/资料页都没有；私域交易员连 AUM 都拿不到 |
| **预估强平价** | 需要 杠杆 + 全仓/逐仓 + 钱包余额 + 维持保证金率 |
| **手续费** | `order-history` 无 fee 字段（`trade-history` 有但不能按窗口查）→ "净盈亏"会偏乐观 |
| 未成交挂单 | 成交记录只有已成交；带单员挂单是否公开**【未验证】** |

### 3.4 关键结论：这些缺失**不影响实盘**

实盘层（`alphafox_live.py`）的规模与杠杆**全部来自设置页，与交易员解耦**（模块文档原话："每仓规模 = live_notional / 价格，与交易员仓位规模解耦"）：

| 环节 | 用的值 |
|---|---|
| 开一份 | `lot_qty = live_notional / 价格`（设置页「每份名义」） |
| 杠杆 | `exchange.set_leverage(symbol, live_leverage)`（设置页「实盘杠杆」） |
| 加/减份 | lots ± 1，目标 `lots × live_notional / mark` |
| 归属校验 `_looks_like_ours` | 交易所杠杆 vs **我们自己记录的杠杆** |

→ **交易员的 leverage / marginBalance / 强平价，实盘一层都没用**；实盘只消费"事件流（开/加/减/平的时机）+ 方向 + 币种"。
→ 受影响的是**纸盘层**：`k_qty`（等比缩放）与纸盘仓的杠杆/保证金/收益率口径；把纸盘也改成"固定名义 + 固定杠杆"即可绕开。

---

## 四、中间有哪些坑（按严重度）

### 🔴 P1 流水"漏一段" = 幽灵平仓 = 真平仓
- **现象**：我们的 `process()` 是**快照 diff**：`prev` 有、`cur` 无 → 判定交易员平仓 → `_close_our_position` + `apply_signal(CLOSE)` → **市价平掉实盘真仓**。
- **坑点**：用"最新 N 条流水"重算持仓时，**开户头在窗口外的长期持仓会整体消失**。实测该交易员约 6 笔/天，`pageSize=10` 只覆盖 ~1.5 天，而他的持仓周期普遍 1~3 天（趋势仓可能更久）。
- **对策**：
  1. **不要纯增量累加**（漏一次就永久偏移），改为**每轮用"最近 N 页"重算**（N 覆盖持仓周期，建议 ≥ 3 天/200 笔）→ 中间漏过也会自愈；
  2. 首次接管必须回溯到"每个币上一次归零"之前（一次性成本）；
  3. 复用第 5 轮新增的**降级保护**：任何一轮数据不可信 → 跳过平仓阶段 + 不推进基线。

### 🔴 P2 被限流时返回的是"200 + 空数据"，与"真的没成交"无法区分
- **现象（trade-history 实测）**：秒级连发后返回 `{"code":"000000","success":true,"data":[],"total":null}` —— 应用层限流，**不是** 429/418。
- **坑点**：直接读成"没有持仓/没有成交" → 触发 P1 的幽灵平仓。
- **对策**：**`total == null` 一律视为"本轮数据不可信"**（等价于 `_DataUnavailable`：不动、不推进基线），并打日志。
- **注**：`order-history` 是否限流、阈值多少**【未验证】**——只在 22 秒间隔下连拉 5 次全部成功；**从未快速连发测试过**。

### 🟠 P3 同一"操作"被拆成多笔，且同毫秒边界有重复/丢失风险
- **现象**：实测同一秒内有 2~3 笔 MARKET 成交（SNDK 66.67+88.75；ETH 556.377+555.201；CL 三笔）。
- **坑点**：
  1. 不做聚类 → 成交流水/交易点位会刷出一堆"碎片"，一次操作看起来像开了三次仓；
  2. `indexValue` 是**毫秒时间戳游标**，同毫秒多笔在翻页边界**可能重复或跨页丢失** → 必须按 `(orderTime, symbol, side, positionSide, executedQty, avgPrice)` 做去重。
- **对策**：本地按"同一币 + 同方向 + 时间窗 ≤ 2 秒"聚类成一次操作；落表时加唯一键去重。

### 🟠 P4 事件延迟变大 → 跟单入场价变差
- **现象**：alphafox 是 15 秒轮询的**实时持仓快照**；币安 `order-history` 只有**已成交**记录，且拉取节奏受"重算窗口 + 限流"约束（若真要 ≥20 秒，反应速度慢 1 倍以上）。
- **影响**：交易员开仓 → 我们发现 → 实盘下市价单，中间的价差就是滑点；趋势/滚仓风格在高波动时更明显。
- **对策**：若做信号源切换，需重新评估 `ALPHAFOX_POLL_SECONDS` 与滑点参数；或维持"alphafox 做信号、币安做校验"的分工。

### 🟠 P5 交易员池子变了
- alphafox 池 = 聚合（币安 + Hyperliquid 等）；币安只给**币安跟单平台的带单员**。
- 现在默认跟单的两个：`bn-5075281354358777856`（熬鹰资本，币安 ✓ 有 order-history）、`hl-0x354d...`（链上，币安 ✗ 完全没有）。
- **对策**：换源=换池子，需要"交易员发现"（`query-list` 排行榜 + `nickname` 搜索）替代 `/api/radar/traders`；HL 类交易员只能继续用 alphafox。

### 🟡 P6 私域组合的"当前持仓"仍然拿不到
- 实测：熬鹰资本 `lead-data/positions` → 0 行（公域才有 759 行）。
- **对策**：私域只能靠 order-history 重建（= P1 的窗口问题）；或验证下面这条线索。
- **未验证线索**：`GET /bapi/futures/v1/friendly/future/spot-copy-trade/common/spot-futures-last-lead` 返回 `futuresPublicLPId / futuresPrivateLPId / futuresSignalLPId`（公域/私域/信号三类 ID）→ **私域持仓也许要用 private LP id 查**。建议一次性 2~3 个请求验证（隔 20 秒）。

### 🟡 P7 未文档化接口会变
- 实测踩过：`public/.../home-page/query-list` → 404（现用 `friendly/...`）；`lead-portfolio/positions` → 404；`order-history` 用 GET → 400（必须 POST）。
- **对策**：所有调用包一层"路径/参数失效检测"（404/400 时报警而不是静默返回空），并保留 alphafox 作为回退源。

### 🟡 P8 字段口径差异（映射层细节）
| alphafox | 币安 | 处理 |
|---|---|---|
| `positions[].absQty`（绝对值） | `positionAmount`（带符号；HEDGE 模式 positionSide=LONG/SHORT） | 按符号 + positionSide 归一 |
| `positions[].leverage` | `order-history` 无 | 纸盘用固定杠杆 |
| `positions[].estimatedLiqPrice` | 无 | 该列显示"—" |
| `positions[].markPrice/unrealizedPnl` | 无 | 用**系统已有** `market.get_mark_price()` 补算 |
| `latestOrders[].canonicalAction` | 无 | 按 side+positionSide（+totalPnl≠0）推 |
| `latestOrders[].notional` | 无 | `executedQty × avgPrice` |
| `latestOrders[].realizedPnl` | `totalPnl` | 平仓腿才有值（开仓腿为 0） |
| `marginBalance` | 无 | k_qty 失去基准（见 3.4） |
| `dna.profile` | `detail.description` | ✓ |
| `dna.profitSharingRate` | 未在实测字段中出现 | 【未验证】 |
| `symbol_stats[]` | 无 | 从 order-history 按 symbol 聚合 |
| `closedPositions[].lifecycleId` 等 | 无等价 | 自建生命周期（重放开平腿） |

---

## 五、实现程度与分期建议

| 阶段 | 内容 | 改动面 | 风险 | 建议 |
|---|---|---|---|---|
| **① 只读旁路** | 拉指定带单员的 order-history，落本地新表，页面新增"币安流水"展示（**不碰跟单链路**） | 新增 1 个 fetch + 1 张表 + 1 个页面区块 | 低 | ✅ 可先做，用来长期观察数据质量 |
| **② 四 TAG 双源** | 成交流水 / 交易点位 / 交易分析 三块改用币安数据装配（**字段同构，前端零改动**）；持有仓位仍用 alphafox | `web.py::_af_build_payload` 装配层 + 新 fetch + 聚合函数 | 中低 | ✅ 推荐，收益最大风险最小 |
| **③ 持仓重建 + 交叉校验** | 用币安 order-history 重建持仓（每轮最近 N 页重算 + 去重 + 降级），与 alphafox 快照**对账**；不一致报警/暂停该交易员 | 新重建器 + 对账报表 | 中 | ✅ 推荐（这才能真正信任币安数据） |
| **④ 完全换源（含信号链路）** | `process()` 输入从"alphafox 快照"换成"币安重建的持仓"；纸盘改固定名义/固定杠杆（去掉 marginBalance 依赖） | `alphafox_trade.process` 输入模型 + 纸盘 k_qty + 全部测试输入 | **高**（P1/P2/P4 全中，需重做 187+185 条测试的输入） | ⏸ 除非接受"固定名义口径 + 反应变慢" |

**推荐路线**：① → ② → ③ 先做（1~2 天，风险可控、收益是"更长历史 + 真实成交 + 交叉校验"）；④ 等 ②③ 跑一段、数据质量有统计之后再评估。

---

## 六、无论如何都必须加的三道护栏（与"换不换源"无关）

1. **空响应可信度判据**：`total == null`（或字段缺失）→ 本轮数据不可信 → 不动仓位、不推进基线、打日志（对齐现有 `_DataUnavailable` / 快照降级语义）。
2. **窗口重算而非增量累加**：每轮用最近 N 页**重新计算**持仓，中间漏采可自愈。
3. **交叉校验（双源时）**：币安重建持仓 vs alphafox 快照，差异超阈值 → 只报警不动作（避免"用不可信数据去平真仓"）。

---

## 七、附：实测证据索引（本轮全部来自服务器真实调用）

| 结论 | 证据 |
|---|---|
| order-history 私域可用 | 熬鹰资本 `total=47`，翻 3 页 10/10/10，`pageSize=50`→37 条 |
| order-history 公域可用 | BTC拖拉机返回 15 条 |
| positions 仅公域 | 公域 759 行 / 私域 0 行 |
| 流水→持仓成立 | 两笔开空加权 = alphafox 持仓（1111.578 @ 2603.4137） |
| trade-history 限流的形态 | 秒级连发 → `200 + data:[] + total:null`；20~30 秒间隔恢复 |
| order-history 限流情况 | **未知**（仅在 22 秒间隔下 5 连发成功，未做连发测试） |
| 页面接口路径会变 | `public/.../query-list`、`lead-portfolio/positions` 均 404；`order-history` 必须 POST |
