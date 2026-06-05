"""配置文件：可根据实际情况调整阈值"""

# === 真人 / 内容质量过滤 ===
MIN_FOLLOWERS = 50                # 粉丝数下限（feed 接口拿不到普通用户粉丝，实际主要作用于大 V）
MIN_ACCOUNT_AGE_DAYS = 30
MAX_POSTS_PER_DAY = 50
MIN_FOLLOWER_FOLLOWING_RATIO = 0.05

# 帖子级质量过滤：粉丝数拿不到时，用互动量代替
# 帖子满足以下任一即可进入榜单：
#   - 作者粉丝 >= 10 万（大 V，肯定已过 filters.is_likely_human）
#   - 点赞 >= MIN_POST_LIKES
#   - 评论 >= MIN_POST_COMMENTS
MIN_POST_LIKES = 3
MIN_POST_COMMENTS = 2

# === 社交去重 / 防刷屏 ===
MAX_POSTS_PER_AUTHOR_PER_TOKEN = 2   # 同一作者对同一代币，前 N 条按原分
AUTHOR_EXTRA_POST_WEIGHT = 0.25      # 超过 N 条后的热度降权系数
SIMILAR_TEXT_WEIGHT = 0.35           # 相似文案重复出现时的热度降权系数

# === 热度计算权重 ===
WEIGHT_LIKE = 1
WEIGHT_COMMENT = 3
WEIGHT_SHARE = 5

# === 抓取参数 ===
# 现在是"一轮 = 5 分钟持续抓取"，所以 INTERVAL = 每轮时长
SCRAPE_ROUND_SECONDS = 300      # 每轮持续 5 分钟
HEADLESS = True                 # 仪表盘模式下建议 True（浏览器别挡视线）

# 随机化抓取节奏，避免被反爬识别为机器人
SCROLL_PAUSE_SECONDS = 3        # 滚动间隔基准值（实际在 [MIN, MAX] 之间随机）
SCROLL_PAUSE_MIN = 2.0          # 滚动间隔随机下限（秒）
SCROLL_PAUSE_MAX = 5.0          # 滚动间隔随机上限（秒）
SCROLL_DISTANCE_MIN = 2500      # 每次滚动距离随机下限（px）
SCROLL_DISTANCE_MAX = 5500      # 每次滚动距离随机上限（px）
SCROLL_RESET_EVERY = 40         # 刷新间隔基准值（实际在 [MIN, MAX] 之间随机）
SCROLL_RESET_EVERY_MIN = 18     # 刷新间隔随机下限（匹配 BURST 35/IDLE 70）
SCROLL_RESET_EVERY_MAX = 25     # 刷新间隔随机上限
SCRAPE_BURST_SECONDS = 35       # 每次连续抓取时长（秒），之后休息模拟用户离开
SCRAPE_IDLE_SECONDS = 70        # 休息时长（秒），之后继续下一轮 burst
PAGE_GOTO_TIMEOUT = 90000       # 页面加载超时（毫秒）
LOW_MEMORY_MODE = True           # Linux 低内存环境（<4GB）限制 Chromium 内存

# === 数据库 ===
DB_PATH = "db/binance_square.db"
# Agent 远程执行时的数据库根路径（部署环境可能与本地不同）
AGENT_DB_ROOT = "/root/binance-monitor/bsm-agent"
# Agent 策略独立数据库（切离 system.db 减少锁冲突）
AGENT_MAIN_DB = "db/agent_main.db"
KOL_DB = "db/kol.db"
SNAPSHOT_DB = "db/snapshot.db"
NL_DB = "db/nl.db"

# === 代币白名单/黑名单 ===
TRACKED_TOKENS = set()

EXCLUDED_TOKENS = {
    "BTC", "ETH", "SOL",
    "USDT", "USDC", "U", "USD1", "USTC",
    "DAI", "BUSD", "TUSD", "USDE", "FDUSD", "PYUSD",
    "SPY", "SPYON", "QQQ", "GLD", "NVDA", "TSLA",
    "DM", "DEX", "CEX", "NFT", "AI", "USA", "UK", "EU",
    "CEO", "CFO", "CTO", "ATH", "ATL",
    "TP", "SL", "ROI", "APY", "APR", "TVL", "DCA",
    "FOMO", "FUD", "HODL", "FYI", "IMO", "AMA",
}

# === 15 分钟榜单 ===
SHORT_WINDOW_MINUTES = 15       # 榜单时间窗口
SHORT_HALF_LIFE_HOURS = 0.25    # 热度衰减半衰期
TOP_N_SHORT = 20                # 榜单显示前 N

# === Web 仪表盘 ===
WEB_HOST = "0.0.0.0"
WEB_PORT = 8000

# === 合约分析 ===
ENABLE_MARKET_ANALYSIS = True
MARKET_ANALYSIS_MAX = 30        # 榜单自动分析前 N 个有合约的代币
MARKET_MAX_RPS = 4              # 公开 API 每秒最大请求数（防触发币安 IP 限流）
MARKET_HEAVY_INTERVAL_ROUNDS = 1  # 重型数据（48h OI/价格、大户多空比）每 N 轮拉一次，1=每轮
WATCHLIST_REFRESH_SECONDS = 300 # 观察列表数据刷新间隔
WATCHLIST_REALTIME_REFRESH_SECONDS = 1   # Web 观察列表自动刷新合约快照
REALTIME_WATCHLIST_POLL_SECONDS = 5      # market_realtime.py 检查观察列表变化间隔
REALTIME_CACHE_FLUSH_SECONDS = 1         # market_realtime.py 写入缓存间隔
REALTIME_DEPTH_INTERVAL = "500ms"        # depth5 推送间隔，100ms/500ms 可选
REALTIME_WATCHLIST_LIGHT = True          # 观察列表只用 markPrice+bookTicker（不订 aggTrade/depth5）

# === 行情确认 / 流动性 ===
DEPTH_LIMIT = 100
DEPTH_RANGE_PCT = 1.0           # 统计正负 1% 盘口深度
MIN_DEPTH_1PCT_USD = 100000     # 1% 单侧深度低于该阈值则降权
MAX_SPREAD_PCT = 0.20           # 买一/卖一价差超过该阈值则降权

# === 收藏代币的学习反馈 ===
LOSS_ARCHIVE_THRESHOLD_PCT = -10.0  # 浮亏超过这个阈值就归档为负面样本（-10 即亏损 10%）
COMPOSITE_HEAT_TOP_N = 20            # 综合热度榜显示前 N
COMPOSITE_HISTORY_WINDOW = 20        # 综合热度参考最近 N 轮历史

# === 自动交易（默认模拟，不会真实下单）===
TRADING_ENABLED = False
TRADING_MODE = "paper"               # paper / live
TRADING_INITIAL_BALANCE = 1000.0

# === 币安实盘 API（TRADING_MODE="live" 时生效）===
BINANCE_API_KEY = ""                 # 合约 API Key
BINANCE_API_SECRET = ""              # 合约 API Secret
TRADING_LEVERAGE = 2

# --- 仓位 sizing（专业量化风格：先定风险，再反推仓位）---
# 默认使用"风险优先"模式：每笔交易最多亏损账户净值的 RISK_PER_TRADE_PCT
# 仓位 = 风险金额 / |entry - stop|
TRADING_SIZING_MODE = "risk_based"       # risk_based / fixed_margin
TRADING_RISK_PER_TRADE_PCT = 1.0         # 每笔最大风险占账户净值的比例（%）
TRADING_ORDER_AMOUNT = 50.0              # fixed_margin 模式下的固定保证金（兼容旧配置）
TRADING_MIN_NOTIONAL = 10.0              # 名义价值下限，低于此值不开仓（避免滑点放大）
TRADING_MAX_NOTIONAL_PCT = 50.0          # 单笔名义价值不超过账户净值的百分比

# --- 风控硬限制 ---
TRADING_MAX_CONCURRENT_POSITIONS = 0     # v2.5：0 = 不限制（用户要求）。
                                          # 可用余额自然限制实际能开的仓位数。
TRADING_MAX_DAILY_LOSS_PCT = 5.0         # 当日浮动+已实现亏损超该百分比则熔断停机
TRADING_MAX_DAILY_TRADES = 15            # 当日最多开仓次数
TRADING_DAILY_LIMIT_ENABLED = True       # 是否启用日交易次数限制（每策略独立计算）
TRADING_COOLDOWN_MINUTES_AFTER_LOSS = 30 # 同一 token 止损后冷却期（分钟）
TRADING_CORRELATED_LIMIT = 2             # 相关度高的板块同向仓位上限（目前按粗分类）

# --- 止损：波动率自适应（ATR 风格）---
# 止损距离 = max(MIN_STOP_PCT, ATR_MULTIPLIER * 最近 N 根 K 线的 ATR%)
# 若拿不到 K 线，回退到 TRADING_STOP_LOSS_PCT
# --- 系统策略自动开仓 ---
SYSTEM_AUTO_TRADE_ENABLED = True        # 是否启用系统自动开仓

# --- 做空 ---
SHORT_SELLING_ENABLED = True             # 做空总开关（KOL Agent 已接入）

# 做空入场硬否决
TRADING_SHORT_MAX_CHANGE_24H_PCT = -15.0      # 24h 跌幅超此值视为超卖，拒绝做空
TRADING_SHORT_MIN_CHANGE_4H_PCT = 0.5          # 4h 涨幅低于此值视为未充分拉伸，不做空
TRADING_SHORT_MIN_FUNDING_PCT = 0.01           # 资金费率 >= 此值视为多头拥挤，可做空
TRADING_SHORT_MAX_FUNDING_PCT = -0.05          # 资金费率 <= 此值视为空头拥挤，拒绝做空
TRADING_SHORT_MIN_LSR = 0.5                    # 散户多空比 <= 此值视为空头过热，拒绝做空
TRADING_SHORT_MAX_TAKER_RATIO = 1.5            # taker >= 此值视为买盘恢复（空头挤压），拒绝做空
TRADING_SHORT_MAX_TAKER_RECOVERY = 10.0        # taker趋势 >= 此值视为买盘恢复中，拒绝做空

# --- 策略独立账户 ---
STRATEGY_INITIAL_AGENT = 1000
STRATEGY_INITIAL_HEAT_AGENT = 1000
STRATEGY_INITIAL_HEAT_AGENT_LESSONS = 1000
STRATEGY_INITIAL_SYSTEM = 1000
STRATEGY_INITIAL_MANUAL = 1000
STRATEGY_INITIAL_KOL_AGENT = 1000
STRATEGY_INITIAL_KOL_SNAPSHOT = 1000
STRATEGY_INITIAL_AGENT_NO_LESSONS = 1000

# --- 止损 ---
TRADING_STOP_MODE = "atr"                # atr / fixed
TRADING_ATR_PERIOD = 14                  # 用多少根 1h K 线算 ATR
TRADING_ATR_STOP_MULTIPLIER = 1.5        # 止损 = 1.5 × ATR
TRADING_STOP_LOSS_PCT = -2.0             # 固定模式 或 ATR 回退时使用
TRADING_STOP_LOSS_MIN_PCT = -2.5         # ATR 模式下止损下限（KOL数据：2-3%止损胜率62% vs 0-2%的17%）
TRADING_STOP_LOSS_MAX_PCT = -5.0         # ATR 模式下止损上限（防止风险过大）

# --- 止盈（基于 R 值阶梯）---
# v4.1 基于 52 笔回测优化：高频锁利 + 小仓跟踪，TP1=1R关80%保证不亏
TRADING_TP1_R = 1.0                      # +1R 平 80%
TRADING_TP1_CLOSE_PCT = 80.0
TRADING_TP2_R = 2.0                      # +2R 再平 10%
TRADING_TP2_CLOSE_PCT = 10.0
TRADING_TRAIL_REMAIN_PCT = 10.0          # 剩余 10% 交给跟踪止盈
TRADING_TRAIL_CALLBACK_PCT = 2.0         # 从高点回撤 2% 触发

# --- 入场质量（评分 + 分档开仓）---
# 不再是所有条件硬 AND，改为分档：
#   FULL (100% 仓位): 所有核心条件通过 + 信号分 >= FULL 阈值
#   HALF (50% 仓位): 至少通过 core_required 的 N 项 + 信号分 >= HALF 阈值
#   SKIP: 其他
TRADING_ENTRY_MODE = "tiered"            # tiered / strict (原来的全AND)
TRADING_SIGNAL_FULL_THRESHOLD = 75       # signals.analyze 返回的 score 阈值（满仓），从65提到75
TRADING_SIGNAL_HALF_THRESHOLD = 65       # 半仓阈值，从55提到65（55-65区间胜率29%均亏10.63%）
TRADING_CORE_REQUIRED_PASS_COUNT = 5     # 7 项核心条件里至少通过几项才可半仓

# --- 追高保护 ---
# 即便 15m/1h 涨幅在区间内，若 4h/24h 已经大幅拉升，也视为追高
TRADING_MAX_CHANGE_4H_PCT = 25.0         # 4h 涨幅超此值则拒绝（追高）
TRADING_MAX_CHANGE_24H_PCT = 50.0        # 24h 涨幅超此值则拒绝

# --- 入场时机硬门槛（v2.2 新增，基于失败归档数据反哺）---
# 观察到历史亏损样本里 funding_hot(27)/lsr_hot(38)/buy_pressure_faded(73) 标签高频命中，
# 说明这些情况下入场就是"派发顶"。做成硬否决。
TRADING_MAX_ENTRY_FUNDING_PCT = 0.05     # 资金费率 >= 0.05%/8h 视为多头拥挤，不开仓
TRADING_MAX_ENTRY_LSR = 1.7              # 散户多空比 >= 1.7 视为情绪过热，不开仓（v2.5 从 2.0 收紧）
TRADING_MAX_ENTRY_TAKER_RATIO = 1.8      # 主动买卖比 >= 1.8 视为买盘透支，不开仓
                                          # （配合 MIN_ENTRY_TAKER_RATIO=1.15，形成 [1.15, 1.8] 区间）

# --- taker 趋势过滤（v2.4 新增，对应 buy_pressure_faded 标签）---
# 即使 taker_ratio 当前在允许区间，如果最近 20m 的 taker 趋势明显衰退，
# 说明买盘正在"消退顶部"，这种情况入场后很快会被买盘消失拖到止损。
# taker_trend_pct 定义：(最新 5m 的 taker_ratio) vs (较早 15m 平均)，负值表示衰退
TRADING_MAX_TAKER_DECAY_PCT = -5.0       # v2.5：-10% → -5%。历史数据显示 -10% 太宽松，
                                          # buy_pressure_faded 标签仍然 67 次高频命中。
                                          # 收紧到 -5%，即"任何明显衰退都不入场"。

# --- 入场时机软门槛 ---
# 15m 涨幅改窄：不要在刚急拉的 K 线顶部买入
TRADING_MAX_ENTRY_CHANGE_15M = 2.0       # 15m 涨幅不超过 2%（之前是 5%，太宽了）
                                          # 理想入场：1h/4h 正向 + 15m 缓和或轻微回调

# 允许"小幅回调入场"（比硬要求 15m > 0 更现实）
TRADING_ALLOW_15M_PULLBACK_PCT = -1.5    # 15m 允许回调到 -1.5% 以内仍视为有效（买回调）

# 大户/散户分歧加分（已存在于 signals，这里显式拉出来做入场参考）
TRADING_PREFER_SMART_MONEY_DIVERGENCE = True  # top_lsr > 1.5 且 lsr < 0.7 时优先开仓

# --- 滑点 / 订单 ---
TRADING_ASSUMED_SLIPPAGE_PCT = 0.05      # 模拟交易假设的市价滑点（入场）
TRADING_STOP_SLIPPAGE_PCT = 0.15         # 止损触发时的假设滑点（通常更坏）
TRADING_LIMIT_ORDER_TIMEOUT_SECONDS = 10
LIMIT_ORDER_TIMEOUT_SECONDS = 600           # 挂单超时自动取消（秒）

# --- 维护 ---
TRADING_SIGNAL_LOCK_RETENTION_HOURS = 72 # signal_lock 表保留时长

# --- 调试 / 日志 ---
TRADING_DEBUG = True             # 打印开仓拒绝原因，找不到"为啥不开仓"时开这个
                                 # 稳定后可以关掉减少日志噪音

# --- Agent 候选币模式 ---
# batch: worker 一轮结束后，把所有上过榜的币一次性交给 Agent（默认，不漏）
# streaming: worker 抓到新上榜的币就立刻交给 Agent（实时响应，资源消耗高）
AGENT_CANDIDATE_MODE = "batch"

# --- Agent 数据源与触发 ---
# "agent_candidates": web.py collector 收集面板数据，按时入库触发 Agent
# "token_heat_history": worker 每轮算热度榜，隔 N 轮触发 Agent（旧逻辑）
AGENT_COLLECT_POLL_SECONDS = 3            # collector 轮询间隔（秒）
AGENT_COLLECT_CACHE_TTL = 2               # candidates 缓存 TTL（秒）
AGENT_COLLECT_INTERVAL_MINUTES = 16       # collector 入库间隔（分钟），offset=1min
AGENT_HERMES_JOB_ID = "a53a7fc71ebf"      # 主 Agent Hermes cron job ID
HEAT_AGENT_HERMES_JOB_ID = "e1e3f1bed2e1"             # 热度 Agent Hermes cron job ID（填上启用）
HEAT_AGENT_LESSONS_HERMES_JOB_ID = "e5a2a49b711c"                 # 热度有教训版 Hermes cron job ID（填上启用）
HEAT_AGENT_LESSONS_TRIGGER_INTERVAL = 2               # 热度有教训版每 N 轮触发，offset=+1
NL_AGENT_HERMES_JOB_ID = "a94fe8ae4085"                              # 无教训版 Agent Hermes cron job ID（填上启用）
HEAT_AGENT_TRIGGER_INTERVAL = 2           # 热度 Agent 每 N 轮触发，offset=0

# --- 手动开仓（收藏触发）的风控豁免 ---
# 手动收藏是用户强意愿信号，某些账户级限制可以跳过：
MANUAL_BYPASS_MAX_CONCURRENT = True   # 手动开仓不受 MAX_CONCURRENT_POSITIONS 限制
MANUAL_BYPASS_SECTOR_LIMIT = True     # 手动开仓不受板块集中度限制
MANUAL_BYPASS_COOLDOWN = False        # 手动开仓仍受止损冷却期约束（建议 False 保护）
# 注意：日亏损熔断永远不豁免，这是最后的保命线

# === Agent 开关 ===
AGENT_TRADE_ENABLED = True                   # Agent-合约扫描
HEAT_AGENT_ENABLED = True                    # Agent-热度榜单
KOL_AGENT_ENABLED = True                     # Agent-KOL
KOL_SNAPSHOT_ENABLED = True                  # Agent-KOL 认知快照
NL_AGENT_ENABLED = True                      # Agent-无教训对照
HEAT_AGENT_LESSONS_ENABLED = True            # Agent-热度有教训

# === Agent 触发间隔 ===
KOL_AGENT_INTERVAL_MINUTES = 8              # KOL Agent 触发间隔（分钟），offset=0
KOL_SNAPSHOT_INTERVAL_MINUTES = 8           # KOL 认知快照 触发间隔（分钟）
NL_AGENT_INTERVAL_MINUTES = 16              # 无教训版 Agent 触发间隔（分钟），offset=3min
AI_REGIME_INTERVAL_MINUTES = 32             # AI 市场研判刷新间隔（分钟），offset=5min

# === KOL Agent LLM ===
KOL_LLM_PROVIDER = "deepseek"                        # "deepseek" / "nvidia"
KOL_LLM_TIMEOUT = 86400                              # LLM 调用超时（秒）
KOL_AGENT_MIN_CONFIDENCE = 70                        # KOL Agent 下单最低信心分（0-100）
KOL_TOKEN_COOLDOWN_MINUTES = 30                      # 同一 token 两次发给 LLM 的最小间隔（分钟）
KOL_CANDIDATES_PER_BATCH = 2                         # KOL 每批发送候选币数量
KOL_KNOWLEDGE_DIR = r"/root/obsidian/MyAi/x"
DEEPSEEK_API_KEY = ""
DEEPSEEK_API_BASE = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-v4-pro"
NVIDIA_API_KEY = ""
NVIDIA_API_BASE = "https://integrate.api.nvidia.com/v1"
NVIDIA_MODEL = "deepseek-ai/deepseek-v4-pro"
NVIDIA_API_KEYS = [
    "",
    "",
    "",
    "",
    "",
]

# === KOL 认知快照 Agent LLM（独立 key）===
KOL_SNAPSHOT_MIN_CONFIDENCE = 70
KOL_SNAPSHOT_KNOWLEDGE_DIR = r"/root/obsidian/MyAi/x"
KOL_SNAPSHOT_LLM_PROVIDER = "nvidia"
KOL_SNAPSHOT_DEEPSEEK_API_KEY = ""
KOL_SNAPSHOT_NVIDIA_API_KEY = ""
KOL_SNAPSHOT_NVIDIA_API_KEYS = [
    "",
    "",
    "",
    "",
]

# === KOL 策略：代币异常度筛选 ===
KOL_MIN_OI_CHANGE_1H_PCT = 4.0             # |1h OI变化| > 4% (P95)
KOL_MIN_OI_CHANGE_4H_PCT = 10.0            # |4h OI变化| > 10% (P95)
KOL_MIN_FUNDING_ABS_PCT = 0.03             # |资金费率| > 0.03%/8h
KOL_MIN_MA20_DEVIATION_PCT = 2.5           # |MA20乖离率| > 2.5% (P95)
KOL_MAX_VOL_OI = 20.0                      # vol/OI > 20x 排除
KOL_MIN_ANOMALY_SCORE = 2                  # 至少 2 维异常入选

# === V3 AI Regime 专用 LLM（独立 key）===
AI_REGIME_API_KEY = ""
AI_REGIME_API_BASE = "https://integrate.api.nvidia.com/v1"
AI_REGIME_MODEL = "deepseek-ai/deepseek-v4-pro"


