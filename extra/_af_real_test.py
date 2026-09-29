"""服务器真实数据验证：真实 snapshot → alphafox_trade.process（临时 DB，不碰生产）

验证真实字段兼容性：positions 的 absQty/avgPrice/markPrice/notional/leverage/side
"""
import os
import sys
import tempfile

sys.path.insert(0, "/root/binance-monitor/bsm-agent")

import config

_TMP = tempfile.mkdtemp(prefix="afr_")
config.DB_PATH = os.path.join(_TMP, "main.db")
config.ALPHAFOX_DB = os.path.join(_TMP, "alphafox.db")

import storage
import alphafox_trade as aft

with storage.get_conn() as conn:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(storage.SCHEMA)
    conn.executescript("""
    DROP TABLE IF EXISTS trade_signal_locks;
    CREATE TABLE trade_signal_locks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT NOT NULL, signal_key TEXT NOT NULL, strategy TEXT DEFAULT 'agent',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(token, signal_key, strategy)
    );
    """)

import requests

AF_BASE = "https://www.alphafox.app"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}


def fetch_snapshot(sid):
    r = requests.get(f"{AF_BASE}/api/radar/snapshot?sourceId={sid}",
                     headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


PARAMS = {"budget": 1000.0, "slip": 0.05, "fee": 0.04}
SID = "bn-5075281354358777856"  # 熬鹰资本

aft.init_db()
aft._prev_snap.clear()

print("== 1. 首轮基线（真实数据）==")
snap1 = fetch_snapshot(SID)
s1 = aft.process(SID, snap1, PARAMS)
data1 = (snap1.get("data") or {})
print(f"  交易员: {data1.get('traderName')} margin={data1.get('marginBalance')}")
print(f"  持仓数: {len(data1.get('positions') or [])}")
print(f"  首轮信号: {s1}")
assert s1 == [], "首轮应无信号"

print("== 2. 真实字段兼容性验证（模拟交易员开新仓）==")
positions = data1.get("positions") or []
if positions:
    # 取第一个持仓验证开仓逻辑（模拟从空仓→有仓）
    p0 = positions[0]
    fake_pos = [dict(p0)]
    # 先用一个"交易员平掉所有其他仓"的假快照做基线，再开这仓
    aft._prev_snap[SID] = {}  # 重置为基线=空
    snap_fake = {"data": {"traderName": data1.get("traderName"),
                          "marginBalance": data1.get("marginBalance"),
                          "positions": fake_pos}}
    s2 = aft.process(SID, snap_fake, PARAMS)
    print(f"  开仓信号: {[x['action'] for x in s2]}")
    with storage.get_conn() as conn:
        rows = conn.execute(
            "SELECT token, side, quantity, entry_price, leverage, notional "
            "FROM trade_positions WHERE strategy='alphafox'").fetchall()
    for r in rows:
        print(f"  我们仓位: {dict(r)}")
    assert len(rows) == 1, "应开一仓"

print("== 3. 平仓（真实数据→空仓）==")
snap_empty = {"data": {"traderName": data1.get("traderName"),
                       "marginBalance": data1.get("marginBalance"),
                       "positions": []}}
s3 = aft.process(SID, snap_empty, PARAMS)
print(f"  平仓信号: {[x['action'] for x in s3]}")
with storage.get_conn() as conn:
    open_rows = conn.execute(
        "SELECT COUNT(*) FROM trade_positions WHERE strategy='alphafox' "
        "AND status IN ('OPEN','PARTIAL')").fetchone()[0]
print(f"  剩余 OPEN 仓: {open_rows}")
assert open_rows == 0

print("\n全部通过：真实数据兼容性 OK")
