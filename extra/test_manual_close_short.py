"""手动市价平仓方向测试：做空盈利/亏损 + 多单对照（临时库，不连外网）。

运行：python extra/test_manual_close_short.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

_TMP = tempfile.mkdtemp(prefix="mcs_test_")
config.DB_PATH = os.path.join(_TMP, "main.db")

import storage
import trade_logic

with storage.get_conn() as conn:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(storage.SCHEMA)
    # SCHEMA 里 trade_signal_locks 是旧结构（无 strategy 列，生产库靠 _migrate 迁移），
    # 新测试库直接重建为带 strategy 的版本
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

REAL_POS_PRICE = trade_logic._position_price


def set_price(p):
    trade_logic._position_price = lambda token, market, realtime: p


def insert_pos(token, side, entry, qty, margin, notional, tp1=None, tp2=None):
    with storage.get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO trade_positions
                (token, symbol, side, status, mode, margin_amount, leverage,
                 notional, quantity, entry_price, limit_price, current_price,
                 stop_loss_price, tp1_price, tp2_price, highest_price,
                 lowest_price, trailing_stop_price, strategy)
            VALUES (?,?,?,'OPEN','paper',?,1,?,?,?,?,?,?,?,?,?,?,?,'agent')
        """, (token, f"{token}USDT", side, margin, notional, qty, entry,
              entry, entry, 0, tp1, tp2, entry, entry, None))
        return cur.lastrowid


PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def main():
    # 1. 做空盈利平仓：entry=100 → 平仓价 90 → PnL +10（修前为 -10）
    set_price(90.0)
    pid = insert_pos("TEST1", "SHORT", 100.0, 1.0, 10.0, 100.0)
    with storage.get_conn() as conn:
        trade_logic.manual_close_on_unwatch(conn, "TEST1")
    with storage.get_conn() as conn:
        row = dict(conn.execute(
            "SELECT * FROM trade_positions WHERE id=?", (pid,)).fetchone())
    check("做空盈利：realized=+10", abs(row["realized_pnl"] - 10.0) < 1e-9,
          str(row["realized_pnl"]))
    check("做空盈利：pnl_pct=+100%", abs(row["pnl_pct"] - 100.0) < 1e-9,
          str(row["pnl_pct"]))
    check("做空盈利：status=CLOSED", row["status"] == "CLOSED", str(row["status"]))

    # 2. 做空亏损平仓：entry=100 → 平仓价 110 → PnL -10
    set_price(110.0)
    pid = insert_pos("TEST2", "SHORT", 100.0, 1.0, 10.0, 100.0)
    with storage.get_conn() as conn:
        trade_logic.manual_close_on_unwatch(conn, "TEST2")
    with storage.get_conn() as conn:
        row = dict(conn.execute(
            "SELECT * FROM trade_positions WHERE id=?", (pid,)).fetchone())
    check("做空亏损：realized=-10", abs(row["realized_pnl"] + 10.0) < 1e-9,
          str(row["realized_pnl"]))
    check("做空亏损：pnl_pct=-100%", abs(row["pnl_pct"] + 100.0) < 1e-9,
          str(row["pnl_pct"]))

    # 3. 多单盈利平仓（对照，不被做空修复破坏）：entry=90 → 平仓 100 → +10
    set_price(100.0)
    pid = insert_pos("TEST3", "LONG", 90.0, 1.0, 10.0, 90.0)
    with storage.get_conn() as conn:
        trade_logic.manual_close_on_unwatch(conn, "TEST3")
    with storage.get_conn() as conn:
        row = dict(conn.execute(
            "SELECT * FROM trade_positions WHERE id=?", (pid,)).fetchone())
    check("多单盈利：realized=+10", abs(row["realized_pnl"] - 10.0) < 1e-9,
          str(row["realized_pnl"]))

    # 4. 做空含历史已实现（TP 部分）+ 剩余平仓：方向一致
    set_price(95.0)
    pid = insert_pos("TEST4", "SHORT", 100.0, 2.0, 10.0, 200.0)
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET closed_qty=1.0, realized_pnl=5.0 "
                     "WHERE id=?", (pid,))
    with storage.get_conn() as conn:
        trade_logic.manual_close_on_unwatch(conn, "TEST4")
    with storage.get_conn() as conn:
        row = dict(conn.execute(
            "SELECT * FROM trade_positions WHERE id=?", (pid,)).fetchone())
    check("做空部分平仓：realized=5+(100-95)*1=10",
          abs(row["realized_pnl"] - 10.0) < 1e-9, str(row["realized_pnl"]))

    # 5. PENDING 挂单平仓按钮 → 取消（不计算 PnL）
    with storage.get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO trade_positions
                (token, symbol, side, status, mode, margin_amount, leverage,
                 notional, quantity, limit_price, current_price,
                 stop_loss_price, strategy)
            VALUES ('TEST5','TEST5USDT','LONG','PENDING','paper',10,1,100,1,
                    95, 95, 90, 'agent')
        """)
        pid = cur.lastrowid
    set_price(100.0)
    with storage.get_conn() as conn:
        trade_logic.manual_close_on_unwatch(conn, "TEST5")
    with storage.get_conn() as conn:
        row = dict(conn.execute(
            "SELECT * FROM trade_positions WHERE id=?", (pid,)).fetchone())
    check("PENDING 平仓 → CANCELED", row["status"] == "CANCELED", str(row["status"]))

    # 6. supertrend 手动平仓：journal 无污染 + close_snap 记录
    config.SUPERTREND_DB = os.path.join(_TMP, "supertrend.db")
    import supertrend_strategy
    supertrend_strategy.init_db()
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute(
            "INSERT INTO supertrend_status (token, trend, upper_band, lower_band, "
            "updated_at) VALUES ('TEST6','UP',105,95,1)")
    with storage.get_conn() as conn:
        jn_before = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    set_price(90.0)
    pid = insert_pos("TEST6", "SHORT", 100.0, 1.0, 10.0, 100.0)
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET strategy='supertrend' WHERE id=?", (pid,))
    with storage.get_conn() as conn:
        trade_logic.manual_close_on_unwatch(conn, "TEST6")
    with storage.get_conn() as conn:
        jn_after = conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        snap = sc.execute("SELECT * FROM supertrend_close_snaps WHERE position_id=?",
                          (pid,)).fetchone()
    check("supertrend 手动平仓不写 journal", jn_after == jn_before,
          f"{jn_before}->{jn_after}")
    check("supertrend 手动平仓记录 close_snap",
          snap is not None and snap["trend"] == "UP",
          str(dict(snap) if snap else None))

    trade_logic._position_price = REAL_POS_PRICE
    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
