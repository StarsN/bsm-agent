# Changelog

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

