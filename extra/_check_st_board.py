"""验证趋势实盘页（web.py）：/api/supertrend/board 聚合 / overview 缓存 / 页面接线。

web.py 不能 import（模块级起线程），沿用 _check_* 的"从源码抽真码块 exec"套路。
关键口径：
- 纸盘 = trade_positions(strategy='supertrend')；实盘 = live_positions(同策略)
- 交易所数字取不到（账户 ok=False）时，实盘列一律 None → 页面显示"—"，**不许写 0**
- 只认本策略的行（同一张表里有 alphafox/agent 的行，必须过滤干净）
"""
import io
import json
import os
import re
import sys
import tempfile
import threading
import types
from datetime import datetime, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)

import config

_TMP = tempfile.mkdtemp(prefix="stboard_")
config.DB_PATH = os.path.join(_TMP, "main.db")
config.SUPERTREND_DB = os.path.join(_TMP, "supertrend.db")
config.LIVE_DB_PATH = os.path.join(_TMP, "live.db")
config.ALPHAFOX_DB = os.path.join(_TMP, "alphafox.db")

import storage
import supertrend_live as stl

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


# ---- 假 exchange（board 内部 import exchange 取账户/持仓）----
FAKE_POSITIONS = [
    {"symbol": "AAAUSDT", "quantity": 2.0, "entry_price": 100.0,
     "mark_price": 110.0, "unrealized_pnl": 20.0, "leverage": 5,
     "position_side": "LONG"},
    # 空单：币安 positionAmt 是负数 → 页面必须显示绝对值
    {"symbol": "BBBUSDT", "quantity": -1.0, "entry_price": 50.0,
     "mark_price": 45.0, "unrealized_pnl": 5.0, "leverage": 3,
     "position_side": "SHORT"},
    # 手动仓：没有对应的 live 记录 → 不该出现在本页持仓里（按纸盘行来）
    {"symbol": "ZZZUSDT", "quantity": 9.0, "entry_price": 10.0,
     "mark_price": 10.0, "unrealized_pnl": 0.0, "leverage": 1,
     "position_side": "LONG"},
]
_ACCT_OK = {"v": True}
fake_ex = types.ModuleType("exchange")
fake_ex.get_account_info = lambda: ({"equity": 1234.5, "available_balance": 1000.0,
                                     "unrealized_pnl": 25.0} if _ACCT_OK["v"] else None)
fake_ex.get_positions = lambda: ([dict(p) for p in FAKE_POSITIONS]
                                 if _ACCT_OK["v"] else [])
fake_ex.get_balance = lambda: (1000.0 if _ACCT_OK["v"] else 0)
fake_ex.get_last_error = lambda: ""
sys.modules["exchange"] = fake_ex

# ---- 假 supertrend_strategy（暂停币会实调币安标记价，测试里必须桩掉）----
_fake_st = types.ModuleType("supertrend_strategy")
_fake_st.ck_calls = []
_fake_st.init_db = lambda: None
_fake_st.get_mark_price = lambda token: (_fake_st.ck_calls.append(token), 999.0)[1]
sys.modules["supertrend_strategy"] = _fake_st

with storage.get_conn() as conn:
    conn.executescript(storage.SCHEMA)
storage.init_onchain_live_db()

# ---- 造数据：3 个监控币（实盘 / 暂停 / 普通）----
import time as _time
now = int(_time.time())
with storage.get_conn(config.SUPERTREND_DB) as conn:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS supertrend_tokens (
            token TEXT PRIMARY KEY, status TEXT DEFAULT 'ACTIVE', added_at INTEGER,
            removed_at INTEGER, notional_usd REAL, sl_tolerance_pct REAL,
            order_tolerance_pct REAL, leverage REAL, direction TEXT DEFAULT 'BOTH',
            flip_market_open INTEGER DEFAULT 0, flip_market_tolerance_pct REAL,
            paused INTEGER DEFAULT 0, sort_order INTEGER, live_mode INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS supertrend_status (
            token TEXT PRIMARY KEY, trend TEXT, upper_band REAL, lower_band REAL,
            last_candle_ts INTEGER, last_sl REAL, last_price REAL, state TEXT,
            fetch_error TEXT, updated_at INTEGER, forming_trend TEXT,
            forming_upper_band REAL, forming_lower_band REAL);
        CREATE TABLE IF NOT EXISTS supertrend_close_snaps (
            position_id INTEGER PRIMARY KEY, token TEXT, trend TEXT,
            upper_band REAL, lower_band REAL, created_at INTEGER);
    """)
    conn.execute("INSERT INTO supertrend_tokens (token, status, added_at, "
                 "notional_usd, leverage, paused, live_mode, sort_order) "
                 "VALUES ('AAA','ACTIVE',?,NULL,NULL,0,1,0)", (now,))
    conn.execute("INSERT INTO supertrend_tokens (token, status, added_at, "
                 "notional_usd, leverage, paused, live_mode, sort_order) "
                 "VALUES ('BBB','ACTIVE',?,80.0,5.0,0,1,1)", (now,))
    conn.execute("INSERT INTO supertrend_tokens (token, status, added_at, "
                 "paused, live_mode, sort_order) VALUES ('CCC','ACTIVE',?,1,0,2)",
                 (now,))
    conn.execute("INSERT INTO supertrend_status (token, trend, upper_band, "
                 "lower_band, last_price, state, updated_at, fetch_error) "
                 "VALUES ('AAA','UP',112.0,100.0,110.0,'HOLDING',?,NULL)", (now,))
    conn.execute("INSERT INTO supertrend_status (token, trend, upper_band, "
                 "lower_band, last_price, state, updated_at, fetch_error) "
                 "VALUES ('BBB','DOWN',52.0,44.0,45.0,'PENDING',?,NULL)", (now,))
    # 暂停币：线程不刷新 → 价格是旧的，compute 会实调标记价覆盖（测试桩返回 999）
    conn.execute("INSERT INTO supertrend_status (token, trend, upper_band, "
                 "lower_band, last_price, state, updated_at, fetch_error) "
                 "VALUES ('CCC','UP',1.0,2.0,7.0,'PAUSED',?,NULL)", (now,))

with storage.get_conn() as conn:
    conn.execute("INSERT INTO trading_settings (key, value) VALUES "
                 "('strategy_initial_supertrend','2000')")
    # 本策略三行：持仓 / 挂单腿 / 已平仓；再混一行别的策略（必须被过滤）
    cur = conn.execute(
        "INSERT INTO trade_positions (token, symbol, side, status, mode, "
        "margin_amount, leverage, notional, quantity, entry_price, current_price, "
        "stop_loss_price, limit_price, unrealized_pnl, realized_pnl, pnl_pct, "
        "advice, strategy, closed_qty) "
        "VALUES ('AAA','AAAUSDT','LONG','OPEN','paper',50.0,5.0,100.0,2.0,100.0,"
        "110.0,95.0,NULL,20.0,0.0,10.0,'','supertrend',0.0)")
    pid_a = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO trade_positions (token, symbol, side, status, mode, "
        "margin_amount, leverage, notional, quantity, entry_price, current_price, "
        "stop_loss_price, limit_price, unrealized_pnl, realized_pnl, pnl_pct, "
        "advice, strategy, closed_qty) "
        "VALUES ('BBB','BBBUSDT','SHORT','PENDING','paper',40.0,3.0,120.0,1.0,"
        "100.0,50.0,60.0,50.0,0.0,0.0,0.0,'','supertrend',0.0)")
    pid_b = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO trade_positions (token, symbol, side, status, mode, "
        "margin_amount, leverage, notional, quantity, entry_price, current_price, "
        "stop_loss_price, limit_price, unrealized_pnl, realized_pnl, pnl_pct, "
        "advice, strategy, closed_qty, closed_at) "
        "VALUES ('AAA','AAAUSDT','LONG','CLOSED','paper',50.0,5.0,100.0,2.0,100.0,"
        "90.0,95.0,NULL,0.0,-12.5,-10.0,'超势反转平仓','supertrend',2.0,"
        "'2026-09-12 03:00:00')")
    pid_c = cur.lastrowid
    conn.execute(
        "INSERT INTO trade_positions (token, symbol, side, status, mode, "
        "margin_amount, leverage, notional, quantity, entry_price, current_price, "
        "stop_loss_price, unrealized_pnl, realized_pnl, pnl_pct, advice, strategy) "
        "VALUES ('ZZZ','ZZZUSDT','LONG','OPEN','paper',10.0,1.0,90.0,9.0,10.0,"
        "10.0,9.0,0.0,0.0,0.0,'','alphafox')")

with storage.get_conn(config.SUPERTREND_DB) as conn:
    conn.execute("INSERT INTO supertrend_close_snaps (position_id, token, trend, "
                 "upper_band, lower_band, created_at) VALUES (?, 'AAA','DOWN',"
                 "112.0,100.0,?)", (pid_c, now))

with storage.get_conn(config.LIVE_DB_PATH) as lc:
    storage.live_position_insert(lc, {
        "paper_position_id": pid_a, "token": "AAA", "symbol": "AAAUSDT",
        "side": "LONG", "strategy": "supertrend", "entry_price": 100.0,
        "quantity": 2.0, "leverage": 5, "margin_usd": 20.0,
        "exchange_orders_json": json.dumps({
            "open": "111", "sl": "222", "sl_price": 95.0, "sl_qty": 2.0,
            "limit": None, "limit_price": None, "limit_qty": None})})
    storage.live_position_insert(lc, {
        "paper_position_id": pid_b, "token": "BBB", "symbol": "BBBUSDT",
        "side": "SHORT", "strategy": "supertrend", "entry_price": None,
        "quantity": 1.0, "leverage": 3, "margin_usd": 40.0,
        "exchange_orders_json": json.dumps({
            "open": None, "sl": "555", "sl_price": 60.0, "sl_qty": 1.0,
            "limit": "444", "limit_price": 50.0, "limit_qty": 1.0}),
        "status": "PENDING"})
    # 已平仓实盘记录（历史面板要拿它的真实成交盈亏）
    storage.live_position_insert(lc, {
        "paper_position_id": pid_c, "token": "AAA", "symbol": "AAAUSDT",
        "side": "LONG", "strategy": "supertrend", "entry_price": 100.0,
        "quantity": 2.0, "leverage": 5, "margin_usd": 50.0})
    storage.live_position_update(lc, 3, {
        "status": "CLOSED", "realized_pnl": -11.8,
        "closed_at": "__CURRENT_TIMESTAMP__"})
    # 别的策略的行：必须被策略过滤挡掉
    with storage.get_conn() as conn:
        row = conn.execute("SELECT id FROM trade_positions WHERE token='ZZZ'"
                           ).fetchone()
    storage.live_position_insert(lc, {
        "paper_position_id": row["id"], "token": "ZZZ", "symbol": "ZZZUSDT",
        "side": "LONG", "strategy": "alphafox", "entry_price": 10.0,
        "quantity": 9.0, "leverage": 1, "margin_usd": 90.0})
    storage.live_trade_log_insert(lc, {
        "token": "AAA", "strategy": "supertrend", "action": "sl_set",
        "price": 95.0, "quantity": 2.0, "paper_position_id": pid_a,
        "exchange_order_id": "222",
        "details_json": json.dumps({"reason": "补开挂止损"})})
    lc.execute("UPDATE live_trade_log SET created_at='2026-09-13 01:00:00'")
    storage.live_trade_log_insert(lc, {
        "token": "ZZZ", "strategy": "alphafox", "action": "open",
        "price": 10.0, "quantity": 9.0})

# ---- 从 web.py 抽真码块执行 ----
sys.path.insert(0, os.path.join(root, "extra"))
from _mut_src import read_module_source       # 变异注入的源码优先（否则读磁盘原文）

web_src = read_module_source("web")


def extract_func(name):
    m = re.search(rf"^def {name}\(", web_src, re.M)
    if not m:
        return None
    m2 = re.search(r"^(?:@app\.|def |class )", web_src[m.end():], re.M)
    end = m.end() + (m2.start() if m2 else len(web_src) - m.end())
    return web_src[m.start():end]


class _HTTPErr(Exception):
    def __init__(self, code, detail):
        self.code, self.detail = code, detail


NS = {"storage": storage, "config": config, "json": json,
      "datetime": datetime, "timedelta": timedelta,
      "time": __import__("time"), "HTTPException": _HTTPErr,
      "_cache": {}, "_cache_lock": threading.Lock(), "threading": threading,
      "trade_logic": __import__("trade_logic"),
      "supertrend_live": stl, "print": print,
      "__builtins__": __builtins__}
# 分页常量是模块级的（不是函数定义）→ 从源码读原值注入，测试里不另抄一份
_step_m = re.search(r"^_ST_HIS_STEP = (\d+)", web_src, re.M)
_max_m = re.search(r"^_ST_HIS_MAX = (\d+)", web_src, re.M)
NS["_ST_HIS_STEP"] = int(_step_m.group(1)) if _step_m else 0
NS["_ST_HIS_MAX"] = int(_max_m.group(1)) if _max_m else 0
missing = []
for fn in ("_af_local_ts", "_cached", "_cache_invalidate", "_st_tol",
           "_st_effective_tol", "api_live_binance_overview",
           "_supertrend_overview_compute", "api_supertrend_overview",
           "api_supertrend_board"):
    src = extract_func(fn)
    if not src:
        missing.append(fn)
        continue
    exec(compile(src, f"web.py:{fn}", "exec"), NS)
check("从 web.py 抽取到全部函数原文", not missing, str(missing))
if missing:
    print(f"结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1)

print("== 1. overview 缓存：与监控页同一份计算 ==")
ov1 = NS["api_supertrend_overview"]()
check("overview 返回 3 张卡片", len(ov1["cards"]) == 3, str(len(ov1["cards"])))
check("暂停币的实时价被回调覆盖（线程不刷新的兜底）",
      any(c["token"] == "CCC" and abs(c["last_price"] - 999.0) < 1e-9
          for c in ov1["cards"]), str([c["last_price"] for c in ov1["cards"]]))
check("暂停币只在本轮实调一次标记价", _fake_st.ck_calls == ["CCC"],
      str(_fake_st.ck_calls))
ov2 = NS["api_supertrend_overview"]()
# 分页切片是**在缓存之外**做的（每次返回新 dict，绝不原地改缓存对象）→ 判"命中缓存"
# 要看**内层载荷是不是同一份** + 币安调用次数，不能再拿最外层 dict 的身份当判据
check("第二次调用命中缓存（同一份载荷、不再触达币安）",
      ov2["cards"] is ov1["cards"] and _fake_st.ck_calls == ["CCC"],
      str(_fake_st.ck_calls))
NS["_cache_invalidate"]("supertrend_overview")
ov3 = NS["api_supertrend_overview"]()
check("失效后重算（缓存键被写操作清掉）",
      ov3 is not ov1 and _fake_st.ck_calls == ["CCC", "CCC"],
      str(_fake_st.ck_calls))

print("== 2. board 聚合：账户 / 盈亏 / 币种 / 持仓 / 历史 / 日志 ==")
board = NS["api_supertrend_board"]()
acc = board["account"]
check("账户概览来自币安账户接口（账户级）",
      abs(acc["equity"] - 1234.5) < 1e-9 and acc["ok"] is True, str(acc))
live = board["stats"]["live"]
check("趋势归属：实盘持仓 1 笔 + 挂单腿 1 笔",
      live["open_count"] == 1 and live["pending_count"] == 1, str(live))
check("趋势归属：占用保证金只算已成交（挂单腿未占地，20 不含 40）",
      abs(live["margin_used"] - 20.0) < 1e-9, str(live["margin_used"]))
check("实盘口径盈亏来自 live_positions（-11.8）",
      abs(live["pnl"] + 11.8) < 1e-9 and live["closed"] == 1 and live["wins"] == 0,
      str(live))
paper = board["stats"]["paper"]
check("纸盘口径盈亏来自 trade_positions（-12.5，笔数 1）",
      abs(paper["pnl"] + 12.5) < 1e-9 and paper["closed"] == 1, str(paper))
acct_sum = board["stats"]["account"]
check("纸盘独立账户：初始 2000 + realized(-12.5) = 1987.5",
      abs(acct_sum["initial_balance"] - 2000.0) < 1e-9
      and abs(acct_sum["realized_pnl"] + 12.5) < 1e-9, str(acct_sum))
opn = board["stats"]["open"]
check("当前浮盈：纸盘 20 / 实盘 25（AAA 20 + BBB 5）",
      abs(opn["paper_pnl"] - 20.0) < 1e-9 and abs(opn["ex_pnl"] - 25.0) < 1e-9,
      str(opn))
check("未平仓计数：纸盘持仓 1 + 挂单 1 / 实盘仓位 2",
      opn["open_paper"] == 1 and opn["open_pending"] == 1
      and opn["open_ex"] == 2, str(opn))

print("== 3. 持仓：纸盘/live/交易所 三源 join ==")
pos = {p["token"]: p for p in board["positions"]}
check("持仓/挂单腿都在（AAA+BBB），别的策略行不混进来",
      set(pos) == {"AAA", "BBB"}, str(sorted(pos)))
check("AAA 纸盘数量 + 现浮盈", abs(pos["AAA"]["quantity"] - 2.0) < 1e-9
      and abs(pos["AAA"]["unrealized_pnl"] - 20.0) < 1e-9, str(pos["AAA"]))
check("AAA 实盘数量/开仓价/浮盈来自交易所（2 / 100 / 20）",
      abs(pos["AAA"]["ex_qty"] - 2.0) < 1e-9
      and abs(pos["AAA"]["ex_entry"] - 100.0) < 1e-9
      and abs(pos["AAA"]["ex_pnl"] - 20.0) < 1e-9, str(pos["AAA"]))
check("AAA 实盘止损从 live 记录的 orders 里取（95 × 2）",
      abs(pos["AAA"]["live_sl"] - 95.0) < 1e-9
      and abs(pos["AAA"]["live_sl_qty"] - 2.0) < 1e-9
      and pos["AAA"]["live_sl_order"] == "222", str(pos["AAA"]["live_sl"]))
check("AAA 状态来自监控币种卡片（HOLDING）",
      pos["AAA"]["state"] == "HOLDING", str(pos["AAA"]["state"]))
check("BBB 是挂单腿（纸盘 PENDING + 实盘 PENDING + 限价 50）",
      pos["BBB"]["status"] == "PENDING" and pos["BBB"]["live_status"] == "PENDING"
      and abs(pos["BBB"]["live_limit_price"] - 50.0) < 1e-9, str(pos["BBB"]))
check("BBB 空单实盘数量显示绝对值", abs(pos["BBB"]["ex_qty"] - 1.0) < 1e-9,
      str(pos["BBB"]["ex_qty"]))
check("无 live 记录的币不给假实盘列（ZZZ 根本没进本页）", "ZZZ" not in pos)

print("== 3b. 孤儿实盘记录（纸盘没活跃行）要在页面上报出来 ==")
with storage.get_conn() as conn:                 # 造一条"纸盘已平、实盘没收敛"的记录
    cur = conn.execute(
        "INSERT INTO trade_positions (token, symbol, side, status, mode, "
        "margin_amount, leverage, notional, quantity, entry_price, "
        "current_price, strategy, closed_qty, realized_pnl, pnl_pct, advice, "
        "closed_at) VALUES ('CCC','CCCUSDT','LONG','CLOSED','paper',50.0,3.0,"
        "100.0,1.0,100.0,100.0,'supertrend',1.0,0.0,0.0,'','2026-09-14 01:00:00')")
    _pid_closed = cur.lastrowid
with storage.get_conn(config.LIVE_DB_PATH) as lc:
    storage.live_position_insert(lc, {
        "paper_position_id": _pid_closed, "token": "CCC", "symbol": "CCCUSDT",
        "side": "LONG", "strategy": "supertrend", "entry_price": 100.0,
        "quantity": 1.0, "leverage": 3, "margin_usd": 30.0})
b3 = NS["api_supertrend_board"]()
check("孤儿实盘记录计入 orphan_count（纸盘已平、实盘还在）",
      b3["stats"]["live"]["orphan_count"] == 1
      and b3["stats"]["live"]["orphan_tokens"] == ["CCC"],
      str(b3["stats"]["live"]))
check("孤儿不混进持仓表（持仓仍按纸盘活跃行走）",
      {p["token"] for p in b3["positions"]} == {"AAA", "BBB"},
      str(sorted({p["token"] for p in b3["positions"]})))

print("== 4. 历史：平仓快照 + 实盘真实盈亏 + 北京时间 ==")
his = board["history"]
check("历史 1 笔（只有本策略的 CLOSED）", len(his) == 1, str(len(his)))
check("历史带平仓时刻趋势快照（DOWN）",
      his[0]["close_snap"] and his[0]["close_snap"]["trend"] == "DOWN",
      str(his[0].get("close_snap")))
check("历史实盘盈亏来自 live 记录（-11.8 ≠ 纸盘 -12.5）",
      abs(float(his[0]["live_pnl"]) + 11.8) < 1e-9, str(his[0].get("live_pnl")))
check("历史时间转北京时间（UTC 03:00 → 11:00）",
      "11:00" in str(his[0]["closed_at"]), str(his[0]["closed_at"]))
check("历史保留平仓原因", his[0]["advice"] == "超势反转平仓", str(his[0]["advice"]))

print("== 5. 交易日志：只出本策略、带原因、时间转北京 ==")
logs = board["trades"]
check("日志只有 supertrend 的行（alphafox 的 open 被过滤）",
      len(logs) == 1 and logs[0]["token"] == "AAA", str(len(logs)))
check("日志动作 + 原因（sl_set / 补开挂止损）",
      logs[0]["action"] == "sl_set"
      and json.loads(logs[0]["details_json"])["reason"] == "补开挂止损",
      str(logs[0]))
check("日志时间转北京时间（UTC 01:00 → 09:00）",
      "09:00" in str(logs[0]["created_at"]), str(logs[0]["created_at"]))

print("== 6. 交易所取不到数：实盘列给 None，不许写 0 ==")
_ACCT_OK["v"] = False
try:
    b2 = NS["api_supertrend_board"]()
finally:
    _ACCT_OK["v"] = True
o2 = b2["stats"]["open"]
check("账户 ok=False 透传", b2["account_ok"] is False, str(b2["account_ok"]))
check("实盘浮盈/实盘笔数给 None（页面显示 —，不是 0）",
      o2["ex_pnl"] is None and o2["open_ex"] is None, str(o2))
check("纸盘口径不受影响（20 / 1 笔）",
      abs(o2["paper_pnl"] - 20.0) < 1e-9 and o2["open_paper"] == 1, str(o2))
check("持仓行实盘列为 None",
      all(p["ex_qty"] is None and p["ex_pnl"] is None for p in b2["positions"]),
      str([(p["token"], p["ex_qty"]) for p in b2["positions"]]))
check("实盘记录/日志照常返回（不依赖交易所接口）",
      b2["stats"]["live"]["open_count"] == 2      # AAA 持仓 + 3b 造的孤儿记录
      and b2["stats"]["live"]["pending_count"] == 1
      and b2["stats"]["live"]["orphan_count"] == 1
      and len(b2["trades"]) == 1,
      str(b2["stats"]["live"]))

print("== 7. 页面与导航接线（源码断言）==")
html = re.search(r'SUPERTREND_LIVE_HTML = """(.*?)\n"""', web_src, re.S)
check("页面常量存在", bool(html))
page = html.group(1) if html else ""
for panel in ("① 币安账户概览", "② 策略盈亏", "③ 监控币种", "④ 持仓",
              "⑤ 历史持仓", "⑥ 交易日志"):
    check(f"面板 {panel}", panel in page)
check("六个渲染函数齐", all(f"function {f}(" in page for f in
      ("renderAcct", "renderStats", "renderCards", "renderPositions",
       "renderHistory", "renderLogs")))
check("拉取 board 接口（带分页 limit）",
      "fetch('/api/supertrend/board?limit='" in page, page[:0])
check("孤儿实盘记录有告警渲染（收敛卡住时看得见）",
      "orphan_count" in page and "没有对应的活跃纸盘仓" in page)
check("轮询间隔与 overview 缓存 TTL 对齐（30s）",
      "setInterval(load, 30000)" in page, "")
check("持仓行有平仓按钮（复用 /api/live-binance/close）",
      "data-close" in page and "/api/live-binance/close" in page)
check("监控币种行点击跳监控页改参数", r"location.href=\\'/supertrend\\'" in page)
check("导航：趋势实盘进 💹 实盘 分组",
      re.search(r'💹 实盘".*?/supertrend-live', web_src, re.S) is not None)
check("路由 /supertrend-live 注册",
      '@app.get("/supertrend-live", response_class=HTMLResponse)' in web_src)
check("overview 走 _cached（两页共享一份计算）",
      '_cached("supertrend_overview", 30, _supertrend_overview_compute)' in web_src)
n_inv = web_src.count('_cache_invalidate("supertrend_overview")')
check("5 个写接口都让缓存失效（加/删/改/暂停/排序）", n_inv >= 5, str(n_inv))
check("改全局设置也失效（设置页改名义/杠杆/开关）",
      '_cache_invalidate("trading", "supertrend_overview")' in web_src)
check("⑤⑥ 分页接线：两面板独立上限 + 按 limit 拉取 + 「加载更多」按钮",
      "const PAGE_STEP = 50, PAGE_MAX = 200;" in page
      and "let _hisLimit = PAGE_STEP, _logLimit = PAGE_STEP;" in page
      and "fetch('/api/supertrend/board?limit=' + _lim)" in page
      and "renderMoreRow('his-more', 'his-cnt', 'his'" in page
      and "renderMoreRow('log-more', 'log-cnt', 'log'" in page
      and 'data-more="' in page)

print("== 8. 扫损重开历史分页（监控页 /supertrend）：每次 20、封顶 200、切片在缓存之外 ==")
st_html = re.search(r'SUPERTREND_HTML = """(.*?)\n"""', web_src, re.S)
st_page = st_html.group(1) if st_html else ""
check("监控页接线：按 history_limit 拉取 + 加载更多按钮 + 条数回显 + 去掉写死的（最近 20 条）",
      "fetch('/api/supertrend/overview?history_limit=' + _lim)" in st_page
      and "function loadMoreHistory()" in st_page
      and 'onclick="loadMoreHistory()"' in st_page
      and 'id="hist-cnt"' in st_page
      and "（最近 20 条）" not in st_page,
      "")
check("翻页竞态：在飞的旧小页结果不许覆盖新翻出来的大页",
      "const _lim = histLimit;" in st_page
      and "if (_lim !== histLimit) return;" in st_page)
# 灌 205 条本策略 CLOSED（连原有那条够翻满 10 页；放在最后，不影响前面的 board 断言）
with storage.get_conn() as conn:
    for i in range(205):
        conn.execute(
            "INSERT INTO trade_positions (token, symbol, side, status, mode, "
            "margin_amount, leverage, notional, quantity, entry_price, "
            "current_price, unrealized_pnl, realized_pnl, pnl_pct, advice, "
            "strategy, closed_qty, closed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"H{i:03d}", f"H{i:03d}USDT", "LONG", "CLOSED", "paper", 50.0, 3.0,
             300.0, 1.0, 100.0, 101.0, 0.0, 1.0, 1.0, "超势反转平仓",
             "supertrend", 1.0, "2026-09-01 00:00:00"))
NS["_cache_invalidate"]("supertrend_overview")
_STEP = NS["_ST_HIS_STEP"]
_MAX = NS["_ST_HIS_MAX"]
p20 = NS["api_supertrend_overview"]()      # 失效后第一次 → 重算一次（正常）
_calls0 = list(_fake_st.ck_calls)          # 从这之后翻页不该再增加任何币安调用
p40 = NS["api_supertrend_overview"](40)
pmax = NS["api_supertrend_overview"](9999)
pmin = NS["api_supertrend_overview"](5)
check(f"默认一页 {_STEP} 条（不是把全部历史都发下来）",
      len(p20["history"]) == _STEP, str(len(p20["history"])))
check("history_limit=40 → 40 条", len(p40["history"]) == 40,
      str(len(p40["history"])))
check("翻页共用同一份计算（币安标记价调用不增加）",
      _fake_st.ck_calls == _calls0, str(_fake_st.ck_calls))
check("切片不动缓存：再要一页拿到的还是同一批",
      [x["id"] for x in NS["api_supertrend_overview"](20)["history"]]
      == [x["id"] for x in p20["history"]])
# 顺序很关键：先要过 20 条（小页），再要 40 条。若实现是"原地改缓存对象"
# （ov["history"] = ov["history"][:n]），缓存会被上一句截成 20 → 这里只能拿到 20
check("小页请求不会把缓存截短（再要 40 条仍是 40）",
      len(NS["api_supertrend_overview"](40)["history"]) == 40,
      str(len(NS["api_supertrend_overview"](40)["history"])))
check(f"低于下限回落成一页（{_STEP}）", len(pmin["history"]) == _STEP,
      str(len(pmin["history"])))
check(f"超上限封顶 {_MAX} 条", len(pmax["history"]) == _MAX,
      str(len(pmax["history"])))

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
