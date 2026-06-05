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
parser.add_argument("--source", default="token_heat_history", help="Data source")
args = parser.parse_args()

# 路径：基于脚本自身位置，不依赖工作目录
SCRIPT_DIR = Path(__file__).resolve().parent          # agent-trade/scripts/
PROJECT_DIR = SCRIPT_DIR.parent.parent                # bsm-agent/
sys.path.insert(0, str(PROJECT_DIR))

try:
    import config
    db_root = getattr(config, "AGENT_DB_ROOT", "")
    DB_NAME = getattr(config, "DB_PATH", "db/binance_square.db")
except Exception:
    db_root = ""
    DB_NAME = "db/binance_square.db"

if db_root:
    DB = str(Path(os.path.expanduser(db_root)) / DB_NAME)
else:
    DB = str(PROJECT_DIR / DB_NAME)

if not os.path.exists(DB):
    print(f"ERROR: 找不到 {DB}")
    sys.exit(1)

# 字段翻译映射：Agent 用 extraction 脚本输出的短名，入库时自动转中文
FIELD_CN = {
    "price": "标记价",
    "15m": "15分钟涨跌",
    "1h": "1小时涨跌",
    "4h": "4小时涨跌",
    "24h": "24小时涨跌",
    "chg_48h": "48小时涨跌",
    "funding": "资金费率(%/8h)",
    "oi_usd": "未平仓(USD)",
    "oi_15m": "OI15分钟变化",
    "oi_1h": "OI1小时变化",
    "oi_4h": "OI4小时变化",
    "oi_48h": "OI48小时变化",
    "taker": "主动买卖比",
    "taker_pct": "主动买入占比",
    "taker_trend": "Taker趋势",
    "spread": "盘口价差",
    "depth_bid": "买盘深度(USD)",
    "depth_ask": "卖盘深度(USD)",
    "imbalance": "盘口失衡度",
    "vol_24h": "24小时成交额",
    "lsr": "散户多空比",
    "top_lsr": "大户多空比",
    "social_score": "社交热度分",
    "mentions": "提及次数",
    "tags": "信号标签",
    "notes": "分析备注",
    "oi_divergence": "OI背离",
    "age": "上币时长",
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
    print("空决策")
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
             dimension_data, market_overview, lesson_checked, source)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            d["action"], d["token"], d.get("tier"),
            d.get("entry_price"), d.get("stop_loss"),
            d.get("tp1_price"), d.get("tp2_price"),
            d.get("close_reason"), d["reason"], "pending",
            d.get("source_round"), d.get("social_score"),
            d.get("mentions"),
            _translate_dimension_data(d.get("dimension_data")), d.get("market_overview"),
            d.get("lesson_checked"), args.source,
        ),
    )

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
print(f"\n写入 {len(decisions)} 条决策")
