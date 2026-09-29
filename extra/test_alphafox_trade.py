"""alphafox 跟单信号处理单元测试（mock 标记价 + 假快照）

覆盖：首轮基线/孤儿清理、开仓(k_qty锁定+成本价)、同币跳过(方案B)、
加仓合并均价、减仓结算、平仓、未跟随仓不操作、杠杆单独变化、对齐式自愈
"""
import io
import json
import os
import sys
import tempfile
import time

# 控制台是 GBK：断言名里的 ⚡ / − / ≈ 之类字符会让 print 直接抛 UnicodeEncodeError
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

_TMP = tempfile.mkdtemp(prefix="aft_")
config.DB_PATH = os.path.join(_TMP, "main.db")
config.ALPHAFOX_DB = os.path.join(_TMP, "alphafox.db")

import storage
import alphafox_trade as aft

with storage.get_conn() as conn:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(storage.SCHEMA)
    # 生产由 storage._migrate 加的列，这里手动补（测试只跑 SCHEMA）
    conn.execute("ALTER TABLE trade_positions ADD COLUMN managed INTEGER DEFAULT 1")
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

PRICE = {}
PERPS = set()
aft.get_mark_price = lambda token: PRICE.get(token)
aft.has_tradable_perp = lambda token: token in PERPS

PARAMS = {"budget": 1000.0, "slip": 0.05, "fee": 0.04}
T1 = "bn-111"
T2 = "bn-222"
K1 = 1000.0 / 100000.0   # T1 的 k_qty


def make_snap(trader_id, margin, positions, orders=None):
    d = {"traderName": trader_id, "marginBalance": margin, "positions": positions}
    if orders is not None:
        d["latestOrders"] = orders
    return {"data": d}


# 平台 canonicalAction 的**真实词表**（服务器真样本实测 + 前端 actLabel 同一套）。
# 夹具必须按真词表造：2026-09-14 踩过 —— 夹具写 OPEN/ADD/REDUCE/CLOSE（我方内部
# 口径），生产里平台给的是 open_long/increase_long/... → 词表对不上，笔数还原
# 静默空转，而测试全绿。
_CANON = {"OPEN": "open_long", "ADD": "increase_long",
          "REDUCE": "decrease_long", "CLOSE": "close_long"}


def mk_order(symbol, action, qty, price, time_ms, canon=None, side="BUY"):
    """平台流水的一笔。action 传内部口径（OPEN/ADD/REDUCE/CLOSE）；
    canon 可显式给平台原词（测词表/拼写兼容时用）。"""
    return {"symbol": symbol, "canonicalAction": canon or _CANON.get(action, action),
            "side": side, "executedQty": qty, "avgPrice": price,
            "notional": qty * price, "time": time_ms, "realizedPnl": 0.0}


def tp(symbol, side, qty, avg, mark, lev=2):
    """交易员持仓项"""
    return {"symbol": symbol, "side": side, "absQty": qty, "avgPrice": avg,
            "markPrice": mark, "notional": qty * mark, "leverage": lev}


def _sig_count(trader_id):
    with storage.get_conn(config.ALPHAFOX_DB) as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM alphafox_signals WHERE trader_id=?",
            (trader_id,)).fetchone()["c"]


def our_pos(token):
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM trade_positions WHERE token=? AND strategy='alphafox' "
            "AND status IN ('OPEN','PARTIAL') ORDER BY id DESC LIMIT 1",
            (token,)).fetchone()
        return dict(row) if row else None


def count_active(token):
    with storage.get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM trade_positions WHERE token=? "
            "AND strategy='alphafox' AND status IN ('OPEN','PARTIAL')",
            (token,)).fetchone()["c"]


def open_qty(token):
    """系统口径：quantity=总量、closed_qty=已平量，持仓量 = 两者之差"""
    p = our_pos(token)
    return max(float(p["quantity"]) - float(p["closed_qty"] or 0), 0) if p else 0.0


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
    aft.init_db()
    # 生产里 process() 只会被"跟单中"的交易员调用（探测线程先查 following_ids）
    # → 测试基线同样先把这些交易员登记为启用，否则等于在测不存在的调用方式
    for _t in ("bn-111", "bn-222", "bn-333", "bn-444", "bn-555", "bn-666",
               "bn-777", "bn-888", "bn-999", "bn-1000", "bn-1111",
               "bn-5555", "bn-6666", "bn-CCC", "bn-9001", "bn-9002"):
        aft.set_follow(_t, _t, "", True)
    aft._prev_snap.clear()
    aft._seen_orders.clear()

    print("== 1. 首轮基线（两交易员均空仓）==")
    s = aft.process(T1, make_snap(T1, 100000, []), PARAMS)
    aft.process(T2, make_snap(T2, 200000, []), PARAMS)
    check("首轮无信号", s == [])

    print("== 2. T1 新开 BTC → 我们开仓（k_qty 锁定 + 成本价 + 杠杆）==")
    PRICE["BTC"] = 50000
    s = aft.process(T1, make_snap(T1, 100000,
                                  [tp("BTCUSDT", "LONG", 100, 50000, 50000, lev=3)]), PARAMS)
    check("开仓信号", len(s) == 1 and s[0]["action"] == "OPEN", str(s))
    p = our_pos("BTC")
    check("已开仓", p is not None and p["side"] == "LONG")
    expect_qty = 100 * K1
    check("qty = 交易员qty × k_qty", p and abs(p["quantity"] - expect_qty) < 1e-9,
          str(p and p["quantity"]))
    buy = 50000 * (1 + 0.05 / 100) * (1 + 0.04 / 100)
    check("entry 含成本", p and abs(p["entry_price"] - buy) < 1e-6,
          str(p and p["entry_price"]))
    check("杠杆跟随(3x)", p and p["leverage"] == 3)
    entry0 = p["entry_price"]

    print("== 3. T2 开 BTC → 跳过（方案B）==")
    s = aft.process(T2, make_snap(T2, 200000,
                                  [tp("BTCUSDT", "LONG", 50, 50000, 50000)]), PARAMS)
    check("SKIP 信号", len(s) == 1 and s[0]["action"] == "SKIP", str(s))
    check("不开第二笔", abs(our_pos("BTC")["quantity"] - expect_qty) < 1e-9)

    print("== 4. T1 加仓（100→200）→ 合并加仓加权均价 ==")
    PRICE["BTC"] = 52000
    s = aft.process(T1, make_snap(T1, 100000,
                                  [tp("BTCUSDT", "LONG", 200, 52000, 52000, lev=3)]), PARAMS)
    check("ADD 信号", len(s) == 1 and s[0]["action"] == "ADD", str(s))
    p = our_pos("BTC")
    q2 = 200 * K1
    check("qty 对齐", p and abs(p["quantity"] - q2) < 1e-9, str(p and p["quantity"]))
    buy2 = 52000 * (1 + 0.05 / 100) * (1 + 0.04 / 100)
    expect_entry = (entry0 * expect_qty + buy2 * (q2 - expect_qty)) / q2
    check("加权均价", p and abs(p["entry_price"] - expect_entry) < 1e-6,
          str(p and p["entry_price"]))
    check("加仓后 margin 同步（净持仓×entry/杠杆）",
          p and abs(p["margin_amount"] - q2 * expect_entry / 3) < 1e-6,
          str(p and p["margin_amount"]))

    print("== 5. T2 加仓 BTC → 识别不操作（未跟随仓）==")
    s = aft.process(T2, make_snap(T2, 200000,
                                  [tp("BTCUSDT", "LONG", 100, 50000, 50000)]), PARAMS)
    check("T2 ADD 信号已识别", len(s) == 1 and s[0]["action"] == "ADD", str(s))
    check("我们仓位不动", abs(our_pos("BTC")["quantity"] - q2) < 1e-9)

    print("== 6. T1 减仓（200→50）→ 比例减仓结算 ==")
    PRICE["BTC"] = 51000
    s = aft.process(T1, make_snap(T1, 100000,
                                  [tp("BTCUSDT", "LONG", 50, 51000, 51000, lev=3)]), PARAMS)
    check("REDUCE 信号", len(s) == 1 and s[0]["action"] == "REDUCE", str(s))
    p = our_pos("BTC")
    q3 = 50 * K1
    # 系统口径：quantity=总数量不变，closed_qty=已平数量，净持仓 = quantity - closed_qty
    net = float(p["quantity"]) - float(p["closed_qty"] or 0)
    check("净持仓对齐（quantity-closed_qty）", p and abs(net - q3) < 1e-9,
          str(p and (p["quantity"], p["closed_qty"])))
    check("closed_qty 记录", p and abs((p["closed_qty"] or 0) - (q2 - q3)) < 1e-9)
    sell = 51000 * (1 - 0.05 / 100) * (1 - 0.04 / 100)
    expect_real = (sell - expect_entry) * (q2 - q3)
    check("realized 结算", p and abs((p["realized_pnl"] or 0) - expect_real) < 1e-6,
          str(p and p["realized_pnl"]))
    check("减仓后 margin 同步", p and abs(p["margin_amount"] - q3 * expect_entry / 3) < 1e-6,
          str(p and p["margin_amount"]))

    print("== 7. T1 平仓 → 我们全平（两轮确认机制）==")
    aft.process(T1, make_snap(T1, 100000, []), PARAMS)  # 第1轮：待确认
    s = aft.process(T1, make_snap(T1, 100000, []), PARAMS)  # 第2轮：确认平仓
    check("CLOSE 信号", len(s) == 1 and s[0]["action"] == "CLOSE", str(s))
    check("已全平", our_pos("BTC") is None)

    print("== 8. T2 仍持有 BTC → 未跟随不操作；T2 平后再开 → 我们开 ==")
    aft.process(T2, make_snap(T2, 200000, []), PARAMS)  # T2 平仓第1轮
    aft.process(T2, make_snap(T2, 200000, []), PARAMS)  # T2 平仓第2轮
    PRICE["BTC"] = 50000
    s = aft.process(T2, make_snap(T2, 200000,
                                  [tp("BTCUSDT", "LONG", 80, 50000, 50000, lev=2)]), PARAMS)
    check("T2 重开 → 我们开仓", len(s) == 1 and s[0]["action"] == "OPEN"
          and our_pos("BTC") is not None, str(s))
    k2 = 1000.0 / 200000.0
    check("新 k_qty 生效", abs(our_pos("BTC")["quantity"] - 80 * k2) < 1e-9)

    print("== 9. 杠杆单独变化 → LEV 信号（不操作仓位金额，仅杠杆同步）==")
    s = aft.process(T2, make_snap(T2, 200000,
                                  [tp("BTCUSDT", "LONG", 80, 50000, 50000, lev=5)]), PARAMS)
    check("LEV 信号", len(s) == 1 and s[0]["action"] == "LEV", str(s))
    check("杠杆同步", our_pos("BTC")["leverage"] == 5)

    print("== 10. 跟单开关管理 ==")
    aft.set_follow(T1, "熬鹰", "Binance", False)
    check("移除跟单", T1 not in aft.following_ids())
    aft.set_follow(T1, "熬鹰", "Binance", True)
    check("重新跟单", T1 in aft.following_ids())

    print("== 11. 币安价失败 → 快照价兜底平仓成功 ==")
    # 当前 T2 持有 BTC 仓（步骤 8 开），模拟 T2 平仓但币安标记价接口失败
    aft.process(T2, make_snap(T2, 200000, []), PARAMS)  # 第1轮：待确认
    PRICE.pop("BTC", None)
    s = aft.process(T2, make_snap(T2, 200000, []), PARAMS)  # 第2轮：确认，币安价失败
    check("币安价失败：快照价兜底平仓成功",
          len(s) == 1 and s[0]["action"] == "CLOSE" and s[0]["executed"] is True
          and not s[0].get("retry"), str(s))
    check("仓位已平", our_pos("BTC") is None)
    s2 = aft.process(T2, make_snap(T2, 200000, []), PARAMS)
    check("循环停止（prev 已更新无重复 CLOSE）", s2 == [], str(s2))
    PRICE["BTC"] = 50000

    print("== 11b. 首轮孤儿清理：我们的归属仓但交易员已平 → 平仓 ==")
    # 构造：手工开一笔归属 T1 的仓，然后 T1 首轮快照不含该仓
    aft._prev_snap.pop(T1, None)
    with storage.get_conn() as conn:
        conn.execute("""
            INSERT INTO trade_positions
            (token, symbol, side, status, mode, margin_amount, leverage, notional,
             quantity, entry_price, limit_price, current_price, stop_loss_price,
             tp1_price, tp2_price, highest_price, lowest_price, trailing_stop_price,
             signal_snapshot, open_reason, advice, strategy)
            VALUES ('ORPHAN','ORPHANUSDT','LONG','OPEN','paper',50,2,100,
             2,50,50,50,NULL,NULL,NULL,50,NULL,NULL,
             '{\"trader_id\": \"bn-111\", \"k_qty\": 0.01, \"label\": \"x\"}',
             'test','test','alphafox')
        """)
    PRICE["ORPHAN"] = 55
    aft.process(T1, make_snap(T1, 100000, []), PARAMS)  # T1 首轮（只识别候选）
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM trade_positions WHERE token='ORPHAN'").fetchone()
    check("首轮只识别不动作（防首轮快照抖动误平）", row and row["status"] == "OPEN",
          str(dict(row) if row else None))
    aft.process(T1, make_snap(T1, 100000, []), PARAMS)  # 第二轮确认 → 平仓
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM trade_positions WHERE token='ORPHAN'").fetchone()
    check("连续两轮都没有 → 孤儿仓被平", row and row["status"] == "CLOSED",
          str(dict(row) if row else None))

    print("== 12. 币安价失败 → 开仓跳过（不兜底）+ 日志 ==")
    T6 = "bn-666"
    aft.process(T6, make_snap(T6, 100000, []), PARAMS)  # 基线
    PRICE.pop("BTC", None)
    s = aft.process(T6, make_snap(T6, 100000,
                                  [tp("BTCUSDT", "LONG", 60, 50000, 50000)]), PARAMS)
    check("币安无交易对 → SKIP 不开仓", len(s) == 1 and s[0]["action"] == "SKIP"
          and our_pos("BTC") is None, str(s))
    PRICE["BTC"] = 50000
    # T2 价格正常时开仓成功（供组 13 使用）
    s2 = aft.process(T2, make_snap(T2, 200000,
                                   [tp("BTCUSDT", "LONG", 60, 50000, 50000)]), PARAMS)
    check("T2 价格正常时开仓成功", len(s2) == 1 and s2[0]["action"] == "OPEN"
          and our_pos("BTC") is not None, str(s2))

    print("== 13. 快照抖动防护：单轮空快照不误判 ==")
    # 当前 T2 持有 BTC 仓（步骤 12 开，qty=60 lev=2）
    s = aft.process(T2, make_snap(T2, 200000, []), PARAMS)
    check("第1轮空快照跳过（无 CLOSE 信号）", s == [], str(s))
    check("仓位仍在（未误平）", our_pos("BTC") is not None)
    # 数据恢复 → 与 prev 相同 → 无信号
    s2 = aft.process(T2, make_snap(T2, 200000,
                                   [tp("BTCUSDT", "LONG", 60, 50000, 50000)]), PARAMS)
    check("恢复后正常对比（无信号）", s2 == [], str(s2))

    print("== 14. 取消跟单清异常计数（防御）==")
    aft.process(T2, make_snap(T2, 200000, []), PARAMS)  # 制造异常第1轮
    check("异常计数已记录", (aft._anomaly_rounds.get(T2) or {}).get("rounds") == 1)
    aft.set_follow(T2, "x", "Binance", False)
    aft.set_follow(T2, "x", "Binance", True)
    check("取消跟单后异常计数清除", aft._anomaly_rounds.get(T2, 0) == 0)

    print("== 15. 骤降防护：5仓→2仓 两轮确认 ==")
    aft._prev_snap.pop(T1, None)
    aft._anomaly_rounds.pop(T1, None)
    positions = [tp(f"T{i}USDT", "LONG", 10, 100 + i, 100 + i) for i in range(5)]
    aft.process(T1, make_snap(T1, 100000, positions), PARAMS)  # 基线 5 仓
    cur2 = positions[:2]  # 骤降到 2 仓
    s1 = aft.process(T1, make_snap(T1, 100000, cur2), PARAMS)
    check("第1轮骤降跳过（无信号）", s1 == [], str(s1))
    s2 = aft.process(T1, make_snap(T1, 100000, cur2), PARAMS)
    check("第2轮确认：3 个 CLOSE 信号",
          len(s2) == 3 and all(x["action"] == "CLOSE" for x in s2),
          str([x["action"] for x in s2]))
    # 单仓丢失（3→2 不达阈值）不防护：真实/抖动由两轮确认兜底
    aft._prev_snap.pop(T1, None)
    aft._anomaly_rounds.pop(T1, None)
    pos3 = positions[:3]
    aft.process(T1, make_snap(T1, 100000, pos3), PARAMS)  # 基线 3 仓
    s3 = aft.process(T1, make_snap(T1, 100000, pos3[:2]), PARAMS)  # 丢 1 个（2/3 不达阈值）
    check("丢1个不触发骤降防护（直接 CLOSE 信号）",
          len(s3) == 1 and s3[0]["action"] == "CLOSE", str([x["action"] for x in s3]))

    print("== 16. 组合：异常确认轮 + 币安价失败 → 快照价兜底平仓 ==")
    with storage.get_conn() as conn:
        conn.execute("DELETE FROM trade_positions WHERE strategy='alphafox'")
    aft._prev_snap.pop(T1, None)
    aft._anomaly_rounds.pop(T1, None)
    aft.process(T1, make_snap(T1, 100000,
                              [tp("BTCUSDT", "LONG", 10, 50000, 50000)]), PARAMS)  # 基线
    with storage.get_conn() as conn:
        conn.execute("""
            INSERT INTO trade_positions
            (token, symbol, side, status, mode, margin_amount, leverage, notional,
             quantity, entry_price, limit_price, current_price, stop_loss_price,
             tp1_price, tp2_price, highest_price, lowest_price, trailing_stop_price,
             signal_snapshot, open_reason, advice, strategy)
            VALUES ('BTC','BTCUSDT','LONG','OPEN','paper',50,2,100,0.002,50000,50000,50000,
            NULL,NULL,NULL,50000,NULL,NULL,
            '{\"trader_id\": \"bn-111\", \"k_qty\": 0.0002, \"label\": \"x\"}',
            't','t','alphafox')
        """)
    aft.process(T1, make_snap(T1, 100000, []), PARAMS)  # 第1轮：待确认
    PRICE.pop("BTC", None)
    s = aft.process(T1, make_snap(T1, 100000, []), PARAMS)  # 第2轮：确认+币安价失败
    check("确认轮币安价失败 → 快照价兜底平仓成功",
          len(s) == 1 and s[0]["action"] == "CLOSE" and s[0]["executed"] is True
          and not s[0].get("retry"), str(s))
    check("仓位已平", our_pos("BTC") is None)
    PRICE["BTC"] = 50000

    print("== 17. 做空开仓价 = 卖出价（成本下浮）==")
    with storage.get_conn() as conn:
        conn.execute("DELETE FROM trade_positions WHERE strategy='alphafox'")
    aft._prev_snap.pop(T1, None)
    aft._anomaly_rounds.pop(T1, None)
    aft.process(T1, make_snap(T1, 100000, []), PARAMS)  # 基线空
    PRICE["BTC"] = 50000
    s = aft.process(T1, make_snap(T1, 100000,
                                  [tp("BTCUSDT", "SHORT", 100, 50000, 50000, lev=3)]), PARAMS)
    check("开空信号", len(s) == 1 and s[0]["action"] == "OPEN", str(s))
    p = our_pos("BTC")
    sell = 50000 * (1 - 0.05 / 100) * (1 - 0.04 / 100)
    check("空单 entry = 卖出价（含成本下浮）", p and abs(p["entry_price"] - sell) < 1e-6,
          str(p and p["entry_price"]))

    print("== 18. 空单加仓加权均价 + 减仓 realized ==")
    # 17 已开 SHORT BTC：qty=100×k @ 卖出价49955.01，lev=3
    PRICE["BTC"] = 49000
    s = aft.process(T1, make_snap(T1, 100000,
                                  [tp("BTCUSDT", "SHORT", 200, 49000, 49000, lev=3)]), PARAMS)
    check("空单加仓信号", len(s) == 1 and s[0]["action"] == "ADD", str(s))
    p = our_pos("BTC")
    k = 1000.0 / 100000.0
    q1 = 100 * k
    q2 = 200 * k
    sell2 = 49000 * (1 - 0.05 / 100) * (1 - 0.04 / 100)
    entry0 = 50000 * (1 - 0.05 / 100) * (1 - 0.04 / 100)
    expect_entry = (entry0 * q1 + sell2 * (q2 - q1)) / q2
    check("空单加权均价（卖出价加权）", p and abs(p["entry_price"] - expect_entry) < 1e-6,
          str(p and p["entry_price"]))
    # 减仓：200 → 50，realized = (entry - 买回价) × Δ（空单平仓是买回，走 _buy_price）
    PRICE["BTC"] = 48500
    s = aft.process(T1, make_snap(T1, 100000,
                                  [tp("BTCUSDT", "SHORT", 50, 48500, 48500, lev=3)]), PARAMS)
    check("空单减仓信号", len(s) == 1 and s[0]["action"] == "REDUCE", str(s))
    p = our_pos("BTC")
    q3 = 50 * k
    buy3 = 48500 * (1 + 0.05 / 100) * (1 + 0.04 / 100)
    expect_real = (expect_entry - buy3) * (q2 - q3)
    check("空单减仓 realized（按买回价结算）",
          p and abs((p["realized_pnl"] or 0) - expect_real) < 1e-6,
          str(p and p["realized_pnl"]))

    print("== 19. 股票代币前缀（xyz:SKHX → 币安 SKHYNIX）+ 标记价失败兜底 ==")
    T3 = "bn-333"
    aft.process(T3, make_snap(T3, 100000, []), PARAMS)  # 基线
    # 场景a：币安有 SKHYNIX 价 → 正常开仓，token 已映射
    PRICE["SKHYNIX"] = 1400
    s = aft.process(T3, make_snap(T3, 100000,
                                  [tp("XYZ:SKHX", "SHORT", 100, 1387.089, 1371.4, lev=10)]),
                    PARAMS)
    check("开仓信号（非 SKIP）", len(s) == 1 and s[0]["action"] == "OPEN", str(s))
    p = our_pos("SKHYNIX")
    check("token 映射为币安 SKHYNIX", p is not None and p["token"] == "SKHYNIX",
          str(p and p["token"]))
    sell = 1400 * (1 - 0.05 / 100) * (1 - 0.04 / 100)
    check("entry 用币安价（卖出价）", p and abs(p["entry_price"] - sell) < 1e-6,
          str(p and p["entry_price"]))
    check("杠杆跟随(10x)", p and p["leverage"] == 10)
    # 平仓：币安价失败 → 快照价兜底 → 两轮确认机制
    s1 = aft.process(T3, make_snap(T3, 100000, []), PARAMS)
    check("第一轮快照空 → 待确认无信号", s1 == [], str(s1))
    PRICE.pop("SKHYNIX", None)  # 币安价失败
    s2 = aft.process(T3, make_snap(T3, 100000, []), PARAMS)
    check("第二轮确认 CLOSE 执行（快照价兜底）", len(s2) == 1 and s2[0]["action"] == "CLOSE"
          and s2[0].get("executed"), str(s2))
    check("本地已平仓", our_pos("SKHYNIX") is None)
    # 第三轮不再重复（prev 已更新）
    s3 = aft.process(T3, make_snap(T3, 100000, []), PARAMS)
    check("循环停止（无重复 CLOSE）", s3 == [], str(s3))

    print("== 20. retry 轮全部信号标 retry（TG 防刷屏）==")
    T4 = "bn-444"
    # 基线：快照含 ZEC（基线轮只建 prev 不发信号，本地无仓）
    aft.process(T4, make_snap(T4, 100000, [tp("ZECUSDT", "LONG", 10, 100, 100)]), PARAMS)
    check("基线已建立（本地无仓）", our_pos("ZEC") is None)
    # 快照切换 + margin=0：CLOSE（本地无仓 → 非异常失败）+ OPEN（margin 异常 → retry）
    s = aft.process(T4, make_snap(T4, 0, [tp("XYZ:AAA", "LONG", 5, 100, 100)]), PARAMS)
    actions = {sg["action"] for sg in s}
    check("一轮含 CLOSE + SKIP", "CLOSE" in actions and "SKIP" in actions, str(s))
    check("全部信号标 retry（含非异常失败的 CLOSE/SKIP）",
          len(s) >= 2 and all(sg.get("retry") for sg in s), str(s))

    print("== 21. 币安无对应交易对的标的 → SKIP 一次不循环 ==")
    T5 = "bn-555"
    aft.process(T5, make_snap(T5, 100000, []), PARAMS)  # 基线
    s = aft.process(T5, make_snap(T5, 100000,
                                  [tp("XYZ:AAA", "LONG", 5, 100, 100)]), PARAMS)
    check("无交易对标的 SKIP 不开仓", len(s) == 1 and s[0]["action"] == "SKIP"
          and our_pos("AAA") is None, str(s))
    s2 = aft.process(T5, make_snap(T5, 100000,
                                   [tp("XYZ:AAA", "LONG", 5, 100, 100)]), PARAMS)
    check("SKIP 后不循环（prev 已更新无重复信号）", s2 == [], str(s2))

    print("== 22. Hyperliquid 符号同步 + 已有映射不覆盖 ==")
    import requests as _req

    class FakeResp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    real_post = _req.post
    _req.post = lambda url, json=None, timeout=None: FakeResp({
        "universe": [{"name": "xyz:SKHX"}, {"name": "xyz:MSTR"},
                     {"name": "BTC"}, {"name": "km:XXX"}]})
    r = aft.sync_symbols_from_hl()
    check("同步：新增 2（MSTR/XXX）、已存在跳过 1（SKHX）、无前缀过滤（BTC）",
          r.get("added") == 2 and r.get("skipped") == 1, str(r))
    with storage.get_conn(config.ALPHAFOX_DB) as conn:
        rows = {x["src_symbol"]: x["token"] for x in conn.execute(
            "SELECT src_symbol, token FROM alphafox_symbol_map")}
    check("SKHX 特例映射未被同步覆盖", rows.get("SKHX") == "SKHYNIX",
          str(rows.get("SKHX")))
    check("MSTR 同名入库", rows.get("MSTR") == "MSTR", str(rows.get("MSTR")))
    check("XXX 入库", rows.get("XXX") == "XXX", str(rows.get("XXX")))
    _req.post = real_post

    print("== 23. DB label 优先于 API traderName（防 alphofox 串名） ==")
    T7 = "bn-777"
    aft.set_follow(T7, "熬鹰资本", "", True)

    def named_snap(name, margin, positions):
        return {"data": {"traderName": name, "marginBalance": margin,
                         "positions": positions}}

    aft.process(T7, named_snap("熬鹰链上fomo", 100000, []), PARAMS)  # 首轮基线
    PRICE["LBLT"] = 66
    s = aft.process(T7, named_snap("熬鹰链上fomo", 100000,
                                   [tp("LBLTUSDT", "LONG", 10, 66, 66, lev=2)]),
                    PARAMS)
    check("信号 label 取 DB 的熬鹰资本（API 串名被忽略）",
          len(s) == 1 and s[0]["label"] == "熬鹰资本",
          str([x.get("label") for x in s]))
    with storage.get_conn(config.ALPHAFOX_DB) as conn:
        row = conn.execute(
            "SELECT trader_label FROM alphafox_signals WHERE trader_id=? "
            "ORDER BY id DESC LIMIT 1", (T7,)).fetchone()
    check("信号日志 trader_label = 熬鹰资本",
          row and row["trader_label"] == "熬鹰资本",
          str(row and row["trader_label"]))

    print("== 24. 异常确认计数时间窗口（停更多天后不误判） ==")
    T8 = "bn-888"
    aft.process(T8, make_snap(T8, 100000,
                              [tp("WIFUSDT", "LONG", 10, 50, 50)]), PARAMS)  # 基线
    # 模拟长时间停更后残留的旧计数（与上次异常间隔 999999 秒）
    aft._anomaly_rounds[T8] = {"rounds": 1, "ts": int(time.time()) - 999999}
    _buf = io.StringIO()
    _old_out = sys.stdout
    sys.stdout = _buf
    try:                       # 捕获诊断日志（判断平台抖动 vs 真清仓要靠它）
        s = aft.process(T8, make_snap(T8, 100000, []), PARAMS)
        _diag1 = _buf.getvalue()
        sys.stdout = _old_out
        check("间隔过久：异常重新从第 1 轮计（不确认、无信号）", s == [], str(s))
        _buf2 = io.StringIO()
        sys.stdout = _buf2
        s = aft.process(T8, make_snap(T8, 100000, []), PARAMS)
    finally:
        sys.stdout = _old_out
    check("连续第 2 轮：确认真实清仓（产生 CLOSE 信号）",
          len(s) >= 1 and s[0]["action"] == "CLOSE", str(s))
    _diag = (_diag1 + _buf2.getvalue())
    check("异常时落结构化诊断日志（[AFLIVE-ANOMALY]，含 raw_n/gone/polledAt）",
          "[AFLIVE-ANOMALY]" in _diag and '"raw_n"' in _diag
          and '"gone"' in _diag and '"polledAt"' in _diag,
          _diag[:200])

    print("== 25. REDUCE 到零 → 仓位 CLOSED（防幽灵仓位） ==")
    T9 = "bn-999"
    PERPS.add("BNBUSDT".replace("USDT", ""))
    aft.process(T9, make_snap(T9, 100000, []), PARAMS)  # 基线
    PRICE["DOT2"] = 7.0
    PERPS.add("DOT2")
    s = aft.process(T9, make_snap(T9, 100000,
                                  [tp("DOT2USDT", "LONG", 10, 7, 7)]), PARAMS)
    check("前置：已开仓", len(s) == 1 and s[0]["executed"], str(s))
    p = our_pos("DOT2")
    check("前置：仓位 OPEN", p is not None and p["status"] == "OPEN")
    # 交易员减到 0（快照行还在）→ REDUCE 全平
    s = aft.process(T9, make_snap(T9, 100000,
                                  [tp("DOT2USDT", "LONG", 0, 7, 7)]), PARAMS)
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT status, closed_qty, quantity, realized_pnl FROM trade_positions "
            "WHERE token='DOT2' AND strategy='alphafox' "
            "ORDER BY id DESC LIMIT 1").fetchone()
    check("减到零：仓位 CLOSED（非幽灵 OPEN）",
          row and row["status"] == "CLOSED", str(row))
    check("减到零：closed_qty == quantity",
          row and abs(row["closed_qty"] - row["quantity"]) < 1e-9, str(row))
    check("信号按'平'上报（减到零=全平，别报成减）",
          len(s) == 1 and s[0]["action"] == "CLOSE" and s[0]["executed"], str(s))

    print("== 26. 有交易对但价格临时失败 → retry 下轮重试 ==")
    T10 = "bn-1000"
    PERPS.add("SOL2")
    aft.process(T10, make_snap(T10, 100000, []), PARAMS)  # 基线
    s = aft.process(T10, make_snap(T10, 100000,
                                   [tp("SOL2USDT", "LONG", 5, 20, 20)]), PARAMS)
    check("价格失败：信号 SKIP + retry（prev 不更新）",
          len(s) == 1 and s[0]["action"] == "SKIP" and s[0].get("retry"),
          str(s))
    check("价格失败：未开仓", our_pos("SOL2") is None)
    PRICE["SOL2"] = 20.0
    s = aft.process(T10, make_snap(T10, 100000,
                                   [tp("SOL2USDT", "LONG", 5, 20, 20)]), PARAMS)
    check("价格恢复：重试开仓成功",
          len(s) == 1 and s[0]["executed"] and our_pos("SOL2") is not None,
          str(s))

    print("== 27. 跟单开关关闭：不跟新仓，平仓/对齐照常 ==")
    T11 = "bn-1111"
    P0 = {"budget": 1000.0, "slip": 0.05, "fee": 0.04, "allow_open": False}
    PERPS.add("AVAX2")
    PRICE["AVAX2"] = 30.0
    aft.process(T11, make_snap(T11, 100000, []), PARAMS)  # 基线
    s = aft.process(T11, make_snap(T11, 100000,
                                   [tp("AVAX2USDT", "LONG", 10, 30, 30)]), P0)
    check("开关关：OPEN 信号 SKIP", len(s) == 1 and s[0]["action"] == "SKIP",
          str(s))
    check("开关关：未开仓", our_pos("AVAX2") is None)
    s = aft.process(T11, make_snap(T11, 100000,
                                   [tp("AVAX2USDT", "LONG", 10, 30, 30)]), P0)
    check("开关关：prev 已更新（不重复 SKIP 信号）", s == [], str(s))
    # 重新开启：期间开的仓不补，但交易员平仓重开时跟随
    s = aft.process(T11, make_snap(T11, 100000, []), PARAMS)  # 空快照第1轮：待确认
    check("重开后：空快照第1轮待确认（防抖动）", s == [], str(s))
    s = aft.process(T11, make_snap(T11, 100000, []), PARAMS)  # 第2轮：确认真实清仓
    check("重开后：交易员平仓信号（executed=False 无仓可平）",
          len(s) == 1 and s[0]["action"] == "CLOSE", str(s))
    s = aft.process(T11, make_snap(T11, 100000,
                                   [tp("AVAX2USDT", "LONG", 8, 31, 31)]), PARAMS)
    check("重开后：新变化正常跟随开仓",
          len(s) == 1 and s[0]["executed"] and our_pos("AVAX2") is not None,
          str(s))

    print("== 28. 空单全平：成交价按买回价（_buy_price）结算 ==")
    T5 = "bn-5555"
    aft.process(T5, make_snap(T5, 100000, []), PARAMS)  # 基线
    PRICE["SOLX"] = 200
    s = aft.process(T5, make_snap(T5, 100000,
                                  [tp("SOLXUSDT", "SHORT", 100, 200, 200, lev=5)]), PARAMS)
    check("开空信号已执行", len(s) == 1 and s[0]["action"] == "OPEN" and s[0]["executed"],
          str(s))
    p = our_pos("SOLX")
    entry_short = 200 * (1 - 0.05 / 100) * (1 - 0.04 / 100)
    check("空单开仓用卖出价（mark 下浮）",
          p and abs(p["entry_price"] - entry_short) < 1e-6,
          str(p and p["entry_price"]))
    qty5 = 100 * K1
    PRICE["SOLX"] = 180
    aft.process(T5, make_snap(T5, 100000, []), PARAMS)   # 异常确认第 1 轮
    s = aft.process(T5, make_snap(T5, 100000, []), PARAMS)  # 第 2 轮 → CLOSE
    check("CLOSE 信号已执行",
          len(s) == 1 and s[0]["action"] == "CLOSE" and s[0]["executed"], str(s))
    buyback = 180 * (1 + 0.05 / 100) * (1 + 0.04 / 100)
    expect_real = (entry_short - buyback) * qty5
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM trade_positions WHERE token='SOLX' "
            "AND strategy='alphafox' ORDER BY id DESC LIMIT 1").fetchone()
    check("平仓价 = 买回价（mark 上浮，不是卖出价）",
          row and abs(row["current_price"] - buyback) < 1e-6,
          str(row and row["current_price"]))
    check("realized 按买回价结算",
          row and abs((row["realized_pnl"] or 0) - expect_real) < 1e-6,
          str(row and row["realized_pnl"]))

    print("== 29. 同时加仓 + 改杠杆：margin 用变化后的持仓量 ==")
    T6 = "bn-6666"
    aft.process(T6, make_snap(T6, 100000, []), PARAMS)
    PRICE["LINKX"] = 50
    s = aft.process(T6, make_snap(T6, 100000,
                                  [tp("LINKXUSDT", "LONG", 100, 50, 50, lev=2)]), PARAMS)
    check("开仓已执行", our_pos("LINKX") is not None, str(s))
    # 一轮内既加仓（100→200）又改杠杆（2→5）
    s = aft.process(T6, make_snap(T6, 100000,
                                  [tp("LINKXUSDT", "LONG", 200, 50, 50, lev=5)]), PARAMS)
    check("ADD 信号已执行", len(s) == 1 and s[0]["action"] == "ADD", str(s))
    p1 = our_pos("LINKX")
    check("杠杆已同步到 5x", p1 and abs(p1["leverage"] - 5) < 1e-9,
          str(p1 and p1["leverage"]))
    expect_margin = p1["quantity"] * p1["entry_price"] / 5
    check("margin = 变化后持仓量 × 均价 / 新杠杆（不少算）",
          p1 and abs(p1["margin_amount"] - expect_margin) < 1e-6,
          f'{p1 and p1["margin_amount"]} vs {expect_margin}')

    print("== 32. 映射表加载失败：保持未加载（下次重试），不回退永久 identity ==")
    _orig_conn = storage.get_conn

    def _boom(*a, **k):
        raise RuntimeError("db down")

    storage.get_conn = _boom
    try:
        aft.reload_symbol_map()
    finally:
        storage.get_conn = _orig_conn
    check("加载失败后缓存为 None（下次会重试）", aft._map_cache is None,
          str(aft._map_cache))
    aft.reload_symbol_map()
    check("恢复后缓存为 dict", isinstance(aft._map_cache, dict),
          str(type(aft._map_cache)))

    print("== 33. 全局 mode=live：不按交易所持仓误平跟单/超势纸盘仓 ==")
    import trade_logic
    import exchange as _ex2
    with storage.get_conn() as conn:
        storage.trading_settings_update(conn, {"mode": "live"})
        for tok, strat in (("LIVEAF", "alphafox"), ("LIVEST", "supertrend"),
                           ("HOLDX", "agent")):
            conn.execute(
                "INSERT INTO trade_positions (token, symbol, side, status, mode, "
                "margin_amount, leverage, notional, quantity, entry_price, "
                "current_price, strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (tok, f"{tok}USDT", "LONG", "OPEN", "paper", 50, 3, 150, 1.0,
                 100, 100, strat))
    # 哨兵价：live 模式下这两行也必须被纸盘循环刷新（下面断言会检查）
    with storage.get_conn() as conn:
        conn.execute("UPDATE trade_positions SET current_price=1.0 "
                     "WHERE token IN ('LIVEAF','LIVEST')")
    # 交易所返回"我们持有的某个币"（HOLDX）→ live 分支的兜底判断成立，
    # 此时不在交易所的 LIVEAF/LIVEST 就会被误判成"实盘已平仓"
    _ex2.get_positions = lambda *a, **k: [
        {"symbol": "HOLDXUSDT", "quantity": 1.0, "mark_price": 100.0,
         "unrealized_pnl": 0.0, "leverage": 3, "entry_price": 100.0,
         "position_side": "LONG"}]
    trade_logic.get_mark_price = lambda token: 100.0
    try:
        with storage.get_conn() as conn:
            trade_logic.update_paper_positions(conn)
    finally:
        with storage.get_conn() as conn:
            storage.trading_settings_update(conn, {"mode": "paper"})
    with storage.get_conn() as conn:
        rows = {r["token"]: dict(r) for r in conn.execute(
            "SELECT token, status, current_price FROM trade_positions "
            "WHERE token IN ('LIVEAF','LIVEST')")}
    check("跟单纸盘仓未被误平", rows["LIVEAF"]["status"] == "OPEN",
          str(rows["LIVEAF"]))
    check("超势纸盘仓未被误平", rows["LIVEST"]["status"] == "OPEN",
          str(rows["LIVEST"]))
    check("跟单纸盘仓仍被刷新（价格从哨兵值更新）",
          rows["LIVEAF"]["current_price"] != 1.0, str(rows["LIVEAF"]))

    print("== 34. 取消跟单与本轮快照竞态：已取消的不许再开仓/写回基线 ==")
    TC = "bn-CCC"
    aft.set_follow(TC, "C", "", True)
    aft._prev_snap.clear()
    aft.process(TC, make_snap(TC, 100000, []), PARAMS)   # 首轮基线
    aft.set_follow(TC, "C", "", False)                   # 用户点"取消跟单"
    PRICE["CANC1"] = 50
    # 探测线程手里还是"取消前"的那份快照（本轮 url 请求期间状态已变）
    s = aft.process(TC, make_snap(TC, 100000,
                                  [tp("CANC1USDT", "LONG", 10, 50, 50, lev=2)]),
                    PARAMS)
    check("已取消：本轮不开新仓", our_pos("CANC1") is None, str(our_pos("CANC1")))
    check("已取消：无信号产出", s == [], str(s))
    check("已取消：基线不被写回（重新跟单首轮才会重建）",
          TC not in aft._prev_snap, str(list(aft._prev_snap.keys())))

    print("== 35. 竞态：行情拉取期间被取消（入口校验已过，落库前必须再校验） ==")
    TD = "bn-DDD"
    aft.set_follow(TD, "D", "", True)
    aft._prev_snap.clear()
    aft.process(TD, make_snap(TD, 100000, []), PARAMS)   # 基线
    _real_mark = aft.get_mark_price

    def _mark_and_cancel(token):
        aft.set_follow(TD, "D", "", False)   # 用户在本轮取价期间点了"取消跟单"
        return PRICE.get(token)

    PRICE["CANC2"] = 50
    aft.get_mark_price = _mark_and_cancel
    try:
        s = aft.process(TD, make_snap(TD, 100000,
                                      [tp("CANC2USDT", "LONG", 10, 50, 50, lev=2)]),
                        PARAMS)
    finally:
        aft.get_mark_price = _real_mark
    check("取价期间被取消 → 不落库（不会留下无人管理的仓）",
          our_pos("CANC2") is None, str(our_pos("CANC2")))
    check("取价期间被取消 → 该信号标 SKIP",
          len(s) == 1 and s[0]["action"] == "SKIP",
          str([(x.get("action"), x.get("executed")) for x in s]))
    check("取价期间被取消 → 末尾不写回基线",
          TD not in aft._prev_snap, str(list(aft._prev_snap.keys())))

    print("== 36. 孤儿确认遇价格不可用：候选不许丢（丢了这仓永远没人平） ==")
    TE = "bn-EEE"
    aft.set_follow(TE, "E", "", True)
    aft._prev_snap.clear()
    with storage.get_conn() as conn:   # 我们持有、但交易员没有的仓（孤儿）
        conn.execute(
            "INSERT INTO trade_positions (token, symbol, side, status, mode, "
            "margin_amount, leverage, notional, quantity, entry_price, "
            "current_price, strategy, closed_qty, signal_snapshot) "
            "VALUES ('ORPH1','ORPH1USDT','LONG','OPEN','paper',100.0,2.0,200.0,"
            "1.0,100.0,100.0,'alphafox',0.0,?)",
            (json.dumps({"trader_id": TE, "k_qty": 0.01}),))
    PRICE["ORPH1"] = 100.0
    aft.process(TE, make_snap(TE, 100000, []), PARAMS)   # 首轮：只识别候选
    check("首轮记下孤儿候选", TE in aft._orphan_pending
          and len(aft._orphan_pending[TE]) == 1, str(aft._orphan_pending.get(TE)))
    PRICE.pop("ORPH1")                                   # 标记价不可用
    aft.process(TE, make_snap(TE, 100000, []), PARAMS)
    check("价格不可用：候选留到下一轮（没被丢掉）",
          TE in aft._orphan_pending and len(aft._orphan_pending[TE]) == 1,
          str(aft._orphan_pending.get(TE)))
    check("价格不可用：不误平", our_pos("ORPH1") is not None, str(our_pos("ORPH1")))
    PRICE["ORPH1"] = 100.0                               # 价格恢复 → 确认平仓
    aft.process(TE, make_snap(TE, 100000, []), PARAMS)
    check("价格恢复后孤儿被确认平掉",
          our_pos("ORPH1") is None, str(our_pos("ORPH1")))

    print("== 37. 上游脏数据：单条坏数据不能废掉整个交易员 ==")
    TF = "bn-FFF"
    aft.set_follow(TF, "F", "", True)
    aft._prev_snap.clear()
    aft.process(TF, make_snap(TF, 100000, []), PARAMS)      # 基线
    PRICE["DIRTY1"] = 50.0
    junk = [
        tp("DIRTY1USDT", "LONG", 10, 50, 50, lev=2),          # 正常的一条
        "not-a-dict",                                         # 元素不是对象
        {"symbol": "DIRTY2USDT", "side": "LONG", "absQty": "N/A",
         "avgPrice": 50, "markPrice": 50, "notional": 500, "leverage": 2},
        {"symbol": "DIRTY3USDT", "side": "LONG", "absQty": -5,
         "avgPrice": 50, "markPrice": 50, "notional": 500, "leverage": 2},
    ]
    s = aft.process(TF, make_snap(TF, 100000, junk), PARAMS)
    check("脏数据不抛异常，正常那条照开",
          len(s) == 1 and s[0]["action"] == "OPEN" and s[0]["executed"]
          and s[0]["symbol"] == "DIRTY1USDT",
          str([(x.get("action"), x.get("symbol")) for x in s]))
    check("字符串数量的持仓被跳过（不开仓）", our_pos("DIRTY2") is None,
          str(our_pos("DIRTY2")))
    check("负数量的持仓被跳过（不当成交易员持仓）", our_pos("DIRTY3") is None,
          str(our_pos("DIRTY3")))

    print("== 38. 上游换拼写（SIGMAUSDT / SIGMA）不得平仓重开 ==")
    TH = "bn-HHH"
    aft.set_follow(TH, "H", "", True)
    aft._prev_snap.clear()
    aft.process(TH, make_snap(TH, 100000, []), PARAMS)   # 首轮建基线
    PRICE["SIGMA"] = 100.0
    s = aft.process(TH, make_snap(TH, 100000,
                                  [tp("SIGMAUSDT", "LONG", 10, 100, 100)]), PARAMS)
    check("前置：已开仓", len(s) == 1 and s[0]["executed"], str(s))
    _pid = our_pos("SIGMA")["id"]
    # 同一笔持仓，上游改用不带 USDT 的写法（实测 7 天内 BTC/ZEC 都发生过）
    s2 = aft.process(TH, make_snap(TH, 100000,
                                   [tp("SIGMA", "LONG", 10, 100, 100)]), PARAMS)
    check("换拼写：不产生任何信号（不是平仓+重开）", s2 == [], str(s2))
    _p2 = our_pos("SIGMA")
    check("换拼写：原仓原样保留（同一 id）",
          _p2 is not None and _p2["id"] == _pid, str(_p2 and _p2["id"]))
    # 反向：先短写法、后长写法，同样不能换手
    s3 = aft.process(TH, make_snap(TH, 100000,
                                   [tp("SIGMAUSDT", "LONG", 10, 100, 100)]), PARAMS)
    check("换回长写法：同样无信号", s3 == [], str(s3))

    print("== 39. 并发平仓：读快照与写库之间被平掉 → 不许回写（会污染已实现盈亏） ==")
    TI = "bn-III"
    aft.set_follow(TI, "I", "", True)
    aft._prev_snap.clear()
    aft.process(TI, make_snap(TI, 100000, []), PARAMS)      # 基线
    PRICE["CONC1"] = 100.0
    aft.process(TI, make_snap(TI, 100000,
                              [tp("CONC1USDT", "LONG", 10, 100, 100)]), PARAMS)
    _p = our_pos("CONC1")
    check("前置：已开仓", _p is not None, str(_p))
    _real_mark = aft.get_mark_price

    def _mark_then_close(token):
        # 模拟并发写者（取消跟单/实盘页平仓）在取价窗口内把这行平掉
        with storage.get_conn() as c:
            c.execute("UPDATE trade_positions SET status='CLOSED', "
                      "closed_qty=quantity, realized_pnl=7.77 WHERE token='CONC1' "
                      "AND strategy='alphafox' AND status IN ('OPEN','PARTIAL')")
        return PRICE.get(token)

    aft.get_mark_price = _mark_then_close
    try:
        s = aft.process(TI, make_snap(TI, 100000,
                                      [tp("CONC1USDT", "LONG", 20, 100, 100)]), PARAMS)
    finally:
        aft.get_mark_price = _real_mark
    with storage.get_conn() as conn:
        row = dict(conn.execute("SELECT * FROM trade_positions WHERE id=?",
                                (_p["id"],)).fetchone())
    check("被并发平掉的行未被回写（realized 保持 7.77、仍是 CLOSED）",
          row["status"] == "CLOSED" and abs(float(row["realized_pnl"]) - 7.77) < 1e-9,
          str({k: row[k] for k in ("status", "quantity", "closed_qty", "realized_pnl")}))
    check("该信号标 executed=False", len(s) == 1 and not s[0]["executed"], str(s))

    print("== 40. 映射表不可用 → 跳过本轮且不推进基线（防静默丢退出信号） ==")
    TJ = "bn-JJJ"
    aft.set_follow(TJ, "J", "", True)
    aft._prev_snap.clear()
    aft.process(TJ, make_snap(TJ, 100000, []), PARAMS)      # 基线
    _snap0 = dict(aft._prev_snap.get(TJ) or {})
    _real_reload = aft.reload_symbol_map

    def _fail_reload():
        aft._map_cache = None          # 模拟 alphafox.db 读取失败（保持"未加载"）

    aft.reload_symbol_map = _fail_reload
    aft._map_cache = None          # 模拟"上次加载就失败过"的未加载状态
    try:
        s = aft.process(TJ, make_snap(TJ, 100000,
                                      [tp("SIGMA2USDT", "LONG", 10, 100, 100)]),
                        PARAMS)
    finally:
        aft.reload_symbol_map = _real_reload
        aft.reload_symbol_map()        # 恢复缓存
    check("映射表不可用：本轮不产信号", s == [], str(s))
    check("映射表不可用：基线未被推进（下轮重试）",
          aft._prev_snap.get(TJ) == _snap0, str(aft._prev_snap.get(TJ)))

    print("== 41. 托管状态机：取消跟单不平仓 / 非托管不跟信号 / 恢复托管追平 ==")
    TK = "bn-KKK"
    aft.set_follow(TK, "K", "", True)
    aft._prev_snap.clear()
    PRICE["HOST1"] = 100.0
    aft.process(TK, make_snap(TK, 100000, []), PARAMS)            # 基线
    aft.process(TK, make_snap(TK, 100000,
                              [tp("HOST1USDT", "LONG", 10, 100, 100)]), PARAMS)
    _h1 = our_pos("HOST1")
    check("41 前置：已开仓", _h1 is not None, str(_h1))
    res = aft.set_follow(TK, "K", "", False)                      # 取消跟单
    _h1b = our_pos("HOST1")
    check("41a 取消跟单：仓位保留（不平仓）", _h1b is not None, str(_h1b))
    check("41a 取消跟单：managed=0（取消托管）",
          _h1b is not None and int(_h1b["managed"]) == 0,
          str(_h1b and _h1b.get("managed")))
    check("41a 取消跟单：返回取消笔数", res.get("managed_off") == 1, str(res))

    TL = "bn-LLL"
    aft.set_follow(TL, "L", "", True)
    aft._prev_snap.clear()
    PRICE["HOST2"] = 100.0
    aft.process(TL, make_snap(TL, 100000, []), PARAMS)            # 基线
    aft.process(TL, make_snap(TL, 100000,
                              [tp("HOST2USDT", "LONG", 10, 100, 100)]), PARAMS)
    _h2 = our_pos("HOST2")
    _q0 = float(_h2["quantity"])
    r = aft.set_position_managed(_h2["id"], False)                # 单笔取消托管
    check("41b 单笔取消托管成功", r.get("ok") and r.get("managed") == 0, str(r))
    s = aft.process(TL, make_snap(TL, 100000,
                                  [tp("HOST2USDT", "LONG", 20, 100, 100)]), PARAMS)
    check("41b 未托管：交易员加仓不落地（ADD executed=False）",
          len(s) == 1 and s[0]["action"] == "ADD" and not s[0]["executed"], str(s))
    check("41b 未托管：纸盘数量未变",
          abs(float(our_pos("HOST2")["quantity"]) - _q0) < 1e-9)
    aft.process(TL, make_snap(TL, 100000, []), PARAMS)   # 快照异常第 1 轮（防抖动）
    s = aft.process(TL, make_snap(TL, 100000, []), PARAMS)  # 第 2 轮确认真实平仓
    check("41b 未托管：交易员平仓不落地（CLOSE executed=False）",
          len(s) == 1 and s[0]["action"] == "CLOSE" and not s[0]["executed"], str(s))
    check("41b 未托管：纸盘仓仍在（没被平掉）", our_pos("HOST2") is not None)

    r = aft.set_position_managed(_h2["id"], True)                 # 恢复托管
    check("41c 恢复托管：置恢复中(2)", r.get("ok") and r.get("managed") == 2, str(r))
    s = aft.process(TL, make_snap(TL, 100000,
                                  [tp("HOST2USDT", "LONG", 30, 100, 100)]), PARAMS)
    _h2b = our_pos("HOST2")
    check("41c 恢复托管：转托管中(1)",
          _h2b is not None and int(_h2b["managed"]) == 1,
          str(_h2b and _h2b.get("managed")))
    check("41c 恢复托管：追平到交易员当前仓（30×k）",
          _h2b is not None and abs(_h2b["quantity"] - 30 * K1) < 1e-9,
          str(_h2b and _h2b["quantity"]))

    TM = "bn-MMM"
    aft.set_follow(TM, "M", "", True)
    aft._prev_snap.clear()
    PRICE["HOST3"] = 100.0
    aft.process(TM, make_snap(TM, 100000, []), PARAMS)
    aft.process(TM, make_snap(TM, 100000,
                              [tp("HOST3USDT", "LONG", 10, 100, 100)]), PARAMS)
    _h3 = our_pos("HOST3")
    aft.set_position_managed(_h3["id"], False)                    # 取消托管
    aft.process(TM, make_snap(TM, 100000, []), PARAMS)            # 交易员平了（不落地）
    r = aft.set_position_managed(_h3["id"], True)
    check("41d 恢复托管：置恢复中", r.get("ok") and r.get("managed") == 2, str(r))
    aft.process(TM, make_snap(TM, 100000, []), PARAMS)   # 第 1 轮缺失：待确认
    _h3a = our_pos("HOST3")
    check("41d 单轮缺失不落已脱离（防单条脏快照误标）",
          _h3a is not None and int(_h3a["managed"]) == 2,
          str(_h3a and _h3a.get("managed")))
    s = aft.process(TM, make_snap(TM, 100000, []), PARAMS)   # 第 2 轮确认
    _h3b = our_pos("HOST3")
    check("41d 交易员已无此仓 → 已脱离(3)、不交易",
          _h3b is not None and int(_h3b["managed"]) == 3,
          str(_h3b and _h3b.get("managed")))
    check("41d 已脱离：仓位仍在", _h3b is not None)
    check("41d 已脱离：本轮无执行动作", all(not x.get("executed") for x in s), str(s))
    aft.process(TM, make_snap(TM, 100000,
                              [tp("HOST3USDT", "LONG", 50, 100, 100)]), PARAMS)
    _q_before = float(our_pos("HOST3")["quantity"])
    aft.process(TM, make_snap(TM, 100000,
                              [tp("HOST3USDT", "LONG", 60, 100, 100)]), PARAMS)
    check("41e 已脱离：交易员重开同币不新建第二笔", count_active("HOST3") == 1,
          str(count_active("HOST3")))
    check("41e 已脱离：交易员加减仓不作用到该仓",
          abs(float(our_pos("HOST3")["quantity"]) - _q_before) < 1e-9,
          str(our_pos("HOST3")["quantity"]))

    TN = "bn-NNN"
    aft.set_follow(TN, "N", "", True)
    aft._prev_snap.clear()
    PRICE["HOST4"] = 100.0
    aft.process(TN, make_snap(TN, 100000, []), PARAMS)
    aft.process(TN, make_snap(TN, 100000,
                              [tp("HOST4USDT", "LONG", 10, 100, 100)]), PARAMS)
    _h4 = our_pos("HOST4")
    aft.set_follow(TN, "N", "", False)
    r = aft.set_position_managed(_h4["id"], True)
    check("41f 交易员未跟单：拒绝恢复托管（无信号源）", not r.get("ok"), str(r))

    TO = "bn-OOO"
    aft.set_follow(TO, "O", "", True)
    aft._prev_snap.clear()
    PRICE["HOST5A"] = 100.0
    PRICE["HOST5B"] = 50.0
    aft.process(TO, make_snap(TO, 100000, []), PARAMS)
    aft.process(TO, make_snap(TO, 100000,
                              [tp("HOST5AUSDT", "LONG", 10, 100, 100),
                               tp("HOST5BUSDT", "LONG", 20, 50, 50)]), PARAMS)
    r1 = aft.set_follow(TO, "O", "", False)
    check("41g 批量取消跟单：2 笔取消托管", r1.get("managed_off") == 2, str(r1))
    check("41g 批量取消：两笔仓位都在",
          our_pos("HOST5A") is not None and our_pos("HOST5B") is not None)
    r2 = aft.set_follow(TO, "O", "", True)
    check("41g 批量重新跟单：2 笔进恢复中", r2.get("reattach") == 2, str(r2))
    s = aft.process(TO, make_snap(TO, 100000,
                                  [tp("HOST5AUSDT", "LONG", 5, 100, 100),
                                   tp("HOST5BUSDT", "LONG", 10, 50, 50)]), PARAMS)
    _a, _b = our_pos("HOST5A"), our_pos("HOST5B")
    check("41g 追平 A（期间交易员减半 → 5×k）",
          abs(open_qty("HOST5A") - 5 * K1) < 1e-9, str(open_qty("HOST5A")))
    check("41g 追平 B（10×k）",
          abs(open_qty("HOST5B") - 10 * K1) < 1e-9, str(open_qty("HOST5B")))
    check("41g 全部转托管中",
          int(_a["managed"]) == 1 and int(_b["managed"]) == 1)

    TP_ = "bn-PPP"
    aft.set_follow(TP_, "P", "", True)
    aft._prev_snap.clear()
    PRICE["HOST6"] = 100.0
    aft.process(TP_, make_snap(TP_, 100000, []), PARAMS)
    aft.process(TP_, make_snap(TP_, 100000,
                               [tp("HOST6USDT", "LONG", 10, 100, 100)]), PARAMS)
    aft.set_follow(TP_, "P", "", False)                       # 取消跟单
    aft.set_follow(TP_, "P", "", True)                        # 重新跟单（交易员已无该仓）
    s = aft.process(TP_, make_snap(TP_, 100000, []), PARAMS)   # 首轮：缺失第 1 次
    s2 = aft.process(TP_, make_snap(TP_, 100000, []), PARAMS)  # 次轮：确认缺失
    _h6 = our_pos("HOST6")
    check("41h 重新跟单：交易员已平的仓 → 已脱离（不交易）",
          _h6 is not None and int(_h6["managed"]) == 3,
          str(_h6 and _h6.get("managed")))
    s3 = aft.process(TP_, make_snap(TP_, 100000, []), PARAMS)  # 第三轮：孤儿确认窗口
    check("41h 孤儿确认不得平掉已脱离仓（决策1：只恢复托管不交易）",
          our_pos("HOST6") is not None, str(our_pos("HOST6")))
    check("41h 三轮均无落地动作",
          all(not x.get("executed") for x in (s + s2 + s3)), str(s + s2 + s3))

    print("== 44. 非托管仓 + 上游无价：守卫要挡在取价之前（否则整轮 retry 拖住基线） ==")
    TS_ = "bn-SSS"
    aft.set_follow(TS_, "S", "", True)
    aft._prev_snap.clear()
    PRICE["TOK2"] = 100.0
    aft.process(TS_, make_snap(TS_, 100000, []), PARAMS)
    aft.process(TS_, make_snap(TS_, 100000,
                               [tp("TOK2USDT", "LONG", 10, 100, 100)]), PARAMS)
    _p44 = our_pos("TOK2")
    r = aft.set_position_managed(_p44["id"], False)
    check("44 前置：已取消托管", r.get("ok"), str(r))
    PRICE.pop("TOK2")                       # 币安行情不可用
    s = aft.process(TS_, make_snap(TS_, 100000,
                                   [tp("TOK2USDT", "LONG", 20, 0, 0)]), PARAMS)
    check("44 非托管 + 无价：不产生 retry（守卫在取价之前返回）",
          bool(s) and not any(x.get("retry") for x in s), str(s))
    check("44 基线正常推进（下轮不重复发同一信号）",
          (aft._prev_snap.get(TS_) or {}).get(("TOK2USDT", "LONG"), {})
          .get("qty") == 20, str(aft._prev_snap.get(TS_)))
    aft.process(TS_, make_snap(TS_, 100000, []), PARAMS)   # 异常第 1 轮
    s = aft.process(TS_, make_snap(TS_, 100000, []), PARAMS)  # 第 2 轮：进入平仓
    check("44 交易员平仓（快照也无价）：同样不 retry、仓仍保留",
          not any(x.get("retry") for x in s) and our_pos("TOK2") is not None,
          str(s))

    print("== 45. 托管状态一变，'缺失计数'必须归零（否则正常启停后就单轮落已脱离） ==")
    TT_ = "bn-TTT"
    aft.set_follow(TT_, "T", "", True)
    aft._prev_snap.clear()
    PRICE["TOK3"] = 100.0
    PRICE["TOK4"] = 200.0
    aft.process(TT_, make_snap(TT_, 100000, []), PARAMS)
    aft.process(TT_, make_snap(TT_, 100000,
                               [tp("TOK3USDT", "LONG", 10, 100, 100),
                                tp("TOK4USDT", "LONG", 5, 200, 200)]), PARAMS)
    _p45 = our_pos("TOK3")
    aft.set_position_managed(_p45["id"], False)
    aft.set_position_managed(_p45["id"], True)
    # 缺 TOK3 的一轮（另一个还在 → 不触发异常闸门）
    aft.process(TT_, make_snap(TT_, 100000,
                               [tp("TOK4USDT", "LONG", 5, 200, 200)]), PARAMS)
    check("45 单轮缺失：停在恢复中(2)",
          int(our_pos("TOK3")["managed"]) == 2,
          str(our_pos("TOK3")["managed"]))
    aft.set_follow(TT_, "T", "", False)     # 用户取消跟单 → 重新跟单
    aft.set_follow(TT_, "T", "", True)
    aft.process(TT_, make_snap(TT_, 100000,
                               [tp("TOK4USDT", "LONG", 5, 200, 200)]), PARAMS)
    check("45 启停后第一轮缺失：不得立刻落已脱离（计数已归零）",
          int(our_pos("TOK3")["managed"]) == 2,
          str(our_pos("TOK3")["managed"]))

    print("== 46. 恢复托管与取消跟单竞态：不得停在'恢复中'死状态 ==")
    TU_ = "bn-UUU"
    aft.set_follow(TU_, "U", "", True)
    aft._prev_snap.clear()
    PRICE["TOK5"] = 100.0
    aft.process(TU_, make_snap(TU_, 100000, []), PARAMS)
    aft.process(TU_, make_snap(TU_, 100000,
                               [tp("TOK5USDT", "LONG", 10, 100, 100)]), PARAMS)
    _p46 = our_pos("TOK5")
    _real_en46 = aft._is_enabled
    _st = {"n": 0}

    def _flaky_enabled(tid):
        _st["n"] += 1
        return _st["n"] == 1     # 前置校验通过，写库后的复查发现交易员已被停用

    aft._is_enabled = _flaky_enabled
    try:
        r = aft.set_position_managed(_p46["id"], True)
    finally:
        aft._is_enabled = _real_en46
    check("46 竞态：拒绝恢复且保持非托管（不留死状态）",
          (not r.get("ok")) and int(our_pos("TOK5")["managed"]) == 0,
          str((r, our_pos("TOK5")["managed"])))

    print("== 47. 孤儿确认遇非价格异常（如 DB 写锁）也必须保住候选 ==")
    TV_ = "bn-VVV"
    aft.set_follow(TV_, "V", "", True)
    aft._prev_snap.clear()
    PRICE["TOK6"] = 100.0
    aft.process(TV_, make_snap(TV_, 100000, []), PARAMS)
    aft.process(TV_, make_snap(TV_, 100000,
                               [tp("TOK6USDT", "LONG", 10, 100, 100)]), PARAMS)
    check("47 前置：已开仓", our_pos("TOK6") is not None)
    aft._prev_snap.clear()                      # 模拟重启（内存基线清空）
    aft.process(TV_, make_snap(TV_, 100000, []), PARAMS)     # 首轮：记候选
    check("47 首轮：记录孤儿候选",
          aft._orphan_pending.get(TV_) == {("TOK6USDT", "LONG")},
          str(aft._orphan_pending.get(TV_)))
    _real_confirm = aft._confirm_orphans

    def _boom(*a, **k):
        raise RuntimeError("database is locked")    # 非 _DataUnavailable

    aft._confirm_orphans = _boom
    try:
        aft.process(TV_, make_snap(TV_, 100000, []), PARAMS)
    finally:
        aft._confirm_orphans = _real_confirm
    check("47 非价格异常：候选保住（下轮还会试，不会永久丢退出信号）",
          aft._orphan_pending.get(TV_) == {("TOK6USDT", "LONG")},
          str(aft._orphan_pending.get(TV_)))

    print("== 48. marginBalance 脏值：不得废掉整轮（交易员平仓信号仍要处理） ==")
    TW_ = "bn-WWW"
    aft.set_follow(TW_, "W", "", True)
    aft._prev_snap.clear()
    PRICE["TOK7"] = 100.0
    aft.process(TW_, make_snap(TW_, 100000, []), PARAMS)
    aft.process(TW_, make_snap(TW_, 100000,
                               [tp("TOK7USDT", "LONG", 10, 100, 100)]), PARAMS)
    check("48 前置：已开仓", our_pos("TOK7") is not None)
    _snap48 = make_snap(TW_, "1,000.50", [])    # 脏 marginBalance + 交易员平仓
    aft.process(TW_, _snap48, PARAMS)           # 异常闸门第 1 轮
    s = aft.process(TW_, _snap48, PARAMS)       # 第 2 轮：应真平掉
    check("48 脏值不废整轮：平仓信号仍执行",
          len(s) == 1 and s[0]["action"] == "CLOSE" and s[0]["executed"], str(s))
    check("48 平仓已落地", our_pos("TOK7") is None)

    print("== 49. 重新跟单（批量）与取消跟单竞态：也不得停在恢复中 ==")
    TX_ = "bn-XXX"
    aft.set_follow(TX_, "X", "", True)
    aft._prev_snap.clear()
    PRICE["TOK8"] = 100.0
    aft.process(TX_, make_snap(TX_, 100000, []), PARAMS)
    aft.process(TX_, make_snap(TX_, 100000,
                               [tp("TOK8USDT", "LONG", 10, 100, 100)]), PARAMS)
    aft.set_follow(TX_, "X", "", False)          # 取消跟单 → 非托管
    _real_en49 = aft._is_enabled
    aft._is_enabled = lambda tid: False          # 批量置 2 之后复查：已被停用
    try:
        aft.set_follow(TX_, "X", "", True)
    finally:
        aft._is_enabled = _real_en49
    check("49 批量竞态：退回非托管（不留'恢复中'死状态）",
          int(our_pos("TOK8")["managed"]) == 0, str(our_pos("TOK8")["managed"]))

    print("== 50. 快照降级（行缺字段/解析失败）→ 不许当成'交易员已平仓' ==")
    TY_ = "bn-YYY"
    aft.set_follow(TY_, "Y", "", True)
    aft._prev_snap.clear()
    PRICE["TOK9"] = 100.0
    PRICE["TOK10"] = 50.0
    aft.process(TY_, make_snap(TY_, 100000, []), PARAMS)
    aft.process(TY_, make_snap(TY_, 100000,
                               [tp("TOK9USDT", "LONG", 10, 100, 100),
                                tp("TOK10USDT", "LONG", 4, 50, 50)]), PARAMS)
    check("50 前置：两笔已开仓",
          our_pos("TOK9") is not None and our_pos("TOK10") is not None)
    _prev50 = dict(aft._prev_snap.get(TY_) or {})
    # 上游某行 symbol 缺失（不是平仓！）
    s = aft.process(TY_, make_snap(TY_, 100000,
                                   [{"symbol": None, "side": "LONG", "absQty": 4,
                                     "avgPrice": 50, "markPrice": 50,
                                     "notional": 200, "leverage": 2},
                                    tp("TOK9USDT", "LONG", 10, 100, 100)]), PARAMS)
    check("50 缺 symbol 的行：本笔不许发 CLOSE（实盘会被市价平掉）",
          not any(x.get("action") == "CLOSE" and x.get("executed") for x in s),
          str(s))
    check("50 两笔仓都还在",
          our_pos("TOK9") is not None and our_pos("TOK10") is not None)
    check("50 基线不推进（下轮重试）", aft._prev_snap.get(TY_) == _prev50,
          str(aft._prev_snap.get(TY_)))
    # absQty 缺失但该 key 有历史 → 沿用上轮数量（不产生动作）
    s = aft.process(TY_, make_snap(TY_, 100000,
                                   [{"symbol": "TOK10USDT", "side": "LONG",
                                     "absQty": None, "avgPrice": 50,
                                     "markPrice": 50, "leverage": 2},
                                    tp("TOK9USDT", "LONG", 10, 100, 100)]), PARAMS)
    check("50 缺 absQty 的行：沿用上轮数量、无减仓动作",
          our_pos("TOK10") is not None
          and abs(open_qty("TOK10") - 4 * K1) < 1e-9, str(s))
    check("50 缺 absQty：不产 CLOSE", not any(x.get("action") == "CLOSE" for x in s),
          str(s))

    print("== 51. 对冲交易员（同币多空）：跟单方向必须确定（不能随重启变） ==")
    TZ_ = "bn-ZZZ"
    aft.set_follow(TZ_, "Z", "", True)
    aft._prev_snap.clear()
    PRICE["HEDGE1"] = 100.0
    aft.process(TZ_, make_snap(TZ_, 100000, []), PARAMS)
    s1 = aft.process(TZ_, make_snap(TZ_, 100000,
                                    [tp("HEDGE1USDT", "SHORT", 3, 100, 100, lev=2),
                                     tp("HEDGE1USDT", "LONG", 5, 100, 100, lev=2)]),
                     PARAMS)
    _d1 = our_pos("HEDGE1")["side"] if our_pos("HEDGE1") else None
    aft._prev_snap.clear()                       # 模拟重启后重来一次
    aft.process(TZ_, make_snap(TZ_, 100000, []), PARAMS)
    s2 = aft.process(TZ_, make_snap(TZ_, 100000,
                                    [tp("HEDGE1USDT", "SHORT", 3, 100, 100, lev=2),
                                     tp("HEDGE1USDT", "LONG", 5, 100, 100, lev=2)]),
                     PARAMS)
    check("51 对冲方向确定（两次顺序一致、只开一侧）",
          _d1 is not None and _d1 == "LONG", str((_d1, s1, s2)))

    print("== 52. 孤儿确认平仓：必须产出信号（原来完全静默、无 TG） ==")
    T1B = "bn-1111"
    aft.set_follow(T1B, "1B", "", True)
    aft._prev_snap.clear()
    PRICE["ORPH1"] = 100.0
    aft.process(T1B, make_snap(T1B, 100000, []), PARAMS)
    aft.process(T1B, make_snap(T1B, 100000,
                               [tp("ORPH1USDT", "LONG", 10, 100, 100)]), PARAMS)
    check("52 前置：已开仓", our_pos("ORPH1") is not None)
    aft._prev_snap.clear()                       # 重启：内存基线清空
    aft.process(T1B, make_snap(T1B, 100000, []), PARAMS)   # 首轮记候选
    s = aft.process(T1B, make_snap(T1B, 100000, []), PARAMS)  # 次轮确认平仓
    check("52 孤儿平仓产出 CLOSE 信号（executed=True，可推 TG）",
          len(s) == 1 and s[0]["action"] == "CLOSE" and s[0]["executed"], str(s))
    check("52 仓位已平", our_pos("ORPH1") is None)

    print("== 53. retry 轮：已执行成功的信号不许被标 retry（否则 TG 永久漏报） ==")
    T1C = "bn-2222"
    aft.set_follow(T1C, "1C", "", True)
    aft._prev_snap.clear()
    PRICE["RETRY1"] = 100.0
    PERPS.add("RETRY2")                # 有交易对但拿不到价格 → 开仓抛 _DataUnavailable
    aft.process(T1C, make_snap(T1C, 100000, []), PARAMS)     # 基线
    # 同一轮：RETRY1 正常开成（executed=True）、RETRY2 价格不可用（retry=True）
    s = aft.process(T1C, make_snap(T1C, 100000,
                                   [tp("RETRY1USDT", "LONG", 10, 100, 100),
                                    tp("RETRY2USDT", "LONG", 4, 50, 50)]), PARAMS)
    _exec = [x for x in s if x.get("executed")]
    _fail = [x for x in s if not x.get("executed")]
    check("53 前置：本轮有真执行成的信号", bool(_exec), str(s))
    check("53 前置：本轮有失败待重试的信号", bool(_fail) and _fail[0].get("retry"),
          str(s))
    check("53 已执行的信号不带 retry（能被 TG 推送）",
          all(not x.get("retry") for x in _exec), str(s))
    check("53 未执行的信号仍标 retry（防幽灵推送）",
          all(x.get("retry") for x in _fail), str(s))

    print("== 54. 恢复托管缺 k_qty：标'已脱离'而不是假装托管中 ==")
    T1D = "bn-3333"
    aft.set_follow(T1D, "1D", "", True)
    aft._prev_snap.clear()
    PRICE["NOKQ1"] = 100.0
    with storage.get_conn() as conn:   # 模拟历史/外部写入：没有 k_qty 的行
        conn.execute(
            "INSERT INTO trade_positions (token, symbol, side, status, mode, "
            "margin_amount, leverage, notional, quantity, entry_price, "
            "current_price, strategy, closed_qty, managed, signal_snapshot) "
            "VALUES ('NOKQ1','NOKQ1USDT','LONG','OPEN','paper',50,3,150,1.0,"
            "100,100,'alphafox',0,2,?)",
            (json.dumps({"trader_id": T1D}),))
    s = aft.process(T1D, make_snap(T1D, 100000,
                                   [tp("NOKQ1USDT", "LONG", 10, 100, 100)]), PARAMS)
    _nq = our_pos("NOKQ1")
    check("54 缺 k_qty：标已脱离（不再假装在跟）",
          _nq is not None and int(_nq["managed"]) == 3,
          str(_nq and _nq.get("managed")))

    print("== 42. 存量行 symbol 拼写不同（上游原始写法）→ 首轮不得误判孤儿平仓 ==")
    TQ = "bn-QQQ"
    aft.set_follow(TQ, "Q", "", True)
    aft._prev_snap.clear()
    PRICE["OLDSP"] = 100.0
    PRICE["SKHYNIX"] = 200.0
    with storage.get_conn() as conn:      # 旧版代码写的行：symbol=上游原始拼写
        for _tok, _sym in (("OLDSP", "OLDSP"), ("SKHYNIX", "XYZ:SKHX")):
            conn.execute(
                "INSERT INTO trade_positions (token, symbol, side, status, mode, "
                "margin_amount, leverage, notional, quantity, entry_price, "
                "current_price, strategy, closed_qty, managed, signal_snapshot) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (_tok, _sym, "LONG", "OPEN", "paper", 50.0, 3.0, 150.0, 1.0,
                 100.0, 100.0, "alphafox", 0.0, 1,
                 json.dumps({"trader_id": TQ, "k_qty": 0.01})))
    _snap42 = make_snap(TQ, 100000, [tp("OLDSPUSDT", "LONG", 100, 100, 100),
                                     tp("SKHYNIXUSDT", "LONG", 100, 200, 200)])
    s1 = aft.process(TQ, _snap42, PARAMS)
    check("42 首轮：拼写不同的存量仓不被当孤儿",
          aft._orphan_pending.get(TQ) == set(), str(aft._orphan_pending.get(TQ)))
    s2 = aft.process(TQ, _snap42, PARAMS)
    check("42 次轮：两笔仓仍在（未被孤儿平仓）",
          our_pos("OLDSP") is not None and our_pos("SKHYNIX") is not None,
          str((our_pos("OLDSP"), our_pos("SKHYNIX"))))
    check("42 两轮无落地动作",
          all(not x.get("executed") for x in (s1 + s2)), str(s1 + s2))

    print("== 43. 取价窗口内被取消托管 → 平仓/加减仓不得落库（TOCTOU） ==")
    TR_ = "bn-RRR"
    aft.set_follow(TR_, "R", "", True)
    aft._prev_snap.clear()
    PRICE["TOK1"] = 100.0
    aft.process(TR_, make_snap(TR_, 100000, []), PARAMS)     # 基线
    aft.process(TR_, make_snap(TR_, 100000,
                               [tp("TOK1USDT", "LONG", 10, 100, 100)]), PARAMS)
    _p43 = our_pos("TOK1")
    check("43 前置：已开仓", _p43 is not None, str(_p43))
    _real_mark43 = aft.get_mark_price

    def _mark_then_unmanage(token):
        # 模拟用户在这段网络调用窗口里点了"取消托管"
        aft.set_position_managed(_p43["id"], False)
        return PRICE.get(token)

    aft.get_mark_price = _mark_then_unmanage
    try:
        aft.process(TR_, make_snap(TR_, 100000, []), PARAMS)   # 快照异常第 1 轮
        aft.process(TR_, make_snap(TR_, 100000, []), PARAMS)   # 第 2 轮：进入平仓
    finally:
        aft.get_mark_price = _real_mark43
    _p43b = our_pos("TOK1")
    check("43 取消托管期间到达的平仓信号被拦（仓仍 OPEN）", _p43b is not None,
          str(_p43b))
    check("43 该仓已是非托管", _p43b and int(_p43b["managed"]) == 0,
          str(_p43b and _p43b.get("managed")))
    with storage.get_conn() as conn:      # 恢复托管 + 重建基线（交易员仍持有 10）
        conn.execute("UPDATE trade_positions SET managed=1 WHERE id=?",
                     (_p43["id"],))
    aft.process(TR_, make_snap(TR_, 100000,
                               [tp("TOK1USDT", "LONG", 10, 100, 100)]), PARAMS)
    aft.get_mark_price = _mark_then_unmanage
    try:
        aft.process(TR_, make_snap(TR_, 100000,
                                   [tp("TOK1USDT", "LONG", 30, 100, 100)]), PARAMS)
    finally:
        aft.get_mark_price = _real_mark43
    check("43 取消托管期间到达的加仓信号也被拦（数量未变）",
          abs(float(our_pos("TOK1")["quantity"])
              - float(_p43["quantity"])) < 1e-9,
          str(our_pos("TOK1")["quantity"]))
    check("43 加仓窗口被取消后转非托管",
          int(our_pos("TOK1")["managed"]) == 0,
          str(our_pos("TOK1")["managed"]))

    print("== 55. 一次净变化压着多笔加仓：按流水还原真实笔数（BUG2）==")
    # 探测间隔（15s）内交易员连着加 3 笔 → 我们只看到一次净变化。
    # 平台流水 latestOrders 每笔带 canonicalAction → 应补记成 3 条 ADD 信号
    # （实盘每笔各加一份、页面/日志的次数才与交易员一致）
    T9 = "bn-9001"
    PRICE["TOKX"] = 1.0
    aft.process(T9, make_snap(T9, 100000, []), PARAMS)   # 空快照建基线
    O1 = mk_order("TOKXUSDT", "OPEN", 100, 1.0, 1000)
    s = aft.process(T9, make_snap(T9, 100000,
                                  [tp("TOKXUSDT", "LONG", 100, 1.0, 1.0)],
                                  orders=[O1]), PARAMS)
    check("55-前置 开仓信号 1 条", len(s) == 1 and s[0]["action"] == "OPEN", str(s))
    p0 = our_pos("TOKX")
    check("55-前置 已建仓", p0 is not None, str(p0))
    _k9 = float(p0["quantity"]) / 100 if p0 else 0.0   # 该交易员的 k_qty

    A1 = mk_order("TOKXUSDT", "ADD", 20, 1.0, 2000)
    A2 = mk_order("TOKXUSDT", "ADD", 20, 1.0, 2100)
    A3 = mk_order("TOKXUSDT", "ADD", 20, 1.0, 2200)
    s = aft.process(T9, make_snap(T9, 100000,
                                  [tp("TOKXUSDT", "LONG", 160, 1.0, 1.0)],
                                  orders=[O1, A1, A2, A3]), PARAMS)
    adds = [x for x in s if x["action"] == "ADD"]
    check("55-a 一次净变化 → 还原成 3 条 ADD 信号",
          len(adds) == 3 and all(x["executed"] for x in adds), str(s))
    check("55-b 纸盘仍然只按净额对齐一次（数量 = 交易员总量×k_qty）",
          abs(open_qty("TOKX") - 160 * _k9) < 1e-9, str(open_qty("TOKX")))

    s = aft.process(T9, make_snap(T9, 100000,
                                  [tp("TOKXUSDT", "LONG", 160, 1.0, 1.0)],
                                  orders=[O1, A1, A2, A3]), PARAMS)
    check("55-c 同一批流水重复轮询：不再重复计数（去重生效）", s == [], str(s))

    A4 = mk_order("TOKXUSDT", "ADD", 40, 1.0, 2300)
    s = aft.process(T9, make_snap(T9, 100000,
                                  [tp("TOKXUSDT", "LONG", 200, 1.0, 1.0)],
                                  orders=[O1, A1, A2, A3, A4]), PARAMS)
    check("55-d 只补新增的那 1 笔（旧的 3 笔不重复）",
          len(s) == 1 and s[0]["action"] == "ADD", str(s))
    check("55-e 日志笔数 = 交易员笔数（含首轮开仓共 5 条写入 alphafox_signals）",
          _sig_count(T9) == 5, str(_sig_count(T9)))

    print("== 56. 流水水位每轮推进：历史单不许在下一次真加仓时重复计数 ==")
    A_OLD = mk_order("TOKXUSDT", "ADD", 5, 1.0, 1500)      # 老单（跟单前就有）
    s = aft.process(T9, make_snap(T9, 100000,
                                  [tp("TOKXUSDT", "LONG", 200, 1.0, 1.0)],
                                  orders=[O1, A_OLD, A1, A2, A3, A4]), PARAMS)
    check("56-a 净额不变的轮次：不产生信号（老单被水位吞掉）", s == [], str(s))
    A5 = mk_order("TOKXUSDT", "ADD", 40, 1.0, 2400)
    s = aft.process(T9, make_snap(T9, 100000,
                                  [tp("TOKXUSDT", "LONG", 240, 1.0, 1.0)],
                                  orders=[O1, A1, A2, A3, A4, A5, A_OLD]), PARAMS)
    check("56-b 下一次真加仓只算 1 笔（老单不重复计数）",
          len(s) == 1 and s[0]["action"] == "ADD", str(s))
    check("56-c 纸盘仍按净额对齐到 240", abs(open_qty("TOKX") - 240 * _k9) < 1e-9,
          str(open_qty("TOKX")))

    print("== 57. 真实数据形态：平台词表 / symbol 拼写 / 重启首轮水位 ==")
    T10 = "bn-9002"
    PRICE["TOKZ"] = 2.0
    # 基线轮：平台每轮都推全量流水（真样本 40+ 条历史），这里也给一条历史单 ——
    # 不给的话水位一直是空的，下一轮"首次见到流水"护栏会把真实的开仓几笔也吞掉
    _hist = mk_order("TOKZUSDT", "ADD", 7, 2.0, 500)
    aft.process(T10, make_snap(T10, 100000, [], orders=[_hist]), PARAMS)
    # ① 一次开仓拆成 3 笔成交（真样本：ETH 3 分钟 5 笔 open_long），
    #    且 symbol 拼写混用（真样本 BTC / ETHUSDT / XYZ:SKHX 三种都出现过）
    B1 = mk_order("TOKZUSDT", "OPEN", 100, 2.0, 3000)
    B2 = mk_order("TOKZ", "OPEN", 60, 2.0, 3100)              # 不带 USDT 后缀
    B3 = mk_order("TOKZUSDT", "OPEN", 40, 2.0, 3200,
                  canon="increase_long")                      # 平台另一种增加词
    s = aft.process(T10, make_snap(T10, 100000,
                                   [tp("TOKZUSDT", "LONG", 200, 2.0, 2.0)],
                                   orders=[B1, B2, B3]), PARAMS)
    check("57-a 开仓 3 笔成交 → 1 条 OPEN + 2 条 ADD（镜像层能开满 3 份）",
          [x["action"] for x in s] == ["OPEN", "ADD", "ADD"], str(s))
    check("57-b 补记信号都算执行成功（否则镜像层不动）",
          all(x["executed"] for x in s), str(s))
    check("57-c 日志写入 = 3 条（页面/统计口径与交易员笔数一致）",
          _sig_count(T10) == 3, str(_sig_count(T10)))

    # ③ 重启（基线 + 内存水位都清空）→ 首轮只建基线，不许把历史流水算成新单
    aft._prev_snap.pop(T10, None)
    aft._seen_orders.pop(T10, None)
    s = aft.process(T10, make_snap(T10, 100000,
                                   [tp("TOKZUSDT", "LONG", 200, 2.0, 2.0)],
                                   orders=[B1, B2, B3]), PARAMS)
    check("57-d 重启首轮：只建基线，不出信号", s == [], str(s))
    B4 = mk_order("TOKZUSDT", "ADD", 50, 2.0, 3300)
    s = aft.process(T10, make_snap(T10, 100000,
                                   [tp("TOKZUSDT", "LONG", 250, 2.0, 2.0)],
                                   orders=[B1, B2, B3, B4]), PARAMS)
    check("57-e 重启后第一次真加仓只算 1 笔（历史 3 笔不被当新单）",
          len(s) == 1 and s[0]["action"] == "ADD", str(s))

    # ④ 脏流水算不出内容键 → 不许计数：否则同一笔每轮都被当"新单"→ 无限加份。
    #    先用一条正常流水把水位建起来（模拟"前几轮已经看过这个交易员的流水"）
    _seed = mk_order("TOKZUSDT", "ADD", 7, 2.0, 3400)
    aft._seen_orders_advance("bn-9003", {"latestOrders": [_seed]})
    _dirty = {"symbol": "TOKZUSDT", "canonicalAction": "increase_long",
              "side": "BUY", "executedQty": "abc", "avgPrice": 2.0,
              "time": 3410, "realizedPnl": 0.0}
    _good = mk_order("TOKZUSDT", "ADD", 10, 2.0, 3500)
    check("57-f 脏流水不计入笔数（宁可少算一笔，也不能算成每轮都是新单）",
          aft._new_action_orders("bn-9003", "TOKZUSDT", "ADD",
                                 {"latestOrders": [_seed, _dirty, _good]}) == 1)
    # ⑤ 从没见过的流水（前几轮 payload 没带 latestOrders）→ 全量历史不算新单
    aft._seen_orders.pop("bn-9004", None)
    check("57-g 首次见到流水时不算新单（防把几小时历史当本轮新增）",
          aft._new_action_orders("bn-9004", "TOKZUSDT", "ADD",
                                 {"latestOrders": [_good, _dirty]}) == 0)

    print("== 59. 恢复托管：实盘侧未就绪 → 留在恢复中，不许转托管中 ==")
    import alphafox_live as _afl59
    TQ = "bn-QQQ"
    aft.set_follow(TQ, "Q", "", True)
    aft._prev_snap.clear()
    PRICE["HOST7"] = 100.0
    aft.process(TQ, make_snap(TQ, 100000, []), PARAMS)
    aft.process(TQ, make_snap(TQ, 100000,
                              [tp("HOST7USDT", "LONG", 10, 100, 100)]), PARAMS)
    _h7 = our_pos("HOST7")
    aft.set_position_managed(_h7["id"], False)
    r = aft.set_position_managed(_h7["id"], True)
    check("59 前置：置恢复中(2)", r.get("ok") and r.get("managed") == 2, str(r))
    _real_reattach = _afl59.on_reattach
    _afl59.on_reattach = lambda *a, **k: False        # 交易所查询不可用
    aft.process(TQ, make_snap(TQ, 100000,
                              [tp("HOST7USDT", "LONG", 30, 100, 100)]), PARAMS)
    check("59 实盘侧未就绪：仍是恢复中(2)，不转托管中",
          int(our_pos("HOST7")["managed"]) == 2,
          str(our_pos("HOST7")["managed"]))
    check("59 未就绪时纸盘照追平（追平是纸盘层的事，不动钱）",
          abs(open_qty("HOST7") - 30 * K1) < 1e-9, str(open_qty("HOST7")))
    _afl59.on_reattach = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("boom"))
    aft.process(TQ, make_snap(TQ, 100000,
                              [tp("HOST7USDT", "LONG", 30, 100, 100)]), PARAMS)
    check("59 实盘侧抛异常：也不许静默落定（仍是恢复中 2）",
          int(our_pos("HOST7")["managed"]) == 2,
          str(our_pos("HOST7")["managed"]))
    _afl59.on_reattach = lambda *a, **k: True         # 接口恢复
    aft.process(TQ, make_snap(TQ, 100000,
                              [tp("HOST7USDT", "LONG", 30, 100, 100)]), PARAMS)
    check("59 实盘侧就绪：才转托管中(1)",
          int(our_pos("HOST7")["managed"]) == 1,
          str(our_pos("HOST7")["managed"]))
    _afl59.on_reattach = _real_reattach

    print("== 58. ⚡ 快速跟随（纸盘编排；实盘侧闸门在 test_alphafox_live 覆盖） ==")
    import alphafox_live as _afl
    # 桩掉实盘层：这里只验纸盘编排（真跑会去碰交易所接口，单元测试里必须隔离）
    _qf_calls, _sync_calls = [], []
    _live_res = {"ok": True, "action": "新建"}
    _afl.quick_follow_live = (
        lambda token, side, qty, max_notional=0.0:
        (_qf_calls.append((token, side, qty)) or dict(_live_res)))
    _afl.sync_alphafox_live = lambda *a, **k: _sync_calls.append(a)
    _afl.on_reattach = lambda *a, **k: True
    TQF = "bn-QF"
    aft.set_follow(TQF, "QF", "", True)
    _LEV_QF = 3
    _TOK1, _TOK2 = "QFL1", "QFL2"
    PRICE[_TOK1], PRICE[_TOK2] = 100.0, 50.0
    PERPS.update({_TOK1, _TOK2})

    r = aft.quick_follow("bn-NOPE", make_snap("x", 100000, []), PARAMS)
    check("未跟单 → 拒绝（先点跟单）", r["ok"] is False and "跟单" in r["msg"],
          str(r))

    r = aft.quick_follow(TQF, make_snap(TQF, 100000, []), PARAMS)
    check("空仓交易员 → 无动作", r["ok"] is True and r["results"] == [], str(r))

    _snap_qf = make_snap(TQF, 100000,
                         [tp(f"{_TOK1}USDT", "LONG", 20, 100.0, 100.0, lev=_LEV_QF),
                          tp(f"{_TOK2}USDT", "SHORT", 40, 50.0, 50.0, lev=_LEV_QF)])
    _calls0, _sync0 = len(_qf_calls), len(_sync_calls)
    r = aft.quick_follow(TQF, _snap_qf, PARAMS, dry_run=True)
    check("dry_run：两个币都返回预览动作（新建）",
          r["ok"] and [x["action"] for x in r["results"]] == ["新建", "新建"],
          str(r))
    check("dry_run 不写库（无纸盘仓）",
          count_active(_TOK1) == 0 and count_active(_TOK2) == 0)
    check("dry_run 不碰实盘（没调 quick_follow_live/对账）",
          len(_qf_calls) == _calls0 and len(_sync_calls) == _sync0,
          f"{_qf_calls[_calls0:]} {_sync_calls[_sync0:]}")

    _sync0 = len(_sync_calls)
    r = aft.quick_follow(TQF, _snap_qf, PARAMS)
    check("真跑：两个币都新建纸盘仓",
          count_active(_TOK1) == 1 and count_active(_TOK2) == 1, str(r))
    p1 = our_pos(_TOK1)
    check("纸盘量 = 交易员量 × k_qty（预算 1000 ÷ 保证金 100000 = 0.01）",
          abs(open_qty(_TOK1) - 20 * 0.01) < 1e-9, str(p1 and p1["quantity"]))
    check("纸盘仓置为托管中", int(p1["managed"]) == 1, str(p1 and p1["managed"]))
    check("实盘侧收到 (token, side, 交易员量)",
          _qf_calls[_calls0:] == [(_TOK1, "LONG", 20.0), (_TOK2, "SHORT", 40.0)],
          str(_qf_calls[_calls0:]))
    check("结尾触发了一轮对账（把实盘补到目标）",
          len(_sync_calls) - _sync0 == 1, str(_sync_calls[_sync0:]))
    check("结果里带实盘动作", all(x.get("live") == "新建" for x in r["results"]),
          str(r))

    # 已有仓（归属本交易员）→ 追平到交易员当前量 + 重新置托管
    _snap_qf2 = make_snap(TQF, 100000,
                          [tp(f"{_TOK1}USDT", "LONG", 30, 100.0, 100.0,
                              lev=_LEV_QF)])
    _qf_calls.clear()
    r = aft.quick_follow(TQF, _snap_qf2, PARAMS)
    check("已有仓 → 追平到交易员当前量 30 × 0.01",
          abs(open_qty(_TOK1) - 0.30) < 1e-9, str(open_qty(_TOK1)))
    check("已有仓 → 动作是「补齐」", r["results"][0]["action"] == "补齐", str(r))
    with storage.get_conn() as conn:      # 用户先取消托管 → 快速跟随要置回托管
        conn.execute("UPDATE trade_positions SET managed=0 WHERE token=?", (_TOK1,))
    r = aft.quick_follow(TQF, _snap_qf2, PARAMS)
    check("已有仓 → 重新置托管", int(our_pos(_TOK1)["managed"]) == 1,
          str(our_pos(_TOK1)["managed"]))

    # 别的交易员的仓占着这个币 → 跳过（不动别人的仓）
    _TOK3 = "QFL3"
    PRICE[_TOK3] = 10.0
    PERPS.add(_TOK3)
    aft.set_follow("bn-QF2", "QF2", "", True)
    _snap_qf3 = make_snap("bn-QF2", 100000,
                          [tp(f"{_TOK3}USDT", "LONG", 100, 10.0, 10.0)])
    aft.quick_follow("bn-QF2", _snap_qf3, PARAMS)
    _q3 = open_qty(_TOK3)
    r = aft.quick_follow(TQF, make_snap(TQF, 100000,
                                        [tp(f"{_TOK3}USDT", "LONG", 500, 10.0, 10.0)]),
                         PARAMS)
    check("该币已被另一交易员占用 → 跳过并说明",
          r["results"][0]["action"] == "跳过"
          and "另一个交易员" in r["results"][0]["reason"], str(r))
    check("别人的仓没被动", abs(open_qty(_TOK3) - _q3) < 1e-9,
          f"{_q3} -> {open_qty(_TOK3)}")

    # 实盘失败（如杠杆不一致）→ 纸盘照建，结果里带原因
    _live_res.update({"ok": False, "action": "失败", "reason": "杠杆不一致"})
    _TOK4 = "QFL4"
    PRICE[_TOK4] = 5.0
    PERPS.add(_TOK4)
    r = aft.quick_follow(TQF, make_snap(TQF, 100000,
                                        [tp(f"{_TOK4}USDT", "LONG", 10, 5.0, 5.0)]),
                         PARAMS)
    check("实盘被拒：纸盘照建", count_active(_TOK4) == 1, str(r))
    check("实盘被拒：结果里带原因（页面能展示）",
          r["results"][0].get("live") == "杠杆不一致", str(r))
    _live_res.update({"ok": True, "action": "新建"})

    # 币安没有该永续 / 没有标记价 → 跳过该币（不建仓）
    _TOK5 = "QFL5"
    PERPS.discard(_TOK5)
    r = aft.quick_follow(TQF, make_snap(TQF, 100000,
                                        [tp(f"{_TOK5}USDT", "LONG", 10, 5.0, 5.0)]),
                         PARAMS)
    check("无永续/无标记价 → 跳过该币",
          r["results"][0]["action"] == "跳过" and count_active(_TOK5) == 0, str(r))

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
