#!/usr/bin/env python3
"""修复 dimension_data：英文 key → 中文 key，同时修复编码乱码"""
import sqlite3
import json
import os
import sys

DB = "extra/binance_square_0507.db"
if not os.path.exists(DB):
    DB = "binance_square.db"
if not os.path.exists(DB):
    print(f"找不到数据库文件")
    sys.exit(1)

# 和 write_decisions.py 一致的映射
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
    "verdict": "信号判定",
    "direction": "走向",
    "tags": "信号标签",
    "notes": "分析备注",
    "oi_divergence": "OI背离",
}

def fix_dimension_data(raw):
    if not raw:
        return raw, 0
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw, 0
    if not isinstance(data, dict):
        return raw, 0

    fixed = {}
    changed = 0
    for k, v in data.items():
        cn = FIELD_CN.get(k, k)  # 有映射就翻译，没映射保持原样
        if cn != k:
            changed += 1
        fixed[cn] = v

    return json.dumps(fixed, ensure_ascii=False), changed


conn = sqlite3.connect(DB)

# 修复 pending_decisions
for table in ["pending_decisions", "journal"]:
    try:
        rows = conn.execute(
            f"SELECT id, dimension_data FROM {table} WHERE dimension_data IS NOT NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        continue

    fixed_count = 0
    for row_id, raw in rows:
        new_data, changed = fix_dimension_data(raw)
        if changed:
            conn.execute(
                f"UPDATE {table} SET dimension_data = ? WHERE id = ?",
                (new_data, row_id),
            )
            fixed_count += 1

    conn.commit()
    print(f"{table}: {len(rows)} 行，修复 {fixed_count} 行")

conn.close()
print("完成")
