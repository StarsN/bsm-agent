"""SQLite 存储：帖子、作者、代币提及、观察列表、行情快照"""
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

-- Agent 候选币池：worker 每轮收集所有上过榜的 token，Agent 从这里读
CREATE TABLE IF NOT EXISTS round_candidates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    token           TEXT NOT NULL,
    score           REAL,
    mentions        INTEGER,
    unique_posts    INTEGER,
    source_round    INTEGER,                -- 来自 worker 第几轮
    mode            TEXT DEFAULT 'batch',   -- batch / streaming
    delivered       INTEGER DEFAULT 0,      -- 0=Agent 未读, 1=已读
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_rc_token_round ON round_candidates(token, source_round);
CREATE INDEX IF NOT EXISTS idx_rc_round ON round_candidates(source_round, delivered);

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
    trailing_stop_price REAL,
    closed_qty         REAL DEFAULT 0,
    realized_pnl       REAL DEFAULT 0,
    unrealized_pnl     REAL DEFAULT 0,
    pnl_pct            REAL DEFAULT 0,
    signal_snapshot    TEXT,
    open_reason        TEXT,
    advice             TEXT,
    exchange_order_id  TEXT,               -- 实盘：合约订单 ID（JSON 格式）
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    closed_at          TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trade_positions_status ON trade_positions(status, token);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trade_one_active_token
ON trade_positions(token)
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
    lesson_checked  TEXT                  -- 开仓前查了哪些 lessons
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
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH, timeout=30.0)
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
    # lessons 新索引
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lessons_ca ON lessons(created_at)")


def init_db():
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
        _migrate(conn)


def upsert_author(conn, author: dict):
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
    conn.execute("""
        INSERT INTO market_snapshots (token, snapshot, analysis, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(token) DO UPDATE SET
            snapshot=excluded.snapshot,
            analysis=excluded.analysis,
            updated_at=excluded.updated_at
    """, (token.upper(), snapshot_json, analysis_json))


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
    conn.execute("""
        INSERT INTO market_realtime_cache (token, symbol, snapshot, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(token) DO UPDATE SET
            symbol=excluded.symbol,
            snapshot=excluded.snapshot,
            updated_at=excluded.updated_at
    """, (token.upper(), symbol.upper(), snapshot_json))


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
        WHERE id NOT IN (
            SELECT id FROM token_heat_history
            ORDER BY id DESC
            LIMIT ?
        )
    """, (keep_last_rounds * 500,))  # 假设每轮最多 500 个代币


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
    for item in items:
        conn.execute("""
            INSERT INTO agent_candidates
                (round_number, token, data, tier, passed, hard_blocks, pass_count, signal_key)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            round_number, item["token"], item["data"], item.get("tier"),
            item.get("passed", 0), item.get("hard_blocks", "[]"),
            item.get("pass_count", 0), item.get("signal_key"),
        ))


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
    }


def trading_settings_get(conn) -> dict:
    settings = trading_settings_defaults()
    rows = conn.execute("SELECT key, value FROM trading_settings").fetchall()
    for row in rows:
        raw = row["value"]
        if row["key"] in {"enabled"}:
            settings[row["key"]] = str(raw).lower() in {"1", "true", "yes", "on"}
        elif row["key"] in {"initial_balance", "leverage", "order_amount"}:
            try:
                settings[row["key"]] = float(raw)
            except (TypeError, ValueError):
                pass
        else:
            settings[row["key"]] = raw
    settings["leverage"] = int(settings.get("leverage") or config.TRADING_LEVERAGE)
    return settings


def trading_settings_update(conn, fields: dict):
    allowed = {"enabled", "mode", "initial_balance", "leverage", "order_amount", "agent_trigger_interval"}
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


def trade_has_active(conn, token: str) -> bool:
    row = conn.execute("""
        SELECT 1 FROM trade_positions
        WHERE token = ? AND status IN ('PENDING', 'OPEN', 'PARTIAL')
        LIMIT 1
    """, (token.upper(),)).fetchone()
    return row is not None


def trade_signal_lock_acquire(conn, token: str, signal_key: str) -> bool:
    try:
        conn.execute("""
            INSERT INTO trade_signal_locks (token, signal_key)
            VALUES (?, ?)
        """, (token.upper(), signal_key))
        return True
    except sqlite3.IntegrityError:
        return False


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
        conn.execute("""
        INSERT INTO trade_positions
            (token, symbol, side, status, mode, margin_amount, leverage, notional,
             quantity, entry_price, limit_price, current_price, stop_loss_price,
             tp1_price, tp2_price, highest_price, trailing_stop_price,
             signal_snapshot, open_reason, advice)
        VALUES
            (:token, :symbol, :side, :status, :mode, :margin_amount, :leverage,
             :notional, :quantity, :entry_price, :limit_price, :current_price,
             :stop_loss_price, :tp1_price, :tp2_price, :highest_price,
             :trailing_stop_price, :signal_snapshot, :open_reason, :advice)
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
      - 止损学习归档表
      - pending_decisions 待处理决策
      - agent_candidates 候选池

    保留：
      - trading_settings 配置（enabled / mode / leverage 等）
      - lessons 教训库（真金白银买来的经验）
      - journal 操作日志（历史记录）
      - 如传入 new_initial_balance，同时更新初始余额

    返回：各表删除的行数 + 新配置
    """
    # 旧日志/教训标记失效，避免关联已删除的持仓数据
    conn.execute("UPDATE journal SET reviewed = 1")
    conn.execute("UPDATE lessons SET learned = 1")
    positions_deleted = conn.execute("DELETE FROM trade_positions").rowcount or 0
    locks_deleted = conn.execute("DELETE FROM trade_signal_locks").rowcount or 0
    archive_deleted = conn.execute("DELETE FROM trade_loss_archive").rowcount or 0
    decisions_deleted = conn.execute("DELETE FROM pending_decisions").rowcount or 0
    candidates_deleted = conn.execute("DELETE FROM agent_candidates").rowcount or 0

    # AUTOINCREMENT 计数器也重置
    for tbl in ("trade_positions", "trade_signal_locks", "trade_loss_archive",
                "pending_decisions", "agent_candidates"):
        conn.execute("DELETE FROM sqlite_sequence WHERE name = ?", (tbl,))

    if new_initial_balance is not None and new_initial_balance > 0:
        trading_settings_update(conn, {"initial_balance": new_initial_balance})

    settings = trading_settings_get(conn)
    return {
        "positions_deleted": positions_deleted,
        "locks_deleted": locks_deleted,
        "loss_archive_deleted": archive_deleted,
        "decisions_deleted": decisions_deleted,
        "candidates_deleted": candidates_deleted,
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


def lessons_stats(conn) -> dict:
    """教训库统计：总数、按 severity 分布、按 root_cause 分布"""
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

def round_candidates_add(conn, tokens: list[dict], source_round: int, mode: str = "batch"):
    """批量写入一批候选币（同一轮同一 token 不重复，分数取较高值）"""
    for t in tokens:
        conn.execute("""
            INSERT INTO round_candidates
                (token, score, mentions, unique_posts, source_round, mode)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(token, source_round) DO UPDATE SET
                score = MAX(round_candidates.score, excluded.score),
                mentions = excluded.mentions,
                unique_posts = excluded.unique_posts,
                mode = excluded.mode
        """, (t["token"].upper(), t.get("score", 0), t.get("mentions", 0),
              t.get("unique_posts", 0), source_round, mode))


def round_candidates_undelivered(conn) -> list[dict]:
    """Agent 读取未处理的候选币（返回原始行含 id，Agent 端去重）"""
    rows = conn.execute("""
        SELECT id, token, score, mentions, unique_posts, source_round, mode
        FROM round_candidates
        WHERE delivered = 0
        ORDER BY score DESC, id ASC
    """).fetchall()
    return [dict(r) for r in rows]


def round_candidates_mark_delivered_by_ids(conn, ids: list[int]):
    """标记指定 ID 的候选币为已处理"""
    if not ids:
        return
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE round_candidates SET delivered = 1 WHERE id IN ({placeholders})", ids
    )


def round_candidates_mark_delivered(conn):
    """标记所有未处理的候选币为已处理（兼容旧调用）"""
    conn.execute(
        "UPDATE round_candidates SET delivered = 1 WHERE delivered = 0"
    )


def watchlist_followups_purge_old(conn, days: int = 3):
    """清理旧的观察列表跟踪数据（浮盈浮亏历史快照）"""
    conn.execute(
        "DELETE FROM watchlist_followups WHERE recorded_at < datetime('now', ?)",
        (f"-{days} days",)
    )


def round_candidates_purge_old(conn, keep_last_rounds: int = 50):
    """清理旧的候选币数据"""
    conn.execute("""
        DELETE FROM round_candidates
        WHERE id NOT IN (
            SELECT id FROM round_candidates
            ORDER BY id DESC
            LIMIT ?
        )
    """, (keep_last_rounds * 200,))
