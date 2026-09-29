"""只读诊断：复现 alphafox_live.sync 的判定路径（不下单、不改数据）"""
import sys
sys.path.insert(0, "/root/binance-monitor/bsm-agent")

import config
import storage
import exchange
import alphafox_live
import alphafox_trade

print("LIVE_DB_PATH =", getattr(config, "LIVE_DB_PATH", "MISSING"))

with storage.get_conn() as conn:
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM trade_positions WHERE strategy='alphafox' "
        "AND status IN ('OPEN','PARTIAL')")]
print("paper_rows =", len(rows))
for r in rows:
    print("   ", r["token"], r["side"], r["status"],
          "qty=", r["quantity"], "updated_at=", r["updated_at"],
          "fresh=", alphafox_live._fresh(r))

try:
    ex = alphafox_live._ex_positions()
    print("ex_positions =", "None(API失败)" if ex is None else len(ex))
    if ex:
        for p in ex:
            print("    EX:", p["symbol"], p.get("position_side"), p["quantity"],
                  "mark=", p.get("mark_price"))
except Exception as e:
    print("ex_positions EXC:", type(e).__name__, e)

print("exempt_keys =", alphafox_live._exempt_keys())
for r in rows:
    tok = r["token"]
    try:
        print(f"supertrend_occupied({tok}) =", alphafox_live._supertrend_occupied(tok))
    except Exception as e:
        print(f"supertrend_occupied({tok}) EXC:", e)

storage.init_onchain_live_db()
with storage.get_conn(config.LIVE_DB_PATH) as lc:
    live_all = [dict(r) for r in lc.execute(
        "SELECT * FROM live_positions WHERE strategy='alphafox'")]
print("live_all(alphafox) =", len(live_all))
for lv in live_all:
    print("    LIVE:", lv["id"], lv["token"], lv["side"], lv["status"], lv["quantity"])

with storage.get_conn() as conn:
    ts = storage.trading_settings_get(conn)
print("live_enabled =", repr(ts.get("alphafox_live_enabled")),
      bool(ts.get("alphafox_live_enabled")))
print("live_notional =", repr(ts.get("alphafox_live_notional")))
print("live_leverage =", repr(ts.get("alphafox_live_leverage")))
