# 链上监控黑名单审计报告

Date: 2026-06-27

## 已修复

- **Step A 候选过滤** (onchain_scanner.py:77): 新增黑名单排除，OKX API 调用前过滤

## 未修复的高危缺口

### GAP 1: Stage 2 没有黑名单检查 (onchain_scanner.py:347-414)
Stage 2 加载观察池所有代币，调 LP/Gas/CEX 的 OKX API，算置信度，晋升 actionable。全程不查黑名单。如果黑名单代币还在池里，可走到 `_open_onchain_position` 自动开仓。

### GAP 2: `_open_onchain_position` 没有黑名单检查 (onchain_scanner.py:582-620)
自动开仓入口，不查黑名单。最后防线缺失。

### GAP 3: Step C2 池刷新没有黑名单检查 (onchain_scanner.py:302-331)
黑名单已加载（271行）但 Step C2 循环未使用。极端并发场景下可能把刚被 archive 删除的黑名单代币 upsert 回池里。

## 未修复的中危缺口

### GAP 4: Stage 3 集群分析没有黑名单检查 (onchain_scanner.py:691-865)
黑名单代币留在池里会走完整集群分析，浪费 OKX API 配额。

### GAP 5: Step A 没有检查 onchain_removed (onchain_scanner.py:72-84)
被移除的代币如果没有同时加入黑名单，下一轮扫描可能重新进入管道。

### GAP 6: 黑名单移除不恢复代币到池 (web.py:1140-1147)
解除黑名单只删 onchain_blacklist 记录，不恢复 onchain_pool。代币只能等下一轮扫描重新发现。

### GAP 7: onchain_removed 没有移除 API (web.py:1152-1171)
有 GET 和 POST add，没有 POST remove。一旦加入 removed 无法通过 API 删除。

### GAP 8: onchain_removed add 不检查活跃仓位 (web.py:1163-1171)
移除代币时不像手动平仓那样调用 trade_logic.manual_close_on_unwatch，活跃仓位不会被关闭。

## 攻击链

三个高危缺口形成完整攻击链：黑名单代币留在观察池 → Stage 2 晋升 actionable → _open_onchain_position 自动开仓
