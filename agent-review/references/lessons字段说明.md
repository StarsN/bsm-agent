# lessons 表字段

## 必填

| 字段 | 说明 | 写法 |
|------|------|------|
| token | 币种，或 `*` 表示全局 | `"FET"` / `"*"` |
| lesson | 具体教训 | 直接可操作，不是泛泛而谈 |
| signal_error | 信号层面哪里判断错了 | 具体的，不是"看错了" |
| what_missed | 遗漏了什么关键信号 | 具体的，不是"没注意" |
| root_cause | 根本原因一句话 | 追本溯源 |
| rule_update | 由此衍生的规则 | 下次可直接套用 |
| severity | critical / warning / medium | 按严重程度 |

## 亏损 vs 盈利的写法

亏损的 lessons 和盈利的 lessons 侧重点不同：

### 亏损单

重点在"为什么错了"。signal_error 和 what_missed 是关键。

```
lesson:     "OI涨+价格横盘+funding>0.03% 时实际是派发而非吸筹"
signal_error: "误读OI增长为利多，未意识到taker卖盘主导"
what_missed: "没注意到taker_buy_sell_ratio=0.58，主动卖盘远超买盘"
root_cause: "只看OI方向没看taker构成，被表面数字误导"
rule_update: "OI 1h >5% 但 taker <1.0 → OI虚假繁荣，不开多"
severity:    warning 或 critical
```

### 盈利单

重点在"什么判断对了，下次能不能复用"。

```
lesson:     "大户/散户分歧 + OI多周期同向 = 可靠的开仓信号"
signal_error: None 或空（盈利单没有信号错误）
what_missed: None 或"大环境在涨，任何做多都可能赚，需要区分 Alpha 和 Beta"
root_cause: "正确识别了聪明钱信号"
rule_update: "散户LSR<0.7 + 大户LSR>1.5 + OI 15m/1h/4h同增 → 优先开仓"
severity:    medium
```

盈利单写 lesson 的价值在于：把"感觉这次不错"变成"这条规则经历史验证过"。未来开仓前查教训时，正面规则和负面规则都在视野里。

## 可选

| 字段 | 说明 |
|------|------|
| order_id | 关联 trade_positions.id |
| direction | long / short |
| entry_price / exit_price | 入场/出场价 |
| pnl_pct | 盈亏百分比 |
| market_snapshot | 入场时行情摘要 |
| macro_context | 市场环境（BTC走势、时段） |

## 质量标准

教训必须让未来的自己"看到就能用"。

- ✅ "OI涨+价格横盘+资金费率>0.03% → 派发信号，不开多"
- ❌ "下次注意OI"

- ✅ "误读OI背离为利多：OI涨但taker卖盘主导，实际是空头开仓"
- ❌ "看错了"
