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
    "SELECT j.* FROM journal j "
    "LEFT JOIN trade_positions tp ON j.order_id = tp.id "
    "WHERE j.reviewed = 0 "
    "AND tp.strategy = 'heat_agent_lessons' "
    "ORDER BY j.id LIMIT 1000"
)]
journal_ids = [j["id"] for j in journal]

# ---- 开/平分组 ----
opens = [j for j in journal if j["action"] == "open"]
closes = [j for j in journal if j["action"] == "close"]

# ---- 噪音止损过滤（5 维度加权评分）----
def _noise_score(open_dd: dict, close_dd: dict, close_price: float) -> tuple[int, bool]:
    """返回 (总分, 是否存在单维度强烈指向信号错误)
    has_severe = True 表示至少一个维度 ≤ -2，即使总分够也不应自动过滤"""
    s = 0
    severe = False

    # 1. OI 动量
    oi15_open = open_dd.get("OI15分钟变化")
    oi15_close = close_dd.get("OI15分钟变化")
    if oi15_open is not None and oi15_close is not None:
        try:
            v15_open, v15_close = float(oi15_open), float(oi15_close)
            if v15_close >= 2:
                s += 2
            elif v15_close <= -2:
                s -= 2
                severe = True
        except (TypeError, ValueError):
            pass

    # 2. OI 加速度
    oi1h_open = open_dd.get("OI1小时变化")
    oi1h_close = close_dd.get("OI1小时变化")
    if all(k is not None for k in [oi1h_open, oi1h_close, oi15_open, oi15_close]):
        try:
            vo15o, vo15c = float(oi15_open), float(oi15_close)
            vo1ho, vo1hc = float(oi1h_open), float(oi1h_close)
            acc_open = vo1ho - vo15o
            acc_close = vo1hc - vo15c
            if acc_close > acc_open + 2:
                s += 1
            elif acc_close < acc_open - 2:
                s -= 1
                severe = True
        except (TypeError, ValueError):
            pass

    # 3. taker 方向
    taker_close = close_dd.get("主动买卖比")
    if taker_close is not None:
        try:
            tc = float(taker_close)
            if tc > 1.05:
                s += 2
            elif tc < 0.95:
                s -= 2
                severe = True
        except (TypeError, ValueError):
            pass

    # 4. 跌幅幅度
    entry_price = open_dd.get("标记价")
    if entry_price is not None and close_price is not None:
        try:
            ep = float(entry_price)
            if ep > 0:
                loss_pct = (close_price - ep) / ep * 100
                if -3 <= loss_pct <= -1.5:
                    s += 2
                elif loss_pct < -4:
                    s -= 2
                    severe = True
        except (TypeError, ValueError):
            pass

    # 5. 盘口健康度
    imbalance = close_dd.get("盘口失衡度")
    if imbalance is not None:
        try:
            imb = float(imbalance)
            if imb > -15:
                s += 1
            elif imb < -30:
                s -= 1
                severe = True
        except (TypeError, ValueError):
            pass

    return s, severe


# 先去掉没有对应 close 的孤立 open（未平仓，不该复盘）
# 这样可以保证后续噪音检测时每笔 close 都有配对 open
close_order_ids_set = {c["order_id"] for c in closes if c.get("order_id")}
orphan_opens = [o for o in opens if o.get("order_id") not in close_order_ids_set]
if orphan_opens:
    orphan_ids = {o["id"] for o in orphan_opens}
    opens = [o for o in opens if o["id"] not in orphan_ids]
    journal = [j for j in journal if j["id"] not in orphan_ids]
    journal_ids = [j["id"] for j in journal]
    print(f"  跳过孤立开仓（未平仓）: {len(orphan_opens)} 条")

# ---- 噪音止损过滤（5 维度加权评分）----
noise_ids = []
if opens and closes:
    open_by_order = {}
    for o in opens:
        oid = o.get("order_id")
        if oid:
            open_by_order[oid] = o

    for c in closes:
        if c.get("close_reason") != "sl_hit":
            continue
        oid = c.get("order_id")
        if not oid:
            continue
        # 孤儿已过滤，此处必有配对 open
        try:
            open_dd = json.loads(open_by_order[oid].get("dimension_data") or "{}")
        except (json.JSONDecodeError, TypeError):
            open_dd = {}
        try:
            close_dd = json.loads(c.get("dimension_data") or "{}")
        except (json.JSONDecodeError, TypeError):
            close_dd = {}
        score, severe = _noise_score(open_dd, close_dd, c.get("price") or 0)
        if score >= 5 and not severe:
            noise_ids.append(c["id"])

# 自动标记噪音为已复盘（close + 配对的 open 一起标记）
if noise_ids:
    noise_set = set(noise_ids)
    paired_open_ids = [open_by_order[c["order_id"]]["id"]
                       for c in closes if c["id"] in noise_set]
    noise_set.update(paired_open_ids)  # open + close 一起从输出排除
    all_noise_ids = list(noise_set)
    ph = ",".join("?" * len(all_noise_ids))
    conn.execute(f"UPDATE journal SET reviewed=1 WHERE id IN ({ph})", all_noise_ids)
    conn.commit()
    journal = [j for j in journal if j["id"] not in noise_set]
    journal_ids = [j["id"] for j in journal]
    opens = [j for j in opens if j["id"] not in noise_set]
    closes = [j for j in closes if j["id"] not in noise_set]
    print(f"  噪音止损已过滤: {len(noise_ids)} 条 close + {len(paired_open_ids)} 条 open")

# ---- 未复盘 journal 涉及的平仓持仓 ----
close_order_ids = [j["order_id"] for j in closes if j.get("order_id")]
closed_positions = []
if close_order_ids:
    ph = ",".join("?" * len(close_order_ids))
    closed_positions = [dict(r) for r in conn.execute(
        f"""SELECT * FROM trade_positions
            WHERE status='CLOSED'
            AND strategy = 'heat_agent_lessons'
            AND id IN ({ph})
            ORDER BY closed_at""",
        close_order_ids,
    )]

# ---- 已有 lessons ----
existing_lessons = [dict(r) for r in conn.execute(
    "SELECT id, token, root_cause, rule_update, severity, created_at "
    "FROM lessons WHERE learned=0 AND strategy='heat_agent_lessons' ORDER BY id DESC"
)]

def _safe_str(v):
    if v is None: return ""
    if isinstance(v, dict): return "。".join(f"{k}: {v}" for k, v in v.items())
    return str(v).strip()

existing_rules = list(set(
    _safe_str(l.get("rule_update")) for l in existing_lessons if l.get("rule_update")
))

# ---- 止损标签统计（Agent 开的单）----
tag_stats = {}
for r in conn.execute(
    """SELECT la.reason_tags FROM trade_loss_archive la
       JOIN trade_positions tp ON la.position_id = tp.id
       WHERE la.reason_tags IS NOT NULL
       AND tp.strategy = 'heat_agent_lessons'"""
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
    "existing_lessons": existing_lessons,
    "existing_rules": existing_rules,
    "tag_stats": tag_stats,
}

with open(args.output, "w", encoding="utf-8") as f:
    json.dump(output, f, default=str, ensure_ascii=False)

print(f"数据已写入 {args.output}")
print(f"  未复盘 journal: {len(journal)} 条（开仓 {len(opens)} / 平仓 {len(closes)}）")
print(f"  涉及平仓: {len(closed_positions)} 笔")
print(f"  已有 lessons: {len(existing_lessons)} 条")
