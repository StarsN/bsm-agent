"""超级趋势策略本地逻辑测试（挂单模式：mock OKX + 标记价，不连外网）。

运行：python extra/test_supertrend_logic.py
所有 DB 落在临时目录，不污染项目 db/。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

_TMP = tempfile.mkdtemp(prefix="st_test_")
config.DB_PATH = os.path.join(_TMP, "main.db")
config.SUPERTREND_DB = os.path.join(_TMP, "supertrend.db")

import storage
import trade_logic
import supertrend_strategy as st

with storage.get_conn() as conn:
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(storage.SCHEMA)
    # SCHEMA 里 trade_signal_locks 是旧结构（无 strategy 列，生产库靠 _migrate 迁移），
    # 新测试库直接重建为带 strategy 的版本
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

# ---- 测试隔离：不发真实网络请求、不真等待 ----
# 1) 本文件 ~22 次 _run_round 会走到 C 段实盘对账，真连币安每次 401 往返 ~0.3s；
#    实盘镜像另有专门套件（extra/test_supertrend_live.py），这里打桩
import supertrend_live as _stl_mod
_stl_mod.sync_supertrend_live = lambda *a, **k: None
# 2) OKX 取数重试间隔 2+4s 与交易对过滤器请求：测试只要确定性，不需要真实等待/请求
st.time.sleep = lambda *a, **k: None          # 该模块的 time 即全局 time，仅测试进程内生效
import exchange as _ex
_ex._fetch_symbol_filters = lambda symbol: {
    "stepSize": 0.001, "minQty": 0.0, "priceStep": 0.01}

# ---- mock：token -> 已闭合K线信号 / 标记价 ----
MOCK_SIG: dict = {}
MOCK_PRICE: dict = {}

REAL_FETCH = st._fetch_okx_supertrend
st._fetch_okx_supertrend = lambda token: MOCK_SIG.get(token)
st.get_mark_price = lambda token: MOCK_PRICE.get(token)
trade_logic.get_mark_price = lambda token: MOCK_PRICE.get(token)

# 全局屏蔽 TG 发送（config 已填真实 token，测试不得触碰真实通知），
# 仅记录调用供组 28 断言
import tg_notify

TG_CALLS: list = []
tg_notify.send_supertrend_reversal = lambda *a, **k: TG_CALLS.append(a)

CANDLE = 15 * 60 * 1000
TOL_SL = 1.0
TOL_ORDER = 1.0


def closed_ts(offset_candles=0):
    now_ms = int(__import__("time").time() * 1000)
    return (now_ms // CANDLE) * CANDLE - CANDLE + offset_candles * CANDLE


def set_sig(token, trend, upper, lower, offset_candles=0, forming=None):
    sig = {
        "ts": closed_ts(offset_candles), "trend": trend,
        "upper_band": upper, "lower_band": lower,
    }
    if forming:
        sig["forming"] = forming
    MOCK_SIG[token] = sig


def new_candle(token):
    """模拟 15 分钟后新闭合K线（ts 前移一根）"""
    MOCK_SIG[token]["ts"] += CANDLE


def pos_of(token, statuses=("OPEN", "PARTIAL")):
    ph = ",".join("?" for _ in statuses)
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM trade_positions WHERE token=? AND strategy='supertrend' "
            f"AND status IN ({ph}) ORDER BY id DESC LIMIT 1",
            (token, *statuses)).fetchone()
        return dict(row) if row else None


def pending_of(token):
    return pos_of(token, ("PENDING",))


def pos_count(token):
    with storage.get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM trade_positions WHERE token=? AND strategy='supertrend'",
            (token,)).fetchone()[0]


def proc(token, tol_sl=TOL_SL, tol_order=TOL_ORDER):
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, token, tol_sl, tol_order, 50.0, 3.0)


PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def main():
    st.init_db()
    # 测试全程假设处于同一个 15m K 线窗口（closed_ts 基准不变）。
    # 仅防"启动瞬间恰好跨边界导致 closed_ts 计算翻页"竞态（测试全 mock、10s 内跑完，
    # age 永远不会超 32min stale 上限，无需长窗口）
    import time as _t
    now_ms = int(_t.time() * 1000)
    remain = CANDLE - (now_ms % CANDLE)
    if remain < 10 * 1000:
        print(f"  （距15m边界 {remain // 1000}s，睡过边界再跑）")
        _t.sleep(remain / 1000 + 3)

    print("== 1. 初始化 + 默认币种种入 ==")
    with storage.get_conn(config.SUPERTREND_DB) as c:
        tokens = {r["token"] for r in c.execute("SELECT token FROM supertrend_tokens")}
    check("BTC/ETH 已种入", tokens == {"BTC", "ETH"}, str(tokens))
    with storage.get_conn(config.SUPERTREND_DB) as c:
        c.execute("UPDATE supertrend_tokens SET status='REMOVED' WHERE token='ETH'")
    st.init_db()
    with storage.get_conn(config.SUPERTREND_DB) as c:
        eth = c.execute("SELECT status FROM supertrend_tokens WHERE token='ETH'").fetchone()
    check("移除过的币不复活", eth["status"] == "REMOVED")

    print("== 2. 首次加入：先等K线闭合，闭合后挂多单（挂单价/轨道SL公式）==")
    set_sig("BTC", "UP", 101000, 99000,
            forming={"trend": "UP", "upper_band": 101300, "lower_band": 99300})
    MOCK_PRICE["BTC"] = 100000
    proc("BTC")  # 首轮：无基准 → 只记基准，等待闭合
    check("首轮不挂单（等待闭合）", pending_of("BTC") is None)
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        row = sc.execute(
            "SELECT state, last_candle_ts FROM supertrend_status WHERE token='BTC'").fetchone()
    check("state=WAITING 且已记基准", row and row["state"] == "WAITING"
          and row["last_candle_ts"] is not None, str(dict(row) if row else None))
    proc("BTC")  # 同一根K线内第二轮 → 仍等待
    check("同K线第二轮仍不挂单", pending_of("BTC") is None)
    new_candle("BTC")
    proc("BTC")  # 新K线闭合 → 挂单
    p = pending_of("BTC")
    check("已挂 PENDING 多单", p and p["status"] == "PENDING" and p["side"] == "LONG",
          str(p and (p["status"], p["side"])))
    check("挂单价=99000*1.01=99990", p and abs(p["limit_price"] - 99990) < 1e-6,
          str(p and p["limit_price"]))
    check("SL=99000*0.99=98010", p and abs(p["stop_loss_price"] - 98010) < 1e-6,
          str(p and p["stop_loss_price"]))
    check("tp1/tp2 NULL", p and p["tp1_price"] is None and p["tp2_price"] is None)
    check("mode=paper", p and p["mode"] == "paper")

    print("== 3. 挂单唯一：重复处理不重挂 ==")
    proc("BTC")
    check("仍只有 1 笔", pos_count("BTC") == 1)

    print("== 4. 挂单更新：新闭合K线 → 挂单价+SL 无条件跟随 ==")
    MOCK_SIG["BTC"]["upper_band"] = 100500
    MOCK_SIG["BTC"]["lower_band"] = 99500
    new_candle("BTC")
    MOCK_PRICE["BTC"] = 100600  # 高于新挂单价 100495 → 不穿越，正常更新挂单
    proc("BTC")
    p = pending_of("BTC")
    check("挂单价更新=99500*1.01=100495", p and abs(p["limit_price"] - 100495) < 1e-6,
          str(p and p["limit_price"]))
    check("SL 更新=99500*0.99=98505", p and abs(p["stop_loss_price"] - 98505) < 1e-6,
          str(p and p["stop_loss_price"]))

    print("== 5. 同一K线不重复更新 ==")
    proc("BTC")
    p = pending_of("BTC")
    check("挂单价不变", p and abs(p["limit_price"] - 100495) < 1e-6)

    print("== 6. 挂单成交：价格触及 → 系统转 OPEN（成交价=挂单价，SL 保留）==")
    MOCK_PRICE["BTC"] = 100400  # ≤ 100495 → 成交
    with storage.get_conn() as mc:
        trade_logic.update_paper_positions(mc)
    p = pos_of("BTC")
    check("转 OPEN", p and p["status"] == "OPEN")
    check("成交价=挂单价（maker 语义）", p and abs(p["entry_price"] - 100495) < 1e-6,
          str(p and p["entry_price"]))
    check("SL 保留（未补算ATR）", p and abs(p["stop_loss_price"] - 98505) < 1e-6,
          str(p and p["stop_loss_price"]))
    check("tp 仍 NULL", p and p["tp1_price"] is None and p["tp2_price"] is None)
    with storage.get_conn() as mc:
        jn = mc.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
    check("journal 无污染", jn == 0, f"journal={jn}")

    print("== 7. 持仓 SL 只朝有利方向 ==")
    set_sig("BTC", "UP", 100000, 99000)  # 轨道下移
    new_candle("BTC")
    proc("BTC")
    p = pos_of("BTC")
    check("轨道下移 SL 不放松（保持 98505）",
          p and abs(p["stop_loss_price"] - 98505) < 1e-6, str(p and p["stop_loss_price"]))
    MOCK_SIG["BTC"]["upper_band"] = 101000  # 轨道上移（直接改字段，避免 set_sig 重置 ts）
    MOCK_SIG["BTC"]["lower_band"] = 100000
    new_candle("BTC")
    proc("BTC")
    p = pos_of("BTC")
    check("轨道上移 SL 收紧=100000*0.99=99000",
          p and abs(p["stop_loss_price"] - 99000) < 1e-6, str(p and p["stop_loss_price"]))

    print("== 8. 系统扫损平仓 + 快照 + journal 无污染 ==")
    MOCK_PRICE["BTC"] = 98900  # ≤ 99000 → 扫损
    with storage.get_conn() as mc:
        trade_logic.update_paper_positions(mc)
        p = mc.execute(
            "SELECT * FROM trade_positions WHERE strategy='supertrend' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        p = dict(p)
        jn = mc.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
        la = mc.execute("SELECT COUNT(*) FROM trade_loss_archive").fetchone()[0]
    check("已平仓", p["status"] == "CLOSED")
    check("journal 无污染", jn == 0, f"journal={jn}")
    check("亏损归档无污染", la == 0, f"archive={la}")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        snap = sc.execute(
            "SELECT * FROM supertrend_close_snaps WHERE position_id=?",
            (p["id"],)).fetchone()
    check("平仓快照已记录", snap and snap["trend"] in ("UP", "DOWN"), str(dict(snap) if snap else None))

    print("== 9. 扫损后等K线闭合 → 价格在挂单价下方 → 直接市价开多（插针重开）==")
    proc("BTC")  # 扫损后同K线内 → 等待闭合，不立即重挂
    check("扫损后同K线不重挂", pos_of("BTC") is None and pending_of("BTC") is None)
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        row = sc.execute(
            "SELECT state FROM supertrend_status WHERE token='BTC'").fetchone()
    check("state=WAITING（等闭合）", row and row["state"] == "WAITING",
          str(dict(row) if row else None))
    new_candle("BTC")
    proc("BTC")
    p = pos_of("BTC")
    check("扫损后市价开多", p and p["status"] == "OPEN" and p["side"] == "LONG",
          str(p and (p["status"], p["side"])))

    print("== 10. 反转：趋势 DOWN（价格高于挂单价 → 市价开空）==")
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    set_sig("BTC", "DOWN", 101000, 99000)
    new_candle("BTC")
    MOCK_PRICE["BTC"] = 100000  # > 挂单价 99990 → 价格到位直接市价开空
    proc("BTC")
    p = pos_of("BTC")
    check("直接市价开空", p and p["status"] == "OPEN" and p["side"] == "SHORT",
          str(p and (p["status"], p["side"])))
    check("空单成交价=现价 100000", p and abs(p["entry_price"] - 100000) < 1e-6,
          str(p and p["entry_price"]))
    check("空单 SL=上轨道*1.01=102010",
          p and abs(p["stop_loss_price"] - 101000 * 1.01) < 1e-6, str(p and p["stop_loss_price"]))
    # 挂单路径：价格低于挂单价 → 正常挂单等涨
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    MOCK_PRICE["BTC"] = 99500  # < 99990 → 挂单
    new_candle("BTC")
    proc("BTC")
    p = pending_of("BTC")
    check("价格低于挂单价 → 正常挂空单", p and p["status"] == "PENDING" and p["side"] == "SHORT",
          str(p and (p["status"], p["side"])))
    check("空单挂单价=上轨道*0.99=99990",
          p and abs(p["limit_price"] - 99990) < 1e-6, str(p and p["limit_price"]))
    # 挂单成交：价格涨到挂单价 → 成交价=挂单价（maker 语义）
    MOCK_PRICE["BTC"] = 100500
    with storage.get_conn() as mc:
        trade_logic.update_paper_positions(mc)
    p = pos_of("BTC")
    check("空单成交价=挂单价（maker 语义）", p and abs(p["entry_price"] - 99990) < 1e-6,
          str(p and p["entry_price"]))
    check("空单 SL 保留", p and abs(p["stop_loss_price"] - 102010) < 1e-6)

    print("== 11. 移除监控：持仓平仓 / 挂单取消（幂等）==")
    ok1 = st.close_supertrend_position("BTC", "REMOVED")
    with storage.get_conn() as mc:
        p = mc.execute(
            "SELECT * FROM trade_positions WHERE token='BTC' AND strategy='supertrend' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        p = dict(p)
    check("持仓已平", ok1 and p["status"] == "CLOSED", str(p["status"]))
    ok2 = st.close_supertrend_position("BTC", "REMOVED")
    check("第二次幂等跳过", ok2 is False)
    # 挂单取消分支：重新挂单后移除 → CANCELED
    set_sig("BTC", "UP", 101000, 99000)
    new_candle("BTC")
    MOCK_PRICE["BTC"] = 100000
    proc("BTC")
    ok3 = st.close_supertrend_position("BTC", "REMOVED")
    with storage.get_conn() as mc:
        p3 = mc.execute(
            "SELECT * FROM trade_positions WHERE token='BTC' AND strategy='supertrend' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        p3 = dict(p3)
    check("挂单已取消", ok3 and p3["status"] == "CANCELED", str(p3["status"]))

    print("== 12. 设置热生效：容忍变化影响挂单价/SL ==")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_tokens SET status='ACTIVE' WHERE token='BTC'")
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    set_sig("BTC", "UP", 101000, 99000)
    MOCK_PRICE["BTC"] = 101200  # 高于挂单价(2%容忍=100980) → 正常挂单
    proc("BTC", tol_sl=2.0, tol_order=2.0)
    p = pending_of("BTC")
    check("SL容忍2% → SL=99000*0.98=97020",
          p and abs(p["stop_loss_price"] - 97020) < 1e-6, str(p and p["stop_loss_price"]))
    check("挂单容忍2% → 挂单价=99000*1.02=100980",
          p and abs(p["limit_price"] - 100980) < 1e-6, str(p and p["limit_price"]))

    print("== 13. 陈旧数据不挂单 ==")
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    set_sig("BTC", "UP", 101000, 99000, offset_candles=-3)  # 45分钟前
    proc("BTC")
    check("陈旧信号不挂单", pending_of("BTC") is None)
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        row = sc.execute(
            "SELECT fetch_error FROM supertrend_status WHERE token='BTC'").fetchone()
    check("status 表记 stale_data", row and row["fetch_error"] == "stale_data",
          str(dict(row) if row else None))

    print("== 14. 新币首次处理失败 → fetch_error 可见（upsert）==")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("DELETE FROM supertrend_status WHERE token='SOL'")
        sc.execute("INSERT OR IGNORE INTO supertrend_tokens (token, status, added_at) "
                   "VALUES ('SOL', 'ACTIVE', 0)")
    MOCK_SIG.pop("SOL", None)
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "SOL", 1.0, 1.0, 50.0, 3.0)
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        row = sc.execute("SELECT fetch_error FROM supertrend_status WHERE token='SOL'").fetchone()
    check("新币首次拉取失败 → 有错误行", row and row["fetch_error"] == "okx_fetch_failed",
          str(dict(row) if row else None))
    set_sig("SOL", "UP", 200, 190)
    MOCK_PRICE.pop("SOL", None)
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "SOL", 1.0, 1.0, 50.0, 3.0)
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        row = sc.execute("SELECT fetch_error FROM supertrend_status WHERE token='SOL'").fetchone()
    check("新币首次无标记价 → mark_price_failed",
          row and row["fetch_error"] == "mark_price_failed", str(dict(row) if row else None))
    MOCK_PRICE["SOL"] = 195
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "SOL", 1.0, 1.0, 50.0, 3.0)
    # 首轮记基准（WAITING），新闭合K线后才挂单
    new_candle("SOL")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "SOL", 1.0, 1.0, 50.0, 3.0)
    p = pending_of("SOL")
    check("恢复后正常挂单", p and p["status"] == "PENDING", str(p and p["status"]))

    print("== 15. 线程单轮流程（_run_round 端到端）==")
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_tokens SET status='ACTIVE' WHERE token='BTC'")
        sc.execute("UPDATE supertrend_tokens SET status='REMOVED' WHERE token IN ('ETH','SOL')")
    with storage.get_conn() as mc:
        storage.trading_settings_update(mc, {"supertrend_enabled": False})
    set_sig("BTC", "UP", 101000, 99000)
    MOCK_PRICE["BTC"] = 100000
    st._run_round()
    check("开关关闭时不挂单", pending_of("BTC") is None)
    with storage.get_conn() as mc:
        storage.trading_settings_update(mc, {"supertrend_enabled": True})
    new_candle("BTC")
    st._run_round()
    check("开关打开后自动挂单", pending_of("BTC") is not None)
    # REMOVED 币种挂单被安全网清理
    set_sig("ETH", "UP", 4000, 3900)
    MOCK_PRICE["ETH"] = 3950
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._place_order(mc, "ETH", MOCK_SIG["ETH"], MOCK_PRICE["ETH"], 1.0, 1.0, 50.0, 3.0)
    st._run_round()
    with storage.get_conn() as mc:
        eth_pos = mc.execute(
            "SELECT status FROM trade_positions WHERE token='ETH' AND strategy='supertrend' "
            "ORDER BY id DESC LIMIT 1").fetchone()
    check("REMOVED 币种挂单被清理", eth_pos and eth_pos["status"] == "CANCELED",
          str(dict(eth_pos) if eth_pos else None))

    print("== 16. OKX 取数逻辑（真实 _fetch + mock requests）==")
    import time as _t
    st._fetch_okx_supertrend = REAL_FETCH

    class FakeResp:
        def __init__(self, data, status=200):
            self._data = data
            self.status_code = status

        def json(self):
            return self._data

    def okx_body(*pairs):
        rows = [
            {"ts": ts, "values": {"superTrend": str(up), "direction": "1",
                                  "trend": tr, "upperBand": str(up), "lowerBand": str(lo)}}
            for ts, (tr, up, lo) in pairs]
        return {"code": "0", "data": [{"data": [{
            "instId": "BTC-USDT-SWAP",
            "timeframes": {"15m": {"indicators": {"supertrend": rows}}},
        }], "mode": "live", "summary": {}, "timestamp": 0}], "msg": ""}

    now_ms = int(_t.time() * 1000)
    forming = (now_ms // CANDLE) * CANDLE
    closed = forming - CANDLE
    st.requests.post = lambda url, json=None, timeout=None: FakeResp(okx_body(
        (closed, ("UP", 101000, 99000)), (forming, ("UP", 101200, 99200))))
    sig = st._fetch_okx_supertrend("BTC")
    check("主路径命中最新闭合K线", sig and sig["ts"] == closed, str(sig))
    check("取闭合值而非形成中", sig and abs(sig["upper_band"] - 101000) < 1e-6)
    old1 = closed - CANDLE
    old2 = closed - 2 * CANDLE
    st.requests.post = lambda url, json=None, timeout=None: FakeResp(okx_body(
        (old2, ("DOWN", 95000, 93000)), (old1, ("DOWN", 96000, 94000))))
    sig = st._fetch_okx_supertrend("BTC", attempts=1)
    check("延迟两根都旧 → 返回 None", sig is None, str(sig))
    st.requests.post = lambda url, json=None, timeout=None: FakeResp(okx_body(
        (forming, ("UP", 1, 2))))
    sig = st._fetch_okx_supertrend("BTC", attempts=1)
    check("只有形成中 → None", sig is None, str(sig))
    st.requests.post = lambda url, json=None, timeout=None: FakeResp(okx_body(
        (closed, ("UP", "NaN", "99000")), (forming, ("UP", 101200, 99200))))
    sig = st._fetch_okx_supertrend("BTC", attempts=1)
    check("NaN 轨道 → None（防 SL 失效持仓裸奔）", sig is None, str(sig))
    st.requests.post = lambda url, json=None, timeout=None: FakeResp(okx_body(
        (closed, ("UP", "Infinity", "99000"))))
    sig = st._fetch_okx_supertrend("BTC", attempts=1)
    check("Infinity 轨道 → None", sig is None, str(sig))
    st.requests.post = lambda url, json=None, timeout=None: FakeResp(okx_body(
        (closed, ("UP", 101000, "-5"))))
    sig = st._fetch_okx_supertrend("BTC", attempts=1)
    check("负轨道 → None", sig is None, str(sig))
    st._fetch_okx_supertrend = lambda token: MOCK_SIG.get(token)

    print("== 17. 参数防御：异常值回退默认 ==")
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
        storage.trading_settings_update(mc, {
            "supertrend_enabled": True,
            "supertrend_sl_tolerance_pct": -5.0,
            "supertrend_order_tolerance_pct": -3.0,
        })
    set_sig("BTC", "UP", 101000, 99000)
    MOCK_PRICE["BTC"] = 100000
    st._run_round()
    p = pending_of("BTC")
    check("容忍负数回退默认 → SL=99000*0.99",
          p and abs(p["stop_loss_price"] - 98010) < 1e-6, str(p and p["stop_loss_price"]))
    check("挂单容忍负数回退默认 → 挂单价=99000*1.01",
          p and abs(p["limit_price"] - 99990) < 1e-6, str(p and p["limit_price"]))
    with storage.get_conn() as mc:
        storage.trading_settings_update(mc, {
            "supertrend_sl_tolerance_pct": 1.0,
            "supertrend_order_tolerance_pct": 1.0,
        })

    print("== 18. 旧库迁移：无 forming 列 → init_db 自动加列且数据保留 ==")
    old_db = os.path.join(_TMP, "old_supertrend.db")
    with storage.get_conn(old_db) as conn:
        conn.executescript("""
        CREATE TABLE supertrend_tokens (
            token TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'ACTIVE',
            added_at INTEGER, removed_at INTEGER);
        CREATE TABLE supertrend_status (
            token TEXT PRIMARY KEY, trend TEXT, upper_band REAL, lower_band REAL,
            last_candle_ts INTEGER, last_sl REAL, last_price REAL,
            state TEXT, fetch_error TEXT, updated_at INTEGER);
        """)
        conn.execute("INSERT INTO supertrend_status (token, trend, upper_band, lower_band, "
                     "last_sl, last_price, state, updated_at) "
                     "VALUES ('BTC','UP',1,2,3,4,'HOLDING',5)")
    config.SUPERTREND_DB = old_db
    st.init_db()
    with storage.get_conn(old_db) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(supertrend_status)").fetchall()]
        row = conn.execute(
            "SELECT trend, last_sl, forming_trend FROM supertrend_status WHERE token='BTC'"
        ).fetchone()
        tok_cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(supertrend_tokens)").fetchall()]
    check("旧库自动加 3 列",
          all(c in cols for c in ("forming_trend", "forming_upper_band", "forming_lower_band")),
          str(cols))
    check("旧数据保留且 forming 为 NULL",
          row and row["trend"] == "UP" and row["last_sl"] == 3
          and row["forming_trend"] is None, str(dict(row) if row else None))
    check("tokens 表自动加 9 列",
          all(c in tok_cols for c in ("notional_usd", "sl_tolerance_pct",
                                      "order_tolerance_pct", "leverage", "direction",
                                      "flip_market_open", "paused", "sort_order",
                                      "close_on_reverse")),
          str(tok_cols))
    config.SUPERTREND_DB = os.path.join(_TMP, "supertrend.db")

    print("== 18b. 主库唯一索引迁移：旧版 → 豁免版且不被第二段覆盖 ==")
    with storage.get_conn() as mc:
        mc.execute("DROP INDEX idx_trade_one_active_token")
        mc.execute("""
            CREATE UNIQUE INDEX idx_trade_one_active_token
            ON trade_positions(token, side, strategy)
            WHERE status IN ('PENDING', 'OPEN', 'PARTIAL')
        """)
        try:
            storage._migrate(mc)
        except Exception as e:
            # 测试库缺 _migrate 尾部引用的表（生产库由其他迁移创建）；
            # 索引迁移在 _migrate 开头已执行完，尾部异常可忽略
            print(f"  （_migrate 尾部缺表跳过：{e}）")
        idx = mc.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_trade_one_active_token'"
        ).fetchone()
    check("旧索引迁移为豁免版且未被覆盖",
          idx and "strategy != 'supertrend'" in (idx["sql"] or ""),
          str(idx["sql"] if idx else None))

    print("== 19. 策略重置：清仓位/信号状态，保留监控币种 ==")
    set_sig("BTC", "UP", 101000, 99000)
    MOCK_PRICE["BTC"] = 100000
    new_candle("BTC")
    proc("BTC")  # 确保有挂单和状态
    with storage.get_conn() as mc:
        result = storage.trade_reset_strategy(mc, "supertrend")
    check("仓位已清", result.get("positions_deleted", 0) >= 0
          and pending_of("BTC") is None and pos_of("BTC") is None)
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        st_cnt = sc.execute("SELECT COUNT(*) FROM supertrend_status").fetchone()[0]
        snap_cnt = sc.execute("SELECT COUNT(*) FROM supertrend_close_snaps").fetchone()[0]
        tok_cnt = sc.execute("SELECT COUNT(*) FROM supertrend_tokens").fetchone()[0]
    check("信号状态已清", st_cnt == 0, f"status={st_cnt}")
    check("平仓快照已清", snap_cnt == 0, f"snaps={snap_cnt}")
    check("监控币种保留", tok_cnt >= 1, f"tokens={tok_cnt}")
    # 重置后线程先记基准等待，下一根闭合K线后恢复挂单
    st._run_round()
    check("重置后首轮不挂单（等待闭合）", pending_of("BTC") is None)
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        row = sc.execute(
            "SELECT state FROM supertrend_status WHERE token='BTC'").fetchone()
    check("state=WAITING", row and row["state"] == "WAITING",
          str(dict(row) if row else None))
    new_candle("BTC")
    st._run_round()
    check("新K线后自动恢复挂单", pending_of("BTC") is not None)

    print("== 20. 挂单价在错误一侧 → 价格到位直接市价开仓 ==")
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    # 用户场景：趋势 DOWN，上轨道 79000，挂单容忍 2.5% → 挂单价 77025 < 现价 78000
    set_sig("BTC", "DOWN", 79000, 77000)
    new_candle("BTC")
    new_candle("BTC")
    new_candle("BTC")
    MOCK_PRICE["BTC"] = 78000
    proc("BTC", tol_sl=1.0, tol_order=2.5)
    p = pos_of("BTC")
    check("直接市价开空（非挂单）", p and p["status"] == "OPEN" and p["side"] == "SHORT",
          str(p and (p["status"], p["side"])))
    check("成交价=现价 78000", p and abs(p["entry_price"] - 78000) < 1e-6,
          str(p and p["entry_price"]))
    check("SL=上轨道*1.01=79790", p and abs(p["stop_loss_price"] - 79790) < 1e-6,
          str(p and p["stop_loss_price"]))
    # 反向验证：挂单容忍正常（1%）→ 挂单价 78210 > 现价 → 正常挂单
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    set_sig("BTC", "DOWN", 79000, 77000)
    new_candle("BTC")
    proc("BTC", tol_sl=1.0, tol_order=1.0)
    p2 = pending_of("BTC")
    check("容忍1%时正常挂单（挂单价=79000*0.99=78210）",
          p2 and p2["status"] == "PENDING" and abs(p2["limit_price"] - 78210) < 1e-6,
          str(p2 and (p2["status"], p2["limit_price"])))

    print("== 21. 挂单更新穿越现价 → 直接市价开仓 ==")
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    set_sig("BTC", "DOWN", 79000, 77000)
    new_candle("BTC")
    new_candle("BTC")
    MOCK_PRICE["BTC"] = 78000
    proc("BTC", tol_sl=1.0, tol_order=1.0)  # 挂单价 78210 > 现价 → 正常挂空单
    p = pending_of("BTC")
    check("正常挂空单", p and p["status"] == "PENDING", str(p and p["status"]))
    # 轨道上移 → 新挂单价 78000*0.99=77220 < 现价 78000 → 穿越
    MOCK_SIG["BTC"]["upper_band"] = 78000
    MOCK_SIG["BTC"]["lower_band"] = 76000
    new_candle("BTC")
    proc("BTC", tol_sl=1.0, tol_order=1.0)
    p = pos_of("BTC")
    check("穿越后直接市价开空", p and p["status"] == "OPEN" and p["side"] == "SHORT",
          str(p and (p["status"], p["side"])))
    check("成交价=现价 78000", p and abs(p["entry_price"] - 78000) < 1e-6,
          str(p and p["entry_price"]))
    check("SL=新上轨道*1.01=78780", p and abs(p["stop_loss_price"] - 78780) < 1e-6,
          str(p and p["stop_loss_price"]))
    check("quantity 按现价重算（名义不漂移）",
          p and abs(p["quantity"] * p["entry_price"] - p["notional"]) < 1e-6,
          str(p and (p["quantity"], p["notional"])))

    print("== 22. per-token 参数覆盖全局 + 越界回退 ==")
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("DELETE FROM supertrend_status")
        sc.execute("UPDATE supertrend_tokens SET status='ACTIVE' WHERE token='BTC'")
        sc.execute("UPDATE supertrend_tokens SET status='REMOVED' WHERE token IN ('ETH','SOL')")
        sc.execute("UPDATE supertrend_tokens SET sl_tolerance_pct=2.0, "
                   "order_tolerance_pct=2.0, notional_usd=100.0, leverage=5.0 "
                   "WHERE token='BTC'")
    set_sig("BTC", "UP", 101000, 99000)
    MOCK_PRICE["BTC"] = 101500  # 高于挂单价 100980 → 正常挂单（不触发市价开）
    new_candle("BTC")
    st._run_round()  # 首轮记基准
    new_candle("BTC")
    st._run_round()  # 挂单：per-token 参数生效
    p = pending_of("BTC")
    check("per-token SL容忍2% → SL=99000*0.98=97020",
          p and abs(p["stop_loss_price"] - 97020) < 1e-6, str(p and p["stop_loss_price"]))
    check("per-token 挂单容忍2% → 挂单价=99000*1.02=100980",
          p and abs(p["limit_price"] - 100980) < 1e-6, str(p and p["limit_price"]))
    check("per-token 名义100 → notional=100",
          p and abs(float(p["notional"]) - 100) < 1e-6, str(p and p["notional"]))
    check("per-token 杠杆5 → margin=20",
          p and abs(float(p["margin_amount"]) - 20) < 1e-6, str(p and p["margin_amount"]))
    # 越界回退全局（per-token 覆盖+防御在 _run_round 层）
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_tokens SET sl_tolerance_pct=99.0 WHERE token='BTC'")
        sc.execute("DELETE FROM supertrend_status")
    set_sig("BTC", "UP", 101000, 99000)
    MOCK_PRICE["BTC"] = 101500  # 高于挂单价 100980（挂单容忍仍 2%）→ 正常挂单
    new_candle("BTC")
    st._run_round()
    new_candle("BTC")
    st._run_round()
    p = pending_of("BTC")
    check("容忍99越界 → 回退全局1% → SL=98010",
          p and abs(p["stop_loss_price"] - 98010) < 1e-6, str(p and p["stop_loss_price"]))
    # 脏值回退（非数字字符串 → 回退全局，不得拖垮整币跳过处理）
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_tokens SET sl_tolerance_pct='abc' WHERE token='BTC'")
        sc.execute("DELETE FROM supertrend_status")
    new_candle("BTC")
    st._run_round()
    new_candle("BTC")
    st._run_round()
    p = pending_of("BTC")
    check("脏值容忍回退全局 → 正常挂单 SL=98010",
          p and abs(p["stop_loss_price"] - 98010) < 1e-6, str(p and p["stop_loss_price"]))
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_tokens SET sl_tolerance_pct=NULL, "
                   "order_tolerance_pct=NULL, notional_usd=NULL, leverage=NULL "
                   "WHERE token='BTC'")

    print("== 23. 只做多：DOWN 不挂单（WAIT_DIR）→ 翻 UP 挂多单 ==")
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("DELETE FROM supertrend_status")
        sc.execute("UPDATE supertrend_tokens SET direction='LONG_ONLY' WHERE token='BTC'")
    set_sig("BTC", "DOWN", 101000, 99000)
    MOCK_PRICE["BTC"] = 100000
    new_candle("BTC")
    new_candle("BTC")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "LONG_ONLY")
    check("DOWN 不挂单", pending_of("BTC") is None and pos_of("BTC") is None)
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        row = sc.execute("SELECT state FROM supertrend_status WHERE token='BTC'").fetchone()
    check("state=WAIT_DIR", row and row["state"] == "WAIT_DIR",
          str(dict(row) if row else None))
    set_sig("BTC", "UP", 101000, 99000)
    new_candle("BTC")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "LONG_ONLY")
    p = pending_of("BTC")
    check("翻 UP 后挂多单", p and p["status"] == "PENDING" and p["side"] == "LONG",
          str(p and (p["status"], p["side"])))

    print("== 24. 只做多：扫损后 DOWN → 不重挂 → 翻 UP 重挂 ==")
    MOCK_PRICE["BTC"] = 99990  # 触及挂单价 → 成交
    with storage.get_conn() as mc:
        trade_logic.update_paper_positions(mc)
    p = pos_of("BTC")
    check("挂单成交 OPEN", p and p["status"] == "OPEN", str(p and p["status"]))
    MOCK_PRICE["BTC"] = 97900  # ≤ SL 98010 → 扫损
    with storage.get_conn() as mc:
        trade_logic.update_paper_positions(mc)
    check("扫损平仓", pos_of("BTC") is None)
    set_sig("BTC", "DOWN", 101000, 99000)
    new_candle("BTC")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "LONG_ONLY")
    check("扫损后 DOWN 不重挂", pending_of("BTC") is None and pos_of("BTC") is None)
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        row = sc.execute("SELECT state FROM supertrend_status WHERE token='BTC'").fetchone()
    check("state=WAIT_DIR（等UP）", row and row["state"] == "WAIT_DIR",
          str(dict(row) if row else None))
    set_sig("BTC", "UP", 101000, 99000)
    new_candle("BTC")
    new_candle("BTC")
    MOCK_PRICE["BTC"] = 101000  # 高于挂单价 99990 → 正常挂多单（等回调）
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "LONG_ONLY")
    p = pending_of("BTC")
    check("翻 UP 后重挂多单", p and p["status"] == "PENDING" and p["side"] == "LONG",
          str(p and (p["status"], p["side"])))

    print("== 25. 只做多：趋势翻 DOWN → 挂单取消（WAIT_DIR）==")
    set_sig("BTC", "DOWN", 101000, 99000)
    new_candle("BTC")
    new_candle("BTC")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "LONG_ONLY")
    with storage.get_conn() as mc:
        row = mc.execute(
            "SELECT status, advice FROM trade_positions WHERE token='BTC' AND "
            "strategy='supertrend' ORDER BY id DESC LIMIT 1").fetchone()
        row = dict(row)
    check("挂单已取消 CANCELED", row["status"] == "CANCELED", str(row["status"]))
    check("advice 含方向限制", "方向限制" in (row["advice"] or ""), str(row["advice"]))
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        st_row = sc.execute("SELECT state FROM supertrend_status WHERE token='BTC'").fetchone()
    check("state=WAIT_DIR", st_row and st_row["state"] == "WAIT_DIR",
          str(dict(st_row) if st_row else None))
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_tokens SET direction='BOTH' WHERE token='BTC'")

    print("== 26. 非法 direction 值 → 回退 BOTH ==")
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_tokens SET direction='XXX' WHERE token='BTC'")
        sc.execute("DELETE FROM supertrend_status")
    set_sig("BTC", "UP", 101000, 99000)
    MOCK_PRICE["BTC"] = 100000
    new_candle("BTC")
    st._run_round()  # 首轮记基准
    new_candle("BTC")
    st._run_round()  # 挂单
    p = pending_of("BTC")
    check("非法 direction 回退 BOTH → 正常挂单",
          p and p["status"] == "PENDING" and p["side"] == "LONG",
          str(p and (p["status"], p["side"])))
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_tokens SET direction='BOTH' WHERE token='BTC'")

    print("== 27. 反转市价开选项（flip_market_open）==")
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("DELETE FROM supertrend_status")
        sc.execute("UPDATE supertrend_tokens SET direction='LONG_ONLY', "
                   "flip_market_open=1 WHERE token='BTC'")
    # 场景1a：DOWN（WAIT_DIR）→ 翻 UP → 反转：市价开多 + 同轮挂单并存
    set_sig("BTC", "DOWN", 101000, 99000)
    MOCK_PRICE["BTC"] = 100000
    new_candle("BTC")
    new_candle("BTC")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "LONG_ONLY", True)
    check("DOWN 不挂单（WAIT_DIR）", pending_of("BTC") is None and pos_of("BTC") is None)
    set_sig("BTC", "UP", 101000, 99000)
    new_candle("BTC")
    new_candle("BTC")
    new_candle("BTC")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "LONG_ONLY", True)
    p = pos_of("BTC")
    pend = pending_of("BTC")
    check("反转后市价开多", p and p["status"] == "OPEN" and p["side"] == "LONG",
          str(p and (p["status"], p["side"])))
    check("成交价=现价 100000", p and abs(p["entry_price"] - 100000) < 1e-6,
          str(p and p["entry_price"]))
    check("open_reason 含反转", p and "反转" in (p["open_reason"] or ""),
          str(p and p["open_reason"]))
    check("同轮挂单并存", pend and pend["status"] == "PENDING",
          str(pend and pend["status"]))
    check("挂单价=99000*1.01=99990", pend and abs(pend["limit_price"] - 99990) < 1e-6,
          str(pend and pend["limit_price"]))
    # 场景1b：并存期间新K线 → 持仓 SL 跟随 + 挂单跟随（独立处理）
    MOCK_SIG["BTC"]["upper_band"] = 100500
    MOCK_SIG["BTC"]["lower_band"] = 99500
    new_candle("BTC")
    MOCK_PRICE["BTC"] = 100600  # 高于新挂单价 100495 → 挂单正常更新
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "LONG_ONLY", True)
    p = pos_of("BTC")
    pend = pending_of("BTC")
    check("并存期间 SL 上移=99500*0.99=98505",
          p and abs(p["stop_loss_price"] - 98505) < 1e-6, str(p and p["stop_loss_price"]))
    check("并存期间挂单价更新=99500*1.01=100495",
          pend and abs(pend["limit_price"] - 100495) < 1e-6,
          str(pend and pend["limit_price"]))
    check("并存期间挂单 SL 同步更新=98505",
          pend and abs(pend["stop_loss_price"] - 98505) < 1e-6,
          str(pend and pend["stop_loss_price"]))
    # 场景2：挂单成交 → 两笔合并（数量相加、开仓价加权、被并入行 CANCELED）
    # 合并由 _run_round 每轮开头兜底执行（不依赖 OKX 取数）→ 走真实入口验证
    MOCK_PRICE["BTC"] = 100400  # ≤ 挂单价 100495 → 成交
    with storage.get_conn() as mc:
        trade_logic.update_paper_positions(mc)
    with storage.get_conn() as mc:
        storage.trading_settings_update(mc, {"supertrend_enabled": True})
    st._run_round()
    with storage.get_conn() as mc:
        rows = mc.execute(
            "SELECT * FROM trade_positions WHERE token='BTC' AND strategy='supertrend' "
            "AND status IN ('OPEN','PARTIAL') ORDER BY id ASC").fetchall()
        cans = mc.execute(
            "SELECT advice FROM trade_positions WHERE token='BTC' AND "
            "strategy='supertrend' AND status='CANCELED'").fetchall()
    check("合并后只剩一笔 OPEN", len(rows) == 1 and rows[0]["status"] == "OPEN",
          f"rows={len(rows)}")
    r = dict(rows[0])
    q_expected = 50 / 100000 + 50 / 100495
    check("数量相加", r and abs(float(r["quantity"]) - q_expected) < 1e-9,
          f"{r and r['quantity']} vs {q_expected}")
    entry_expected = (50 + 50) / q_expected
    check("开仓价加权平均", r and abs(float(r["entry_price"]) - entry_expected) < 1e-6,
          f"{r and r['entry_price']} vs {entry_expected}")
    check("名义相加=100", r and abs(float(r["notional"]) - 100) < 1e-6,
          str(r and r["notional"]))
    check("被并入行 CANCELED 且 advice 含并入",
          len(cans) >= 1 and "并入" in (cans[-1]["advice"] or ""),
          str([c["advice"] for c in cans]))
    # 场景3：扫损后趋势仍 UP（非反转）→ 只挂单，不市价开
    MOCK_PRICE["BTC"] = 98400  # ≤ SL 98505 → 扫损
    with storage.get_conn() as mc:
        trade_logic.update_paper_positions(mc)
    check("扫损平仓", pos_of("BTC") is None)
    MOCK_SIG["BTC"]["upper_band"] = 100000
    MOCK_SIG["BTC"]["lower_band"] = 99000
    new_candle("BTC")
    MOCK_PRICE["BTC"] = 100000
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "LONG_ONLY", True)
    p = pos_of("BTC")
    pend = pending_of("BTC")
    check("扫损后仍 UP → 只挂单（无市价开）",
          p is None and pend and pend["status"] == "PENDING",
          str((p and p["status"], pend and pend["status"])))
    # 场景4：首次加入即 UP（非反转）→ 首轮等闭合，之后只挂单
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("DELETE FROM supertrend_status")
    set_sig("BTC", "UP", 101000, 99000)
    new_candle("BTC")
    new_candle("BTC")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "LONG_ONLY", True)
    check("首次即 UP → 首轮等闭合不市价开",
          pos_of("BTC") is None and pending_of("BTC") is None)
    new_candle("BTC")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "LONG_ONLY", True)
    p = pos_of("BTC")
    pend = pending_of("BTC")
    check("下一根K线 → 只挂单（无市价开）",
          p is None and pend and pend["status"] == "PENDING",
          str((p and p["status"], pend and pend["status"])))
    # 场景5：flip=0（默认）→ 翻 UP 只挂单
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("DELETE FROM supertrend_status")
        sc.execute("UPDATE supertrend_tokens SET flip_market_open=0 WHERE token='BTC'")
    set_sig("BTC", "DOWN", 101000, 99000)
    new_candle("BTC")
    new_candle("BTC")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "LONG_ONLY", False)
    set_sig("BTC", "UP", 101000, 99000)
    new_candle("BTC")
    new_candle("BTC")
    new_candle("BTC")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "LONG_ONLY", False)
    p = pos_of("BTC")
    pend = pending_of("BTC")
    check("flip=0 翻 UP → 只挂单（无市价开）",
          p is None and pend and pend["status"] == "PENDING",
          str((p and p["status"], pend and pend["status"])))
    # 场景6：BOTH + flip=1 → 反转选项无效（原逻辑）
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("DELETE FROM supertrend_status")
        sc.execute("UPDATE supertrend_tokens SET direction='BOTH' WHERE token='BTC'")
    set_sig("BTC", "UP", 101000, 99000)
    new_candle("BTC")
    new_candle("BTC")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "BOTH", True)
    p = pos_of("BTC")
    pend = pending_of("BTC")
    check("BOTH+flip=1 → 首轮等闭合不市价开",
          p is None and pending_of("BTC") is None)
    new_candle("BTC")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "BOTH", True)
    p = pos_of("BTC")
    pend = pending_of("BTC")
    check("BOTH+flip=1 → 只挂单（无市价开）",
          p is None and pend and pend["status"] == "PENDING",
          str((p and p["status"], pend and pend["status"])))
    # 场景7：反转轮挂单价已到位 → 跳过第二笔（只市价开一笔）
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("DELETE FROM supertrend_status")
        sc.execute("UPDATE supertrend_tokens SET direction='LONG_ONLY', "
                   "flip_market_open=1 WHERE token='BTC'")
    set_sig("BTC", "DOWN", 101000, 99000)
    MOCK_PRICE["BTC"] = 100000
    new_candle("BTC")
    new_candle("BTC")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "LONG_ONLY", True)
    set_sig("BTC", "UP", 101000, 99000)
    new_candle("BTC")
    new_candle("BTC")
    new_candle("BTC")
    MOCK_PRICE["BTC"] = 98900  # 低于挂单价 99990 → 价格已到位
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "LONG_ONLY", True)
    p = pos_of("BTC")
    check("反转轮价格已到位 → 只市价开一笔（不挂第二笔）",
          p and p["status"] == "OPEN" and pending_of("BTC") is None,
          str((p and p["status"], pending_of("BTC") and pending_of("BTC")["status"])))
    # 场景8：SHORT_ONLY + flip=1 对称：UP（WAIT_DIR）→ 翻 DOWN → 市价开空 + 挂空单
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("DELETE FROM supertrend_status")
        sc.execute("UPDATE supertrend_tokens SET direction='SHORT_ONLY' WHERE token='BTC'")
    set_sig("BTC", "UP", 101000, 99000)
    MOCK_PRICE["BTC"] = 100000
    new_candle("BTC")
    new_candle("BTC")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "SHORT_ONLY", True)
    check("UP 不挂单（SHORT_ONLY）", pending_of("BTC") is None and pos_of("BTC") is None)
    set_sig("BTC", "DOWN", 101000, 99000)
    new_candle("BTC")
    new_candle("BTC")
    new_candle("BTC")
    MOCK_PRICE["BTC"] = 99000  # 低于空单挂单价 99990 → 挂空单正常（不触发市价）
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "SHORT_ONLY", True)
    p = pos_of("BTC")
    pend = pending_of("BTC")
    check("翻 DOWN → 市价开空", p and p["status"] == "OPEN" and p["side"] == "SHORT",
          str(p and (p["status"], p["side"])))
    check("空单 SL=上轨道*1.01=102010",
          p and abs(p["stop_loss_price"] - 102010) < 1e-6, str(p and p["stop_loss_price"]))
    check("同轮挂空单并存", pend and pend["status"] == "PENDING" and pend["side"] == "SHORT",
          str(pend and (pend["status"], pend["side"])))
    check("空单挂单价=上轨道*0.99=99990",
          pend and abs(pend["limit_price"] - 99990) < 1e-6,
          str(pend and pend["limit_price"]))
    # 场景9：并存时移除监控 → 挂单取消 + 持仓平掉（不残留持仓）
    ok = st.close_supertrend_position("BTC", "REMOVED")
    with storage.get_conn() as mc:
        pend_left = mc.execute(
            "SELECT status FROM trade_positions WHERE token='BTC' AND "
            "strategy='supertrend' AND status='PENDING'").fetchone()
        closed_row = mc.execute(
            "SELECT status, advice FROM trade_positions WHERE token='BTC' AND "
            "strategy='supertrend' AND status='CLOSED' ORDER BY id DESC LIMIT 1").fetchone()
    check("移除并存：挂单已取消", pend_left is None,
          str(dict(pend_left) if pend_left else None))
    check("移除并存：持仓已平", pos_of("BTC") is None,
          str(pos_of("BTC") and pos_of("BTC")["status"]))
    check("移除并存：返回 True（实际处理）", ok is True, str(ok))
    check("持仓行 CLOSED 且 advice 含移除",
          closed_row and closed_row["status"] == "CLOSED"
          and "移除" in (closed_row["advice"] or ""),
          str(dict(closed_row) if closed_row else None))
    ok2 = st.close_supertrend_position("BTC", "REMOVED")
    check("再次调用幂等 False", ok2 is False, str(ok2))
    # 恢复默认
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_tokens SET direction='BOTH', "
                   "flip_market_open=0 WHERE token='BTC'")

    print("== 28. 暂停监控 + 反转 TG ==")
    # 暂停：线程跳过（不拉信号不处理，持仓/挂单不动）
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("DELETE FROM supertrend_status")
        sc.execute("UPDATE supertrend_tokens SET paused=1 WHERE token='BTC'")
    set_sig("BTC", "UP", 101000, 99000)
    MOCK_PRICE["BTC"] = 100000
    new_candle("BTC")
    new_candle("BTC")
    st._run_round()
    check("暂停时线程不挂单", pending_of("BTC") is None and pos_of("BTC") is None)
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        row = sc.execute("SELECT * FROM supertrend_status WHERE token='BTC'").fetchone()
    check("暂停时 status 无写入", row is None, str(dict(row) if row else None))
    # 暂停期间持仓/挂单仍由 2s 循环管理：先挂单成交再扫损（暂停前已有仓位场景的等价验证）
    # 恢复（API 清 status，这里直接模拟）：走首次流程
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_tokens SET paused=0 WHERE token='BTC'")
    st._run_round()
    check("恢复后首轮等闭合不挂单", pending_of("BTC") is None and pos_of("BTC") is None)
    new_candle("BTC")
    st._run_round()
    check("新K线后正常挂单", pending_of("BTC") is not None)
    # 反转 TG：全局 mock 记录调用（顶部已屏蔽真实发送）
    TG_CALLS.clear()
    # 有挂单 + 趋势翻转（UP→DOWN）→ 触发
    set_sig("BTC", "DOWN", 102000, 100000)
    new_candle("BTC")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "BOTH", False)
    check("趋势翻转触发 TG 一次", len(TG_CALLS) == 1, f"calls={len(TG_CALLS)}")
    check("TG 参数正确（BTC UP→DOWN）",
          TG_CALLS and TG_CALLS[0][0] == "BTC" and TG_CALLS[0][1] == "UP"
          and TG_CALLS[0][2] == "DOWN", str(TG_CALLS))
    # 同K线第二轮 → 不重复触发
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "BOTH", False)
    check("同K线不重复触发", len(TG_CALLS) == 1, f"calls={len(TG_CALLS)}")
    # 首次（status 清空后无旧趋势）→ 不触发
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("DELETE FROM supertrend_status")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "BOTH", False)
    check("首次无旧趋势不触发", len(TG_CALLS) == 1, f"calls={len(TG_CALLS)}")
    # 清理
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("DELETE FROM supertrend_status")
        sc.execute("UPDATE supertrend_tokens SET paused=0 WHERE token='BTC'")

    print("== 30. 合并兜底：OKX 取数失败/暂停币也要合并同向多笔 ==")
    import supertrend_live
    supertrend_live.sync_supertrend_live = lambda: None  # 本组只验合并，屏蔽 C 段
    with storage.get_conn() as mc:
        storage.trading_settings_update(mc, {"supertrend_enabled": True})

    def _mk_rows(token, qty1, qty2):
        with storage.get_conn() as conn:
            for q in (qty1, qty2):
                conn.execute(
                    "INSERT INTO trade_positions (token, symbol, side, status, mode, "
                    "margin_amount, leverage, notional, quantity, entry_price, "
                    "current_price, stop_loss_price, strategy) "
                    "VALUES (?, ?, 'LONG', 'OPEN', 'paper', 50, 3, ?, ?, 100, 100, 95, "
                    "'supertrend')",
                    (token, f"{token}USDT", q * 100, q))

    def _active_rows(token):
        with storage.get_conn() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM trade_positions WHERE token=? AND strategy='supertrend' "
                "AND status IN ('OPEN','PARTIAL')", (token,)).fetchall()]

    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("INSERT OR REPLACE INTO supertrend_tokens "
                   "(token, status, added_at, live_mode, paused) "
                   "VALUES ('MGX', 'ACTIVE', 0, 0, 0)")
        sc.execute("INSERT OR REPLACE INTO supertrend_tokens "
                   "(token, status, added_at, live_mode, paused) "
                   "VALUES ('MGY', 'ACTIVE', 0, 0, 1)")   # 暂停
    _mk_rows("MGX", 1.0, 2.0)
    _mk_rows("MGY", 1.0, 3.0)
    MOCK_SIG.pop("MGX", None)      # OKX 取数失败
    MOCK_PRICE.pop("MGX", None)    # 标记价也失败
    st._run_round()
    rows = _active_rows("MGX")
    check("OKX/标记价失败仍合并为 1 笔", len(rows) == 1, str(len(rows)))
    check("合并后数量 = 1+2 = 3",
          rows and abs(rows[0]["quantity"] - 3.0) < 1e-9,
          str(rows[0]["quantity"] if rows else None))
    rows = _active_rows("MGY")
    check("暂停币也合并为 1 笔", len(rows) == 1, str(len(rows)))
    check("暂停币合并后数量 = 1+3 = 4",
          rows and abs(rows[0]["quantity"] - 4.0) < 1e-9,
          str(rows[0]["quantity"] if rows else None))

    print("== 31. 合并竞态：1s 循环旧快照不得复活 CANCELED 行（防数量/盈亏双计） ==")
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
        cur = mc.execute(
            "INSERT INTO trade_positions (token, symbol, side, status, mode, "
            "margin_amount, leverage, notional, quantity, entry_price, current_price, "
            "stop_loss_price, strategy) VALUES ('RACE','RACEUSDT','LONG','OPEN',"
            "'paper',50,3,150,1.0,100,100,95,'supertrend')")
        rid = cur.lastrowid
    MOCK_PRICE["RACE"] = 90.0            # 跌破 SL 95
    with storage.get_conn() as mc:
        stale = storage.trade_open_positions(mc)   # 1s 循环已取的旧快照
    with storage.get_conn() as mc:                 # 合并线程把该行标 CANCELED
        mc.execute("UPDATE trade_positions SET status='CANCELED' WHERE id=?", (rid,))
    old_fn = trade_logic.storage.trade_open_positions
    trade_logic.storage.trade_open_positions = lambda conn: stale
    try:
        with storage.get_conn() as mc:
            trade_logic.update_paper_positions(mc)
    finally:
        trade_logic.storage.trade_open_positions = old_fn
    with storage.get_conn() as mc:
        row = mc.execute("SELECT status, realized_pnl FROM trade_positions "
                         "WHERE id=?", (rid,)).fetchone()
    check("CANCELED 行未被复活为 CLOSED", row["status"] == "CANCELED",
          str(dict(row)))
    check("未产生幽灵 realized", not (row["realized_pnl"] or 0), str(dict(row)))

    print("== 32. 反转市价容忍（flip_market_tolerance_pct）==")

    def _reset_pos():
        with storage.get_conn() as mc:
            mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
        with storage.get_conn(config.SUPERTREND_DB) as sc:
            sc.execute("DELETE FROM supertrend_status")

    def _setup_flip(direction, tol, flip=1):
        """设为指定方向/容忍 → 在受限方向跑一轮记基准（WAIT_DIR）。
        受限方向：只做多=DOWN（反转前画上轨 101000）；只做空=UP（画下轨 99000）"""
        _reset_pos()
        with storage.get_conn(config.SUPERTREND_DB) as sc:
            sc.execute("UPDATE supertrend_tokens SET direction=?, flip_market_open=?, "
                       "flip_market_tolerance_pct=? WHERE token='BTC'",
                       (direction, flip, tol))
        set_sig("BTC", "DOWN" if direction == "LONG_ONLY" else "UP", 101000, 99000)
        MOCK_PRICE["BTC"] = 100000
        new_candle("BTC")
        new_candle("BTC")
        with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
            st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, direction, True, tol)

    def _flip(direction, trend, price, tol):
        """翻到允许方向跑一轮（走真实入口，带容忍参数）"""
        set_sig("BTC", trend, 101000, 99000)
        MOCK_PRICE["BTC"] = price
        new_candle("BTC")
        new_candle("BTC")
        new_candle("BTC")
        with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
            st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, direction, True, tol)

    # 32a：只做多 + 容忍1%，参考线=反转前上轨 101000 → 阈值 101000*1.01=102010
    _setup_flip("LONG_ONLY", 1.0)
    _flip("LONG_ONLY", "UP", 100500, 1.0)     # 100500 ≤ 102010 → 容忍内
    p = pos_of("BTC")
    pend = pending_of("BTC")
    check("32a 容忍内：市价开多", p and p["status"] == "OPEN" and p["side"] == "LONG",
          str(p and (p["status"], p["side"])))
    check("32a 容忍内：同轮挂单并存", pend and pend["status"] == "PENDING",
          str(pend and pend["status"]))

    # 32b：同币，价格跑到 102500 > 102010 → 超容忍：不打市价、只挂单
    _setup_flip("LONG_ONLY", 1.0)
    _flip("LONG_ONLY", "UP", 102500, 1.0)
    p = pos_of("BTC")
    pend = pending_of("BTC")
    check("32b 超容忍：无市价仓", p is None, str(p and p["status"]))
    check("32b 超容忍：只挂单", pend and pend["status"] == "PENDING",
          str(pend and pend["status"]))
    check("32b 超容忍：挂单价仍=99000*1.01=99990",
          pend and abs(pend["limit_price"] - 99990) < 1e-6,
          str(pend and pend["limit_price"]))
    check("32b 超容忍：原因写进挂单 open_reason（页面据此留痕）",
          pend and "反转市价跳过" in (pend["open_reason"] or ""),
          str(pend and pend["open_reason"]))
    check("32b 超容忍：open_reason 仍以原有文案开头（不破坏展示）",
          pend and (pend["open_reason"] or "").startswith("超级趋势 15m"),
          str(pend and pend["open_reason"]))
    check("32b 超容忍：advice 不含原因（运行期会被 1s 循环重写，不能放那）",
          pend and "反转市价跳过" not in (pend["advice"] or ""),
          str(pend and pend["advice"]))

    # 32c：容忍 0 → 不设限，价格再远也直接市价进（向后兼容）
    _setup_flip("LONG_ONLY", 0.0)
    _flip("LONG_ONLY", "UP", 105000, 0.0)
    p = pos_of("BTC")
    check("32c 容忍0：直接市价进", p and p["status"] == "OPEN",
          str(p and p["status"]))
    check("32c 容忍0：成交价=现价 105000",
          p and abs(p["entry_price"] - 105000) < 1e-6, str(p and p["entry_price"]))

    # 32d：旧轨道脏数据（NULL）→ 不设限，不拿脏数据拦真交易
    _setup_flip("LONG_ONLY", 1.0)
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_status SET upper_band=NULL WHERE token='BTC'")
    _flip("LONG_ONLY", "UP", 105000, 1.0)
    p = pos_of("BTC")
    check("32d 旧轨道 NULL：按不设限直接市价进", p and p["status"] == "OPEN",
          str(p and p["status"]))

    # 32e：翻空对称 —— 只做空，参考线=反转前下轨 99000 → 阈值 99000*0.99=98010
    _setup_flip("SHORT_ONLY", 1.0)
    _flip("SHORT_ONLY", "DOWN", 97500, 1.0)   # 97500 < 98010 → 超容忍
    p = pos_of("BTC")
    pend = pending_of("BTC")
    check("32e 翻空超容忍：无市价仓 + 只挂空单",
          p is None and pend and pend["side"] == "SHORT",
          str((p and p["status"], pend and pend["side"])))
    check("32e 翻空超容忍：open_reason 留痕",
          pend and "反转市价跳过" in (pend["open_reason"] or ""),
          str(pend and pend["open_reason"]))

    # 32f：翻空容忍内
    _setup_flip("SHORT_ONLY", 1.0)
    _flip("SHORT_ONLY", "DOWN", 98500, 1.0)   # 98500 ≥ 98010 → 容忍内
    p = pos_of("BTC")
    check("32f 翻空容忍内：市价开空",
          p and p["status"] == "OPEN" and p["side"] == "SHORT",
          str(p and (p["status"], p["side"])))

    _reset_pos()
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_tokens SET direction='BOTH', flip_market_open=0, "
                   "flip_market_tolerance_pct=NULL WHERE token='BTC'")

    print("== 33. 参考线不可用（状态表停更 > 1 根K线）→ 只挂单，不 fail-open ==")
    # 场景：暂停恢复 / OKX 连续取数失败 / 重启 / 切实盘后，supertrend_status 停更很久，
    # 旧轨道是几小时前的另一条线——拿它当"反转前趋势线"等于随机门闸。
    # 这些场景恰恰价格已经跑远 → 必须拦，不能放行市价追高。
    _setup_flip("LONG_ONLY", 1.0)
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_status SET last_candle_ts = last_candle_ts - ?",
                   (5 * CANDLE,))          # 人为把基准推远 5 根K线
    _flip("LONG_ONLY", "UP", 100500, 1.0)  # 价格其实在容忍内（100500 ≤ 102010）
    p = pos_of("BTC")
    pend = pending_of("BTC")
    check("参考线不可用：不打市价（即使现价在容忍范围内）", p is None,
          str(p and p["status"]))
    check("参考线不可用：只挂限价单", pend and pend["status"] == "PENDING",
          str(pend and pend["status"]))
    check("参考线不可用：留痕说明原因",
          pend and "参考线不可用" in (pend["open_reason"] or ""),
          str(pend and pend["open_reason"]))

    print("== 34. 合并竞态：SELECT 之后被平掉的 extras 不得并进 keep（A3）==")

    class _RaceConn:
        """在 _merge_positions **重读某条 extras 的状态那一刻**抢先把它平掉
        （模拟 1s 循环在 SELECT 与累加之间扫损）。必须卡在重读那一句上——在
        外层 SELECT 之前就平掉的话，那条压根不会进 rows，竞态根本没被模拟到。"""

        def __init__(self, conn, victim_id):
            self._c = conn
            self._victim = victim_id
            self.fired = False

        def execute(self, sql, params=()):
            if (not self.fired
                    and "WHERE id=? AND" in sql and "status IN ('OPEN','PARTIAL')" in sql
                    and params and params[0] == self._victim):
                self.fired = True
                with storage.get_conn() as c2:
                    c2.execute("UPDATE trade_positions SET status='CLOSED', "
                               "advice='SL 扫损', closed_at=CURRENT_TIMESTAMP "
                               "WHERE id=?", (self._victim,))
            return self._c.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._c, name)

    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
        ids = []
        for qty in (1.0, 2.0, 3.0):     # 同向三笔，最早的是 keep
            cur = mc.execute(
                "INSERT INTO trade_positions (token, symbol, side, status, mode, "
                "margin_amount, leverage, notional, quantity, entry_price, "
                "current_price, stop_loss_price, strategy) "
                "VALUES ('MRG','MRGUSDT','LONG','OPEN','paper',?,3,?,?,100,100,95,"
                "'supertrend')", (qty * 100 / 3, qty * 100, qty))
            ids.append(cur.lastrowid)
    race = None
    with storage.get_conn() as mc:
        race = _RaceConn(mc, ids[1])
        st._merge_positions(race, "MRG")
    check("竞态确实被模拟到（重读那一刻把中间那笔平掉了）", race and race.fired)
    with storage.get_conn() as mc:
        keep = mc.execute("SELECT quantity, notional FROM trade_positions "
                          "WHERE id=?", (ids[0],)).fetchone()
        can = mc.execute("SELECT status FROM trade_positions WHERE id=?",
                         (ids[2],)).fetchone()
    check("合并只并进还活着的那笔：数量 = 1.0 + 3.0 = 4.0（不含被平掉的 2.0）",
          abs(float(keep["quantity"]) - 4.0) < 1e-9,
          f"keep 数量 {keep['quantity']}")
    check("名义也不含被平掉那笔", abs(float(keep["notional"]) - 400.0) < 1e-6,
          f"notional {keep['notional']}")
    check("还活着的那笔已被并入并标 CANCELED", can and can["status"] == "CANCELED",
          str(can))
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE token='MRG'")

    print("== 35. 总开关关闭时：已移除币仍要被清理（A9）==")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_tokens SET status='REMOVED' WHERE token='ETH'")
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    with storage.get_conn() as mc:
        mc.execute(
            "INSERT INTO trade_positions (token, symbol, side, status, mode, "
            "margin_amount, leverage, notional, quantity, entry_price, "
            "current_price, stop_loss_price, strategy) "
            "VALUES ('ETH','ETHUSDT','LONG','OPEN','paper',50,3,150,1.0,3000,3000,"
            "2900,'supertrend')")
    MOCK_PRICE["ETH"] = 3000.0
    with storage.get_conn() as mc:
        storage.trading_settings_update(mc, {"supertrend_enabled": False})
    st._run_round()                      # 总开关关闭
    with storage.get_conn() as mc:
        st_rows = [dict(r) for r in mc.execute(
            "SELECT status FROM trade_positions WHERE token='ETH' "
            "AND strategy='supertrend'")]
    check("总开关关闭时，已移除币的残留仓仍被清理（不是永远没人管）",
          st_rows and st_rows[0]["status"] == "CLOSED", str(st_rows))
    with storage.get_conn() as mc:
        storage.trading_settings_update(mc, {"supertrend_enabled": True})
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_tokens SET status='ACTIVE' WHERE token='ETH'")

    print("== 36. flip_tol_pct 漏传必须可见（A8）==")
    _reset_pos()
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_tokens SET direction='LONG_ONLY', "
                   "flip_market_open=1, flip_market_tolerance_pct=1.0 "
                   "WHERE token='BTC'")
    import io as _io
    import contextlib as _ctx
    _buf = _io.StringIO()
    with _ctx.redirect_stdout(_buf):
        # 故意漏传 flip_tol_pct（模拟"新调用点忘了传"）
        with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
            st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "LONG_ONLY", True)
    check("漏传 flip_tol_pct → 打 [WARN] 告警（不静默）",
          "flip_tol_pct 未传" in _buf.getvalue(), _buf.getvalue()[-200:])
    # 显式传 0 是合法值，不该告警
    _buf2 = _io.StringIO()
    with _ctx.redirect_stdout(_buf2):
        with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
            st._process_token(mc, sc, "BTC", 1.0, 1.0, 50.0, 3.0, "LONG_ONLY",
                              True, 0.0)
    check("显式传 0（不设限）不告警（区分「漏传」与「显式不设限」）",
          "flip_tol_pct 未传" not in _buf2.getvalue(), _buf2.getvalue()[-200:])
    _reset_pos()
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_tokens SET direction='BOTH', flip_market_open=0, "
                   "flip_market_tolerance_pct=NULL WHERE token='BTC'")

    print("== 37. 反转市价平仓（close_on_reverse）==")
    SLIP = config.TRADING_STOP_SLIPPAGE_PCT

    def cor_proc(cor):
        with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
            st._process_token(mc, sc, "COR", 1.0, 1.0, 50.0, 3.0,
                              "BOTH", False, 0.0, cor)

    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("DELETE FROM supertrend_status WHERE token='COR'")
        sc.execute("INSERT OR REPLACE INTO supertrend_tokens "
                   "(token, status, added_at, direction, close_on_reverse) "
                   "VALUES ('COR','ACTIVE',0,'BOTH',0)")
    # 建仓：UP 趋势，价格 99500 ≤ 挂单价 99990 → 价格到位直接市价开多
    set_sig("COR", "UP", 101000, 99000)
    MOCK_PRICE["COR"] = 99500
    cor_proc(False)                 # 首轮记基准
    new_candle("COR")
    cor_proc(False)
    p = pos_of("COR")
    check("37 建仓：市价开多 @99500 SL=98010",
          p and p["side"] == "LONG" and abs(p["entry_price"] - 99500) < 1e-6
          and abs(p["stop_loss_price"] - 98010) < 1e-6,
          str(p and (p["status"], p["side"], p["entry_price"])))
    # 顺向：趋势仍 UP，开启设置也不平（防判反）
    cor_proc(True)
    check("37 顺向（多单+UP）开启设置不平", pos_of("COR") is not None)
    # 关 = 原逻辑：翻 DOWN → 冻结 SL、不主动平仓
    set_sig("COR", "DOWN", 101000, 99000)
    new_candle("COR")
    new_candle("COR")
    MOCK_PRICE["COR"] = 100800
    cor_proc(False)
    p = pos_of("COR")
    check("37 设置关：翻转不主动平仓（原逻辑）", p and p["status"] == "OPEN",
          str(p and p["status"]))
    check("37 设置关：SL 冻结不动（98010）",
          p and abs(p["stop_loss_price"] - 98010) < 1e-6, str(p and p["stop_loss_price"]))
    # 开 + 存量逆势：同一根K线内开启（无 new_candle）→ 也平（状态型、每轮检查）
    cor_proc(True)
    with storage.get_conn() as mc:
        row = dict(mc.execute(
            "SELECT * FROM trade_positions WHERE token='COR' AND strategy='supertrend' "
            "ORDER BY id DESC LIMIT 1").fetchone())
    fill = 100800 * (1 - SLIP / 100)
    check("37 存量补平：同K线内开启即平（不带新K线门槛）", row["status"] == "CLOSED",
          str(row["status"]))
    check("37 平仓价含 0.15% 假设滑点",
          abs(float(row["current_price"]) - fill) < 1e-6,
          f'{row["current_price"]} vs {fill}')
    check("37 advice 记趋势反转+滑点",
          "趋势反转" in (row["advice"] or "") and "含假设滑点" in (row["advice"] or ""),
          str(row["advice"]))
    qty = 50 / 99500
    check("37 realized 按滑点价结算",
          abs(float(row["realized_pnl"]) - (fill - 99500) * qty) < 1e-6,
          f'{row["realized_pnl"]} vs {(fill - 99500) * qty}')
    # 逆势挂单一并撤：DOWN 下挂空单 → 翻 UP → 设置开 → 挂单 CANCELED
    new_candle("COR")
    MOCK_PRICE["COR"] = 99500
    cor_proc(False)                 # 趋势仍 DOWN：挂空单 @ 101000*0.99=99990
    pend = pending_of("COR")
    check("37 建空单挂单（前置）", pend and pend["side"] == "SHORT",
          str(pend and pend["side"]))
    set_sig("COR", "UP", 101000, 99000)
    new_candle("COR")
    cor_proc(True)
    with storage.get_conn() as mc:
        row2 = dict(mc.execute(
            "SELECT * FROM trade_positions WHERE token='COR' AND strategy='supertrend' "
            "ORDER BY id DESC LIMIT 1").fetchone())
    check("37 逆势挂单被一并撤销",
          pending_of("COR") is None and row2["status"] == "CANCELED"
          and "趋势反转" in (row2["advice"] or ""),
          str(row2))
    # 取价失败轮不动作（冻结 SL 兜底）；同一根K线内下一轮重试成功
    new_candle("COR")
    MOCK_PRICE["COR"] = 100500
    cor_proc(False)                 # UP 下挂多单 @ 99990（现价高于挂单价）
    pend = pending_of("COR")
    check("37 建多单挂单（前置）", pend and pend["side"] == "LONG",
          str(pend and pend["side"]))
    MOCK_PRICE["COR"] = 99980
    with storage.get_conn() as mc:
        trade_logic.update_paper_positions(mc)   # 挂单成交 OPEN @99990
    check("37 挂单成交 OPEN", pos_of("COR") is not None)
    set_sig("COR", "DOWN", 101000, 99000)
    new_candle("COR")
    MOCK_PRICE.pop("COR")           # 取不到标记价 → 整轮早退不动作
    cor_proc(True)
    check("37 取价失败轮：不动作、不炸（冻结 SL 兜底）", pos_of("COR") is not None)
    MOCK_PRICE["COR"] = 100700      # 价格恢复 → 同一根K线内下一轮重试成功
    cor_proc(True)
    check("37 取价恢复：同K线内自动重试平仓", pos_of("COR") is None)
    # 方向切换遗留逆势仓 + flip_market_open：翻转轮同轮"先平 → 反手市价开 + 挂单"
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("DELETE FROM supertrend_status WHERE token='COR'")
        sc.execute("UPDATE supertrend_tokens SET direction='SHORT_ONLY', "
                   "flip_market_open=1 WHERE token='COR'")
    with storage.get_conn() as mc:   # 手工造"切方向遗留"的多单（引擎自己开不出来）
        mc.execute(
            "INSERT INTO trade_positions (token, symbol, side, status, mode, "
            "margin_amount, leverage, notional, quantity, entry_price, "
            "current_price, stop_loss_price, strategy) "
            "VALUES ('COR','CORUSDT','LONG','OPEN','paper',50,3,150,0.0015,"
            "100000,100000,98010,'supertrend')")
    def flip_proc():
        # direction/flip 由调用方传入（_run_round 读库），cor_proc 写死了 BOTH → 这里直调
        with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
            st._process_token(mc, sc, "COR", 1.0, 1.0, 50.0, 3.0,
                              "SHORT_ONLY", True, 0.0, True)

    set_sig("COR", "UP", 101000, 99000)    # SHORT_ONLY 受限方向：记基准（旧趋势 UP）
    MOCK_PRICE["COR"] = 100000
    flip_proc()
    set_sig("COR", "DOWN", 101000, 99000)  # 翻到允许方向（was_blocked）
    new_candle("COR")
    MOCK_PRICE["COR"] = 99500
    flip_proc()
    sh = pos_of("COR")
    check("37 翻转轮同轮反手：市价开空",
          sh and sh["status"] == "OPEN" and sh["side"] == "SHORT",
          str(sh and (sh["status"], sh["side"])))
    check("37 翻转轮同轮反手：成交价=现价 99500",
          sh and abs(sh["entry_price"] - 99500) < 1e-6, str(sh and sh["entry_price"]))
    check("37 翻转轮同轮反手：空单 SL=上轨*1.01=102010",
          sh and abs(sh["stop_loss_price"] - 102010) < 1e-6,
          str(sh and sh["stop_loss_price"]))
    pend = pending_of("COR")
    check("37 翻转轮同轮反手：同轮挂单并存", pend and pend["side"] == "SHORT",
          str(pend and pend["side"]))
    with storage.get_conn() as mc:
        lg = dict(mc.execute(
            "SELECT * FROM trade_positions WHERE token='COR' AND strategy='supertrend' "
            "AND side='LONG' ORDER BY id DESC LIMIT 1").fetchone())
    check("37 翻转轮同轮反手：遗留多单已被平（趋势反转）",
          lg and lg["status"] == "CLOSED" and "趋势反转" in (lg["advice"] or ""), str(lg))
    # 清理
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE token='COR'")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("DELETE FROM supertrend_status WHERE token='COR'")
        sc.execute("DELETE FROM supertrend_tokens WHERE token='COR'")

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
