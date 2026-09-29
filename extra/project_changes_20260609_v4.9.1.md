---
name: v4-9-1-changes-20260609
description: v4.9.1 CG/GMGN 全链路重构 + 设置页面分类整理
metadata:
  type: project
---

# v4.9.1 改版记录 2026-06-09

## CG 搜索重构 (market.py)
- 精确 symbol 匹配，不取兜底，避免搜索歧义（"D"→USDT、"S"→Tether）
- 搜索/元数据失败不缓存 None，下轮重试（修复 CG 限流导致 1h 无数据）
- URL encoding 修复中文代币名
- `""` key 识别原生币，不误取跨链包装版合约（BNB 不再取 Eth WBNB）
- `asset_platform_id` 判代币原生链，不拍脑袋用固定优先级
- 市值与链信息解耦：原生币保留市值但跳过 GMGN

## GMGN 链映射 (market.py)
- 删除 5 个无效映射（arb/polygon/avax/op/sui），只保留 GMGN CLI 支持的链
- 不支持链返回 None，不启动 CLI 子进程
- _gmgn_chain() 改为 str|None 返回类型

## 设置页面 (web.py)
- 6 panel → 3 panel：系统参数 / 策略配置 / 系统操作
- 每策略参数聚合（开关+金额+间隔+专有参数）

## KOL (kol_agent.py)
- LLM 持仓 TOP5 → TOP10

**Why:** CG 搜索歧义和限流缓存导致大量代币获取不到链上数据，链映射虚增导致无效 GMGN CLI 调用
**How to apply:** git pull → restart worker.py + web.py
