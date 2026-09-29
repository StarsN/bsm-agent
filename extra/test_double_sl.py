"""主流趋势策略：反转并存窗口的"双 SL / 重复记录"一致性测试（跑真代码）。

背景：反转市价开同时产生「市价腿 OPEN」+「挂单腿 PENDING」两条纸盘行，实盘侧
对应两条 live 记录、两张 SL（各覆盖自己那条腿）。纸盘 1s 循环把两条腿合并成一笔后，
若实盘侧没跟着收敛，会出现：
  · SL 总量 ≠ 交易所持仓（超额 → 第二张触发时无仓可平；欠额 → 只平一半）
  · 两条 OPEN 记录代表同一个仓（实盘重复记账）
  · 记录数量 ≠ 纸盘数量（之后的合法减仓被"疑似手动仓"守卫跳过）

本套件覆盖三条对称时序 + 四个"不能被打坏"的场景：
  ① 纸盘 1s 循环先转 OPEN 并合并（~98% 常态）
  ② 60s 对账先撞上"交易所已成交、纸盘还是 PENDING"的窄窗口（~2%）
  ⑤ SL 触发后纸盘/实盘能否收敛
  ⑥ 并存但挂单**没成交** → 一个字都不能动
  ⑦ 反方向残留记录（换向场景）→ 必须保留
  ⑧ 部分成交 → 用这条挂单自己的成交量，不是整仓量

运行：python extra/test_double_sl.py
"""
import io
import json
import os
import sys
import tempfile
import time
import types

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

_TMP = tempfile.mkdtemp(prefix="dblsl_")
config.DB_PATH = os.path.join(_TMP, "main.db")
config.SUPERTREND_DB = os.path.join(_TMP, "supertrend.db")
config.LIVE_DB_PATH = os.path.join(_TMP, "live.db")
config.BINANCE_HEDGE_MODE = True

import storage                                              # noqa: E402
import supertrend_strategy                                  # noqa: E402
import supertrend_live as stl                               # noqa: E402

with storage.get_conn() as conn:
    conn.executescript(storage.SCHEMA)
    conn.executescript("""
    DROP TABLE IF EXISTS trade_signal_locks;
    CREATE TABLE trade_signal_locks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT NOT NULL, signal_key TEXT NOT NULL,
        strategy TEXT DEFAULT 'agent',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(token, signal_key, strategy)
    );
    """)
supertrend_strategy.init_db()
storage.init_onchain_live_db()

PRICE = {}
supertrend_strategy.get_mark_price = lambda token: PRICE.get(token)
import exchange as _ex                                       # noqa: E402
_ex._fetch_symbol_filters = lambda symbol: {
    "stepSize": 0.001, "minQty": 0.0, "priceStep": 0.01}
supertrend_strategy.time.sleep = lambda *a, **k: None

FAKE = types.SimpleNamespace(
    positions=[], orders=[], algo_orders=[], _next_id=9000,
    last_error="", _last_error_msg="", fail_open_orders=False,
    fail_positions=False)


def _market_open(token, side, qty):
    price = PRICE.get(token, 100.0)
    sym = f"{token}USDT"
    for p in FAKE.positions:
        if p["symbol"] == sym and p["position_side"] == side:
            p["quantity"] += qty
            return {"order_id": 1, "status": "FILLED", "price": price, "quantity": qty}
    FAKE.positions.append({"symbol": sym, "quantity": qty, "entry_price": price,
                           "mark_price": price, "unrealized_pnl": 0.0,
                           "leverage": 3, "position_side": side})
    return {"order_id": 1, "status": "FILLED", "price": price, "quantity": qty}


def _close(token, qty, position_side="LONG"):
    price = PRICE.get(token, 100.0)
    for p in FAKE.positions:
        if p["symbol"] == f"{token}USDT" and p["position_side"] == position_side:
            p["quantity"] = max(p["quantity"] - qty, 0)
    return {"order_id": 1, "price": price, "quantity": qty, "status": "FILLED"}


def _limit_open(token, side, qty, price, position_side="LONG"):
    FAKE._next_id += 1
    FAKE.orders.append({"order_id": FAKE._next_id, "symbol": f"{token}USDT",
                        "side": side, "type": "LIMIT", "price": price,
                        "quantity": qty, "position_side": position_side,
                        "status": "NEW"})
    return {"order_id": FAKE._next_id, "status": "NEW", "price": price,
            "quantity": qty}


def _stop_loss(token, side, qty, stop_price, position_side="LONG"):
    FAKE._next_id += 1
    FAKE.algo_orders.append({"order_id": FAKE._next_id, "symbol": f"{token}USDT",
                             "side": side, "type": "STOP_MARKET",
                             "triggerPrice": stop_price, "quantity": qty,
                             "position_side": position_side, "status": "NEW"})
    return {"order_id": FAKE._next_id, "status": "NEW"}


def _cancel(token, order_id):
    for o in FAKE.orders + FAKE.algo_orders:
        if o["order_id"] == order_id:
            o["status"] = "CANCELED"
    return True


def _get_open_orders(token=None):
    """字段名用币安真实的 orderId / executedQty（判成交的代码读这两个）"""
    rows = [o for o in FAKE.orders if o["status"] == "NEW"]
    if token:
        rows = [o for o in rows if o["symbol"] == f"{token.upper()}USDT"]
    return [dict(o, orderId=o["order_id"],
                 executedQty=float(o.get("executed_qty") or 0)) for o in rows]


def _fill_limit(token):
    """整张成交：单子从挂单列表消失"""
    for o in FAKE.orders:
        if o["symbol"] == f"{token}USDT" and o["status"] == "NEW" \
                and o["type"] == "LIMIT":
            o["status"] = "FILLED"
            return True
    return False


def _fill_limit_partial(token, qty):
    """部分成交：未成交部分仍挂着，executedQty>0"""
    for o in FAKE.orders:
        if o["symbol"] == f"{token}USDT" and o["status"] == "NEW" \
                and o["type"] == "LIMIT":
            o["executed_qty"] = qty
            return True
    return False


FAKE.get_positions = lambda: [dict(p) for p in FAKE.positions
                              if abs(p["quantity"]) > 1e-9]
FAKE.get_positions_or_none = lambda: (
    None if FAKE.fail_positions else FAKE.get_positions())
FAKE.get_open_orders = _get_open_orders
FAKE.get_open_orders_or_none = lambda token=None: (
    None if FAKE.fail_open_orders else _get_open_orders(token))
FAKE.get_algo_orders = lambda token=None: [
    o for o in FAKE.algo_orders if o["status"] == "NEW"]
FAKE.get_last_error = lambda: ""
FAKE.get_account_info = lambda: {"equity": 1000.0, "margin_balance": 500.0,
                                 "available_balance": 500.0, "unrealized_pnl": 0.0}
FAKE.get_balance = lambda: 1000.0
FAKE._round_qty = lambda symbol, qty: qty
FAKE.set_leverage = lambda symbol, lev: True
FAKE.market_open_long = lambda token, qty: _market_open(token, "LONG", qty)
FAKE.market_open_short = lambda token, qty: _market_open(token, "SHORT", qty)
FAKE.limit_open = _limit_open
FAKE.stop_loss_order = _stop_loss
FAKE.cancel_order = _cancel
FAKE.cancel_all_orders = lambda token: True
FAKE.close_position_market = lambda token, qty, position_side="LONG": _close(
    token, qty, position_side)
FAKE.fill_limit = _fill_limit
FAKE.fill_limit_partial = _fill_limit_partial
stl.exchange = FAKE

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def sl_algos(token):
    return [a for a in FAKE.algo_orders
            if a["symbol"] == f"{token}USDT" and a["status"] == "NEW"]


def insert_paper(token, status, qty, entry, sl, limit_price=None):
    with storage.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO trade_positions (token, symbol, side, status, mode, "
            "margin_amount, leverage, notional, quantity, entry_price, "
            "limit_price, current_price, stop_loss_price, strategy) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (token, f"{token}USDT", "LONG", status, "paper", 50.0, 3.0,
             150.0, qty, entry, limit_price,
             entry if status == "OPEN" else (limit_price or entry), sl,
             "supertrend"))
        return cur.lastrowid


def reset(token, price=60000.0):
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE token=?", (token,))
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        lc.execute("DELETE FROM live_positions WHERE token=?", (token,))
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("INSERT OR REPLACE INTO supertrend_tokens "
                   "(token, status, added_at, live_mode) VALUES (?,?,?,1)",
                   (token, "ACTIVE", int(time.time())))
    PRICE[token] = price
    FAKE.positions.clear()
    FAKE.orders.clear()
    FAKE.algo_orders.clear()
    FAKE.fail_open_orders = False
    FAKE.fail_positions = False
    stl._rounds_done = 0


def open_coexist(token):
    """反转那一轮：市价腿 + 挂单腿，两条腿都镜像到币安"""
    reset(token)
    insert_paper(token, "OPEN", 0.001, 60000.0, 59000.0)
    insert_paper(token, "PENDING", 0.001, None, 59000.0, limit_price=59500.0)
    for _ in range(3):
        stl.sync_supertrend_live()


def paper_convert_and_merge(token):
    """模拟纸盘 1s 循环：PENDING → OPEN，再走 60s 轮开头的兜底合并"""
    with storage.get_conn() as mc:
        mc.execute("UPDATE trade_positions SET status='OPEN', entry_price=59500, "
                   "current_price=59500 WHERE token=? AND status='PENDING'", (token,))
    with storage.get_conn() as mc:
        supertrend_strategy._merge_positions(mc, token)


def state(token):
    """交易所 / 纸盘 / live 三方状态"""
    pos = sum(p["quantity"] for p in FAKE.positions if p["symbol"] == f"{token}USDT")
    sl = sum(a["quantity"] for a in sl_algos(token))
    with storage.get_conn() as mc:
        pq = float(mc.execute(
            "SELECT COALESCE(SUM(quantity-IFNULL(closed_qty,0)),0) q "
            "FROM trade_positions WHERE token=? AND strategy='supertrend' "
            "AND status IN ('OPEN','PARTIAL')", (token,)).fetchone()["q"])
        pany = [dict(r) for r in mc.execute(
            "SELECT id,status FROM trade_positions WHERE token=? "
            "AND strategy='supertrend'", (token,))]
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        lrows = [dict(r) for r in lc.execute(
            "SELECT id,status,quantity,side,paper_position_id FROM live_positions "
            "WHERE token=? AND strategy='supertrend' "
            "AND status IN ('OPEN','PARTIAL','PENDING') ORDER BY id", (token,))]
    return {"pos": pos, "sl": sl, "pq": pq, "paper": pany, "live": lrows}


def report(tag, token, st=None):
    st = st or state(token)
    print(f"    {tag}: 交易所持仓 {st['pos']:.6g} | SL {len(sl_algos(token))} 张 "
          f"总 {st['sl']:.6g} | 纸盘 {st['pq']:.6g} | live "
          f"{[(r['id'], r['status'], r['quantity']) for r in st['live']]}")


with storage.get_conn(config.SUPERTREND_DB) as c:
    c.execute("UPDATE supertrend_tokens SET status='REMOVED'")

# ------------------------------------------------------------------ ①
print("== ① 时序①：纸盘 1s 循环先转 OPEN 并合并（~98% 常态）==")
open_coexist("T1")
FAKE.fill_limit("T1")
_market_open("T1", "LONG", 0.001)
paper_convert_and_merge("T1")
stl.sync_supertrend_live()
s = state("T1")
report("成交后", "T1", s)
check("①-a SL 总覆盖 == 交易所持仓", abs(s["sl"] - s["pos"]) < 1e-9,
      f"SL {s['sl']:.6g} vs 持仓 {s['pos']:.6g}")
check("①-b live 只剩 1 条记录，数量 == 纸盘",
      len(s["live"]) == 1 and abs(float(s["live"][0]["quantity"]) - s["pq"]) < 1e-9,
      str([(r['id'], r['status'], r['quantity']) for r in s['live']]))

# ------------------------------------------------------------------ ②
print("\n== ② 时序②：60s 对账先撞上窄窗口（交易所已成交、纸盘还是 PENDING）==")
open_coexist("T2")
FAKE.fill_limit("T2")
_market_open("T2", "LONG", 0.001)
stl.sync_supertrend_live()          # 对账先跑（纸盘仍 PENDING → 走成交分支）
paper_convert_and_merge("T2")       # 之后纸盘才转 OPEN 并合并
stl.sync_supertrend_live()
s = state("T2")
report("合并后", "T2", s)
check("②-a SL 总覆盖 == 交易所持仓（不是超额）", abs(s["sl"] - s["pos"]) < 1e-9,
      f"SL {s['sl']:.6g} vs 持仓 {s['pos']:.6g}")
check("②-b 只剩 1 条 OPEN 记录（不重复记账）",
      len(s["live"]) == 1 and s["live"][0]["status"] == "OPEN",
      str([(r['id'], r['status']) for r in s['live']]))
check("②-c 记录数量 == 纸盘", len(s["live"]) == 1
      and abs(float(s["live"][0]["quantity"]) - s["pq"]) < 1e-9,
      f"{s['live'][0]['quantity'] if s['live'] else None} vs 纸盘 {s['pq']:.6g}")

# ------------------------------------------------------------------ ⑤
print("\n== ⑤ SL 触发后：纸盘/实盘能不能收敛（两个时序各验一次）==")
for tok in ("T1", "T2"):
    for a in list(sl_algos(tok)):
        _close(tok, a["quantity"], a["position_side"])
        a["status"] = "FILLED"
    PRICE[tok] = 58900.0
    stl.sync_supertrend_live()
    s = state(tok)
    check(f"⑤ {tok} 纸盘收敛为 CLOSED/CANCELED",
          all(r["status"] in ("CLOSED", "CANCELED") for r in s["paper"]),
          str(s["paper"]))
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        allrows = [dict(r) for r in lc.execute(
            "SELECT id,status FROM live_positions WHERE token=? AND "
            "strategy='supertrend'", (tok,))]
    check(f"⑤ {tok} live 记录全部 CLOSED",
          all(r["status"] == "CLOSED" for r in allrows), str(allrows))

# ------------------------------------------------------------------ ⑥
print("\n== ⑥ 并存但限价单没成交：新逻辑一个字都不能动 ==")
open_coexist("T3")
stl.sync_supertrend_live()
s = state("T3")
check("⑥ 限价单仍在 + 1 条 OPEN + 1 条 PENDING",
      len([o for o in FAKE.orders if o["status"] == "NEW"]) == 1
      and len(s["live"]) == 2
      and len([r for r in s["live"] if r["status"] == "OPEN"]) == 1
      and len([r for r in s["live"] if r["status"] == "PENDING"]) == 1,
      f"限价单 {len([o for o in FAKE.orders if o['status'] == 'NEW'])} 张, live "
      f"{[(r['id'], r['status']) for r in s['live']]}")

# ------------------------------------------------------------------ ⑦
print("\n== ⑦ 反方向残留记录（换向场景）必须保留 ==")
reset("T4")
insert_paper("T4", "OPEN", 0.001, 60000.0, 59000.0)
for _ in range(3):
    stl.sync_supertrend_live()
with storage.get_conn(config.LIVE_DB_PATH) as lc:
    storage.live_position_insert(lc, {
        "paper_position_id": 99999, "token": "T4", "symbol": "T4USDT",
        "side": "SHORT", "strategy": "supertrend", "entry_price": 61000.0,
        "quantity": 0.001, "leverage": 3, "margin_usd": 50.0,
        "exchange_orders_json": "{}", "tp1_done": 0, "tp2_done": 0,
        "status": "OPEN"})
FAKE.positions.append({"symbol": "T4USDT", "quantity": 0.001,
                       "entry_price": 61000.0, "mark_price": 61000.0,
                       "unrealized_pnl": 0.0, "leverage": 3,
                       "position_side": "SHORT"})
stl.sync_supertrend_live()
with storage.get_conn(config.LIVE_DB_PATH) as lc:
    rows = [dict(r) for r in lc.execute(
        "SELECT id,side,status FROM live_positions WHERE token='T4'")]
check("⑦ 反方向残留记录被保留（没被新逻辑误收敛）",
      any(r["side"] == "SHORT" and r["status"] == "OPEN" for r in rows),
      str([(r['id'], r['side'], r['status']) for r in rows]))

# ------------------------------------------------------------------ ⑧
print("\n== ⑧ 部分成交：用这条挂单自己的成交量 ==")
open_coexist("T5")
FAKE.fill_limit_partial("T5", 0.0004)   # 0.001 的挂单只成交 0.0004（单子仍在挂）
_market_open("T5", "LONG", 0.0004)      # 交易所持仓 = 市价腿 0.001 + 0.0004
stl.sync_supertrend_live()
s = state("T5")
report("部分成交后", "T5", s)
check("⑧-a 挂单腿记录量 == 实际成交的 0.0004（不是整仓 0.0014、也不是挂单原量 0.001）",
      len(s["live"]) == 2 and abs(float(s["live"][1]["quantity"]) - 0.0004) < 1e-9,
      str([(r['id'], r['status'], r['quantity']) for r in s['live']]))
check("⑧-b SL 总覆盖 == 交易所持仓（市价腿 0.001 + 这条腿 0.0004）",
      abs(s["sl"] - s["pos"]) < 1e-9,
      f"SL {s['sl']:.6g} vs 持仓 {s['pos']:.6g}")

# ------------------------------------------------------------------ ⑨
print("\n== ⑨ 被跟单实盘占用：重复记录不收敛、SL 不重挂（A5′）==")
open_coexist("T6")
FAKE.fill_limit("T6")
_market_open("T6", "LONG", 0.001)
stl.sync_supertrend_live()          # 先走成交分支
paper_convert_and_merge("T6")       # 纸盘合并 → 出现指向已合并行的重复记录
with storage.get_conn(config.LIVE_DB_PATH) as lc:
    storage.live_position_insert(lc, {
        "paper_position_id": 77777, "token": "T6", "symbol": "T6USDT",
        "side": "LONG", "strategy": "alphafox", "entry_price": 60000.0,
        "quantity": 0.001, "leverage": 3, "margin_usd": 50.0,
        "exchange_orders_json": "{}", "tp1_done": 0, "tp2_done": 0,
        "status": "OPEN"})
n_sl_before = len(sl_algos("T6"))
stl.sync_supertrend_live()
s = state("T6")
check("⑨-a 占用期间：重复记录未被收敛（仍 2 条 OPEN）",
      len(s["live"]) == 2, str([(r['id'], r['status']) for r in s['live']]))
check("⑨-b 占用期间：SL 没被重挂（张数不变）",
      len(sl_algos("T6")) == n_sl_before,
      f"{n_sl_before} → {len(sl_algos('T6'))}")
# 释放占用（跟单平掉）→ 下一轮自愈
with storage.get_conn(config.LIVE_DB_PATH) as lc:
    lc.execute("UPDATE live_positions SET status='CLOSED' WHERE token='T6' "
               "AND strategy='alphafox'")
stl.sync_supertrend_live()
s = state("T6")
check("⑨-c 释放后自愈：重复记录被收敛，只剩 1 条",
      len(s["live"]) == 1, str([(r['id'], r['status'], r['quantity'])
                                for r in s['live']]))
check("⑨-d 释放后 SL 总覆盖 == 交易所持仓（交接完成）",
      abs(s["sl"] - s["pos"]) < 1e-9,
      f"SL {s['sl']:.6g} vs 持仓 {s['pos']:.6g}")
with storage.get_conn(config.LIVE_DB_PATH) as _lc:
    _logs = [dict(r) for r in _lc.execute(
        "SELECT action, details_json FROM live_trade_log WHERE token='T6' "
        "ORDER BY id").fetchall()]


def _reason(r):
    try:
        return (json.loads(r["details_json"]) if r["details_json"] else {}
                ).get("reason", "")
    except ValueError:
        return ""


check("⑨-e 覆盖量交接留痕（撤旧止损 + 重挂，原因写明交接）",
      any(r["action"] == "sl_cancel" and "交接" in _reason(r) for r in _logs)
      and any(r["action"] == "sl_update" and "交接" in _reason(r) for r in _logs),
      str([(r["action"], _reason(r)[:18]) for r in _logs[-6:]]))
check("⑨-f 重复记录被撤的单也留痕",
      any(r["action"] == "sl_cancel" and "重复记录收敛" in _reason(r)
          for r in _logs),
      str([(r["action"], _reason(r)[:18]) for r in _logs[-6:]]))

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
