"""端到端验证：alphafox_trade 写纸盘仓 → alphafox_live 能否查到并镜像。

覆盖本轮修复核心：两边 strategy 值必须一致（'alphafox'）。
链路：process(快照) 开纸盘仓 → apply_signal 镜像实盘 → sync 对账 → CLOSE 跟随退出。
"""
import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

_TMP = tempfile.mkdtemp(prefix="e2e_alf_")
config.DB_PATH = os.path.join(_TMP, "main.db")
config.SUPERTREND_DB = os.path.join(_TMP, "supertrend.db")
config.LIVE_DB_PATH = os.path.join(_TMP, "live.db")
config.ALPHAFOX_DB = os.path.join(_TMP, "alphafox.db")
config.BINANCE_HEDGE_MODE = True

import storage
import alphafox_trade
import alphafox_live

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
storage.init_onchain_live_db()
alphafox_trade.init_db()
with storage.get_conn(config.SUPERTREND_DB) as conn:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS supertrend_tokens (
        token TEXT PRIMARY KEY, status TEXT DEFAULT 'ACTIVE',
        live_mode INTEGER DEFAULT 0
    );
    """)
with storage.get_conn(config.SUPERTREND_DB) as conn:
    conn.executescript(storage.SUPERTREND_SCHEMA if hasattr(storage, "SUPERTREND_SCHEMA") else "")
    conn.execute("CREATE TABLE IF NOT EXISTS supertrend_status (token TEXT PRIMARY KEY)")

PRICE = {"BTC": 50000.0}
alphafox_trade.get_mark_price = lambda token: PRICE.get(token)


class FakeExchange:
    def __init__(self):
        self.positions = []
        self.calls = []
        self._last_error_msg = ""
        self.last_error = ""
        # 比例模型：实盘层要读"我的可用余额"（k = 余额 ÷ 交易员保证金）
        self.balance = 800.0

    def get_account_info(self):
        return {"available_balance": self.balance, "equity": self.balance}

    def get_balance(self):
        return self.balance

    def get_positions(self):
        return [dict(p) for p in self.positions if abs(p["quantity"]) > 1e-9]

    def get_last_error(self):
        return self.last_error

    def set_leverage(self, symbol, lev):
        self.calls.append(("set_lev", symbol, lev))
        return True

    def _open(self, token, side, qty):
        price = PRICE.get(token, 100.0)
        self.calls.append(("open", side, token, round(qty, 8)))
        for p in self.positions:
            if p["symbol"] == f"{token}USDT" and p["position_side"] == side:
                p["quantity"] += qty
                p["mark_price"] = price
                return {"order_id": 1, "status": "FILLED", "price": price, "quantity": qty}
        self.positions.append({"symbol": f"{token}USDT", "quantity": qty,
                               "entry_price": price, "mark_price": price,
                               "unrealized_pnl": 0.0, "leverage": 50,
                               "position_side": side})
        return {"order_id": 2, "status": "FILLED", "price": price, "quantity": qty}

    def market_open_long(self, token, qty):
        return self._open(token, "LONG", qty)

    def market_open_short(self, token, qty):
        return self._open(token, "SHORT", qty)

    def close_position_market(self, token, qty, position_side="LONG"):
        self.calls.append(("close", token, round(qty, 8), position_side))
        for p in self.positions:
            if p["symbol"] == f"{token}USDT" and p["position_side"] == position_side:
                p["quantity"] = max(p["quantity"] - qty, 0)
        return {"order_id": 3, "price": PRICE.get(token, 100.0),
                "quantity": qty, "status": "FILLED"}

    def pos_qty(self, token, side="LONG"):
        for p in self.positions:
            if p["symbol"] == f"{token}USDT" and p["position_side"] == side:
                return p["quantity"]
        return 0.0


FAKE = FakeExchange()
alphafox_live.exchange = FAKE

TRADER = "hl-0x354dd2b1719ee86a2f4e04befa137c1cbe80c667"
# 与生产探测线程构造的 trade_params 同形（live_notional 现在是**单笔上限**，
# 0 = 不限；规模由"我的可用余额 ÷ 交易员保证金"的比例决定）
PARAMS = {"budget": 1000.0, "slip": 0.05, "fee": 0.04, "allow_open": True,
          "live_notional": 0.0}
PASS, FAIL = 0, 0

# 比例模型：我的可用余额 800U ÷ 交易员保证金 100000U → k = 0.008
# （交易员 10 枚 BTC → 我们跟 0.08 枚）
K_REF = 800.0 / 100000.0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


_TP_HIST: dict = {}   # 假平台成交流水（跨轮累积）：{(token, side): [point, ...]}


def _synth_tp(positions, no_close=False):
    """照真样本合成 tradePoints：持仓各一条 OPEN；这轮没了的仓补一条 CLOSE
    （真平台会记，实测 7/7）。模拟"平台抖动"（丢行但无平仓记录）→ no_close=True。"""
    import time as _t
    keys = set()
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        sym = str(p.get("symbol") or "").upper()
        if not sym:
            continue
        keys.add((sym[:-4] if sym.endswith("USDT") else sym,
                  str(p.get("side") or "").upper()))
    now_ms = int(_t.time() * 1000)

    def _pt(k, action, ts):
        return {"symbol": f"{k[0]}USDT",
                "side": "BUY" if k[1] == "LONG" else "SELL",
                "action": action, "quantity": 1.0, "price": 1.0, "timeMs": ts}

    for k in sorted(keys):
        lst = _TP_HIST.setdefault(k, [])
        if not lst or lst[-1]["action"] != "OPEN":
            lst.append(_pt(k, "OPEN", now_ms - 60000))
    for k, lst in sorted(_TP_HIST.items()):
        if k not in keys and lst and lst[-1]["action"] == "OPEN" and not no_close:
            lst.append(_pt(k, "CLOSE", now_ms))
    return {f"{k[0]}USDT": [dict(x) for x in lst]
            for k, lst in _TP_HIST.items() if lst}


def snap(positions, polled="p1", no_close=False):
    return {"data": {"marginBalance": 100000.0, "traderName": "测试交易员",
                     "polledAt": polled, "positions": positions,
                     "tradePoints": _synth_tp(positions, no_close)}}


BTC_POS = [{"symbol": "BTC", "baseAsset": "BTC", "side": "LONG", "absQty": 10.0,
            "avgPrice": 49900.0, "markPrice": 50000.0, "notional": 500000.0,
            "unrealizedPnl": 1000.0, "leverage": 25}]

print("== 1. 首轮基线（空仓）==")
sigs = alphafox_trade.process(TRADER, snap([], "p0"), PARAMS)
check("首轮无信号", sigs == [], str(sigs))

print("== 2. 交易员开仓 → 纸盘写仓（strategy='alphafox'）==")
sigs = alphafox_trade.process(TRADER, snap(BTC_POS, "p1"), PARAMS)
check("产生 OPEN 信号且已执行",
      len(sigs) == 1 and sigs[0]["action"] == "OPEN" and sigs[0]["executed"],
      str(sigs))
with storage.get_conn() as conn:
    row = conn.execute(
        "SELECT * FROM trade_positions WHERE status IN ('OPEN','PARTIAL')").fetchone()
check("纸盘行 strategy='alphafox'", row and row["strategy"] == "alphafox",
      str(dict(row) if row else None))
check("纸盘行有数量/入场价", row and float(row["quantity"] or 0) > 0
      and float(row["entry_price"] or 0) > 0)
paper_qty = float(row["quantity"])

print("== 3. apply_signal → 实盘镜像（本次修复的核心路径）==")
alphafox_live.apply_signal(sigs[0], True, 0.0, 50.0)
storage.init_onchain_live_db()
with storage.get_conn(config.LIVE_DB_PATH) as lc:
    live = lc.execute("SELECT * FROM live_positions WHERE strategy='alphafox'").fetchall()
check("live 记录已写入（strategy='alphafox'）", len(live) == 1,
      str([dict(r) for r in live]))
lv = dict(live[0]) if live else {}
# 比例模型：目标 = 交易员量(10) × k(0.008) = 0.08
qty_ref = 10.0 * K_REF
check(f"live 数量 = 交易员 10 × k=0.008 = {qty_ref}",
      abs(float(lv.get("quantity") or 0) - qty_ref) < 1e-12, str(lv.get("quantity")))
check("实盘杠杆 = 50", float(lv.get("leverage") or 0) == 50, str(lv.get("leverage")))
check("交易所已开仓", abs(FAKE.pos_qty("BTC") - qty_ref) < 1e-12,
      str(FAKE.pos_qty("BTC")))
_ord = json.loads(lv.get("exchange_orders_json") or "{}")
check("k 冻结值 = 余额 800 ÷ 保证金 100000",
      abs(float(_ord.get("k") or 0) - K_REF) < 1e-12
      and abs(float(_ord.get("balance_ref") or 0) - 800.0) < 1e-9
      and abs(float(_ord.get("trader_margin_ref") or 0) - 100000.0) < 1e-9,
      str(_ord))

print("== 4. sync 对账：能查到纸盘仓（旧代码此处查不到）==")
before = len(FAKE.calls)
alphafox_live.sync_alphafox_live(True, 0.0, 50.0)
# 目标与现价无关（交易员量 × 冻结 k）→ 对账什么都不该做
check("对账不动仓（目标 == 现状）",
      abs(FAKE.pos_qty("BTC") - qty_ref) < 1e-12, str(FAKE.pos_qty("BTC")))
check("对账未产生多余交易所动作（仅对齐/无动作）",
      len(FAKE.calls) - before <= 1, str(FAKE.calls[before:]))

print("== 5. 交易员加仓 → 按比例补齐到新目标 ==")
BTC_ADD = [dict(BTC_POS[0], absQty=20.0, notional=1000000.0)]
sigs = alphafox_trade.process(TRADER, snap(BTC_ADD, "p2"), PARAMS)
check("ADD 信号已执行", len(sigs) == 1 and sigs[0]["action"] == "ADD"
      and sigs[0]["executed"], str(sigs))
alphafox_live.apply_signal(sigs[0], True, 0.0, 50.0)
# 加仓量 = 新目标 − 交易所现仓（量化过）→ 加完直接落在目标上，对账无事可做
check("实盘加仓 → 直接对齐到「交易员 20 × k=0.008 = 0.16」",
      abs(FAKE.pos_qty("BTC") - 20.0 * K_REF) < 1e-12,
      f"{FAKE.pos_qty('BTC')} vs {20.0 * K_REF}")
with storage.get_conn(config.LIVE_DB_PATH) as lc:
    lv = dict(lc.execute("SELECT * FROM live_positions WHERE strategy='alphafox' "
                         "AND status='OPEN'").fetchone())
check("live 记录数量跟上新目标",
      abs(float(lv.get("quantity") or 0) - 20.0 * K_REF) < 1e-12,
      str(lv.get("quantity")))

print("== 6. 交易员平仓 → 实盘跟随全平 ==")
# 快照异常防护：positions 清空需连续 2 轮确认
alphafox_trade.process(TRADER, snap([], "p3"), PARAMS)
sigs = alphafox_trade.process(TRADER, snap([], "p4"), PARAMS)
check("CLOSE 信号已执行", len(sigs) == 1 and sigs[0]["action"] == "CLOSE"
      and sigs[0]["executed"], str(sigs))
alphafox_live.apply_signal(sigs[0], True, 0.0, 50.0)
check("交易所已平干净", FAKE.pos_qty("BTC") == 0.0, str(FAKE.pos_qty("BTC")))
with storage.get_conn(config.LIVE_DB_PATH) as lc:
    lv = dict(lc.execute("SELECT * FROM live_positions WHERE strategy='alphafox' "
                         "ORDER BY id DESC LIMIT 1").fetchone())
check("live 记录 CLOSED", lv["status"] == "CLOSED", lv["status"])

print("== 7. 存量仓豁免：开关前的仓不镜像 ==")
sigs = alphafox_trade.process(TRADER, snap(BTC_POS, "p5"), PARAMS)
check("新仓 OPEN 信号", len(sigs) == 1 and sigs[0]["action"] == "OPEN", str(sigs))
with storage.get_conn() as conn:
    row = conn.execute("SELECT id, token, side FROM trade_positions "
                       "WHERE strategy='alphafox' "
                       "AND status IN ('OPEN','PARTIAL')").fetchone()
key = f"{row['token']}|{row['side']}#{row['id']}"
with storage.get_conn() as conn:
    import json
    storage.trading_settings_update(
        conn, {"alphafox_live_exempt_paper_ids": json.dumps([key])})
check("豁免 key 与 web 记录格式一致（token|side#纸盘仓id）",
      key.startswith("BTC|LONG#"), key)
alphafox_live.apply_signal(sigs[0], True, 0.0, 50.0)
check("豁免仓 OPEN：不镜像", FAKE.pos_qty("BTC") == 0.0, str(FAKE.pos_qty("BTC")))
alphafox_live.sync_alphafox_live(True, 0.0, 50.0)
check("豁免仓 sync 补开：也不镜像", FAKE.pos_qty("BTC") == 0.0, str(FAKE.pos_qty("BTC")))

print("== 8. 存量仓闸门：早于本次进程启动的仓不镜像（发版/重启后只跟新仓） ==")
# 清掉 7 的豁免清单，避免干扰
with storage.get_conn() as conn:
    import json as _json
    storage.trading_settings_update(
        conn, {"alphafox_live_exempt_paper_ids": _json.dumps([])})
# 清场：交易员平掉第 7 组的仓（快照异常防护需连续 2 轮确认）
alphafox_trade.process(TRADER, snap([], "p6a"), PARAMS)
sigs = alphafox_trade.process(TRADER, snap([], "p6b"), PARAMS)
check("清场：CLOSE 已执行",
      len(sigs) == 1 and sigs[0]["action"] == "CLOSE" and sigs[0]["executed"],
      str(sigs))
# 交易员开仓 → 纸盘写仓 → created_at 回拨一小时（模拟"上次进程就在的存量仓"）
sigs = alphafox_trade.process(TRADER, snap(BTC_POS, "p7"), PARAMS)
check("存量场景：OPEN 信号已执行",
      len(sigs) == 1 and sigs[0]["action"] == "OPEN" and sigs[0]["executed"], str(sigs))
with storage.get_conn() as conn:
    conn.execute("UPDATE trade_positions SET created_at=datetime('now','-3600 seconds') "
                 "WHERE token='BTC' AND strategy='alphafox' "
                 "AND status IN ('OPEN','PARTIAL')")
alphafox_live._rounds_done = 99
alphafox_live.apply_signal(sigs[0], True, 0.0, 50.0)
check("存量仓：信号 OPEN 不镜像", FAKE.pos_qty("BTC") == 0.0, str(FAKE.pos_qty("BTC")))
alphafox_live.sync_alphafox_live(True, 0.0, 50.0)
check("存量仓：对账也不补开", FAKE.pos_qty("BTC") == 0.0, str(FAKE.pos_qty("BTC")))
# 交易员再平仓（2 轮确认）→ 重新开仓：新仓 created_at 晚于启动 → 正常镜像
alphafox_trade.process(TRADER, snap([], "p8a"), PARAMS)
sigs = alphafox_trade.process(TRADER, snap([], "p8b"), PARAMS)
check("清场 2：CLOSE 已执行",
      len(sigs) == 1 and sigs[0]["action"] == "CLOSE" and sigs[0]["executed"],
      str(sigs))
sigs = alphafox_trade.process(TRADER, snap(BTC_POS, "p9"), PARAMS)
check("新仓信号已执行",
      len(sigs) == 1 and sigs[0]["action"] == "OPEN" and sigs[0]["executed"],
      str(sigs))
alphafox_live.apply_signal(sigs[0], True, 0.0, 50.0)
check("启动后新仓：正常镜像", FAKE.pos_qty("BTC") > 0, str(FAKE.pos_qty("BTC")))

print("== 9. 取消跟单 → 仓位全部保留（转非托管）；重新跟单 → 恢复托管继续跟随 ==")
_res = alphafox_trade.set_follow(TRADER, "熬鹰链上fomo", "", False)
check("取消跟单：回报取消托管笔数", _res.get("managed_off", 0) >= 1, str(_res))
check("取消跟单：实盘仓保留（不平仓）", FAKE.pos_qty("BTC") > 0,
      str(FAKE.pos_qty("BTC")))
with storage.get_conn() as conn:
    _rows = [dict(r) for r in conn.execute(
        "SELECT status, managed FROM trade_positions WHERE token='BTC' "
        "AND strategy='alphafox' ORDER BY id DESC LIMIT 1")]
check("取消跟单：纸盘仓仍 OPEN 且转非托管",
      _rows and _rows[0]["status"] == "OPEN" and int(_rows[0]["managed"]) == 0,
      str(_rows))
with storage.get_conn(config.LIVE_DB_PATH) as lc:
    _lv = [dict(r) for r in lc.execute(
        "SELECT status FROM live_positions WHERE token='BTC' "
        "AND strategy='alphafox' ORDER BY id DESC LIMIT 1")]
check("取消跟单：live 记录仍 OPEN（仓位还在）",
      _lv and _lv[0]["status"] == "OPEN", str(_lv))
# 取消后本轮快照竞态：即便又来一份新快照，也不许产生任何信号
alphafox_trade._prev_snap.clear()
sigs = alphafox_trade.process(TRADER, snap(BTC_POS, "p10"), PARAMS)
check("取消后：新快照不产生信号", sigs == [], str(sigs))

_res2 = alphafox_trade.set_follow(TRADER, "熬鹰链上fomo", "", True)
check("重新跟单：回报恢复托管笔数", _res2.get("reattach", 0) >= 1, str(_res2))
alphafox_trade.process(TRADER, snap(BTC_POS, "p11"), PARAMS)   # 首轮：追平
with storage.get_conn() as conn:
    _r2 = dict(conn.execute(
        "SELECT status, managed FROM trade_positions WHERE token='BTC' "
        "AND strategy='alphafox' ORDER BY id DESC LIMIT 1").fetchone())
check("重新跟单：转回托管中", _r2["status"] == "OPEN" and int(_r2["managed"]) == 1,
      str(_r2))
# 恢复托管后：交易员平仓照常跟随（纸盘 + 实盘都平）
alphafox_trade.process(TRADER, snap([], "p12"), PARAMS)
sigs = alphafox_trade.process(TRADER, snap([], "p13"), PARAMS)
check("恢复托管后：交易员平仓仍跟随（CLOSE executed）",
      len(sigs) == 1 and sigs[0]["action"] == "CLOSE" and sigs[0]["executed"],
      str(sigs))
with storage.get_conn() as conn:
    _row9 = dict(conn.execute(
        "SELECT status FROM trade_positions WHERE token='BTC' "
        "AND strategy='alphafox' ORDER BY id DESC LIMIT 1").fetchone())
check("恢复托管后：纸盘已平", _row9["status"] == "CLOSED", str(_row9))
alphafox_live.apply_signal(sigs[0], True, 0.0, 50.0)
check("恢复托管后：实盘已平", FAKE.pos_qty("BTC") == 0.0, str(FAKE.pos_qty("BTC")))

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
