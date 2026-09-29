# KOL 页面重构 — 技术方案 v4.4

## 概述

面向量化交易系统的 "AI 判断流详细版" 面板。左侧 K 线对照，右侧 Agent 思考过程与关键点位时间线。

核心改动：System Prompt 输出结构扩展 4 个新字段，DB 表加列，前端从单卡片改为 4 区卡片（元数据/价格网格/逻辑推演/证据标签）。

---

## 一、System Prompt 新 JSON 格式 (kol_agent.py)

### 旧格式（11 个顶层字段）
```
token, trend, timeline, price_levels, position_analysis, timing, risk_control, direction, confidence, reason
```

### 新格式（变更对照）

| 旧字段 | 新字段 | 说明 |
|--------|--------|------|
| position_analysis | — | 合并到 summary |
| reason | — | 合并到 summary |
| — | summary | 新增，100-200 字盘面摘要 |
| timing | reasoning.sj | 缩进到 reasoning 子对象 |
| risk_control | reasoning.fk | 缩进到 reasoning 子对象 |
| — | reasoning.wz | 位置分析 |
| direction | direction | 不变 (long/short/none) |
| confidence | confidence | 不变 (0-100) |
| — | **action** | 新增，格式 "操作类型 / 强度分" |
| — | **status** | 新增，ENTER / WAIT |
| — | **context_tag** | 新增，盘面场景标签 |
| — | **evidence_tags** | 新增，证据链数组 |

### 字段缩略名设计
- `wz` = 位置（当前价在支撑/阻力中的位置）
- `sj` = 时机（何时入场/等待什么条件）
- `fk` = 风控（止损/止盈设置逻辑）
- 目的：减少 LLM token 消耗，每分析节省 ~20 tokens

### LLM 输出 JSON 结构
```json
{
  "analyses": [{
    "token": "FLUID",
    "trend": "偏空",
    "price_levels": {
      "current": 1.602, "support": 1.597, "resistance": 1.623,
      "entry": 1.602, "stop_loss": 1.631, "take_profit": 1.555
    },
    "summary": "1.82 高点后回落，OI 横盘而 taker 偏卖，资金费率转正但 Premium 为负",
    "reasoning": {
      "wz": "当前价压在 1.60 支撑带，反弹弱但未有效跌破",
      "sj": "等 15m 收破 1.597，或 1.61 反抽失败",
      "fk": "止损放 1.63 上方防反抽，TP1 看前低回踩区，TP2 看 1.49"
    },
    "action": "出货 / 71",
    "status": "WAIT",
    "context_tag": "range_low · support_holding",
    "evidence_tags": ["taker 0.74", "24h -5.5%", "BTC横盘", "支撑未破"],
    "direction": "short",
    "confidence": 75
  }]
}
```

### 字段详解
- **action**: "操作类型 / 强度分"。建仓/加仓/出货/观望 + 0-100 整数
- **status**: ENTER=现在可入场, WAIT=时机未到。direction=none 时必填 WAIT
- **context_tag**: 用 " · " 连接，例: "range_low · support_holding", "near_resistance", "breakout_confirmed"
- **evidence_tags**: 字符串数组，每条 5-15 字，只引用 prompt 中的具体数据。direction=none 时填 ["无明显信号"]
- **summary**: 盘面综合摘要 100-200 字
- **reasoning.wz/sj/fk**: 位置/时机/风控三段式逻辑

---

## 二、数据库改动 (storage.py)

### 2.1 DDL — 加 4 列
```sql
ALTER TABLE kol_analyses ADD COLUMN action         TEXT;
ALTER TABLE kol_analyses ADD COLUMN status         TEXT;
ALTER TABLE kol_analyses ADD COLUMN context_tag    TEXT;
ALTER TABLE kol_analyses ADD COLUMN evidence_tags  TEXT;
```

写在 `_migrate()` 函数内，放在已有的 kol_analyses 迁移段（约 storage.py:494-501 附近），用 `PRAGMA table_info` 检测列是否存在后再 ALTER。

### 2.2 kol_analysis_insert — 旧 11 列 → 新 15 列
```sql
INSERT INTO kol_analyses
  (token, trend, timeline, price_levels, position_analysis,
   timing, risk_control, direction, confidence, reason, llm_log_id,
   action, status, context_tag, evidence_tags)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
```

- evidence_tags 存为 JSON 字符串（`json.dumps`）
- action/status/context_tag 存为纯文本

### 2.3 kol_analyses_latest — SELECT 加新列
```sql
SELECT id, token, trend, price_levels, position_analysis, timing,
       risk_control, direction, confidence, reason,
       action, status, context_tag, evidence_tags,
       created_at
FROM kol_analyses
WHERE created_at >= datetime('now', '-12 hours')
ORDER BY id DESC
```

- evidence_tags 返回时 `json.loads` 还原为数组
- 旧列保留不删，兼容已有数据

---

## 三、API 重构 (web.py)

### /api/kol/analyses?symbol=FLUIDUSDT

#### 响应格式
```json
{
  "symbol": "FLUIDUSDT",
  "meta": {
    "time_window": "最近 12 小时",
    "max_items": 8,
    "total_count": 3
  },
  "judgments": [
    {
      "id": 1024,
      "timestamp": "18:03:57",
      "direction": "SHORT",
      "action": "出货 / 71",
      "status": "WAIT",
      "context_tag": "range_low · support_holding",
      "summary": "1.82 高点后回落，OI 横盘而 taker 偏卖...",
      "grid_data": {
        "current_price": "1.602",
        "support": "1.597",
        "resistance": "1.623",
        "entry": "1.602",
        "sl": "1.631",
        "tp": "1.555 / 1.49"
      },
      "reasoning": {
        "位置": "当前价压在 1.60 支撑带，反弹弱但未有效跌破",
        "时机": "等 15m 收破 1.597，或 1.61 反抽失败",
        "风控": "止损放 1.63 上方防反抽..."
      },
      "evidence_tags": ["taker 0.74", "24h -5.5%", "BTC横盘"]
    }
  ]
}
```

### 数据映射规则

API 字段 | DB 来源 | 处理逻辑
---------|---------|---------
id | id | 直接透传
timestamp | created_at | UTC+8, HH:MM:SS 格式
direction | direction | .upper()
action | action | 直接透传
status | status | 直接透传
context_tag | context_tag | 直接透传
summary | position_analysis 或 reason | 新数据用旧字段做 fallback（取第一个非空的）
grid_data.current_price | price_levels.current | _fmt_price()
grid_data.support | price_levels.support | 同上
grid_data.resistance | price_levels.resistance | 同上
grid_data.entry | price_levels.entry | 同上
grid_data.sl | price_levels.stop_loss | 同上
grid_data.tp | price_levels.take_profit | 同上
reasoning.位置 | risk_control 或 reasoning.wz | fallback: 旧 risk_control → 位置
reasoning.时机 | timing 或 reasoning.sj | fallback: 旧 timing → 时机
reasoning.风控 | — 或 reasoning.fk | 新字段优先
evidence_tags | evidence_tags | json.loads

### Fallback 逻辑
因为旧数据没有 action/status/context_tag/evidence_tags，API 需兼容：
- action 为空 → 用 direction + confidence 拼接，如 "LONG / 75"
- status 为空 → 不显示 badge
- context_tag 为空 → 不显示
- evidence_tags 为空 → 不显示该区
- reasoning 为空 → 用旧 timing/risk_control 拼凑
- summary → 取 position_analysis（旧数据）或 summary 对应的旧字段

---

## 四、前端重构 (web.py KOL_AGENT_HTML)

### 4.1 整体布局

```
┌────────────────────────────────────────────────┐
│  Header: 标题 + 币种选择器                     │
├──────────────────────┬─────────────────────────┤
│  K线图 (60%)         │  时间线卡片列表 (40%)   │
│  TradingView iframe  │  可垂直滚动             │
│                      │                         │
│  [15m] [1H] [4H]    │  ┌─────────────────┐   │
│                      │  │ 卡片 1: 4区结构  │   │
│  ┌────────────────┐  │  ├─────────────────┤   │
│  │  K 线蜡烛图    │  │  │ 卡片 2          │   │
│  │                │  │  ├─────────────────┤   │
│  │                │  │  │ 卡片 3          │   │
│  ├────────────────┤  │  └─────────────────┘   │
│  │  成交量        │  │                         │
│  └────────────────┘  │                         │
└──────────────────────┴─────────────────────────┘
```

### 4.2 组件拆解

#### Header
- 主标题 "AI 判断流详细版"（亮白）
- 副标题说明（暗灰）
- 币种药丸按钮组，选中态深蓝底+白字，未选中透明底+暗边框
- 数据源：从 /api/kol/analyses?symbol=ALL 获取有数据的币种列表

#### 左侧 K 线
- TradingView 全功能 Widget iframe
- URL 模板: `https://s.tradingview.com/widgetembed/?symbol=BINANCE:{symbol}&interval={interval}&theme=dark`
- 周期切换: 15m / 1H / 4H，默认 15m
- 切换币种自动更新 symbol

#### 右侧时间线卡片 — 四区结构

```
┌─────────────────────────────────────────┐
│ 18:03:57  SHORT  出货/71  [WAIT]        │  ← 一区：元数据
│              range_low · support_holding │
│ 1.82高点后回落，OI横盘而taker偏卖...    │  ← 摘要
├─────────────────────────────────────────┤
│ 现价   │ 支撑  │ 阻力  │ 入场 │ SL  │ TP │  ← 二区：价格网格
│ 1.602  │1.597  │1.623  │1.602 │1.631│... │
├─────────────────────────────────────────┤
│ 位置: 当前价压在1.60支撑带...           │  ← 三区：逻辑推演
│ 时机: 等15m收破1.597...                 │
│ 风控: 止损放1.63上方...                │
├─────────────────────────────────────────┤
│ [taker 0.74] [24h -5.5%] [BTC横盘]     │  ← 四区：证据标签
└─────────────────────────────────────────┘
```

### 4.3 样式规范
- 深色模式 (#1a1a2e 底色, #16213e 卡片底)
- 方向颜色: SHORT=#ef4444, LONG=#10b981
- 状态 Badge: ENTER=绿底白字, WAIT=黄底黑字
- 文本层级: 主文字 #e2e8f0, 次级 #94a3b8, 三级 #64748b
- 价格网格用深色条纹底 (#0f0f23)
- 证据标签: 圆角药丸 border, 浅灰底或根据内容微着色

### 4.4 JS 交互逻辑
```
1. loadSymbols() → GET /api/kol/analyses → 提取币种列表 → 渲染 Header 药丸
2. onCoinClick(symbol) → loadTimeline(symbol) + updateChart(symbol)
3. loadTimeline(symbol) → GET /api/kol/analyses?symbol=X → 渲染卡片列表
4. auto-refresh: setInterval(loadTimeline, 60000) 每 60s 刷新
```

---

## 五、实施顺序

1. ✅ kol_agent.py — System Prompt 新 JSON 格式
2. 🔲 storage.py — DDL 迁移（ALTER TABLE ADD COLUMN ×4）
3. 🔲 storage.py — kol_analysis_insert 15 列更新
4. 🔲 storage.py — kol_analyses_latest SELECT 更新
5. 🔲 web.py — /api/kol/analyses 重构（向后兼容旧数据）
6. 🔲 web.py — KOL_AGENT_HTML 前端 4 区卡片

步骤 2-4 可在一次 storage.py 编辑中完成，步骤 5-6 需分别处理。

---

## 六、向后兼容

- 旧 11 列全部保留，不删不改
- 新 4 列允许 NULL，旧数据自动为 NULL
- API fallback 逻辑覆盖新旧两种数据格式
- 前端 conditionally render：四区中某区数据为空则不显示该区
