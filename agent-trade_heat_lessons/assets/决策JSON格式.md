# 决策 JSON 格式

保存为 `/tmp/agent_decisions.json`，然后执行 `python3 scripts/write_decisions.py --decisions /tmp/agent_decisions.json`。

## 开仓

Agent 只决定开哪个币、什么档位。入场价、止损止盈由系统用实时价格计算。

```json
{
  "market_read": "对当前市场环境的一句话判断",
  "decisions": [
    {
      "action": "open_long",
      "token": "FET",
      "tier": "full",
      "reason": "详细决策理由",
      "source_round": 61,
      "social_score": 12.5,
      "mentions": 3,
      "dimension_data": "JSON.stringify(候选对象的全部 30 个字段)，一个不漏，系统自动翻译中文",
      "market_overview": "BTC走势 + 时段",
      "lesson_checked": "查了哪些lessons，是否命中"
    }
  ]
}
```

## 必填字段

| 字段 | 说明 |
|------|------|
| action | `"open_long"` |
| token | 币种代号 |
| tier | `"full"` / `"half"` / `"quarter"` |
| reason | **必填**，详细决策理由 |
| source_round | 从提取数据的 `latest_round` 取 |
| social_score | 从对应候选的 `social_score` 取 |
| mentions | 从对应候选的 `mentions` 取 |
| dimension_data | `JSON.stringify(候选对象的全部 30 个字段)`，一个不漏。系统入库时自动翻译中文 |
| market_overview | `btc` + `fear_greed` + `session` 组合 |
| lesson_checked | 查了哪些 lessons，是否命中 |

以下字段由系统填，**Agent 不需要写**：entry_price, stop_loss, tp1_price, tp2_price。

## 空决策

不开仓时 decisions 留空数组。**不要**把上面的示例行当成真实数据写进去。

```json
{"market_read": "...", "decisions": []}
```
