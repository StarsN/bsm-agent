"""超级趋势策略：主流币 15m 超势永远持仓 + SL 跟随 + 扫损重开。

架构：
- 仓位写入主库 trade_positions（strategy='supertrend'、mode='paper'），由系统
  auto_trader 2s 循环（trade_logic.update_paper_positions）统一扫损检测/平仓
- 策略自身状态（监控币种/信号）在独立 DB config.SUPERTREND_DB
- 线程每 60s 一轮：每轮读 trading_settings（热生效）→ 开关 False 空转 →
  清理已移除币种持仓 → 遍历活跃币种拉 OKX 超势指标 → 开仓/SL 更新

核心规则：无持仓 → 按最新闭合 15m K 线趋势开仓（插针扫损重开原方向与
反转扫损开反向仓是同一条规则）；SL = 持仓侧轨道 ± 容忍%，只朝有利方向移动；
无止盈，唯一退出 = 扫损；移除监控 = 平仓。
"""
import json
import time

import requests

import config
import storage
from market import get_mark_price

OKX_INDICATOR_URL = "https://www.okx.com/api/v5/aigc/mcp/indicators"
CANDLE_MS = 15 * 60 * 1000
# 最新闭合K线的 ts（起点时间）距今 ∈ [15, 30) 分钟：
# 刚闭合时 15 分钟，快切换下一根时接近 30 分钟。
# 过时一根的K线距今 ∈ [30, 45) 分钟——30 分钟是精确分界线。
# 开仓新鲜度上限 = 30 分钟 + 2 分钟时钟余量，杜绝"过时一根"误判新鲜。
DATA_FRESH_MAX_AGE_MS = 32 * 60 * 1000

_SUPERTREND_DDL = """
CREATE TABLE IF NOT EXISTS supertrend_tokens (
    token       TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'ACTIVE',   -- ACTIVE / REMOVED
    added_at    INTEGER,
    removed_at  INTEGER
);

CREATE TABLE IF NOT EXISTS supertrend_status (
    token           TEXT PRIMARY KEY,
    trend           TEXT,                 -- UP / DOWN（最新已闭合15m K线）
    upper_band      REAL,
    lower_band      REAL,
    last_candle_ts  INTEGER,              -- 最新已闭合K线时间戳(ms)，SL 更新去重用
    last_sl         REAL,
    last_price      REAL,
    state           TEXT,                 -- HOLDING / FLAT
    fetch_error     TEXT,
    updated_at      INTEGER,
    forming_trend       TEXT,             -- 正在形成的K线趋势（仅页面参考，策略不用）
    forming_upper_band  REAL,
    forming_lower_band  REAL
);
"""


def init_db():
    """建表 + 幂等种入默认币种（INSERT OR IGNORE：移除过的币不会复活）"""
    with storage.get_conn(config.SUPERTREND_DB) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(_SUPERTREND_DDL)
        # 增量迁移走系统统一工具（同主库/onchain.db 的 _ensure_columns 机制）
        storage._ensure_columns(conn, "supertrend_status", [
            ("forming_trend", "TEXT"),
            ("forming_upper_band", "REAL"),
            ("forming_lower_band", "REAL"),
        ])
        now = int(time.time())
        for token in getattr(config, "SUPERTREND_TOKENS", []) or []:
            conn.execute(
                "INSERT OR IGNORE INTO supertrend_tokens (token, status, added_at) "
                "VALUES (?, 'ACTIVE', ?)",
                (token.upper(), now),
            )


def _parse_okx_response(data: dict) -> list[dict]:
    """从 OKX 指标接口响应中提取 supertrend 数组。

    实测结构（2026-09-05 服务器验证）是深层嵌套：
    {"code":"0","data":[{"data":[{"timeframes":{"15m":
      {"indicators":{"supertrend":[{"ts":...,"values":{...}}, ...]}}}}]}]}
    用递归查找 key=='supertrend' 的列表，兼容 OKX 后续调整包裹结构。
    """
    found: list = []

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "supertrend" and isinstance(v, list):
                    found.extend(v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def _fetch_okx_supertrend(token: str, attempts: int = 3) -> dict | None:
    """拉 OKX 15m 超势指标，返回已闭合K线的
    {ts(ms), trend('UP'/'DOWN'), upper_band, lower_band}，失败返回 None。
    attempts：重试次数（web 添加验证用 1 次快速失败，线程用 3 次）。
    坑：带 paramList 会返回空数组，必须省略（服务端默认 10,3）。
    """
    body = {
        "instId": f"{token.upper()}-USDT-SWAP",
        "timeframes": ["15m"],
        "indicators": {"supertrend": {"returnList": True, "limit": 2}},
    }
    last_err = "unknown"
    for attempt in range(attempts):
        try:
            resp = requests.post(OKX_INDICATOR_URL, json=body, timeout=10)
            if resp.status_code == 200:
                rows = _parse_okx_response(resp.json())
                rows = [r for r in rows if r.get("values")]
                if rows:
                    rows.sort(key=lambda r: int(r.get("ts", 0)))
                    now_ms = int(time.time() * 1000)
                    forming_ts = (now_ms // CANDLE_MS) * CANDLE_MS   # 正在形成的K线起点
                    closed_ts = forming_ts - CANDLE_MS               # 最新已闭合K线起点
                    # 主路径：确定性定位最新闭合K线（命中即100%是它）
                    closed_row = next(
                        (r for r in rows if int(r.get("ts", 0)) == closed_ts), None)
                    if closed_row is None:
                        # 兜底（OKX 延迟/时钟边界）：取返回中最新且已闭合的K线。
                        # 严格校验 age < 30 分钟：K线对齐15分钟整点，此条件下
                        # ts 必然等于 closed_ts（过时一根的 age ≥ 30 分钟被精确排除）
                        cands = [r for r in rows if int(r.get("ts", 0)) < forming_ts]
                        if cands:
                            newest = max(cands, key=lambda x: int(x.get("ts", 0)))
                            age_ms = now_ms - int(newest.get("ts", 0))
                            if age_ms < 2 * CANDLE_MS:
                                closed_row = newest
                    if closed_row:
                        vals = closed_row.get("values") or {}
                        trend = str(vals.get("trend") or "").upper()
                        if trend in ("UP", "DOWN") and vals.get("upperBand") and vals.get("lowerBand"):
                            # 正在形成的K线轨道（仅页面参考，策略不用）
                            forming = None
                            f_row = next((r for r in rows
                                          if int(r.get("ts", 0)) == forming_ts), None)
                            if f_row:
                                fv = f_row.get("values") or {}
                                ft = str(fv.get("trend") or "").upper()
                                if ft in ("UP", "DOWN") and fv.get("upperBand") and fv.get("lowerBand"):
                                    forming = {
                                        "trend": ft,
                                        "upper_band": float(fv["upperBand"]),
                                        "lower_band": float(fv["lowerBand"]),
                                    }
                            return {
                                "ts": int(closed_row.get("ts", 0)),
                                "trend": trend,
                                "upper_band": float(vals["upperBand"]),
                                "lower_band": float(vals["lowerBand"]),
                                "forming": forming,
                            }
                last_err = f"HTTP {resp.status_code} / 数据缺失"
            else:
                last_err = f"HTTP {resp.status_code}"
        except Exception as e:
            last_err = str(e)
        if attempt < attempts - 1:
            time.sleep(2 + attempt * 2)
    if attempts > 1:
        print(f"[supertrend] {token} OKX 指标拉取失败: {last_err}")
    return None


def _calc_sl(side: str, sig: dict, tol_pct: float) -> float:
    """SL 公式：多 = 下轨道×(1−容忍%)，空 = 上轨道×(1+容忍%)"""
    if side == "LONG":
        return round(sig["lower_band"] * (1 - tol_pct / 100), 8)
    return round(sig["upper_band"] * (1 + tol_pct / 100), 8)


def _open_position(conn, token: str, sig: dict, price: float,
                   tol_pct: float, notional: float, leverage: float) -> bool:
    """直接 INSERT 主库 trade_positions（不经 open_paper_position，避免风控/ATR 拦截）。
    前置检查单币单仓（trade_has_active 无 side 过滤）。"""
    if storage.trade_has_active(conn, token, "supertrend"):
        return False
    side = "LONG" if sig["trend"] == "UP" else "SHORT"
    sl = _calc_sl(side, sig, tol_pct)
    position = {
        "token": token,
        "symbol": f"{token}USDT",
        "side": side,
        "status": "OPEN",
        "mode": "paper",                       # 显式写死，与全局 TRADING_MODE 无关
        "margin_amount": notional / leverage,
        "leverage": leverage,
        "notional": notional,
        "quantity": notional / price,
        "entry_price": price,
        "limit_price": price,
        "current_price": price,
        "stop_loss_price": sl,
        "tp1_price": None,
        "tp2_price": None,
        "highest_price": price if side == "LONG" else None,
        "lowest_price": price if side == "SHORT" else None,
        "trailing_stop_price": None,
        "signal_snapshot": json.dumps({
            "trend": sig["trend"],
            "upper_band": sig["upper_band"],
            "lower_band": sig["lower_band"],
            "candle_ts": sig["ts"],
        }, ensure_ascii=False),
        "open_reason": f"超级趋势 15m {sig['trend']} 开{'多' if side == 'LONG' else '空'}",
        "advice": f"跟随15m超级趋势：止损 @ ${sl:.6g}（无止盈）",
        "strategy": "supertrend",
    }
    ok = storage.trade_position_insert(conn, position)
    if ok:
        print(f"[supertrend] {token} 开{side} @ {price} SL={sl}（轨道±{tol_pct}%）")
    else:
        print(f"[supertrend] {token} 开仓 INSERT 失败（唯一索引冲突？）")
    return ok


def _update_sl(conn, pos: dict, sig: dict, tol_pct: float) -> None:
    """新闭合 K 线 → SL 只朝有利方向更新。原生 SQL 带状态条件，防写入已平仓行。
    同步更新 advice 文本（页面/历史显示用，避免显示开仓时的旧止损）。"""
    new_sl = _calc_sl(pos["side"], sig, tol_pct)
    old_sl = pos.get("stop_loss_price")
    if pos["side"] == "LONG":
        if old_sl is None or new_sl > old_sl:
            cur = conn.execute(
                "UPDATE trade_positions SET stop_loss_price=?, advice=?, "
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status IN ('OPEN','PARTIAL')",
                (new_sl, f"跟随15m超级趋势：止损 @ ${new_sl:.6g}（无止盈）", pos["id"]),
            )
            if cur.rowcount > 0:
                print(f"[supertrend] {pos['token']} SL 上移 {old_sl} → {new_sl}")
    else:
        if old_sl is None or new_sl < old_sl:
            cur = conn.execute(
                "UPDATE trade_positions SET stop_loss_price=?, advice=?, "
                "updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status IN ('OPEN','PARTIAL')",
                (new_sl, f"跟随15m超级趋势：止损 @ ${new_sl:.6g}（无止盈）", pos["id"]),
            )
            if cur.rowcount > 0:
                print(f"[supertrend] {pos['token']} SL 下移 {old_sl} → {new_sl}")


def close_supertrend_position(token: str, reason: str = "REMOVED") -> bool:
    """幂等平仓唯一入口（web 移除 + 线程清理安全网共用）。
    口径对齐现有平仓路径：close 价写 current_price、realized/pnl_pct 同口径。
    返回是否实际平掉（已被其他路径平掉返回 False，静默）。"""
    token = token.upper()
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM trade_positions WHERE token=? AND strategy='supertrend' "
            "AND status IN ('OPEN','PARTIAL') ORDER BY id DESC LIMIT 1",
            (token,),
        ).fetchone()
        if not row:
            return False
        pos = dict(row)
        price = get_mark_price(token)
        if not price or price <= 0:
            print(f"[supertrend] {token} 平仓失败：缺少标记价")
            return False
        entry = float(pos.get("entry_price") or pos.get("limit_price") or price)
        qty = float(pos.get("quantity") or 0)
        closed_qty = float(pos.get("closed_qty") or 0)
        open_qty = max(qty - closed_qty, 0)
        if open_qty <= 0:
            return False
        realized = float(pos.get("realized_pnl") or 0)
        if pos.get("side") == "SHORT":
            realized += (entry - price) * open_qty
        else:
            realized += (price - entry) * open_qty
        margin = float(pos.get("margin_amount") or 1)
        advice = "移除监控：按市价平仓" if reason == "REMOVED" else f"超势策略平仓（{reason}）"
        cur = conn.execute(
            "UPDATE trade_positions SET status='CLOSED', current_price=?, "
            "closed_qty=?, realized_pnl=?, unrealized_pnl=0, "
            "pnl_pct=?, advice=?, closed_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status IN ('OPEN','PARTIAL')",
            (price, qty, realized, (realized / margin) * 100, advice, pos["id"]),
        )
        if cur.rowcount == 0:
            return False
        storage.trade_signal_lock_release(conn, token, "supertrend")
        print(f"[supertrend] {token} 平仓（{reason}）@ {price} realized={realized:.4f}")
        return True


def _mark_error(conn_st, token: str, err: str) -> None:
    """记录取数失败（upsert：新币第一次失败也要有行，页面才能看到错误）"""
    now = int(time.time())
    conn_st.execute("""
        INSERT INTO supertrend_status (token, fetch_error, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(token) DO UPDATE SET
            fetch_error=excluded.fetch_error, updated_at=excluded.updated_at
    """, (token, err, now))


def _write_status(conn_st, token: str, sig: dict, price: float, pos: dict | None) -> None:
    """写 supertrend_status：信号/价格/持仓状态，last_candle_ts 用于 SL 更新去重"""
    now = int(time.time())
    state = "HOLDING" if pos else "FLAT"
    forming = sig.get("forming") or {}
    conn_st.execute("""
        INSERT INTO supertrend_status
            (token, trend, upper_band, lower_band, last_candle_ts, last_sl,
             last_price, state, fetch_error, updated_at,
             forming_trend, forming_upper_band, forming_lower_band)
        VALUES (?,?,?,?,?,?,?,?,NULL,?,?,?,?)
        ON CONFLICT(token) DO UPDATE SET
            trend=excluded.trend, upper_band=excluded.upper_band,
            lower_band=excluded.lower_band, last_candle_ts=excluded.last_candle_ts,
            last_sl=excluded.last_sl, last_price=excluded.last_price,
            state=excluded.state, fetch_error=NULL, updated_at=excluded.updated_at,
            forming_trend=excluded.forming_trend,
            forming_upper_band=excluded.forming_upper_band,
            forming_lower_band=excluded.forming_lower_band
    """, (token, sig["trend"], sig["upper_band"], sig["lower_band"], sig["ts"],
          pos.get("stop_loss_price") if pos else None, price, state, now,
          forming.get("trend"), forming.get("upper_band"), forming.get("lower_band")))


def _process_token(conn, conn_st, token: str, tol_pct: float,
                   notional: float, leverage: float) -> None:
    """单币处理：拉信号 → 取价 → 有仓更新 SL / 无仓开仓。独立 try/except 由调用方保证。"""
    sig = _fetch_okx_supertrend(token)
    if sig is None:
        _mark_error(conn_st, token, "okx_fetch_failed")
        return
    price = get_mark_price(token)
    if not price:
        _mark_error(conn_st, token, "mark_price_failed")
        return

    pos = conn.execute(
        "SELECT * FROM trade_positions WHERE token=? AND strategy='supertrend' "
        "AND status IN ('OPEN','PARTIAL') ORDER BY id DESC LIMIT 1",
        (token,),
    ).fetchone()
    pos = dict(pos) if pos else None

    if pos:
        # 新闭合 K 线才更新 SL（用 status 表 last_candle_ts 去重）
        old_row = conn_st.execute(
            "SELECT last_candle_ts FROM supertrend_status WHERE token=?",
            (token,),
        ).fetchone()
        old_ts = old_row["last_candle_ts"] if old_row else None
        if old_ts != sig["ts"]:
            _update_sl(conn, pos, sig, tol_pct)
    else:
        # 数据新鲜才开仓（防止接口陈旧数据导致的错误方向开仓）
        now_ms = int(time.time() * 1000)
        if now_ms - sig["ts"] <= DATA_FRESH_MAX_AGE_MS:
            _open_position(conn, token, sig, price, tol_pct, notional, leverage)
        else:
            _mark_error(conn_st, token, "stale_data")
            return  # 不开仓，保持旧状态（不写 _write_status 以免覆盖错误标记）

    pos = conn.execute(
        "SELECT stop_loss_price FROM trade_positions WHERE token=? AND "
        "strategy='supertrend' AND status IN ('OPEN','PARTIAL') "
        "ORDER BY id DESC LIMIT 1",
        (token,),
    ).fetchone()
    pos_ref = {"stop_loss_price": pos["stop_loss_price"]} if pos else None
    _write_status(conn_st, token, sig, price, pos_ref)


def _run_round() -> None:
    """单轮：读设置（热生效）→ 开关 False 直接返回 → 清理 REMOVED → 遍历 ACTIVE。"""
    with storage.get_conn() as main_conn:
        settings = storage.trading_settings_get(main_conn)
        enabled = bool(settings.get("supertrend_enabled", True))
        # 容忍 0 是合法值（SL=轨道本身），不能用 `or` 默认（0 是 falsy 会被吞掉）
        raw_tol = settings.get("supertrend_sl_tolerance_pct")
        tol = float(raw_tol) if raw_tol is not None else config.SUPERTREND_SL_TOLERANCE_PCT
        notional = float(settings.get("supertrend_notional_usd")
                         or config.SUPERTREND_NOTIONAL_USD)
        leverage = float(settings.get("supertrend_leverage")
                         or config.SUPERTREND_LEVERAGE)
    # 防御异常值（DB 被直接改坏/历史脏数据）：容忍负数会让 SL 算到错误方向
    if not (0 <= tol <= 20):
        tol = config.SUPERTREND_SL_TOLERANCE_PCT
    if notional <= 0:
        notional = config.SUPERTREND_NOTIONAL_USD
    if not (1 <= leverage <= 125):
        leverage = config.SUPERTREND_LEVERAGE
    if not enabled:
        return

    with storage.get_conn(config.SUPERTREND_DB) as conn_st:
        active = [dict(r) for r in conn_st.execute(
            "SELECT * FROM supertrend_tokens WHERE status='ACTIVE'")]
        removed = [dict(r) for r in conn_st.execute(
            "SELECT * FROM supertrend_tokens WHERE status='REMOVED'")]

    # A. 清理已移除币种持仓（安全网；web API 已即时平仓，这里是兜底）
    for t in removed:
        try:
            close_supertrend_position(t["token"], "REMOVED")
        except Exception as e:
            print(f"[supertrend] 清理 {t['token']} 异常: {e}")

    # B. 遍历活跃币种（单币失败不拖累其他币）
    for t in active:
        try:
            with storage.get_conn() as main_conn:
                with storage.get_conn(config.SUPERTREND_DB) as conn_st:
                    _process_token(main_conn, conn_st, t["token"],
                                   tol, notional, leverage)
        except Exception as e:
            print(f"[supertrend] {t['token']} 处理异常: {e}")
        # 币间间隔：平滑 OKX 调用峰值（限额 5次/秒，20币也仅 0.3次/秒，此为保险）
        time.sleep(0.3)


def supertrend_loop():
    """60s 线程：每轮读 trading_settings（热生效）→ 开关 False 空转。
    线程始终创建（worker 无条件启动），设置页运行中切换开关 ≤60s 生效。"""
    init_db()
    print("[supertrend] 已启动")
    while True:
        t0 = time.time()
        try:
            _run_round()
        except Exception as e:
            print(f"[supertrend] 本轮异常: {e}")
        elapsed = time.time() - t0
        time.sleep(max(0.0, 60.0 - elapsed))
