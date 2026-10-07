"""反转市价容忍：端到端接线测试（跑完整链路，不只测单函数）。

为什么单独一个套件：单元测试直接调 `_process_token` 会掩盖"某一层漏注册"——
曾发生两起（都已修，本套件就是它们的回归锁）：
  1. `storage.trading_settings_update` 白名单没加 `supertrend_flip_tolerance_pct`
     → 设置页保存返回成功、值被静默丢弃 → 全局闸门永远失效
  2. 留痕写在 `advice` → 1s 循环（trade_logic.update_paper_positions）与
     `_update_order` 都会重写该字段 → 留痕活不过 1 秒

覆盖：设置保存→读回→策略读取→跳过留痕→1s 循环→挂单更新→ref 过期守卫
运行：python extra/_check_flip_wiring.py
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

_TMP = tempfile.mkdtemp(prefix="flip_wire_")
config.DB_PATH = os.path.join(_TMP, "main.db")
config.SUPERTREND_DB = os.path.join(_TMP, "supertrend.db")

import storage                                              # noqa: E402
import trade_logic                                          # noqa: E402
import supertrend_strategy as st                            # noqa: E402

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

import supertrend_live as _stl                               # noqa: E402
_stl.sync_supertrend_live = lambda *a, **k: None
st.time.sleep = lambda *a, **k: None
import exchange as _ex                                       # noqa: E402
_ex._fetch_symbol_filters = lambda symbol: {
    "stepSize": 0.001, "minQty": 0.0, "priceStep": 0.01}

MOCK_SIG, MOCK_PRICE = {}, {}
st._fetch_okx_supertrend = lambda token: MOCK_SIG.get(token)
st.get_mark_price = lambda token: MOCK_PRICE.get(token)
trade_logic.get_mark_price = lambda token: MOCK_PRICE.get(token)
import tg_notify                                             # noqa: E402
tg_notify.send_supertrend_reversal = lambda *a, **k: None

CANDLE = 15 * 60 * 1000
PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def closed_ts(offset=0):
    import time as _t
    now_ms = int(_t.time() * 1000)
    return (now_ms // CANDLE) * CANDLE - CANDLE + offset * CANDLE


st.init_db()
storage.init_db()
with storage.get_conn() as c:
    storage.trading_settings_update(c, {"supertrend_enabled": True})
with storage.get_conn(config.SUPERTREND_DB) as sc:
    sc.execute("UPDATE supertrend_tokens SET status='REMOVED'")   # 清掉种入的币
    sc.execute("INSERT OR REPLACE INTO supertrend_tokens "
               "(token, status, direction, flip_market_open) "
               "VALUES ('BTC','ACTIVE','LONG_ONLY',1)")

print("== 1. 全局设置：保存 → 读回（曾因 storage 白名单漏注册而静默失效）==")
check("trading_settings_defaults 含该键",
      "supertrend_flip_tolerance_pct" in storage.trading_settings_defaults())
with storage.get_conn() as c:
    storage.trading_settings_update(c, {"supertrend_flip_tolerance_pct": 3.0})
with storage.get_conn() as c:
    got = storage.trading_settings_get(c).get("supertrend_flip_tolerance_pct")
check("保存 3.0 后读回 3.0（不是 None / 不是字符串）",
      isinstance(got, float) and abs(got - 3.0) < 1e-9, repr(got))
# 脏值路径：库里直接塞字符串，读出来必须是 float（否则策略端 float() 会拿到字符串）
with storage.get_conn() as c:
    c.execute("UPDATE trading_settings SET value='4.5' "
              "WHERE key='supertrend_flip_tolerance_pct'")
with storage.get_conn() as c:
    got = storage.trading_settings_get(c).get("supertrend_flip_tolerance_pct")
check("库里是字符串 '4.5' → 读出 float 4.5", isinstance(got, float) and abs(got - 4.5) < 1e-9,
      repr(got))
with storage.get_conn() as c:
    storage.trading_settings_update(c, {"supertrend_flip_tolerance_pct": 1.0})

print("== 2. 策略端真读到全局值（不传每币覆盖 → 用全局 1%）==")


def reset_pos():
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("DELETE FROM supertrend_status")
        sc.execute("UPDATE supertrend_tokens SET flip_market_tolerance_pct=NULL "
                   "WHERE token='BTC'")


def run_flip(price, break_adjacency=False):
    """受限方向 DOWN 记基准 → 翻 UP（走 _run_round，读真实全局/每币设置）"""
    reset_pos()
    MOCK_SIG["BTC"] = {"ts": closed_ts(0), "trend": "DOWN",
                       "upper_band": 101000.0, "lower_band": 99000.0}
    MOCK_PRICE["BTC"] = 100000.0
    st._run_round()
    MOCK_SIG["BTC"] = {"ts": closed_ts(1), "trend": "DOWN",
                       "upper_band": 101000.0, "lower_band": 99000.0}
    st._run_round()                       # 记基准（WAIT_DIR）
    if break_adjacency:
        with storage.get_conn(config.SUPERTREND_DB) as sc:
            sc.execute("UPDATE supertrend_status SET last_candle_ts = last_candle_ts - ?",
                       (5 * CANDLE,))
    MOCK_SIG["BTC"] = {"ts": closed_ts(2), "trend": "UP",
                       "upper_band": 101000.0, "lower_band": 99000.0}
    MOCK_PRICE["BTC"] = price
    st._run_round()                       # 反转：市价 + 挂单


p = None
run_flip(100500.0)
with storage.get_conn() as mc:
    p = mc.execute("SELECT * FROM trade_positions WHERE token='BTC' "
                   "AND strategy='supertrend' AND status='OPEN'").fetchone()
check("全局容忍 1% 生效：价格 100500 ≤ 101000×1.01 → 市价开多", p is not None,
      str(p and dict(p)))

print("== 3. 留痕：跳过 → 1s 循环 + 挂单更新之后仍在（曾存 advice 秒没）==")
run_flip(102500.0)                        # > 101000×1.01 → 超容忍，只挂单
with storage.get_conn() as mc:
    pend = dict(mc.execute("SELECT * FROM trade_positions WHERE token='BTC' "
                           "AND strategy='supertrend' AND status='PENDING'").fetchone())
    no_market = not mc.execute(
        "SELECT 1 FROM trade_positions WHERE token='BTC' AND strategy='supertrend' "
        "AND status IN ('OPEN','PARTIAL')").fetchone()
check("超容忍：无市价仓", no_market)
check("超容忍：open_reason 带留痕", "反转市价跳过" in (pend["open_reason"] or ""),
      str(pend["open_reason"]))

# 真实 1s 循环（会重写 advice）
with storage.get_conn() as mc:
    trade_logic.update_paper_positions(mc)
with storage.get_conn() as mc:
    after = dict(mc.execute("SELECT * FROM trade_positions WHERE id=?",
                            (pend["id"],)).fetchone())
check("1s 循环后 open_reason 留痕仍在", "反转市价跳过" in (after["open_reason"] or ""),
      str(after["open_reason"]))
check("1s 循环确实重写了 advice（证明留痕当初放它是错的）",
      after["advice"] != pend["advice"], f'{pend["advice"]!r} -> {after["advice"]!r}')

# 新 K 线 → 挂单改价（_update_order 也重写 advice）
for i in (3, 4):
    MOCK_SIG["BTC"] = {"ts": closed_ts(i), "trend": "UP",
                       "upper_band": 101000.0, "lower_band": 99500.0}
    MOCK_PRICE["BTC"] = 103000.0
    st._run_round()
with storage.get_conn() as mc:
    after2 = dict(mc.execute("SELECT * FROM trade_positions WHERE id=?",
                             (pend["id"],)).fetchone())
check("挂单改价后 open_reason 留痕仍在", "反转市价跳过" in (after2["open_reason"] or ""),
      str(after2["open_reason"]))

print("== 4. ref 过期：基准 K 线不相邻 → 不拿旧轨道当闸门，且**只挂单不追高** ==")
run_flip(102500.0, break_adjacency=True)
with storage.get_conn() as mc:
    p2 = mc.execute("SELECT 1 FROM trade_positions WHERE token='BTC' "
                    "AND strategy='supertrend' AND status IN ('OPEN','PARTIAL')").fetchone()
    pend2 = mc.execute("SELECT open_reason FROM trade_positions WHERE token='BTC' "
                       "AND strategy='supertrend' AND status='PENDING'").fetchone()
# 不能 fail-open：这些场景（暂停恢复/取数连挂/重启/切实盘）恰好是价格已跑远的场景，
# 放行等于闸门在最该生效的时候失效
check("基准过期 → 不打市价（哪怕现价看似在容忍内）", p2 is None,
      str(p2 and dict(p2)))
check("基准过期 → 只挂限价单且留痕说明原因",
      pend2 is not None and "参考线不可用" in (pend2["open_reason"] or ""),
      str(pend2 and dict(pend2)))

print("== 5. web.py 接线（源码断言：少一层就会静默失效）==")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # extra/
from _mut_src import read_module_source         # 变异注入的源码优先，否则变异不可见
web_src = read_module_source("web")
check("TradingSettingsBody 有该字段", "supertrend_flip_tolerance_pct: float | None = None" in web_src)
check("api_trading_settings 白名单含该键",
      '"supertrend_order_tolerance_pct", "supertrend_flip_tolerance_pct"' in web_src)
check("设置页范围校验存在", "supertrend 反转市价容忍%必须在 [0, 20] 之间" in web_src)
check("overview params 返回 flip_tolerance_pct（按生效值 _st_tol 给）",
      '"flip_tolerance_pct": _st_tol(' in web_src
      and 'settings.get("supertrend_flip_tolerance_pct"),' in web_src)
check("卡片 API 返回每币值", '"flip_market_tolerance_pct": t.get("flip_market_tolerance_pct")' in web_src)
check("保存 API UPDATE 含该列", "flip_market_tolerance_pct=?," in web_src)
check("全局设置页有输入框", 'id="trade-supertrend-flip-tolerance"' in web_src)
check("每币弹窗有输入框", 'id="set-flip-tol"' in web_src)
check("全局默认摘要行含反转容忍",
      "· 反转市价容忍 ' + d.params.flip_tolerance_pct" in web_src)
check("留痕渲染读 open_reason（不是 advice）",
      "pend.open_reason.indexOf('反转市价跳过')" in web_src
      and "pend.advice.indexOf('反转市价跳过')" not in web_src)

print("== 6. B2/B3/B4：校验对称 / 空串语义 / 回显口径 ==")
check("B2 挂单容忍也有越界校验（原来只有 SL 容忍有）",
      'if "supertrend_order_tolerance_pct" in fields and not '
      '(0 <= fields["supertrend_order_tolerance_pct"] <= 20):' in web_src)
check("B3 三个容忍框都走 numOrUndef（空框不提交，不会把已设值改成 0）",
      "function numOrUndef(id)" in web_src
      and "supertrend_sl_tolerance_pct: numOrUndef(" in web_src
      and "supertrend_order_tolerance_pct: numOrUndef(" in web_src
      and "supertrend_flip_tolerance_pct: numOrUndef(" in web_src
      and "supertrend_flip_tolerance_pct: Number(" not in web_src)
check("B4 overview params 按生效值给（复刻策略端 clamp，不回显 DB 原始值）",
      "def _st_tol(v, default" in web_src
      and 'settings.get("supertrend_flip_tolerance_pct"),' in web_src
      and "_st_tol(" in web_src)

print("== 7. C-1~4：显示口径（页面显示 == 实际生效）==")
check("C-1 /api/settings 的三个容忍也走 clamp（否则设置页显示越界值、策略却回退了）",
      's[key] = _st_tol(s.get(key), default)' in web_src
      and 'for key, default in (' in web_src)
check("C-2/C-4 卡片返回生效值（每币合法用它、否则全局）",
      '"flip_tolerance_effective": _st_effective_tol(' in web_src
      and "def _st_effective_tol(tok_v, global_v)" in web_src)
check("C-4 卡片按生效值显示（用全局值的币也能看到闸门；越界值不会显示成假口径）",
      "cust.push('反转容忍' + c.flip_tolerance_effective + '%')" in web_src
      and "cust.push('反转容忍' + c.flip_market_tolerance_pct" not in web_src)
check("C-4′ 弹窗回填仍用每币原始值（编辑时看到的是库里存的，不是折算后的）",
      "set-flip-tol').value = c.flip_market_tolerance_pct != null" in web_src)
check("C-3 全局容忍框标注「留空=不改动」",
      "留空=不改动此项" in web_src)

print("== 8. 反转市价平仓（close_on_reverse）接线 ==")
check("web 卡片 API 返回该字段",
      '"close_on_reverse": t.get("close_on_reverse") in (1, "1", True),' in web_src)
check("web 保存 API UPDATE 含该列",
      "flip_market_tolerance_pct=?, close_on_reverse=? " in web_src)
_i = web_src.find('cor = 1 if str(body.get("close_on_reverse"))')
_seg = web_src[_i:_i + 400] if _i >= 0 else ""
check("BOTH 币不做 close_on_reverse 清零（与 flip 不同）",
      _i >= 0 and "cor = 0" not in _seg)
check("弹窗有下拉", 'id="set-cor"' in web_src)
check("弹窗回填读每币值",
      "document.getElementById('set-cor').value = c.close_on_reverse ? '1' : '0';" in web_src)
check("保存提交带该字段",
      "close_on_reverse: document.getElementById('set-cor').value," in web_src)
check("卡片徽标显示反转平仓", "if (c.close_on_reverse) cust.push('反转平仓');" in web_src)

# 端到端：DB 列 → _run_round 读取（tok_cor）→ 判据 → 市价平仓
reset_pos()
with storage.get_conn(config.SUPERTREND_DB) as sc:
    sc.execute("UPDATE supertrend_tokens SET close_on_reverse=1 WHERE token='BTC'")
MOCK_SIG["BTC"] = {"ts": closed_ts(0), "trend": "UP",
                   "upper_band": 101000.0, "lower_band": 99000.0}
MOCK_PRICE["BTC"] = 100500.0
st._run_round()                      # 首轮记基准
MOCK_SIG["BTC"] = {"ts": closed_ts(1), "trend": "UP",
                   "upper_band": 101000.0, "lower_band": 99000.0}
st._run_round()                      # 新K线 → 挂多单
MOCK_PRICE["BTC"] = 99980.0
with storage.get_conn() as mc:
    trade_logic.update_paper_positions(mc)   # 挂单成交 OPEN @99990
with storage.get_conn() as mc:
    held = mc.execute("SELECT 1 FROM trade_positions WHERE token='BTC' "
                      "AND strategy='supertrend' "
                      "AND status IN ('OPEN','PARTIAL')").fetchone()
check("前置：持仓 OPEN", held is not None)
MOCK_SIG["BTC"] = {"ts": closed_ts(2), "trend": "DOWN",
                   "upper_band": 101000.0, "lower_band": 99000.0}
MOCK_PRICE["BTC"] = 100700.0
st._run_round()                      # 反转：LONG vs DOWN → 市价平仓
with storage.get_conn() as mc:
    gone = not mc.execute("SELECT 1 FROM trade_positions WHERE token='BTC' "
                          "AND strategy='supertrend' "
                          "AND status IN ('OPEN','PARTIAL')").fetchone()
    last = dict(mc.execute("SELECT * FROM trade_positions WHERE token='BTC' "
                           "AND strategy='supertrend' "
                           "ORDER BY id DESC LIMIT 1").fetchone())
check("端到端：反转后市价平仓（DB 列 → _run_round 全链路）",
      gone and last["status"] == "CLOSED" and "趋势反转" in (last["advice"] or ""),
      str(last))
with storage.get_conn(config.SUPERTREND_DB) as sc:
    st_state = sc.execute("SELECT state FROM supertrend_status WHERE token='BTC'").fetchone()
check("平仓轮状态归位 WAIT_DIR（只做多等 UP）",
      st_state and st_state["state"] == "WAIT_DIR",
      str(dict(st_state) if st_state else None))
with storage.get_conn(config.SUPERTREND_DB) as sc:
    sc.execute("UPDATE supertrend_tokens SET close_on_reverse=0 WHERE token='BTC'")
reset_pos()

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
