"""超级趋势策略本地逻辑测试（mock OKX + 标记价，不连外网）。

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

# ---- mock：token -> 已闭合K线信号 / 标记价 ----
MOCK_SIG: dict = {}
MOCK_PRICE: dict = {}

REAL_FETCH = st._fetch_okx_supertrend
st._fetch_okx_supertrend = lambda token: MOCK_SIG.get(token)
st.get_mark_price = lambda token: MOCK_PRICE.get(token)
trade_logic.get_mark_price = lambda token: MOCK_PRICE.get(token)

CANDLE = 15 * 60 * 1000


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


def open_pos(main_conn, token):
    row = main_conn.execute(
        "SELECT * FROM trade_positions WHERE token=? AND strategy='supertrend' "
        "AND status IN ('OPEN','PARTIAL') ORDER BY id DESC LIMIT 1", (token,)
    ).fetchone()
    return dict(row) if row else None


def pos_count(main_conn, token):
    return main_conn.execute(
        "SELECT COUNT(*) FROM trade_positions WHERE token=? AND strategy='supertrend'",
        (token,)).fetchone()[0]


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
    print("== 1. 初始化 + 默认币种种入 ==")
    st.init_db()
    with storage.get_conn(config.SUPERTREND_DB) as c:
        tokens = {r["token"] for r in c.execute("SELECT token FROM supertrend_tokens")}
    check("BTC/ETH 已种入", tokens == {"BTC", "ETH"}, str(tokens))
    # 幂等：重复 init 不重复/不复活
    with storage.get_conn(config.SUPERTREND_DB) as c:
        c.execute("UPDATE supertrend_tokens SET status='REMOVED' WHERE token='ETH'")
    st.init_db()
    with storage.get_conn(config.SUPERTREND_DB) as c:
        eth = c.execute("SELECT status FROM supertrend_tokens WHERE token='ETH'").fetchone()
    check("移除过的币不复活", eth["status"] == "REMOVED")

    print("== 2. 开仓：趋势 UP 开多，SL=下轨道x(1-1%) ==")
    set_sig("BTC", "UP", 101000, 99000,
            forming={"trend": "UP", "upper_band": 101300, "lower_band": 99300})
    MOCK_PRICE["BTC"] = 100000
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 50.0, 3.0)
    with storage.get_conn() as mc:
        p = open_pos(mc, "BTC")
    check("已开多", p and p["side"] == "LONG" and p["status"] == "OPEN")
    check("SL=99000*0.99=98010", p and abs(p["stop_loss_price"] - 98010) < 1e-6,
          str(p and p["stop_loss_price"]))
    check("SL 用已闭合轨道（不受 forming 99300 影响）",
          p and abs(p["stop_loss_price"] - 99300 * 0.99) > 100, str(p and p["stop_loss_price"]))
    check("margin=50/3", p and abs(p["margin_amount"] - 50 / 3) < 1e-6)
    check("quantity=50/100000", p and abs(p["quantity"] - 0.0005) < 1e-9)
    check("mode=paper 写死", p and p["mode"] == "paper")
    check("tp1/tp2 NULL（无止盈）", p and p["tp1_price"] is None and p["tp2_price"] is None)
    check("symbol=BTCUSDT", p and p["symbol"] == "BTCUSDT")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        row = sc.execute(
            "SELECT forming_trend, forming_upper_band, forming_lower_band "
            "FROM supertrend_status WHERE token='BTC'").fetchone()
    check("status 表写入实时轨道", row and row["forming_trend"] == "UP"
          and abs(row["forming_upper_band"] - 101300) < 1e-6
          and abs(row["forming_lower_band"] - 99300) < 1e-6,
          str(dict(row) if row else None))

    print("== 3. 单币单仓：重复处理不开新仓 ==")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 50.0, 3.0)
    with storage.get_conn() as mc:
        check("仍只有 1 笔", pos_count(mc, "BTC") == 1)

    print("== 4. SL 跟随：只朝有利方向 ==")
    set_sig("BTC", "UP", 100500, 99500)  # 新闭合K线，下轨道上移
    new_candle("BTC")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 50.0, 3.0)
    with storage.get_conn() as mc:
        p = open_pos(mc, "BTC")
    check("SL 上移至 99500*0.99=98505", p and abs(p["stop_loss_price"] - 98505) < 1e-6,
          str(p and p["stop_loss_price"]))
    check("advice 同步新 SL（不显示旧数）", p and "98505" in (p["advice"] or ""),
          str(p and p["advice"]))
    MOCK_SIG["BTC"]["upper_band"] = 99000  # 轨道下移 → SL 不放松
    MOCK_SIG["BTC"]["lower_band"] = 98000
    new_candle("BTC")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 50.0, 3.0)
    with storage.get_conn() as mc:
        p = open_pos(mc, "BTC")
    check("轨道下移 SL 不放松（保持 98505）",
          p and abs(p["stop_loss_price"] - 98505) < 1e-6, str(p and p["stop_loss_price"]))

    print("== 5. 系统 2s 链路扫损平仓 + 跳过 journal/归档 ==")
    MOCK_PRICE["BTC"] = 98400  # ≤ SL 98505 → 触发
    with storage.get_conn() as mc:
        trade_logic.update_paper_positions(mc)
        p = mc.execute(
            "SELECT * FROM trade_positions WHERE strategy='supertrend' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        p = dict(p)
        jn = mc.execute("SELECT COUNT(*) FROM journal").fetchone()[0]
        la = mc.execute("SELECT COUNT(*) FROM trade_loss_archive").fetchone()[0]
    check("已平仓", p["status"] == "CLOSED")
    check("成交价=SLx滑点(0.15%)", abs(p["current_price"] - 98505 * (1 - 0.15 / 100)) < 1e-6
          or abs(p["current_price"] - 98400) < 1e-6, str(p["current_price"]))
    check("journal 无污染", jn == 0, f"journal={jn}")
    check("亏损归档无污染", la == 0, f"archive={la}")

    print("== 6. 插针扫损→同向重开 ==")
    new_candle("BTC")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 50.0, 3.0)
    with storage.get_conn() as mc:
        p = open_pos(mc, "BTC")
    check("插针后重开多", p and p["side"] == "LONG" and p["status"] == "OPEN")

    print("== 7. 反转扫损→开反向仓 ==")
    MOCK_PRICE["BTC"] = 95000  # 打掉多仓 SL
    with storage.get_conn() as mc:
        trade_logic.update_paper_positions(mc)
    set_sig("BTC", "DOWN", 101000, 99000)  # 趋势反转 DOWN
    new_candle("BTC")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 50.0, 3.0)
    with storage.get_conn() as mc:
        p = open_pos(mc, "BTC")
    check("反转后开空", p and p["side"] == "SHORT" and p["status"] == "OPEN")
    check("空单 SL=上轨道x1.01=102010",
          p and abs(p["stop_loss_price"] - 101000 * 1.01) < 1e-6, str(p and p["stop_loss_price"]))

    print("== 8. 空单 SL 只下移 ==")
    MOCK_SIG["BTC"]["upper_band"] = 100500  # 上轨道下移 → 空单 SL 下移
    MOCK_SIG["BTC"]["lower_band"] = 99000
    new_candle("BTC")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 50.0, 3.0)
    with storage.get_conn() as mc:
        p = open_pos(mc, "BTC")
    check("空单 SL 下移至 100500*1.01=101505",
          p and abs(p["stop_loss_price"] - 100500 * 1.01) < 1e-6, str(p and p["stop_loss_price"]))

    print("== 8b. 空单持有中趋势翻 UP：新上轨道低于 SL → SL 下移收紧 ==")
    MOCK_SIG["BTC"]["trend"] = "UP"          # 趋势翻转
    MOCK_SIG["BTC"]["upper_band"] = 100000   # 新 UP 信号的上轨道低于当前 SL
    MOCK_SIG["BTC"]["lower_band"] = 99000
    new_candle("BTC")
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 50.0, 3.0)
    with storage.get_conn() as mc:
        p = open_pos(mc, "BTC")
    check("翻 UP 后空单 SL 收紧至 100000*1.01=101000",
          p and p["side"] == "SHORT"
          and abs(p["stop_loss_price"] - 101000) < 1e-6,
          str(p and (p["side"], p["stop_loss_price"])))
    check("持仓方向不变（仍是空单，不因趋势翻转而换仓）", p and p["side"] == "SHORT")

    print("== 9. 移除监控：幂等平仓 ==")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_tokens SET status='REMOVED' WHERE token='BTC'")
    ok1 = st.close_supertrend_position("BTC", "REMOVED")
    with storage.get_conn() as mc:
        p = mc.execute(
            "SELECT * FROM trade_positions WHERE token='BTC' AND strategy='supertrend' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        p = dict(p)
    check("移除平仓成功", ok1 and p["status"] == "CLOSED")
    check("advice=移除监控", "移除监控" in (p["advice"] or ""), str(p["advice"]))
    ok2 = st.close_supertrend_position("BTC", "REMOVED")
    check("第二次平仓幂等跳过", ok2 is False)

    print("== 10. 设置热生效：容忍% 变化影响新 SL ==")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_tokens SET status='ACTIVE' WHERE token='BTC'")
    set_sig("BTC", "UP", 101000, 99000)
    MOCK_PRICE["BTC"] = 100000
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 2.0, 50.0, 3.0)  # 容忍改 2%
    with storage.get_conn() as mc:
        p = open_pos(mc, "BTC")
    check("容忍2% → SL=99000*0.98=97020",
          p and abs(p["stop_loss_price"] - 97020) < 1e-6, str(p and p["stop_loss_price"]))

    print("== 11. 陈旧数据不开仓 ==")
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    set_sig("BTC", "UP", 101000, 99000, offset_candles=-3)  # 45分钟前
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "BTC", 1.0, 50.0, 3.0)
    with storage.get_conn() as mc:
        check("陈旧信号不开仓", open_pos(mc, "BTC") is None)
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        row = sc.execute(
            "SELECT fetch_error FROM supertrend_status WHERE token='BTC'").fetchone()
        check("status 表记 stale_data", row and row["fetch_error"] == "stale_data",
              str(dict(row) if row else None))

    print("== 12. 线程单轮流程（_run_round 端到端）==")
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_tokens SET status='ACTIVE' WHERE token='BTC'")
        sc.execute("UPDATE supertrend_tokens SET status='REMOVED' WHERE token='ETH'")
    # 12a. 开关关闭 → 空转不开仓
    with storage.get_conn() as mc:
        storage.trading_settings_update(mc, {"supertrend_enabled": False})
    set_sig("BTC", "UP", 101000, 99000)
    MOCK_PRICE["BTC"] = 100000
    st._run_round()
    with storage.get_conn() as mc:
        check("开关关闭时空转不开仓", open_pos(mc, "BTC") is None)
    # 12b. 开关打开 + 容忍%热生效 → 自动开仓用新值
    with storage.get_conn() as mc:
        storage.trading_settings_update(mc, {"supertrend_enabled": True,
                                             "supertrend_sl_tolerance_pct": 2.0})
    st._run_round()
    with storage.get_conn() as mc:
        p = open_pos(mc, "BTC")
    check("开关打开后自动开仓", p and p["status"] == "OPEN")
    check("trading_settings 容忍2% 热生效",
          p and abs(p["stop_loss_price"] - 99000 * 0.98) < 1e-6,
          str(p and p["stop_loss_price"]))
    # 12c. REMOVED 币种的持仓被线程安全网清理
    set_sig("ETH", "UP", 4000, 3900)
    MOCK_PRICE["ETH"] = 3950
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._open_position(mc, "ETH", MOCK_SIG["ETH"], 3950, 1.0, 50.0, 3.0)
    st._run_round()
    with storage.get_conn() as mc:
        eth_pos = mc.execute(
            "SELECT status FROM trade_positions WHERE token='ETH' AND strategy='supertrend' "
            "ORDER BY id DESC LIMIT 1").fetchone()
    check("REMOVED 币种持仓被安全网清理",
          eth_pos and eth_pos["status"] == "CLOSED", str(dict(eth_pos) if eth_pos else None))

    print("== 13. OKX 取数逻辑（真实 _fetch + mock requests）==")
    import time as _t
    st._fetch_okx_supertrend = REAL_FETCH

    class FakeResp:
        def __init__(self, data, status=200):
            self._data = data
            self.status_code = status

        def json(self):
            return self._data

    def okx_body(*pairs):
        # 实测真实响应结构（2026-09-05 服务器验证）：深层嵌套
        rows = [
            {"ts": ts, "values": {"superTrend": str(up), "direction": "1",
                                  "trend": tr, "upperBand": str(up), "lowerBand": str(lo)}}
            for ts, (tr, up, lo) in pairs]
        return {"code": "0", "data": [{"data": [{
            "instId": "BTC-USDT-SWAP",
            "timeframes": {"15m": {"indicators": {"supertrend": rows}}},
        }], "mode": "live", "summary": {}, "timestamp": now_ms}], "msg": ""}

    now_ms = int(_t.time() * 1000)
    forming = (now_ms // CANDLE) * CANDLE
    closed = forming - CANDLE
    # 13a. 正常：返回最新闭合 + 正在形成 → 主路径确定性命中闭合那根
    st.requests.post = lambda url, json=None, timeout=None: FakeResp(okx_body(
        (closed, ("UP", 101000, 99000)), (forming, ("UP", 101200, 99200))))
    sig = st._fetch_okx_supertrend("BTC")
    check("主路径命中最新闭合K线", sig and sig["ts"] == closed, str(sig))
    check("取闭合值而非形成中", sig and abs(sig["upper_band"] - 101000) < 1e-6,
          str(sig and sig["upper_band"]))
    check("forming 参考数据带回",
          sig and sig.get("forming") and sig["forming"]["trend"] == "UP"
          and abs(sig["forming"]["upper_band"] - 101200) < 1e-6,
          str(sig and sig.get("forming")))
    # 13b. 延迟：返回两根都旧（漏了最新闭合）→ 兜底年龄校验拦截 → None
    old1 = closed - CANDLE
    old2 = closed - 2 * CANDLE
    st.requests.post = lambda url, json=None, timeout=None: FakeResp(okx_body(
        (old2, ("DOWN", 95000, 93000)), (old1, ("DOWN", 96000, 94000))))
    sig = st._fetch_okx_supertrend("BTC")
    check("延迟两根都旧 → 返回 None（不开仓）", sig is None, str(sig))
    # 13c. 只有正在形成的K线 → None
    st.requests.post = lambda url, json=None, timeout=None: FakeResp(okx_body(
        (forming, ("UP", 1, 2))))
    sig = st._fetch_okx_supertrend("BTC")
    check("只有形成中 → None", sig is None, str(sig))
    # 13d. 空数据 → None
    st.requests.post = lambda url, json=None, timeout=None: FakeResp(
        {"data": {"supertrend": []}})
    sig = st._fetch_okx_supertrend("BTC")
    check("空数据 → None", sig is None, str(sig))
    # 恢复 mock
    st._fetch_okx_supertrend = lambda token: MOCK_SIG.get(token)

    print("== 14. 新币首次处理失败 → fetch_error 可见（upsert）==")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("DELETE FROM supertrend_status WHERE token='SOL'")
        sc.execute("INSERT OR IGNORE INTO supertrend_tokens (token, status, added_at) "
                   "VALUES ('SOL', 'ACTIVE', 0)")
    # 14a. 首次拉取即失败（无信号）
    MOCK_SIG.pop("SOL", None)
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "SOL", 1.0, 50.0, 3.0)
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        row = sc.execute("SELECT fetch_error FROM supertrend_status WHERE token='SOL'").fetchone()
    check("新币首次拉取失败 → 有错误行", row and row["fetch_error"] == "okx_fetch_failed",
          str(dict(row) if row else None))
    # 14b. 首次无标记价
    set_sig("SOL", "UP", 200, 190)
    MOCK_PRICE.pop("SOL", None)
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "SOL", 1.0, 50.0, 3.0)
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        row = sc.execute("SELECT fetch_error FROM supertrend_status WHERE token='SOL'").fetchone()
    check("新币首次无标记价 → mark_price_failed",
          row and row["fetch_error"] == "mark_price_failed", str(dict(row) if row else None))
    # 14c. 恢复后错误清除
    MOCK_PRICE["SOL"] = 195
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        st._process_token(mc, sc, "SOL", 1.0, 50.0, 3.0)
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        row = sc.execute("SELECT fetch_error, state FROM supertrend_status WHERE token='SOL'").fetchone()
    check("恢复后错误清除 + 开仓 HOLDING",
          row and row["fetch_error"] is None and row["state"] == "HOLDING",
          str(dict(row) if row else None))

    print("== 15. 防御分支与失败场景 ==")
    # 15a. 平仓时无标记价 → 返回 False 且仓位保留（下轮重试）
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    set_sig("BTC", "UP", 101000, 99000)
    MOCK_PRICE["BTC"] = 100000
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_tokens SET status='ACTIVE' WHERE token='BTC'")
        st._process_token(mc, sc, "BTC", 1.0, 50.0, 3.0)
    MOCK_PRICE["BTC"] = None  # 币安标记价接口失败
    ok = st.close_supertrend_position("BTC", "REMOVED")
    with storage.get_conn() as mc:
        p = open_pos(mc, "BTC")
    check("无标记价平仓失败且仓位保留", ok is False and p and p["status"] == "OPEN")
    # 15b. 恢复价格后平仓成功
    MOCK_PRICE["BTC"] = 100500
    ok = st.close_supertrend_position("BTC", "REMOVED")
    check("价格恢复后平仓成功", ok is True)
    # 15c. status 表被清空但有仓位 → 下一轮仍正确更新 SL（old_ts None 分支）
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("DELETE FROM supertrend_status WHERE token='BTC'")
    MOCK_PRICE["BTC"] = 100000
    with storage.get_conn() as mc, storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_tokens SET status='ACTIVE' WHERE token='BTC'")
        st._process_token(mc, sc, "BTC", 1.0, 50.0, 3.0)  # 无仓 → 重开
    with storage.get_conn() as mc:
        p = open_pos(mc, "BTC")
    check("status 清空后重开正常", p and p["status"] == "OPEN" and p["side"] == "LONG")
    # 15d. 多币种一轮：一币失败不影响其他币（_run_round 的单币隔离）
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("INSERT OR IGNORE INTO supertrend_tokens (token, status, added_at) "
                   "VALUES ('DOGE', 'ACTIVE', 0)")
    set_sig("BTC", "UP", 101000, 99000)
    set_sig("DOGE", "UP", 0.1, 0.09)
    MOCK_PRICE["BTC"] = 100000
    MOCK_PRICE["DOGE"] = 0.095
    MOCK_SIG.pop("ETH", None)  # ETH 无信号 → 失败，不影响 BTC/DOGE
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_tokens SET status='ACTIVE' WHERE token IN ('BTC','ETH')")
    st._run_round()
    with storage.get_conn() as mc:
        btc = open_pos(mc, "BTC")
        doge = open_pos(mc, "DOGE")
    check("一币失败不影响其他币（BTC 开仓）", btc is not None)
    check("一币失败不影响其他币（DOGE 开仓）", doge is not None)
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        eth_row = sc.execute(
            "SELECT fetch_error FROM supertrend_status WHERE token='ETH'").fetchone()
    check("失败币记录 fetch_error", eth_row and eth_row["fetch_error"] == "okx_fetch_failed",
          str(dict(eth_row) if eth_row else None))

    print("== 16. 参数防御：异常值回退默认 ==")
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
        storage.trading_settings_update(mc, {
            "supertrend_enabled": True,
            "supertrend_sl_tolerance_pct": -5.0,   # 负数 → 回退 1.0
            "supertrend_notional_usd": -100,       # 负数 → 回退 50
            "supertrend_leverage": 0,              # 0 → 回退 3
        })
    with storage.get_conn(config.SUPERTREND_DB) as sc:
        sc.execute("UPDATE supertrend_tokens SET status='ACTIVE' WHERE token='BTC'")
        sc.execute("UPDATE supertrend_tokens SET status='REMOVED' WHERE token != 'BTC'")
    set_sig("BTC", "UP", 101000, 99000)
    MOCK_PRICE["BTC"] = 100000
    st._run_round()
    with storage.get_conn() as mc:
        p = open_pos(mc, "BTC")
    check("容忍-5 回退默认 → SL=99000*0.99=98010",
          p and abs(p["stop_loss_price"] - 98010) < 1e-6,
          str(p and p["stop_loss_price"]))
    check("金额-100 回退默认 → margin=50/3",
          p and abs(p["margin_amount"] - 50 / 3) < 1e-6, str(p and p["margin_amount"]))
    check("杠杆0 回退默认 → leverage=3", p and p["leverage"] == 3, str(p and p["leverage"]))
    # 16b. 容忍 0 是合法值：SL = 轨道本身（不被 falsy 陷阱吞掉）
    with storage.get_conn() as mc:
        mc.execute("DELETE FROM trade_positions WHERE strategy='supertrend'")
        storage.trading_settings_update(mc, {"supertrend_sl_tolerance_pct": 0.0})
    set_sig("BTC", "UP", 101000, 99000)
    MOCK_PRICE["BTC"] = 100000
    st._run_round()
    with storage.get_conn() as mc:
        p = open_pos(mc, "BTC")
    check("容忍0 → SL=下轨道本身 99000",
          p and abs(p["stop_loss_price"] - 99000) < 1e-6,
          str(p and p["stop_loss_price"]))
    # 恢复正常设置
    with storage.get_conn() as mc:
        storage.trading_settings_update(mc, {
            "supertrend_sl_tolerance_pct": 1.0,
            "supertrend_notional_usd": 50.0,
            "supertrend_leverage": 3.0,
        })

    print("== 17. 旧库迁移：无 forming 列 → init_db 自动加列且数据保留 ==")
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
    check("旧库自动加 3 列",
          all(c in cols for c in ("forming_trend", "forming_upper_band", "forming_lower_band")),
          str(cols))
    check("旧数据保留且 forming 为 NULL",
          row and row["trend"] == "UP" and row["last_sl"] == 3
          and row["forming_trend"] is None, str(dict(row) if row else None))
    config.SUPERTREND_DB = os.path.join(_TMP, "supertrend.db")  # 恢复

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
