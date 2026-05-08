# 候选币市场数据字段

提取脚本输出的每个候选对象包含以下字段。所有数值字段都可能为 `null`。

## 价格变化（%）

```
price       — 合约标记价
15m         — 15分钟价格变化
1h          — 1小时价格变化
4h          — 4小时价格变化
24h         — 24小时价格变化（合约K线计算）
chg_48h     — 48小时价格变化
```

## OI 变化（%）

```
oi_15m      — OI 15分钟变化率
oi_1h       — OI 1小时变化率
oi_4h       — OI 4小时变化率
oi_48h      — OI 48小时变化率
oi_usd      — 未平仓合约金额（美元）
```

## 资金费率与多空比

```
funding     — 资金费率 (%/8h)
lsr         — 散户多空比（>1 = 多头多）
top_lsr     — 大户多空比（可能为 NULL）
```

## 主动买卖

```
taker       — 主动买卖比（近20m）
taker_pct   — 主动买入占比 %
taker_trend — Taker趋势（正=买盘增强，负=衰退）
```

## 盘口流动性

```
spread      — 盘口买卖价差 %
depth_bid   — ±1% 买盘深度（美元）
depth_ask   — ±1% 卖盘深度（美元）
imbalance   — 盘口失衡度（正=买盘多）
vol_24h     — 24小时成交额（USDT）
```

## 分析结果

```
verdict     — 信号判定（✅看起来健康 / 🎯值得留意 / ⚠️过热预警 / 📉信号偏弱 / ⚪中性 / 数据不足）
direction   — 走向（↑偏多 / ↓偏空 / 震荡 / 不明）
tags        — 信号标签列表（如 ["funding:极高", "taker:买盘强"]）
notes       — 人类可读解读
oi_divergence — OI背离检测 {type, direction, oi_pct, price_pct, note} 或 None
```

## 元数据

```
token       — 币种
social_score— 社交热度分
mentions    — 提及次数
age         — 上币时长，"0d10h" 格式。根据 15m K 线实际根数×15 分估算。None=无数据
```
