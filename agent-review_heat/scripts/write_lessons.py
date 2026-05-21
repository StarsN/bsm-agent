#!/usr/bin/env python3
"""将 Agent 复盘教训写入 lessons 表。去重由 Agent 在写入前自查 existing_lessons 完成。"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description="Write review lessons to DB")
parser.add_argument("--lessons", required=True, help="Path to Agent-generated lessons JSON file")
parser.add_argument("--journal-ids", required=True, help="'NONE' or comma-separated journal IDs to mark as reviewed")
parser.add_argument("--strategy", default="heat_agent", help="Strategy")
args = parser.parse_args()

# 路径：基于脚本自身位置
SCRIPT_DIR = Path(__file__).resolve().parent          # agent-review/scripts/
PROJECT_DIR = SCRIPT_DIR.parent.parent                # bsm-agent/
sys.path.insert(0, str(PROJECT_DIR))

DB_NAME = "binance_square.db"
try:
    import config
    db_root = getattr(config, "AGENT_DB_ROOT", "")
except Exception:
    db_root = ""

if db_root:
    DB = str(Path(os.path.expanduser(db_root)) / DB_NAME)
else:
    DB = str(PROJECT_DIR / DB_NAME)

if not os.path.exists(DB):
    print(f"ERROR: 找不到 {DB}")
    sys.exit(1)

# 读 Agent 写的教训
with open(args.lessons, "r", encoding="utf-8") as f:
    data = json.load(f)

if not isinstance(data, dict):
    print(f"ERROR: JSON 顶层必须是对象（信封格式），当前是 {type(data).__name__}")
    print('正确格式: {"lessons": [...]}')
    sys.exit(1)
if "lessons" not in data:
    print("ERROR: JSON 缺少 'lessons' 字段")
    print('正确格式: {"lessons": [...]}')
    sys.exit(1)

lessons = data["lessons"]
deprecate_ids = data.get("deprecate_ids", [])

def _safe_str(v):
    """兼容 dict 和 string 类型的 rule_update"""
    if v is None:
        return ""
    if isinstance(v, dict):
        return "。".join(f"{k}: {v}" for k, v in v.items())
    return str(v).strip()


conn = sqlite3.connect(DB)

# 废弃旧规则
if deprecate_ids:
    ph = ",".join("?" * len(deprecate_ids))
    conn.execute(f"UPDATE lessons SET learned=1 WHERE id IN ({ph})", deprecate_ids)
    print(f"已废弃 {len(deprecate_ids)} 条旧教训: {deprecate_ids}")

written = 0
for l in lessons:
    rule = _safe_str(l.get("rule_update"))

    conn.execute(
        """INSERT INTO lessons
            (order_id, token, direction, entry_price, exit_price, pnl_pct,
             market_snapshot, macro_context, signal_error, what_missed,
             root_cause, lesson, rule_update, severity, learned, strategy)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            l.get("order_id"), l["token"], l.get("direction"),
            l.get("entry_price"), l.get("exit_price"), l.get("pnl_pct"),
            l.get("market_snapshot"), l.get("macro_context"),
            l.get("signal_error"), l.get("what_missed"),
            l.get("root_cause"), l.get("lesson", ""),
            _safe_str(rule) or None, l.get("severity", "medium"),
            0, args.strategy,
        ),
    )
    written += 1
    print(f"写入: {l['token']} [{l.get('severity', 'medium')}] {l.get('lesson', '')[:60]}")

# 标记 journal 已复盘
if args.journal_ids and args.journal_ids.strip().upper() != "NONE":
    ids = [int(x.strip()) for x in args.journal_ids.split(",") if x.strip()]
    if ids:
        ph = ",".join("?" * len(ids))
        conn.execute(f"UPDATE journal SET reviewed = 1 WHERE id IN ({ph})", ids)
        print(f"已标记 {len(ids)} 条 journal 为已复盘")

conn.commit()
conn.close()
print(f"\n写入 {written} 条")
