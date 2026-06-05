"""SQLite 存储：帖子、作者、代币提及、观察列表、行情快照"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterable
import config


SCHEMA = """
CREATE TABLE IF NOT EXISTS authors (
    user_id         TEXT PRIMARY KEY,
    username        TEXT,
    followers       INTEGER,
    following       INTEGER,
    account_created TIMESTAMP,
    post_count_24h  INTEGER DEFAULT 0,
    is_human        INTEGER,
    last_seen       TIMESTAMP
);

CREATE TABLE IF NOT EXISTS posts (
    post_id        TEXT PRIMARY KEY,
    user_id        TEXT,
    content        TEXT,
    likes          INTEGER DEFAULT 0,
    comments       INTEGER DEFAULT 0,
    shares         INTEGER DEFAULT 0,
    posted_at      TIMESTAMP,
    fetched_at     TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES authors(user_id)
);

CREATE TABLE IF NOT EXISTS mentions (
    post_id   TEXT,
    token     TEXT,
    PRIMARY KEY (post_id, token)
);

CREATE TABLE IF NOT EXISTS watchlist (
    token       TEXT PRIMARY KEY,
    added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    token       TEXT PRIMARY KEY,
    snapshot    TEXT,      -- JSON 序列化的 snap dict
    analysis    TEXT,      -- JSON 序列化的 analysis dict
    updated_at  TIMESTAMP
);

CREATE TABLE IF NOT EXISTS market_realtime_cache (
    token       TEXT PRIMARY KEY,
    symbol      TEXT,
    snapshot    TEXT,
    updated_at  TIMESTAMP
);

-- worker 心跳表：只有一行，key='worker'
CREATE TABLE IF NOT EXISTS worker_status (
    key             TEXT PRIMARY KEY,
    stage           TEXT,      -- idle / scraping / saving / market / sleeping
    detail          TEXT,      -- 当前阶段的人类可读说明
    round_start     TIMESTAMP, -- 本轮开始时间
    round_number    INTEGER DEFAULT 0,
    last_heartbeat  TIMESTAMP,
    posts_this_round      INTEGER DEFAULT 0,
    saved_this_round      INTEGER DEFAULT 0,
    total_posts           INTEGER DEFAULT 0,
    total_authors         INTEGER DEFAULT 0
);

-- 代币热度历史：每轮给每个上榜代币记一条
CREATE TABLE IF NOT EXISTS token_heat_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    token         TEXT NOT NULL,
    round_number  INTEGER,
    recorded_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    score         REAL,           -- 当轮热度分
    mentions      INTEGER,
    unique_posts  INTEGER,
    total_likes   INTEGER,
    total_comments INTEGER,
    total_shares  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_heat_token ON token_heat_history(token, recorded_at);
CREATE INDEX IF NOT EXISTS idx_heat_round ON token_heat_history(round_number);

-- 收藏入场记录：收藏时的锚定数据
CREATE TABLE IF NOT EXISTS watchlist_entries (
    token           TEXT PRIMARY KEY,
    anchored_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    anchor_price    REAL,
    anchor_snapshot TEXT,           -- 完整快照 JSON
    anchor_analysis TEXT,           -- 分析结果 JSON
    max_drawdown    REAL DEFAULT 0, -- 从锚定后出现过的最大浮亏（负数，%）
    peak_profit     REAL DEFAULT 0, -- 最高浮盈（正数，%）
    archived        INTEGER DEFAULT 0 -- 是否已归档为负面样本
);

-- 收藏跟踪：每次刷新追加一条
CREATE TABLE IF NOT EXISTS watchlist_followups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT NOT NULL,
    recorded_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    price           REAL,
    pnl_pct         REAL,           -- 相对锚定价的浮盈浮亏 %
    snapshot        TEXT,           -- 当时的完整快照
    analysis        TEXT
);
CREATE INDEX IF NOT EXISTS idx_followup_token ON watchlist_followups(token, recorded_at);

-- 归档的负面样本（亏损案例）
CREATE TABLE IF NOT EXISTS loss_samples (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT NOT NULL,
    anchored_at     TIMESTAMP,
    archived_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    anchor_price    REAL,
    bottom_price    REAL,
    max_drawdown    REAL,           -- 最大浮亏
    anchor_snapshot TEXT,           -- 入场快照
    anchor_analysis TEXT,
    followup_count  INTEGER,        -- 经历了多少次刷新
    followups_json  TEXT            -- 所有 followup 的完整序列 JSON
);

CREATE INDEX IF NOT EXISTS idx_posts_posted_at ON posts(posted_at);
CREATE INDEX IF NOT EXISTS idx_mentions_token ON mentions(token);

CREATE TABLE IF NOT EXISTS trading_settings (
    key          TEXT PRIMARY KEY,
    value        TEXT,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trade_positions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    token              TEXT NOT NULL,
    symbol             TEXT NOT NULL,
    side               TEXT NOT NULL,
    status             TEXT NOT NULL,
    mode               TEXT NOT NULL DEFAULT 'paper',
    margin_amount      REAL NOT NULL,
    leverage           REAL NOT NULL,
    notional           REAL NOT NULL,
    quantity           REAL NOT NULL,
    entry_price        REAL,
    limit_price        REAL,
    current_price      REAL,
    stop_loss_price    REAL,
    tp1_price          REAL,
    tp2_price          REAL,
    highest_price      REAL,
    lowest_price       REAL,
    trailing_stop_price REAL,
    closed_qty         REAL DEFAULT 0,
    realized_pnl       REAL DEFAULT 0,
    unrealized_pnl     REAL DEFAULT 0,
    pnl_pct            REAL DEFAULT 0,
    signal_snapshot    TEXT,
    open_reason        TEXT,
    advice             TEXT,
    exchange_order_id  TEXT,               -- 实盘：合约订单 ID（JSON 格式）
    strategy           TEXT DEFAULT 'agent', -- 策略来源：agent / system
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at          TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trade_positions_status ON trade_positions(status, token);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_one_active_token
ON trade_positions(token, side, strategy)
WHERE status IN ('PENDING', 'OPEN', 'PARTIAL');

CREATE TABLE IF NOT EXISTS trade_signal_locks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT NOT NULL,
    signal_key      TEXT NOT NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(token, signal_key)
);

CREATE INDEX IF NOT EXISTS idx_trade_signal_locks_token
ON trade_signal_locks(token, created_at);

CREATE TABLE IF NOT EXISTS trade_loss_archive (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id     INTEGER,
    token           TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    entry_price     REAL,
    exit_price      REAL,
    realized_pnl    REAL,
    pnl_pct         REAL,
    failed_reason   TEXT,
    reason_tags     TEXT,
    entry_snapshot  TEXT,
    exit_snapshot   TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trade_loss_token ON trade_loss_archive(token, created_at);

-- Agent 决策队列：Agent 写入，auto_trader 读取执行
CREATE TABLE IF NOT EXISTS pending_decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    action          TEXT NOT NULL,        -- 'open_long' / 'close'
    token           TEXT NOT NULL,
    tier            TEXT,                 -- 'full' / 'half' / 'quarter'
    entry_price     REAL,                 -- Agent 建议的开仓价（系统校验，不合理则兜底）
    stop_loss       REAL,                 -- Agent 建议的止损价（系统校验，不合理则 ATR 兜底）
    tp1_price       REAL,                 -- Agent 建议的止盈1（系统校验，不合理则自动算）
    tp2_price       REAL,                 -- Agent 建议的止盈2（系统校验，不合理则自动算）
    close_reason    TEXT,                 -- action=close 时的平仓理由
    reason          TEXT NOT NULL,        -- Agent 的详细决策理由（必填）
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending → consumed / rejected / expired
    consumed_at     TIMESTAMP,
    reject_reason   TEXT,                 -- 被 risk.py 拒绝的原因
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    agent_run_id    TEXT,                 -- 本轮 Agent 运行的唯一标识
    source_round    INTEGER,              -- 来自 worker 第几轮
    social_score    REAL,                 -- 候选币的社交热度分
    mentions        INTEGER,              -- 候选币的提及次数
    -- 日志字段：Agent 决策时填写，系统开仓时读取写入 journal
    dimension_data  TEXT,                 -- 入场时的市场数据快照（JSON）
    market_overview TEXT,                 -- 市场环境一句话（BTC走势、时段）
    lesson_checked  TEXT,                 -- 开仓前查了哪些 lessons
    source          TEXT DEFAULT 'agent_candidates'  -- 数据源：agent_candidates / token_heat_history
);

CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_decisions(status, created_at);
CREATE INDEX IF NOT EXISTS idx_pending_token ON pending_decisions(token, created_at);

-- Agent 教训库：亏损单复盘，Agent 自主写入，开仓前必查
CREATE TABLE IF NOT EXISTS lessons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER,                -- 关联 trade_positions.id
    token           TEXT NOT NULL,
    direction       TEXT,                   -- long / short
    entry_price     REAL,
    exit_price      REAL,
    pnl_pct         REAL,                   -- 亏损百分比（负数）
    market_snapshot TEXT,                   -- 入场时的行情快照摘要
    macro_context   TEXT,                   -- 入场时的市场环境（BTC走势、时段等）
    signal_error    TEXT,                   -- 信号判断失误（如"误读OI背离"）
    what_missed     TEXT,                   -- 复盘发现遗漏的关键信号
    root_cause      TEXT,                   -- 根本原因（一句话）
    lesson          TEXT NOT NULL,          -- 教训内容（必填，人类可读）
    rule_update     TEXT,                   -- 由此衍生的规则（如"4h涨超25%不开多"）
    severity        TEXT DEFAULT 'medium',  -- critical / warning / medium
    learned         INTEGER DEFAULT 0,      -- 0=仍适用, 1=已被新规则覆盖
    strategy        TEXT DEFAULT 'agent',   -- 归属策略
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_lessons_token ON lessons(token, created_at);
CREATE INDEX IF NOT EXISTS idx_lessons_learned ON lessons(learned, severity);
CREATE INDEX IF NOT EXISTS idx_lessons_ca ON lessons(created_at);

-- Agent 操作日志：每次开仓/平仓写一条，每日复盘时提炼为 lessons
CREATE TABLE IF NOT EXISTS journal (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER,                -- 关联 trade_positions.id（平仓时回填）
    token           TEXT NOT NULL,
    action          TEXT NOT NULL,           -- open / close
    price           REAL,                   -- 操作价格（开仓价 or 平仓价）
    tier            TEXT,                   -- full / half / quarter（开仓时）
    stop_loss       REAL,                   -- 止损价（开仓时）
    tp1_price       REAL,                   -- 止盈1（开仓时）
    tp2_price       REAL,                   -- 止盈2（开仓时）
    reason          TEXT NOT NULL,          -- Agent 的详细决策理由
    dimension_data  TEXT,                   -- 入场/出场时的市场数据快照（JSON）
    market_overview TEXT,                   -- 市场环境一句话（BTC走势、时段）
    lesson_checked  TEXT,                   -- 开仓前查了哪些lessons（记录）
    pnl_pct         REAL,                   -- 盈亏%（平仓时）
    close_reason    TEXT,                   -- 平仓理由（平仓时）
    hold_duration   TEXT,                   -- 持仓时长（平仓时）
    pending_decision_id INTEGER,            -- 关联 pending_decisions.id
    source_round    INTEGER,                -- 来自 worker 第几轮
    social_score    REAL,                   -- 开仓时的社交热度分
    reviewed        INTEGER DEFAULT 0,       -- 0=未复盘, 1=已复盘
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_journal_token ON journal(token, created_at);
CREATE INDEX IF NOT EXISTS idx_journal_action ON journal(action, created_at);

-- Agent 候选币池（每轮面板扫描结果快照）
CREATE TABLE IF NOT EXISTS agent_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_number    INTEGER NOT NULL,
    token           TEXT NOT NULL,
    data            TEXT NOT NULL,           -- 候选币全字段 JSON（同 extract 脚本 candidates 格式）
    tier            TEXT,                    -- full / half / skip
    passed          INTEGER DEFAULT 0,       -- 0 / 1
    hard_blocks     TEXT,                    -- JSON 数组
    pass_count      INTEGER,
    signal_key      TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_candidates_round ON agent_candidates(round_number);
CREATE INDEX IF NOT EXISTS idx_agent_candidates_token_round ON agent_candidates(token, round_number);

-- KOL Agent 分析结果
CREATE TABLE IF NOT EXISTS kol_analyses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT NOT NULL,
    trend           TEXT,
    timeline        TEXT,
    price_levels    TEXT,
    summary         TEXT,
    reasoning       TEXT,
    position_analysis TEXT,
    timing          TEXT,
    risk_control    TEXT,
    direction       TEXT,
    confidence      TEXT,
    reason          TEXT,
    llm_log_id      INTEGER,
    action          TEXT,
    status          TEXT,
    context_tag     TEXT,
    evidence_tags   TEXT,
    missing_data    TEXT,
    raw_response    TEXT,
    system_prompt   TEXT,
    user_prompt     TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_kol_analyses_token ON kol_analyses(token);
CREATE INDEX IF NOT EXISTS idx_kol_analyses_created ON kol_analyses(created_at);

-- KOL Agent 候选币累积表（独立于 agent_candidates，按 KOL 自己的周期入库）
CREATE TABLE IF NOT EXISTS kol_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_number    INTEGER NOT NULL,
    token           TEXT NOT NULL,
    data            TEXT,
    tier            TEXT,
    passed          INTEGER DEFAULT 1,
    hard_blocks     TEXT,
    pass_count      INTEGER,
    signal_key      TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_kol_candidates_round ON kol_candidates(round_number);
CREATE INDEX IF NOT EXISTS idx_kol_candidates_token_round ON kol_candidates(token, round_number);

-- 无教训版 Agent 候选币累积表
CREATE TABLE IF NOT EXISTS nl_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_number    INTEGER NOT NULL,
    token           TEXT NOT NULL,
    data            TEXT,
    tier            TEXT,
    passed          INTEGER DEFAULT 1,
    hard_blocks     TEXT,
    pass_count      INTEGER,
    signal_key      TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_nl_candidates_round ON nl_candidates(round_number);
CREATE INDEX IF NOT EXISTS idx_nl_candidates_token_round ON nl_candidates(token, round_number);

-- KOL LLM 调用日志
CREATE TABLE IF NOT EXISTS kol_llm_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    provider        TEXT,
    model           TEXT,
    candidate_count INTEGER,
    prompt_chars    INTEGER,
    response_chars  INTEGER,
    duration_ms     INTEGER,
    success         INTEGER DEFAULT 0,
    error           TEXT,
    analyses_count  INTEGER,
    system_prompt   TEXT,
    user_prompt     TEXT,
    raw_response    TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_kol_llm_logs_created ON kol_llm_logs(created_at);
"""


@contextmanager
def get_conn(db_path: str = None):
    """db_path=None → system.db；否则 → 指定路径"""
    path = db_path if db_path else config.DB_PATH
    # 确保 db/ 目录存在（sqlite3 不会自动创建父目录）
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _migrate(conn):
    """老库迁移：加 first_seen_at 列 + 新表"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(posts)").fetchall()]
    if "first_seen_at" not in cols:
        conn.execute("ALTER TABLE posts ADD COLUMN first_seen_at TIMESTAMP")
        conn.execute("UPDATE posts SET first_seen_at = fetched_at WHERE first_seen_at IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_posts_first_seen ON posts(first_seen_at)")
    conn.execute("""
        UPDATE trade_positions
        SET pnl_pct = realized_pnl / margin_amount * 100
        WHERE status = 'CLOSED'
          AND margin_amount > 0
          AND ABS(COALESCE(realized_pnl, 0)) > 0.0000001
          AND ABS(COALESCE(pnl_pct, 0)) < 0.0000001
    """)
    conn.execute("""
        UPDATE trade_positions
        SET current_price = stop_loss_price
        WHERE status = 'CLOSED'
          AND advice LIKE '-2% 止损%'
          AND stop_loss_price IS NOT NULL
          AND ABS(COALESCE(current_price, 0) - COALESCE(entry_price, 0)) < 0.0000001
    """)
    conn.execute("""
        DELETE FROM trade_loss_archive
        WHERE position_id IS NOT NULL
          AND id NOT IN (
              SELECT MIN(id)
              FROM trade_loss_archive
              WHERE position_id IS NOT NULL
              GROUP BY position_id
          )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_loss_position
        ON trade_loss_archive(position_id)
        WHERE position_id IS NOT NULL
    """)
    # pending_decisions 新增字段
    pd_cols = [r[1] for r in conn.execute("PRAGMA table_info(pending_decisions)").fetchall()]
    for col, typ in [("entry_price", "REAL"), ("stop_loss", "REAL"),
                     ("tp1_price", "REAL"), ("tp2_price", "REAL"),
                     ("dimension_data", "TEXT"), ("market_overview", "TEXT"),
                     ("lesson_checked", "TEXT"), ("source_round", "INTEGER"),
                     ("social_score", "REAL"), ("mentions", "INTEGER")]:
        if col not in pd_cols:
            conn.execute(f"ALTER TABLE pending_decisions ADD COLUMN {col} {typ}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_token ON pending_decisions(token, created_at)")
    # journal 新增字段
    j_cols = [r[1] for r in conn.execute("PRAGMA table_info(journal)").fetchall()]
    for col, typ in [("pending_decision_id", "INTEGER"), ("source_round", "INTEGER"),
                     ("social_score", "REAL")]:
        if col not in j_cols:
            conn.execute(f"ALTER TABLE journal ADD COLUMN {col} {typ}")
    # journal.reviewed 字段
    j_cols = [r[1] for r in conn.execute("PRAGMA table_info(journal)").fetchall()]
    if "reviewed" not in j_cols:
        conn.execute("ALTER TABLE journal ADD COLUMN reviewed INTEGER DEFAULT 0")
    # trade_positions 新字段
    tp_cols = [r[1] for r in conn.execute("PRAGMA table_info(trade_positions)").fetchall()]
    if "exchange_order_id" not in tp_cols:
        conn.execute("ALTER TABLE trade_positions ADD COLUMN exchange_order_id TEXT")
    if "strategy" not in tp_cols:
        conn.execute("ALTER TABLE trade_positions ADD COLUMN strategy TEXT DEFAULT 'agent'")
    if "lowest_price" not in tp_cols:
        conn.execute("ALTER TABLE trade_positions ADD COLUMN lowest_price REAL")
    if "pending_decision_id" not in tp_cols:
        conn.execute("ALTER TABLE trade_positions ADD COLUMN pending_decision_id INTEGER")
    # kol_analyses strategy 列（kol_snapshot 策略隔离）
    ka_cols = {r["name"] for r in conn.execute("PRAGMA table_info(kol_analyses)").fetchall()}
    if "strategy" not in ka_cols:
        conn.execute("ALTER TABLE kol_analyses ADD COLUMN strategy TEXT DEFAULT 'kol_agent'")
    if "order_type" not in tp_cols:
        conn.execute("ALTER TABLE trade_positions ADD COLUMN order_type TEXT DEFAULT 'market'")
        conn.execute("UPDATE trade_positions SET order_type = 'limit' WHERE status = 'PENDING'")
    # 更新唯一索引为 (token, side, strategy) 实现策略级数据隔离
    conn.execute("DROP INDEX IF EXISTS idx_trade_one_active_token")
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_one_active_token
        ON trade_positions(token, side, strategy)
        WHERE status IN ('PENDING', 'OPEN', 'PARTIAL')
    """)
    # signal_lock 策略隔离：重建唯一约束为 (token, signal_key, strategy)
    sl_cols = [r[1] for r in conn.execute("PRAGMA table_info(trade_signal_locks)").fetchall()]
    if "strategy" not in sl_cols:
        # SQLite 不支持 ALTER TABLE DROP CONSTRAINT，只能重建表
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trade_signal_locks_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token TEXT NOT NULL,
                signal_key TEXT NOT NULL,
                strategy TEXT DEFAULT 'agent',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(token, signal_key, strategy)
            );
            INSERT INTO trade_signal_locks_new (id, token, signal_key, created_at)
                SELECT id, token, signal_key, created_at FROM trade_signal_locks;
            DROP TABLE trade_signal_locks;
            ALTER TABLE trade_signal_locks_new RENAME TO trade_signal_locks;
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_lock_token ON trade_signal_locks(token, created_at)")
    # pending_decisions source 列
    pd_cols = [r[1] for r in conn.execute("PRAGMA table_info(pending_decisions)").fetchall()]
    if "source" not in pd_cols:
        conn.execute("ALTER TABLE pending_decisions ADD COLUMN source TEXT DEFAULT 'agent_candidates'")
    # lessons strategy 列
    l_cols = [r[1] for r in conn.execute("PRAGMA table_info(lessons)").fetchall()]
    if "strategy" not in l_cols:
        conn.execute("ALTER TABLE lessons ADD COLUMN strategy TEXT DEFAULT 'agent'")
    # lessons 新索引
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lessons_ca ON lessons(created_at)")
    # kol_analyses 存 prompt
    ka_cols = [r[1] for r in conn.execute("PRAGMA table_info(kol_analyses)").fetchall()]
    if "system_prompt" not in ka_cols:
        conn.execute("ALTER TABLE kol_analyses ADD COLUMN system_prompt TEXT")
    if "user_prompt" not in ka_cols:
        conn.execute("ALTER TABLE kol_analyses ADD COLUMN user_prompt TEXT")
    if "llm_log_id" not in ka_cols:
        conn.execute("ALTER TABLE kol_analyses ADD COLUMN llm_log_id INTEGER")
    if "action" not in ka_cols:
        conn.execute("ALTER TABLE kol_analyses ADD COLUMN action TEXT")
    if "status" not in ka_cols:
        conn.execute("ALTER TABLE kol_analyses ADD COLUMN status TEXT")
    if "context_tag" not in ka_cols:
        conn.execute("ALTER TABLE kol_analyses ADD COLUMN context_tag TEXT")
    if "evidence_tags" not in ka_cols:
        conn.execute("ALTER TABLE kol_analyses ADD COLUMN evidence_tags TEXT")
    if "summary" not in ka_cols:
        conn.execute("ALTER TABLE kol_analyses ADD COLUMN summary TEXT")
    if "reasoning" not in ka_cols:
        conn.execute("ALTER TABLE kol_analyses ADD COLUMN reasoning TEXT")
    if "missing_data" not in ka_cols:
        conn.execute("ALTER TABLE kol_analyses ADD COLUMN missing_data TEXT")
    # kol_llm_logs 存 prompt（从 kol_analyses 迁出，减少冗余）
    kll_cols = [r[1] for r in conn.execute("PRAGMA table_info(kol_llm_logs)").fetchall()]
    if "system_prompt" not in kll_cols:
        conn.execute("ALTER TABLE kol_llm_logs ADD COLUMN system_prompt TEXT")
    if "user_prompt" not in kll_cols:
        conn.execute("ALTER TABLE kol_llm_logs ADD COLUMN user_prompt TEXT")
    if "raw_response" not in kll_cols:
        conn.execute("ALTER TABLE kol_llm_logs ADD COLUMN raw_response TEXT")
    # round_candidates 遗留表，无人读写的弃用数据
    conn.execute("DELETE FROM round_candidates")
    # token_heat_history round_number 索引
    conn.execute("CREATE INDEX IF NOT EXISTS idx_heat_round ON token_heat_history(round_number)")


def init_db():
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
        _migrate(conn)
    init_agent_dbs()


def _init_one_agent_db(db_path: str, ddl: str):
    """单个 Agent DB 初始化"""
    with get_conn(db_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(ddl)


_AGENT_MAIN_DDL = """\
CREATE TABLE IF NOT EXISTS agent_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_number    INTEGER NOT NULL,
    token           TEXT    NOT NULL,
    data            TEXT,
    tier            TEXT,
    passed          INTEGER DEFAULT 0,
    hard_blocks     TEXT,
    pass_count      INTEGER DEFAULT 0,
    signal_key      TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_agent_candidates_token ON agent_candidates(token);
CREATE INDEX IF NOT EXISTS idx_agent_candidates_round ON agent_candidates(round_number);
"""

_KOL_DDL = """\
CREATE TABLE IF NOT EXISTS kol_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_number    INTEGER NOT NULL,
    token           TEXT    NOT NULL,
    data            TEXT,
    tier            TEXT,
    passed          INTEGER DEFAULT 0,
    hard_blocks     TEXT,
    pass_count      INTEGER DEFAULT 0,
    signal_key      TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_kol_candidates_token ON kol_candidates(token);
CREATE TABLE IF NOT EXISTS kol_analyses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT NOT NULL,
    trend           TEXT,
    timeline        TEXT,
    price_levels    TEXT,
    summary         TEXT,
    reasoning       TEXT,
    position_analysis TEXT,
    timing          TEXT,
    risk_control    TEXT,
    direction       TEXT,
    confidence      TEXT,
    reason          TEXT,
    llm_log_id      INTEGER,
    action          TEXT,
    status          TEXT,
    context_tag     TEXT,
    evidence_tags   TEXT,
    missing_data    TEXT,
    raw_response    TEXT,
    system_prompt   TEXT,
    user_prompt     TEXT,
    strategy        TEXT DEFAULT 'kol_agent',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_kol_analyses_token ON kol_analyses(token);
CREATE INDEX IF NOT EXISTS idx_kol_analyses_created ON kol_analyses(created_at);
CREATE TABLE IF NOT EXISTS kol_llm_logs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    provider        TEXT,
    model           TEXT,
    candidate_count INTEGER,
    prompt_chars    INTEGER,
    response_chars  INTEGER,
    duration_ms     INTEGER,
    success         INTEGER,
    error           TEXT,
    analyses_count  INTEGER,
    system_prompt   TEXT,
    user_prompt     TEXT,
    raw_response    TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

_NL_DDL = """\
CREATE TABLE IF NOT EXISTS nl_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    round_number    INTEGER NOT NULL,
    token           TEXT    NOT NULL,
    data            TEXT,
    tier            TEXT,
    passed          INTEGER DEFAULT 0,
    hard_blocks     TEXT,
    pass_count      INTEGER DEFAULT 0,
    signal_key      TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_nl_candidates_token ON nl_candidates(token);
"""


def init_agent_dbs():
    _init_one_agent_db(config.AGENT_MAIN_DB, _AGENT_MAIN_DDL)
    _init_one_agent_db(config.KOL_DB, _KOL_DDL)
    _init_one_agent_db(config.SNAPSHOT_DB, _KOL_DDL)
    _init_one_agent_db(config.NL_DB, _NL_DDL)


def upsert_author(conn, author: dict):
    import time as _t
    for attempt in range(5):
        try:
            conn.execute("""
                INSERT INTO authors (user_id, username, followers, following,
                                     account_created, post_count_24h, is_human, last_seen)
                VALUES (:user_id, :username, :followers, :following,
                        :account_created, :post_count_24h, :is_human, :last_seen)
                ON CONFLICT(user_id) DO UPDATE SET
                    username=excluded.username,
                    followers=excluded.followers,
                    following=excluded.following,
                    post_count_24h=excluded.post_count_24h,
                    is_human=excluded.is_human,
                    last_seen=excluded.last_seen
            """, author)
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 4:
                _t.sleep(1 + attempt * 0.5)
            else:
                raise


def upsert_post(conn, post: dict):
    """首次插入时 first_seen_at = fetched_at；已有记录只更新互动量和 fetched_at"""
    conn.execute("""
        INSERT INTO posts (post_id, user_id, content, likes, comments, shares,
                           posted_at, fetched_at, first_seen_at)
        VALUES (:post_id, :user_id, :content, :likes, :comments, :shares,
                :posted_at, :fetched_at, :fetched_at)
        ON CONFLICT(post_id) DO UPDATE SET
            likes=excluded.likes,
            comments=excluded.comments,
            shares=excluded.shares,
            fetched_at=excluded.fetched_at
    """, post)


def insert_mentions(conn, post_id: str, tokens: Iterable[str]):
    conn.executemany(
        "INSERT OR IGNORE INTO mentions (post_id, token) VALUES (?, ?)",
        [(post_id, t) for t in tokens],
    )


def purge_old(conn, days: int = 7):
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    conn.execute("DELETE FROM posts WHERE posted_at < ?", (cutoff,))
    conn.execute("""
        DELETE FROM mentions
        WHERE post_id NOT IN (SELECT post_id FROM posts)
    """)


# === 观察列表 ===

def watchlist_get_all(conn) -> list[str]:
    cur = conn.execute("SELECT token FROM watchlist ORDER BY added_at DESC")
    return [r["token"] for r in cur.fetchall()]


def watchlist_add(conn, token: str):
    conn.execute(
        "INSERT OR IGNORE INTO watchlist (token, added_at) VALUES (?, CURRENT_TIMESTAMP)",
        (token.upper(),)
    )


def watchlist_remove(conn, token: str):
    conn.execute("DELETE FROM watchlist WHERE token = ?", (token.upper(),))


# === 合约快照缓存 ===

def snapshot_upsert(conn, token: str, snapshot_json: str, analysis_json: str):
    import time as _t
    for attempt in range(5):
        try:
            conn.execute("""
                INSERT INTO market_snapshots (token, snapshot, analysis, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(token) DO UPDATE SET
                    snapshot=excluded.snapshot,
                    analysis=excluded.analysis,
                    updated_at=excluded.updated_at
            """, (token.upper(), snapshot_json, analysis_json))
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 4:
                _t.sleep(1 + attempt * 0.5)
            else:
                raise


def snapshot_get(conn, token: str) -> dict | None:
    row = conn.execute(
        "SELECT token, snapshot, analysis, updated_at FROM market_snapshots WHERE token = ?",
        (token.upper(),)
    ).fetchone()
    return dict(row) if row else None


def snapshot_get_all(conn) -> list[dict]:
    cur = conn.execute(
        "SELECT token, snapshot, analysis, updated_at FROM market_snapshots"
    )
    return [dict(r) for r in cur.fetchall()]


def realtime_upsert(conn, token: str, symbol: str, snapshot_json: str):
    import time as _t
    for attempt in range(5):
        try:
            conn.execute("""
                INSERT INTO market_realtime_cache (token, symbol, snapshot, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(token) DO UPDATE SET
                    symbol=excluded.symbol,
                    snapshot=excluded.snapshot,
                    updated_at=excluded.updated_at
            """, (token.upper(), symbol.upper(), snapshot_json))
            return
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 4:
                _t.sleep(1 + attempt * 0.5)
            else:
                raise


def realtime_get(conn, token: str) -> dict | None:
    row = conn.execute(
        "SELECT token, symbol, snapshot, updated_at FROM market_realtime_cache WHERE token = ?",
        (token.upper(),)
    ).fetchone()
    return dict(row) if row else None


def realtime_get_all(conn) -> list[dict]:
    cur = conn.execute(
        "SELECT token, symbol, snapshot, updated_at FROM market_realtime_cache"
    )
    return [dict(r) for r in cur.fetchall()]


# === Worker 状态（心跳 + 进度）===

def status_update(conn, **fields):
    """更新 worker 状态（任何字段可选）"""
    fields["last_heartbeat"] = "__CURRENT_TIMESTAMP__"
    # 先确保那一行存在
    conn.execute(
        "INSERT OR IGNORE INTO worker_status (key) VALUES ('worker')"
    )
    # 构造 UPDATE
    sets = []
    params = []
    for k, v in fields.items():
        if v == "__CURRENT_TIMESTAMP__":
            sets.append(f"{k} = CURRENT_TIMESTAMP")
        else:
            sets.append(f"{k} = ?")
            params.append(v)
    sql = f"UPDATE worker_status SET {', '.join(sets)} WHERE key = 'worker'"
    conn.execute(sql, params)


def status_get(conn) -> dict | None:
    row = conn.execute("SELECT * FROM worker_status WHERE key = 'worker'").fetchone()
    return dict(row) if row else None


# === 热度历史 ===

def heat_history_add(conn, round_number: int, token_scores: list[dict]):
    """一次性写入本轮所有代币的热度快照"""
    rows = [
        (s["token"], round_number, s["score"], s["mentions"],
         s["unique_posts"], s["total_likes"], s["total_comments"], s["total_shares"])
        for s in token_scores
    ]
    conn.executemany("""
        INSERT INTO token_heat_history
            (token, round_number, score, mentions, unique_posts,
             total_likes, total_comments, total_shares)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, rows)


def heat_history_recent(conn, token: str, limit: int = 10) -> list[dict]:
    """拿某代币最近 N 轮的热度记录（按时间降序）"""
    cur = conn.execute("""
        SELECT round_number, recorded_at, score, mentions, unique_posts
        FROM token_heat_history
        WHERE token = ?
        ORDER BY id DESC
        LIMIT ?
    """, (token, limit))
    return [dict(r) for r in cur.fetchall()]


def heat_history_purge_old(conn, keep_last_rounds: int = 200):
    """只保留最近 N 轮的历史（避免库无限增长）"""
    conn.execute("""
        DELETE FROM token_heat_history
        WHERE round_number <= (
            SELECT COALESCE(MAX(round_number), 0) - ?
            FROM token_heat_history
        )
    """, (keep_last_rounds,))


def leaderboard_signal_key(conn) -> str:
    row = conn.execute("SELECT MAX(id) AS max_id FROM token_heat_history").fetchone()
    if row and row["max_id"]:
        return f"heat:{row['max_id']}"
    status = status_get(conn)
    if status and status.get("round_number"):
        return f"worker:{status['round_number']}"
    return "no-history"


# === Agent 候选币池 ===

def agent_candidates_insert_batch(conn, round_number: int, items: list[dict]):
    """批量写入一轮的候选币评估结果。每条 items 含 token/data/tier/passed/hard_blocks/pass_count/signal_key。"""
    import time as _t
    for item in items:
        for attempt in range(5):
            try:
                conn.execute("""
                    INSERT INTO agent_candidates
                        (round_number, token, data, tier, passed, hard_blocks, pass_count, signal_key)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (
                    round_number, item["token"], item["data"], item.get("tier"),
                    item.get("passed", 0), item.get("hard_blocks", "[]"),
                    item.get("pass_count", 0), item.get("signal_key"),
                ))
                break
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() and attempt < 4:
                    _t.sleep(1 + attempt * 0.5)
                else:
                    raise


def agent_candidates_get_latest(conn, rounds: int = 2) -> list[dict]:
    """读取最近 N 轮的候选币，按 token 去重取每币最新一条。"""
    cur = conn.execute("""
        SELECT a.* FROM agent_candidates a
        INNER JOIN (
            SELECT token, MAX(id) AS max_id
            FROM agent_candidates
            WHERE round_number > (
                SELECT COALESCE(MAX(round_number), 0) - ? FROM agent_candidates
            )
            GROUP BY token
        ) b ON a.id = b.max_id
        ORDER BY a.id
    """, (rounds - 1,))
    return [dict(r) for r in cur.fetchall()]


def agent_candidates_purge_old(conn, keep_last_rounds: int = 20):
    conn.execute("""
        DELETE FROM agent_candidates
        WHERE round_number <= (
            SELECT COALESCE(MAX(round_number), 0) - ? FROM agent_candidates
        )
    """, (keep_last_rounds,))


# === 收藏锚定 ===

def entry_get(conn, token: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM watchlist_entries WHERE token = ?",
        (token.upper(),)
    ).fetchone()
    return dict(row) if row else None


def entry_upsert(conn, token: str, anchor_price: float,
                 anchor_snapshot_json: str, anchor_analysis_json: str):
    """收藏时调用：记录锚定价和快照"""
    conn.execute("""
        INSERT INTO watchlist_entries
            (token, anchored_at, anchor_price, anchor_snapshot, anchor_analysis,
             max_drawdown, peak_profit, archived)
        VALUES (?, CURRENT_TIMESTAMP, ?, ?, ?, 0, 0, 0)
        ON CONFLICT(token) DO NOTHING
    """, (token.upper(), anchor_price, anchor_snapshot_json, anchor_analysis_json))


def entry_delete(conn, token: str):
    conn.execute("DELETE FROM watchlist_entries WHERE token = ?", (token.upper(),))
    conn.execute("DELETE FROM watchlist_followups WHERE token = ?", (token.upper(),))


def entry_update_extremes(conn, token: str, pnl_pct: float):
    """用新的浮盈浮亏值更新该代币的历史极值"""
    row = entry_get(conn, token)
    if not row:
        return
    max_dd = min(row.get("max_drawdown") or 0, pnl_pct)
    peak   = max(row.get("peak_profit") or 0, pnl_pct)
    conn.execute("""
        UPDATE watchlist_entries
        SET max_drawdown = ?, peak_profit = ?
        WHERE token = ?
    """, (max_dd, peak, token.upper()))


def followup_add(conn, token: str, price: float, pnl_pct: float,
                 snapshot_json: str, analysis_json: str):
    conn.execute("""
        INSERT INTO watchlist_followups
            (token, price, pnl_pct, snapshot, analysis)
        VALUES (?, ?, ?, ?, ?)
    """, (token.upper(), price, pnl_pct, snapshot_json, analysis_json))


def followup_get_all(conn, token: str) -> list[dict]:
    cur = conn.execute("""
        SELECT id, recorded_at, price, pnl_pct, snapshot, analysis
        FROM watchlist_followups
        WHERE token = ?
        ORDER BY id ASC
    """, (token.upper(),))
    return [dict(r) for r in cur.fetchall()]


# === 负面样本归档 ===

def archive_loss_sample(conn, token: str, bottom_price: float, max_drawdown: float):
    """把一条入场+后续序列归档为负面样本"""
    entry = entry_get(conn, token)
    if not entry:
        return
    followups = followup_get_all(conn, token)
    import json as _json
    conn.execute("""
        INSERT INTO loss_samples
            (token, anchored_at, anchor_price, bottom_price, max_drawdown,
             anchor_snapshot, anchor_analysis, followup_count, followups_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        token.upper(),
        entry.get("anchored_at"),
        entry.get("anchor_price"),
        bottom_price,
        max_drawdown,
        entry.get("anchor_snapshot"),
        entry.get("anchor_analysis"),
        len(followups),
        _json.dumps(followups, default=str, ensure_ascii=False),
    ))
    # 标记已归档
    conn.execute("UPDATE watchlist_entries SET archived = 1 WHERE token = ?",
                 (token.upper(),))


def loss_samples_stats(conn, feature_filter: dict | None = None) -> dict:
    """统计已归档的负面样本的共性
    这个简化版只返回一些基础统计，不做复杂的特征挖掘（留给以后扩展）
    """
    cur = conn.execute("""
        SELECT token, max_drawdown, anchor_analysis, followup_count
        FROM loss_samples
    """)
    samples = [dict(r) for r in cur.fetchall()]
    if not samples:
        return {"count": 0}

    import json as _json
    avg_drawdown = sum(s["max_drawdown"] or 0 for s in samples) / len(samples)

    # 统计入场 verdict 的分布
    verdict_count = {}
    direction_count = {}
    for s in samples:
        try:
            a = _json.loads(s["anchor_analysis"] or "{}")
            v = a.get("verdict", "?")
            d = a.get("direction", "?")
            verdict_count[v] = verdict_count.get(v, 0) + 1
            direction_count[d] = direction_count.get(d, 0) + 1
        except Exception:
            continue

    return {
        "count": len(samples),
        "avg_drawdown_pct": round(avg_drawdown, 2),
        "anchor_verdict_distribution": verdict_count,
        "anchor_direction_distribution": direction_count,
    }


# === Trading settings / positions ===

def trading_settings_defaults() -> dict:
    return {
        "enabled": config.TRADING_ENABLED,
        "mode": config.TRADING_MODE,
        "initial_balance": config.TRADING_INITIAL_BALANCE,
        "leverage": config.TRADING_LEVERAGE,
        "order_amount": config.TRADING_ORDER_AMOUNT,
        "agent_collect_interval_minutes": getattr(config, "AGENT_COLLECT_INTERVAL_MINUTES", 15),
        "agent_trigger_interval": getattr(config, "HEAT_AGENT_TRIGGER_INTERVAL", 3),
        "strategy_initial_agent": getattr(config, "STRATEGY_INITIAL_AGENT", 1000),
        "strategy_initial_heat_agent": getattr(config, "STRATEGY_INITIAL_HEAT_AGENT", 1000),
        "strategy_initial_heat_agent_lessons": getattr(config, "STRATEGY_INITIAL_HEAT_AGENT_LESSONS", 1000),
        "strategy_initial_system": getattr(config, "STRATEGY_INITIAL_SYSTEM", 1000),
        "strategy_initial_manual": getattr(config, "STRATEGY_INITIAL_MANUAL", 1000),
        "strategy_initial_kol_agent": getattr(config, "STRATEGY_INITIAL_KOL_AGENT", 1000),
        "strategy_initial_agent_no_lessons": getattr(config, "STRATEGY_INITIAL_AGENT_NO_LESSONS", 1000),
        "kol_agent_interval_minutes": getattr(config, "KOL_AGENT_INTERVAL_MINUTES", 15),
        "nl_agent_interval_minutes": getattr(config, "NL_AGENT_INTERVAL_MINUTES", 30),
        "heat_agent_lessons_trigger_interval": getattr(config, "HEAT_AGENT_LESSONS_TRIGGER_INTERVAL", 3),
        "ai_regime_interval_minutes": getattr(config, "AI_REGIME_INTERVAL_MINUTES", 30),
        "kol_llm_provider": getattr(config, "KOL_LLM_PROVIDER", "deepseek"),
        "kol_agent_min_confidence": getattr(config, "KOL_AGENT_MIN_CONFIDENCE", 70),
        "kol_token_cooldown_minutes": getattr(config, "KOL_TOKEN_COOLDOWN_MINUTES", 30),
        "agent_trade_enabled": getattr(config, "AGENT_TRADE_ENABLED", True),
        "heat_agent_enabled": getattr(config, "HEAT_AGENT_ENABLED", True),
        "kol_agent_enabled": getattr(config, "KOL_AGENT_ENABLED", True),
        "kol_snapshot_enabled": getattr(config, "KOL_SNAPSHOT_ENABLED", True),
        "strategy_initial_kol_snapshot": getattr(config, "STRATEGY_INITIAL_KOL_SNAPSHOT", 1000),
        "kol_snapshot_interval_minutes": getattr(config, "KOL_SNAPSHOT_INTERVAL_MINUTES", 8),
        "kol_snapshot_min_confidence": getattr(config, "KOL_SNAPSHOT_MIN_CONFIDENCE", 70),
        "kol_snapshot_llm_provider": getattr(config, "KOL_SNAPSHOT_LLM_PROVIDER", "nvidia"),
        "nl_agent_enabled": getattr(config, "NL_AGENT_ENABLED", True),
        "heat_agent_lessons_enabled": getattr(config, "HEAT_AGENT_LESSONS_ENABLED", True),
        "trading_daily_limit_enabled": getattr(config, "TRADING_DAILY_LIMIT_ENABLED", True),
        "limit_order_timeout_seconds": getattr(config, "LIMIT_ORDER_TIMEOUT_SECONDS", 600),
        "system_auto_trade_enabled": getattr(config, "SYSTEM_AUTO_TRADE_ENABLED", True),
    }


def trading_settings_get(conn) -> dict:
    settings = trading_settings_defaults()
    rows = conn.execute("SELECT key, value FROM trading_settings").fetchall()
    for row in rows:
        raw = row["value"]
        if row["key"] in {"enabled", "agent_trade_enabled", "heat_agent_enabled", "kol_agent_enabled", "kol_snapshot_enabled", "nl_agent_enabled", "heat_agent_lessons_enabled", "trading_daily_limit_enabled", "system_auto_trade_enabled"}:
            settings[row["key"]] = str(raw).lower() in {"1", "true", "yes", "on"}
        elif row["key"] in {"initial_balance", "leverage", "order_amount", "kol_agent_min_confidence", "kol_token_cooldown_minutes", "limit_order_timeout_seconds", "kol_snapshot_min_confidence"}:
            try:
                settings[row["key"]] = float(raw)
            except (TypeError, ValueError):
                pass
        else:
            settings[row["key"]] = raw
    settings["leverage"] = int(settings.get("leverage") or config.TRADING_LEVERAGE)
    settings["kol_agent_min_confidence"] = int(settings.get("kol_agent_min_confidence") or 70)
    settings["kol_snapshot_min_confidence"] = int(settings.get("kol_snapshot_min_confidence") or 70)
    settings["kol_token_cooldown_minutes"] = int(settings.get("kol_token_cooldown_minutes") or 30)
    return settings


def trading_settings_update(conn, fields: dict):
    allowed = {"enabled", "mode", "initial_balance", "leverage", "order_amount", "agent_collect_interval_minutes", "agent_trigger_interval", "strategy_initial_agent", "strategy_initial_heat_agent", "strategy_initial_heat_agent_lessons", "strategy_initial_system", "strategy_initial_manual", "strategy_initial_kol_agent", "strategy_initial_agent_no_lessons", "kol_agent_interval_minutes", "nl_agent_interval_minutes", "heat_agent_lessons_trigger_interval", "ai_regime_interval_minutes", "kol_llm_provider", "kol_agent_min_confidence", "kol_token_cooldown_minutes", "agent_trade_enabled", "heat_agent_enabled", "kol_agent_enabled", "nl_agent_enabled", "heat_agent_lessons_enabled", "trading_daily_limit_enabled", "system_auto_trade_enabled", "limit_order_timeout_seconds", "kol_snapshot_enabled", "kol_snapshot_interval_minutes", "strategy_initial_kol_snapshot", "kol_snapshot_min_confidence", "kol_snapshot_llm_provider"}
    rows = []
    for key, value in fields.items():
        if key in allowed:
            rows.append((key, str(value)))
    conn.executemany("""
        INSERT INTO trading_settings (key, value, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at=excluded.updated_at
    """, rows)


def trade_open_positions(conn) -> list[dict]:
    cur = conn.execute("""
        SELECT * FROM trade_positions
        WHERE status IN ('PENDING', 'OPEN', 'PARTIAL')
        ORDER BY id DESC
    """)
    return [dict(r) for r in cur.fetchall()]


def trade_positions_all(conn, limit: int = 50) -> list[dict]:
    cur = conn.execute("""
        SELECT * FROM trade_positions
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    return [dict(r) for r in cur.fetchall()]


def trade_positions_with_kol_enrichment(conn) -> list[dict]:
    """对所有持仓，对 kol_agent/snapshot 策略补 entry_trend/confidence"""
    positions = trade_positions_all(conn, limit=10000)
    with get_conn(config.KOL_DB) as kol_conn, \
         get_conn(config.SNAPSHOT_DB) as snap_conn:
        for p in positions:
            if p.get("strategy") not in ("kol_agent", "kol_snapshot"):
                continue
            token = p["token"]
            c = snap_conn if p.get("strategy") == "kol_snapshot" else kol_conn
            row = c.execute(
                "SELECT trend, confidence FROM kol_analyses WHERE token=? AND created_at <= ? ORDER BY id DESC LIMIT 1",
                (token, p["created_at"]),
            ).fetchone()
            if row:
                p["entry_trend"] = row["trend"]
                p["entry_confidence"] = row["confidence"]
            row = c.execute(
                "SELECT trend, confidence FROM kol_analyses WHERE token=? ORDER BY id DESC LIMIT 1",
                (token,),
            ).fetchone()
            if row:
                p["latest_trend"] = row["trend"]
                p["latest_confidence"] = row["confidence"]
    return positions


def trade_has_active(conn, token: str, strategy: str = None) -> bool:
    if strategy:
        row = conn.execute("""
            SELECT 1 FROM trade_positions
            WHERE token = ? AND strategy = ? AND status IN ('PENDING', 'OPEN', 'PARTIAL')
            LIMIT 1
        """, (token.upper(), strategy)).fetchone()
    else:
        row = conn.execute("""
            SELECT 1 FROM trade_positions
            WHERE token = ? AND status IN ('PENDING', 'OPEN', 'PARTIAL')
            LIMIT 1
        """, (token.upper(),)).fetchone()
    return row is not None


def trade_signal_lock_acquire(conn, token: str, signal_key: str, strategy: str = "agent") -> bool:
    try:
        conn.execute("""
            INSERT INTO trade_signal_locks (token, signal_key, strategy)
            VALUES (?, ?, ?)
        """, (token.upper(), signal_key, strategy))
        return True
    except sqlite3.IntegrityError:
        return False


def trade_signal_lock_release(conn, token: str, strategy: str):
    """平仓时释放对应策略的信号锁"""
    conn.execute(
        "DELETE FROM trade_signal_locks WHERE token=? AND strategy=?",
        (token.upper(), strategy),
    )


def trade_signal_lock_cleanup(conn, retention_hours: int = 72) -> int:
    """清理超过 retention_hours 的旧 signal_lock 记录。返回删除条数。"""
    cur = conn.execute("""
        DELETE FROM trade_signal_locks
        WHERE created_at < datetime('now', ?)
    """, (f"-{retention_hours} hours",))
    return cur.rowcount or 0


def trade_count_today_opened(conn) -> int:
    """今日（UTC）开过多少仓（按 created_at 统计，含已平仓的）"""
    row = conn.execute("""
        SELECT COUNT(*) AS n FROM trade_positions
        WHERE date(created_at) = date('now')
    """).fetchone()
    return int(row["n"] or 0) if row else 0


def trade_realized_pnl_today(conn) -> float:
    """今日（UTC）已实现盈亏（按 closed_at 统计）"""
    row = conn.execute("""
        SELECT COALESCE(SUM(realized_pnl), 0) AS pnl FROM trade_positions
        WHERE closed_at IS NOT NULL
          AND date(closed_at) = date('now')
    """).fetchone()
    return float(row["pnl"] or 0) if row else 0.0


def trade_last_stop_loss_map(conn, hours: int = 24) -> dict:
    """
    返回 {token: last_stop_loss_closed_at(str)} —— 最近 hours 小时内因止损平仓的 token
    用 advice 或 failed_reason 粗略识别"止损"
    """
    cur = conn.execute("""
        SELECT token, MAX(closed_at) AS closed_at
        FROM trade_positions
        WHERE closed_at IS NOT NULL
          AND closed_at > datetime('now', ?)
          AND (advice LIKE '%止损%' OR status = 'CLOSED' AND realized_pnl < 0)
        GROUP BY token
    """, (f"-{hours} hours",))
    result = {}
    for row in cur.fetchall():
        if row["closed_at"]:
            result[row["token"].upper()] = row["closed_at"]
    return result


def trade_open_positions_by_sector(conn) -> dict:
    """返回 {sector: count}。需要在调用方用 risk.sector_of 做映射。"""
    # 这个函数只返回 token list，分类交给 risk 模块
    rows = conn.execute("""
        SELECT token FROM trade_positions
        WHERE status IN ('PENDING', 'OPEN', 'PARTIAL')
    """).fetchall()
    return [row["token"] for row in rows]


def trade_position_insert(conn, position: dict):
    try:
        if "lowest_price" not in position:
            position["lowest_price"] = None
        conn.execute("""
        INSERT INTO trade_positions
            (token, symbol, side, status, mode, margin_amount, leverage, notional,
             quantity, entry_price, limit_price, current_price, stop_loss_price,
             tp1_price, tp2_price, highest_price, lowest_price, trailing_stop_price,
             signal_snapshot, open_reason, advice, strategy)
        VALUES
            (:token, :symbol, :side, :status, :mode, :margin_amount, :leverage,
             :notional, :quantity, :entry_price, :limit_price, :current_price,
             :stop_loss_price, :tp1_price, :tp2_price, :highest_price, :lowest_price,
             :trailing_stop_price, :signal_snapshot, :open_reason, :advice,
             :strategy)
        """, position)
        return True
    except sqlite3.IntegrityError:
        return False


def trade_position_update(conn, position_id: int, fields: dict):
    fields = {k: v for k, v in fields.items() if k != "id"}
    fields["updated_at"] = "__CURRENT_TIMESTAMP__"
    sets = []
    params = []
    for key, value in fields.items():
        if value == "__CURRENT_TIMESTAMP__":
            sets.append(f"{key}=CURRENT_TIMESTAMP")
        else:
            sets.append(f"{key}=?")
            params.append(value)
    params.append(position_id)
    conn.execute(f"UPDATE trade_positions SET {', '.join(sets)} WHERE id=?", params)


def trade_loss_archive_add(conn, sample: dict):
    conn.execute("""
        INSERT OR IGNORE INTO trade_loss_archive
            (position_id, token, symbol, entry_price, exit_price, realized_pnl,
             pnl_pct, failed_reason, reason_tags, entry_snapshot, exit_snapshot)
        VALUES
            (:position_id, :token, :symbol, :entry_price, :exit_price,
             :realized_pnl, :pnl_pct, :failed_reason, :reason_tags,
             :entry_snapshot, :exit_snapshot)
    """, sample)


def trade_loss_archive_recent(conn, limit: int = 50) -> list[dict]:
    cur = conn.execute("""
        SELECT * FROM trade_loss_archive
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))
    return [dict(r) for r in cur.fetchall()]


def trade_loss_archive_stats(conn) -> dict:
    rows = trade_loss_archive_recent(conn, limit=500)
    if not rows:
        return {"count": 0, "tag_counts": {}, "recent": []}
    import json as _json
    tag_counts = {}
    for row in rows:
        try:
            tags = _json.loads(row.get("reason_tags") or "[]")
        except Exception:
            tags = []
        for tag in tags:
            tag_counts[tag] = tag_counts.get(tag, 0) + 1
    return {
        "count": len(rows),
        "tag_counts": tag_counts,
        "recent": rows[:10],
    }


def trade_reset_all(conn, new_initial_balance: float | None = None) -> dict:
    """
    一键重置：清空所有交易记录，回到账户初始状态。

    清理内容：
      - 所有持仓（含 PENDING / OPEN / PARTIAL / CLOSED / CANCELED）
      - signal_lock 防重复表
      - pending_decisions 待处理决策
      - agent_candidates 候选池

    保留：
      - trading_settings 配置（enabled / mode / leverage 等）
      - lessons 教训库（真金白银买来的经验）
      - journal 操作日志（历史记录）
      - trade_loss_archive 止损归档样本（历史记录）
      - 如传入 new_initial_balance，同时更新初始余额

    返回：各表删除的行数 + 新配置
    """
    # 旧日志标记失效
    conn.execute("UPDATE journal SET reviewed = 1")
    positions_deleted = conn.execute("DELETE FROM trade_positions").rowcount or 0
    locks_deleted = conn.execute("DELETE FROM trade_signal_locks").rowcount or 0
    decisions_deleted = conn.execute("DELETE FROM pending_decisions").rowcount or 0

    # 候选池和分析记录在 agent DB
    candidates_deleted = 0
    with get_conn(config.AGENT_MAIN_DB) as ac:
        candidates_deleted += ac.execute("DELETE FROM agent_candidates").rowcount or 0
    with get_conn(config.NL_DB) as ac:
        candidates_deleted += ac.execute("DELETE FROM nl_candidates").rowcount or 0
    with get_conn(config.KOL_DB) as ac:
        candidates_deleted += ac.execute("DELETE FROM kol_candidates").rowcount or 0
        ac.execute("DELETE FROM kol_analyses")
        ac.execute("DELETE FROM kol_llm_logs")
    with get_conn(config.SNAPSHOT_DB) as ac:
        candidates_deleted += ac.execute("DELETE FROM kol_candidates").rowcount or 0
        ac.execute("DELETE FROM kol_analyses")
        ac.execute("DELETE FROM kol_llm_logs")

    # AUTOINCREMENT 计数器也重置
    for tbl in ("trade_positions", "trade_signal_locks", "pending_decisions"):
        conn.execute("DELETE FROM sqlite_sequence WHERE name = ?", (tbl,))
    with get_conn(config.AGENT_MAIN_DB) as ac:
        ac.execute("DELETE FROM sqlite_sequence WHERE name = 'agent_candidates'")

    if new_initial_balance is not None and new_initial_balance > 0:
        trading_settings_update(conn, {"initial_balance": new_initial_balance})

    settings = trading_settings_get(conn)
    return {
        "positions_deleted": positions_deleted,
        "locks_deleted": locks_deleted,
        "decisions_deleted": decisions_deleted,
        "candidates_deleted": candidates_deleted,
        "settings": settings,
    }


def trade_reset_strategy(conn, strategy: str, new_initial_balance: float | None = None) -> dict:
    """
    按策略重置：只清该策略的数据，不影响其他策略。

    清理内容：
      - trade_positions WHERE strategy = ?
      - trade_signal_locks WHERE strategy = ?
      - pending_decisions WHERE source = ?
      - 策略专属候选池表（agent→agent_candidates, nl→nl_candidates, kol→kol_candidates）
      - KOL 专属表（kol_analyses, kol_llm_logs）

    保留：
      - lessons / journal / trade_loss_archive
      - 其他策略的全部数据
    """
    source_map = {
        "agent": "agent_candidates",
        "heat_agent": "token_heat_history",
        "heat_agent_lessons": "token_heat_history_lessons",
        "agent_no_lessons": "nl_candidates",
        "kol_agent": "kol_agent",
        "kol_snapshot": "kol_snapshot",
    }

    # 更新策略初始金额
    balance_key = f"strategy_initial_{strategy}"
    if new_initial_balance is not None and new_initial_balance > 0:
        trading_settings_update(conn, {balance_key: new_initial_balance})

    journal_marked = conn.execute(
        "UPDATE journal SET reviewed = 1 WHERE order_id IN "
        "(SELECT id FROM trade_positions WHERE strategy = ?)", (strategy,)
    ).rowcount or 0

    pos_del = conn.execute(
        "DELETE FROM trade_positions WHERE strategy = ?", (strategy,)
    ).rowcount or 0
    lock_del = conn.execute(
        "DELETE FROM trade_signal_locks WHERE strategy = ?", (strategy,)
    ).rowcount or 0

    src = source_map.get(strategy, "")
    dec_del = 0
    if src:
        dec_del = conn.execute(
            "DELETE FROM pending_decisions WHERE source = ?", (src,)
        ).rowcount or 0

    cand_del = 0
    if strategy == "agent":
        with get_conn(config.AGENT_MAIN_DB) as ac:
            cand_del = ac.execute("DELETE FROM agent_candidates").rowcount or 0
    elif strategy == "agent_no_lessons":
        with get_conn(config.NL_DB) as ac:
            cand_del = ac.execute("DELETE FROM nl_candidates").rowcount or 0
    elif strategy == "kol_agent":
        with get_conn(config.KOL_DB) as ac:
            cand_del = ac.execute("DELETE FROM kol_candidates").rowcount or 0
            ac.execute("DELETE FROM kol_analyses WHERE strategy = 'kol_agent'")
            ac.execute("DELETE FROM kol_llm_logs")  # 无 strategy 列，物理隔离=过滤
    elif strategy == "kol_snapshot":
        with get_conn(config.SNAPSHOT_DB) as ac:
            cand_del = ac.execute("DELETE FROM kol_candidates").rowcount or 0
            ac.execute("DELETE FROM kol_analyses WHERE strategy = 'kol_snapshot'")
            ac.execute("DELETE FROM kol_llm_logs")  # 无 strategy 列，物理隔离=过滤

    settings = trading_settings_get(conn)
    return {
        "strategy": strategy,
        "positions_deleted": pos_del,
        "locks_deleted": lock_del,
        "decisions_deleted": dec_del,
        "candidates_deleted": cand_del,
        "settings": settings,
    }


# === Agent 教训库 ===

def lessons_add(conn, lesson: dict):
    """Agent 写入一条教训（亏损单复盘）"""
    conn.execute("""
        INSERT INTO lessons
            (order_id, token, direction, entry_price, exit_price, pnl_pct,
             market_snapshot, macro_context, signal_error, what_missed,
             root_cause, lesson, rule_update, severity)
        VALUES
            (:order_id, :token, :direction, :entry_price, :exit_price, :pnl_pct,
             :market_snapshot, :macro_context, :signal_error, :what_missed,
             :root_cause, :lesson, :rule_update, :severity)
    """, lesson)


def lessons_query(conn, token: str = None, learned: int = 0) -> list[dict]:
    """查询教训。token=None 查全局教训（symbol='*'），learned=0 只查仍适用的。"""
    if token:
        rows = conn.execute(
            "SELECT * FROM lessons WHERE token = ? AND learned = ? "
            "ORDER BY severity DESC, created_at DESC",
            (token.upper(), learned),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM lessons WHERE learned = ? "
            "ORDER BY severity DESC, created_at DESC",
            (learned,),
        ).fetchall()
    return [dict(r) for r in rows]


def lessons_recent(conn, limit: int = 20) -> list[dict]:
    """最近 N 条教训（含已学习的）"""
    rows = conn.execute(
        "SELECT * FROM lessons ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def lessons_stats(conn, strategy: str = None) -> dict:
    """教训库统计（策略隔离）"""
    if strategy:
        rows = conn.execute("SELECT * FROM lessons WHERE strategy=?", (strategy,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM lessons").fetchall()
    if not rows:
        return {"count": 0}
    import json as _json
    severity_dist = {}
    cause_dist = {}
    for r in rows:
        sev = r["severity"] or "medium"
        severity_dist[sev] = severity_dist.get(sev, 0) + 1
        cause = r["root_cause"] or "unknown"
        cause_dist[cause] = cause_dist.get(cause, 0) + 1
    return {
        "count": len(rows),
        "active": sum(1 for r in rows if r["learned"] == 0),
        "severity_dist": severity_dist,
        "cause_dist": cause_dist,
    }


def lessons_mark_learned(conn, lesson_id: int):
    """标记某条教训已被新规则覆盖"""
    conn.execute("UPDATE lessons SET learned = 1 WHERE id = ?", (lesson_id,))


# === Agent 操作日志 ===

def journal_add(conn, entry: dict):
    """写入一条操作日志（开仓 or 平仓）"""
    conn.execute("""
        INSERT INTO journal
            (order_id, token, action, price, tier, stop_loss, tp1_price, tp2_price,
             reason, dimension_data, market_overview, lesson_checked,
             pnl_pct, close_reason, hold_duration,
             pending_decision_id, source_round, social_score)
        VALUES
            (:order_id, :token, :action, :price, :tier, :stop_loss, :tp1_price, :tp2_price,
             :reason, :dimension_data, :market_overview, :lesson_checked,
             :pnl_pct, :close_reason, :hold_duration,
             :pending_decision_id, :source_round, :social_score)
    """, entry)


def journal_add_open(conn, position_id: int, token: str, price: float,
                     tier: str, stop_loss: float, tp1_price: float, tp2_price: float,
                     reason: str, dimension_data: str = None,
                     market_overview: str = None, lesson_checked: str = None,
                     pending_decision_id: int = None, source_round: int = None,
                     social_score: float = None):
    """系统开仓后自动写 journal。从 pending_decisions 读取 Agent 填写的字段。"""
    journal_add(conn, {
        "order_id": position_id, "token": token, "action": "open", "price": price,
        "tier": tier, "stop_loss": stop_loss, "tp1_price": tp1_price,
        "tp2_price": tp2_price, "reason": reason,
        "dimension_data": dimension_data, "market_overview": market_overview,
        "lesson_checked": lesson_checked,
        "pnl_pct": None, "close_reason": None, "hold_duration": None,
        "pending_decision_id": pending_decision_id,
        "source_round": source_round, "social_score": social_score,
    })


def journal_add_close(conn, order_id: int, token: str, price: float,
                      reason: str, pnl_pct: float = None,
                      close_reason: str = "system",
                      hold_duration: str = None,
                      dimension_data: str = None,
                      market_overview: str = None,
                      pending_decision_id: int = None):
    """系统平仓后自动写 journal。平仓时的市场快照由调用方传入。"""
    journal_add(conn, {
        "order_id": order_id, "token": token, "action": "close", "price": price,
        "tier": None, "stop_loss": None, "tp1_price": None, "tp2_price": None,
        "reason": reason, "dimension_data": dimension_data,
        "market_overview": market_overview, "lesson_checked": None,
        "pnl_pct": pnl_pct, "close_reason": close_reason,
        "hold_duration": hold_duration,
        "pending_decision_id": pending_decision_id,
        "source_round": None, "social_score": None,
    })


def journal_query(conn, token: str = None, action: str = None,
                  since: str = None, limit: int = 50) -> list[dict]:
    """查询日志。可按 token / action / 时间过滤。"""
    sql = "SELECT * FROM journal WHERE 1=1"
    params = []
    if token:
        sql += " AND token = ?"
        params.append(token.upper())
    if action:
        sql += " AND action = ?"
        params.append(action)
    if since:
        sql += " AND created_at >= ?"
        params.append(since)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def journal_mark_reviewed(conn, journal_ids: list[int]):
    """标记 journal 行已复盘"""
    if not journal_ids:
        return
    ph = ",".join("?" * len(journal_ids))
    conn.execute(
        f"UPDATE journal SET reviewed = 1 WHERE id IN ({ph})",
        journal_ids,
    )


def journal_unreviewed(conn, limit: int = 200) -> list[dict]:
    """读取未复盘的 journal 行"""
    rows = conn.execute(
        "SELECT * FROM journal WHERE reviewed = 0 ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def journal_today(conn) -> list[dict]:
    """今天所有日志"""
    rows = conn.execute(
        "SELECT * FROM journal WHERE date(created_at) = date('now') ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def journal_stats(conn) -> dict:
    """日志统计"""
    total = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    today = conn.execute(
        "SELECT COUNT(*) FROM journal WHERE date(created_at) = date('now')"
    ).fetchone()[0]
    opens = conn.execute(
        "SELECT COUNT(*) FROM journal WHERE action = 'open'"
    ).fetchone()[0]
    closes = conn.execute(
        "SELECT COUNT(*) FROM journal WHERE action = 'close'"
    ).fetchone()[0]
    return {"total": total, "today": today, "opens": opens, "closes": closes}


# === Agent 候选币池 ===

def watchlist_followups_purge_old(conn, days: int = 3):
    """清理旧的观察列表跟踪数据（浮盈浮亏历史快照）"""
    conn.execute(
        "DELETE FROM watchlist_followups WHERE recorded_at < datetime('now', ?)",
        (f"-{days} days",)
    )




# === KOL Agent ===

def kol_analysis_insert(conn, analysis: dict, agent_db: str = None):
    import json as _json
    c = conn
    if agent_db:
        os.makedirs(os.path.dirname(agent_db) or ".", exist_ok=True)
        c = sqlite3.connect(agent_db, timeout=30.0)
        c.execute("PRAGMA journal_mode = WAL")
    c.execute(
        """INSERT INTO kol_analyses
            (token, trend, timeline, price_levels, summary, reasoning,
             position_analysis, timing, risk_control, direction, confidence,
             reason, llm_log_id, action, status, context_tag, evidence_tags, strategy, missing_data)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            analysis.get("token", ""), analysis.get("trend"),
            _json.dumps(analysis.get("timeline"), ensure_ascii=False),
            _json.dumps(analysis.get("price_levels"), ensure_ascii=False),
            analysis.get("summary"),
            _json.dumps(analysis.get("reasoning"), ensure_ascii=False),
            analysis.get("position_analysis"),
            analysis.get("timing"),
            _json.dumps(analysis.get("risk_control"), ensure_ascii=False),
            analysis.get("direction"), analysis.get("confidence"),
            analysis.get("reason"),
            analysis.get("llm_log_id"),
            None,
            analysis.get("status"),
            analysis.get("context_tag"),
            _json.dumps(analysis.get("evidence_tags"), ensure_ascii=False),
            analysis.get("strategy", "kol_agent"),
            analysis.get("missing_data", "无"),
        ),
    )
    if agent_db:
        c.commit()
        c.close()


def kol_analyses_latest(conn, symbol: str = "", strategy: str = ""):
    import json as _json
    q = (
        "SELECT id, token, trend, timeline, price_levels, "
        "summary, reasoning, position_analysis, timing, risk_control, direction, confidence, reason, "
        "action, status, context_tag, evidence_tags, missing_data, "
        "strategy, created_at "
        "FROM kol_analyses "
        "WHERE created_at >= datetime('now', '-12 hours') "
    )
    params: list = []
    if strategy:
        q += " AND strategy = ? "
        params.append(strategy)
    if symbol:
        q += " AND token = ? "
        params.append(symbol.upper())
    q += " ORDER BY id DESC"
    if symbol:
        q += " LIMIT 8"
    rows = conn.execute(q, tuple(params) if params else ()).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        for field in ("timeline", "price_levels", "risk_control", "evidence_tags", "reasoning"):
            try:
                d[field] = _json.loads(d[field]) if d[field] else None
            except (Exception):
                pass

        # --- 字段映射: 旧列 → 新字段 (fallback 兼容迁移前数据) ---
        # summary: 新字段优先, 空则取 position_analysis / reason
        if not d.get("summary"):
            d["summary"] = d.get("position_analysis") or d.get("reason") or ""

        # reasoning: 新字段优先, 空则用旧 timing + risk_control 拼凑
        rs = d.get("reasoning")
        if not isinstance(rs, dict):
            rs = {}
        if not (rs.get("wz") or rs.get("sj") or rs.get("fk")):
            if d.get("timing"):
                rs["sj"] = d["timing"]
            rc = d.get("risk_control")
            if isinstance(rc, dict):
                parts = [v for v in rc.values() if v]
                if parts:
                    rs["fk"] = "；".join(parts)
        d["reasoning"] = rs if (rs.get("wz") or rs.get("sj") or rs.get("fk")) else None

        # action: 新字段优先, 空则用 direction / confidence 拼接 "LONG / 75"
        if not d.get("action"):
            dir_upper = (d.get("direction") or "").upper()
            conf = d.get("confidence", "")
            if dir_upper:
                d["action"] = f"{dir_upper} / {conf}"
        # --- 映射结束 ---

        results.append(d)
    return results


def kol_llm_log_insert(conn, log: dict, agent_db: str = None):
    import time as _t
    c = conn
    close_after = False
    if agent_db:
        os.makedirs(os.path.dirname(agent_db) or ".", exist_ok=True)
        c = sqlite3.connect(agent_db, timeout=30.0)
        c.execute("PRAGMA journal_mode = WAL")
        close_after = True
    for attempt in range(5):
        try:
            c.execute(
                """INSERT INTO kol_llm_logs
                    (provider, model, candidate_count, prompt_chars, response_chars,
                     duration_ms, success, error, analyses_count,
                     system_prompt, user_prompt, raw_response)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (log.get("provider"), log.get("model"), log.get("candidate_count"),
                 log.get("prompt_chars"), log.get("response_chars"), log.get("duration_ms"),
                 log.get("success", 0), log.get("error"), log.get("analyses_count", 0),
                 log.get("system_prompt"), log.get("user_prompt"), log.get("raw_response")),
            )
            log_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            if close_after:
                c.commit()
                c.close()
            return log_id
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and attempt < 4:
                _t.sleep(1 + attempt * 0.5)
            else:
                if close_after:
                    c.close()
                raise


def kol_llm_logs_recent(conn, limit: int = 30, strategy: str = ""):
    if strategy:
        rows = conn.execute(
            "SELECT DISTINCT l.id, l.provider, l.model, l.candidate_count, l.prompt_chars, l.response_chars, "
            "l.duration_ms, l.success, l.error, l.analyses_count, l.created_at, "
            "COALESCE(GROUP_CONCAT(CASE WHEN a.missing_data != '无' THEN a.token || ':' || a.missing_data END, ' | '), '') AS missing_data "
            "FROM kol_llm_logs l "
            "JOIN kol_analyses a ON a.llm_log_id = l.id AND a.strategy = ? "
            "GROUP BY l.id ORDER BY l.id DESC LIMIT ?", (strategy, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, provider, model, candidate_count, prompt_chars, response_chars, "
            "duration_ms, success, error, analyses_count, created_at, '' AS missing_data "
            "FROM kol_llm_logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


# === KOL 候选币累积 ===

def kol_candidates_insert_batch(conn, round_number: int, batch: list[dict]):
    conn.executemany(
        """INSERT INTO kol_candidates
            (round_number, token, data, tier, passed, hard_blocks, pass_count, signal_key)
        VALUES (?,?,?,?,?,?,?,?)""",
        [(round_number, r["token"], r["data"], r.get("tier"), r.get("passed", 1),
          r.get("hard_blocks"), r.get("pass_count", 0), r.get("signal_key"))
         for r in batch],
    )


def kol_candidates_latest_round(conn):
    """返回最新一轮的候选币，data 字段不反序列化（由调用方处理）"""
    row = conn.execute(
        "SELECT MAX(round_number) FROM kol_candidates"
    ).fetchone()
    if not row or row[0] is None:
        return []
    round_num = row[0]
    return [dict(r) for r in conn.execute(
        "SELECT * FROM kol_candidates WHERE round_number=? ORDER BY id", (round_num,)
    ).fetchall()]


def kol_candidates_purge_old(conn, keep_last_rounds: int = 30):
    """只保留最近 N 轮数据"""
    row = conn.execute(
        "SELECT DISTINCT round_number FROM kol_candidates ORDER BY round_number DESC LIMIT 1 OFFSET ?",
        (keep_last_rounds,)
    ).fetchone()
    if row:
        conn.execute("DELETE FROM kol_candidates WHERE round_number <= ?", (row[0],))


# === 无教训版 Agent 候选币累积 ===

def nl_candidates_insert_batch(conn, round_number: int, batch: list[dict]):
    conn.executemany(
        """INSERT INTO nl_candidates
            (round_number, token, data, tier, passed, hard_blocks, pass_count, signal_key)
        VALUES (?,?,?,?,?,?,?,?)""",
        [(round_number, r["token"], r["data"], r.get("tier"), r.get("passed", 1),
          r.get("hard_blocks"), r.get("pass_count", 0), r.get("signal_key"))
         for r in batch],
    )


def nl_candidates_purge_old(conn, keep_last_rounds: int = 30):
    row = conn.execute(
        "SELECT DISTINCT round_number FROM nl_candidates ORDER BY round_number DESC LIMIT 1 OFFSET ?",
        (keep_last_rounds,)
    ).fetchone()
    if row:
        conn.execute("DELETE FROM nl_candidates WHERE round_number <= ?", (row[0],))
