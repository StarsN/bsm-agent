"""服务器只读审计：扫描所有 DB 中带 strategy 字段的表，列出取值并检查脏值。

用法（服务器上）: python /tmp/_audit_strategy_values.py
"""
import sqlite3

DBS = [
    "/root/binance-monitor/bsm-agent/db/binance_square.db",
    "/root/binance-monitor/bsm-agent/db/onchain_live.db",
    "/root/binance-monitor/bsm-agent/db/alphafox.db",
    "/root/binance-monitor/bsm-agent/db/supertrend.db",
]

for path in DBS:
    print("=" * 20, path)
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except Exception as e:
        print("  打不开:", e)
        continue
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    for t in tables:
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        except Exception:
            continue
        if "strategy" not in cols:
            continue
        try:
            rows = list(conn.execute(
                f"SELECT strategy, COUNT(*) FROM {t} GROUP BY strategy"))
        except Exception as e:
            print(f"  {t}: 查询失败 {e}")
            continue
        vals = ", ".join(f"{r[0]}({r[1]})" for r in rows) or "(空表)"
        flag = ""
        for r in rows:
            if r[0] and "alphofox" in str(r[0]).lower():
                flag = "  <<< 脏值(alphofox)!"
        print(f"  {t}: {vals}{flag}")
    conn.close()
print("\n审计完成：除本行外，出现 '<<< 脏值' 即为需要修的数据")
