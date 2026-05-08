# agent_lessons 字段说明

提取脚本输出的 `agent_lessons` 是复盘 skill 写的历史教训。开仓前必须查。

每条的字段：

  .id             — lessons 表 ID
  .token          — 币种，'*' 表示全局教训（适用于所有币种）
  .direction      — long / short
  .entry_price    — 入场价
  .exit_price     — 出场价
  .pnl_pct        — 盈亏百分比（负数 = 亏损）
  .signal_error   — 入场时信号层面哪里判断错了
  .what_missed    — 复盘时发现遗漏了什么关键信号
  .root_cause     — 根本原因一句话
  .lesson         — 具体教训内容
  .rule_update    — 由此衍生的规则，如 "4h涨超25%不开多"
  .severity       — critical / warning / medium
  .learned        — 0=仍适用, 1=已被覆盖

## 其他字段速查

### positions 对象字段
```python
p["token"]        # "***" 掩码
p["side"]         # "LONG" / "SHORT"
p["entry_price"]  # 入场价
p["current_price"]  # 当前价
p["stop_loss_price"]  # 止损价
p["tp1_price"]    # 止盈1
p["tp2_price"]    # 止盈2
p["pnl_pct"]      # 盈亏百分比
p["margin_amount"]  # 保证金
p["highest_price"]  # 持仓期间最高价
p["status"]       # "OPEN"
```

### account 对象字段
```python
acct["equity"]      # 总权益
acct["available"]   # 可用余额
acct["locked"]      # 锁定保证金
acct["initial"]     # 初始资金
acct["realized"]    # 已实现盈亏
acct["unrealized"]  # 未实现盈亏
acct["trades_today"]  # 今日交易笔数
acct["open_count"]  # 当前持仓数
```

开仓前，看候选币的 token 有没有对应教训，再看有没有全局教训（token='*'）。

重点关注 `rule_update`——这是复盘时提炼的规则，直接告诉你"什么情况下不要开仓"。如果当前候选的特征匹配某条 rule_update 描述的场景，要么不开，要么在 reason 里说明为什么认为这次不同。
