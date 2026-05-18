#!/usr/bin/env python3
"""提取市场数据，写 JSON 到指定文件。Agent 分析时使用。"""
import argparse
import sqlite3
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

parser = argparse.ArgumentParser(description="Extract market data for Agent")
parser.add_argument("--output", required=True, help="Output JSON file path")
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

data_source = "agent_candidates"
try:
    ts = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM trading_settings").fetchall()}
    data_source = ts.get("agent_data_source", getattr(config, "AGENT_DATA_SOURCE", "agent_candidates"))
except Exception:
    data_source = getattr(config, "AGENT_DATA_SOURCE", "agent_candidates")

# 旧路径需要的轮次（新路径也用于 latest_round 输出字段）
latest_round = conn.execute(
    "SELECT COALESCE(MAX(round_number), 0) FROM token_heat_history"
).fetchone()[0] or 0

if data_source == "token_heat_history":
    # ---- 旧逻辑：token_heat_history + market_snapshots 联表，最近一轮 TOP30 ----

    rows = [dict(r) for r in conn.execute(
        "SELECT token, score, mentions, unique_posts FROM token_heat_history "
        "WHERE round_number = ? ORDER BY score DESC",
        (latest_round,)
    )]
    best_by_token = {}
    for r in rows:
        t = r["token"]
        if t not in best_by_token or r["score"] > best_by_token[t]["score"]:
            best_by_token[t] = r
    candidates_raw = sorted(
        best_by_token.values(), key=lambda x: x["score"], reverse=True
    )[:30]

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
                "tags": a.get("tags", []),
                "notes": a.get("notes", []),
                "age": (
                    None if not s.get("klines_15m_count") else
                    ">1d" if s["klines_15m_count"] >= 96 else
                    f"{s['klines_15m_count'] * 15 // 60 // 24}d"
                    f"{s['klines_15m_count'] * 15 // 60 % 24}h"
                ),
            })
else:
    # ---- 新逻辑：agent_candidates 时间窗口 ----
    inter_min = 15
    try:
        ts = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM trading_settings").fetchall()}
        inter_min = int(ts.get("agent_collect_interval_minutes", getattr(config, "AGENT_COLLECT_INTERVAL_MINUTES", 15)))
    except Exception:
        pass
    ac_rows = conn.execute(
        "SELECT a.data FROM agent_candidates a "
        "INNER JOIN ("
        "  SELECT token, MAX(id) AS max_id FROM agent_candidates "
        f"  WHERE created_at >= datetime('now', '-{inter_min + 2} minutes') "
        "  GROUP BY token"
        ") b ON a.id = b.max_id "
        "ORDER BY a.id"
    ).fetchall()

    candidates = []
    for r in ac_rows:
        try:
            d = json.loads(r["data"])
            for k in ("realtime", "tier", "passed", "hard_block", "pass_count", "suggestion", "reasons", "verdict", "direction"):
                d.pop(k, None)
            candidates.append(d)
        except (json.JSONDecodeError, TypeError):
            pass

# ---- 持仓（Agent 开的单）----
positions = [dict(p) for p in conn.execute(
    """SELECT token, side, entry_price, current_price, stop_loss_price,
       tp1_price, tp2_price, pnl_pct, margin_amount, highest_price, status
       FROM trade_positions
       WHERE status IN ('OPEN','PARTIAL')
       AND strategy = 'agent'"""
)]

# ---- 账户（仅 Agent 策略）----
settings = {r["key"]: r["value"] for r in conn.execute(
    "SELECT * FROM trading_settings"
)}
initial = float(settings.get("strategy_initial_agent", 1000))
realized = conn.execute(
    "SELECT COALESCE(SUM(realized_pnl),0) FROM trade_positions WHERE strategy='agent'"
).fetchone()[0]
unrealized = conn.execute(
    "SELECT COALESCE(SUM(unrealized_pnl),0) FROM trade_positions "
    "WHERE status IN ('OPEN','PARTIAL') AND strategy='agent'"
).fetchone()[0]
locked = conn.execute(
    "SELECT COALESCE(SUM(margin_amount),0) FROM trade_positions "
    "WHERE status IN ('OPEN','PARTIAL') AND strategy='agent'"
).fetchone()[0]
today_count = conn.execute(
    "SELECT COUNT(*) FROM trade_positions "
    "WHERE strategy='agent' AND date(created_at, '+8 hours') = date('now', '+8 hours')"
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
    """SELECT la.token, la.pnl_pct, la.failed_reason, la.reason_tags
       FROM trade_loss_archive la
       JOIN trade_positions tp ON la.position_id = tp.id
       WHERE tp.strategy = 'agent'
       ORDER BY la.created_at DESC LIMIT 10"""
):
    archive_lessons.append({
        "token": r["token"], "pnl": r["pnl_pct"],
        "reason": r["failed_reason"],
        "tags": json.loads(r["reason_tags"]) if r["reason_tags"] else [],
    })

tag_stats = {}
for r in conn.execute(
    """SELECT la.reason_tags FROM trade_loss_archive la
       JOIN trade_positions tp ON la.position_id = tp.id
       WHERE la.reason_tags IS NOT NULL AND tp.strategy = 'agent'"""
):
    for t in json.loads(r["reason_tags"]):
        tag_stats[t] = tag_stats.get(t, 0) + 1

agent_lessons = [dict(r) for r in conn.execute(
    """SELECT id, token, direction, entry_price, exit_price, pnl_pct,
       signal_error, what_missed, root_cause, lesson, rule_update, severity
       FROM lessons WHERE learned=0
       AND rule_update IS NOT NULL AND rule_update != ''
       ORDER BY severity DESC, created_at DESC"""
)]

# ---- 今日日志 ----
today_journal = [dict(r) for r in conn.execute(
    "SELECT j.token, j.action, j.action AS action_type, j.price, j.tier, "
    "j.reason, j.pnl_pct, j.close_reason, j.hold_duration, j.created_at "
    "FROM journal j "
    "LEFT JOIN trade_positions tp ON j.order_id = tp.id "
    "WHERE date(j.created_at, '+8 hours') = date('now', '+8 hours') "
    "AND (j.action = 'open' OR tp.strategy = 'agent') "
    "ORDER BY j.id"
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
    "candidate_count": len(candidates),
    "total_candidates": len(candidates_raw) if data_source == "token_heat_history" else len(ac_rows),
    "current_round": current_round,
    "latest_round": latest_round,
    "worker_stage": stage,
    "btc": btc,
    "fear_greed": fng,
    "session": session,
}

with open(args.output, "w", encoding="utf-8") as f:
    json.dump(output, f, default=str, ensure_ascii=False)

print(f"数据已写入 {args.output}")
print(f"  候选: {len(candidates)} 有快照"
      f" / {len(candidates_raw) if data_source == 'token_heat_history' else len(ac_rows)} 总行数")
print(f"  持仓: {len(positions)} 个")
print(f"  Worker: round={current_round} stage={stage}")
