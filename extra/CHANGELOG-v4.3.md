# v4.3 全景字段 + DB优化 + KOL时序 + 策略重置 + 设置补全

**Commit**: 23aae10  
**日期**: 2026-05-26

## 新数据字段（P0-P3 全量落地）

| 优先级 | 字段 | 数据源 | 位置 |
|--------|------|--------|------|
| P0 | vol_oi_ratio | Binance volume/OI 计算 | market.py |
| P0 | 恐惧贪婪 | alternative.me | dashboard.py 面板 |
| P1 | listing_age_days | Binance exchangeInfo onboardDate | market.py |
| P1 | basis_pct | OKX tickers 批量 | dashboard.py+worker.py |
| P1 | okx_funding_pct | OKX funding-rate 线程池 | dashboard.py+worker.py |
| P1 | funding_history | 同上 | dashboard.py+worker.py |
| P2 | sector/chain | token_tags.json 静态配置 | market.py |
| P3 | liq_long/short_usd/count | Binance WS !forceOrder@arr | liquidation.py+worker.py |

- 深度解读卡片从 3 个扩展到 7 个
- KOL Agent 全景数据注入（_build_kol_data_blob + _build_panorama_context）
- 其他 Agent 策略不受影响（数据管道隔离）

## DB 爆炸治理

- kol_llm_logs 存 system_prompt/user_prompt/raw_response（每批1份）
- kol_analyses 删 3 列 + 加 llm_log_id 外键
- 单批存储：434KB → 69KB，降 84%
- 旧数据建议 VACUUM 回收 ~250MB

## KOL 决策时间线修复

- open_paper_position 补 journal_add_open（之前只有手动交易有）
- 时间线 consumed 只跳过有 journal open 的（j_open_tokens 查询）
- 兼容 KOL 旧数据（95 条 consumed 无 journal open，不跳过）

## V3 AI Regime

- 独立 NVIDIA API key (AI_REGIME_API_KEY)，不与 KOL Agent 共用
- 异步 bg 线程：缓存 miss 时秒返"生成中..."，后台拉 LLM
- try/finally 防死锁（bg 线程崩溃后 ai_loading 可复位）
- 错误日志 logs/ai_regime_llm.log

## 设置页补全

- KOL Agent 最低信心分 (kol_agent_min_confidence)
- 日交易次数限制开关 (trading_daily_limit_enabled)
- KOL LLM 服务商切换 (kol_llm_provider)
- AI 研判间隔补保存
- 3 个字段改为直接读 DB（移除 config 中间层）
- 策略独立重置（trade_reset_strategy）
- select 控件对齐（appearance: none 修复 Windows 原生渲染）

## Bug 修复

- KOL loadLessons 误调用 → 决策时间线 panel DOM null
- 时间线 j_open_tokens 查询在 with 块外（conn 已关闭 → 500 错误）
- macro_1h 颜色用错流动性指标 → 改为涨跌方向
- BTC ticker 未解包列表 + 字段名 24h change% 不存在
- 宏观事件列表未按时间排序
