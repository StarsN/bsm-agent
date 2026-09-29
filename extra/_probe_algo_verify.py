"""确认条件单链路是否真的可用：live 记录里存的条件单 id + 用正确端点查历史。"""
import glob
import json
import os
import sqlite3
import sys
from urllib.request import Request, urlopen
from urllib.error import HTTPError

sys.path.insert(0, "/root/binance-monitor/bsm-agent")
import exchange  # noqa: E402

DB = "/root/binance-monitor/bsm-agent/db/onchain_live.db"
if not os.path.exists(DB):
    DB = "/root/binance-monitor/bsm-agent/db/onchain.db"
c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
c.row_factory = sqlite3.Row
rows = [dict(r) for r in c.execute(
    "SELECT token, strategy, status, quantity, exchange_orders_json, created_at "
    "FROM live_positions ORDER BY id DESC LIMIT 15")]
print("=== live_positions（各策略，看存的条件单 id）===")
for r in rows:
    o = {}
    try:
        o = json.loads(r.get("exchange_orders_json") or "{}")
    except Exception:
        pass
    print(f"  {r['token']:10} {r['strategy']:11} {r['status']:7} "
          f"qty={r['quantity']} sl={o.get('sl')} tp1={o.get('tp1')} "
          f"limit={o.get('limit')}  {str(r['created_at'])[:19]}")

syms = sorted({r["token"] + "USDT" for r in rows
               if json.loads(r.get("exchange_orders_json") or "{}").get("sl")})
print(f"\n=== 对存过条件单 id 的 symbol 查 /fapi/v1/allAlgoOrders ===\n{syms}")
for sym in syms[:4]:
    url = exchange.FAPI_REST + "/fapi/v1/allAlgoOrders?" + exchange._sign(
        {"symbol": sym, "limit": 5})
    req = Request(url, method="GET")
    req.add_header("X-MBX-APIKEY", exchange.config.BINANCE_API_KEY)
    try:
        with urlopen(req, timeout=10) as r:
            print(f"  {sym}: 200 {r.read()[:300]}")
    except HTTPError as e:
        print(f"  {sym}: HTTP {e.code} {e.read()[:200]}")
    except Exception as e:  # noqa: BLE001
        print(f"  {sym}: ERR {e}")

print("\n=== 日志里 algoOrder / 404 的痕迹 ===")
for f in sorted(glob.glob("/root/binance-monitor/bsm-agent/logs/*/*.log"))[-6:]:
    try:
        for ln in open(f, encoding="utf-8", errors="replace"):
            if "algoOrder" in ln or "HTTP 404" in ln or "[exchange]" in ln:
                print(f"  {os.path.basename(f)}: {ln.strip()[:150]}")
    except OSError:
        pass
