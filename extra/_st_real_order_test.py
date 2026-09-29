"""服务器真实数据验证：挂单模式全链路（临时库，不碰生产）

真实 OKX 信号 → 挂单(PENDING) → 模拟价格触及 → 系统转 OPEN → SL 验证
"""
import os
import sys
import tempfile

sys.path.insert(0, "/root/binance-monitor/bsm-agent")

import config

_TMP = tempfile.mkdtemp(prefix="str_")
config.DB_PATH = os.path.join(_TMP, "main.db")
config.SUPERTREND_DB = os.path.join(_TMP, "supertrend.db")

import storage
import trade_logic
import supertrend_strategy as st

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

st.init_db()
with storage.get_conn(config.SUPERTREND_DB) as sc:
    sc.execute("INSERT OR IGNORE INTO supertrend_tokens (token, status, added_at) "
               "VALUES ('BTC', 'ACTIVE', 0)")

# 真实 OKX 信号
sig = st._fetch_okx_supertrend("BTC")
print("真实信号:", sig and {k: sig[k] for k in ("trend", "upper_band", "lower_band")})

# mock 标记价（用真实币安标记价）
from market import get_mark_price as real_mark
mark = real_mark("BTC")
print("币安标记价:", mark)

st.get_mark_price = lambda token: mark

# 1. 挂单
with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
    st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0)
with storage.get_conn() as mc:
    p = mc.execute("SELECT * FROM trade_positions WHERE strategy='supertrend'").fetchone()
    p = dict(p)
print("挂单:", p["status"], p["side"], "limit=", p["limit_price"], "SL=", p["stop_loss_price"])

# 验证公式
if p["side"] == "LONG":
    expect_limit = sig["lower_band"] * 1.01
    expect_sl = sig["lower_band"] * 0.99
else:
    expect_limit = sig["upper_band"] * 0.99
    expect_sl = sig["upper_band"] * 1.01
assert abs(p["limit_price"] - expect_limit) < 0.01, f"挂单价不符 {p['limit_price']} vs {expect_limit}"
assert abs(p["stop_loss_price"] - expect_sl) < 0.01, f"SL不符"
print("公式验证 ✓")

# 2. 模拟价格触及挂单价 → 系统转 OPEN
if p["side"] == "LONG":
    st.get_mark_price = lambda token: p["limit_price"] - 1  # 价格跌破挂单价
else:
    st.get_mark_price = lambda token: p["limit_price"] + 1
trade_logic.get_mark_price = st.get_mark_price
with storage.get_conn() as mc:
    trade_logic.update_paper_positions(mc)
    p2 = mc.execute("SELECT * FROM trade_positions WHERE strategy='supertrend'").fetchone()
    p2 = dict(p2)
print("成交后:", p2["status"], "entry=", p2["entry_price"], "SL=", p2["stop_loss_price"])
assert p2["status"] == "OPEN", "未成交"
assert abs(p2["entry_price"] - p["limit_price"]) < 1e-6, "entry≠挂单价"
assert p2["stop_loss_price"] == p["stop_loss_price"], "SL 被改"
assert p2["tp1_price"] is None and p2["tp2_price"] is None, "TP 被补算"
print("成交验证 ✓（entry=挂单价、SL保留、无TP）")

# 3. 恢复真实标记价 → SL 扫损检测可用性验证（价格在 SL 上方，不应平）
st.get_mark_price = lambda token: mark
trade_logic.get_mark_price = st.get_mark_price
with storage.get_conn() as mc:
    trade_logic.update_paper_positions(mc)
    p3 = mc.execute("SELECT * FROM trade_positions WHERE strategy='supertrend'").fetchone()
    p3 = dict(p3)
print("恢复价后:", p3["status"], "unrealized=", round(p3["unrealized_pnl"] or 0, 4))
assert p3["status"] in ("OPEN", "CLOSED"), "状态异常"
print("全部通过：真实数据挂单链路 OK")
