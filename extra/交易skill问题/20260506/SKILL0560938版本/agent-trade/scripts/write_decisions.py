#!/usr/bin/env python3
"""将 Agent 决策写入 pending_decisions 表。读取 Agent 生成的决策 JSON 文件并写入 DB。"""
import argparse
import json
import os
import sqlite3
import sys

parser = argparse.ArgumentParser(description="Write Agent decisions to pending_decisions")
parser.add_argument("--decisions", required=True, help="Path to Agent-generated decisions JSON file")
args = parser.parse_args()

# DB 定位：AGENT_DB_ROOT 已包含项目路径，拼接文件名即可
DB_NAME = "binance_square.db"

try:
    sys.path.insert(0, ".")
    import config
    db_root = getattr(config, "AGENT_DB_ROOT", "")
except Exception:
    db_root = ""

if db_root:
    DB = os.path.join(os.path.expanduser(db_root), DB_NAME)
else:
    DB = os.path.join(os.path.expanduser("~/binance-monitor/bsm-agent"), DB_NAME)

if not os.path.exists(DB):
    print(f"ERROR: 找不到 {DB}")
    sys.exit(1)

# 读 Agent 的决策
with open(args.decisions, "r", encoding="utf-8") as f:
    data = json.load(f)

decisions = data.get("decisions", [])
market_read = data.get("market_read", "")

if not decisions:
    conn = sqlite3.connect(DB)
    total = conn.execute(
        "SELECT COUNT(*) FROM round_candidates WHERE delivered=0"
    ).fetchone()[0]
    conn.close()
    print(f"空决策：不标记候选币，留待下一轮（当前 {total} 条未交付）")
    sys.exit(0)

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

for d in decisions:
    action = d.get("action", "")
    token = d.get("token", "")
    if action not in ("open_long", "close"):
        print(f"跳过无效 action: {action}")
        continue
    if not token or not d.get("reason"):
        print(f"跳过不完整: token={token} reason={bool(d.get('reason'))}")
        continue

    conn.execute(
        """INSERT INTO pending_decisions
            (action, token, tier, entry_price, stop_loss, tp1_price, tp2_price,
             close_reason, reason, status,
             source_round, social_score, mentions,
             dimension_data, market_overview, lesson_checked)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            d["action"], d["token"], d.get("tier"),
            d.get("entry_price"), d.get("stop_loss"),
            d.get("tp1_price"), d.get("tp2_price"),
            d.get("close_reason"), d["reason"], "pending",
            d.get("source_round"), d.get("social_score"),
            d.get("mentions"),
            d.get("dimension_data"), d.get("market_overview"),
            d.get("lesson_checked"),
        ),
    )

# 只标记被交易币种的 candidate_ids
traded_tokens = set(d["token"] for d in decisions)
marked_count = 0
for token in traded_tokens:
    rows = conn.execute(
        "SELECT id FROM round_candidates WHERE token=? AND delivered=0",
        (token,),
    ).fetchall()
    if rows:
        ids = [r[0] for r in rows]
        placeholders = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE round_candidates SET delivered=1 WHERE id IN ({placeholders})",
            ids,
        )
        marked_count += len(ids)
        print(f"标记 {token}: {len(ids)} 条候选已交付")

conn.commit()

# 验证写入
verify = conn.execute(
    "SELECT token, action, status FROM pending_decisions "
    "ORDER BY rowid DESC LIMIT ?",
    (max(len(decisions), 1),),
).fetchall()
for v in verify:
    print(f"决策验证: {v['token']} {v['action']} -> status={v['status']}")

conn.close()
print(f"\n写入 {len(decisions)} 条决策，已标记 {marked_count} 条候选")
