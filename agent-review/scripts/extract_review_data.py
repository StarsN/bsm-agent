#!/usr/bin/env python3
"""提取今日复盘数据，写 JSON 到指定文件。"""
import argparse
import sqlite3
import json
import os
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description="Extract review data")
parser.add_argument("--output", required=True, help="Output JSON file path")
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

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# ---- 未复盘的 journal ----
journal = [dict(r) for r in conn.execute(
    "SELECT * FROM journal WHERE reviewed = 0 ORDER BY id LIMIT 200"
)]
journal_ids = [j["id"] for j in journal]

# ---- 开/平分组 ----
opens = [j for j in journal if j["action"] == "open"]
closes = [j for j in journal if j["action"] == "close"]

# ---- 未复盘 journal 涉及的平仓持仓 ----
close_order_ids = [j["order_id"] for j in closes if j.get("order_id")]
closed_positions = []
if close_order_ids:
    ph = ",".join("?" * len(close_order_ids))
    closed_positions = [dict(r) for r in conn.execute(
        f"""SELECT * FROM trade_positions
            WHERE status='CLOSED'
            AND id IN ({ph})
            ORDER BY closed_at""",
        close_order_ids,
    )]

# ---- 活跃持仓 ----
open_positions = [dict(r) for r in conn.execute(
    """SELECT token, side, entry_price, current_price, stop_loss_price,
       tp1_price, tp2_price, pnl_pct, margin_amount, highest_price,
       open_reason, created_at
       FROM trade_positions
       WHERE status IN ('OPEN','PARTIAL')
       AND json_extract(signal_snapshot, '$.source') = 'agent'"""
)]

# ---- 已有 lessons ----
existing_lessons = [dict(r) for r in conn.execute(
    "SELECT id, token, root_cause, rule_update, severity, created_at "
    "FROM lessons WHERE learned=0 ORDER BY id DESC"
)]

existing_rules = list(set(
    l["rule_update"].strip() for l in existing_lessons if l.get("rule_update")
))

# ---- 止损标签统计（Agent 开的单）----
tag_stats = {}
for r in conn.execute(
    """SELECT la.reason_tags FROM trade_loss_archive la
       JOIN trade_positions tp ON la.position_id = tp.id
       WHERE la.reason_tags IS NOT NULL
       AND json_extract(tp.signal_snapshot, '$.source') = 'agent'"""
):
    for t in json.loads(r["reason_tags"]):
        tag_stats[t] = tag_stats.get(t, 0) + 1

conn.close()

output = {
    "journal_ids": journal_ids,
    "journal": journal,
    "opens": opens,
    "closes": closes,
    "closed_positions": closed_positions,
    "open_positions": open_positions,
    "existing_lessons": existing_lessons,
    "existing_rules": existing_rules,
    "tag_stats": tag_stats,
}

with open(args.output, "w", encoding="utf-8") as f:
    json.dump(output, f, default=str, ensure_ascii=False)

print(f"数据已写入 {args.output}")
print(f"  未复盘 journal: {len(journal)} 条（开仓 {len(opens)} / 平仓 {len(closes)}）")
print(f"  涉及平仓: {len(closed_positions)} 笔  活跃: {len(open_positions)} 笔")
print(f"  已有 lessons: {len(existing_lessons)} 条")
