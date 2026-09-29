"""验证 web.py 平仓接口的兜底段 + 账户概览 ok 判定（抽真码块执行）。

覆盖第三轮审查修复：
- 交易所查询失败必须抛 503（HTTPException 是 Exception 子类，放在 try 里会被
  兜底 except 吞掉 → 接口仍返回 ok:true，页面以为已平）
- 兜底平仓零成交/无返回时**不得**把 live 记录标 CLOSED（否则记录已平而实盘还在
  = 无人对账的裸仓）
- 账户接口静默失败（返回 None）且余额兜底也为 0 时，ok 必须是 False
"""
import io
import json
import os
import re
import sys
import tempfile
import types

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)

import config

_TMP = tempfile.mkdtemp(prefix="afclose_")
config.DB_PATH = os.path.join(_TMP, "main.db")
config.SUPERTREND_DB = os.path.join(_TMP, "supertrend.db")
config.LIVE_DB_PATH = os.path.join(_TMP, "live.db")

import storage


class HTTPException(Exception):
    """fastapi.HTTPException 的最小替身（本地环境没装 fastapi；语义一致：
    Exception 子类 + status_code/detail —— "被 except Exception 吞掉"正是本检查要抓的坑）"""
    def __init__(self, status_code: int, detail: str = ""):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail

with storage.get_conn() as conn:
    conn.executescript(storage.SCHEMA)
storage.init_onchain_live_db()

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


# ---- 假 exchange ----
EX = types.ModuleType("exchange")
EX.last_error = ""
EX._last_error_msg = ""
EX.positions = [{"symbol": "AAAUSDT", "quantity": 2.0, "position_side": "LONG",
                 "mark_price": 110.0}]   # 标记价 110 vs 记录 entry 100 → 估算 +20
EX.fail_positions = False
EX.close_fill = 2.0          # 平仓回报的成交量（0=零成交）


def _get_positions():
    if EX.fail_positions:
        EX.last_error = "api down"
        raise RuntimeError("api down")
    EX.last_error = ""
    return [dict(p) for p in EX.positions]


def _get_last_error():
    return EX.last_error


def _close_position_market(token, qty, position_side="LONG"):
    if EX.close_fill <= 0:
        return {"order_id": 1, "price": 100.0, "quantity": 0.0, "status": "EXPIRED"}
    for p in EX.positions:
        if p["symbol"] == f"{token}USDT":
            p["quantity"] = max(p["quantity"] - EX.close_fill, 0)
    return {"order_id": 2, "price": 100.0, "quantity": EX.close_fill,
            "status": "FILLED"}


EX.get_positions = _get_positions
EX.get_last_error = _get_last_error
EX.close_position_market = _close_position_market
EX.cancel_all_orders = lambda token: True
EX.get_account_info = lambda: {"equity": 1000.0, "available_balance": 800.0,
                               "unrealized_pnl": 5.0}
EX.get_balance = lambda: 800.0
sys.modules["exchange"] = EX

# 其它被接口懒加载的模块：给最小桩，避免依赖真实链路
for _name in ("supertrend_live", "alphafox_live", "trade_logic", "exchange_bridge"):
    _m = types.ModuleType(_name)
    if _name == "supertrend_live":
        _m.close_live = lambda token: False
    elif _name == "alphafox_live":
        _m.close_live = lambda token: False
    else:
        _m.close_live_position = lambda token: None
        _m.manual_close_on_unwatch = lambda conn, token: {"ok": False}
    sys.modules[_name] = _m

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


NS = {"storage": storage, "config": config, "json": json, "HTTPException": HTTPException,
      "os": os, "types": types, "__builtins__": __builtins__}
for fn in ("api_live_binance_close", "api_live_binance_overview"):
    src = extract_func(fn)
    if not src:
        print(f"[FAIL] 未能抽取 {fn}")
        sys.exit(1)
    exec(compile(src, f"web.py:{fn}", "exec"), NS)

print("== 1. 兜底平仓：成交校验 ==")
with storage.get_conn(config.LIVE_DB_PATH) as lc:
    storage.live_position_insert(lc, {
        "paper_position_id": 1, "token": "AAA", "symbol": "AAAUSDT",
        "side": "LONG", "strategy": "alphafox", "entry_price": 100.0,
        "quantity": 2.0, "leverage": 5, "margin_usd": 20.0})


def _live_status():
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        return lc.execute("SELECT status FROM live_positions WHERE token='AAA' "
                          "ORDER BY id DESC LIMIT 1").fetchone()["status"]


EX.close_fill = 0.0          # 零成交
_r0 = NS["api_live_binance_close"]({"token": "AAA"})
check("零成交：live 记录保持 OPEN（不假平仓）", _live_status() == "OPEN",
      _live_status())
check("零成交：接口如实返回 ok=False（页面不弹'已平仓'）",
      _r0.get("ok") is False and "detail" in _r0, str(_r0))

EX.close_fill = 2.0          # 成交
NS["api_live_binance_close"]({"token": "AAA"})
check("成交：live 记录收敛 CLOSED", _live_status() == "CLOSED", _live_status())
with storage.get_conn(config.LIVE_DB_PATH) as lc:
    _rp = float(lc.execute("SELECT realized_pnl FROM live_positions "
                           "WHERE token='AAA' ORDER BY id DESC LIMIT 1")
               .fetchone()["realized_pnl"])
check("成交：写了 realized 估算（按标记价，不留 0 混进胜率）",
      abs(_rp - 20.0) < 1e-6, str(_rp))

print("== 2. 交易所查询失败：必须抛 503（不能被 except 吞成 ok:true） ==")
with storage.get_conn(config.LIVE_DB_PATH) as lc:
    storage.live_position_insert(lc, {
        "paper_position_id": 2, "token": "BBB", "symbol": "BBBUSDT",
        "side": "LONG", "strategy": "alphafox", "entry_price": 100.0,
        "quantity": 2.0, "leverage": 5, "margin_usd": 20.0})
EX.fail_positions = True
_raised = None
try:
    NS["api_live_binance_close"]({"token": "BBB"})
except HTTPException as e:
    _raised = e
except Exception as e:  # noqa: BLE001
    _raised = e
EX.fail_positions = False
check("查询失败：抛出 HTTPException(503)（不是 ok:true）",
      isinstance(_raised, HTTPException) and _raised.status_code == 503,
      repr(_raised))
with storage.get_conn(config.LIVE_DB_PATH) as lc:
    _st = lc.execute("SELECT status FROM live_positions WHERE token='BBB' "
                     "ORDER BY id DESC LIMIT 1").fetchone()["status"]
check("查询失败：记录不被误标平仓", _st == "OPEN", _st)

print("== 3. 账户概览 ok 判定 ==")
check("账户正常：ok=True",
      NS["api_live_binance_overview"]().get("ok") is True,
      str(NS["api_live_binance_overview"]()))
EX.get_account_info = lambda: None          # 静默失败（返回 None）
EX.get_balance = lambda: 0.0                # 兜底也失败
_o = NS["api_live_binance_overview"]()
check("账户静默失败 + 余额为 0：ok=False（页面提示而不是显示 $0）",
      _o.get("ok") is False, str(_o))

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
