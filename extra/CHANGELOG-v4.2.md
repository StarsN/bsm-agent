# v4.2 市场全景 Dashboard + 策略扩展 + KOL 短仓 + 碰撞防护

**Commit**: c135449  
**日期**: 2026-05-23

## 新模块

### 市场全景 Dashboard
- `dashboard.py`: 全新数据聚合模块，全部使用 OKX CLI，零币安调用
- `/dashboard` 页面: Dark Mode 设计，两大模块（风控/异常窗口 + V3 AI Regime）
- 3 个新 API:
  - `/api/risk-metrics` — 流动性、z_depth、1h/3h大局、数据年龄、恐惧贪婪
  - `/api/macro-events` — 美国宏观事件排期、风险评级（关键词匹配FOMC/CPI/NFP等）
  - `/api/ai-regime` — DeepSeek LLM 市场研判(32min间隔)、24h regime时间线、证据对比
- AI研判 32min 间隔 + offset=5min，与4个Agent LLM调用永不撞车
- 13处Bug修复: regime大小写、时间比较logic、JSON解析、orderbook列表解包、宏观字段名等

### 系统设置页修复
- grid布局对齐 + select/input高度统一
- 导航栏补全市场全景链接

## 新策略

- **agent_no_lessons**: 无教训对照组，独立 nl_candidates 表 + SKILL
- **agent_heat_lessons**: 热度榜单有教训版，独立 token_heat_history_lessons 数据源 + review SKILL
- **KOL Agent**: DeepSeek LLM 市场分析，独立触发(8min)，支持做多/做空

## 核心引擎改造

- 做空支持: trade_logic.py open_paper_position() side=SHORT，stop/trail/PnL计算适配
- lowest_price 字段追踪（做空止盈用）
- 策略级数据隔离: UNIQUE(token, side, strategy) 索引
- kol_llm_logs: LLM调用日志表，DeepSeek/NVIDIA双provider
- 碰撞防护: 所有Agent周期性偏移调度，GCD互质保证不重叠

## 安全修复

- 导航栏全部页面统一，不再遗漏链接

## 待做 (P0-P3)

- P0: vol/OI比值、恐惧贪婪入库
- P1: OKX费率历史、Basis基差、OKX费率对比、上线时间
- P2: token类型/链静态配置
- P3: 爆仓WebSocket数据流
