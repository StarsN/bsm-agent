# market_snapshots.snapshot JSON 字段

所有字段都可能为 None。默认 `heavy=True`（每轮全量），top_lsr/oi_4h/oi_48h/chg_48h 都会拉到。

```
mark_price           — 合约标记价
funding_rate_pct     — 资金费率 (%/8h)
oi_usd               — 未平仓合约金额
oi_change_15m_pct    — OI 15分钟变化率
oi_change_1h_pct     — OI 1小时变化率
oi_change_4h_pct     — OI 4小时变化率
oi_change_48h_pct    — OI 48小时变化率
change_15m_pct       — 15分钟价格变化
change_1h_pct        — 1小时价格变化
change_4h_pct        — 4小时价格变化
change_24h_pct       — 24小时价格变化（合约K线计算）
change_48h_pct       — 48小时价格变化
volume_24h_usd       — 24小时成交额（USDT）
long_short_ratio     — 散户多空比（>1 = 多头多）
top_trader_ls_ratio  — 大户多空比（可能为 NULL）
taker_buy_sell_ratio — 主动买卖比（近20m）
taker_buy_pct        — 主动买入占比 %
taker_trend_pct      — Taker趋势（正=买盘增强，负=衰退）
bid_ask_spread_pct   — 盘口买卖价差
depth_bid_1pct_usd   — ±1% 买盘深度（美元）
depth_ask_1pct_usd   — ±1% 卖盘深度（美元）
depth_imbalance_pct  — 盘口失衡度（正=买盘多）
```

## analysis JSON 字段

```
score               — 综合分 0-100（仅参考）
verdict             — 标签（✅看起来健康 / 🎯值得留意 / ⚠️过热预警 / 📉信号偏弱 / ⚪中性 / 数据不足）
direction           — 走向：↑偏多 / ↓偏空 / 震荡 / 不明
tags                — 信号标签列表（如 ["funding:极高", "taker:买盘强"]）
notes               — 人类可读解读
oi_divergence       — OI背离检测 {type, direction, oi_pct, price_pct, note} 或 None
```

## extraction 脚本的字段映射

脚本已将这些命名为短名，供分析使用：
- `price` → mark_price
- `15m` / `1h` / `4h` / `24h` → change_X_pct
- `oi_15m` / `oi_1h` / `oi_4h` / `oi_48h` → oi_change_X_pct
- `funding` → funding_rate_pct
- `taker` → taker_buy_sell_ratio
- 等等

详见 `scripts/extract_market_data.py` 的 candidates 组装部分。
