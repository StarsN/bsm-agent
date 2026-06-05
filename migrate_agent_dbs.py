#!/usr/bin/env python3
"""DB分库数据迁移 — 纯 sqlite3，不依赖系统代码。

用法:
  python migrate_agent_dbs.py <system.db> [--dry-run] [--out-dir <dir>]

  system.db         源数据库路径
  --dry-run         只统计不写入
  --out-dir <dir>   输出目录，默认与 system.db 同目录
"""
import sqlite3, os, sys, argparse


def row_count(conn, table, where="1=1"):
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]
    except sqlite3.Error as e:
        return f"ERROR: {e}"


def copy_table(src, dst_path, table, where="1=1", bindings=None, dry=False):
    """复制表数据，select 出全部列 → insert 到目标库"""
    try:
        if not os.path.exists(dst_path):
            dst = sqlite3.connect(dst_path)
            dst.execute("PRAGMA journal_mode = WAL")
            dst.close()
    except sqlite3.Error as e:
        print(f"  {table}: INIT ERROR ({e})")
        return 0

    try:
        # 用 PRAGMA table_info 反推列定义（兼容 ALTER TABLE 后的库）
        src_cols = src.execute(f"PRAGMA table_info({table})").fetchall()
        col_defs = []
        for c in src_cols:
            col_name = c[1]
            col_type = c[2] or "TEXT"
            default = f" DEFAULT {c[4]}" if c[4] is not None else ""
            not_null = " NOT NULL" if c[3] else ""
            pk = " PRIMARY KEY AUTOINCREMENT" if c[5] else ""
            col_defs.append(f'"{col_name}" {col_type}{not_null}{default}{pk}')

        dst = sqlite3.connect(dst_path)
        dst.execute(f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(col_defs)})")

        # 复制索引
        idx_rows = src.execute(
            f"SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='{table}' AND sql IS NOT NULL"
        ).fetchall()
        for ir in idx_rows:
            try:
                dst.execute(ir[0])
            except sqlite3.Error:
                pass

        dst.commit()
        dst.close()
    except sqlite3.Error as e:
        print(f"  {table}: CREATE ERROR ({e})")
        return 0

    try:
        if bindings:
            rows = src.execute(f"SELECT * FROM {table} WHERE {where}", bindings).fetchall()
        else:
            rows = src.execute(f"SELECT * FROM {table} WHERE {where}").fetchall()
    except sqlite3.Error as e:
        print(f"  {table}: READ ERROR ({e})")
        return 0

    if not rows:
        print(f"  {table}: 0 rows")
        return 0

    if dry:
        print(f"  {table}: {len(rows)} rows (dry-run)")
        return len(rows)

    try:
        dst = sqlite3.connect(dst_path)
        cols = [c[1] for c in dst.execute(f"PRAGMA table_info({table})")]
        placeholders = ", ".join("?" * len(cols))
        col_names = ", ".join(cols)
        for r in rows:
            vals = [r[c] for c in cols]
            dst.execute(f"INSERT OR IGNORE INTO {table} ({col_names}) VALUES ({placeholders})", vals)
        dst.commit()
        dst.close()
        print(f"  {table}: {len(rows)} rows -> {os.path.basename(dst_path)}")
        return len(rows)
    except sqlite3.Error as e:
        print(f"  {table}: WRITE ERROR ({e})")
        return 0


def main():
    parser = argparse.ArgumentParser(description="DB分库数据迁移")
    parser.add_argument("system_db", help="源 system.db 路径")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    parser.add_argument("--out-dir", help="输出目录，默认同 system.db")
    args = parser.parse_args()

    src_path = args.system_db
    if not os.path.exists(src_path):
        print(f"ERROR: {src_path} not found")
        sys.exit(1)

    out_dir = args.out_dir or os.path.dirname(src_path) or "."
    dry = args.dry_run

    db = lambda name: os.path.join(out_dir, name)

    src = sqlite3.connect(src_path)
    src.row_factory = sqlite3.Row

    total = 0
    mode = "DRY RUN" if dry else "MIGRATE"
    print(f"  {mode}: {src_path} -> {out_dir}/")
    print()

    # 1. agent_candidates -> agent_main.db
    print("[1] agent_candidates")
    total += copy_table(src, db("agent_main.db"), "agent_candidates", dry=dry)

    # 2. nl_candidates -> nl.db
    print("\n[2] nl_candidates")
    total += copy_table(src, db("nl.db"), "nl_candidates", dry=dry)

    # 3. kol_candidates -> kol.db + snapshot.db (共享，各拷一份)
    print("\n[3] kol_candidates -> kol.db + snapshot.db")
    n = copy_table(src, db("kol.db"), "kol_candidates", dry=dry)
    total += n
    copy_table(src, db("snapshot.db"), "kol_candidates", dry=dry)

    # 4. kol_analyses -> kol.db + snapshot.db (全量各拷一份)
    print("\n[4] kol_analyses -> kol.db + snapshot.db")
    n = copy_table(src, db("kol.db"), "kol_analyses", dry=dry)
    total += n
    copy_table(src, db("snapshot.db"), "kol_analyses", dry=dry)

    # 5. kol_llm_logs -> kol.db + snapshot.db (全量各拷一份)
    print("\n[5] kol_llm_logs -> kol.db + snapshot.db")
    n = copy_table(src, db("kol.db"), "kol_llm_logs", dry=dry)
    total += n
    copy_table(src, db("snapshot.db"), "kol_llm_logs", dry=dry)

    src.close()
    print(f"\n  Done. {total} rows migrated.")
    if dry:
        print("  Run without --dry-run to execute.")


if __name__ == "__main__":
    main()
