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
    total_authors         INTEGER DEFAULT 0,
    data_source           TEXT,           -- "binance" / "okx"
    okx_tokens_count      INTEGER DEFAULT 0,
    okx_last_round        INTEGER
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
WHERE status IN ('PENDING', 'OPEN', 'PARTIAL') AND strategy != 'supertrend';
-- supertrend 豁免：反转市价开+挂单并存需要同币同向两笔活跃（OPEN+PENDING），
-- 加仓合并后由策略线程归并为一笔，重复开仓防护走应用层 trade_has_active

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

-- 链上监控 — 扫描器状态
CREATE TABLE IF NOT EXISTS onchain_scanner_status (
    key              TEXT PRIMARY KEY,
    stage            TEXT,
    detail           TEXT,
    round_number     INTEGER DEFAULT 0,
    tokens_scanned   INTEGER DEFAULT 0,
    candidates_found INTEGER DEFAULT 0,
    pool_size        INTEGER DEFAULT 0,
    actionable_size  INTEGER DEFAULT 0,
    last_scan_start  TIMESTAMP,
    last_scan_end    TIMESTAMP,
    last_heartbeat   TIMESTAMP
);

-- 链上监控 — 每轮快照（时序核心）
CREATE TABLE IF NOT EXISTS onchain_snapshots (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    round_number            INTEGER NOT NULL,
    token                   TEXT NOT NULL,
    token_address           TEXT NOT NULL,
    chain                   TEXT DEFAULT 'bsc',
    hot_rank                INTEGER,
    top10_hold_pct          REAL,
    holders_count           INTEGER,
    market_cap_usd          REAL,
    cluster_concentration   TEXT,
    top100_holdings_pct     REAL,
    holder_same_fund_pct    REAL,
    holder_new_addr_pct     REAL,
    holder_same_creation_pct REAL,
    rug_pull_pct            REAL,
    effective_concentration REAL,
    divergence_score        REAL,
    snapshot_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_onchain_snap_token ON onchain_snapshots(token, round_number);
CREATE INDEX IF NOT EXISTS idx_onchain_snap_round ON onchain_snapshots(round_number);

-- 链上监控 — LP 储备快照
CREATE TABLE IF NOT EXISTS onchain_lp_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT NOT NULL,
    token_address   TEXT NOT NULL,
    chain           TEXT DEFAULT 'bsc',
    round_number    INTEGER NOT NULL,
    token_amount    REAL,
    snapshot_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_onchain_lp_token ON onchain_lp_snapshots(token, round_number);

-- 链上监控 — 观察池 / 建仓池
CREATE TABLE IF NOT EXISTS onchain_pool (
    token                   TEXT PRIMARY KEY,
    token_address           TEXT NOT NULL,
    chain                   TEXT DEFAULT 'bsc',
    pool_stage              TEXT DEFAULT 'observation',
    surface_top10_pct       REAL,
    effective_concentration REAL,
    fund_source_same_pct    REAL,
    divergence_detected     INTEGER DEFAULT 0,
    days_in_pool            INTEGER DEFAULT 0,
    lp_reserve_trend        TEXT,
    lp_trend_score          REAL,
    gas_seeding_signal      INTEGER DEFAULT 0,
    cex_staging_signal      INTEGER DEFAULT 0,
    confidence_score        REAL DEFAULT 0,
    first_seen_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    rounds_in_pool          INTEGER DEFAULT 0,
    backtest_winrate        REAL
);
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


def _ensure_columns(conn, table: str, columns: list[tuple[str, str]]):
    """批量添加不存在的列（幂等）"""
    existing = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    for col_name, col_type in columns:
        if col_name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_type}")


def _migrate(conn):
    """老库迁移：加 first_seen_at 列 + 新表"""
    # 一币一活跃仓唯一索引：supertrend 豁免（反转市价开+挂单并存）。
    # 旧库索引含 supertrend → 条件重建一次（DROP IF EXISTS + CREATE IF NOT EXISTS 并发安全）
    idx_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='idx_trade_one_active_token'").fetchone()
    if idx_row and "strategy != 'supertrend'" not in (idx_row["sql"] or ""):
        conn.execute("DROP INDEX IF EXISTS idx_trade_one_active_token")
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_one_active_token
            ON trade_positions(token, side, strategy)
            WHERE status IN ('PENDING', 'OPEN', 'PARTIAL') AND strategy != 'supertrend'
        """)
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
    # 跟单托管状态：1=托管中 0=已取消托管 2=恢复中(待追平) 3=已脱离(交易员已无此仓)
    # 默认 1：存量行自动视为托管中，行为与加列前一致
    if "managed" not in tp_cols:
        conn.execute("ALTER TABLE trade_positions ADD COLUMN managed INTEGER DEFAULT 1")
    # 老库首次迁移无索引时建新版（旧版→新版的条件重建已在前面的检查中处理，
    # 此处绝不能无条件 DROP 重建，否则会把 supertrend 豁免版本覆盖回旧版）
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_one_active_token
        ON trade_positions(token, side, strategy)
        WHERE status IN ('PENDING', 'OPEN', 'PARTIAL') AND strategy != 'supertrend'
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
    # round_candidates 遗留表，无人读写的弃用数据（老库里有、**全新库没有** →
    # 不加保护时全新部署/换库/从备份恢复会在 init_db 直接抛 no such table 崩掉，
    # web 的 __main__ 里那次调用没兜底，进程起不来）
    try:
        conn.execute("DELETE FROM round_candidates")
    except sqlite3.OperationalError:
        pass
    # token_heat_history round_number 索引
    conn.execute("CREATE INDEX IF NOT EXISTS idx_heat_round ON token_heat_history(round_number)")
    # worker_status OKX 数据源列
    ws_cols = [r[1] for r in conn.execute("PRAGMA table_info(worker_status)").fetchall()]
    for col, typ in [("data_source", "TEXT"), ("okx_tokens_count", "INTEGER"),
                     ("okx_last_round", "INTEGER")]:
        if col not in ws_cols:
            conn.execute(f"ALTER TABLE worker_status ADD COLUMN {col} {typ}")

    # onchain pool: 后续新增列的迁移占位
    _ensure_columns(conn, "onchain_pool", [
        ("backtest_winrate", "REAL"),
        ("confidence_score", "REAL DEFAULT 0"),
        ("prev_new_addr_pct", "REAL"),
    ])
    _ensure_columns(conn, "onchain_snapshots", [
        ("divergence_score", "REAL"),
        ("effective_concentration", "REAL"),
        ("cluster_raw_json", "TEXT"),
    ])
    _ensure_columns(conn, "onchain_scanner_status", [
        ("actionable_size", "INTEGER DEFAULT 0"),
    ])


_ONCHAIN_DDL = """\
CREATE TABLE IF NOT EXISTS onchain_scanner_status (
    key              TEXT PRIMARY KEY,
    stage            TEXT,
    detail           TEXT,
    round_number     INTEGER DEFAULT 0,
    tokens_scanned   INTEGER DEFAULT 0,
    candidates_found INTEGER DEFAULT 0,
    pool_size        INTEGER DEFAULT 0,
    actionable_size  INTEGER DEFAULT 0,
    last_scan_start  TIMESTAMP,
    last_scan_end    TIMESTAMP,
    last_heartbeat   TIMESTAMP
);

CREATE TABLE IF NOT EXISTS onchain_snapshots (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    round_number            INTEGER NOT NULL,
    token                   TEXT NOT NULL,
    token_address           TEXT NOT NULL,
    chain                   TEXT DEFAULT 'bsc',
    hot_rank                INTEGER,
    top10_hold_pct          REAL,
    holders_count           INTEGER,
    market_cap_usd          REAL,
    cluster_concentration   TEXT,
    top100_holdings_pct     REAL,
    holder_same_fund_pct    REAL,
    holder_new_addr_pct     REAL,
    holder_same_creation_pct REAL,
    rug_pull_pct            REAL,
    effective_concentration REAL,
    divergence_score        REAL,
    snapshot_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_onchain_snap_token ON onchain_snapshots(token, round_number);
CREATE INDEX IF NOT EXISTS idx_onchain_snap_round ON onchain_snapshots(round_number);

CREATE TABLE IF NOT EXISTS onchain_lp_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT NOT NULL,
    token_address   TEXT NOT NULL,
    chain           TEXT DEFAULT 'bsc',
    round_number    INTEGER NOT NULL,
    token_amount    REAL,
    snapshot_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_onchain_lp_token ON onchain_lp_snapshots(token, round_number);

CREATE TABLE IF NOT EXISTS onchain_pool (
    token                   TEXT PRIMARY KEY,
    token_address           TEXT NOT NULL,
    chain                   TEXT DEFAULT 'bsc',
    pool_stage              TEXT DEFAULT 'observation',
    surface_top10_pct       REAL,
    effective_concentration REAL,
    fund_source_same_pct    REAL,
    divergence_detected     INTEGER DEFAULT 0,
    days_in_pool            INTEGER DEFAULT 0,
    lp_reserve_trend        TEXT,
    lp_trend_score          REAL,
    gas_seeding_signal      INTEGER DEFAULT 0,
    cex_staging_signal      INTEGER DEFAULT 0,
    confidence_score        REAL DEFAULT 0,
    prev_new_addr_pct       REAL,
    backtest_winrate        REAL,
    first_seen_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    rounds_in_pool          INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS onchain_clusters (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    round_number     INTEGER NOT NULL,
    token            TEXT NOT NULL,
    token_address    TEXT NOT NULL,
    chain            TEXT DEFAULT 'bsc',
    cluster_index    INTEGER NOT NULL,
    holding_percent  REAL,
    trend_type       TEXT,
    pnl_percent      REAL,
    address_count    INTEGER,
    is_real_cluster  INTEGER DEFAULT 1,
    addresses_json   TEXT,
    snapshot_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_oc_token_round ON onchain_clusters(token, round_number);

CREATE TABLE IF NOT EXISTS onchain_address_portfolio (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    round_number     INTEGER NOT NULL,
    token            TEXT NOT NULL,
    cluster_index    INTEGER NOT NULL,
    address          TEXT NOT NULL,
    portfolio_json   TEXT,
    snapshot_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_oap_token_round ON onchain_address_portfolio(token, round_number);

CREATE TABLE IF NOT EXISTS onchain_wallet_analysis (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    round_number     INTEGER NOT NULL,
    token            TEXT NOT NULL,
    cluster_index    INTEGER NOT NULL,
    address          TEXT NOT NULL,
    total_value_usd  REAL,
    buy_count_30d    INTEGER DEFAULT 0,
    sell_count_30d   INTEGER DEFAULT 0,
    buy_volume_30d   REAL,
    sell_volume_30d  REAL,
    avg_buy_value    REAL,
    avg_sell_value   REAL,
    realized_pnl     REAL,
    win_rate         REAL,
    last_trade_time  TIMESTAMP,
    analysis_json    TEXT,
    snapshot_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_owa_token_round ON onchain_wallet_analysis(token, round_number);

CREATE TABLE IF NOT EXISTS onchain_cluster_trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    round_number     INTEGER NOT NULL,
    token            TEXT NOT NULL,
    cluster_index    INTEGER NOT NULL,
    addresses_batch  TEXT,
    trades_json      TEXT,
    snapshot_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_oct_token_round ON onchain_cluster_trades(token, round_number);

CREATE TABLE IF NOT EXISTS onchain_cluster_tags (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    round_number     INTEGER NOT NULL,
    token            TEXT NOT NULL,
    cluster_index    INTEGER NOT NULL,
    tag_type         TEXT NOT NULL,
    tag_label        TEXT NOT NULL,
    tag_detail       TEXT,
    snapshot_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_octag_token ON onchain_cluster_tags(token, round_number);

CREATE TABLE IF NOT EXISTS onchain_blacklist (
    token       TEXT PRIMARY KEY,
    added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS onchain_removed (
    token       TEXT PRIMARY KEY,
    reason      TEXT DEFAULT 'manual',
    added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS onchain_pool_archive (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    token                   TEXT NOT NULL,
    token_address           TEXT,
    chain                   TEXT,
    pool_stage              TEXT,
    surface_top10_pct       REAL,
    effective_concentration REAL,
    fund_source_same_pct    REAL,
    divergence_detected     INTEGER,
    days_in_pool            REAL,
    rounds_in_pool          INTEGER,
    lp_reserve_trend        TEXT,
    lp_trend_score          REAL,
    gas_seeding_signal      INTEGER,
    cex_staging_signal      INTEGER,
    confidence_score        REAL,
    first_seen_at           TIMESTAMP,
    last_updated_at         TIMESTAMP,
    reason                  TEXT NOT NULL,
    archived_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _onchain_migrate(conn):
    """onchain.db 增量迁移"""
    _ensure_columns(conn, "onchain_snapshots", [
        ("divergence_score", "REAL"),
        ("effective_concentration", "REAL"),
        ("cluster_raw_json", "TEXT"),
    ])
    _ensure_columns(conn, "onchain_scanner_status", [
        ("actionable_size", "INTEGER DEFAULT 0"),
    ])
    _ensure_columns(conn, "onchain_pool", [
        ("backtest_winrate", "REAL"),
        ("confidence_score", "REAL DEFAULT 0"),
        ("prev_new_addr_pct", "REAL"),
    ])
    # 确保黑名单表存在（增量迁移，不影响已有数据）
    conn.execute("""\
CREATE TABLE IF NOT EXISTS onchain_blacklist (
    token       TEXT PRIMARY KEY,
    added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)""")
    conn.execute("""\
CREATE TABLE IF NOT EXISTS onchain_removed (
    token       TEXT PRIMARY KEY,
    reason      TEXT DEFAULT 'manual',
    added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)""")
    conn.execute("""\
CREATE TABLE IF NOT EXISTS onchain_pool_archive (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    token                   TEXT NOT NULL,
    token_address           TEXT,
    chain                   TEXT,
    pool_stage              TEXT,
    surface_top10_pct       REAL,
    effective_concentration REAL,
    fund_source_same_pct    REAL,
    divergence_detected     INTEGER,
    days_in_pool            REAL,
    rounds_in_pool          INTEGER,
    lp_reserve_trend        TEXT,
    lp_trend_score          REAL,
    gas_seeding_signal      INTEGER,
    cex_staging_signal      INTEGER,
    confidence_score        REAL,
    first_seen_at           TIMESTAMP,
    last_updated_at         TIMESTAMP,
    reason                  TEXT NOT NULL,
    archived_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)""")


def _purge_by_round(conn, table: str, keep_last_rounds: int = 50):
    """通用：按 round_number 保留最近 N 轮"""
    row = conn.execute(
        f"SELECT DISTINCT round_number FROM {table} ORDER BY round_number DESC LIMIT 1 OFFSET ?",
        (keep_last_rounds,),
    ).fetchone()
    if row:
        conn.execute(f"DELETE FROM {table} WHERE round_number <= ?", (row[0],))


def init_onchain_db():
    """初始化链上监控独立 DB"""
    with get_conn(config.ONCHAIN_DB) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(_ONCHAIN_DDL)
        _onchain_migrate(conn)


def init_alpha_db():
    """初始化 Alpha 集群查询独立 DB（仅集群相关表）"""
    with get_conn(config.ALPHA_CLUSTER_DB) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(_ONCHAIN_DDL)


def alpha_clear_token(conn, token: str):
    """删除 alpha DB 中指定 token 的旧集群数据"""
    for table in ("onchain_clusters", "onchain_wallet_analysis",
                  "onchain_cluster_trades", "onchain_cluster_tags"):
        conn.execute(f"DELETE FROM {table} WHERE token=?", (token,))


_LIVE_DDL = """\
CREATE TABLE IF NOT EXISTS live_positions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_position_id     INTEGER NOT NULL UNIQUE,
    token                 TEXT NOT NULL,
    symbol                TEXT NOT NULL,
    side                  TEXT DEFAULT 'LONG',
    strategy              TEXT DEFAULT 'onchain',
    entry_price           REAL,
    quantity              REAL,
    leverage              INTEGER DEFAULT 1,
    margin_usd            REAL,
    exchange_orders_json  TEXT,
    tp1_done              INTEGER DEFAULT 0,
    tp2_done              INTEGER DEFAULT 0,
    status                TEXT DEFAULT 'OPEN',
    realized_pnl          REAL DEFAULT 0,
    created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at             TIMESTAMP
);

CREATE TABLE IF NOT EXISTS live_daily_snapshots (
    date                  TEXT NOT NULL,
    strategy              TEXT DEFAULT 'onchain',
    equity                REAL,
    available_balance     REAL,
    margin_used           REAL,
    unrealized_pnl        REAL,
    realized_pnl          REAL,
    daily_pnl             REAL,
    fees_paid             REAL DEFAULT 0,
    snapshot_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (date, strategy)
);

CREATE TABLE IF NOT EXISTS live_trade_log (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    token                 TEXT NOT NULL,
    strategy              TEXT DEFAULT 'onchain',
    action                TEXT,
    price                 REAL,
    quantity              REAL,
    paper_position_id     INTEGER,
    exchange_order_id     TEXT,
    details_json          TEXT,
    created_at            TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# ---- 加密事件（event_agent.py）：抓到的快讯 / AI 判定 / 事件驱动下单 ----
_EVENT_DDL = """
CREATE TABLE IF NOT EXISTS crypto_event_raw (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id        INTEGER UNIQUE NOT NULL,   -- 平台消息 id：去重键 + 翻页游标
    ts            TEXT NOT NULL,             -- 原始 ISO（UTC）
    text          TEXT NOT NULL,             -- 原文
    coins         TEXT NOT NULL DEFAULT '[]',-- 识别出的币种（JSON 数组）
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ce_raw_id ON crypto_event_raw(id DESC);

CREATE TABLE IF NOT EXISTS crypto_event_ai (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      INTEGER NOT NULL,          -- → crypto_event_raw.id
    coin          TEXT NOT NULL,
    impact        TEXT,                      -- 利多 / 利空 / 中性 / 无关
    reason        TEXT,
    confidence    INTEGER,
    model         TEXT,
    status        TEXT DEFAULT 'pending',    -- pending / ok / failed
    attempts      INTEGER DEFAULT 0,
    analyzed_at   TEXT,
    traded        INTEGER DEFAULT 0,         -- 是否已被策略消费（防重复下单）
    UNIQUE(event_id, coin)
);
CREATE INDEX IF NOT EXISTS idx_ce_ai_state ON crypto_event_ai(status, id);

CREATE TABLE IF NOT EXISTS crypto_event_trade (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      INTEGER NOT NULL,
    coin          TEXT NOT NULL,
    position_id   INTEGER,
    side          TEXT,
    ok            INTEGER,
    reason        TEXT,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 水位 + 上次运行时间（页面要显示"上次抓取/上次分析"）
CREATE TABLE IF NOT EXISTS crypto_event_meta (
    key           TEXT PRIMARY KEY,
    value         TEXT
);
"""


def init_event_db():
    """初始化加密事件独立 DB"""
    with get_conn(config.EVENT_DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(_EVENT_DDL)


def event_meta_get(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM crypto_event_meta WHERE key=?",
                       (key,)).fetchone()
    return (row["value"] if row and row["value"] is not None else default)


def event_meta_set(conn, key: str, value) -> None:
    conn.execute("INSERT INTO crypto_event_meta (key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))


# ---------- 美股/商品事件（独立库，2026-10-02）----------
# 与加密事件同构，但标的是**美股个股 / 商品**（币安 TradFi 永续，不含指数/ETF）。
# 独立库是故意的：抓取共用（event_agent 一次抓、写两个库），AI 判定/交易/页面全隔离。
_STOCK_EVENT_DDL = """
CREATE TABLE IF NOT EXISTS stock_event_raw (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id        INTEGER UNIQUE NOT NULL,
    ts            TEXT NOT NULL,
    text          TEXT NOT NULL,
    tickers       TEXT NOT NULL DEFAULT '[]',   -- 识别出的美股/商品代码（JSON 数组）
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_se_raw_id ON stock_event_raw(id DESC);

CREATE TABLE IF NOT EXISTS stock_event_ai (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      INTEGER NOT NULL,
    ticker        TEXT NOT NULL,
    impact        TEXT,                          -- 利好 / 利空 / 中性 / 无关
    reason        TEXT,
    confidence    INTEGER,
    model         TEXT,
    status        TEXT DEFAULT 'pending',
    attempts      INTEGER DEFAULT 0,
    analyzed_at   TEXT,
    traded        INTEGER DEFAULT 0,
    UNIQUE(event_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_se_ai_state ON stock_event_ai(status, id);

CREATE TABLE IF NOT EXISTS stock_event_trade (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      INTEGER NOT NULL,
    ticker        TEXT NOT NULL,
    position_id   INTEGER,
    side          TEXT,
    ok            INTEGER,
    reason        TEXT,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS stock_event_meta (
    key           TEXT PRIMARY KEY,
    value         TEXT
);
"""


def init_stock_event_db():
    """初始化美股/商品事件独立 DB"""
    with get_conn(config.STOCK_EVENT_DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(_STOCK_EVENT_DDL)


def stock_event_meta_get(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM stock_event_meta WHERE key=?",
                       (key,)).fetchone()
    return (row["value"] if row and row["value"] is not None else default)


def stock_event_meta_set(conn, key: str, value) -> None:
    conn.execute("INSERT INTO stock_event_meta (key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))


# ---------- 科技股动态（独立库，2026-10-07）----------
# 新闻雷达（不交易）：金十消息拆条 → 三层识别 → 每个 (消息,条目,标的) 三元组
# 独立搜索+AI 成图解。主键即三元组，单条失败重试不影响同消息其他条目。
_TECH_NEWS_DDL = """
CREATE TABLE IF NOT EXISTS tech_news_raw (
    msg_id     INTEGER PRIMARY KEY,          -- 金十消息 id
    pub_ts     TEXT,                         -- 消息时间（原样字符串）
    text       TEXT NOT NULL,                -- 消息全文（拆条前，防漏原文）
    items_json TEXT NOT NULL DEFAULT '[]',   -- [{"no":1,"text":"…","stocks":[{code,name,cat}]}]
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tn_raw_created ON tech_news_raw(created_at DESC);

CREATE TABLE IF NOT EXISTS tech_news_ai (
    msg_id     INTEGER NOT NULL,
    item_no    INTEGER NOT NULL,
    stock_code TEXT NOT NULL,                -- 公司代码 / @存储 等行业码 / @科技综合
    stock_name TEXT NOT NULL,
    cat        TEXT NOT NULL,
    status     INTEGER DEFAULT 0,            -- 0 待处理 / 1 完成 / 2 失败
    attempts   INTEGER DEFAULT 0,
    degrade    INTEGER DEFAULT 0,            -- 1=搜索失败，退化为金十原文整理
    search_json TEXT,                        -- Google News 原始结果
    summary_json TEXT,                       -- 图解 JSON（headline/tldr/points/numbers/impact/sources）
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (msg_id, item_no, stock_code)
);
CREATE INDEX IF NOT EXISTS idx_tn_ai_state ON tech_news_ai(status, msg_id);

CREATE TABLE IF NOT EXISTS tech_news_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def init_tech_news_db():
    """初始化科技股动态独立 DB"""
    with get_conn(config.TECH_NEWS_DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(_TECH_NEWS_DDL)


def tech_news_meta_get(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM tech_news_meta WHERE key=?",
                       (key,)).fetchone()
    return (row["value"] if row and row["value"] is not None else default)


def tech_news_meta_set(conn, key: str, value) -> None:
    conn.execute("INSERT INTO tech_news_meta (key, value) VALUES (?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, str(value)))


def init_onchain_live_db():
    """初始化链上实盘独立 DB"""
    with get_conn(config.LIVE_DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(_LIVE_DDL)


def live_position_insert(conn, pos: dict) -> bool:
    """插入 live 记录。返回**是否真的插入了**（靠 rowcount 判）：
    `INSERT OR IGNORE` 在 paper_position_id 唯一键冲突时静默跳过，旧写法恒返 True
    → 调用方以为建好了、其实一条记录都没有（下一轮又补建、每轮多挂一张 SL）。

    保留 OR IGNORE（改成裸 INSERT 会让冲突抛异常，打断跟单/链上的补建路径）——
    只是把"被忽略"如实告诉调用方。"""
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO live_positions
               (paper_position_id, token, symbol, side, strategy,
                entry_price, quantity, leverage, margin_usd,
                exchange_orders_json, tp1_done, tp2_done, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pos["paper_position_id"], pos["token"], pos["symbol"],
             pos.get("side", "LONG"), pos.get("strategy", "onchain"),
             pos["entry_price"], pos["quantity"], pos.get("leverage", 1),
             pos.get("margin_usd"), pos.get("exchange_orders_json"),
             pos.get("tp1_done", 0), pos.get("tp2_done", 0),
             pos.get("status", "OPEN")),
        )
        return cur.rowcount > 0
    except Exception:
        return False


def live_position_update(conn, live_id: int, fields: dict):
    if not fields:
        return
    sets = []
    params = []
    for k, v in fields.items():
        if v == "__CURRENT_TIMESTAMP__":
            sets.append(f"{k}=CURRENT_TIMESTAMP")
        else:
            sets.append(f"{k}=?")
            params.append(v)
    params.append(live_id)
    conn.execute(
        f"UPDATE live_positions SET {', '.join(sets)} WHERE id=?",
        params,
    )


def live_position_get_by_paper(conn, paper_position_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM live_positions WHERE paper_position_id=?",
        (paper_position_id,),
    ).fetchone()
    return dict(row) if row else None


def live_positions_open(conn, strategy: str = None) -> list[dict]:
    sql = "SELECT * FROM live_positions WHERE status IN ('OPEN','PARTIAL')"
    params = ()
    if strategy:
        sql += " AND strategy=?"
        params = (strategy,)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def live_positions_closed(conn, limit: int = 50,
                          strategy: str = None) -> list[dict]:
    """已平仓实盘持仓历史（页面展示）。strategy 为空=全部策略"""
    sql = "SELECT * FROM live_positions WHERE status='CLOSED'"
    params: list = []
    if strategy:
        sql += " AND strategy=?"
        params.append(strategy)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def live_trade_log_insert(conn, entry: dict):
    conn.execute(
        """INSERT INTO live_trade_log
           (token, strategy, action, price, quantity,
            paper_position_id, exchange_order_id, details_json)
           VALUES (?,?,?,?,?,?,?,?)""",
        (entry["token"], entry.get("strategy", "onchain"),
         entry["action"], entry.get("price"), entry.get("quantity"),
         entry.get("paper_position_id"), entry.get("exchange_order_id"),
         entry.get("details_json")),
    )


def live_trade_log_for_token(conn, token: str, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM live_trade_log WHERE token=? ORDER BY id DESC LIMIT ?",
        (token, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def live_daily_snapshot_upsert(conn, entry: dict):
    conn.execute(
        """INSERT OR REPLACE INTO live_daily_snapshots
           (date, strategy, equity, available_balance, margin_used,
            unrealized_pnl, realized_pnl, daily_pnl, fees_paid)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (entry["date"], entry.get("strategy", "onchain"),
         entry["equity"], entry.get("available_balance"),
         entry.get("margin_used"), entry.get("unrealized_pnl"),
         entry.get("realized_pnl"), entry.get("daily_pnl", 0),
         entry.get("fees_paid", 0)),
    )


def live_daily_snapshots_get(conn, strategy: str = None, limit: int = 30) -> list[dict]:
    if strategy:
        rows = conn.execute(
            "SELECT * FROM live_daily_snapshots WHERE strategy=? ORDER BY date DESC LIMIT ?",
            (strategy, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM live_daily_snapshots ORDER BY date DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


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

_AGENT_SHORT_DDL = """\
CREATE TABLE IF NOT EXISTS agent_short_candidates (
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
CREATE INDEX IF NOT EXISTS idx_agent_short_candidates_token ON agent_short_candidates(token);
"""


def init_agent_dbs():
    _init_one_agent_db(config.AGENT_MAIN_DB, _AGENT_MAIN_DDL)
    _init_one_agent_db(config.KOL_DB, _KOL_DDL)
    _init_one_agent_db(config.SNAPSHOT_DB, _KOL_DDL)
    _init_one_agent_db(config.NL_DB, _NL_DDL)
    _init_one_agent_db(config.AGENT_SHORT_DB, _AGENT_SHORT_DDL)


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


# === Agent 做空版候选币池 ===

def agent_short_candidates_insert_batch(conn, round_number: int, items: list[dict]):
    import time as _t
    for item in items:
        for attempt in range(5):
            try:
                conn.execute("""
                    INSERT INTO agent_short_candidates
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


def agent_short_candidates_get_latest(conn, rounds: int = 2) -> list[dict]:
    cur = conn.execute("""
        SELECT a.* FROM agent_short_candidates a
        INNER JOIN (
            SELECT token, MAX(id) AS max_id
            FROM agent_short_candidates
            WHERE round_number > (
                SELECT COALESCE(MAX(round_number), 0) - ? FROM agent_short_candidates
            )
            GROUP BY token
        ) b ON a.id = b.max_id
        ORDER BY a.id
    """, (rounds - 1,))
    return [dict(r) for r in cur.fetchall()]


def agent_short_candidates_purge_old(conn, keep_last_rounds: int = 20):
    conn.execute("""
        DELETE FROM agent_short_candidates
        WHERE round_number <= (
            SELECT COALESCE(MAX(round_number), 0) - ? FROM agent_short_candidates
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
        "agent_short_enabled": getattr(config, "AGENT_SHORT_ENABLED", True),
        "trading_daily_limit_enabled": getattr(config, "TRADING_DAILY_LIMIT_ENABLED", True),
        "okx_data_source_enabled": getattr(config, "OKX_DATA_SOURCE_ENABLED", False),
        "limit_order_timeout_seconds": getattr(config, "LIMIT_ORDER_TIMEOUT_SECONDS", 600),
        "system_auto_trade_enabled": getattr(config, "SYSTEM_AUTO_TRADE_ENABLED", True),
        "strategy_initial_agent_short": getattr(config, "STRATEGY_INITIAL_AGENT_SHORT", 1000),
        "agent_short_interval_minutes": getattr(config, "AGENT_SHORT_INTERVAL_MINUTES", 16),
        "onchain_strategy_enabled": getattr(config, "ONCHAIN_STRATEGY_ENABLED", True),
        "onchain_scan_enabled": getattr(config, "ONCHAIN_SCAN_ENABLED", True),
        "strategy_initial_onchain": getattr(config, "STRATEGY_INITIAL_ONCHAIN", 1000),
        # 链上监控阈值
        "onchain_scan_interval_hours": getattr(config, "ONCHAIN_SCAN_INTERVAL_HOURS", 4),
        "onchain_divergence_fund_source_min": getattr(config, "ONCHAIN_DIVERGENCE_FUND_SOURCE_MIN", 30.0),
        "onchain_surface_top10_max": getattr(config, "ONCHAIN_SURFACE_TOP10_MAX", 40.0),
        "onchain_observation_max_days": getattr(config, "ONCHAIN_OBSERVATION_MAX_DAYS", 7),
        # 集群监控
        "onchain_cluster_enabled": getattr(config, "ONCHAIN_CLUSTER_ENABLED", True),
        "onchain_cluster_analysis_enabled": getattr(config, "ONCHAIN_CLUSTER_ANALYSIS_ENABLED", True),
        "onchain_cluster_analysis_interval": getattr(config, "ONCHAIN_CLUSTER_ANALYSIS_INTERVAL", 1),
        "onchain_cluster_top_addresses": getattr(config, "ONCHAIN_CLUSTER_TOP_ADDRESSES", 5),
        "onchain_cluster_min_holding_pct": getattr(config, "ONCHAIN_CLUSTER_MIN_HOLDING_PCT", 1.0),
        "onchain_cluster_keep_rounds": getattr(config, "ONCHAIN_CLUSTER_KEEP_ROUNDS", 1500),
        "onchain_live_mode": getattr(config, "ONCHAIN_LIVE_MODE", "paper"),
        # 超级趋势策略
        "supertrend_enabled": getattr(config, "SUPERTREND_ENABLED", True),
        # 加密事件（设置页可改，热生效）
        "event_strategy_enabled": getattr(config, "EVENT_STRATEGY_ENABLED", True),
        "event_min_confidence": getattr(config, "EVENT_MIN_CONFIDENCE", 70),
        "event_fresh_minutes": getattr(config, "EVENT_FRESH_MINUTES", 10),
        "event_cooldown_minutes": getattr(config, "EVENT_COOLDOWN_MINUTES", 30),
        "event_daily_max": getattr(config, "EVENT_DAILY_MAX", 12),
        "event_poll_seconds": getattr(config, "EVENT_POLL_SECONDS", 60),
        # 美股/商品事件（独立库；抓取复用加密事件那套）
        "stock_event_enabled": getattr(config, "STOCK_EVENT_ENABLED", True),
        "stock_event_trade_enabled": getattr(config, "STOCK_EVENT_TRADE_ENABLED", True),
        "stock_event_min_confidence": getattr(config, "STOCK_EVENT_MIN_CONFIDENCE", 75),
        "stock_event_fresh_minutes": getattr(config, "STOCK_EVENT_FRESH_MINUTES", 15),
        "stock_event_cooldown_minutes": getattr(config, "STOCK_EVENT_COOLDOWN_MINUTES", 120),
        "stock_event_daily_max": getattr(config, "STOCK_EVENT_DAILY_MAX", 6),
        "stock_event_max_tickers": getattr(config, "STOCK_EVENT_MAX_TICKERS", 3),
        "strategy_initial_stock_event": getattr(config, "STRATEGY_INITIAL_STOCK_EVENT", 1000),
        # 科技股动态（新闻雷达，独立库；开关=入库/处理总闸）
        "tech_news_enabled": getattr(config, "TECH_NEWS_ENABLED", True),
        "supertrend_sl_tolerance_pct": getattr(config, "SUPERTREND_SL_TOLERANCE_PCT", 1.0),
        "supertrend_order_tolerance_pct": getattr(config, "SUPERTREND_ORDER_TOLERANCE_PCT", 1.0),
        "supertrend_flip_tolerance_pct": getattr(config, "SUPERTREND_FLIP_TOLERANCE_PCT", 0.0),
        "supertrend_notional_usd": getattr(config, "SUPERTREND_NOTIONAL_USD", 50.0),
        "supertrend_leverage": getattr(config, "SUPERTREND_LEVERAGE", 3.0),
        "strategy_initial_supertrend": getattr(config, "STRATEGY_INITIAL_SUPERTREND", 1000),
        # 跟单雷达
        "alphafox_poll_seconds": getattr(config, "ALPHAFOX_POLL_SECONDS", 30),
        "strategy_initial_alphafox": 1000,
        "alphafox_trade_enabled": getattr(config, "ALPHAFOX_TRADE_ENABLED", True),
        "alphafox_trade_budget": getattr(config, "ALPHAFOX_TRADE_BUDGET", 1000.0),
        "alphafox_slippage_pct": getattr(config, "ALPHAFOX_SLIPPAGE_PCT", 0.05),
        "alphafox_fee_pct": getattr(config, "ALPHAFOX_FEE_PCT", 0.04),
    }


def trading_settings_get(conn) -> dict:
    settings = trading_settings_defaults()
    rows = conn.execute("SELECT key, value FROM trading_settings").fetchall()
    for row in rows:
        raw = row["value"]
        if row["key"] in {"enabled", "event_strategy_enabled", "stock_event_enabled", "stock_event_trade_enabled", "tech_news_enabled", "agent_trade_enabled", "heat_agent_enabled", "kol_agent_enabled", "kol_snapshot_enabled", "nl_agent_enabled", "heat_agent_lessons_enabled", "trading_daily_limit_enabled", "system_auto_trade_enabled", "agent_short_enabled", "okx_data_source_enabled", "onchain_strategy_enabled", "onchain_scan_enabled", "onchain_cluster_enabled", "onchain_cluster_analysis_enabled", "supertrend_enabled", "alphafox_trade_enabled", "alphafox_live_enabled", "event_strategy_enabled"}:
            settings[row["key"]] = str(raw).lower() in {"1", "true", "yes", "on"}
        elif row["key"] in {"initial_balance", "leverage", "order_amount", "kol_agent_min_confidence", "kol_token_cooldown_minutes", "limit_order_timeout_seconds", "kol_snapshot_min_confidence", "onchain_scan_interval_hours", "onchain_divergence_fund_source_min", "onchain_surface_top10_max", "onchain_observation_max_days", "strategy_initial_onchain", "onchain_cluster_analysis_interval", "onchain_cluster_top_addresses", "onchain_cluster_min_holding_pct", "onchain_cluster_keep_rounds", "supertrend_sl_tolerance_pct", "supertrend_order_tolerance_pct", "supertrend_flip_tolerance_pct", "supertrend_notional_usd", "supertrend_leverage", "strategy_initial_supertrend", "alphafox_poll_seconds", "event_min_confidence", "event_fresh_minutes", "event_cooldown_minutes", "event_daily_max", "event_poll_seconds",
            "stock_event_min_confidence", "stock_event_fresh_minutes", "stock_event_cooldown_minutes", "stock_event_daily_max", "stock_event_max_tickers", "strategy_initial_stock_event",
            "alphafox_trade_budget", "alphafox_slippage_pct", "alphafox_fee_pct", "alphafox_live_notional", "alphafox_live_leverage",
            "strategy_initial_agent", "strategy_initial_heat_agent", "strategy_initial_heat_agent_lessons", "strategy_initial_system", "strategy_initial_manual", "strategy_initial_kol_agent", "strategy_initial_agent_no_lessons", "strategy_initial_agent_short", "strategy_initial_kol_snapshot",
            "agent_collect_interval_minutes", "agent_trigger_interval", "kol_agent_interval_minutes", "nl_agent_interval_minutes", "agent_short_interval_minutes", "heat_agent_lessons_trigger_interval", "ai_regime_interval_minutes", "kol_snapshot_interval_minutes"}:
            try:
                settings[row["key"]] = float(raw)
            except (TypeError, ValueError):
                pass
        else:
            settings[row["key"]] = raw
    settings["leverage"] = int(settings.get("leverage") or config.TRADING_LEVERAGE)
    settings["alphafox_poll_seconds"] = int(
        settings.get("alphafox_poll_seconds") or getattr(config, "ALPHAFOX_POLL_SECONDS", 30))
    settings["kol_agent_min_confidence"] = int(settings.get("kol_agent_min_confidence") or 70)
    settings["kol_snapshot_min_confidence"] = int(settings.get("kol_snapshot_min_confidence") or 70)
    settings["kol_token_cooldown_minutes"] = int(settings.get("kol_token_cooldown_minutes") or 30)
    return settings


def trading_settings_update(conn, fields: dict):
    allowed = {"enabled", "mode", "initial_balance", "leverage", "order_amount", "agent_collect_interval_minutes", "agent_trigger_interval", "strategy_initial_agent", "strategy_initial_heat_agent", "strategy_initial_heat_agent_lessons", "strategy_initial_system", "strategy_initial_manual", "strategy_initial_kol_agent", "strategy_initial_agent_no_lessons", "strategy_initial_agent_short", "kol_agent_interval_minutes", "nl_agent_interval_minutes", "heat_agent_lessons_trigger_interval", "ai_regime_interval_minutes", "kol_llm_provider", "kol_agent_min_confidence", "kol_token_cooldown_minutes", "agent_trade_enabled", "heat_agent_enabled", "kol_agent_enabled", "nl_agent_enabled", "heat_agent_lessons_enabled", "agent_short_enabled", "trading_daily_limit_enabled", "system_auto_trade_enabled", "limit_order_timeout_seconds", "kol_snapshot_enabled", "kol_snapshot_interval_minutes", "strategy_initial_kol_snapshot", "kol_snapshot_min_confidence", "kol_snapshot_llm_provider", "agent_short_interval_minutes", "okx_data_source_enabled", "onchain_scan_interval_hours", "onchain_divergence_fund_source_min", "onchain_surface_top10_max", "onchain_observation_max_days", "strategy_initial_onchain", "onchain_strategy_enabled", "onchain_scan_enabled", "onchain_cluster_enabled", "onchain_cluster_analysis_enabled", "onchain_cluster_analysis_interval", "onchain_cluster_top_addresses", "onchain_cluster_min_holding_pct", "onchain_cluster_keep_rounds", "onchain_live_mode", "supertrend_enabled", "supertrend_sl_tolerance_pct", "supertrend_order_tolerance_pct", "supertrend_flip_tolerance_pct", "supertrend_notional_usd", "supertrend_leverage", "strategy_initial_supertrend", "alphafox_poll_seconds", "event_min_confidence", "event_fresh_minutes", "event_cooldown_minutes", "event_daily_max", "event_poll_seconds", "alphafox_trade_enabled", "alphafox_trade_budget", "alphafox_slippage_pct", "alphafox_fee_pct", "alphafox_live_enabled", "alphafox_live_exempt_paper_ids", "alphafox_live_notional", "alphafox_live_ratio", "alphafox_live_max_notional", "alphafox_live_leverage", "event_strategy_enabled", "event_min_confidence", "event_fresh_minutes", "event_cooldown_minutes", "event_daily_max", "event_poll_seconds", "stock_event_enabled", "stock_event_trade_enabled", "stock_event_min_confidence", "stock_event_fresh_minutes", "stock_event_cooldown_minutes", "stock_event_daily_max", "stock_event_max_tickers", "strategy_initial_stock_event", "tech_news_enabled"}
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


def trade_position_update(conn, position_id: int, fields: dict,
                          status_in: tuple | None = None) -> int:
    """按 id 更新持仓。status_in 给定时附加状态条件并返回受影响行数
    （0=状态已变，调用方应放弃本次写入：防把 CANCELED/CLOSED 行改回活跃，
    例如 1s 循环用旧快照把已合并(CANCELED)的行又按扫损平掉 → 数量/盈亏双计）。"""
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
    where = "WHERE id=?"
    if status_in:
        ph = ",".join("?" for _ in status_in)
        where += f" AND status IN ({ph})"
        params.extend(status_in)
    cur = conn.execute(
        f"UPDATE trade_positions SET {', '.join(sets)} {where}", params)
    return cur.rowcount


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
    with get_conn(config.AGENT_SHORT_DB) as ac:
        candidates_deleted += ac.execute("DELETE FROM agent_short_candidates").rowcount or 0
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
        "agent_short": "agent_candidates_short",
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
    elif strategy == "agent_short":
        with get_conn(config.AGENT_SHORT_DB) as ac:
            cand_del = ac.execute("DELETE FROM agent_short_candidates").rowcount or 0
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
    elif strategy == "supertrend":
        # 清策略自身 DB 的信号状态/平仓快照；监控币种列表（supertrend_tokens）保留
        try:
            with get_conn(config.SUPERTREND_DB) as sc:
                sc.execute("DELETE FROM supertrend_status")
                sc.execute("DELETE FROM supertrend_close_snaps")
        except Exception:
            pass

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


# ============================================================
# 链上监控 (On-Chain Monitor) — CRUD
# ============================================================

def onchain_status_update(conn, **fields):
    """写入/更新扫描器状态（单行 upsert）"""
    keys = list(fields.keys())
    placeholders = ", ".join(["?"] * len(keys))
    set_cols = ", ".join(f"{k}=excluded.{k}" for k in keys if k != "key")
    conn.execute(
        f"INSERT INTO onchain_scanner_status ({', '.join(keys)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT(key) DO UPDATE SET {set_cols}",
        list(fields.values()),
    )


def onchain_status_get(conn) -> dict | None:
    row = conn.execute("SELECT * FROM onchain_scanner_status WHERE key='scanner'").fetchone()
    return dict(row) if row else None


def onchain_snapshot_insert_batch(conn, round_number: int, snapshots: list[dict]):
    for s in snapshots:
        conn.execute(
            """INSERT INTO onchain_snapshots
               (round_number, token, token_address, chain,
                hot_rank, top10_hold_pct, holders_count, market_cap_usd,
                cluster_concentration, top100_holdings_pct,
                holder_same_fund_pct, holder_new_addr_pct,
                holder_same_creation_pct, rug_pull_pct,
                effective_concentration, divergence_score, cluster_raw_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (round_number, s["token"], s["token_address"], s.get("chain", "bsc"),
             s.get("hot_rank"), s.get("top10_hold_pct"), s.get("holders_count"),
             s.get("market_cap_usd"), s.get("cluster_concentration"),
             s.get("top100_holdings_pct"), s.get("holder_same_fund_pct"),
             s.get("holder_new_addr_pct"), s.get("holder_same_creation_pct"),
             s.get("rug_pull_pct"), s.get("effective_concentration"),
             s.get("divergence_score"), s.get("cluster_raw_json")),
        )


def onchain_snapshots_for_token(conn, token: str, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM onchain_snapshots WHERE token=? ORDER BY round_number DESC LIMIT ?",
        (token, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def onchain_snapshots_purge_old(conn, keep_last_rounds: int = 50):
    row = conn.execute(
        "SELECT DISTINCT round_number FROM onchain_snapshots ORDER BY round_number DESC LIMIT 1 OFFSET ?",
        (keep_last_rounds,),
    ).fetchone()
    if row:
        conn.execute("DELETE FROM onchain_snapshots WHERE round_number <= ?", (row[0],))


def onchain_pool_upsert(conn, entry: dict):
    keys = list(entry.keys())
    placeholders = ", ".join("?" for _ in keys)
    set_cols = ", ".join(f"{k}=excluded.{k}" for k in keys if k != "token")
    conn.execute(
        f"INSERT INTO onchain_pool ({', '.join(keys)}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT(token) DO UPDATE SET {set_cols}",
        list(entry.values()),
    )


def onchain_pool_get_all(conn, stage: str = None) -> list[dict]:
    if stage:
        rows = conn.execute(
            "SELECT * FROM onchain_pool WHERE pool_stage=? ORDER BY last_updated_at DESC",
            (stage,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM onchain_pool ORDER BY last_updated_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def onchain_pool_remove(conn, token: str):
    conn.execute("DELETE FROM onchain_pool WHERE token=?", (token,))
    conn.execute("DELETE FROM onchain_lp_snapshots WHERE token=?", (token,))


def onchain_lp_snapshot_insert_batch(conn, snapshots: list[dict]):
    for s in snapshots:
        conn.execute(
            """INSERT INTO onchain_lp_snapshots
               (token, token_address, chain, round_number, token_amount)
               VALUES (?,?,?,?,?)""",
            (s["token"], s["token_address"], s.get("chain", "bsc"),
             s["round_number"], s.get("token_amount")),
        )


def onchain_lp_snapshots_for_token(conn, token: str, limit: int = 5) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM onchain_lp_snapshots WHERE token=? ORDER BY round_number DESC LIMIT ?",
        (token, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ---- 集群监控 ----

def onchain_cluster_insert_batch(conn, round_number: int, records: list[dict]):
    for r in records:
        conn.execute(
            """INSERT INTO onchain_clusters
               (round_number, token, token_address, chain, cluster_index,
                holding_percent, trend_type, pnl_percent, address_count,
                is_real_cluster, addresses_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (round_number, r["token"], r["token_address"], r.get("chain", "bsc"),
             r["cluster_index"], r.get("holding_percent"), r.get("trend_type"),
             r.get("pnl_percent"), r.get("address_count"),
             r.get("is_real_cluster", 1), r.get("addresses_json")),
        )


def onchain_clusters_for_token(conn, token: str, limit_rounds: int = 1) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM onchain_clusters WHERE token=?
           AND round_number IN (SELECT DISTINCT round_number FROM onchain_clusters
               WHERE token=? ORDER BY round_number DESC LIMIT ?)
           ORDER BY round_number DESC, cluster_index ASC""",
        (token, token, limit_rounds),
    ).fetchall()
    return [dict(r) for r in rows]


def onchain_address_portfolio_insert_batch(conn, records: list[dict]):
    for r in records:
        conn.execute(
            """INSERT INTO onchain_address_portfolio
               (round_number, token, cluster_index, address, portfolio_json)
               VALUES (?,?,?,?,?)""",
            (r["round_number"], r["token"], r["cluster_index"],
             r["address"], r.get("portfolio_json")),
        )


def onchain_address_portfolio_for_token(conn, token: str, round_number: int = None) -> list[dict]:
    if round_number is None:
        rn_row = conn.execute(
            "SELECT MAX(round_number) FROM onchain_address_portfolio WHERE token=?",
            (token,),
        ).fetchone()
        if not rn_row or rn_row[0] is None:
            return []
        round_number = rn_row[0]
    rows = conn.execute(
        "SELECT * FROM onchain_address_portfolio WHERE token=? AND round_number=?",
        (token, round_number),
    ).fetchall()
    return [dict(r) for r in rows]


def onchain_cluster_trades_insert(conn, record: dict):
    conn.execute(
        """INSERT INTO onchain_cluster_trades
           (round_number, token, cluster_index, addresses_batch, trades_json)
           VALUES (?,?,?,?,?)""",
        (record["round_number"], record["token"], record["cluster_index"],
         record.get("addresses_batch"), record.get("trades_json")),
    )


def onchain_cluster_trades_for_token(conn, token: str, round_number: int = None) -> list[dict]:
    if round_number is None:
        rn_row = conn.execute(
            "SELECT MAX(round_number) FROM onchain_cluster_trades WHERE token=?",
            (token,),
        ).fetchone()
        if not rn_row or rn_row[0] is None:
            return []
        round_number = rn_row[0]
    rows = conn.execute(
        "SELECT * FROM onchain_cluster_trades WHERE token=? AND round_number=?",
        (token, round_number),
    ).fetchall()
    return [dict(r) for r in rows]


def onchain_cluster_tag_insert(conn, tag: dict):
    conn.execute(
        """INSERT INTO onchain_cluster_tags
           (round_number, token, cluster_index, tag_type, tag_label, tag_detail)
           VALUES (?,?,?,?,?,?)""",
        (tag["round_number"], tag["token"], tag["cluster_index"],
         tag["tag_type"], tag["tag_label"], tag.get("tag_detail")),
    )


def onchain_cluster_tags_for_token(conn, token: str, limit_rounds: int = 1) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM onchain_cluster_tags WHERE token=?
           AND round_number IN (SELECT DISTINCT round_number FROM onchain_cluster_tags
               WHERE token=? ORDER BY round_number DESC LIMIT ?)
           ORDER BY round_number DESC, cluster_index ASC""",
        (token, token, limit_rounds),
    ).fetchall()
    return [dict(r) for r in rows]


def onchain_wallet_analysis_insert_batch(conn, records: list[dict]):
    for r in records:
        conn.execute(
            """INSERT INTO onchain_wallet_analysis
               (round_number, token, cluster_index, address,
                total_value_usd, buy_count_30d, sell_count_30d,
                buy_volume_30d, sell_volume_30d, avg_buy_value, avg_sell_value,
                realized_pnl, win_rate, last_trade_time, analysis_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["round_number"], r["token"], r["cluster_index"], r["address"],
             r.get("total_value_usd"), r.get("buy_count_30d", 0),
             r.get("sell_count_30d", 0), r.get("buy_volume_30d"),
             r.get("sell_volume_30d"), r.get("avg_buy_value"), r.get("avg_sell_value"),
             r.get("realized_pnl"), r.get("win_rate"), r.get("last_trade_time"),
             r.get("analysis_json")),
        )


def onchain_wallet_analysis_for_token(conn, token: str, round_number: int = None) -> list[dict]:
    if round_number is None:
        rn_row = conn.execute(
            "SELECT MAX(round_number) FROM onchain_wallet_analysis WHERE token=?",
            (token,),
        ).fetchone()
        if not rn_row or rn_row[0] is None:
            return []
        round_number = rn_row[0]
    rows = conn.execute(
        "SELECT * FROM onchain_wallet_analysis WHERE token=? AND round_number=?",
        (token, round_number),
    ).fetchall()
    return [dict(r) for r in rows]


# ---- 黑名单 ----

def onchain_blacklist_get_all(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM onchain_blacklist ORDER BY added_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def onchain_blacklist_add(conn, token: str):
    conn.execute(
        "INSERT OR REPLACE INTO onchain_blacklist (token) VALUES (?)",
        (token.upper(),),
    )


def onchain_blacklist_remove(conn, token: str):
    conn.execute(
        "DELETE FROM onchain_blacklist WHERE token=?",
        (token.upper(),),
    )


# ---- 移除名单 ----

def onchain_removed_get_all(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM onchain_removed ORDER BY added_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def onchain_removed_add(conn, token: str, reason: str = "manual"):
    conn.execute(
        "INSERT OR REPLACE INTO onchain_removed (token, reason) VALUES (?,?)",
        (token.upper(), reason),
    )


def onchain_pool_archive(conn, token: str, reason: str):
    """先归档 onchain_pool 行到 onchain_pool_archive，再删除"""
    conn.execute("""
        INSERT INTO onchain_pool_archive
        (token, token_address, chain, pool_stage, surface_top10_pct,
         effective_concentration, fund_source_same_pct, divergence_detected,
         days_in_pool, rounds_in_pool, lp_reserve_trend, lp_trend_score,
         gas_seeding_signal, cex_staging_signal, confidence_score,
         first_seen_at, last_updated_at, reason)
        SELECT token, token_address, chain, pool_stage, surface_top10_pct,
               effective_concentration, fund_source_same_pct, divergence_detected,
               days_in_pool, rounds_in_pool, lp_reserve_trend, lp_trend_score,
               gas_seeding_signal, cex_staging_signal, confidence_score,
               first_seen_at, last_updated_at, ?
        FROM onchain_pool WHERE token=?
    """, (reason, token))
    conn.execute("DELETE FROM onchain_pool WHERE token=?", (token,))
    conn.execute("DELETE FROM onchain_lp_snapshots WHERE token=?", (token,))
