"""主流趋势策略：纸盘 / 实盘一致性 —— 三块方案的单元测试。

**本文件不修改任何生产代码。** 三块判据先在本文件里实现（生产落地后把
`_limit_gone` / `_is_orphan_live_pend` / `_need_qty_writeback` 改成从
`supertrend_live` 导入即可，断言不用动）。

背景：反转市价开会同时产生「市价腿 OPEN」+「挂单腿 PENDING」两条纸盘行。
实盘镜像层在这个并存窗口有三处不同步：

  (a′) 成交判据错用"交易所有持仓" → 每轮把市价腿的持仓误判成"挂单成交了"
       → 撤掉币安上真正等回调的限价单 + 多记一条 OPEN（实盘盈亏双计）
   2    live 挂单记录指向的纸盘行已结束时没人收敛 → 僵尸 PENDING
   3    合并后 live OPEN 记录的数量不刷新 → 后续合法减仓被"疑似手动仓"守卫跳过

运行：python extra/test_coexist_sync.py
"""
import io
import os
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

_TMP = tempfile.mkdtemp(prefix="coexist_ut_")
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

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


class FakeExchange:
    """币安 mock。挂单只在 status=='NEW' 时可见（与真实 openOrders 一致）。"""

    def __init__(self):
        self.positions, self.orders, self.algo_orders = [], [], []
        self._next_id = 9000
        self.fail_open_orders = False
        self.raise_open_orders = False

    def get_positions(self):
        return [dict(p) for p in self.positions if abs(p["quantity"]) > 1e-9]

    def get_positions_or_none(self):
        """判"持仓消失"要用它：失败返回 None（与"确实没有持仓"的 [] 区分）"""
        if getattr(self, "fail_positions", False):
            return None
        return self.get_positions()

    def get_open_orders(self, token=None):
        """现有接口：失败与"确实没有挂单"都返回 []（无法区分）"""
        if self.raise_open_orders:
            raise RuntimeError("network down")
        if self.fail_open_orders:
            return []
        rows = [o for o in self.orders if o["status"] == "NEW"]
        if token:
            rows = [o for o in rows if o["symbol"] == f"{token.upper()}USDT"]
        # 带币安真实字段名 orderId / executedQty（判成交的代码读这两个）
        return [{"orderId": o["order_id"], "symbol": o["symbol"],
                 "executedQty": float(o.get("executed_qty") or 0)} for o in rows]

    def get_open_orders_or_none(self, token=None):
        """生产版要新增的接口：**失败返回 None**，与"没有挂单"的 [] 区分开。
        判"我们那张单还在不在"必须用它——否则查单失败会被当成"单子没了"
        = 判成交 = 回到原来的 bug。"""
        if self.raise_open_orders or self.fail_open_orders:
            return None
        return self.get_open_orders(token)

    def get_algo_orders(self, token=None):
        return [o for o in self.algo_orders if o["status"] == "NEW"]

    def get_last_error(self):
        return ""

    def get_account_info(self):
        return {"equity": 1000.0, "margin_balance": 500.0,
                "available_balance": 500.0, "unrealized_pnl": 0.0}

    def get_balance(self):
        return 1000.0

    def _round_qty(self, symbol, qty):
        return qty

    def set_leverage(self, symbol, leverage):
        return True

    def _market_open(self, token, side, qty):
        price = PRICE.get(token, 100.0)
        sym = f"{token}USDT"
        for p in self.positions:
            if p["symbol"] == sym and p["position_side"] == side:
                p["quantity"] += qty
                return {"order_id": 1, "status": "FILLED", "price": price,
                        "quantity": qty}
        self.positions.append({"symbol": sym, "quantity": qty,
                               "entry_price": price, "mark_price": price,
                               "unrealized_pnl": 0.0, "leverage": 3,
                               "position_side": side})
        return {"order_id": 1, "status": "FILLED", "price": price, "quantity": qty}

    def market_open_long(self, token, qty):
        return self._market_open(token, "LONG", qty)

    def market_open_short(self, token, qty):
        return self._market_open(token, "SHORT", qty)

    def limit_open(self, token, side, qty, price, position_side="LONG"):
        self._next_id += 1
        self.orders.append({"order_id": self._next_id, "symbol": f"{token}USDT",
                            "side": side, "type": "LIMIT", "price": price,
                            "quantity": qty, "position_side": position_side,
                            "status": "NEW"})
        return {"order_id": self._next_id, "status": "NEW", "price": price,
                "quantity": qty}

    def stop_loss_order(self, token, side, qty, stop_price, position_side="LONG"):
        self._next_id += 1
        self.algo_orders.append({"order_id": self._next_id,
                                 "symbol": f"{token}USDT", "side": side,
                                 "type": "STOP_MARKET", "triggerPrice": stop_price,
                                 "quantity": qty,
                                 "position_side": position_side, "status": "NEW"})
        return {"order_id": self._next_id, "status": "NEW"}

    def cancel_order(self, token, order_id):
        for o in self.orders + self.algo_orders:
            if o["order_id"] == order_id:
                o["status"] = "CANCELED"
        return True

    def cancel_all_orders(self, token):
        for o in self.orders + self.algo_orders:
            if o["symbol"] == f"{token}USDT":
                o["status"] = "CANCELED"
        return True

    def close_position_market(self, token, qty, position_side="LONG"):
        price = PRICE.get(token, 100.0)
        for p in self.positions:
            if p["symbol"] == f"{token}USDT" and p["position_side"] == position_side:
                p["quantity"] = max(p["quantity"] - qty, 0)
        return {"order_id": 1, "price": price, "quantity": qty, "status": "FILLED"}

    def live_orders(self, token=None):
        rows = [o for o in self.orders if o["status"] == "NEW"]
        return [o for o in rows if not token or o["symbol"] == f"{token}USDT"]

    def fill_limit(self, token):
        for o in self.orders:
            if o["symbol"] == f"{token}USDT" and o["status"] == "NEW" \
                    and o["type"] == "LIMIT":
                o["status"] = "FILLED"
                return True
        return False

    def fill_limit_partial(self, token, qty):
        for o in self.orders:
            if o["symbol"] == f"{token}USDT" and o["status"] == "NEW" \
                    and o["type"] == "LIMIT":
                o["executed_qty"] = qty
                return True
        return False

    def pos_qty(self, token, side="LONG"):
        for p in self.positions:
            if p["symbol"] == f"{token}USDT" and p["position_side"] == side:
                return p["quantity"]
        return 0.0


FAKE = FakeExchange()
stl.exchange = FAKE

# ============================================================================
# 三块方案的判据：直接导入生产实现（落地后不再手写，测试锁的是真代码）
# ============================================================================
from supertrend_live import (                               # noqa: E402
    _limit_fill_state, _is_orphan_live_pend, _need_qty_writeback)


def _limit_filled(token, oid):
    """薄封装：测试里按"单号"判成交，生产接口是 (是否成交, 这条挂单自己的量)"""
    return _limit_fill_state(token, {"limit": oid})[0]


# ============================================================================
print("== 1. (a′) 成交判据 _limit_filled 单元测试 ==")
FAKE.orders.clear()
FAKE._next_id = 9000
r1 = FAKE.limit_open("AAA", "BUY", 1.0, 100.0)
FAKE.limit_open("BBB", "BUY", 1.0, 100.0)          # 干扰项：别的币也有挂单
check("1-a 单子还在挂且零成交 → 判『没成交』",
      _limit_filled("AAA", r1["order_id"]) is False)
check("1-h 单子还在挂但已部分成交（executedQty>0）→ 判『已成交』",
      FAKE.fill_limit_partial("AAA", 0.4) is True
      and _limit_filled("AAA", r1["order_id"]) is True)
FAKE.fill_limit("AAA")
check("1-b 单子整张成交（从挂单列表消失）→ 判『已成交』",
      _limit_filled("AAA", r1["order_id"]) is True)
check("1-d 别的币的挂单/成交不干扰本币判决（BBB 仍在挂）",
      _limit_filled("AAA", r1["order_id"]) is True
      and len(FAKE.live_orders("BBB")) == 1)
check("1-c 没单号 → 判『没成交』（保守）", _limit_filled("AAA", None) is False)
FAKE.raise_open_orders = True
check("1-e 查单抛异常 → 判『没成交』（保守，绝不误判成成交）",
      _limit_filled("AAA", r1["order_id"]) is False)
FAKE.raise_open_orders = False
FAKE.fail_open_orders = True
check("1-f 查单接口失败 → 判『没成交』（靠 None 把失败与空列表分开，"
      "这是生产版必须新增 get_open_orders_or_none 的原因）",
      _limit_filled("AAA", r1["order_id"]) is False)
check("1-g 对照：用现有的 get_open_orders 时，失败(空列表)会被误判成『已成交』"
      "——这正是原 bug 的形态",
      stl.exchange.get_open_orders("AAA") == []
      and int(r1["order_id"]) not in set())
FAKE.fail_open_orders = False

# ============================================================================
print("\n== 2. 第二块：孤儿 live 挂单记录判据 ==")
check("2-a 指向当前活跃 PENDING → 不是孤儿",
      _is_orphan_live_pend(7, {"paper_position_id": 7}) is False)
check("2-b 指向别的纸盘行 → 是孤儿",
      _is_orphan_live_pend(7, {"paper_position_id": 8}) is True)
check("2-c 纸盘已无 PENDING（None）→ 是孤儿",
      _is_orphan_live_pend(None, {"paper_position_id": 7}) is True)
check("2-d 没有 live 挂单记录 → 不是孤儿", _is_orphan_live_pend(7, None) is False)

# ============================================================================
print("\n== 3. 第三块：记录数量回写判据 ==")
check("3-a 交易所一致但记录旧 → 需要回写",
      _need_qty_writeback(0.001, 0.002, True, True) is True)
check("3-b 记录已跟平 → 不回写（不做无谓写库）",
      _need_qty_writeback(0.002, 0.002, True, True) is False)
check("3-c 交易所查不到 → 不回写", _need_qty_writeback(0.001, 0.002, False, True) is False)
check("3-d 纸盘不新鲜 → 不回写（沿用原函数的新鲜度闸门）",
      _need_qty_writeback(0.001, 0.002, True, False) is False)


# ============================================================================
# 端到端：三块一起 → 纸盘/实盘四项一致
# ============================================================================
def insert_paper(token, status, side="LONG", qty=1.0, entry=100.0, sl=95.0,
                 limit_price=None, lev=3.0, margin=50.0):
    with storage.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO trade_positions (token, symbol, side, status, mode, "
            "margin_amount, leverage, notional, quantity, entry_price, "
            "limit_price, current_price, stop_loss_price, strategy) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (token, f"{token}USDT", side, status, "paper", margin, lev,
             margin * lev, qty, entry, limit_price,
             entry if status in ("OPEN", "PARTIAL") else (limit_price or entry),
             sl, "supertrend"))
        return cur.lastrowid


def live_rows_of(token):
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        return [dict(r) for r in lc.execute(
            "SELECT * FROM live_positions WHERE token=? ORDER BY id", (token,))]


def paper_active_qty(token):
    with storage.get_conn() as mc:
        r = mc.execute(
            "SELECT COALESCE(SUM(quantity-IFNULL(closed_qty,0)),0) q "
            "FROM trade_positions WHERE token=? AND strategy='supertrend' "
            "AND status IN ('OPEN','PARTIAL')", (token,)).fetchone()
        return float(r["q"] or 0)


def paper_pending_cnt(token):
    with storage.get_conn() as mc:
        return mc.execute(
            "SELECT COUNT(*) c FROM trade_positions WHERE token=? "
            "AND strategy='supertrend' AND status='PENDING'",
            (token,)).fetchone()["c"]


def reset_token(token, price):
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
    FAKE.raise_open_orders = False
    stl._rounds_done = 0


def consistency(token):
    """四项一致性指标 + live 记录明细"""
    p_qty = paper_active_qty(token)
    p_pend = paper_pending_cnt(token)
    e_qty = FAKE.pos_qty(token)
    e_lim = len(FAKE.live_orders(token))
    live = [r for r in live_rows_of(token)
            if r["status"] in ("OPEN", "PARTIAL", "PENDING")]
    return {"p_qty": p_qty, "e_qty": e_qty, "p_pend": p_pend, "e_lim": e_lim,
            "live": live,
            "l_open": [r for r in live if r["status"] in ("OPEN", "PARTIAL")],
            "l_pend": [r for r in live if r["status"] == "PENDING"]}


def open_coexist(token, price=60000.0):
    """反转那一轮：市价腿 + 挂单腿，两条腿都镜像到币安"""
    reset_token(token, price)
    insert_paper(token, "OPEN", qty=0.001, entry=price, sl=price * 0.9833)
    insert_paper(token, "PENDING", qty=0.001, entry=None, sl=price * 0.9833,
                 limit_price=price * 0.9917)
    for _ in range(3):
        stl.sync_supertrend_live()


# 说明：三块改动已落到生产代码（exchange.get_open_orders_or_none +
# supertrend_live 的 _limit_gone / _is_orphan_live_pend / _need_qty_writeback 及其接线），
# 本套件直接跑真代码，不再 monkey-patch。「会不会被改回旧行为」由
# extra/_mutation_check.py 的 M125~M127 保证。

with storage.get_conn(config.SUPERTREND_DB) as c:
    c.execute("UPDATE supertrend_tokens SET status='REMOVED'")


def settle(token, fill_limit_qty=0.0):
    """模拟限价成交（单子消失 + 持仓增加）+ 纸盘 1s 循环转 OPEN + 合并"""
    if fill_limit_qty > 0:
        FAKE.fill_limit(token)
        FAKE._market_open(token, "LONG", fill_limit_qty)
        with storage.get_conn() as mc:
            mc.execute("UPDATE trade_positions SET status='OPEN', "
                       "entry_price=limit_price, current_price=limit_price "
                       "WHERE token=? AND status='PENDING'", (token,))
        with storage.get_conn() as mc:
            supertrend_strategy._merge_positions(mc, token)
    stl.sync_supertrend_live()


print("\n== 4. 端到端：三块一起（纸盘/实盘四项一致）==")

print("---- 4.1 并存 + 挂单成交 ----")
open_coexist("BTC")
settle("BTC", fill_limit_qty=0.001)
c = consistency("BTC")
print(f"     纸盘 {c['p_qty']:.6g}/{c['p_pend']} 交易所 {c['e_qty']:.6g}/{c['e_lim']} "
      f"live {[(r['id'], r['status'], r['quantity']) for r in c['live']]}")
check("4.1-a 持仓量一致", abs(c["p_qty"] - c["e_qty"]) < 1e-9,
      f"{c['p_qty']} vs {c['e_qty']}")
check("4.1-b 挂单数一致", c["p_pend"] == c["e_lim"], f"{c['p_pend']} vs {c['e_lim']}")
check("4.1-c live 记录数与纸盘活跃行一致", len(c["live"]) == 1, f"{len(c['live'])} 条")
check("4.1-d 无僵尸 PENDING", not c["l_pend"], str(c["l_pend"]))
check("4.1-e 记录数量 == 交易所持仓",
      c["l_open"] and abs(float(c["l_open"][0]["quantity"]) - c["e_qty"]) < 1e-9,
      f"{c['l_open'][0]['quantity'] if c['l_open'] else None} vs {c['e_qty']}")

print("---- 4.2 并存 + 挂单未成交（应完全不动）----")
open_coexist("ETH", price=3000.0)
settle("ETH")
c = consistency("ETH")
check("4.2-a 限价单仍在交易所", c["e_lim"] == 1, f"{c['e_lim']} 张")
check("4.2-b 挂单记录仍是 PENDING 且没被误收敛",
      len(c["l_pend"]) == 1 and len(c["l_open"]) == 1,
      str([(r['id'], r['status']) for r in c['live']]))
check("4.2-c 持仓量一致", abs(c["p_qty"] - c["e_qty"]) < 1e-9,
      f"{c['p_qty']} vs {c['e_qty']}")

print("---- 4.3 非并存（只有挂单）+ 成交（照旧走转 OPEN）----")
reset_token("SOL", 200.0)
insert_paper("SOL", "PENDING", qty=1.0, entry=None, sl=190.0, limit_price=195.0)
for _ in range(3):
    stl.sync_supertrend_live()
settle("SOL", fill_limit_qty=1.0)
c = consistency("SOL")
check("4.3-a 1 条 OPEN、无僵尸",
      len(c["l_open"]) == 1 and not c["l_pend"],
      str([(r['id'], r['status']) for r in c['live']]))
check("4.3-b 记录数量 == 交易所持仓",
      abs(float(c["l_open"][0]["quantity"]) - c["e_qty"]) < 1e-9,
      f"{c['l_open'][0]['quantity']} vs {c['e_qty']}")

print("---- 4.4 纸盘撤单（没成交）→ 记录收敛 + 孤儿限价单撤掉 ----")
reset_token("XRP", 50.0)
insert_paper("XRP", "PENDING", qty=10.0, entry=None, sl=47.0, limit_price=49.0)
for _ in range(3):
    stl.sync_supertrend_live()
check("4.4-前置 交易所已挂 1 张限价单", len(FAKE.live_orders("XRP")) == 1,
      str(len(FAKE.live_orders("XRP"))))
with storage.get_conn() as mc:
    mc.execute("UPDATE trade_positions SET status='CANCELED' "
               "WHERE token='XRP' AND status='PENDING'")
insert_paper("XRP", "OPEN", qty=10.0, entry=50.0, sl=47.0)
stl.sync_supertrend_live()
c = consistency("XRP")
check("4.4-a 挂单记录收敛", not c["l_pend"],
      str([(r['id'], r['status']) for r in c['live']]))
check("4.4-b 孤儿限价单被撤", c["e_lim"] == 0, f"剩 {c['e_lim']} 张")

print("---- 4.5 并存合并后 → 后续减仓不再被'疑似手动仓'守卫误跳过 ----")
open_coexist("DOGE", price=1.0)
settle("DOGE", fill_limit_qty=0.001)
c = consistency("DOGE")
check("4.5-前置 合并后记录数量已跟上",
      abs(float(c["l_open"][0]["quantity"]) - c["e_qty"]) < 1e-9,
      f"{c['l_open'][0]['quantity']} vs {c['e_qty']}")
with storage.get_conn() as mc:                    # 模拟纸盘减半
    mc.execute("UPDATE trade_positions SET quantity=quantity/2 "
               "WHERE token='DOGE' AND strategy='supertrend' "
               "AND status IN ('OPEN','PARTIAL')")
stl.sync_supertrend_live()
check("4.5-a 减仓真的执行了（没被当成手动仓跳过）",
      abs(FAKE.pos_qty("DOGE") - paper_active_qty("DOGE")) < 1e-9,
      f"交易所 {FAKE.pos_qty('DOGE'):.6g} vs 纸盘 {paper_active_qty('DOGE'):.6g}")

print("\n== 5. 生产接线核对（三块判据确实被调用了）==")
with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "supertrend_live.py"), encoding="utf-8") as f:
    _sl_src = f.read()
check("5-a _mirror_pending 的成交判据用了 _limit_fill_state",
      "_limit_fill_state(token, _load_orders(live_pend))" in _sl_src
      and "if live_pend and ex_row and filled:" in _sl_src)
check("5-b _sync_token 里有孤儿挂单记录收敛",
      "_is_orphan_live_pend(paper_pend[\"id\"] if paper_pend else None, live_pend)"
      in _sl_src)
check("5-c _align_open 里有数量回写", "_need_qty_writeback(" in _sl_src)
check("5-d 判成交用的是 None 语义的查询（不是会被误判的 get_open_orders）",
      "exchange.get_open_orders_or_none(token)" in _sl_src
      and "exchange.get_open_orders(token)" not in _sl_src)
with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "exchange.py"), encoding="utf-8") as f:
    _ex_src = f.read()
check("5-e exchange.py 已新增 get_open_orders_or_none",
      "def get_open_orders_or_none(" in _ex_src)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
