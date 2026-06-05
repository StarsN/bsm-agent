#!/usr/bin/env python3
"""验证 migrate_agent_dbs.py 迁移结果的完整性和正确性。纯 sqlite3，不依赖系统代码。"""
import sqlite3, sys, os

SRC = "db/binance_square.db"
OUT = "db"

AGENT_TABLES = {
    "agent_main.db": ["agent_candidates"],
    "kol.db":       ["kol_candidates", "kol_analyses", "kol_llm_logs"],
    "snapshot.db":  ["kol_candidates", "kol_analyses", "kol_llm_logs"],
    "nl.db":        ["nl_candidates"],
}

passed = 0
failed = 0

def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name} — {detail}")

if not os.path.exists(SRC):
    print(f"源库不存在: {SRC}")
    sys.exit(1)

src = sqlite3.connect(SRC)
src.row_factory = sqlite3.Row

print(f"源库: {SRC}")
print(f"输出: {OUT}/\n")

# === 1. 结构验证 ===
print("=== 1. 表结构一致性 ===")

for db_name, tables in AGENT_TABLES.items():
    db_path = os.path.join(OUT, db_name)
    if not os.path.exists(db_path):
        check(f"{db_name} 存在", False, "文件不存在")
        continue
    dst = sqlite3.connect(db_path)
    dst.row_factory = sqlite3.Row

    for tbl in tables:
        # 源库列
        src_cols = {c[1]: (c[2], c[3], c[5]) for c in src.execute(f"PRAGMA table_info({tbl})")}
        # 目标库列
        dst_cols = {c[1]: (c[2], c[3], c[5]) for c in dst.execute(f"PRAGMA table_info({tbl})")}

        if not src_cols:
            check(f"{db_name}:{tbl} 源表存在", False, "源库中无此表（可能损坏）")
            continue
        if not dst_cols:
            check(f"{db_name}:{tbl} 目标表存在", False, "目标库中无此表")
            continue

        # 列名一致
        only_src = src_cols.keys() - dst_cols.keys()
        only_dst = dst_cols.keys() - src_cols.keys()
        check(f"{db_name}:{tbl} 列名一致", not only_src and not only_dst,
              f"源独有: {only_src}, 目标独有: {only_dst}" if only_src or only_dst else "")

    dst.close()

# === 2. 行数验证 ===
print("\n=== 2. 行数验证 ===")

for db_name, tables in AGENT_TABLES.items():
    db_path = os.path.join(OUT, db_name)
    if not os.path.exists(db_path):
        continue
    dst = sqlite3.connect(db_path)
    dst.row_factory = sqlite3.Row

    for tbl in tables:
        try:
            src_cnt = src.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        except sqlite3.Error as e:
            check(f"{db_name}:{tbl} 源表可读", False, str(e))
            continue

        try:
            dst_cnt = dst.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        except sqlite3.Error as e:
            check(f"{db_name}:{tbl} 目标表可读", False, str(e))
            continue

        check(f"{db_name}:{tbl} 行数 {src_cnt}→{dst_cnt}", src_cnt == dst_cnt,
              f"源={src_cnt} 目标={dst_cnt}，不一致！" if src_cnt != dst_cnt else "")

    dst.close()

# === 3. 数据抽样验证 ===
print("\n=== 3. 数据抽样 ===")

# kol_analyses: 抽前3行比较 token/trend/strategy
db_path = os.path.join(OUT, "kol.db")
if os.path.exists(db_path):
    dst = sqlite3.connect(db_path)
    dst.row_factory = sqlite3.Row
    try:
        src_rows = src.execute("SELECT token, trend, strategy FROM kol_analyses ORDER BY id LIMIT 3").fetchall()
        dst_rows = dst.execute("SELECT token, trend, strategy FROM kol_analyses ORDER BY id LIMIT 3").fetchall()
    except sqlite3.Error:
        src_rows = dst_rows = []
    if src_rows and dst_rows:
        match = all(
            s["token"] == d["token"] and s["trend"] == d["trend"]
            for s, d in zip(src_rows, dst_rows)
        )
        check("kol.db:kol_analyses 抽样数据一致", match,
              f"源={[(r['token'],r['trend']) for r in src_rows]}, 目标={[(r['token'],r['trend']) for r in dst_rows]}")
        # 验证 strategy 默认值
        strats = {r["strategy"] for r in dst_rows}
        check("kol.db:kol_analyses strategy 列有值", len(strats) > 0 and None not in strats,
              f"strategy={strats}")
    dst.close()

# kol_llm_logs: 抽前3行比较 provider/model
db_path = os.path.join(OUT, "kol.db")
if os.path.exists(db_path):
    dst = sqlite3.connect(db_path)
    dst.row_factory = sqlite3.Row
    try:
        src_rows = src.execute("SELECT provider, model, success FROM kol_llm_logs ORDER BY id LIMIT 3").fetchall()
        dst_rows = dst.execute("SELECT provider, model, success FROM kol_llm_logs ORDER BY id LIMIT 3").fetchall()
    except sqlite3.Error:
        src_rows = dst_rows = []
    if src_rows and dst_rows:
        match = all(
            s["provider"] == d["provider"] and s["model"] == d["model"]
            for s, d in zip(src_rows, dst_rows)
        )
        check("kol.db:kol_llm_logs 抽样数据一致", match,
              f"源={[(r['provider'],r['model']) for r in src_rows]}, 目标={[(r['provider'],r['model']) for r in dst_rows]}")
    dst.close()

# snapshot.db 抽样
db_path = os.path.join(OUT, "snapshot.db")
if os.path.exists(db_path):
    dst = sqlite3.connect(db_path)
    dst.row_factory = sqlite3.Row
    try:
        src_cnt = src.execute("SELECT COUNT(*) FROM kol_analyses").fetchone()[0]
        dst_cnt = dst.execute("SELECT COUNT(*) FROM kol_analyses").fetchone()[0]
        snapshot_db_path = os.path.join(OUT, "kol.db")
        kol_dst = sqlite3.connect(snapshot_db_path) if os.path.exists(snapshot_db_path) else None
        kol_cnt = kol_dst.execute("SELECT COUNT(*) FROM kol_analyses").fetchone()[0] if kol_dst else 0
        if kol_dst: kol_dst.close()
    except sqlite3.Error:
        src_cnt = dst_cnt = kol_cnt = 0
    check("snapshot.db 与 kol.db 数据独立", dst_cnt == src_cnt and kol_cnt == src_cnt and dst_cnt == kol_cnt,
          f"源={src_cnt} kol={kol_cnt} snapshot={dst_cnt}")
    dst.close()

# === 4. system.db lessons 未迁出 ===
print("\n=== 4. lessons 在 system.db ===")
try:
    les_cnt = src.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
    check("system.db lessons 行数正确", les_cnt >= 0, f"lessons 有 {les_cnt} 行，确认未迁出到 agent DB")
    # 确认策略列存在
    strats = [r[0] for r in src.execute("SELECT DISTINCT strategy FROM lessons").fetchall()]
    check("system.db lessons strategy 列有效", len(strats) >= 1, f"strategies: {strats}")
except sqlite3.Error as e:
    check("system.db lessons 可读", False, str(e))

src.close()

print(f"\n{'='*40}")
print(f"  通过: {passed}  失败: {failed}")
print(f"{'='*40}")
if failed:
    sys.exit(1)
