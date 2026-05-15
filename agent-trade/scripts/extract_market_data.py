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

# ---- 候选币（最近 interval+2 分钟内面板收集的全量数据，2min 缓冲防漏）----
inter_min = getattr(config, "AGENT_COLLECT_INTERVAL_MINUTES", 15)
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
        # 去掉系统内部字段，Agent 只看原始市场数据
        for k in ("realtime", "tier", "passed", "hard_block", "pass_count", "suggestion", "reasons", "verdict", "direction"):
            d.pop(k, None)
        candidates.append(d)
    except (json.JSONDecodeError, TypeError):
        pass

latest_round = conn.execute(
    "SELECT COALESCE(MAX(round_number), 0) FROM token_heat_history"
).fetchone()[0] or 0

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
       AND rule_update IS NOT NULL AND rule_update != ''
       ORDER BY severity DESC, created_at DESC"""
)]

# ---- 今日日志 ----
today_journal = [dict(r) for r in conn.execute(
    "SELECT token, action, action AS action_type, price, tier, reason, pnl_pct, close_reason, "
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
    "candidate_count": len(candidates),
    "total_candidates": len(ac_rows),
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
print(f"  候选: {len(candidates)} 有快照 / {len(ac_rows)} 去重后")
print(f"  持仓: {len(positions)} 个")
print(f"  Worker: round={current_round} stage={stage}")
