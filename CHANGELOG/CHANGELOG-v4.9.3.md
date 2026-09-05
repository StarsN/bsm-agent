# v4.9.3 CHANGELOG

## 抓取优化（解决 420s 超时 + Target crashed 问题）

### 核心改动

**scraper.py：**
- page.goto 初始+定期刷新两处都加 asyncio 90s 硬兜底，防 Playwright 超时失效导致 while 循环 hang
- burst/idle 从固定 50/45 改为随机 35-45/60-70，比例约 38:62
- context.close() 加 10s 超时保护，防浏览器已死时无限等待
- PAGE_GOTO_TIMEOUT 从 90s → 60s → 30s，不重试
- 初始 goto 从 3 次重试改为单次

**worker.py：**
- CG/GMGN 调用从快照生成移除，仅在 KOL LLM 请求时按需获取
- 每轮结束后增加 45-75s 轮间休息，释放 Chromium 内存
- 超时+异常轮次也休息
- OKX 聪明钱+市值保留

### 数据源迁移

**kol_agent.py：**
- 新增 `_enrich_with_onchain_data()`，CG→chain/contract/fdv + GMGN→holder/smart money
- gmgn_* 和 sm_* 字段 0 值不跳过（`if x` → `if x is not None`）

**web.py：**
- 前端持仓分布卡片、GMGN 聪明钱卡片删除
- _build_data_blob / _build_kol_data_blob 移除对应字段
- _refresh_watchlist_tokens 移除 CG/GMGN 调用

### 问题修复

- 761 次 Chromium trap int3：2GB 服务器内存不足+VM CPU 指令兼容问题（非代码 bug）
- KOL LLM prompt 缺失持仓数据：AVE_API_KEY 为空→填入即可恢复
- 链上聪明钱 0 值被跳过：`is not None` 修复
