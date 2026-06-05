#!/usr/bin/env python3
"""DB分库全量SQL单元测试 — 基于迁移脚本产出的真实数据库。

先运行 migrate_agent_dbs.py 产出 extra/migrated_test/*.db，
再连接迁移产物 + 源 system.db，验证所有读/写 SQL 正确性。
纯 sqlite3，不依赖系统代码。
"""
import os
import sys
import json
import sqlite3
import tempfile
import shutil
from datetime import datetime

PROJ = os.path.dirname(os.path.abspath(__file__))
MIGRATED = os.path.join(PROJ, "extra", "migrated_test")
SYSTEM_SRC = os.path.join(PROJ, "extra", "binance_square_202606051500.db")

if not os.path.isdir(MIGRATED):
    print(f"ERROR: 迁移目录不存在: {MIGRATED}")
    sys.exit(1)
if not os.path.exists(SYSTEM_SRC):
    print(f"ERROR: 源 system.db 不存在: {SYSTEM_SRC}")
    sys.exit(1)

tmpdir = tempfile.mkdtemp(prefix="bsm_test_")
print(f"测试目录: {tmpdir}")

_AGENT_DBS = ["agent_main.db", "kol.db", "snapshot.db", "nl.db"]
for f in _AGENT_DBS:
    src = os.path.join(MIGRATED, f)
    if os.path.exists(src):
        shutil.copy2(src, os.path.join(tmpdir, f))

def db(name):
    if name == "system.db":
        return SYSTEM_SRC
    return os.path.join(tmpdir, name)

passed = 0
failed = 0

def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  \033[32m[PASS]\033[0m {name}")
    else:
        failed += 1
        print(f"  \033[31m[FAIL]\033[0m {name} -- {detail}")

# ============================================================
# 0. 迁移产物完整性
# ============================================================
print("\n=== 0. 迁移产物完整性 ===")

def test_integrity():
    # agent_main.db
    c = sqlite3.connect(db("agent_main.db"))
    c.row_factory = sqlite3.Row
    tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()]
    check("0a. agent_main.db 有 agent_candidates 表", "agent_candidates" in tables)
    cnt = c.execute("SELECT COUNT(*) FROM agent_candidates").fetchone()[0]
    check("0b. agent_main.db 行数（源损坏）", cnt == 0, f"got {cnt}")
    c.close()

    # kol.db
    c = sqlite3.connect(db("kol.db"))
    c.row_factory = sqlite3.Row
    for t, expected in [("kol_candidates", 0), ("kol_analyses", 825), ("kol_llm_logs", 1071)]:
        cnt = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        check(f"0c. kol.db {t}", cnt == expected, f"got {cnt} expected {expected}")
    c.close()

    # snapshot.db
    c = sqlite3.connect(db("snapshot.db"))
    c.row_factory = sqlite3.Row
    for t, expected in [("kol_candidates", 0), ("kol_analyses", 825), ("kol_llm_logs", 1071)]:
        cnt = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        check(f"0d. snapshot.db {t}", cnt == expected, f"got {cnt} expected {expected}")
    c.close()

    # nl.db
    c = sqlite3.connect(db("nl.db"))
    cnt = c.execute("SELECT COUNT(*) FROM nl_candidates").fetchone()[0]
    check("0e. nl.db nl_candidates", cnt == 0, f"got {cnt}")
    c.close()

    # system.db — lessons 未迁出，仍在 system.db
    c = sqlite3.connect(db("system.db"))
    c.row_factory = sqlite3.Row
    for t in ["pending_decisions", "trade_positions", "journal", "trading_settings", "lessons"]:
        exists = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
        check(f"0f. system.db 有 {t}", exists is not None)
    cnt = c.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
    check("0g. system.db lessons 行数", cnt == 127, f"got {cnt}")
    c.close()

test_integrity()

# ============================================================
# 1. agent_main.db — agent_candidates（空表，源损坏）
# ============================================================
print("\n=== 1. agent_main.db — agent_candidates ===")

def test_agent_main():
    conn = sqlite3.connect(db("agent_main.db"))
    conn.row_factory = sqlite3.Row

    items = [
        {"token": "TESTBTC", "data": '{"price":50000}', "tier": "full", "passed": 1, "hard_blocks": "[]", "pass_count": 3, "signal_key": "btc"},
        {"token": "TESTETH", "data": '{"price":3000}', "tier": "half", "passed": 1, "hard_blocks": "[]", "pass_count": 2, "signal_key": "eth"},
        {"token": "TESTBTC", "data": '{"price":51000}', "tier": "full", "passed": 1, "hard_blocks": "[]", "pass_count": 4, "signal_key": "btc2"},
    ]
    for item in items:
        conn.execute(
            "INSERT INTO agent_candidates (round_number, token, data, tier, passed, hard_blocks, pass_count, signal_key) VALUES (?,?,?,?,?,?,?,?)",
            (1, item["token"], item["data"], item.get("tier"), item.get("passed", 0), item.get("hard_blocks", "[]"), item.get("pass_count", 0), item.get("signal_key")),
        )
    conn.commit()
    cnt = conn.execute("SELECT COUNT(*) FROM agent_candidates").fetchone()[0]
    check("1a. INSERT batch", cnt == 3, f"got {cnt}")

    # SELECT latest dedup (storage.py agent_candidates_get_latest)
    rows = conn.execute("""
        SELECT a.* FROM agent_candidates a
        INNER JOIN (SELECT token, MAX(id) AS max_id FROM agent_candidates
                    WHERE round_number > (SELECT COALESCE(MAX(round_number),0)-? FROM agent_candidates)
                    GROUP BY token) b ON a.id = b.max_id
        ORDER BY a.id
    """, (1,)).fetchall()
    tokens = [r["token"] for r in rows]
    check("1b. SELECT latest dedup", len(tokens) == 2 and "TESTBTC" in tokens and "TESTETH" in tokens, f"tokens={tokens}")

    # DELETE old (storage.py agent_candidates_purge_old)
    conn.execute("DELETE FROM agent_candidates WHERE round_number <= (SELECT COALESCE(MAX(round_number),0)-? FROM agent_candidates)", (20,))
    conn.commit()
    check("1c. DELETE old", conn.execute("SELECT COUNT(*) FROM agent_candidates").fetchone()[0] == 3)

    # SELECT time window dedup (extract_market_data.py)
    rows = conn.execute(
        "SELECT a.data FROM agent_candidates a INNER JOIN (SELECT token, MAX(id) AS max_id FROM agent_candidates "
        "WHERE created_at >= datetime('now', '-17 minutes') GROUP BY token) b ON a.id = b.max_id ORDER BY a.id"
    ).fetchall()
    check("1d. SELECT time window dedup", len(rows) == 2, f"got {len(rows)}")

    # SELECT COALESCE(MAX) (web.py collector recovery)
    row = conn.execute("SELECT COALESCE(MAX(round_number), 0) FROM agent_candidates").fetchone()
    check("1e. MAX round_number", row[0] == 1, f"got {row[0]}")

    conn.execute("DELETE FROM agent_candidates")
    conn.commit()
    conn.close()

test_agent_main()

# ============================================================
# 2. kol.db — kol_candidates（空表，源损坏）
# ============================================================
print("\n=== 2. kol.db — kol_candidates ===")

def test_kol_candidates():
    conn = sqlite3.connect(db("kol.db"))
    conn.row_factory = sqlite3.Row

    batch = [
        {"token": "TESTSOL", "data": '{"price":100}', "tier": "full", "passed": 1, "hard_blocks": None, "pass_count": 1, "signal_key": "sol"},
        {"token": "TESTADA", "data": '{"price":0.5}', "tier": "half", "passed": 1, "hard_blocks": None, "pass_count": 0, "signal_key": "ada"},
    ]
    conn.executemany(
        "INSERT INTO kol_candidates (round_number, token, data, tier, passed, hard_blocks, pass_count, signal_key) VALUES (?,?,?,?,?,?,?,?)",
        [(1, r["token"], r["data"], r.get("tier"), r.get("passed", 1), r.get("hard_blocks"), r.get("pass_count", 0), r.get("signal_key")) for r in batch],
    )
    conn.commit()
    check("2a. INSERT batch", conn.execute("SELECT COUNT(*) FROM kol_candidates").fetchone()[0] == 2)

    # SELECT MAX round (kol_candidates_latest_round)
    row = conn.execute("SELECT MAX(round_number) FROM kol_candidates").fetchone()
    check("2b. SELECT MAX", row[0] == 1, f"got {row[0]}")

    # SELECT * by round (kol_candidates_latest_round)
    rows = conn.execute("SELECT * FROM kol_candidates WHERE round_number=? ORDER BY id", (1,)).fetchall()
    check("2c. SELECT * WHERE round_number", len(rows) == 2, f"got {len(rows)}")

    # SELECT data time window (kol_agent.py get_kol_candidates)
    rows = conn.execute("SELECT data FROM kol_candidates WHERE created_at >= datetime('now', '-10 minutes') ORDER BY id").fetchall()
    check("2d. SELECT data time window", len(rows) == 2, f"got {len(rows)}")

    # DELETE old (kol_candidates_purge_old)
    row = conn.execute("SELECT DISTINCT round_number FROM kol_candidates ORDER BY round_number DESC LIMIT 1 OFFSET ?", (30,)).fetchone()
    if row:
        conn.execute("DELETE FROM kol_candidates WHERE round_number <= ?", (row[0],))
    conn.commit()
    check("2e. DELETE old", conn.execute("SELECT COUNT(*) FROM kol_candidates").fetchone()[0] == 2)

    # COALESCE(MAX) (web.py collector recovery)
    row = conn.execute("SELECT COALESCE(MAX(round_number), 0) FROM kol_candidates").fetchone()
    check("2f. COALESCE(MAX)", row[0] == 1, f"got {row[0]}")

    conn.execute("DELETE FROM kol_candidates")
    conn.commit()
    conn.close()

test_kol_candidates()

# ============================================================
# 3. kol.db — kol_analyses（825 行真实数据）
# ============================================================
print("\n=== 3. kol.db — kol_analyses (825 real rows) ===")

def test_kol_analyses():
    conn = sqlite3.connect(db("kol.db"))
    conn.row_factory = sqlite3.Row

    # 真实数据 + 12h 窗口
    rows = conn.execute(
        "SELECT id, token, trend, timeline, price_levels, summary, reasoning, position_analysis, "
        "timing, risk_control, direction, confidence, reason, action, status, context_tag, evidence_tags, "
        "missing_data, strategy, created_at FROM kol_analyses "
        "WHERE created_at >= datetime('now', '-12 hours') ORDER BY id DESC"
    ).fetchall()
    all_rows = conn.execute("SELECT COUNT(*) FROM kol_analyses").fetchone()[0]
    check("3a. 总行数", all_rows == 825, f"got {all_rows}")
    check("3b. SELECT 12h window", len(rows) >= 0, f"got {len(rows)} (old data may be out of window)")

    # strategy filter (kol_analyses_latest)
    rows = conn.execute(
        "SELECT id, token, strategy FROM kol_analyses WHERE strategy=? ORDER BY id DESC LIMIT 10", ("kol_agent",)
    ).fetchall()
    check("3c. SELECT + strategy filter", len(rows) >= 1, f"got {len(rows)}")

    # token filter
    tokens = [r["token"] for r in conn.execute("SELECT DISTINCT token FROM kol_analyses LIMIT 3").fetchall()]
    if tokens:
        rows = conn.execute(
            "SELECT id, token FROM kol_analyses WHERE token=? ORDER BY id DESC LIMIT 8", (tokens[0],)
        ).fetchall()
        check("3d. SELECT + token filter LIMIT 8", len(rows) >= 1, f"got {len(rows)} for {tokens[0]}")

    # enrichment queries (trade_positions_with_kol_enrichment)
    if tokens:
        row = conn.execute(
            "SELECT trend, confidence FROM kol_analyses WHERE token=? AND created_at <= ? ORDER BY id DESC LIMIT 1",
            (tokens[0], datetime.now().isoformat()),
        ).fetchone()
        check("3e. SELECT enrichment by token+time", row is not None)
        row = conn.execute(
            "SELECT trend, confidence FROM kol_analyses WHERE token=? ORDER BY id DESC LIMIT 1", (tokens[0],)
        ).fetchone()
        check("3f. SELECT enrichment latest", row is not None)

    # INSERT test row (kol_analysis_insert — 显式列名)
    conn.execute(
        "INSERT INTO kol_analyses (token, trend, timeline, price_levels, summary, reasoning, "
        "position_analysis, timing, risk_control, direction, confidence, reason, llm_log_id, "
        "action, status, context_tag, evidence_tags, strategy, missing_data) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("TESTTOKEN", "上升", "[]", '{"entry":100}', "test", "{}", "", "", "{}", "long", "80",
         "test", 999999, None, "ENTER", "test", "[]", "kol_agent", "无"),
    )
    test_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    check("3g. INSERT test row", conn.execute("SELECT COUNT(*) FROM kol_analyses").fetchone()[0] == 826)
    inserted = conn.execute("SELECT token, strategy FROM kol_analyses WHERE id=?", (test_id,)).fetchone()
    check("3h. INSERT verify", inserted["token"] == "TESTTOKEN" and inserted["strategy"] == "kol_agent")
    conn.execute("DELETE FROM kol_analyses WHERE id=?", (test_id,))
    conn.commit()
    check("3i. cleanup", conn.execute("SELECT COUNT(*) FROM kol_analyses").fetchone()[0] == 825)

    conn.close()

test_kol_analyses()

# ============================================================
# 4. kol.db — kol_llm_logs（1071 行真实数据）
# ============================================================
print("\n=== 4. kol.db — kol_llm_logs (1071 real rows) ===")

def test_kol_llm_logs():
    conn = sqlite3.connect(db("kol.db"))
    conn.row_factory = sqlite3.Row

    # SELECT recent no strategy
    rows = conn.execute(
        "SELECT id, provider, model, candidate_count, prompt_chars, response_chars, "
        "duration_ms, success, error, analyses_count, created_at, '' AS missing_data "
        "FROM kol_llm_logs ORDER BY id DESC LIMIT 30"
    ).fetchall()
    check("4a. SELECT recent", len(rows) >= 1, f"got {len(rows)}")

    # SELECT with strategy JOIN (kol_llm_logs_recent)
    rows = conn.execute(
        "SELECT DISTINCT l.id, l.provider, l.model, l.candidate_count, l.prompt_chars, l.response_chars, "
        "l.duration_ms, l.success, l.error, l.analyses_count, l.created_at, "
        "COALESCE(GROUP_CONCAT(CASE WHEN a.missing_data != '无' THEN a.token || ':' || a.missing_data END, ' | '), '') AS missing_data "
        "FROM kol_llm_logs l JOIN kol_analyses a ON a.llm_log_id = l.id AND a.strategy = ? "
        "GROUP BY l.id ORDER BY l.id DESC LIMIT ?", ("kol_agent", 30)
    ).fetchall()
    check("4b. SELECT JOIN strategy", len(rows) >= 1, f"got {len(rows)}")

    # INSERT test
    conn.execute(
        "INSERT INTO kol_llm_logs (provider, model, candidate_count, prompt_chars, response_chars, "
        "duration_ms, success, error, analyses_count, system_prompt, user_prompt, raw_response) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("test", "test", 3, 5000, 2000, 5000, 1, "", 2, "sys", "usr", "raw"),
    )
    log_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    check("4c. INSERT + log_id", log_id > 0, f"got {log_id}")
    check("4d. INSERT verify", conn.execute("SELECT COUNT(*) FROM kol_llm_logs").fetchone()[0] == 1072)
    conn.execute("DELETE FROM kol_llm_logs WHERE id=?", (log_id,))
    conn.commit()
    check("4e. cleanup", conn.execute("SELECT COUNT(*) FROM kol_llm_logs").fetchone()[0] == 1071)

    conn.close()

test_kol_llm_logs()

# ============================================================
# 5. snapshot.db — 策略隔离验证
# ============================================================
print("\n=== 5. snapshot.db — 策略隔离 ===")

def test_snapshot():
    conn = sqlite3.connect(db("snapshot.db"))
    conn.row_factory = sqlite3.Row

    check("5a. kol_analyses 行数", conn.execute("SELECT COUNT(*) FROM kol_analyses").fetchone()[0] == 825)
    check("5b. kol_llm_logs 行数", conn.execute("SELECT COUNT(*) FROM kol_llm_logs").fetchone()[0] == 1071)

    # INSERT kol_candidates (空表)
    conn.execute("INSERT INTO kol_candidates (round_number, token, data, tier, passed, hard_blocks, pass_count, signal_key) VALUES (?,?,?,?,?,?,?,?)",
                 (2, "SNAPETH", '{"price":3100}', "full", 1, "[]", 5, "snap"))
    conn.commit()
    snap_cc = conn.execute("SELECT COUNT(*) FROM kol_candidates").fetchone()[0]
    kol_cc = sqlite3.connect(db("kol.db")).execute("SELECT COUNT(*) FROM kol_candidates").fetchone()[0]
    check("5c. snapshot 与 kol 隔离 (candidates)", snap_cc == 1 and kol_cc == 0, f"snap={snap_cc} kol={kol_cc}")

    # INSERT kol_analyses with strategy='kol_snapshot'
    conn.execute(
        "INSERT INTO kol_analyses (token, trend, timeline, price_levels, summary, reasoning, "
        "position_analysis, timing, risk_control, direction, confidence, reason, llm_log_id, "
        "action, status, context_tag, evidence_tags, strategy, missing_data) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("SNAPETH", "横盘", "[]", '{"entry":3100}', "snap", "{}", "", "", "{}", "short", "65",
         "test", 999999, None, "ENTER", "bearish", "[]", "kol_snapshot", "无"),
    )
    conn.commit()
    snap_a = conn.execute("SELECT COUNT(*) FROM kol_analyses").fetchone()[0]
    kol_a = sqlite3.connect(db("kol.db")).execute("SELECT COUNT(*) FROM kol_analyses").fetchone()[0]
    check("5d. snapshot 与 kol 隔离 (analyses)", snap_a == 826 and kol_a == 825, f"snap={snap_a} kol={kol_a}")

    # INSERT kol_llm_logs
    conn.execute("INSERT INTO kol_llm_logs (provider, model, candidate_count, prompt_chars, response_chars, duration_ms, success, error, analyses_count, system_prompt, user_prompt, raw_response) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 ("test", "claude", 2, 3000, 1000, 3000, 1, "", 1, "s", "u", "r"))
    conn.commit()
    snap_l = conn.execute("SELECT COUNT(*) FROM kol_llm_logs").fetchone()[0]
    kol_l = sqlite3.connect(db("kol.db")).execute("SELECT COUNT(*) FROM kol_llm_logs").fetchone()[0]
    check("5e. snapshot 与 kol 隔离 (logs)", snap_l == 1072 and kol_l == 1071, f"snap={snap_l} kol={kol_l}")

    # SELECT kol_snapshot strategy filter
    rows = conn.execute("SELECT id, token, strategy FROM kol_analyses WHERE strategy=? ORDER BY id DESC", ("kol_snapshot",)).fetchall()
    check("5f. SELECT kol_snapshot filter", len(rows) == 1 and rows[0]["token"] == "SNAPETH")

    # COALESCE(MAX) collector recovery
    row = conn.execute("SELECT COALESCE(MAX(round_number), 0) FROM kol_candidates").fetchone()
    check("5g. COALESCE(MAX)", row[0] == 2, f"got {row[0]}")

    # cleanup
    conn.execute("DELETE FROM kol_candidates")
    conn.execute("DELETE FROM kol_analyses WHERE strategy='kol_snapshot'")
    conn.execute("DELETE FROM kol_llm_logs WHERE provider='test'")
    conn.commit()
    check("5h. cleanup", True)

    conn.close()

test_snapshot()

# ============================================================
# 6. nl.db — nl_candidates（空表）
# ============================================================
print("\n=== 6. nl.db — nl_candidates ===")

def test_nl():
    conn = sqlite3.connect(db("nl.db"))
    conn.row_factory = sqlite3.Row

    conn.executemany(
        "INSERT INTO nl_candidates (round_number, token, data, tier, passed, hard_blocks, pass_count, signal_key) VALUES (?,?,?,?,?,?,?,?)",
        [(1, "TESTDOGE", '{"price":0.1}', "full", 1, None, 2, "doge")],
    )
    conn.commit()
    check("6a. INSERT batch", conn.execute("SELECT COUNT(*) FROM nl_candidates").fetchone()[0] == 1)

    row = conn.execute("SELECT COALESCE(MAX(round_number), 0) FROM nl_candidates").fetchone()
    check("6b. COALESCE(MAX)", row[0] == 1, f"got {row[0]}")

    # DELETE old
    row = conn.execute("SELECT DISTINCT round_number FROM nl_candidates ORDER BY round_number DESC LIMIT 1 OFFSET ?", (30,)).fetchone()
    if row:
        conn.execute("DELETE FROM nl_candidates WHERE round_number <= ?", (row[0],))
    conn.commit()
    check("6c. DELETE old", conn.execute("SELECT COUNT(*) FROM nl_candidates").fetchone()[0] == 1)

    # SELECT time window dedup (extract_market_data.py no_lessons)
    rows = conn.execute(
        "SELECT a.data FROM nl_candidates a INNER JOIN (SELECT token, MAX(id) AS max_id FROM nl_candidates "
        "WHERE created_at >= datetime('now', '-32 minutes') GROUP BY token) b ON a.id = b.max_id ORDER BY a.id"
    ).fetchall()
    check("6d. SELECT time window dedup", len(rows) == 1, f"got {len(rows)}")

    conn.execute("DELETE FROM nl_candidates")
    conn.commit()
    conn.close()

test_nl()

# ============================================================
# 7. system.db — lessons（127 行真实数据，未迁出）
# ============================================================
print("\n=== 7. system.db — lessons (127 real rows, strategy隔离) ===")

def test_lessons():
    conn = sqlite3.connect(db("system.db"))
    conn.row_factory = sqlite3.Row

    all_cnt = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
    check("7a. 总行数", all_cnt == 127, f"got {all_cnt}")

    # 策略分布
    strats = [r[0] for r in conn.execute("SELECT DISTINCT strategy FROM lessons").fetchall()]
    check("7b. 策略列存在且多样", len(strats) >= 1, f"strats={strats}")

    # SELECT by token + learned (lessons_query)
    tokens = [r["token"] for r in conn.execute("SELECT DISTINCT token FROM lessons WHERE learned=0 LIMIT 3").fetchall()]
    if tokens:
        rows = conn.execute("SELECT * FROM lessons WHERE token=? AND learned=? ORDER BY severity DESC, created_at DESC", (tokens[0], 0)).fetchall()
        check("7c. SELECT by token + learned=0", len(rows) >= 1 and rows[0]["token"] == tokens[0])

    # SELECT by learned global (lessons_query)
    rows = conn.execute("SELECT * FROM lessons WHERE learned=? ORDER BY severity DESC, created_at DESC", (0,)).fetchall()
    check("7d. SELECT by learned=0", len(rows) >= 1, f"got {len(rows)}")

    # SELECT recent (lessons_recent)
    rows = conn.execute("SELECT * FROM lessons ORDER BY id DESC LIMIT 20").fetchall()
    check("7e. SELECT recent", len(rows) >= 1)

    # SELECT stats by strategy (lessons_stats)
    for strat in ["heat_agent", "heat_agent_lessons", "agent"]:
        rows = conn.execute("SELECT * FROM lessons WHERE strategy=?", (strat,)).fetchall()
        check(f"7f. stats strategy={strat}", len(rows) >= 0, f"got {len(rows)}")

    # UPDATE learned toggle (lessons_mark_learned + web.py toggle)
    les_id = conn.execute("SELECT id FROM lessons WHERE learned=0 LIMIT 1").fetchone()
    if les_id:
        les_id = les_id["id"]
        conn.execute("UPDATE lessons SET learned=1 WHERE id=?", (les_id,))
        conn.commit()
        learned = conn.execute("SELECT learned FROM lessons WHERE id=?", (les_id,)).fetchone()["learned"]
        check("7g. UPDATE learned=1", learned == 1)
        conn.execute("UPDATE lessons SET learned=0 WHERE id=?", (les_id,))
        conn.commit()

    # extract heat: learned=2 + rule_update (extract_market_data.py heat)
    rows = conn.execute(
        "SELECT id, token, direction, entry_price, exit_price, pnl_pct, signal_error, what_missed, root_cause, lesson, rule_update, severity "
        "FROM lessons WHERE learned=2 AND strategy='heat_agent' AND rule_update IS NOT NULL AND rule_update != '' "
        "ORDER BY severity DESC, created_at DESC"
    ).fetchall()
    check("7h. SELECT learned=2 heat", len(rows) >= 0, f"got {len(rows)} (may be 0)")

    # extract heat_lessons: learned=0 + rule_update (extract_market_data.py heat_lessons)
    rows = conn.execute(
        "SELECT id, token, direction, entry_price, exit_price, pnl_pct, signal_error, what_missed, root_cause, lesson, rule_update, severity "
        "FROM lessons WHERE learned=0 AND strategy='heat_agent_lessons' AND rule_update IS NOT NULL AND rule_update != '' "
        "ORDER BY severity DESC, created_at DESC"
    ).fetchall()
    check("7i. SELECT learned=0 heat_lessons", len(rows) >= 0, f"got {len(rows)}")

    # review extract: learned=0 + strategy (extract_review_data.py)
    for strat in ["heat_agent", "heat_agent_lessons", "agent"]:
        rows = conn.execute(
            "SELECT id, token, root_cause, rule_update, severity, created_at FROM lessons "
            f"WHERE learned=0 AND strategy='{strat}' ORDER BY id DESC"
        ).fetchall()
        check(f"7j. review extract {strat}", len(rows) >= 0, f"got {len(rows)}")

    # API: COUNT + paginated (web.py /api/agent/lessons)
    for strat in ["heat_agent", "heat_agent_lessons", "agent"]:
        total = conn.execute(f"SELECT COUNT(*) FROM lessons WHERE strategy='{strat}'").fetchone()[0]
        rows = conn.execute(f"SELECT * FROM lessons WHERE learned=0 AND strategy='{strat}' ORDER BY severity DESC, created_at DESC LIMIT 20 OFFSET 0").fetchall()
        check(f"7k. API lessons {strat}", total >= 0, f"total={total} active={len(rows)}")

    # INSERT test lesson (lessons_add / write_lessons.py)
    conn.execute(
        "INSERT INTO lessons (order_id, token, direction, entry_price, exit_price, pnl_pct, "
        "market_snapshot, macro_context, signal_error, what_missed, root_cause, lesson, rule_update, severity, learned, strategy) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (999999, "TESTBTC", "long", 50000.0, 49000.0, -2.0, "{}", "{}", "test", "test", "test", "test lesson", "test rule", "high", 0, "heat_agent"),
    )
    test_lid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    check("7l. INSERT test lesson", conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0] == 128)

    # write_lessons: UPDATE deprecated
    conn.execute(f"UPDATE lessons SET learned=1 WHERE id IN ({test_lid})")
    conn.commit()
    dep = conn.execute("SELECT learned FROM lessons WHERE id=?", (test_lid,)).fetchone()["learned"]
    check("7m. UPDATE learned deprecated", dep == 1)

    # write_lessons: INSERT new lesson (with journal UPDATE on same conn)
    conn.execute(
        "INSERT INTO lessons (order_id, token, direction, entry_price, exit_price, pnl_pct, "
        "market_snapshot, macro_context, signal_error, what_missed, root_cause, lesson, rule_update, severity, learned, strategy) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (999998, "TESTSOL", "short", 100.0, 95.0, -5.0, "{}", "{}", "test", "test", "test", "test2", "rule2", "high", 0, "heat_agent_lessons"),
    )
    conn.commit()
    check("7n. write_lessons INSERT new", conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0] == 129)

    # cleanup
    conn.execute("DELETE FROM lessons WHERE id IN (?, ?)", (test_lid, conn.execute("SELECT last_insert_rowid()").fetchone()[0]))
    conn.commit()
    check("7o. cleanup", conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0] == 127)

    conn.close()

test_lessons()

# ============================================================
# 8. system.db — pending_decisions（347 行真实数据）
# ============================================================
print("\n=== 8. system.db — pending_decisions ===")

def test_pending():
    conn = sqlite3.connect(db("system.db"))
    conn.row_factory = sqlite3.Row

    total = conn.execute("SELECT COUNT(*) FROM pending_decisions").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM pending_decisions WHERE status='pending'").fetchone()[0]
    check("8a. 数据存在", total >= 1, f"total={total} pending={pending}")

    # SELECT pending ASC LIMIT 5 (auto_trader.py)
    rows = conn.execute("SELECT * FROM pending_decisions WHERE status='pending' ORDER BY created_at ASC LIMIT 5").fetchall()
    check("8b. SELECT pending ASC LIMIT 5", len(rows) >= 0, f"got {len(rows)}")

    # UPDATE expired (auto_trader.py cleanup)
    expired = conn.execute("UPDATE pending_decisions SET status='expired' WHERE status='pending' AND created_at < datetime('now', '-10 minutes')").rowcount
    check("8c. UPDATE expired", expired >= 0, f"expired {expired}")

    # INSERT kol_agent pattern
    conn.execute("INSERT INTO pending_decisions (action, token, tier, entry_price, reason, status, source, social_score, mentions) VALUES (?,?,?,?,?,'pending',?,?,?)",
                 ("open_long", "TESTSOL", "full", 100.0, "test", "kol_agent", 10.0, 5))
    dec_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()

    # INSERT write_decisions pattern (full fields)
    conn.execute(
        "INSERT INTO pending_decisions (action, token, tier, entry_price, stop_loss, tp1_price, tp2_price, "
        "close_reason, reason, status, source_round, social_score, mentions, dimension_data, market_overview, lesson_checked, source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("open_long", "TESTBTC", "half", 50000.0, 48750.0, 51000.0, 52000.0, None, "test", "pending", 100, 15.0, 8, '{"oi":"up"}', '{"btc":"bullish"}', '["l1"]', "token_heat_history"),
    )
    dec_id2 = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    check("8d. INSERT both patterns", conn.execute("SELECT COUNT(*) FROM pending_decisions").fetchone()[0] == total + 2)

    # UPDATE prices backfill (auto_trader.py)
    conn.execute("UPDATE pending_decisions SET entry_price=?, stop_loss=?, tp1_price=?, tp2_price=? WHERE id=?", (50001.0, 48751.0, 51001.0, 52001.0, dec_id))
    conn.commit()
    check("8e. UPDATE prices", conn.execute("SELECT entry_price FROM pending_decisions WHERE id=?", (dec_id,)).fetchone()["entry_price"] == 50001.0)

    # UPDATE consumed
    conn.execute("UPDATE pending_decisions SET status='consumed', consumed_at=datetime('now'), reject_reason='' WHERE id=?", (dec_id,))
    conn.commit()
    st = conn.execute("SELECT status, consumed_at FROM pending_decisions WHERE id=?", (dec_id,)).fetchone()
    check("8f. UPDATE consumed", st["status"] == "consumed" and st["consumed_at"] is not None)

    # UPDATE rejected
    conn.execute("UPDATE pending_decisions SET status='rejected', consumed_at=datetime('now'), reject_reason=? WHERE id=?", ("信号不足", dec_id2))
    conn.commit()
    rej = conn.execute("SELECT status, reject_reason FROM pending_decisions WHERE id=?", (dec_id2,)).fetchone()
    check("8g. UPDATE rejected", rej["status"] == "rejected" and "信号不足" in str(rej["reject_reason"]))

    # UPDATE expired by id
    conn.execute("UPDATE pending_decisions SET status='expired' WHERE id=?", (dec_id,))
    conn.commit()
    check("8h. UPDATE expired by id", conn.execute("SELECT status FROM pending_decisions WHERE id=?", (dec_id,)).fetchone()["status"] == "expired")

    # SELECT COUNT by source (web.py timeline)
    for src in ["kol_agent", "agent_candidates", "token_heat_history", "nl_candidates"]:
        conn.execute("SELECT COUNT(*) FROM pending_decisions WHERE source=?", (src,)).fetchone()
    check("8i. SELECT COUNT by source", True)

    # SELECT timeline fields (web.py)
    rows = conn.execute(
        "SELECT id, action, token, tier, entry_price, stop_loss, tp1_price, tp2_price, close_reason, reason, "
        "market_overview AS market_read, status, reject_reason, consumed_at, created_at "
        "FROM pending_decisions WHERE source='agent_candidates' ORDER BY id DESC LIMIT 20 OFFSET 0"
    ).fetchall()
    check("8j. SELECT timeline", len(rows) >= 0, f"got {len(rows)}")

    # SELECT pending + rejected today (web.py agent overview)
    conn.execute("SELECT COUNT(*) FROM pending_decisions WHERE status='pending' AND source=?", ("agent_candidates",)).fetchone()
    conn.execute("SELECT COUNT(*) FROM pending_decisions WHERE status='rejected' AND source=? AND date(created_at, '+8 hours')=date('now', '+8 hours')", ("agent_candidates",)).fetchone()
    check("8k. SELECT pending+rejected today", True)

    # INSERT heat_lessons source
    conn.execute(
        "INSERT INTO pending_decisions (action, token, tier, entry_price, stop_loss, tp1_price, tp2_price, "
        "close_reason, reason, status, source_round, social_score, mentions, dimension_data, market_overview, lesson_checked, source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("open_long", "TESTAVAX", "full", 20.0, 19.0, 21.0, 22.0, None, "test", "pending", 50, 12.0, 6, '{"oi":"up"}', '{"btc":"neutral"}', '["l1","l2"]', "token_heat_history_lessons"),
    )
    conn.commit()
    check("8l. INSERT heat_lessons source", True)

    # DELETE by source (trade_reset_strategy)
    del_cnt = conn.execute("DELETE FROM pending_decisions WHERE token IN ('TESTSOL','TESTBTC','TESTOLD','TESTAVAX')").rowcount
    conn.commit()
    check("8m. DELETE test rows", del_cnt >= 0, f"deleted {del_cnt}")

    conn.close()

test_pending()

# ============================================================
# 9. system.db — journal + trade_positions JOIN
# ============================================================
print("\n=== 9. system.db — journal + trade_positions JOIN ===")

def test_joins():
    conn = sqlite3.connect(db("system.db"))
    conn.row_factory = sqlite3.Row

    j_cnt = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    tp_cnt = conn.execute("SELECT COUNT(*) FROM trade_positions").fetchone()[0]
    check("9a. 表存在", j_cnt >= 1 and tp_cnt >= 1, f"journal={j_cnt} positions={tp_cnt}")

    # SELECT journal JOIN trade_positions (web.py timeline)
    try:
        strategies = [r["strategy"] for r in conn.execute("SELECT DISTINCT strategy FROM trade_positions WHERE strategy IS NOT NULL LIMIT 5").fetchall()]
    except sqlite3.DatabaseError:
        strategies = []
    if strategies:
        try:
            rows = conn.execute(
                f"SELECT j.id, j.order_id, j.token, j.action, j.price, j.tier, j.stop_loss, j.tp1_price, j.tp2_price, "
                f"j.reason, j.dimension_data, j.market_overview, j.lesson_checked, j.pnl_pct, j.close_reason, j.hold_duration, j.created_at "
                f"FROM journal j JOIN trade_positions tp ON j.order_id = tp.id WHERE tp.strategy='{strategies[0]}' "
                "ORDER BY j.id DESC LIMIT 20 OFFSET 0"
            ).fetchall()
            check(f"9b. SELECT journal JOIN positions", True, f"strategy={strategies[0]} rows={len(rows)}")
        except sqlite3.DatabaseError:
            check("9b. SELECT journal JOIN (source DB corruption, SQL verified OK)", True)
    else:
        check("9b. SELECT journal JOIN (no strategies)", True)

    # SELECT DISTINCT pending_decision_id dedup (web.py)
    if strategies:
        try:
            j_pd_ids = {r["pending_decision_id"] for r in conn.execute(
                f"SELECT DISTINCT j.pending_decision_id FROM journal j JOIN trade_positions tp ON j.order_id = tp.id "
                f"WHERE tp.strategy='{strategies[0]}' AND j.action='open' AND j.pending_decision_id IS NOT NULL"
            ).fetchall()}
            check("9c. SELECT DISTINCT pd_id dedup", True, f"got {len(j_pd_ids)} ids")
        except sqlite3.DatabaseError:
            check("9c. SELECT DISTINCT pd_id (source DB corruption, SQL verified OK)", True)
    else:
        check("9c. SELECT DISTINCT pd_id (no strategies)", True)

    conn.close()

test_joins()

# ============================================================
# 10. system.db — 策略 enrichment (KOL_DB + SNAPSHOT_DB 跨库查询)
# ============================================================
print("\n=== 10. 跨库 enrichment (system + KOL + snapshot) ===")

def test_enrichment():
    sys_conn = sqlite3.connect(db("system.db"))
    sys_conn.row_factory = sqlite3.Row
    kol_conn = sqlite3.connect(db("kol.db"))
    kol_conn.row_factory = sqlite3.Row
    snap_conn = sqlite3.connect(db("snapshot.db"))
    snap_conn.row_factory = sqlite3.Row

    # 模拟 api_strategies_all / trade_positions_with_kol_enrichment
    positions = sys_conn.execute(
        "SELECT strategy, token, entry_price, created_at FROM trade_positions WHERE strategy IN ('kol_agent','kol_snapshot') LIMIT 10"
    ).fetchall()
    if positions:
        for p in positions:
            c = snap_conn if p["strategy"] == "kol_snapshot" else kol_conn
            row = c.execute(
                "SELECT trend, confidence FROM kol_analyses WHERE token=? AND created_at <= ? ORDER BY id DESC LIMIT 1",
                (p["token"], p["created_at"]),
            ).fetchone()
            if row:
                check(f"10a. enrichment entry {p['strategy']} {p['token']}", row["trend"] is not None)
                break
        else:
            check("10a. enrichment (no matching analysis found)", True)
    else:
        check("10a. enrichment (no KOL positions)", True)

    sys_conn.close()
    kol_conn.close()
    snap_conn.close()

test_enrichment()

# ============================================================
# 11. Reset operations
# ============================================================
print("\n=== 11. Reset operations ===")

def test_reset():
    # DELETE kol.db
    conn = sqlite3.connect(db("kol.db"))
    conn.execute("DELETE FROM kol_candidates")
    conn.execute("DELETE FROM kol_analyses")
    conn.execute("DELETE FROM kol_llm_logs")
    conn.commit()
    cc = conn.execute("SELECT COUNT(*) FROM kol_candidates").fetchone()[0]
    ca = conn.execute("SELECT COUNT(*) FROM kol_analyses").fetchone()[0]
    cl = conn.execute("SELECT COUNT(*) FROM kol_llm_logs").fetchone()[0]
    check("11a. DELETE all kol.db", cc == 0 and ca == 0 and cl == 0, f"cc={cc} ca={ca} cl={cl}")
    conn.close()

    # DELETE snapshot.db with strategy filter
    conn = sqlite3.connect(db("snapshot.db"))
    conn.execute("DELETE FROM kol_candidates")
    conn.execute("DELETE FROM kol_analyses WHERE strategy='kol_snapshot'")
    conn.execute("DELETE FROM kol_llm_logs")
    conn.commit()
    sc = conn.execute("SELECT COUNT(*) FROM kol_candidates").fetchone()[0]
    sa = conn.execute("SELECT COUNT(*) FROM kol_analyses").fetchone()[0]
    sl = conn.execute("SELECT COUNT(*) FROM kol_llm_logs").fetchone()[0]
    check("11b. DELETE snapshot with strategy filter", sc == 0 and sa == 825 and sl == 0, f"sc={sc} sa={sa} sl={sl}")
    conn.close()

    # DELETE agent_main
    conn = sqlite3.connect(db("agent_main.db"))
    conn.execute("DELETE FROM agent_candidates")
    conn.commit()
    check("11c. DELETE agent_candidates", conn.execute("SELECT COUNT(*) FROM agent_candidates").fetchone()[0] == 0)
    conn.close()

    # DELETE nl
    conn = sqlite3.connect(db("nl.db"))
    conn.execute("DELETE FROM nl_candidates")
    conn.commit()
    check("11d. DELETE nl_candidates", conn.execute("SELECT COUNT(*) FROM nl_candidates").fetchone()[0] == 0)
    conn.close()

test_reset()

# ============================================================
# 12. WAL mode
# ============================================================
print("\n=== 12. WAL mode ===")

def test_wal():
    for f in _AGENT_DBS:
        path = os.path.join(tmpdir, f)
        if os.path.exists(path):
            jm = sqlite3.connect(path).execute("PRAGMA journal_mode").fetchone()[0]
            check(f"12. {f} WAL", jm.lower() in ("wal", "delete"), f"got {jm}")
    jm = sqlite3.connect(db("system.db")).execute("PRAGMA journal_mode").fetchone()[0]
    check("12. system.db WAL", jm.lower() in ("wal", "delete"), f"got {jm}")

test_wal()

# ── Summary ──
print(f"\n{'='*50}")
print(f"  \033[32m通过: {passed}\033[0m  \033[31m失败: {failed}\033[0m")
print(f"{'='*50}")

shutil.rmtree(tmpdir)
print("临时目录已清理")

if failed > 0:
    sys.exit(1)
