"""验证 tp1_done/tp2_done 加 >0 条件的影响：
场景1：正常仓位（有 TP 价）→ TP1 触发平 80%（改动前后行为必须一致）
场景2：无 TP 仓位 + closed_ratio 0.9 → 不得进入 TP/跟踪逻辑（当前代码有 bug，会误平仓）
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

_TMP = tempfile.mkdtemp(prefix="tp_guard_")
config.DB_PATH = os.path.join(_TMP, "main.db")

import storage
import trade_logic

with storage.get_conn() as conn:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(storage.SCHEMA)
    conn.executescript("""
    DROP TABLE IF EXISTS trade_signal_locks;
    CREATE TABLE trade_signal_locks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT NOT NULL,
        signal_key TEXT NOT NULL,
        strategy TEXT DEFAULT 'agent',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(token, signal_key, strategy)
    );
    """)

# mock 价格（场景内直接操纵 realtime 缓存或 get_mark_price）
trade_logic.get_mark_price = lambda token: PRICE[token] if token in PRICE else None
# mock 平仓分支的重行情调用（避免真实币安 API）
trade_logic.get_market_snapshot = lambda token, heavy=False: {}
trade_logic.analyze_signals = lambda snap, social_score=0: {}

PRICE = {}


def insert_position(token, strategy, side, entry, tp1, tp2, closed_qty, qty, highest):
    with storage.get_conn() as conn:
        conn.execute("""
            INSERT INTO trade_positions
            (token, symbol, side, status, mode, margin_amount, leverage, notional,
             quantity, entry_price, limit_price, current_price, stop_loss_price,
             tp1_price, tp2_price, highest_price, lowest_price, trailing_stop_price,
             signal_snapshot, open_reason, advice, strategy, closed_qty,
             realized_pnl, unrealized_pnl, pnl_pct)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (token, token + "USDT", side, "OPEN", "paper", 50, 2, 100,
              qty, entry, entry, entry, None,
              tp1, tp2, highest, None, None,
              None, "test", "test", strategy, closed_qty, 0, 0, 0))


def pos_status(token):
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT status, closed_qty, quantity, trailing_stop_price, stop_loss_price "
            "FROM trade_positions WHERE token=? ORDER BY id DESC LIMIT 1",
            (token,)).fetchone()
        return dict(row)


PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


print("== 场景1：正常仓位（有TP价）TP1 触发平 80% ==")
insert_position("TESTA", "manual", "LONG", entry=100.0, tp1=101.0, tp2=102.0,
                closed_qty=0, qty=1.0, highest=100.0)
PRICE["TESTA"] = 101.5  # 涨过 TP1(101)
with storage.get_conn() as conn:
    trade_logic.update_paper_positions(conn)
p = pos_status("TESTA")
check("TP1 触发 → PARTIAL", p["status"] == "PARTIAL", str(p))
check("平 80%（closed_qty=0.8）", abs((p["closed_qty"] or 0) - 0.8) < 1e-6,
      str(p["closed_qty"]))
check("止损移到保本（entry）", abs((p["stop_loss_price"] or 0) - 100.0) < 1e-6,
      str(p["stop_loss_price"]))

print("== 场景2：无TP仓位 + closed_ratio 0.9 + 价格回撤3%（不得误平）==")
insert_position("TESTB", "alphafox", "LONG", entry=100.0, tp1=None, tp2=None,
                closed_qty=0.9, qty=1.0, highest=120.0)
PRICE["TESTB"] = 116.0  # 回撤 3.3%（若进入 trailing：120×0.98=117.6 ≤ 116 → 旧逻辑会平仓）
with storage.get_conn() as conn:
    trade_logic.update_paper_positions(conn)
p = pos_status("TESTB")
check("无TP仓位不被误平（保持 OPEN）", p["status"] == "OPEN", str(p))
check("trailing_stop_price 不被设置", p["trailing_stop_price"] is None,
      str(p["trailing_stop_price"]))

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
