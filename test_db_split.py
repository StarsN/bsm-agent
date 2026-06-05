#!/usr/bin/env python3
"""DB分库单元测试 — 测试所有 agent_db 路由和连接"""
import os, sys, tempfile, shutil, sqlite3

# 临时目录模拟项目环境
tmpdir = tempfile.mkdtemp(prefix="bsm_test_")
print(f"测试目录: {tmpdir}")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

# Override DB paths to temp dir for testing, keep everything else from real config
config.DB_PATH = os.path.join(tmpdir, "binance_square.db")
config.AGENT_MAIN_DB = os.path.join(tmpdir, "agent_main.db")
config.KOL_DB = os.path.join(tmpdir, "kol.db")
config.SNAPSHOT_DB = os.path.join(tmpdir, "snapshot.db")
config.NL_DB = os.path.join(tmpdir, "nl.db")
config.AGENT_DB_ROOT = tmpdir

passed = 0
failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} -- {detail}")

# ============================================================
# Test 1: get_conn routing
# ============================================================
print("\n=== Test 1: get_conn routing ===")
from storage import get_conn

# system.db
with get_conn() as conn:
    db = conn.execute("PRAGMA database_list").fetchone()
    check("get_conn() → system.db", os.path.basename(db[2]) == "binance_square.db")

# agent DB
with get_conn(config.KOL_DB) as conn:
    db = conn.execute("PRAGMA database_list").fetchone()
    check("get_conn(KOL_DB) → kol.db", os.path.basename(db[2]) == "kol.db")

with get_conn(config.SNAPSHOT_DB) as conn:
    check("get_conn(SNAPSHOT_DB) opens OK", True)

with get_conn(config.AGENT_MAIN_DB) as conn:
    check("get_conn(AGENT_MAIN_DB) opens OK", True)

with get_conn(config.NL_DB) as conn:
    check("get_conn(NL_DB) opens OK", True)

# None → system.db
with get_conn(None) as conn:
    db = conn.execute("PRAGMA database_list").fetchone()
    check("get_conn(None) → system.db", os.path.basename(db[2]) == "binance_square.db")

# ============================================================
# Test 2: init_agent_dbs creates tables
# ============================================================
print("\n=== Test 2: init_agent_dbs ===")
from storage import init_agent_dbs
init_agent_dbs()

def table_exists(db_path, table):
    conn = sqlite3.connect(db_path)
    r = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    conn.close()
    return r is not None

check("agent_main.db has agent_candidates", table_exists(config.AGENT_MAIN_DB, "agent_candidates"))
check("kol.db has kol_candidates", table_exists(config.KOL_DB, "kol_candidates"))
check("kol.db has kol_analyses", table_exists(config.KOL_DB, "kol_analyses"))
check("kol.db has kol_llm_logs", table_exists(config.KOL_DB, "kol_llm_logs"))
check("snapshot.db has kol_candidates", table_exists(config.SNAPSHOT_DB, "kol_candidates"))
check("snapshot.db has kol_analyses", table_exists(config.SNAPSHOT_DB, "kol_analyses"))
check("snapshot.db has kol_llm_logs", table_exists(config.SNAPSHOT_DB, "kol_llm_logs"))
check("nl.db has nl_candidates", table_exists(config.NL_DB, "nl_candidates"))
# lessons 在 system.db（由 init_db() 创建），测试里手动建表
with get_conn() as conn:
    conn.execute("CREATE TABLE IF NOT EXISTS lessons (id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT, lesson TEXT, strategy TEXT DEFAULT 'agent', learned INTEGER DEFAULT 0, severity TEXT DEFAULT 'medium', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    check("system.db has lessons", conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='lessons'").fetchone() is not None)

# ============================================================
# Test 3: kol_analysis_insert with agent_db
# ============================================================
print("\n=== Test 3: kol_analysis_insert ===")
from storage import kol_analysis_insert

# Setup system.db with minimal schema
with get_conn() as conn:
    conn.execute("CREATE TABLE IF NOT EXISTS trading_settings (key TEXT, value TEXT)")

analysis = {
    "token": "BTC", "trend": "上升",
    "summary": "test", "direction": "long", "confidence": "75",
    "status": "ENTER", "strategy": "kol_agent",
}

# Insert with agent_db
kol_analysis_insert(None, analysis, agent_db=config.KOL_DB)

with get_conn(config.KOL_DB) as conn:
    row = conn.execute("SELECT token, strategy FROM kol_analyses").fetchone()
    check("kol_analysis_insert to KOL_DB", row is not None and row["token"] == "BTC" and row["strategy"] == "kol_agent")

# Insert to SNAPSHOT_DB
analysis["strategy"] = "kol_snapshot"
kol_analysis_insert(None, analysis, agent_db=config.SNAPSHOT_DB)

with get_conn(config.SNAPSHOT_DB) as conn:
    row = conn.execute("SELECT token, strategy FROM kol_analyses").fetchone()
    check("kol_analysis_insert to SNAPSHOT_DB", row is not None and row["strategy"] == "kol_snapshot")

# Backward compatibility: no agent_db
with get_conn() as sys_conn:
    sys_conn.execute("CREATE TABLE IF NOT EXISTS kol_analyses (id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT, trend TEXT, timeline TEXT, price_levels TEXT, summary TEXT, reasoning TEXT, position_analysis TEXT, timing TEXT, risk_control TEXT, direction TEXT, confidence TEXT, reason TEXT, llm_log_id INTEGER, action TEXT, status TEXT, context_tag TEXT, evidence_tags TEXT, strategy TEXT DEFAULT 'kol_agent', missing_data TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    kol_analysis_insert(sys_conn, analysis)
    row = sys_conn.execute("SELECT token FROM kol_analyses").fetchone()
    check("kol_analysis_insert without agent_db", row is not None and row["token"] == "BTC")

# ============================================================
# Test 4: kol_llm_log_insert with agent_db + return log_id
# ============================================================
print("\n=== Test 4: kol_llm_log_insert ===")
from storage import kol_llm_log_insert

log_data = {"provider": "test", "model": "gpt", "candidate_count": 2, "prompt_chars": 100,
            "response_chars": 200, "duration_ms": 300, "success": 1, "error": "",
            "analyses_count": 2, "system_prompt": "sys", "user_prompt": "usr", "raw_response": "raw"}

log_id = kol_llm_log_insert(None, log_data, agent_db=config.KOL_DB)
check("kol_llm_log_insert returns log_id > 0", log_id is not None and log_id > 0)

with get_conn(config.KOL_DB) as conn:
    row = conn.execute("SELECT id, provider FROM kol_llm_logs WHERE id=?", (log_id,)).fetchone()
    check("kol_llm_log_insert wrote to KOL_DB", row is not None and row["provider"] == "test")

# Snapshot DB
log_id2 = kol_llm_log_insert(None, log_data, agent_db=config.SNAPSHOT_DB)
check("kol_llm_log_insert to SNAPSHOT_DB returns log_id", log_id2 is not None and log_id2 > 0)

with get_conn(config.SNAPSHOT_DB) as conn:
    row = conn.execute("SELECT id FROM kol_llm_logs WHERE id=?", (log_id2,)).fetchone()
    check("kol_llm_log_insert wrote to SNAPSHOT_DB", row is not None)

# Backward compatibility: no agent_db
with get_conn() as sys_conn:
    sys_conn.execute("CREATE TABLE IF NOT EXISTS kol_llm_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, model TEXT, candidate_count INTEGER, prompt_chars INTEGER, response_chars INTEGER, duration_ms INTEGER, success INTEGER, error TEXT, analyses_count INTEGER, system_prompt TEXT, user_prompt TEXT, raw_response TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    log_id3 = kol_llm_log_insert(sys_conn, log_data)
    check("kol_llm_log_insert without agent_db works", log_id3 is not None and log_id3 > 0)

# ============================================================
# Test 5: trade_positions_with_kol_enrichment
# ============================================================
print("\n=== Test 5: trade_positions_with_kol_enrichment ===")
from storage import trade_positions_with_kol_enrichment, trade_position_insert

# Setup system.db with minimal trade_positions
with get_conn() as sys_conn:
    sys_conn.execute("CREATE TABLE IF NOT EXISTS trade_positions (id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT, symbol TEXT, side TEXT DEFAULT 'LONG', status TEXT DEFAULT 'OPEN', mode TEXT DEFAULT 'paper', margin_amount REAL DEFAULT 50, leverage REAL DEFAULT 2, notional REAL DEFAULT 100, quantity REAL DEFAULT 1, limit_price REAL, entry_price REAL, stop_loss_price REAL, tp1_price REAL, tp2_price REAL, current_price REAL, pnl_pct REAL DEFAULT 0, closed_qty REAL DEFAULT 0, realized_pnl REAL DEFAULT 0, unrealized_pnl REAL DEFAULT 0, highest_price REAL, lowest_price REAL, trailing_stop_price REAL, signal_snapshot TEXT, open_reason TEXT, advice TEXT, strategy TEXT DEFAULT 'agent', order_type TEXT DEFAULT 'market', pending_decision_id INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    sys_conn.execute("INSERT INTO trade_positions (token, strategy, entry_price, created_at) VALUES ('ETH', 'kol_agent', 3000, datetime('now'))")

# Insert analysis to KOL_DB for enrichment
with get_conn(config.KOL_DB) as conn:
    conn.execute("INSERT INTO kol_analyses (token, trend, confidence, created_at) VALUES ('ETH', '下降', '65', datetime('now'))")
    conn.execute("INSERT INTO kol_analyses (token, trend, confidence, created_at) VALUES ('ETH', '横盘', '55', datetime('now'))")

# Test enrichment
with get_conn() as sys_conn:
    positions = trade_positions_with_kol_enrichment(sys_conn)
    kol_positions = [p for p in positions if p.get("strategy") == "kol_agent"]
    check("enrichment finds KOL positions", len(kol_positions) > 0)
    if kol_positions:
        p = kol_positions[0]
        check("enrichment adds entry_trend", p.get("entry_trend") is not None)
        check("enrichment adds latest_confidence", p.get("latest_confidence") is not None)

# ============================================================
# Test 6: _execute_kol_trades (auto_trader refactoring)
# ============================================================
print("\n=== Test 6: _execute_kol_trades ===")
from kol_agent import _execute_kol_trades

# Setup pending_decisions table in system.db
with get_conn() as sys_conn:
    sys_conn.execute("CREATE TABLE IF NOT EXISTS pending_decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT, token TEXT, tier TEXT, entry_price REAL, reason TEXT, status TEXT DEFAULT 'pending', source TEXT, social_score REAL, mentions INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")

# Setup trading_settings
sys.path.insert(0, os.path.join(tmpdir))
with get_conn() as sys_conn:
    sys_conn.execute("INSERT OR REPLACE INTO trading_settings (key, value) VALUES ('kol_agent_min_confidence', '70')")

analyses = [{
    "token": "BTC", "direction": "long", "confidence": 80,
    "status": "ENTER", "position_size": "full",
    "price_levels": {"entry": 50000},
    "summary": "test long"
}]
candidates = [{"token": "BTC", "social_score": 10, "mentions": 5}]

with get_conn() as sys_conn:
    inserted = _execute_kol_trades(sys_conn, analyses, candidates, strategy="kol_agent")
    check("_execute_kol_trades returns inserted=1", inserted == 1)

    row = sys_conn.execute("SELECT * FROM pending_decisions WHERE source='kol_agent'").fetchone()
    check("pending_decisions has KOL decision", row is not None)
    check("pending_decisions action=open_long", row is not None and row["action"] == "open_long")
    check("pending_decisions entry_price=50000", row is not None and row["entry_price"] == 50000)
    check("pending_decisions tier=full", row is not None and row["tier"] == "full")

# Filtered out: direction=none
analyses2 = [{"token": "ETH", "direction": "none", "confidence": 80, "status": "ENTER", "price_levels": {"entry": 3000}, "summary": "no"}]
with get_conn() as sys_conn:
    inserted2 = _execute_kol_trades(sys_conn, analyses2, candidates, strategy="kol_agent")
    check("_execute_kol_trades filters direction=none", inserted2 == 0)

# Filtered out: confidence < 70
analyses3 = [{"token": "SOL", "direction": "long", "confidence": 50, "status": "ENTER", "price_levels": {"entry": 100}, "summary": "low conf"}]
with get_conn() as sys_conn:
    inserted3 = _execute_kol_trades(sys_conn, analyses3, candidates, strategy="kol_agent")
    check("_execute_kol_trades filters low confidence", inserted3 == 0)

# Filtered out: no entry_price
analyses4 = [{"token": "ADA", "direction": "long", "confidence": 80, "status": "ENTER", "price_levels": {}, "summary": "no entry"}]
with get_conn() as sys_conn:
    inserted4 = _execute_kol_trades(sys_conn, analyses4, candidates, strategy="kol_agent")
    check("_execute_kol_trades filters missing entry_price", inserted4 == 0)

# ============================================================
# Test 7: get_kol_candidates routing
# ============================================================
print("\n=== Test 7: get_kol_candidates ===")
from kol_agent import get_kol_candidates

# Setup
with get_conn(config.KOL_DB) as conn:
    conn.execute("""INSERT INTO kol_candidates (round_number, token, data) VALUES (1, 'BTC', '{"token":"BTC","price":50000}')""")

with get_conn() as sys_conn:
    sys_conn.execute("INSERT OR REPLACE INTO trading_settings (key, value) VALUES ('kol_agent_interval_minutes', '8')")
    sys_conn.execute("INSERT OR REPLACE INTO trading_settings (key, value) VALUES ('kol_token_cooldown_minutes', '30')")

with get_conn() as sys_conn:
    candidates = get_kol_candidates(sys_conn, "kol_agent")
    check("get_kol_candidates returns data", len(candidates) > 0)
    if candidates:
        check("get_kol_candidates has price", candidates[0].get("price") == 50000)

# ============================================================
# Summary
# ============================================================
print(f"\n{'='*50}")
print(f"  通过: {passed}  失败: {failed}")
print(f"{'='*50}")

# Cleanup
shutil.rmtree(tmpdir)
print("临时目录已清理")

if failed > 0:
    sys.exit(1)
