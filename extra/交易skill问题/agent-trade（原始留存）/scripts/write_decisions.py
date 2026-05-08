#!/usr/bin/env python3
"""将 Agent 决策写入 pending_decisions 表。读取 Agent 生成的决策 JSON 文件并写入 DB。"""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description="Write Agent decisions to pending_decisions")
parser.add_argument("--decisions", required=True, help="Path to Agent-generated decisions JSON file")
args = parser.parse_args()

# 路径：基于脚本自身位置，不依赖工作目录
SCRIPT_DIR = Path(__file__).resolve().parent          # agent-trade/scripts/
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

# 字段翻译映射：Agent 用英文 key 写 dimension_data，入库时自动转中文
FIELD_CN = {
    "mark_price": "标记价",
    "change_15m_pct": "15分钟涨跌",
    "change_1h_pct": "1小时涨跌",
    "change_4h_pct": "4小时涨跌",
    "change_24h_pct": "24小时涨跌",
    "change_48h_pct": "48小时涨跌",
    "funding_rate_pct": "资金费率(%/8h)",
    "oi_usd": "未平仓(USD)",
    "oi_change_15m_pct": "OI15分钟变化",
    "oi_change_1h_pct": "OI1小时变化",
    "oi_change_4h_pct": "OI4小时变化",
    "oi_change_48h_pct": "OI48小时变化",
    "taker_buy_sell_ratio": "主动买卖比",
    "taker_buy_pct": "主动买入占比",
    "taker_trend_pct": "Taker趋势",
    "bid_ask_spread_pct": "盘口价差",
    "depth_bid_1pct_usd": "买盘深度(USD)",
    "depth_ask_1pct_usd": "卖盘深度(USD)",
    "depth_imbalance_pct": "盘口失衡度",
    "volume_24h_usd": "24小时成交额",
    "long_short_ratio": "散户多空比",
    "top_trader_ls_ratio": "大户多空比",
}


def _translate_dimension_data(raw: str | None) -> str | None:
    """把 dimension_data 里的英文 key 翻译成中文，方便复盘阅读。"""
    if not raw:
        return raw
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw
    if not isinstance(data, dict):
        return raw
    return json.dumps(
        {FIELD_CN.get(k, k): v for k, v in data.items()},
        ensure_ascii=False,
    )


# 读 Agent 的决策
with open(args.decisions, "r", encoding="utf-8") as f:
    data = json.load(f)

# 校验格式
if not isinstance(data, dict):
    print(f"ERROR: JSON 顶层必须是对象（信封格式），当前是 {type(data).__name__}")
    print("正确格式: {\"market_read\": \"...\", \"decisions\": [...]}")
    sys.exit(1)
if "decisions" not in data:
    print("ERROR: JSON 缺少 'decisions' 字段")
    print("正确格式: {\"market_read\": \"...\", \"decisions\": [...]}")
    sys.exit(1)
if "market_read" not in data:
    print("ERROR: JSON 缺少 'market_read' 字段（空决策也必须填）")
    print("正确格式: {\"market_read\": \"...\", \"decisions\": [...]}")
    sys.exit(1)

decisions = data["decisions"]
market_read = data["market_read"]

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
            _translate_dimension_data(d.get("dimension_data")), d.get("market_overview"),
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
