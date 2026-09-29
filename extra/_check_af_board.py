"""验证跟单托管页（web.py）：board 聚合 / manage 开关 / 页面与导航接线。

web.py 不能 import（模块级起线程），沿用 _check_* 的"从源码抽真码块 exec"套路：
- 抽 _af_trader_labels / _af_managed_state / api_live_binance_overview /
  api_live_binance_positions / api_alphafox_board 的原文执行
- 页面与路由用源码断言（区块、按钮、导航、确认文案）
"""
import io
import json
import os
import re
import sys
import tempfile
import types
from datetime import datetime, timedelta

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)

import config

_TMP = tempfile.mkdtemp(prefix="afboard_")
config.DB_PATH = os.path.join(_TMP, "main.db")
config.SUPERTREND_DB = os.path.join(_TMP, "supertrend.db")
config.LIVE_DB_PATH = os.path.join(_TMP, "live.db")
config.ALPHAFOX_DB = os.path.join(_TMP, "alphafox.db")

import storage
import alphafox_trade

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


# ---- 假 exchange（board 内部会 import exchange 取账户/持仓）----
FAKE_POSITIONS = [
    {"symbol": "AAAUSDT", "quantity": 2.0, "entry_price": 100.0,
     "mark_price": 110.0, "unrealized_pnl": 20.0, "leverage": 5,
     "position_side": "LONG"},
    # 历史行 symbol 未归一（生产实测 BTC / XYZ:SKHX 都出现过）也要能 join 上。
    # 数量按币安口径给负数（SHORT 的 positionAmt 是负的）→ 页面必须显示绝对值
    {"symbol": "DDDUSDT", "quantity": -3.0, "entry_price": 50.0,
     "mark_price": 55.0, "unrealized_pnl": 15.0, "leverage": 5,
     "position_side": "SHORT"},
]
fake_ex = types.ModuleType("exchange")
fake_ex.get_account_info = lambda: {"equity": 1234.5, "available_balance": 1000.0,
                                    "unrealized_pnl": 20.0}
fake_ex.get_positions = lambda: [dict(p) for p in FAKE_POSITIONS]
fake_ex.get_balance = lambda: 1000.0
fake_ex.get_last_error = lambda: ""
fake_ex._last_error_msg = ""
sys.modules["exchange"] = fake_ex

with storage.get_conn() as conn:
    conn.executescript(storage.SCHEMA)
    conn.execute("ALTER TABLE trade_positions ADD COLUMN managed INTEGER DEFAULT 1")
storage.init_onchain_live_db()
alphafox_trade.init_db()
alphafox_trade.set_follow("bn-1", "老王", "", True)
alphafox_trade.set_follow("bn-2", "小李", "", False)
alphafox_trade._prev_snap["bn-1"] = {}      # 老王当前无 AAA 仓 → 触发"交易员已平"警示


def add_paper(token, side, status, managed, tid, realized=0.0, pnl_pct=0.0,
              advice="", symbol=None):
    with storage.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO trade_positions (token, symbol, side, status, mode, "
            "margin_amount, leverage, notional, quantity, entry_price, "
            "current_price, strategy, closed_qty, realized_pnl, pnl_pct, "
            "advice, managed, signal_snapshot, closed_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (token, symbol or f"{token}USDT", side, status, "paper", 50.0, 5.0,
             100.0, 2.0, 100.0, 110.0, "alphafox", 0.0, realized, pnl_pct,
             advice, managed,
             json.dumps({"trader_id": tid, "k_qty": 0.01}),
             "2026-09-12 03:00:00" if status == "CLOSED" else None))
        return cur.lastrowid


pid_a = add_paper("AAA", "LONG", "OPEN", 1, "bn-1")
pid_b = add_paper("BBB", "SHORT", "OPEN", 0, "bn-2")
pid_c = add_paper("CCC", "LONG", "CLOSED", 1, "bn-1", realized=12.5,
                  pnl_pct=25.0, advice="跟单交易员平仓")
# 旧版代码写的行：symbol 是上游原始拼写（不带 USDT）→ join 必须归一
pid_d = add_paper("DDD", "SHORT", "OPEN", 0, "bn-2", symbol="DDD")
# 托管中**但没有实盘记录**（实盘那腿没建起来）→ 页面必须提示"实盘未建立：原因"
pid_e = add_paper("EEE", "LONG", "OPEN", 1, "bn-1")

with storage.get_conn(config.LIVE_DB_PATH) as lc:
    storage.live_position_insert(lc, {
        "paper_position_id": pid_a, "token": "AAA", "symbol": "AAAUSDT",
        "side": "LONG", "strategy": "alphafox", "entry_price": 100.0,
        "quantity": 2.0, "leverage": 5, "margin_usd": 20.0})
    storage.live_position_insert(lc, {
        "paper_position_id": pid_c, "token": "CCC", "symbol": "CCCUSDT",
        "side": "LONG", "strategy": "alphafox", "entry_price": 100.0,
        "quantity": 2.0, "leverage": 5, "margin_usd": 20.0})
    storage.live_position_update(lc, 2, {
        "status": "CLOSED", "realized_pnl": 11.8,
        "closed_at": "__CURRENT_TIMESTAMP__"})
    storage.live_trade_log_insert(lc, {
        "token": "AAA", "strategy": "alphafox", "action": "rebalance_add",
        "price": 100.0, "quantity": 1.0, "paper_position_id": pid_a,
        "details_json": json.dumps({"reason": "对账补仓（固定名义·价格漂移修正）"})})

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
      "datetime": datetime, "timedelta": timedelta, "alphafox_trade": alphafox_trade,
      "alphafox_live": __import__("alphafox_live"),
      "time": __import__("time"), "HTTPException": _HTTPErr,
      "_af_last_snap": {},          # 探测缓存（测试自己塞数据）
      "os": os, "types": types, "__builtins__": __builtins__}
missing = []
for fn in ("_af_local_ts", "_af_trader_labels", "_af_managed_state", "_af_num",
           "api_live_binance_overview", "api_live_binance_positions",
           "api_alphafox_board", "api_alphafox_token_settings"):
    src = extract_func(fn)
    if not src:
        missing.append(fn)
        continue
    exec(compile(src, f"web.py:{fn}", "exec"), NS)
check("从 web.py 抽取到全部函数原文", not missing, str(missing))
if missing:
    print(f"结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1)

print("== 1. _af_managed_state：5 种托管状态 + 警示 ==")
st = NS["_af_managed_state"]
check("托管中", st(1, True, {"AAAUSDT|LONG": 1}, ("AAAUSDT", "LONG"))[0] == "托管中")
check("已取消托管", st(0, True, {}, ("AAAUSDT", "LONG"))[0] == "已取消托管")
check("恢复中", st(2, True, {}, ("AAAUSDT", "LONG"))[0] == "恢复中")
check("已脱离", st(3, True, {}, ("AAAUSDT", "LONG"))[0] == "已脱离")
_s = st(0, True, {}, ("AAAUSDT", "LONG"))
check("取消托管 + 交易员已平该仓 → 红字警示",
      _s[1] == "red" and "交易员已平" in _s[2], str(_s))
_s = st(0, False, None, ("AAAUSDT", "LONG"))
check("取消跟单（无信号源）→ 提示无退出信号", "无退出信号" in _s[2], str(_s))
_s = st(3, True, {}, ("AAAUSDT", "LONG"))
check("已脱离 → 请自行处理", "自行处理" in _s[2], str(_s))

print("== 2. board 聚合：持仓/历史/日志都带跟单员 + 托管状态 ==")
# 实盘层"这一腿没建起来"的原因（真实运行时由 _mirror_missing 每轮写入）
NS["alphafox_live"]._note_skip("EEE", "LONG", "补开跳过：交易所已有非镜像持仓（0.285）")
board = NS["api_alphafox_board"]()
pos = {p["token"]: p for p in board["positions"]}
check("持仓返回 4 笔", len(board["positions"]) == 4,
      str(len(board["positions"])))
check("AAA 带跟单员名（老王）", pos["AAA"]["trader_label"] == "老王",
      str(pos["AAA"]["trader_label"]))
check("AAA 托管中 + 实盘数量来自交易所",
      pos["AAA"]["state"] == "托管中" and abs(pos["AAA"]["ex_quantity"] - 2.0) < 1e-9,
      str({k: pos["AAA"][k] for k in ("state", "ex_quantity")}))
check("BBB 已取消托管 + 警示红字",
      pos["BBB"]["state"] == "已取消托管" and pos["BBB"]["state_cls"] == "muted",
      str({k: pos["BBB"][k] for k in ("state", "state_cls")}))
check("DDD 行 symbol 未归一（'DDD'）也能 join 上交易所实盘数据",
      (pos["DDD"]["ex_quantity"] or 0) == 3.0,   # 不许 - 运算：变异下是 None，会崩成"假抓住"
      str({k: pos["DDD"][k] for k in ("symbol", "ex_quantity")}))
check("空单实盘数量显示为绝对值（交易所 positionAmt 是负数）",
      pos["DDD"]["ex_quantity"] == 3.0, str(pos["DDD"]["ex_quantity"]))
check("无交易所持仓时 ex_quantity=None 不报错（BBB 无实盘）",
      pos["BBB"]["ex_quantity"] is None, str(pos["BBB"]["ex_quantity"]))
# ---- 实盘那一腿建没建起来：页面提示（2026-09-23 用户报"托管中但没跟减仓"）----
check("托管中 + 有实盘记录 → 不提示实盘未建立",
      pos["AAA"]["live_warn"] == "", repr(pos["AAA"]["live_warn"]))
check("托管中 + 无实盘记录 → 带出实盘层的跳过原因",
      pos["EEE"]["live_warn"] == "补开跳过：交易所已有非镜像持仓（0.285）",
      repr(pos["EEE"]["live_warn"]))
check("非托管态（取消托管）不提示——本来就不该有实盘动作",
      pos["BBB"]["live_warn"] == "", repr(pos["BBB"]["live_warn"]))
check("页面渲染该提示（源码断言：少了就静默退化回「看不出实盘没跟」）",
      "p.live_warn ?" in web_src and "实盘未建立：" in web_src)
his = board["history"]
check("历史带跟单员 + 实盘盈亏", len(his) == 1 and his[0]["trader_label"] == "老王"
      and abs(float(his[0]["live_pnl"]) - 11.8) < 1e-9, str(his))
check("历史保留平仓原因", his[0]["advice"] == "跟单交易员平仓", str(his[0]["advice"]))
# 2026-09-14 用户要求：开仓价 / 浮动盈亏要 **纸盘 / 实盘** 两边都显示
check("持仓带实盘开仓价 ex_entry（页面用来拼 纸盘/实盘）",
      abs(float(pos["AAA"]["ex_entry"]) - 100.0) < 1e-9, str(pos["AAA"].get("ex_entry")))
check("持仓带实盘浮盈 ex_pnl", abs(float(pos["AAA"]["ex_pnl"]) - 20.0) < 1e-9,
      str(pos["AAA"].get("ex_pnl")))
check("历史带实盘开仓价 live_entry",
      his[0].get("live_entry") is not None
      and abs(float(his[0]["live_entry"]) - 100.0) < 1e-9, str(his[0].get("live_entry")))
check("页面把开仓价/盈亏渲染成 纸盘/实盘（源码断言，少一半就静默退化）",
      "num(p.entry_price) + ' / '" in web_src
      and "p.ex_entry == null" in web_src and "p.live_entry == null" in web_src)
check("历史时间按北京时间输出（UTC 03:00 → 11:00）",
      his[0]["closed_at"].startswith("2026-09-12 11:00:00"),
      str(his[0]["closed_at"]))
logs = board["trades"]
check("日志带跟单员（按 paper_position_id 归属）",
      len(logs) == 1 and logs[0]["trader_label"] == "老王", str(logs))
check("日志 details_json 可解析出说明（前端 JSON.parse 后显示）",
      "对账补仓" in (json.loads(logs[0]["details_json"] or "{}").get("reason") or ""),
      str(logs[0]["details_json"]))
check("页面日志渲染走 JSON.parse（不会把原始 JSON 串显示出来）",
      "JSON.parse(t.details_json" in web_src)
check("账户概览透传交易所数据", board["account"]["equity"] == 1234.5,
      str(board["account"]))
check("实盘口径统计", board["stats"]["live"]["closed"] == 1
      and abs(float(board["stats"]["live"]["pnl"]) - 11.8) < 1e-9,
      str(board["stats"]["live"]))
check("纸盘口径统计", board["stats"]["paper"]["closed"] == 1
      and abs(float(board["stats"]["paper"]["pnl"]) - 12.5) < 1e-9,
      str(board["stats"]["paper"]))
check("浮动/未平仓口径", board["stats"]["open"]["open_paper"] == 4
      and board["stats"]["open"]["open_ex"] == 2
      and abs(float(board["stats"]["open"]["ex_pnl"]) - 35.0) < 1e-9,
      str(board["stats"]["open"]))
check("账户查询失败可区分（ok=False 时不冒充 0 权益）",
      board.get("account_ok") is True, str(board.get("account_ok")))

# 交易所取数整体失败：账户 OK 标志 + 实盘口径一律 None（页面显示"—"而不是 0）
_orig_acct = fake_ex.get_account_info


def _acct_down():
    raise RuntimeError("api down")


fake_ex.get_account_info = _acct_down
try:
    _b2 = NS["api_alphafox_board"]()
finally:
    fake_ex.get_account_info = _orig_acct
check("交易所取数失败：account_ok=False + 实盘浮动/未平仓给 None（不冒充 0）",
      _b2.get("account_ok") is False
      and _b2["stats"]["open"]["ex_pnl"] is None
      and _b2["stats"]["open"]["open_ex"] is None,
      str((_b2.get("account_ok"), _b2["stats"]["open"])))

print("== 3. 路由 / 页面 / 导航接线 ==")
check("board 路由已注册", '@app.get("/api/alphafox/board")' in web_src)
check("manage 路由已注册", '@app.post("/api/alphafox/manage")' in web_src)
check("manage 失败返回 409（页面据此弹提示）",
      'raise HTTPException(409, res.get("msg") or "操作失败")' in web_src)
check("manage 缺省 managed 报错（缺字段不得静默当成取消托管）",
      'if "managed" not in body:' in web_src)
check("board 用 _pos_key 归一做 join（老行拼写不一也能对上实盘）",
      "alphafox_trade._pos_key(pos)" in web_src)
check("时间列走 _af_local_ts（sqlite3 不做 TIMESTAMP 解析，老写法是死代码）",
      "def _af_local_ts(" in web_src
      and "_af_local_ts(r[k])" in web_src
      and "pos[k] = _af_local_ts(pos[k])" in web_src)
check("页面路由已注册", '@app.get("/alphafox-live", response_class=HTMLResponse)' in web_src)
check("导航渲染含新页面", '_nav_html("alphafox-live")' in web_src)

# 导航结构：三个实盘页收在「💹 实盘」TAG 下，且高亮逻辑正确
# （抽 _nav_html + 常量原文渲染，比 grep 源码可靠）
_nav_bits = [re.search(r"_NAV_ITEMS = \[.*?\n\]", web_src, re.S),
             re.search(r"_NAV_DROPDOWNS = \[.*?\n\]", web_src, re.S),
             re.search(r"_NAV_TAIL = \(.*?\)", web_src, re.S)]


def _indented_block(src, name):
    """抽一个函数的完整源码：从 def 行到下一个顶格非空行为止
    （extract_func 遇到"函数后面不是 @app./def 而是普通语句"时会多抽）"""
    i = src.find(f"def {name}(")
    if i < 0:
        return None
    out = []
    for ln in src[i:].split("\n"):
        if out and ln.strip() and not ln.startswith((" ", "\t")):
            break
        out.append(ln)
    return "\n".join(out)


_hnav = _indented_block(web_src, "_nav_html")
if any(b is None for b in _nav_bits) or not _hnav:
    check("导航代码可抽取", False, "未定位 _NAV_ITEMS/_NAV_DROPDOWNS/_NAV_TAIL")
else:
    NS2 = {"__builtins__": __builtins__}
    exec(compile("\n".join(b.group(0) for b in _nav_bits) + "\n" + _hnav,
                 "web.py:nav", "exec"), NS2)
    _html = NS2["_nav_html"]("alphafox-live")
    _i = _html.find("💹 实盘")
    _seg = _html[_i:_i + 700] if _i >= 0 else ""
    check("顶部有「实盘」TAG 且含三个实盘页",
          _i >= 0 and 'href="/live-binance"' in _seg
          and 'href="/live-onchain"' in _seg
          and 'href="/alphafox-live"' in _seg, _seg[:200])
    check("跟单托管在 TAG 内且高亮",
          'href="/alphafox-live" class="active"' in _seg, _seg[:200])
    check("实盘页不再零散挂在顶部一级导航",
          _html.find('href="/live-binance"') > _html.find("💹 实盘"),
          _html[:200])
for sec, tag in (("① 币安账户概览", 'id="acct"'), ("② 跟单盈亏", 'id="stats"'),
                 ("③ 跟单交易员持仓", 'id="trd-wrap"'),
                 ("④ 跟单持仓", 'id="pos-wrap"'), ("⑤ 历史持仓", 'id="his-wrap"'),
                 ("⑥ 交易日志", 'id="log-wrap"'), ("⑦ 币种设置", 'id="ts-wrap"')):
    check(f"页面区块 {sec}", sec in web_src and tag in web_src)
# 段落标号只看**跟单托管页这一份**（别页也有 ①②…，在 web_src 全局数会误判）
_af_page = (web_src.split('ALPHAFOX_LIVE_HTML = """')[1].split('"""')[0]
            if 'ALPHAFOX_LIVE_HTML = """' in web_src else "")
check("交易员持仓卡片排在「跟单盈亏」下面（用户 2026-09-15 要求）",
      _af_page.find("② 跟单盈亏") < _af_page.find("③ 跟单交易员持仓")
      < _af_page.find("④ 跟单持仓"), "")
check("段落标号 ①~⑦ 各只出现一次（改顺序时别留下重号）",
      all(_af_page.count(m) == 1 for m in "①②③④⑤⑥⑦"),
      str({m: _af_page.count(m) for m in "①②③④⑤⑥⑦"}))
check("持仓行有平仓按钮", 'data-close=' in web_src)
check("平仓确认写明会平掉该币所有策略持仓/撤单（原来只写跟单）",
      "平掉该币所有策略的实盘持仓" in web_src)
check("持仓行有取消/恢复托管按钮",
      'data-on="0">取消托管' in web_src and 'data-on="1">恢复托管' in web_src)
check("取消托管二次确认（无退出信号提示）",
      "跟单仓没有止损，取消后失去唯一退出信号" in web_src)
check("恢复托管二次确认（纸盘对齐/实盘不额外下单/手动量不被按旧档位改回）",
      "不动交易所" in web_src and "不会被按旧档位改回" in web_src
      and "最多半份的零头会被对账校正" in web_src)
check("恢复中(2) 也能退回非托管（卡死时的行内出路）",
      "Number(p.managed) === 0 || Number(p.managed) === 3" in web_src)
check("页面 5 秒轮询", "setInterval(load, 5000)" in web_src)
check("雷达页取消跟单文案已改（不再说同时平仓）",
      "将同时平掉" not in web_src and '将全部"取消托管"' in web_src)
check("雷达页重新跟单有确认 + 报数", "已重新跟单" in web_src and "笔持仓恢复托管" in web_src)
check("@app.post(\"/api/alphafox/follow\") 返回 managed_off/reattach",
      "**(res or {})" in web_src)

print("== 6. 跟单交易员持仓卡片（与雷达页共享探测快照）==")
NS["_af_last_snap"]["bn-1"] = {
    "positions": [{"symbol": "TOKXUSDT", "side": "LONG", "absQty": 10,
                   "avgPrice": 2.0, "markPrice": 2.2, "notional": 22.0,
                   "leverage": 3, "unrealizedPnl": 2.0,
                   "estimatedLiqPrice": 1.5, "lastOrderTime": 1789405426522},
                  # 上游对空头给过负名义（-3636278.98）→ 页面必须显示绝对值
                  {"symbol": "SHORTX", "side": "SHORT", "absQty": 5,
                   "avgPrice": 10.0, "markPrice": 9.0, "notional": -450.0,
                   "leverage": 5, "unrealizedPnl": 50.0}],
    "polledAt": 111, "name": "老王"}
board2 = NS["api_alphafox_board"]()
cards = {t["trader_id"]: t for t in board2["traders"]}
check("每个跟单中的交易员一张卡（bn-1 在内）", "bn-1" in cards, str(list(cards)))
check("已取消跟单的交易员不出卡片（bn-2 未跟单）", "bn-2" not in cards,
      str(list(cards)))
c1 = cards["bn-1"]
check("卡片带持仓明细与汇总（名义/浮盈求和，空头负名义取绝对）",
      c1["pos_count"] == 2 and not c1["stale"]
      and abs(c1["notional_sum"] - (22.0 + 450.0)) < 1e-9
      and abs(c1["upnl_sum"] - (2.0 + 50.0)) < 1e-9
      and c1["positions"][0]["symbol"] == "TOKXUSDT",
      str(c1))
check("卡片明细带强平价与最后下单时间（信息不省略）",
      abs(float(c1["positions"][0].get("liq_price") or 0) - 1.5) < 1e-9
      and c1["positions"][0].get("last_order_time") == 1789405426522,
      str(c1["positions"][0]))   # 用 .get+or 0：字段缺失时给 FAIL，别让 float(None) 崩成假抓住
check("空头负名义被取绝对（页面不会显示 -$450）",
      abs(float(c1["positions"][1]["notional"]) - 450.0) < 1e-9,
      str(c1["positions"][1]))
_stale = next(t for k, t in cards.items() if k != "bn-1")
check("没探测过的交易员：stale + positions=None（不谎报空仓）",
      _stale["stale"] is True and _stale["positions"] is None, str(_stale))
check("卡片不新增任何平台请求（页面渲染走同一份缓存）",
      "renderTraders(d.traders || [])" in web_src)

print("== 7. 币种设置：每币杠杆覆盖 + 生效值 + 接口写/清 ==")
check("token_options 含映射表与交易过的币",
      "AAA" in board2["token_options"] and len(board2["token_options"]) > 0,
      str(board2["token_options"]))
check("token_settings 带全局值（杠杆 + 单笔上限 + 映射比例）",
      board2["token_settings"]["globals"]["live_leverage"] > 0
      and board2["token_settings"]["globals"]["max_notional"] >= 0
      and 0 < board2["token_settings"]["globals"]["live_ratio"] <= 1,
      str(board2["token_settings"].get("globals")))
ts_api = NS["api_alphafox_token_settings"]
r = ts_api({"token": "tokz", "live_leverage": 7})
check("写：只剩杠杆一项可覆盖",
      abs(r["live_leverage"] - 7) < 1e-9 and abs(r["eff_leverage"] - 7) < 1e-9,
      str(r))
b3 = NS["api_alphafox_board"]()
item = next(x for x in b3["token_settings"]["items"] if x["token"] == "TOKZ")
check("board 里能读到覆盖 + 生效杠杆",
      abs(item["live_leverage"] - 7) < 1e-9
      and abs(item["eff_leverage"] - 7) < 1e-9,
      str(item))
check("生效值真的会被跟单层用（_eff_live_params 同口径）",
      abs(NS["alphafox_live"]._eff_live_params("TOKZ", 50.0) - 7.0) < 1e-9,
      str(NS["alphafox_live"]._eff_live_params("TOKZ", 50.0)))
r = ts_api({"token": "TOKZ", "live_leverage": ""})
check("清：留空 → 覆盖整行删除，生效值回全局",
      r["live_leverage"] is None
      and abs(NS["alphafox_live"]._eff_live_params("TOKZ", 50.0) - 50.0) < 1e-9,
      str(r))
try:
    ts_api({"token": "A B!", "live_leverage": 5})
    _bad = None
except Exception as e:
    _bad = e
check("非法币种被拒（400）", _bad is not None and getattr(_bad, "code", 0) == 400,
      str(_bad))
try:
    ts_api({"token": "OKTOK", "live_leverage": 0})
    _bad2 = None
except Exception as e:
    _bad2 = e
check("越界杠杆被拒（400）", _bad2 is not None and getattr(_bad2, "code", 0) == 400,
      str(_bad2))
check("页面接线：⑦币种设置面板 + 渲染 + 保存入口齐全",
      "⑦ 币种设置" in web_src
      and "renderTokenSettings(d.token_settings || {}, d.token_options || [])" in web_src
      and "function saveTokenSetting()" in web_src
      and "function saveGlobalLive()" in web_src
      and "只对<b>之后新开的</b>跟单仓生效" in web_src)
check("映射比例接线：⑦面板输入框 + 回填 + 保存一起提交 + 设置页字段",
      'id="g-ratio"' in web_src and "g.live_ratio" in web_src
      and "body.alphafox_live_ratio = Number(rt)" in web_src
      and 'id="trade-alphafox-live-ratio"' in web_src
      and "alphafox_live_ratio: Number(document.getElementById('trade-alphafox-live-ratio')"
          ".value || 1)" in web_src)
check("映射比例边界：>1（放大风险）与 0/负数都被 400 拒（只允许 0.01~1）",
      'alphafox_live_ratio" in fields \\\n            and not (0.01 <= '
      'fields["alphafox_live_ratio"] <= 1.0)' in web_src
      or "0.01 <= fields[\"alphafox_live_ratio\"] <= 1.0" in web_src,
      "设置接口里的映射比例校验缺失")
check("币种设置：已交易过的币做成可点选按钮（datalist 不弹=用户以为没得选）",
      'id="ts-chips"' in web_src and "data-chip=" in web_src
      and "ts-chips').addEventListener" in web_src
      and "已交易过的币（点一下填入）" in web_src)
check("页面接线：③交易员持仓面板 + 渲染齐全",
      "③ 跟单交易员持仓" in web_src and "trd-grid" in web_src)
check("⑤⑥ 分页接线：两面板独立上限 + 按 limit 拉取 + 「加载更多」按钮",
      "const PAGE_STEP = 50, PAGE_MAX = 200;" in web_src
      and "let _hisLimit = PAGE_STEP, _logLimit = PAGE_STEP;" in web_src
      and "fetch('/api/alphafox/board?limit=' + _lim)" in web_src
      and "renderMoreRow('his-more', 'his-cnt', 'his'" in web_src
      and "renderMoreRow('log-more', 'log-cnt', 'log'" in web_src
      and 'data-more="' in web_src)
check("⑤⑥ 分页：翻页状态在轮询间保持（模块级变量，不随刷新复位）",
      "let _hisLimit" in web_src and "_hisLimit = Math.min(PAGE_MAX" in web_src)
# 两页各一份 → 用计数断言（只查"存在"的话，改掉其中一份仍能匹配上另一份 = 假通过）
check("⑤⑥ 分页：两页都做了「点击撞上在飞请求 → 回来补拉」（趋势页 30s 一轮，否则像卡死）",
      web_src.count("if (loading) { _reloadPending = true; return; }") == 2
      and web_src.count("if (_reloadPending) { _reloadPending = false; load(); }") == 2,
      f"点击补拉 {web_src.count('if (loading) { _reloadPending = true; return; }')} 处 / "
      f"补拉执行 {web_src.count('if (_reloadPending) { _reloadPending = false; load(); }')} 处")
_trd_css = web_src.split(".trd-pos")[1][:220] if ".trd-pos" in web_src else ""
check("卡片是正方形（aspect-ratio）且卡片内可滚动",
      "aspect-ratio:1/1" in web_src and "trd-body{flex:1;overflow-y:auto}" in web_src,
      "")
check("持仓明细不再省略号截断（原来 nowrap+ellipsis 会把信息吃掉）",
      "text-overflow:ellipsis" not in _trd_css and "white-space:normal" in _trd_css,
      _trd_css[:120])

print("== 9. live 库读不到时：不许把「读不到」误报成「实盘未建立」 ==")
_orig_init = storage.init_onchain_live_db


def _init_boom(*a, **k):
    raise RuntimeError("live db down")


storage.init_onchain_live_db = _init_boom
try:
    b3 = NS["api_alphafox_board"]()
    check("live 库读不到 → 一行都不提示（否则全部误报红字）",
          all(p["live_warn"] == "" for p in b3["positions"]),
          str([(p["token"], p["live_warn"]) for p in b3["positions"]]))
finally:
    storage.init_onchain_live_db = _orig_init

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
