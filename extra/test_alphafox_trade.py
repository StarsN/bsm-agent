"""alphafox 跟单信号处理单元测试（mock 标记价 + 假快照）

覆盖：首轮基线/孤儿清理、开仓(k_qty锁定+成本价)、同币跳过(方案B)、
加仓合并均价、减仓结算、平仓、未跟随仓不操作、杠杆单独变化、对齐式自愈
"""
import os
import sys
import tempfile

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
aft.get_mark_price = lambda token: PRICE.get(token)

PARAMS = {"budget": 1000.0, "slip": 0.05, "fee": 0.04}
T1 = "bn-111"
T2 = "bn-222"
K1 = 1000.0 / 100000.0   # T1 的 k_qty


def make_snap(trader_id, margin, positions):
    return {"data": {"traderName": trader_id, "marginBalance": margin,
                     "positions": positions}}


def tp(symbol, side, qty, avg, mark, lev=2):
    """交易员持仓项"""
    return {"symbol": symbol, "side": side, "absQty": qty, "avgPrice": avg,
            "markPrice": mark, "notional": qty * mark, "leverage": lev}


def our_pos(token):
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM trade_positions WHERE token=? AND strategy='alphafox' "
            "AND status IN ('OPEN','PARTIAL') ORDER BY id DESC LIMIT 1",
            (token,)).fetchone()
        return dict(row) if row else None


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
    aft._prev_snap.clear()

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
    aft.process(T1, make_snap(T1, 100000, []), PARAMS)  # T1 首轮（基线，无 ORPHAN 仓）
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM trade_positions WHERE token='ORPHAN'").fetchone()
    check("孤儿仓被平", row and row["status"] == "CLOSED",
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
    check("异常计数已记录", aft._anomaly_rounds.get(T2, 0) == 1)
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
    # 减仓：200 → 50，realized = (entry - sell) × Δ（空单方向）
    PRICE["BTC"] = 48500
    s = aft.process(T1, make_snap(T1, 100000,
                                  [tp("BTCUSDT", "SHORT", 50, 48500, 48500, lev=3)]), PARAMS)
    check("空单减仓信号", len(s) == 1 and s[0]["action"] == "REDUCE", str(s))
    p = our_pos("BTC")
    q3 = 50 * k
    sell3 = 48500 * (1 - 0.05 / 100) * (1 - 0.04 / 100)
    expect_real = (expect_entry - sell3) * (q2 - q3)
    check("空单减仓 realized（entry-sell 方向正确）",
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

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
