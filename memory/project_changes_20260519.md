---
name: project-changes-20260519
description: v4.1 策略隔离、双Agent面板、导航统一、11处Bug修复
metadata:
  type: project
---

## v4.1 策略数据全链路隔离 + 双Agent并行 + Web修复

**Why:** 两套Agent数据源（agent_candidates / token_heat_history）需要完全独立的账户、持仓、教训、日志，同时在同一面板上展示。

### 策略隔离
- trade_positions / lessons / pending_decisions 加 strategy/source 列
- 4个 extract 脚本全量 SQL 按 strategy 过滤
- write_decisions --source、write_lessons --strategy 参数化
- web.py 所有 Agent API 按 strategy 参数隔离
- auto_trader source→strategy 映射（agent_candidates→agent, token_heat_history→heat_agent）

### 双Agent并行
- 新建 agent-trade_heat/ agent-review_heat/ 目录
- HEAT_AGENT_HTML = AGENT_HTML.replace() 链生成热度面板
- 独立临时文件（_heat 后缀），互不覆盖
- 不同数据源：agent_candidates vs token_heat_history+market_snapshots

### 导航栏统一（5页）
- 所有 .nav-bar a 统一 24px/13px
- 所有 .nav-dropdown > a 加 display:inline-block 对齐
- 策略页补 dropdown 菜单
- Agent 页补 nav-dropdown-content CSS

### 关键Bug修复（11处）
1. today_journal SQL 泄露跨策略数据：4个 extract 全部改为 tp.strategy 直接过滤
2. overview API 缓存 key 不区分策略 → agent_overview_{strategy}
3. pending/rejected 计数不过滤 source → 加 WHERE source=?
4. _max_dd_cache 全局共享 → per-strategy dict
5. HEAT_AGENT_HTML overview 替换链：缺引号 → 多括号 → 去字符串拼接（最终匹配 URL 字符串不含 fetch 闭合括号）
6. TradingSettingsBody 缺 strategy_initial_heat_agent 字段
7. Agent 页 CSS 缺 nav-dropdown-content 规则
8. 策略页缺 dropdown 菜单和 nav-dropdown CSS

### 部署注意
- 服务器路径：/root/binance-monitor/bsm-agent/
- Hermes skill 注册：agent-trade, agent-trade-heat, agent-review, agent-review-heat
- 重启：manage_processes.py restart
- 热度Agent需要 Hermes cron job ID 配置
