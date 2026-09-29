"""⚡ 快速跟随 全链路走查（跑真引擎，不摆中间态）。

`test_alphafox_trade` 第 58 段把实盘层桩掉了（单元测试要隔离交易所接口），
`test_alphafox_live` 第 65 段又只调实盘层函数 —— 两边拼起来才是一条完整链路。
这里用**真** alphafox_live（只换掉 exchange 与标记价）走一遍：

  Q1 dry_run 预览：不写库、不下单、不碰交易所
  Q2 真跑：纸盘新建 + 托管；实盘过闸门后由**随后那一轮对账**补到目标量
  Q3 幂等：再点一次 → 已跟，不重复开、不重复下单
  Q4 交易员加仓后再点 → 纸盘追平 + 实盘对账补齐
  Q5 交易所杠杆 ≠ 设置 → 该币实盘失败 + quick_follow_blocked 日志（纸盘照建）
  Q6 交易所已有我们的仓（记录丢失）→ 认领：比例按现状冻结，零下单
  Q7 交易所持仓远超"我们会开的量"→ 失败不认领（防认领手动仓）
  Q8 余额变了：已有仓的比例 k 不受影响（开仓冻结）
"""
import io
import json
import os
import sys
import tempfile
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
_T = tempfile.mkdtemp(prefix="af_qf_")
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
BAL = 500.0            # 我的可用余额
MARGIN = 100000.0      # 交易员保证金余额 → k = 0.005
K = BAL / MARGIN
TR = "bn-QF"


def _mark(tok):
    return PRICE.get(tok)


aft.get_mark_price = _mark
aft.has_tradable_perp = lambda tok: True
afl.get_mark_price = _mark
aft._map_cache = {}          # 符号映射：identity（TOKXUSDT → TOKX）


class FAKE:
    """币安 mock：持仓 + 市价单 + 杠杆 + 可用余额"""
    positions = []
    orders = []
    lev = {}
    balance = BAL
    fail_positions = False
    _id = 9000

    @classmethod
    def get_account_info(cls):
        return {"available_balance": cls.balance, "equity": cls.balance}

    @classmethod
    def get_balance(cls):
        return cls.balance

    @classmethod
    def get_positions(cls):
        if cls.fail_positions:
            raise RuntimeError("api down")
        return [dict(p) for p in cls.positions if abs(p["quantity"]) > 1e-9]

    get_positions_or_none = classmethod(lambda c: c.get_positions())
    get_last_error = classmethod(lambda c: "")
    _round_qty = staticmethod(lambda symbol, qty: round(qty, 8))
    _round_qty_down = staticmethod(lambda symbol, qty: round(qty, 8))

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
                              "leverage": cls.lev.get(sym, 5),
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

    @classmethod
    def add_manual(cls, tok, side, qty, lev):
        sym = f"{tok}USDT"
        cls.positions.append({"symbol": sym, "quantity": qty,
                              "entry_price": PRICE.get(tok, 0.0),
                              "mark_price": PRICE.get(tok, 0.0),
                              "unrealized_pnl": 0.0, "leverage": lev,
                              "position_side": side})


afl.exchange = FAKE

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def snap(positions, margin=MARGIN):
    return {"data": {"traderName": "快速跟随测试", "marginBalance": margin,
                     "positions": positions}}


def tp(symbol, side, qty, avg, mark, lev=5):
    return {"symbol": symbol, "side": side, "absQty": qty, "avgPrice": avg,
            "markPrice": mark, "notional": qty * mark, "leverage": lev}


PARAMS = {"budget": 1000.0, "slip": 0.05, "fee": 0.04, "allow_open": True}
LIVE = (True, 0.0, 5.0)      # (live_enabled, max_notional, live_leverage)


def set_bal(v):
    FAKE.balance = float(v)
    afl._BAL_CACHE.update({"ts": 0.0, "v": 0.0})


def set_live(enabled):
    with storage.get_conn() as c:
        storage.trading_settings_update(
            c, {"alphafox_live_enabled": bool(enabled),
                "alphafox_live_leverage": 5.0,
                "alphafox_live_max_notional": 0.0})


def set_alpha(v):
    with storage.get_conn() as c:
        storage.trading_settings_update(c, {"alphafox_live_ratio": v})


def paper(tok):
    with storage.get_conn() as c:
        r = c.execute("SELECT * FROM trade_positions WHERE token=? "
                      "AND strategy='alphafox' ORDER BY id DESC LIMIT 1",
                      (tok,)).fetchone()
        return dict(r) if r else None


def live(tok):
    with storage.get_conn(config.LIVE_DB_PATH) as c:
        r = c.execute("SELECT * FROM live_positions WHERE token=? "
                      "AND strategy='alphafox' ORDER BY id DESC LIMIT 1",
                      (tok,)).fetchone()
        return dict(r) if r else None


print("========== Q1. dry_run 预览：不写库、不碰交易所 ==========")
aft.set_follow(TR, "快速跟随测试", "", True)
set_live(True)
PRICE["QF1"], PRICE["QF2"] = 100.0, 50.0
_s = snap([tp("QF1USDT", "LONG", 20, 100.0, 100.0),
           tp("QF2USDT", "SHORT", 40, 50.0, 50.0)])
r = aft.quick_follow(TR, _s, PARAMS, dry_run=True)
check("Q1-a 两个币都预览成「新建」",
      [x["action"] for x in r["results"]] == ["新建", "新建"], str(r))
check("Q1-b 没建纸盘仓", paper("QF1") is None and paper("QF2") is None)
check("Q1-c 交易所一个单都没下", FAKE.orders == [], str(FAKE.orders))

print("\n========== Q2. 真跑：纸盘新建 + 实盘由对账补到目标 ==========")
r = aft.quick_follow(TR, _s, PARAMS)
check("Q2-a 纸盘两个仓都建了且托管中",
      paper("QF1") and paper("QF2")
      and int(paper("QF1")["managed"]) == 1 and int(paper("QF2")["managed"]) == 1,
      str([r["results"][0], r["results"][1]]))
check("Q2-b 纸盘量 = 交易员量 × k_qty（预算 1000 ÷ 保证金 100000 = 0.01）",
      abs(float(paper("QF1")["quantity"]) - 20 * 0.01) < 1e-9
      and abs(float(paper("QF2")["quantity"]) - 40 * 0.01) < 1e-9,
      f"{paper('QF1')['quantity']} / {paper('QF2')['quantity']}")
check("Q2-c 实盘按比例镜像（20 × k=0.005 = 0.1）",
      abs(FAKE.pos_qty("QF1") - 20 * K) < 1e-12, str(FAKE.pos_qty("QF1")))
check("Q2-d 空头也镜像（40 × 0.005 = 0.2）",
      abs(FAKE.pos_qty("QF2", "SHORT") - 40 * K) < 1e-12,
      str(FAKE.pos_qty("QF2", "SHORT")))
_lr = live("QF1")
check("Q2-e 记录冻结比例 k=0.005",
      abs(float(json.loads(_lr["exchange_orders_json"] or "{}").get("k") or 0)
          - K) < 1e-12, str(_lr["exchange_orders_json"]))
check("Q2-f 结果里实盘动作是「新建」",
      all(x.get("live") == "新建" for x in r["results"]), str(r["results"]))
check("Q2-g 每币一单（不多不少）", len(FAKE.orders) == 2, str(FAKE.orders))

print("\n========== Q3. 幂等：再点一次 → 已跟，不重复开 ==========")
_n0 = len(FAKE.orders)
r = aft.quick_follow(TR, _s, PARAMS)
check("Q3-a 两个币都返回「补齐」（已有仓）",
      [x["action"] for x in r["results"]] == ["补齐", "补齐"], str(r))
check("Q3-b 实盘侧回报「已跟」",
      all(x.get("live") == "已跟" for x in r["results"]), str(r["results"]))
check("Q3-c 一个单都没再下", len(FAKE.orders) == _n0, str(FAKE.orders[_n0:]))
check("Q3-d 纸盘仓没被重复建（每币 1 条活跃）",
      sum(1 for t in ("QF1", "QF2")
          if paper(t) and paper(t)["status"] in ("OPEN", "PARTIAL")) == 2)
check("Q3-e 实盘记录也没多出来",
      len([1 for t in ("QF1", "QF2") if live(t)]) == 2)

print("\n========== Q4. 交易员加仓后再点 → 纸盘追平 + 实盘补齐 ==========")
_s2 = snap([tp("QF1USDT", "LONG", 50, 100.0, 100.0)])
_n0 = len(FAKE.orders)
r = aft.quick_follow(TR, _s2, PARAMS)
check("Q4-a 纸盘追平到 50 × 0.01 = 0.5",
      abs(float(paper("QF1")["quantity"]) - 0.5) < 1e-9,
      str(paper("QF1")["quantity"]))
check("Q4-b 实盘补到 50 × 0.005 = 0.25",
      abs(FAKE.pos_qty("QF1") - 50 * K) < 1e-12, str(FAKE.pos_qty("QF1")))
check("Q4-c 只下了一笔补仓单", len(FAKE.orders) - _n0 == 1,
      str(FAKE.orders[_n0:]))
check("Q4-d QF2 不在这次快照里 → 原仓不动（不误平）",
      abs(FAKE.pos_qty("QF2", "SHORT") - 40 * K) < 1e-12,
      str(FAKE.pos_qty("QF2", "SHORT")))

print("\n========== Q5. 交易所杠杆 ≠ 设置 → 该币实盘失败 + 记日志 ==========")
PRICE["QF3"] = 10.0
FAKE.add_manual("QF3", "LONG", 100.0, 20)     # 用户自己开的仓，杠杆 20 ≠ 设置 5
r = aft.quick_follow(TR, snap([tp("QF3USDT", "LONG", 500, 10.0, 10.0)]), PARAMS)
check("Q5-a 该币实盘失败（原因带杠杆）",
      r["results"][0].get("live") and "杠杆" in r["results"][0]["live"],
      str(r["results"][0]))
check("Q5-b 纸盘照建（纸盘是我们自己的账）",
      paper("QF3") is not None, str(r["results"][0]))
with storage.get_conn(config.LIVE_DB_PATH) as _c:
    _lg = [dict(x) for x in _c.execute(
        "SELECT * FROM live_trade_log WHERE token='QF3' AND "
        "action='quick_follow_blocked' ORDER BY id DESC LIMIT 1")]
check("Q5-c 写了 quick_follow_blocked 日志（页面/事后可查）",
      bool(_lg) and "杠杆" in (json.loads(
          _lg[0]["details_json"] or "{}").get("reason") or ""), str(_lg))
check("Q5-d 用户的手动仓没被动（数量/杠杆都没改）",
      abs(FAKE.pos_qty("QF3") - 100.0) < 1e-9
      and FAKE.positions[-1]["leverage"] == 20, str(FAKE.positions[-1]))

print("\n========== Q6. 交易所已有我们的仓（记录丢失）→ 认领，零下单 ==========")
PRICE["QF4"] = 100.0
FAKE.positions.append({"symbol": "QF4USDT", "quantity": 30 * K,
                       "entry_price": 100.0, "mark_price": 100.0,
                       "unrealized_pnl": 0.0, "leverage": 5,
                       "position_side": "LONG"})       # 记录丢了，仓还在
_n0 = len(FAKE.orders)
r = aft.quick_follow(TR, snap([tp("QF4USDT", "LONG", 30, 100.0, 100.0)]), PARAMS)
check("Q6-a 认领成功（返回新建/已跟，不是失败）",
      r["results"][0]["action"] == "新建" and r["results"][0].get("live"),
      str(r["results"][0]))
check("Q6-b 记了一个单都没下（比例按现状冻结）", len(FAKE.orders) == _n0,
      str(FAKE.orders[_n0:]))
_lr4 = live("QF4")
check("Q6-c 记录量 = 交易所现状 0.15",
      abs(float(_lr4["quantity"]) - 30 * K) < 1e-12, str(_lr4["quantity"]))
check("Q6-d k 按现状冻结 = 0.15 ÷ 交易员 30 = 0.005",
      abs(float(json.loads(_lr4["exchange_orders_json"] or "{}").get("k") or 0)
          - K) < 1e-12, str(_lr4["exchange_orders_json"]))

print("\n========== Q7. 交易所持仓远超预期 → 失败不认领（防认领手动仓）==========")
PRICE["QF5"] = 100.0
FAKE.positions.append({"symbol": "QF5USDT", "quantity": 20.0,
                       "entry_price": 100.0, "mark_price": 100.0,
                       "unrealized_pnl": 0.0, "leverage": 5,
                       "position_side": "LONG"})       # 目标会是 20×0.005=0.1
r = aft.quick_follow(TR, snap([tp("QF5USDT", "LONG", 20, 100.0, 100.0)]), PARAMS)
check("Q7-a 判「不像我们的」→ 实盘失败",
      r["results"][0].get("live") and "非镜像" in r["results"][0]["live"],
      str(r["results"][0]))
check("Q7-b 手动仓原封不动", abs(FAKE.pos_qty("QF5") - 20.0) < 1e-9,
      str(FAKE.pos_qty("QF5")))
check("Q7-c 实盘没给这个币建记录", live("QF5") is None, str(live("QF5")))

print("\n========== Q8. 余额变了：已有仓的比例 k 不受影响（开仓冻结）==========")
set_bal(50.0)                                    # 可用余额 500 → 50
_n0 = len(FAKE.orders)
afl.sync_alphafox_live(*LIVE)
check("Q8-a 对账不下单（目标只跟交易员量 × 冻结 k 走）",
      len(FAKE.orders) == _n0, str(FAKE.orders[_n0:]))
check("Q8-b 实盘数量仍 0.25（交易员 50 × 冻结 k=0.005）",
      abs(FAKE.pos_qty("QF1") - 50 * K) < 1e-12, str(FAKE.pos_qty("QF1")))
set_bal(BAL)

print("\n========== Q9. 存量仓豁免的币：快速跟随要解豁免并真跟 ==========")
PRICE["QF6"] = 100.0
# 造一个"实盘开关 0→1 之前就存在"的纸盘仓（豁免语义：不镜像）
with storage.get_conn() as _c:
    _pid = _c.execute(
        "INSERT INTO trade_positions (token, symbol, side, status, mode, "
        "margin_amount, leverage, notional, quantity, entry_price, "
        "current_price, strategy, closed_qty, signal_snapshot, managed) "
        "VALUES ('QF6','QF6USDT','LONG','OPEN','paper',50,5,250,0.2,100,100,"
        "'alphafox',0,?,1)",
        (json.dumps({"trader_id": TR, "k_qty": 0.01,
                     "trader_margin": MARGIN}),)).lastrowid
    storage.trading_settings_update(
        _c, {"alphafox_live_exempt_paper_ids": json.dumps([f"QF6|LONG#{_pid}"])})
check("Q9-a 前置：该币在豁免清单里", afl._exempt_has("QF6", "LONG"))
afl.sync_alphafox_live(*LIVE)
check("Q9-b 豁免仓对账不镜像（这就是豁免的语义）", FAKE.pos_qty("QF6") == 0.0,
      str(FAKE.pos_qty("QF6")))
_n0 = len(FAKE.orders)
r = aft.quick_follow(TR, snap([tp("QF6USDT", "LONG", 20, 100.0, 100.0)]), PARAMS)
check("Q9-c 快速跟随解掉豁免", not afl._exempt_has("QF6", "LONG"))
check("Q9-d 实盘真跟上了（20 × 0.005 = 0.1）",
      abs(FAKE.pos_qty("QF6") - 20 * K) < 1e-12, str(FAKE.pos_qty("QF6")))
check("Q9-e 只下了一笔", len(FAKE.orders) - _n0 == 1, str(FAKE.orders[_n0:]))

print("\n========== Q11. 该币被主流趋势实盘占用 → 说清楚，不假装已跟 ==========")
PRICE["QF8"] = 100.0
with storage.get_conn(config.SUPERTREND_DB) as _c:      # 超势开着该币的实盘
    _c.executescript("""
    CREATE TABLE IF NOT EXISTS supertrend_tokens (
        token TEXT PRIMARY KEY, status TEXT DEFAULT 'ACTIVE',
        live_mode INTEGER DEFAULT 0);
    """)
    _c.execute("INSERT OR REPLACE INTO supertrend_tokens "
               "(token, status, live_mode) VALUES ('QF8','ACTIVE',1)")
with storage.get_conn() as _c:                          # 且超势有活跃纸盘仓
    _c.execute("INSERT INTO trade_positions (token, symbol, side, status, mode, "
               "margin_amount, leverage, notional, quantity, entry_price, "
               "current_price, strategy, closed_qty) "
               "VALUES ('QF8','QF8USDT','LONG','OPEN','paper',50,3,150,1,100,100,"
               "'supertrend',0)")
_n0 = len(FAKE.orders)
r = aft.quick_follow(TR, snap([tp("QF8USDT", "LONG", 20, 100.0, 100.0)]), PARAMS)
check("Q11-a 实盘如实回报「被主流趋势占用」",
      "主流趋势" in (r["results"][0].get("live") or ""), str(r["results"][0]))
check("Q11-b 没给该币下任何单", len(FAKE.orders) == _n0, str(FAKE.orders[_n0:]))
check("Q11-c 也没建实盘记录", live("QF8") is None, str(live("QF8")))

print("\n========== Q12. 存量仓（早于进程启动）+ 快速跟随 → 越过存量仓闸门 ==========")
PRICE["QF9"] = 100.0
# 跟了一周的纸盘仓（created_at 回拨一小时 = 存量仓闸门会拦它）
with storage.get_conn() as _c:
    _c.execute(
        "INSERT INTO trade_positions (token, symbol, side, status, mode, "
        "margin_amount, leverage, notional, quantity, entry_price, "
        "current_price, strategy, closed_qty, signal_snapshot, managed, "
        "created_at, updated_at) "
        "VALUES ('QF9','QF9USDT','LONG','OPEN','paper',50,5,100,0.1,100,100,"
        "'alphafox',0,?,1,datetime('now','-3600 seconds'),CURRENT_TIMESTAMP)",
        (json.dumps({"trader_id": TR, "k_qty": 0.005,
                     "trader_margin": MARGIN}),))
afl._rounds_done = 99
afl.sync_alphafox_live(*LIVE)
check("Q12-a 没有显式点击时：存量仓闸门拦下（不镜像）", FAKE.pos_qty("QF9") == 0.0,
      str(FAKE.pos_qty("QF9")))
_n0 = len(FAKE.orders)
r = aft.quick_follow(TR, snap([tp("QF9USDT", "LONG", 40, 100.0, 100.0)]), PARAMS)
check("Q12-b 快速跟随越过存量仓闸门：实盘真建仓（40 × 0.005 = 0.2）",
      abs(FAKE.pos_qty("QF9") - 40 * K) < 1e-12, str(FAKE.pos_qty("QF9")))
check("Q12-c 只下了一笔", len(FAKE.orders) - _n0 == 1, str(FAKE.orders[_n0:]))

print("\n========== Q13. 交易员杠杆比我们高 → 跟不动，如实回报（不谎报「新建」）==========")
# 我们按比例跟 = 名义跟着交易员走，但保证金要用**我们的**杠杆算：
#   需要保证金 = 我的余额 × 交易员有效杠杆 ÷ 我的杠杆
# 交易员 10x 有效杠杆、我们设 5x → 需要 2 倍余额 → 开不动（用户语义：开不动就不开）
PRICE["QFA"] = 100.0
set_bal(200.0)
_n0 = len(FAKE.orders)
r = aft.quick_follow(TR, snap([tp("QFAUSDT", "LONG", 10000, 100.0, 100.0,
                                  lev=10)]), PARAMS)
check("Q13-a 实盘如实回报「资金不够」",
      "资金不够" in (r["results"][0].get("live") or ""), str(r["results"][0]))
check("Q13-b 没下任何单", len(FAKE.orders) == _n0, str(FAKE.orders[_n0:]))
check("Q13-c 纸盘照建（纸盘有自己的预算，与实盘杠杆无关）",
      paper("QFA") is not None, str(r["results"][0]))
# 把我们的杠杆提到 20x（够覆盖他的 10x）→ 同一币再点一次就能跟
with storage.get_conn(config.ALPHAFOX_DB) as _c:
    _c.execute("INSERT OR REPLACE INTO alphafox_token_settings "
               "(token, live_notional, live_leverage, updated_at) "
               "VALUES ('QFA', NULL, 20.0, ?)", (int(time.time()),))
r = aft.quick_follow(TR, snap([tp("QFAUSDT", "LONG", 10000, 100.0, 100.0,
                                  lev=10)]), PARAMS)
check("Q13-d 杠杆提到 20x 后：跟上了（10000 × k=0.002 × 100 = 2000U 名义）",
      abs(FAKE.pos_qty("QFA") - 20.0) < 1e-9
      and r["results"][0].get("live") == "新建",
      f"{FAKE.pos_qty('QFA')} / {r['results'][0]}")
set_bal(BAL)

print("\n========== Q15. 开仓比例自洽：实盘/纸盘 = 我的余额 ÷ 纸盘预算 ==========")
# 纸盘 k_qty = 预算 1000 ÷ 保证金 10w = 0.01；实盘 k = 余额 500 ÷ 10w = 0.005
# → 同一笔跟单，实盘数量应当**正好**是纸盘的一半（与价格无关）
_p = paper("QF1")
_l = live("QF1")
_net = float(_p["quantity"]) - float(_p["closed_qty"] or 0)
check("Q15-a 实盘量 / 纸盘净量 = 0.005 ÷ 0.01 = 0.5",
      abs(float(_l["quantity"]) / _net - 0.5) < 1e-9,
      f"实盘 {_l['quantity']} / 纸盘 {_net}")
check("Q15-b 两者都等于「交易员量 × 各自的 k」（50 × 0.01 / 50 × 0.005）",
      abs(_net - 0.5) < 1e-9 and abs(float(_l["quantity"]) - 0.25) < 1e-12,
      f"{_net} / {_l['quantity']}")

print("\n========== Q16. 映射比例 α：快速跟随的实盘也按 α 折算 ==========")
PRICE["QFC"] = 100.0
set_alpha(0.5)                 # 只跟他一半
set_live(True)
r = aft.quick_follow(TR, snap([tp("QFCUSDT", "LONG", 20, 100.0, 100.0)]), PARAMS)
check("Q16-a 实盘按半量开：20 × (0.5 × 500/100000) = 0.05（全额是 0.1）",
      abs(FAKE.pos_qty("QFC") - 0.05) < 1e-12, str(FAKE.pos_qty("QFC")))
check("Q16-b 纸盘不受 α 影响（纸盘有自己的预算口径）",
      abs(float(paper("QFC")["quantity"]) - 0.2) < 1e-9,
      str(paper("QFC")["quantity"]))
_lr = live("QFC")
check("Q16-c 记录冻结 α=0.5 + k=0.0025",
      abs(float(json.loads(_lr["exchange_orders_json"] or "{}").get("alpha") or 0)
          - 0.5) < 1e-9
      and abs(float(json.loads(_lr["exchange_orders_json"] or "{}").get("k") or 0)
              - 0.0025) < 1e-12, str(_lr["exchange_orders_json"]))
_n0 = len(FAKE.orders)
set_alpha(1.0)                 # 改回全跟 → 已有仓不动
afl.sync_alphafox_live(*LIVE)
check("Q16-d 改 α 不动已有仓（冻结语义）",
      len(FAKE.orders) == _n0 and abs(FAKE.pos_qty("QFC") - 0.05) < 1e-12,
      f"{FAKE.pos_qty('QFC')} {FAKE.orders[_n0:]}")

print("\n========== Q10. 实盘开关关着：纸盘照建，实盘如实回报（不假装已跟）==========")
set_live(False)
PRICE["QF7"] = 100.0
_n0 = len(FAKE.orders)
r = aft.quick_follow(TR, snap([tp("QF7USDT", "LONG", 20, 100.0, 100.0)]), PARAMS)
check("Q10-a 纸盘照建", paper("QF7") is not None, str(r["results"][0]))
check("Q10-b 实盘如实回报「开关关闭」",
      "开关关闭" in (r["results"][0].get("live") or ""), str(r["results"][0]))
check("Q10-c 没下任何单", len(FAKE.orders) == _n0, str(FAKE.orders[_n0:]))
check("Q10-d 没建实盘记录", live("QF7") is None, str(live("QF7")))
set_live(True)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
