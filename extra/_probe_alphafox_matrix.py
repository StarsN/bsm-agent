"""跟单状态 × 事件矩阵探针 —— 跑真引擎（process → apply_signal → 对账）。

覆盖现有用例没系统覆盖的组合：托管态 {0 取消,1 托管,2 恢复中,3 已脱离} ×
事件 {交易员加仓, 交易员减仓, 交易员平仓, 用户手动加仓}，
每一步断言**策略表**：

  managed | 交易员加仓 | 交易员减仓 | 交易员平仓 | 用户手动改仓
  --------|-----------|-----------|-----------|-------------
  1 托管中 |  实盘跟加 |  实盘跟减 |  实盘平   | 下一轮按记录对齐(不越界)
  0 取消   |  不动     |  不动     |  不动     | 只事实同步
  2 恢复中 |  不动     |  不动     |  不动     | 只事实同步
  3 已脱离 |  不动     |  不动     |  不动     | 只事实同步

"不动" = 系统一笔单都不下（交易所持仓数量不变）。
"""
import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
_T = tempfile.mkdtemp(prefix="af_matrix_")
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
aft.get_mark_price = lambda tok: PRICE.get(tok)
aft.has_tradable_perp = lambda tok: True
afl.get_mark_price = lambda tok: PRICE.get(tok)
aft._map_cache = {}


class FAKE:
    positions = []
    orders = []
    lev = {}
    _id = 9000
    balance = 1000.0

    @classmethod
    def get_account_info(cls):
        return {"available_balance": cls.balance, "equity": cls.balance}

    @classmethod
    def get_balance(cls):
        return cls.balance

    @classmethod
    def get_positions(cls):
        return [dict(p) for p in cls.positions if abs(p["quantity"]) > 1e-9]

    get_last_error = classmethod(lambda c: "")
    _round_qty = staticmethod(lambda symbol, qty: round(qty, 8))
    _round_qty_down = staticmethod(lambda symbol, qty: round(qty, 8))
    _fetch_symbol_filters = staticmethod(
        lambda symbol: {"stepSize": 0.0, "minQty": 0.0, "priceStep": 0.01})

    @classmethod
    def set_leverage(cls, symbol, leverage):
        cls.lev[symbol] = leverage
        for p in cls.positions:
            if p["symbol"] == symbol:
                p["leverage"] = leverage
        return True

    @classmethod
    def _open(cls, tok, side, qty):
        sym = f"{tok}USDT"
        price = PRICE.get(tok, 100.0)
        cls._id += 1
        cls.orders.append({"id": cls._id, "token": tok, "act": "open", "qty": qty})
        for p in cls.positions:
            if p["symbol"] == sym and p["position_side"] == side:
                p["quantity"] += qty
                return {"order_id": cls._id, "status": "FILLED",
                        "price": price, "quantity": qty}
        cls.positions.append({"symbol": sym, "quantity": qty, "entry_price": price,
                              "mark_price": price, "unrealized_pnl": 0.0,
                              "leverage": cls.lev.get(sym, 3), "position_side": side})
        return {"order_id": cls._id, "status": "FILLED", "price": price,
                "quantity": qty}

    market_open_long = classmethod(lambda c, tok, qty: c._open(tok, "LONG", qty))

    @classmethod
    def close_position_market(cls, tok, qty, position_side="LONG"):
        sym = f"{tok}USDT"
        price = PRICE.get(tok, 100.0)
        cls._id += 1
        cls.orders.append({"id": cls._id, "token": tok, "act": "close", "qty": qty})
        for p in cls.positions:
            if p["symbol"] == sym and p["position_side"] == position_side:
                p["quantity"] = max(p["quantity"] - qty, 0)
        return {"order_id": cls._id, "price": price, "quantity": qty,
                "status": "FILLED"}

    @classmethod
    def pos_qty(cls, tok, side="LONG"):
        return sum(p["quantity"] for p in cls.positions
                   if p["symbol"] == f"{tok}USDT" and p["position_side"] == side)


afl.exchange = FAKE
PARAMS = {"budget": 1000.0, "slip": 0.05, "fee": 0.04, "allow_open": True,
          "live_notional": 0.0}
LIVE = (True, 0.0, 3.0)
TM = 100000.0
K = FAKE.balance / TM                      # 0.01
TR = "bn-MX"
STATE_CN = {0: "取消托管", 1: "托管中", 2: "恢复中", 3: "已脱离"}

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def snap(positions):
    return {"data": {"traderName": "矩阵", "marginBalance": TM,
                     "positions": positions, "latestOrders": []}}


def tp(symbol, side, qty, avg, mark, lev=3):
    return {"symbol": symbol, "side": side, "absQty": qty, "avgPrice": avg,
            "markPrice": mark, "notional": qty * mark, "leverage": lev}


def poll(positions):
    sigs = aft.process(TR, snap(positions), PARAMS)
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


def live_act(tok):
    with storage.get_conn(config.LIVE_DB_PATH) as c:
        r = c.execute("SELECT * FROM live_positions WHERE token=? AND strategy='alphafox' "
                      "AND status IN ('OPEN','PARTIAL') ORDER BY id DESC LIMIT 1",
                      (tok,)).fetchone()
        return dict(r) if r else None


def setup(tok, qty=10.0):
    """建一个正常的托管中持仓：交易员 10 → 纸盘 10×k_qty、实盘 10×K。"""
    PRICE[tok] = 100.0
    aft._prev_snap.clear()
    poll([])
    poll([tp(f"{tok}USDT", "LONG", qty, 100.0, 100.0)])
    return paper(tok), live_act(tok)


# 只调一次：set_follow(True) 会把非托管仓批量转"恢复中"（见 _batch_managed），
# 放在建仓前调用，否则会把矩阵里刚设好的 managed 全部冲掉
aft.set_follow(TR, "矩阵", "", True)


def to_state(tok, state):
    pid = paper(tok)["id"]
    with storage.get_conn() as c:
        c.execute("UPDATE trade_positions SET managed=? WHERE id=?", (state, pid))
    return pid


def n_orders():
    return len(FAKE.orders)


print("=" * 70)
print("矩阵：托管态 × 事件 —— 每格断言『系统该不该下单』")
print("=" * 70)

# ---------------------------------------------------------------- 托管中(1)
for ev, fn, want in (
    ("交易员加仓", lambda t: poll([tp(f"{t}USDT", "LONG", 20, 100.0, 100.0)]), True),
    ("交易员减仓", lambda t: poll([tp(f"{t}USDT", "LONG", 5, 100.0, 100.0)]), True),
):
    tok = "M1ADD" if "加" in ev else "M1RED"
    setup(tok)
    n0 = n_orders()
    _ex0 = FAKE.pos_qty(tok)
    fn(tok)
    moved = abs(FAKE.pos_qty(tok) - _ex0) > 1e-9
    check(f"[托管中] {ev} → {'实盘跟单' if want else '不动'}",
          moved == want, f"下单 {n_orders() - n0} 笔, 交易所 {_ex0}→{FAKE.pos_qty(tok)}")

tok = "M1CLS"
setup(tok)
n0 = n_orders()
poll([])
poll([])                     # 两轮确认平仓
check("[托管中] 交易员平仓 → 实盘平",
      FAKE.pos_qty(tok) < 1e-9 and n_orders() > n0,
      f"下单 {n_orders() - n0} 笔, 交易所 {FAKE.pos_qty(tok)}")

# ------------------------------------------------- 非托管 0 / 2 / 3
# "恢复中"是**过渡态**：交易所可用时，下一轮 process 就会落定成"托管中"（那是正确行为）。
# 想观察"停在恢复中时事件怎么处理"，必须把落定挡住（模拟交易所查询不可用）——
# 只有这种情形下事件才会真的撞上 managed==2。
_REAL_REATTACH = afl.on_reattach
for state in (0, 2, 3):
    tag = STATE_CN[state]
    if state == 2:
        afl.on_reattach = lambda *a, **k: False       # 落定失败 → 停在恢复中
    else:
        afl.on_reattach = _REAL_REATTACH
    for ev, key, fn in (
        ("交易员加仓", "ADD", lambda t: poll([tp(f"{t}USDT", "LONG", 20, 100.0, 100.0)])),
        ("交易员减仓", "RED", lambda t: poll([tp(f"{t}USDT", "LONG", 5, 100.0, 100.0)])),
    ):
        tok = f"N{state}{key}"
        setup(tok)
        to_state(tok, state)
        poll([tp(f"{tok}USDT", "LONG", 10, 100.0, 100.0)])   # 让状态生效一轮
        check(f"[{tag}] 前置：事件前仍停在 {tag}",
              int(paper(tok)["managed"]) == state, str(paper(tok)["managed"]))
        n0 = n_orders()
        _ex0 = FAKE.pos_qty(tok)
        fn(tok)
        check(f"[{tag}] {ev} → 实盘零下单",
              n_orders() == n0 and abs(FAKE.pos_qty(tok) - _ex0) < 1e-12,
              f"下单 {n_orders() - n0} 笔, 交易所 {_ex0}→{FAKE.pos_qty(tok)}")
    # 交易员平仓
    tok = f"N{state}CLS"
    setup(tok)
    to_state(tok, state)
    poll([tp(f"{tok}USDT", "LONG", 10, 100.0, 100.0)])
    n0 = n_orders()
    _ex0 = FAKE.pos_qty(tok)
    poll([])
    poll([])
    poll([])
    check(f"[{tag}] 交易员平仓 → 实盘零下单（仓位保留）",
          n_orders() == n0 and abs(FAKE.pos_qty(tok) - _ex0) < 1e-12,
          f"下单 {n_orders() - n0} 笔, 交易所 {_ex0}→{FAKE.pos_qty(tok)}, "
          f"managed={paper(tok)['managed']}")
    # 用户手动加仓
    tok = f"N{state}MAN"
    setup(tok)
    to_state(tok, state)
    for p in FAKE.positions:
        if p["symbol"] == f"{tok}USDT":
            p["quantity"] = 0.30
    n0 = n_orders()
    poll([tp(f"{tok}USDT", "LONG", 10, 100.0, 100.0)])
    lr = live_act(tok)
    check(f"[{tag}] 用户手动加仓 → 只事实同步、零下单",
          n_orders() == n0 and abs(FAKE.pos_qty(tok) - 0.30) < 1e-12
          and abs(float((lr or {}).get("quantity") or 0) - 0.30) < 1e-9,
          f"下单 {n_orders() - n0} 笔, 记录 {float((lr or {}).get('quantity') or 0):.6g}, "
          f"交易所 {FAKE.pos_qty(tok):.6g}")

afl.on_reattach = _REAL_REATTACH

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
