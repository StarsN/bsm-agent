"""跟单链路生产数据体检（只读）。

在服务器上运行，交叉核对三处状态是否一致：
  纸盘仓(trade_positions) × 实盘记录(live_positions) × 信号日志(alphafox_signals)
不修改任何东西。
"""
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

BASE = "/root/binance-monitor/bsm-agent"
CST = timezone(timedelta(hours=8))


def conn(path):
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def cst(ts):
    if not ts:
        return ""
    return datetime.fromtimestamp(float(ts), CST).strftime("%m-%d %H:%M:%S")


main = conn(f"{BASE}/db/binance_square.db")
live = conn(f"{BASE}/db/onchain_live.db")
af = conn(f"{BASE}/db/alphafox.db")

paper = [dict(r) for r in main.execute(
    "SELECT * FROM trade_positions WHERE strategy='alphafox' "
    "AND status IN ('OPEN','PARTIAL','PENDING') ORDER BY token")]
all_live = [dict(r) for r in live.execute(
    "SELECT * FROM live_positions WHERE strategy='alphafox' ORDER BY token, id")]
live_open = [r for r in all_live if r["status"] in ("OPEN", "PARTIAL")]
exempt = []
row = main.execute("SELECT value FROM trading_settings "
                   "WHERE key='alphafox_live_exempt_paper_ids'").fetchone()
if row and row["value"]:
    exempt = json.loads(row["value"])
notional = main.execute("SELECT value FROM trading_settings "
                        "WHERE key='alphafox_live_notional'").fetchone()
notional = float(notional["value"]) if notional else 90.0

print("=" * 84)
print(f"体检时间 {datetime.now(CST).strftime('%Y-%m-%d %H:%M:%S')} CST")
print(f"纸盘活跃行 {len(paper)} | 实盘记录(全部) {len(all_live)} | 实盘活跃 {len(live_open)}")
print(f"豁免清单 {exempt} | 每份名义 {notional}u")
print("=" * 84)

# ---- 1. 纸盘活跃行 ↔ 实盘记录 ----
print("\n【1】纸盘活跃行 vs 实盘记录（按 token+side 配对）")
for p in paper:
    ex = exempt_key = f"{p['token']}|{p['side']}#{p['id']}"
    cands = [r for r in live_open
             if r["token"] == p["token"] and (r.get("side") or "LONG") == p["side"]]
    if not cands:
        flag = "豁免仓(预期)" if exempt_key in exempt else "★ 无实盘记录"
        print(f"  {p['token']:10} {p['side']:5} 纸盘#{p['id']:<5} "
              f"qty={p['quantity']:.6g} closed={p['closed_qty']:.6g}  {flag}")
        continue
    lr = cands[0]
    lots = (json.loads(lr.get("exchange_orders_json") or "{}") or {}).get("lots")
    mark = float(p.get("current_price") or p.get("entry_price") or 0)
    tgt = (lots or 1) * notional / mark if mark else 0
    drift = (lr["quantity"] - tgt) / tgt * 100 if tgt else 0
    stale = lr.get("paper_position_id") != p["id"]
    note = []
    if stale:
        note.append(f"★ 记录挂旧纸盘id#{lr.get('paper_position_id')}")
    if abs(drift) > 6:
        note.append(f"★ 数量偏离目标 {drift:+.1f}%")
    print(f"  {p['token']:10} {p['side']:5} 纸盘#{p['id']:<5} "
          f"qty={p['quantity']:.6g} closed={p['closed_qty']:.6g} | "
          f"实盘#{lr['id']} lots={lots} qty={lr['quantity']:.6g} "
          f"目标≈{tgt:.6g}  {' '.join(note)}")

# ---- 2. 实盘活跃记录没有对应纸盘仓（残留） ----
print("\n【2】实盘活跃记录但纸盘无对应活跃仓（残留/孤儿）")
paper_keys = {(p["token"], p["side"]) for p in paper}
orphan = [r for r in live_open if (r["token"], r.get("side") or "LONG") not in paper_keys]
if not orphan:
    print("  无 ✓")
for r in orphan:
    age = ""
    try:
        age = f"创建 {cst(r['created_at'])}"
    except Exception:
        pass
    print(f"  ★ {r['token']:10} {r.get('side'):5} 实盘#{r['id']} status={r['status']} "
          f"qty={r['quantity']:.6g} paper#{r.get('paper_position_id')} {age}")

# ---- 3. 纸盘幽灵仓 / 实盘异常数量 ----
print("\n【3】状态异常行")
bad = 0
for p in paper:
    q, cq = float(p["quantity"] or 0), float(p["closed_qty"] or 0)
    if q <= 0 or cq >= q - 1e-12:
        print(f"  ★ 纸盘#{p['id']} {p['token']} status={p['status']} qty={q} closed={cq}")
        bad += 1
for r in live_open:
    if float(r["quantity"] or 0) <= 0:
        print(f"  ★ 实盘#{r['id']} {r['token']} qty={r['quantity']}")
        bad += 1
if not bad:
    print("  无 ✓")

# ---- 4. 近 3 天信号健康度 ----
print("\n【4】近 3 天信号（executed=0 的非 SKIP 信号 = 该跟没跟）")
since = int(time.time()) - 3 * 86400
rows = [dict(r) for r in af.execute(
    "SELECT * FROM alphafox_signals WHERE created_at >= ? ORDER BY id", (since,))]
from collections import Counter
cnt = Counter((r["action"], int(r["executed"] or 0)) for r in rows)
print("  " + " | ".join(f"{a}/exec={e}:{n}" for (a, e), n in sorted(cnt.items())))
missed = [r for r in rows if not r["executed"] and r["action"] not in ("SKIP",)]
for r in missed[-15:]:
    print(f"  ★ {cst(r['created_at'])} {r['action']:6} {r['symbol']:12} "
          f"{r['side']:5} qty={r['trader_qty']} executed=0")
if not missed:
    print("  无未执行的跟单动作 ✓")

# ---- 5. 实盘成交流水（最近 12 条） ----
print("\n【5】实盘成交流水 live_trade_log（最近 12 条）")
for r in live.execute("SELECT * FROM live_trade_log WHERE strategy='alphafox' "
                      "ORDER BY id DESC LIMIT 12"):
    print(f"  {str(r['created_at'])[:19]}  {r['token']:10} {r['action']:16} "
          f"qty={r['quantity']}  paper#{r['paper_position_id']}")

# ---- 6. 交易员 ----
print("\n【6】跟单交易员")
for r in af.execute("SELECT * FROM alphafox_traders"):
    print(f"  {r['trader_id']}  {r['label']}  enabled={r['enabled']}  "
          f"updated={str(r['updated_at'])[:19]}")
