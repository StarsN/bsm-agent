# Changelog

## v4.7.1 — KOL 策略简化 + Agent 采集守卫 (2026-06-04)

- KOL 模板：去全景方向规则，只保留 KOL 框架筛选，LLM 自行结合数据判断方向
- KOL+快照：去 missing_data 字段
- 主 Agent：`agent_trade_enabled` 关闭时跳过数据收集+入库，避免无效 DB 写入

## v4.7 — KOL 策略重构：全景 AI 方向 + 挂单全风控 + K 线摘要 + LLM 反馈 (2026-06-03)

### 全景方向重构（AI 为核心）
- AI 市场研判替代 BTC 成为方向判断核心依据，显式标注 →偏多/偏空/无方向
- 四规则：BTC 与 AI 同向 full、分歧以 AI 为准 half、AI 震荡参考 BTC、BTC 闪崩 >3% 兜底
- 全景数据全中文化：恐惧贪婪映射中文、z_depth→订单簿深度、BTC 标签替成交量 desc
- 双重过滤器（全景方向 + KOL 框架）强制 LLM 按标准筛选，禁止自创逻辑

### 挂单全风控
- open_limit_position 补 5 项账户风控（日亏损熔断/日交易上限/最大持仓/冷却期/板块集中度）
- ATR 止损 + compute_position_size 风险反推仓位，与 open_paper_position 对齐
- 手动挂单豁免软风控（持仓数/冷却期/集中度），硬风控（日亏损/交易上限）永不豁免
- 仅 ENTER + 有效 entry 挂单，去市价单路径
- LLM position_size 字段控制 full/half 仓位档位

### K 线数据优化
- 裸 K 线改结构化摘要：趋势 + 区间 + 位置 + 量能（三层时间框架窗口独立）
- 裸 K 线保留但限制输出根数（1h/4h/日线 = 6/6/7），摘要用全量数据
- 日线扩至 90 根（大级别位置）、1h 扩至 24 根存储
- get_daily_klines >=3 放宽，新币不丢数据

### Prompt 增强
- missing_data 字段：LLM 反馈还缺什么数据，写入 DB 并在 LLM 日志展示
- 聪明钱多头/空头均价（覆盖 LLM 40+ 次"主力 OI 均价"需求）
- KOL 知识改读 TradeSnapshot/_hermes_short_v1.md，清理仓位管理/执行规则等非分析内容
- 快照四步交叉推演法、entry 必填强调、WAIT/ENTER 正确定义

### Bug 修复
- 快照间隔从 DB 独立刷新（之前永远等于 KOL 间隔）
- get_kol_candidates 策略独立时间窗
- analyze_candidates 显式设置 strategy="kol_agent"
- 流动性双重"价差"修复
- KOL 规则段"全景偏空"与第一层 AI 核心逻辑冲突修复

## v4.6 — KOL 快照策略 + 并发 LLM + DB 重试 + 日志分天 (2026-05-30)
- kol_snapshot 策略：与 KOL 完全并行，读 6 位交易员 _KnowledgeSnapshot_short.md
- 独立 System Prompt（三步推演法 + 矩阵交叉推理）
- 独立 API Key、Provider、冷却缓存、触发间隔（offset=4）
- 监控页 /agent-kol-snapshot + K 线页 /kol?strategy=kol_snapshot 数据完全隔离

### 并发 LLM 调用
- analyze_candidates / analyze_candidates_snapshot 改为 ThreadPoolExecutor 并发
- 4 key 同时请求，单轮从 12 分钟降到 3 分钟

### DB 写入重试（5 次递增间隔）
- kol_llm_log_insert / upsert_author / agent_candidates_insert_batch
- realtime_upsert / snapshot_upsert 全部覆盖

### 日志按天分文件
- manage_processes.py：logs/{进程名}/{日期}.log，启动时自动清理 30 天前

### Bug 修复
- system_auto_trade_enabled：storage bool 转换集、allowed 集、defaults 全链路补齐
- KOL/SNAP 监控页 LLM 日志 + K 线数据隔离
- KOL_AGENT_HTML 补 strategy 参数传递
- 爬虫 page.goto 加重试（3 次）

## v4.5 — 挂单全链路修复 + KOL 策略审查 (2026-05-29)

### order_type 字段（市价/挂单区分）
- trade_positions 新增 order_type 列（DEFAULT 'market'），migration PENDING 回填 'limit'
- Agent 监控页（AGENT_HTML + 4个衍生页）当前持仓表加"类型"列
- 策略页（STRATEGIES_HTML）当前持仓 + 平仓表加"类型"列
- 后端 SQL 全部覆盖：SELECT * + /api/strategy/stats 显式列出

### 挂单取消三路径对齐
- 手动取消 (web.py api_limit_order_cancel): 补 pending_decisions → 'expired'
- 取消收藏取消 (trade_logic.py manual_close_on_unwatch): 补 signal_lock_release + pending_decisions → 'expired'
- 自动超时取消 (trade_logic.py update_paper_positions): 已有

### 挂单前端修复
- loadPending() 超时计算从硬编码 600s 改为读取 API settings.limit_order_timeout_seconds
- 修复变量名遮蔽 (d → dd for Date)

### KOL 策略修复
- 冷却缓存 state-before-DB: _kol_token_last_sent 更新移到 conn.commit() 后
- ENTER 路径 pending_decisions 顺序反转: 先 INSERT → 开仓 → 回标 consumed + journal 回填 pending_decision_id
- 决策时间线去重: token 名匹配 → pending_decision_id 精确匹配，修复同 token 旧 journal 误杀新挂单触发
- NL Agent state-before-DB: _nl_last_flush/_nl_running/_nl_round 移到 DB 写入后

### 系统策略开关
- 设置页面新增"系统策略自动开仓"开关
- auto_trader.py: config.SYSTEM_AUTO_TRADE_ENABLED → settings.get("system_auto_trade_enabled")
- 完整链路: Pydantic Model → API 白名单 → HTML → JS load/save → 后端判断

### Bug 修复
- config.py NVIDIA_API_KEYS 数组 9 个 key 缺引号 → 导致全模块 import 失败
- trade_logic.py _build_account_context open_positions 漏 PENDING → 风控计数修复
## v4.4 鈥?KOL 椤甸潰閲嶆瀯 (杩涜涓?

### KOL LLM 杈撳嚭瀛楁鎵╁睍
- System Prompt 閲嶆瀯锛氭柊澧?`action`(鎿嶄綔/寮哄害)銆乣status`(ENTER/WAIT)銆乣context_tag`(鐩橀潰鍦烘櫙)銆乣evidence_tags`(璇佹嵁閾? 鍥涗釜瀛楁
- `position_analysis` + `reason` 鍚堝苟涓?`summary`锛?00-200瀛楃洏闈㈡憳瑕侊級
- `timing` 鈫?`reasoning.sj`锛宍risk_control` 鈫?`reasoning.fk`锛屾柊澧?`reasoning.wz`(浣嶇疆)
- 瀛楁缂╃暐鍚嶈璁★細wz(浣嶇疆)/sj(鏃舵満)/fk(椋庢帶) 鈥?鍑忓皯 LLM token 娑堣€?- 淇 Prompt action 绀轰緥锛氫粠 "寤轰粨 / 鍔犱粨 / 鍑鸿揣 / 瑙傛湜 / 73" 鈫?"寤轰粨 / 82"

### 鏁版嵁搴?- kol_analyses 琛?DDL 鏂板 6 鍒? summary, reasoning, action, status, context_tag, evidence_tags
- 鏃у垪 position_analysis/timing/risk_control/reason 淇濈暀涓嶅垹锛孨ULL 鍏煎
- Migration: PRAGMA table_info 妫€娴?+ ALTER TABLE ADD COLUMN
- kol_analysis_insert: 17 鍒?INSERT锛宺easoning/evidence_tags 瀛?JSON
- kol_analyses_latest: SELECT 鍚柊鍒楋紝COALESCE 鍚戝墠鍏煎鏃ф暟鎹紝json.loads 杩樺師 JSON 瀛楁

### 鍚庣閫昏緫
- _execute_kol_trades: 鏂板 status != "ENTER" 杩囨护锛學AIT 鐘舵€佷笉涓嬪崟
- _execute_kol_trades: reason 瀛楁鏀圭敤 a.get("summary", "")

### API
- /api/kol/analyses 鏂板 ?symbol= 鍙傛暟鏀寔鍗曞竵杩囨护
- 鍝嶅簲鏍煎紡浠嶄负 {tokens, by_token}锛堥潪鎶€鏈柟妗堜腑鐨?{symbol, meta, judgments}锛?
### 鍓嶇
- KOL_AGENT_HTML 閲嶅啓涓?4 鍖哄崱鐗囷細
  - Zone 1: badge 琛岋紙trend 路 direction 路 confidence 路 status 路 action 路 context_tag锛? summary
  - Zone 2: 6 鏍间环鏍肩煩闃碉紙鐜颁环/鏀拺/闃诲姏/鍏ュ満/姝㈡崯/姝㈢泩锛?  - Zone 3: 鎺ㄦ紨 key:value 琛岋紙浣嶇疆:/鏃舵満:/椋庢帶:锛?  - Zone 4: evidence_tags 鏍囩锛堥鑹茬紪鐮侊細缁?澶氬ご淇″彿锛岀孩=绌哄ご淇″彿锛岀伆=涓€э級
- Timeline Header 鏄剧ず "[甯佺] 鍒ゆ柇鏃堕棿绾?路 鏈€杩?2灏忔椂 路 X鏉?
- K绾垮懆鏈熼€夋嫨鍣?[15m] [1H] [4H]锛岄粯璁?15m
- 60s 鑷姩鍒锋柊褰撳墠閫変腑甯佺
- 鎸夊竵绉嶇嫭绔嬪姞杞斤細loadTokens() 鑾峰彇鍒楄〃 鈫?loadTimeline(token) 鑾峰彇璇︽儏
- 鏃у瓧娈靛吋瀹癸細summary 鈫?position_analysis fallback锛宺easoning 鈫?timing/risk_control fallback

### 瀛楁鏄犲皠淇 (storage.py kol_analyses_latest)
- SQL 鏀瑰洖閫夋嫨鍘熷鍒楋紝fallback 閫昏緫绉昏嚦 Python 灞?- 淇 Bug B: 鏃?risk_control JSON 鍦?SQL COALESCE 涓弻閲嶇紪鐮?鈫?Python 灞傛彁鍙?dict values 鐢?锛?鎷兼帴

### 宸茬煡宸窛锛堝叏閮ㄥ凡淇锛?- 鉁?鍓嶇鏍囬 鈫?"AI 鍒ゆ柇娴佽缁嗙増"
- 鉁?context_tag 绉昏嚦 Zone 1锛坆adge 琛屾湯灏撅級
- 鉁?reasoning 鏀逛负 key:value 琛屾牸寮?- 鉁?evidence_tags 棰滆壊缂栫爜锛坆ull/bear/neutral锛?- 鉁?Timeline Header 鍚竵绉嶅悕 / 鏉℃暟
- 鉁?K 绾垮懆鏈熼€夋嫨鍣?[15m] [1H] [4H]
- 鉁?60s 鑷姩鍒锋柊
- API 鍝嶅簲鏍煎紡浠嶄负 {tokens, by_token}锛堢敤鎴疯鍙綋鍓嶆牸寮忥級

### 娣卞害瀹¤淇 (2026-05-28)
- 淇: `/agent-kol` 璺敱鎸囧悜鏃?KOL_MONITOR_HTML 鈫?鏀逛负 KOL_AGENT_HTML
- 淇: SQL LIMIT 8 鍦ㄤ笉绛涢€?symbol 鏃惰法 token 鎴柇 鈫?浠呭湪鎸囧畾 symbol 鏃跺姞 LIMIT
- 淇: `_execute_kol_trades` 娉ㄩ噴 "direction=long" 鈫?"direction=long/short"

### 宸茬煡 Bug
- Bug C (鍘嗗彶): candidate_map key 澶у皬鍐欎笉涓€鑷达紙椋庨櫓鏋佷綆锛宼oken 閮芥槸澶у啓鏉ユ簮锛?
### 宸插畬鎴愮殑鏀瑰姩
- [x] System Prompt 鏂?JSON 鏍煎紡 + action 绀轰緥淇 (kol_agent.py)
- [x] DB DDL 鍔?summary/reasoning 鍒?(storage.py)
- [x] DB Migration: action/status/context_tag/evidence_tags/summary/reasoning 脳6 (storage.py)
- [x] kol_analysis_insert 17 鍒楁洿鏂?(storage.py)
- [x] kol_analyses_latest SELECT + COALESCE fallback (storage.py)
- [x] _execute_kol_trades: status 杩囨护 + reason鈫抯ummary (kol_agent.py)
- [x] /api/kol/analyses ?symbol= 鍙傛暟 (web.py)
- [x] KOL_AGENT_HTML 4 鍖哄崱鐗囬噸鍐?(web.py)

