#!/usr/bin/env python3
"""提取市场数据，写 JSON 到指定文件。Agent 分析时使用。"""
import argparse
import sqlite3
import json
import os
import sys
from datetime import datetime, timezone
from urllib.request import Request, urlopen

parser = argparse.ArgumentParser(description="Extract market data for Agent")
parser.add_argument("--output", required=True, help="Output JSON file path")
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

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

# ---- worker 状态 ----
ws = dict(conn.execute(
    "SELECT * FROM worker_status ORDER BY rowid DESC LIMIT 1"
).fetchone() or {})

current_round = ws.get("round_number", 0)
stage = ws.get("stage", "unknown")

# ---- 交易时段 ----
now_utc = datetime.now(timezone.utc)
hour = now_utc.hour
weekday = now_utc.weekday()  # 0=Mon, 6=Sun
if weekday >= 5:
    session = "周末低流动性"
elif 0 <= hour < 7:
    session = "亚洲早盘（低流动性）"
elif 7 <= hour < 13:
    session = "欧亚重叠（中等流动性）"
elif 13 <= hour < 16:
    session = "欧美重叠（高流动性）"
elif 16 <= hour < 22:
    session = "美国时段（高流动性）"
else:
    session = "亚洲凌晨（低流动性）"

# ---- BTC 走势（直接调合约 API）----
def _get_json(url, timeout=8):
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

btc = {
    "price": None, "chg_15m": None, "chg_1h": None,
    "chg_4h": None, "chg_24h": None, "funding": None,
    "oi_1h": None, "oi_4h": None,
}

# 标记价 + 资金费率
prem = _get_json("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT")
if prem:
    btc["price"] = float(prem.get("markPrice", 0))
    btc["funding"] = float(prem.get("lastFundingRate", 0)) * 100

# K 线：15m × 100 根 = 25h 覆盖
kl = _get_json("https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=15m&limit=100")
if kl and len(kl) >= 97:
    closes = [float(k[4]) for k in kl]
    now = closes[-1]
    if closes[-2] > 0:
        btc["chg_15m"] = (now - closes[-2]) / closes[-2] * 100
    if len(closes) >= 5 and closes[-5] > 0:
        btc["chg_1h"] = (now - closes[-5]) / closes[-5] * 100
    if len(closes) >= 17 and closes[-17] > 0:
        btc["chg_4h"] = (now - closes[-17]) / closes[-17] * 100
    if closes[0] > 0:
        btc["chg_24h"] = (now - closes[0]) / closes[0] * 100

# OI 变化
oi = _get_json("https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT")
oi_hist = _get_json(
    "https://fapi.binance.com/futures/data/openInterestHist?"
    "symbol=BTCUSDT&period=5m&limit=13"
)
if oi and oi_hist and len(oi_hist) >= 2:
    try:
        oi_latest = float(oi_hist[-1].get("sumOpenInterestValue", 0))
        if len(oi_hist) >= 13 and float(oi_hist[0].get("sumOpenInterestValue", 0)) > 0:
            btc["oi_1h"] = (oi_latest - float(oi_hist[0]["sumOpenInterestValue"])) / float(oi_hist[0]["sumOpenInterestValue"]) * 100
        if len(oi_hist) >= 4:
            oi_15m = float(oi_hist[-4].get("sumOpenInterestValue", 0))
            if oi_15m > 0:
                btc["oi_15m"] = (oi_latest - oi_15m) / oi_15m * 100
    except (TypeError, ValueError, KeyError):
        pass

# OI 4h
oi_4h_data = _get_json(
    "https://fapi.binance.com/futures/data/openInterestHist?"
    "symbol=BTCUSDT&period=15m&limit=17"
)
if oi_4h_data and len(oi_4h_data) >= 2:
    try:
        a = float(oi_4h_data[-1].get("sumOpenInterestValue", 0))
        b = float(oi_4h_data[0].get("sumOpenInterestValue", 0))
        if b > 0:
            btc["oi_4h"] = (a - b) / b * 100
    except (TypeError, ValueError, KeyError):
        pass

# ---- 恐惧贪婪指数 ----
fng = None
fng_data = _get_json("https://api.alternative.me/fng/?limit=2")
if fng_data and fng_data.get("data"):
    try:
        latest = fng_data["data"][0]
        prev = fng_data["data"][1] if len(fng_data["data"]) > 1 else None
        fng = {
            "value": int(latest.get("value", 0)),
            "classification": latest.get("value_classification", ""),
            "prev_value": int(prev.get("value", 0)) if prev else None,
        }
    except (TypeError, ValueError, KeyError, IndexError):
        pass

# ---- 候选币（source_round 窗口过滤，最近 3 轮）----
latest_round = conn.execute(
    "SELECT MAX(source_round) FROM round_candidates"
).fetchone()[0] or 0

candidate_rows = [dict(r) for r in conn.execute(
    "SELECT id, token, score, mentions, source_round FROM round_candidates "
    "WHERE delivered=0 AND source_round >= ? ORDER BY score DESC",
    (max(latest_round - 3, 1),)
)]
candidate_ids = [r["id"] for r in candidate_rows]

# 按 token 去重取最高分
best_by_token = {}
for r in candidate_rows:
    t = r["token"]
    if t not in best_by_token or r["score"] > best_by_token[t]["score"]:
        best_by_token[t] = r
candidates_raw = sorted(
    best_by_token.values(), key=lambda x: x["score"], reverse=True
)[:30]

# ---- 关联市场快照 ----
candidates = []
for h in candidates_raw:
    snap = conn.execute(
        "SELECT snapshot, analysis FROM market_snapshots WHERE token=?",
        (h["token"],)
    ).fetchone()
    if snap:
        s = json.loads(snap["snapshot"])
        a = json.loads(snap["analysis"])
        candidates.append({
            "token": h["token"],
            "social_score": h["score"],
            "mentions": h["mentions"],
            "price": s.get("mark_price"),
            "15m": s.get("change_15m_pct"),
            "1h": s.get("change_1h_pct"),
            "4h": s.get("change_4h_pct"),
            "24h": s.get("change_24h_pct"),
            "oi_15m": s.get("oi_change_15m_pct"),
            "oi_1h": s.get("oi_change_1h_pct"),
            "oi_4h": s.get("oi_change_4h_pct"),
            "oi_48h": s.get("oi_change_48h_pct"),
            "funding": s.get("funding_rate_pct"),
            "lsr": s.get("long_short_ratio"),
            "top_lsr": s.get("top_trader_ls_ratio"),
            "taker": s.get("taker_buy_sell_ratio"),
            "taker_pct": s.get("taker_buy_pct"),
            "taker_trend": s.get("taker_trend_pct"),
            "spread": s.get("bid_ask_spread_pct"),
            "depth_bid": s.get("depth_bid_1pct_usd"),
            "depth_ask": s.get("depth_ask_1pct_usd"),
            "imbalance": s.get("depth_imbalance_pct"),
            "vol_24h": s.get("volume_24h_usd"),
            "oi_usd": s.get("oi_usd"),
            "chg_48h": s.get("change_48h_pct"),
            "oi_divergence": a.get("oi_divergence"),
            "verdict": a.get("verdict"),
            "direction": a.get("direction"),
            "tags": a.get("tags", []),
            "notes": a.get("notes", []),
            "snapshot_cn": {
                "标记价": s.get("mark_price"),
                "15分钟涨跌": s.get("change_15m_pct"),
                "1小时涨跌": s.get("change_1h_pct"),
                "4小时涨跌": s.get("change_4h_pct"),
                "24小时涨跌": s.get("change_24h_pct"),
                "48小时涨跌": s.get("change_48h_pct"),
                "资金费率(%/8h)": s.get("funding_rate_pct"),
                "未平仓(USD)": s.get("oi_usd"),
                "OI15分钟变化": s.get("oi_change_15m_pct"),
                "OI1小时变化": s.get("oi_change_1h_pct"),
                "OI4小时变化": s.get("oi_change_4h_pct"),
                "OI48小时变化": s.get("oi_change_48h_pct"),
                "主动买卖比": s.get("taker_buy_sell_ratio"),
                "主动买入占比": s.get("taker_buy_pct"),
                "Taker趋势": s.get("taker_trend_pct"),
                "盘口价差": s.get("bid_ask_spread_pct"),
                "买盘深度(USD)": s.get("depth_bid_1pct_usd"),
                "卖盘深度(USD)": s.get("depth_ask_1pct_usd"),
                "盘口失衡度": s.get("depth_imbalance_pct"),
                "24小时成交额": s.get("volume_24h_usd"),
                "散户多空比": s.get("long_short_ratio"),
                "大户多空比": s.get("top_trader_ls_ratio"),
                "信号判定": a.get("verdict"),
                "走向": a.get("direction"),
                "信号标签": a.get("tags", []),
                "分析备注": a.get("notes", []),
                "OI背离": a.get("oi_divergence"),
                "社交热度分": h["score"],
                "提及次数": h["mentions"],
            },
        })

# ---- 持仓（Agent 开的单）----
positions = [dict(p) for p in conn.execute(
    """SELECT token, side, entry_price, current_price, stop_loss_price,
       tp1_price, tp2_price, pnl_pct, margin_amount, highest_price, status
       FROM trade_positions
       WHERE status IN ('OPEN','PARTIAL')
       AND json_extract(signal_snapshot, '$.source') = 'agent'"""
)]

# ---- 账户 ----
settings = {r["key"]: r["value"] for r in conn.execute(
    "SELECT * FROM trading_settings"
)}
initial = float(settings.get("initial_balance", 1000))
realized = conn.execute(
    "SELECT COALESCE(SUM(realized_pnl),0) FROM trade_positions"
).fetchone()[0]
unrealized = conn.execute(
    "SELECT COALESCE(SUM(unrealized_pnl),0) FROM trade_positions "
    "WHERE status IN ('OPEN','PARTIAL')"
).fetchone()[0]
locked = conn.execute(
    "SELECT COALESCE(SUM(margin_amount),0) FROM trade_positions "
    "WHERE status IN ('OPEN','PARTIAL')"
).fetchone()[0]
today_count = conn.execute(
    "SELECT COUNT(*) FROM trade_positions "
    "WHERE date(created_at, '+8 hours') = date('now', '+8 hours')"
).fetchone()[0]

account = {
    "equity": round(initial + realized + unrealized, 2),
    "available": round(initial + realized - locked, 2),
    "initial": initial,
    "realized": round(realized, 2),
    "unrealized": round(unrealized, 2),
    "locked": round(locked, 2),
    "trades_today": today_count,
    "open_count": len(positions),
}

# ---- 教训 ----
archive_lessons = []
for r in conn.execute(
    """SELECT token, pnl_pct, failed_reason, reason_tags
       FROM trade_loss_archive ORDER BY created_at DESC LIMIT 10"""
):
    archive_lessons.append({
        "token": r["token"], "pnl": r["pnl_pct"],
        "reason": r["failed_reason"],
        "tags": json.loads(r["reason_tags"]) if r["reason_tags"] else [],
    })

tag_stats = {}
for r in conn.execute(
    "SELECT reason_tags FROM trade_loss_archive WHERE reason_tags IS NOT NULL"
):
    for t in json.loads(r["reason_tags"]):
        tag_stats[t] = tag_stats.get(t, 0) + 1

agent_lessons = [dict(r) for r in conn.execute(
    """SELECT id, token, direction, entry_price, exit_price, pnl_pct,
       signal_error, what_missed, root_cause, lesson, rule_update, severity
       FROM lessons WHERE learned=0
       ORDER BY severity DESC, created_at DESC"""
)]

# ---- 今日日志 ----
today_journal = [dict(r) for r in conn.execute(
    "SELECT token, action, price, tier, reason, pnl_pct, close_reason, "
    "hold_duration, created_at "
    "FROM journal WHERE date(created_at, '+8 hours') = date('now', '+8 hours') "
    "ORDER BY id"
)]

conn.close()

output = {
    "candidates": candidates,
    "positions": positions,
    "account": account,
    "archive_lessons": archive_lessons,
    "tag_stats": tag_stats,
    "agent_lessons": agent_lessons,
    "today_journal": today_journal,
    "candidate_ids": candidate_ids,
    "current_round": current_round,
    "worker_stage": stage,
    "btc": btc,
    "fear_greed": fng,
    "session": session,
}

with open(args.output, "w", encoding="utf-8") as f:
    json.dump(output, f, default=str, ensure_ascii=False)

print(f"数据已写入 {args.output}")
print(f"  候选: {len(candidates)} 有快照 / {len(candidate_ids)} 总ID")
print(f"  持仓: {len(positions)} 个")
print(f"  Worker: round={current_round} stage={stage}")
