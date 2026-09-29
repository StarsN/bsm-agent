"""跟单完整流程走查 —— 跑真引擎（process → apply_signal → 对账），不手工摆中间态。

每步验：
  (a) 纸盘数量 == 交易员数量 × k_qty（纸盘跟交易员）
  (b) 实盘记录数量 ≈ 交易所持仓（±容忍带）
  (c) 记录数量 ≈ 份数 × 冻结的每份名义 / mark（冻结语义自洽）
  (d) 冻结的每份名义/杠杆 == 开仓那一刻的生效值（改设置不动已有仓）

流程：A 开仓 → B 一个探测间隔内连加 3 笔（还原真实笔数）→ C 减一份
      → D 手动平仓（纸盘+实盘一起收尾、之后不再复活）→ E 交易员平仓（无动作）
      → F 交易员新开一笔（用当前生效值，含每币覆盖）→ G 改设置不动已有仓
"""
import io
import json
import os
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
_T = tempfile.mkdtemp(prefix="af_cycle_")
config.DB_PATH = os.path.join(_T, "main.db")
config.SUPERTREND_DB = os.path.join(_T, "st.db")
config.LIVE_DB_PATH = os.path.join(_T, "live.db")
config.ALPHAFOX_DB = os.path.join(_T, "af.db")
config.BINANCE_HEDGE_MODE = True

import storage  # noqa: E402
import alphafox_trade as aft  # noqa: E402
import alphafox_live as afl  # noqa: E402

with storage.get_conn() as _c:
    _c.execute("PRAGMA journal_mode=WAL")
    _c.executescript(storage.SCHEMA)
    _c.executescript("""
    DROP TABLE IF EXISTS trade_signal_locks;
    ALTER TABLE trade_positions ADD COLUMN managed INTEGER DEFAULT 1;
    CREATE TABLE trade_signal_locks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL,
        signal_key TEXT NOT NULL, strategy TEXT DEFAULT 'agent',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(token, signal_key, strategy));
    """)
aft.init_db()
storage.init_onchain_live_db()

PRICE = {}
PERPS = set()


def _mark(tok):
    return PRICE.get(tok)


aft.get_mark_price = _mark
aft.has_tradable_perp = lambda tok: tok in PERPS or True
afl.get_mark_price = _mark
aft._map_cache = {}          # 符号映射：identity（TOKXUSDT → TOKX）

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


class FAKE:
    """币安 mock：持仓 + 市价单 + 杠杆（跟单实盘层只用这些）"""
    positions = []
    orders = []          # 记录下过的市价单
    lev = {}
    _id = 9000
    balance = 500.0      # 比例模型：k = 我的可用余额 ÷ 交易员保证金

    @classmethod
    def get_account_info(cls):
        return {"available_balance": cls.balance, "equity": cls.balance}

    @classmethod
    def get_balance(cls):
        return cls.balance

    @classmethod
    def get_positions(cls):
        return [dict(p) for p in cls.positions if abs(p["quantity"]) > 1e-9]

    get_positions_or_none = classmethod(lambda c: c.get_positions())
    get_last_error = classmethod(lambda c: "")
    _round_qty = staticmethod(lambda symbol, qty: round(qty, 8))
    _round_qty_down = staticmethod(lambda symbol, qty: round(qty, 8))

    @classmethod
    def set_leverage(cls, symbol, leverage):
        cls.lev[symbol] = leverage
        return True

    @classmethod
    def _open(cls, tok, side, qty):
        sym = f"{tok}USDT"
        price = PRICE.get(tok, 0.0)
        cls._id += 1
        cls.orders.append({"id": cls._id, "token": tok, "side": side,
                           "qty": qty, "price": price, "action": "open"})
        for p in cls.positions:
            if p["symbol"] == sym and p["position_side"] == side:
                p["quantity"] += qty
                return {"order_id": cls._id, "status": "FILLED",
                        "price": price, "quantity": qty}
        cls.positions.append({"symbol": sym, "quantity": qty, "entry_price": price,
                              "mark_price": price, "unrealized_pnl": 0.0,
                              "leverage": cls.lev.get(sym, 3),
                              "position_side": side})
        return {"order_id": cls._id, "status": "FILLED", "price": price,
                "quantity": qty}

    market_open_long = classmethod(lambda c, tok, qty: c._open(tok, "LONG", qty))
    market_open_short = classmethod(lambda c, tok, qty: c._open(tok, "SHORT", qty))

    @classmethod
    def close_position_market(cls, tok, qty, position_side="LONG"):
        sym = f"{tok}USDT"
        price = PRICE.get(tok, 0.0)
        cls._id += 1
        cls.orders.append({"id": cls._id, "token": tok, "side": position_side,
                           "qty": qty, "price": price, "action": "close"})
        for p in cls.positions:
            if p["symbol"] == sym and p["position_side"] == position_side:
                p["quantity"] = max(p["quantity"] - qty, 0)
        return {"order_id": cls._id, "price": price, "quantity": qty,
                "status": "FILLED"}

    @classmethod
    def pos_qty(cls, tok, side="LONG"):
        return sum(p["quantity"] for p in cls.positions
                   if p["symbol"] == f"{tok}USDT"
                   and p["position_side"] == side)


afl.exchange = FAKE

PARAMS = {"budget": 1000.0, "slip": 0.05, "fee": 0.04, "allow_open": True,
          "live_notional": 0.0}
# (live_enabled, max_notional, live_leverage) —— 比例模型下第二项是**单笔上限**
# （0 = 不限）；规模由 k = 我的可用余额 500U ÷ 交易员保证金 100000U = 0.005 决定
LIVE = (True, 0.0, 3.0)
TRADER_MARGIN = 100000.0
K = FAKE.balance / TRADER_MARGIN          # 0.005
TR = "bn-A"


def snap(margin, positions, orders=None):
    d = {"traderName": "熬鹰", "marginBalance": margin, "positions": positions}
    if orders is not None:
        d["latestOrders"] = orders
    return {"data": d}


def tp(symbol, side, qty, avg, mark, lev=3):
    return {"symbol": symbol, "side": side, "absQty": qty, "avgPrice": avg,
            "markPrice": mark, "notional": qty * mark, "leverage": lev}


# 平台真词表（服务器真样本：open_long/increase_long/close_long/...）。夹具按内部
# 口径写、代码也按内部口径比 —— 就是 2026-09-14 那个"测试全绿、生产空转"的坑
_CANON = {"OPEN": "open_long", "ADD": "increase_long",
          "REDUCE": "decrease_long", "CLOSE": "close_long"}


def mk_order(symbol, action, qty, price, t, canon=None):
    return {"symbol": symbol, "canonicalAction": canon or _CANON.get(action, action),
            "side": "BUY", "executedQty": qty, "avgPrice": price,
            "notional": qty * price, "time": t, "realizedPnl": 0.0}


def poll(positions, orders=None, margin=TRADER_MARGIN):
    """一轮探测（等价探测线程）：process → 每条信号 apply_signal → 对账"""
    sigs = aft.process(TR, snap(margin, positions, orders), PARAMS)
    for s in sigs:
        afl.apply_signal(s, LIVE[0], LIVE[1], LIVE[2])
    afl.sync_alphafox_live(LIVE[0], LIVE[1], LIVE[2])
    return sigs


def paper(tok):
    with storage.get_conn() as c:
        r = c.execute("SELECT * FROM trade_positions WHERE token=? "
                      "AND strategy='alphafox' ORDER BY id DESC LIMIT 1",
                      (tok,)).fetchone()
        return dict(r) if r else None


def live(tok):
    """最近一条记录（含 CLOSED，供看收尾状态）"""
    with storage.get_conn(config.LIVE_DB_PATH) as c:
        r = c.execute("SELECT * FROM live_positions WHERE token=? "
                      "AND strategy='alphafox' ORDER BY id DESC LIMIT 1",
                      (tok,)).fetchone()
        return dict(r) if r else None


def live_active(tok):
    """**活跃**记录（CLOSED 行的 quantity 是历史值，不能拿去和交易所比）"""
    with storage.get_conn(config.LIVE_DB_PATH) as c:
        r = c.execute("SELECT * FROM live_positions WHERE token=? "
                      "AND strategy='alphafox' "
                      "AND status IN ('OPEN','PARTIAL') ORDER BY id DESC LIMIT 1",
                      (tok,)).fetchone()
        return dict(r) if r else None


def orders_of(tok):
    with storage.get_conn(config.LIVE_DB_PATH) as c:
        r = c.execute("SELECT exchange_orders_json FROM live_positions WHERE token=? "
                      "AND strategy='alphafox' ORDER BY id DESC LIMIT 1",
                      (tok,)).fetchone()
    return json.loads(r["exchange_orders_json"]) if r else {}


def frozen(tok):
    """记录里冻结的比例 k / 杠杆"""
    return orders_of(tok).get("k"), (live(tok) or {}).get("leverage")


def lvq(tok):
    return float((live_active(tok) or {}).get("quantity") or 0)


def verify(tag, tok, trader_qty, band=0.05):
    p = paper(tok)
    pk = float(p["quantity"]) - float(p["closed_qty"] or 0) if p else 0.0
    ex = FAKE.pos_qty(tok)
    lq = lvq(tok)
    k, _ = frozen(tok)
    ok = True
    if trader_qty is None:
        if pk > 1e-9 or ex > 1e-9:
            check(f"{tag} (a) 该笔已结束（纸盘/实盘都该是 0）", False,
                  f"纸盘 {pk:.6g} 实盘 {ex:.6g}")
            ok = False
    else:
        kq = float(((p or {}).get("signal_snapshot") and
                    json.loads(p["signal_snapshot"]).get("k_qty")) or 0)
        if abs(pk - trader_qty * kq) > 1e-6:
            check(f"{tag} (a) 纸盘 == 交易员量×k_qty", False,
                  f"纸盘 {pk:.6g} vs {trader_qty * kq:.6g}")
            ok = False
    if abs(ex - lq) > 1e-6:
        check(f"{tag} (b) 实盘记录 == 交易所持仓", False,
              f"记录 {lq:.6g} vs 交易所 {ex:.6g}")
        ok = False
    if lq > 0 and k:
        # (c) 比例模型：记录量 == 交易员量 × 冻结 k
        want = trader_qty * k
        if abs(lq - want) > max(abs(want) * band, 1e-6):
            check(f"{tag} (c) 记录 == 交易员量 × 冻结 k", False,
                  f"记录 {lq:.6g} vs 期望 {want:.6g}（k={k}）")
            ok = False
    if ok:
        check(f"{tag} 一致性（纸盘/记录/交易所/冻结语义）", True)
    return {"paper": pk, "ex": ex, "live": lq}


# =====================================================================
print("========== A. 交易员开仓 → 纸盘 + 实盘各就位 ==========")
aft.set_follow(TR, "熬鹰资本", "", True)
aft._prev_snap.clear()
aft._seen_orders.clear()
PRICE["TOKX"] = 100.0
poll([])                                        # 空快照建基线
O1 = mk_order("TOKXUSDT", "OPEN", 10, 100.0, 1000)
s = poll([tp("TOKXUSDT", "LONG", 10, 100.0, 100.0)], [O1])
check("A1 开仓信号 1 条（executed）",
      len(s) == 1 and s[0]["action"] == "OPEN" and s[0]["executed"], str(s))
verify("A2", "TOKX", 10)
check("A3 实盘按比例开仓 10 × k=0.005 = 0.05",
      abs(FAKE.pos_qty("TOKX") - 10 * K) < 1e-12, str(FAKE.pos_qty("TOKX")))
check("A4 冻结了比例 k=0.005 / 杠杆 3",
      abs(float(frozen("TOKX")[0]) - K) < 1e-12 and frozen("TOKX")[1] == 3,
      str(frozen("TOKX")))

print("\n========== B. 一个探测间隔内连加 3 笔 → 补齐到新目标 ==========")
A1 = mk_order("TOKXUSDT", "ADD", 3, 100.0, 1100)
A2 = mk_order("TOKXUSDT", "ADD", 3, 100.0, 1110)
A3 = mk_order("TOKXUSDT", "ADD", 4, 100.0, 1120)
n0 = len([o for o in FAKE.orders if o["action"] == "open"])
s = poll([tp("TOKXUSDT", "LONG", 20, 100.0, 100.0)],
         [O1, A1, A2, A3])
adds = [x for x in s if x["action"] == "ADD"]
check("B1 一次净变化还原成 3 条 ADD", len(adds) == 3, str(s))
check("B2 实盘只下 1 单（按目标补齐；后两条 ADD 已在目标 → 无操作）",
      len([o for o in FAKE.orders if o["action"] == "open"]) - n0 == 1,
      str(FAKE.orders[n0:]))
check("B3 记录量 = 交易员 20 × k=0.005 = 0.1",
      abs(lvq("TOKX") - 20 * K) < 1e-12, str(lvq("TOKX")))
verify("B4", "TOKX", 20)

print("\n========== C. 交易员减仓（两笔并成一次净变化）==========")
R1 = mk_order("TOKXUSDT", "REDUCE", 2, 100.0, 1200)
R2 = mk_order("TOKXUSDT", "REDUCE", 3, 100.0, 1210)
n0 = len([o for o in FAKE.orders if o["action"] == "close"])
s = poll([tp("TOKXUSDT", "LONG", 15, 100.0, 100.0)],
         [O1, A1, A2, A3, R1, R2])
check("C1 两笔减仓都还原（对称：减仓同样按流水笔数）",
      len([x for x in s if x["action"] == "REDUCE"]) == 2, str(s))
check("C2 实盘只平 1 次（第二条 REDUCE 已在目标 → 无操作）",
      len([o for o in FAKE.orders
           if o["action"] == "close"]) - n0 == 1,
      str(FAKE.orders[n0:]))
check("C3 记录量 = 交易员 15 × k=0.005 = 0.075",
      abs(lvq("TOKX") - 15 * K) < 1e-12, str(lvq("TOKX")))
verify("C4", "TOKX", 15)

print("\n========== D. 手动平仓（交易所持仓消失）→ 纸盘+实盘一起收尾 ==========")
FAKE.positions = [p for p in FAKE.positions if p["symbol"] != "TOKXUSDT"]
with storage.get_conn(config.LIVE_DB_PATH) as _lc:   # 越过 90s 建仓保护
    _lc.execute("UPDATE live_positions SET created_at=datetime('now','-200 seconds') "
                "WHERE token='TOKX'")
afl.sync_alphafox_live(LIVE[0], LIVE[1], LIVE[2])
p = paper("TOKX")
check("D1 纸盘这一笔一起收尾（CLOSED + 留痕）",
      p and p["status"] == "CLOSED" and "不再跟这一笔" in (p["advice"] or ""),
      str(p and (p["status"], p["advice"])))
check("D2 实盘记录收尾", (live("TOKX") or {}).get("status") == "CLOSED",
      str((live("TOKX") or {}).get("status")))
verify("D3", "TOKX", None)
n0 = len(FAKE.orders)
A9 = mk_order("TOKXUSDT", "ADD", 20, 100.0, 1300)
s = poll([tp("TOKXUSDT", "LONG", 35, 100.0, 100.0)],
         [O1, A1, A2, A3, R1, R2, A9])
check("D4 交易员再加仓：不下任何单（这笔已结束）", len(FAKE.orders) == n0,
      str(FAKE.orders[n0:]))
check("D5 也没有新建 live 记录",
      len([r for r in [live("TOKX")] if r]) == 1 and lvq("TOKX") == 0,
      str(lvq("TOKX")))

print("\n========== E. 交易员平仓 → 已结束的这笔不动 ==========")
# 空快照要连探两轮：第一轮按"快照异常待确认"处理（防脏快照误平），第二轮才认
s = poll([], [O1, A1, A2, A3, R1, R2, A9])
check("E1 第一轮：按快照异常挂起（不动作）", s == [], str(s))
s = poll([], [O1, A1, A2, A3, R1, R2, A9])
check("E2 第二轮：确认真实变化；只会补一条**未执行**的 CLOSE（这笔早已收尾）",
      all(x["action"] == "CLOSE" and not x["executed"] for x in s), str(s))
check("E2b 交易所没被这条无意义 CLOSE 动过", abs(FAKE.pos_qty("TOKX")) < 1e-9,
      str(FAKE.pos_qty("TOKX")))

print("\n========== F. 交易员新开一笔 + 每币杠杆覆盖 → 新仓用生效值并冻结 ==========")
with storage.get_conn(config.ALPHAFOX_DB) as _c:
    _c.execute("INSERT OR REPLACE INTO alphafox_token_settings "
               "(token, live_notional, live_leverage, updated_at) VALUES (?,?,?,?)",
               ("TOKX", None, 5.0, int(time.time())))
n0 = len(FAKE.orders)
s = poll([tp("TOKXUSDT", "LONG", 12, 100.0, 100.0)])   # 键重新出现 → 新开一笔
check("F1 新开一笔", any(x["action"] == "OPEN" and x["executed"] for x in s), str(s))
poll([tp("TOKXUSDT", "LONG", 12, 100.0, 100.0)],
     [mk_order("TOKXUSDT", "OPEN", 12, 100.0, 1400)])   # 带流水的下一轮（推进水位）
check("F2 新仓：规模仍按比例（12 × 0.005 = 0.06）、杠杆用每币覆盖 5",
      abs(FAKE.pos_qty("TOKX") - 12 * K) < 1e-12
      and abs(float(frozen("TOKX")[1]) - 5) < 1e-9,
      f"{FAKE.pos_qty('TOKX')} / {frozen('TOKX')}")
verify("F3", "TOKX", 12)
# 改覆盖 → 已有仓不动（杠杆在开仓那一刻冻结）
with storage.get_conn(config.ALPHAFOX_DB) as _c:
    _c.execute("UPDATE alphafox_token_settings SET live_leverage=20 "
               "WHERE token='TOKX'")
n0 = len(FAKE.orders)
afl.sync_alphafox_live(LIVE[0], LIVE[1], LIVE[2])
check("F4 改覆盖后已有仓不动、不下单", len(FAKE.orders) == n0 and
      abs(FAKE.pos_qty("TOKX") - 12 * K) < 1e-12,
      f"{FAKE.pos_qty('TOKX')} {FAKE.orders[n0:]}")
check("F5 冻结值没被改写（k 与杠杆都不动）",
      abs(float(frozen("TOKX")[0]) - K) < 1e-12 and float(frozen("TOKX")[1]) == 5,
      str(frozen("TOKX")))

print("\n========== G. 交易员减到零 → 全平 + 收尾 ==========")
n0 = len(FAKE.orders)
s = poll([tp("TOKXUSDT", "LONG", 5, 100.0, 100.0)],
         [mk_order("TOKXUSDT", "OPEN", 12, 100.0, 1400),
          mk_order("TOKXUSDT", "REDUCE", 7, 100.0, 1500)])
check("G1 交易员减仓（未到零）→ 实盘减 1 次", len(FAKE.orders) - n0 == 1,
      str(FAKE.orders[n0:]))
s = poll([], [mk_order("TOKXUSDT", "OPEN", 12, 100.0, 1400),
              mk_order("TOKXUSDT", "REDUCE", 7, 100.0, 1500),
              mk_order("TOKXUSDT", "CLOSE", 5, 100.0, 1600)])
# 两轮确认机制：第一轮待确认，第二轮才平
s = poll([], [mk_order("TOKXUSDT", "OPEN", 12, 100.0, 1400),
              mk_order("TOKXUSDT", "REDUCE", 7, 100.0, 1500),
              mk_order("TOKXUSDT", "CLOSE", 5, 100.0, 1600)])
check("G2 交易员平仓 → 实盘清零", abs(FAKE.pos_qty("TOKX")) < 1e-9,
      str(FAKE.pos_qty("TOKX")))
verify("G3", "TOKX", None)

print("\n========== H. 混合窗口：一轮里'加 3 笔 + 减 1 笔' → 按净笔数还原 ==========")
# 口径（2026-09-14 对称化后）：加仓/减仓都按**流水真实笔数**，取两者的净笔数
# （+3 −1 = +2）→ 份数只 +2，不会再出现"加仓按笔数、减仓被吞"的单向偏高。
PRICE["TOKY"] = 100.0
poll([])
# 开仓那一轮就把这笔流水带上（平台每轮都推全量 latestOrders，真样本 40+ 条）：
# 不带的话，水位里没有它 → 下一轮会被当"新增加仓"再多加一份（夹具不真实会骗过测试）
_o0 = mk_order("TOKYUSDT", "OPEN", 10, 100.0, 3000)
s = poll([tp("TOKYUSDT", "LONG", 10, 100.0, 100.0)], [_o0])
check("H1 前置：开仓镜像 10 × 0.005 = 0.05",
      abs(lvq("TOKY") - 10 * K) < 1e-12, str(lvq("TOKY")))
_h1 = mk_order("TOKYUSDT", "ADD", 3, 100.0, 3001)
_h2 = mk_order("TOKYUSDT", "ADD", 3, 100.0, 3002)
_h3 = mk_order("TOKYUSDT", "ADD", 4, 100.0, 3003)
_r1 = mk_order("TOKYUSDT", "REDUCE", 4, 100.0, 3004)
s = poll([tp("TOKYUSDT", "LONG", 16, 100.0, 100.0)], [_o0, _h1, _h2, _h3, _r1])
print(f"     净笔数 +2（加 3 笔 − 减 1 笔）→ 纸盘量 {paper('TOKY')['quantity']}")
check("H2 纸盘量 = 16 × k_qty（净笔数还原，不再偏高）",
      abs(float(paper("TOKY")["quantity"]) - 16 * 0.01) < 1e-9,
      str(paper("TOKY")["quantity"]))
check("H3 记录量 = 交易员 16 × k=0.005 = 0.08",
      abs(lvq("TOKY") - 16 * K) < 1e-12, str(lvq("TOKY")))

print("\n========== I. 恢复中：不减仓；落定按现状冻结（手动加的仓不被抹掉） ==========")
# 不变量：managed=ON ⟺ 记录里的 k 已按"最近一次观测到的交易所现状"冻好。
# 恢复中(2) 期间实盘只做事实同步、不下任何单；观察不到现状就停在恢复中。
TOKZ = "TOKZ"
PRICE[TOKZ] = 100.0
poll([])                                       # 基线
_o = mk_order(f"{TOKZ}USDT", "OPEN", 10, 100.0, 4000)
poll([tp(f"{TOKZ}USDT", "LONG", 10, 100.0, 100.0)], [_o])
check("I1 前置：实盘开仓 10 × 0.005 = 0.05",
      abs(FAKE.pos_qty(TOKZ) - 10 * K) < 1e-12, str(FAKE.pos_qty(TOKZ)))
_pid = paper(TOKZ)["id"]
with storage.get_conn() as c:                  # 取消托管 + 你在币安手动加到 0.15
    c.execute("UPDATE trade_positions SET managed=0 WHERE id=?", (_pid,))
for _p in FAKE.positions:
    if _p["symbol"] == f"{TOKZ}USDT":
        _p["quantity"] = 0.15
poll([tp(f"{TOKZ}USDT", "LONG", 10, 100.0, 100.0)], [_o])
check("I2 非托管：只事实同步（记录 ← 0.15），交易所不动",
      abs(lvq(TOKZ) - 0.15) < 1e-12 and abs(FAKE.pos_qty(TOKZ) - 0.15) < 1e-12,
      f"{lvq(TOKZ)} {FAKE.pos_qty(TOKZ)}")
with storage.get_conn() as c:                  # 点「恢复托管」
    c.execute("UPDATE trade_positions SET managed=2 WHERE id=?", (_pid,))
_real_reattach = afl.on_reattach
afl.on_reattach = lambda *a, **k: False        # 这一轮交易所查询不可用
_n = len(FAKE.orders)
poll([tp(f"{TOKZ}USDT", "LONG", 10, 100.0, 100.0)], [_o])
check("I3 实盘侧未就绪：留在恢复中(2)、零下单",
      int(paper(TOKZ)["managed"]) == 2 and len(FAKE.orders) == _n,
      f"{paper(TOKZ)['managed']} {FAKE.orders[_n:]}")
check("I4 未就绪时 k 不许被改写（还是开仓时的 0.005）",
      abs(float(frozen(TOKZ)[0]) - K) < 1e-12, str(frozen(TOKZ)))
poll([tp(f"{TOKZ}USDT", "LONG", 5, 100.0, 100.0)],
     [_o, mk_order(f"{TOKZ}USDT", "REDUCE", 5, 100.0, 4100)])
check("I5 恢复中遇交易员减仓：实盘零下单（旧 k 不参与下单）",
      len(FAKE.orders) == _n and abs(FAKE.pos_qty(TOKZ) - 0.15) < 1e-12,
      f"{FAKE.orders[_n:]} {FAKE.pos_qty(TOKZ)}")
afl.on_reattach = _real_reattach               # 交易所恢复
poll([tp(f"{TOKZ}USDT", "LONG", 5, 100.0, 100.0)],
     [_o, mk_order(f"{TOKZ}USDT", "REDUCE", 5, 100.0, 4100)])
check("I6 落定：k 按现状 = 0.15 ÷ 5 = 0.03",
      abs(float(frozen(TOKZ)[0]) - 0.03) < 1e-12, str(frozen(TOKZ)))
check("I7 落定后零下单、手动加的仓一分不动（仍 0.15、已托管中）",
      int(paper(TOKZ)["managed"]) == 1 and abs(FAKE.pos_qty(TOKZ) - 0.15) < 1e-12
      and len(FAKE.orders) == _n,
      f"{paper(TOKZ)['managed']} {FAKE.pos_qty(TOKZ)} {FAKE.orders[_n:]}")

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
