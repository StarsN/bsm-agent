"""超级趋势策略：主流币 15m 超势永远持仓 + SL 跟随 + 扫损重开。

架构：
- 仓位写入主库 trade_positions（strategy='supertrend'、mode='paper'），由系统
  auto_trader 1s 循环（trade_logic.update_paper_positions）统一扫损检测/平仓
- 策略自身状态（监控币种/信号）在独立 DB config.SUPERTREND_DB
- 线程每 60s 一轮：每轮读 trading_settings（热生效）→ 开关 False 空转 →
  清理已移除币种持仓 → 遍历活跃币种拉 OKX 超势指标 → 开仓/SL 更新

核心规则：无持仓 → 按最新闭合 15m K 线趋势开仓（插针扫损重开原方向与
反转扫损开反向仓是同一条规则）；SL = 持仓侧轨道 ± 容忍%，只朝有利方向移动；
无止盈，唯一退出 = 扫损；移除监控 = 平仓。
"""
import json
import math
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
    removed_at  INTEGER,
    notional_usd        REAL,              -- NULL=用全局默认
    sl_tolerance_pct    REAL,              -- NULL=用全局默认
    order_tolerance_pct REAL,              -- NULL=用全局默认
    leverage            REAL,              -- NULL=用全局默认
    direction           TEXT DEFAULT 'BOTH',  -- BOTH / LONG_ONLY / SHORT_ONLY
    flip_market_open    INTEGER DEFAULT 0,  -- 1=趋势反转到允许方向时立即市价开（仅方向限制生效）
    flip_market_tolerance_pct REAL,         -- 反转市价容忍%（NULL=用全局；0=不设限直接市价进）
    paused              INTEGER DEFAULT 0,  -- 1=暂停监控（线程跳过；持仓/挂单不动，1s 循环仍管）
    sort_order          INTEGER,            -- 卡片拖拽排序（NULL 排最后）
    live_mode           INTEGER DEFAULT 0   -- 1=实盘镜像（纸盘为策略引擎，supertrend_live 同步币安）
);

CREATE TABLE IF NOT EXISTS supertrend_status (
    token           TEXT PRIMARY KEY,
    trend           TEXT,                 -- UP / DOWN（最新已闭合15m K线）
    upper_band      REAL,
    lower_band      REAL,
    last_candle_ts  INTEGER,              -- 最新已闭合K线时间戳(ms)，SL 更新去重用
    last_sl         REAL,
    last_price      REAL,
    state           TEXT,                 -- HOLDING / PENDING / WAITING / WAIT_DIR / FLAT
    fetch_error     TEXT,
    updated_at      INTEGER,
    forming_trend       TEXT,             -- 正在形成的K线趋势（仅页面参考，策略不用）
    forming_upper_band  REAL,
    forming_lower_band  REAL
);

CREATE TABLE IF NOT EXISTS supertrend_close_snaps (
    position_id INTEGER PRIMARY KEY,      -- 关联主库 trade_positions.id
    token       TEXT,
    trend       TEXT,                     -- 平仓时刻的最新15m趋势（status 表快照）
    upper_band  REAL,
    lower_band  REAL,
    created_at  INTEGER
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
        storage._ensure_columns(conn, "supertrend_tokens", [
            ("notional_usd", "REAL"),
            ("sl_tolerance_pct", "REAL"),
            ("order_tolerance_pct", "REAL"),
            ("leverage", "REAL"),
            ("direction", "TEXT DEFAULT 'BOTH'"),
            ("flip_market_open", "INTEGER DEFAULT 0"),
            ("flip_market_tolerance_pct", "REAL"),
            ("paused", "INTEGER DEFAULT 0"),
            ("sort_order", "INTEGER"),
            ("live_mode", "INTEGER DEFAULT 0"),
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

                        def _bands(v: dict):
                            """轨道值校验：json 允许 NaN/Infinity 字面量（真值检查拦不住），
                            NaN SL 会让 1s 循环永不扫损、实盘不挂止损 → 持仓裸奔"""
                            ub = v.get("upperBand")
                            lb = v.get("lowerBand")
                            if not ub or not lb:
                                return None
                            try:
                                fub, flb = float(ub), float(lb)
                            except (TypeError, ValueError):
                                return None
                            if not (math.isfinite(fub) and math.isfinite(flb)
                                    and fub > 0 and flb > 0):
                                return None
                            return fub, flb

                        bands = _bands(vals)
                        if trend in ("UP", "DOWN") and bands:
                            # 正在形成的K线轨道（仅页面参考，策略不用）
                            forming = None
                            f_row = next((r for r in rows
                                          if int(r.get("ts", 0)) == forming_ts), None)
                            if f_row:
                                fv = f_row.get("values") or {}
                                ft = str(fv.get("trend") or "").upper()
                                fbands = _bands(fv)
                                if ft in ("UP", "DOWN") and fbands:
                                    forming = {
                                        "trend": ft,
                                        "upper_band": fbands[0],
                                        "lower_band": fbands[1],
                                    }
                            return {
                                "ts": int(closed_row.get("ts", 0)),
                                "trend": trend,
                                "upper_band": bands[0],
                                "lower_band": bands[1],
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


def _calc_order_price(side: str, sig: dict, tol_order_pct: float) -> float:
    """挂单价：多 = 下轨道×(1+挂单容忍%)，空 = 上轨道×(1−挂单容忍%)"""
    if side == "LONG":
        return round(sig["lower_band"] * (1 + tol_order_pct / 100), 8)
    return round(sig["upper_band"] * (1 - tol_order_pct / 100), 8)


def _merge_positions(conn, token: str) -> None:
    """同币同向多笔活跃仓位 → 合并成一笔（数量相加、开仓价加权、名义/保证金相加）。
    后开仓并入最早一笔，被并入行标 CANCELED（不进平仓历史）。
    **本函数与 1s 循环（trade_logic）并发**：SELECT 之后、累加/写回之前，某一行可能
    已被扫损平掉。所以每一行在动手前都要复查状态（keep 靠更新语句的 rowcount 兜底、
    extras 靠累加前重读），否则会把已离场那笔的量并进来，虚增再也回不去。"""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM trade_positions WHERE token=? AND strategy='supertrend' "
        "AND status IN ('OPEN','PARTIAL') ORDER BY id ASC", (token,))]
    if len(rows) < 2:
        return
    # 本函数只合并"同向"多笔（SQL 没有 side 条件）。真出现双向持仓时把空单并进
    # 多单会直接吞掉一侧的盈亏——宁可跳过让人工处理
    if len({r.get("side") or "LONG" for r in rows}) > 1:
        print(f"[supertrend] {token} 同时存在多空双向持仓，跳过合并（请人工处理）")
        return
    keep = rows[0]
    # extras 必须在**累加前**重读一次状态：SELECT 与本次累加之间，1s 循环可能把它扫损
    # 平掉（CLOSED）。那一笔的数量/保证金不该再并进 keep —— 否则 keep 永久虚增，而下面
    # 那句带 status 条件的 CANCELED 更新会 rowcount=0 静默失败，虚增再也回不来
    # （之后的盈亏、减仓、平仓口径全错）。keep 行由下面的 rowcount 检查兜住。
    extras = []
    for r in rows[1:]:
        cur_row = conn.execute(
            "SELECT * FROM trade_positions WHERE id=? AND "
            "status IN ('OPEN','PARTIAL')", (r["id"],)).fetchone()
        if cur_row:
            extras.append(dict(cur_row))
    if not extras:
        return  # 窗口内 extras 全离场 → 本轮无需合并
    keep_qty = float(keep.get("quantity") or 0)
    keep_entry = float(keep.get("entry_price") or 0)
    keep_notional = float(keep.get("notional") or 0)
    keep_margin = float(keep.get("margin_amount") or 0)
    for extra in extras:
        q = float(extra.get("quantity") or 0)
        e = float(extra.get("entry_price") or 0)
        tot_qty = keep_qty + q
        if tot_qty > 0:
            keep_entry = (keep_qty * keep_entry + q * e) / tot_qty
        keep_qty = tot_qty
        keep_notional += float(extra.get("notional") or 0)
        keep_margin += float(extra.get("margin_amount") or 0)
    # 先更新 keep 行（rowcount 检查：keep 若刚被 1s 循环扫损平仓则放弃本轮合并，
    # extras 保持原状由 1s 循环继续管理，避免"keep 已平 + extras 已取消"双丢）
    merged = [keep] + extras
    if keep.get("side") == "LONG":
        keep_high = max([float(r.get("highest_price") or 0) or keep_entry
                         for r in merged])
        cur = conn.execute(
            "UPDATE trade_positions SET quantity=?, entry_price=?, notional=?, "
            "margin_amount=?, highest_price=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status IN ('OPEN','PARTIAL')",
            (keep_qty, keep_entry, keep_notional, keep_margin, keep_high, keep["id"]))
    else:
        keep_low = min([float(r.get("lowest_price") or 0) or keep_entry
                        for r in merged])
        cur = conn.execute(
            "UPDATE trade_positions SET quantity=?, entry_price=?, notional=?, "
            "margin_amount=?, lowest_price=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status IN ('OPEN','PARTIAL')",
            (keep_qty, keep_entry, keep_notional, keep_margin, keep_low, keep["id"]))
    if cur.rowcount == 0:
        return  # keep 已离场：本轮不合并，extras 不动（下轮再判或由 1s 循环管理）
    for extra in extras:
        conn.execute(
            "UPDATE trade_positions SET status='CANCELED', advice=?, "
            "closed_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('OPEN','PARTIAL')",
            (f"加仓合并：已并入持仓 #{keep['id']}（数量相加）", extra["id"]))
    print(f"[supertrend] {token} 合并仓位：{len(merged)} 笔 → #{keep['id']} "
          f"数量 {keep_qty:.8f}")


def _place_order(conn, token: str, sig: dict, price: float, tol_order_pct: float,
                 tol_sl_pct: float, notional: float, leverage: float,
                 force_market: bool = False, allow_with_open: bool = False,
                 note: str = "") -> bool:
    """入场：挂单价在现价正确一侧 → 挂单（PENDING）；否则价格已到位 → 直接市价开仓（OPEN）。
    force_market：跳过挂单价判定直接市价开（趋势反转选项）。
    allow_with_open：已有持仓时也允许入场（反转流程：市价开 + 同轮挂单并存），
    此时挂单价已到位则跳过（不挂第二笔，避免双市价单）。
    note：挂单时附在 open_reason 末尾的说明（反转市价被容忍度拦下时写原因，页面据此留痕）。
    写 open_reason 不写 advice —— 后者会被 1s 循环与 _update_order 重写。
    成交由系统 1s 循环的 PENDING 分支处理（entry=挂单价，maker 语义无滑点）。"""
    if not allow_with_open and storage.trade_has_active(conn, token, "supertrend"):
        return False
    side = "LONG" if sig["trend"] == "UP" else "SHORT"
    limit_price = _calc_order_price(side, sig, tol_order_pct)
    sl = _calc_sl(side, sig, tol_sl_pct)
    snap_json = json.dumps({
        "trend": sig["trend"],
        "upper_band": sig["upper_band"],
        "lower_band": sig["lower_band"],
        "candle_ts": sig["ts"],
    }, ensure_ascii=False)

    # 挂单价在现价错误一侧 → 价格已到位，直接市价开仓
    market_now = force_market or (side == "LONG" and limit_price >= price) \
        or (side == "SHORT" and limit_price <= price)
    if allow_with_open and not force_market and market_now:
        return False  # 反转流程的挂单：价格已到位不再加第二笔

    if market_now:
        if force_market:
            open_reason = (f"超级趋势 15m {sig['trend']} 趋势反转到允许方向，"
                           f"市价开{'多' if side == 'LONG' else '空'}")
            advice = (f"趋势反转：市价开{'多' if side == 'LONG' else '空'} @ ${price:.6g}，"
                      f"止损 @ ${sl:.6g}（无止盈）")
        else:
            open_reason = f"超级趋势 15m {sig['trend']} 价格到位市价开{'多' if side == 'LONG' else '空'}"
            advice = f"价格已到位：市价开仓 @ ${price:.6g}，止损 @ ${sl:.6g}（无止盈）"
        position = {
            "token": token,
            "symbol": f"{token}USDT",
            "side": side,
            "status": "OPEN",
            "mode": "paper",
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
            "signal_snapshot": snap_json,
            "open_reason": open_reason,
            "advice": advice,
            "strategy": "supertrend",
        }
        ok = storage.trade_position_insert(conn, position)
        if ok:
            print(f"[supertrend] {token} 市价开{side} @ {price} SL={sl}"
                  + ("（趋势反转）" if force_market else "（价格到位）"))
        return ok

    position = {
        "token": token,
        "symbol": f"{token}USDT",
        "side": side,
        "status": "PENDING",
        "mode": "paper",
        "margin_amount": notional / leverage,
        "leverage": leverage,
        "notional": notional,
        "quantity": notional / limit_price,
        "entry_price": None,
        "limit_price": limit_price,
        "current_price": limit_price,
        "stop_loss_price": sl,
        "tp1_price": None,
        "tp2_price": None,
        "highest_price": None,
        "lowest_price": None,
        "trailing_stop_price": None,
        "signal_snapshot": snap_json,
        # note 必须写 open_reason，**不能写 advice**：运行期有两处会重写超势挂单的 advice
        # （trade_logic.update_paper_positions 的 1s 循环、本文件的 _update_order）——
        # 留痕活不过 1 秒。open_reason 全项目只有本函数写，是稳定的
        "open_reason": (f"超级趋势 15m {sig['trend']} 挂{'多' if side == 'LONG' else '空'}单"
                        + (f"，{note}" if note else "")),
        "advice": f"挂单中：{'多' if side == 'LONG' else '空'} @ ${limit_price:.6g}，止损 @ ${sl:.6g}",
        "strategy": "supertrend",
    }
    ok = storage.trade_position_insert(conn, position)
    if ok:
        print(f"[supertrend] {token} 挂{side}单 @ {limit_price} SL={sl}")
    else:
        print(f"[supertrend] {token} 挂单 INSERT 失败（唯一索引冲突？）")
    return ok


def _update_order(conn, pos: dict, sig: dict, price: float, tol_order_pct: float,
                  tol_sl_pct: float) -> None:
    """新闭合 K 线 → 挂单价 + SL 无条件跟随轨道（仅 PENDING 状态）。
    新挂单价穿越现价（跑到错误一侧）→ 价格已到位 → 直接转市价开仓。"""
    new_price = _calc_order_price(pos["side"], sig, tol_order_pct)
    new_sl = _calc_sl(pos["side"], sig, tol_sl_pct)
    market_now = (pos["side"] == "LONG" and new_price >= price) \
        or (pos["side"] == "SHORT" and new_price <= price)

    if market_now:
        # 转市价开仓：entry 变现价，quantity 按现价重算（防名义金额漂移）
        new_qty = float(pos.get("notional") or 0) / price
        cur = conn.execute(
            "UPDATE trade_positions SET status='OPEN', entry_price=?, "
            "limit_price=?, current_price=?, quantity=?, "
            "margin_amount=?, highest_price=?, lowest_price=?, "
            "stop_loss_price=?, advice=?, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status='PENDING'",
            (price, price, price, new_qty,
             float(pos.get("notional") or 0) / float(pos.get("leverage") or 1),
             price if pos["side"] == "LONG" else None,
             price if pos["side"] == "SHORT" else None,
             new_sl,
             f"价格已到位：市价开仓 @ ${price:.6g}，止损 @ ${new_sl:.6g}（无止盈）",
             pos["id"]),
        )
        if cur.rowcount > 0:
            print(f"[supertrend] {pos['token']} 挂单价穿越现价，市价开{pos['side']} @ {price} SL={new_sl}")
        return

    # 挂单价变化时 quantity 同步重算：成交价=挂单价（maker），保证名义金额不漂移
    new_qty = float(pos.get("notional") or 0) / new_price
    cur = conn.execute(
        "UPDATE trade_positions SET limit_price=?, stop_loss_price=?, "
        "quantity=?, current_price=?, advice=?, updated_at=CURRENT_TIMESTAMP "
        "WHERE id=? AND status='PENDING'",
        (new_price, new_sl, new_qty, new_price,
         f"挂单中：{'多' if pos['side'] == 'LONG' else '空'} @ ${new_price:.6g}，止损 @ ${new_sl:.6g}",
         pos["id"]),
    )
    if cur.rowcount > 0:
        print(f"[supertrend] {pos['token']} 挂单更新 @ {new_price} SL={new_sl}")


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
    挂单（PENDING）→ 取消。返回是否实际处理（已被处理返回 False，静默）。
    并存场景（市价仓+挂单）：挂单取消后继续平持仓，不能提前 return。"""
    token = token.upper()
    with storage.get_conn() as conn:
        handled = False
        # 挂单 → 取消（并存时持仓还要继续平，不 return；反转窗口可能多行）
        pends = [dict(r) for r in conn.execute(
            "SELECT * FROM trade_positions WHERE token=? AND strategy='supertrend' "
            "AND status='PENDING'", (token,)).fetchall()]
        for pend in pends:
            conn.execute(
                "UPDATE trade_positions SET status='CANCELED', "
                "advice='移除监控：挂单已取消', closed_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status='PENDING'",
                (pend["id"],),
            )
            storage.trade_signal_lock_release(conn, token, "supertrend")
            print(f"[supertrend] {token} 挂单已取消（{reason}）")
            handled = True
        # 持仓 → 全部平掉（反转并存窗口可能有超过一行的活跃仓，逐行处理）
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM trade_positions WHERE token=? AND strategy='supertrend' "
            "AND status IN ('OPEN','PARTIAL')", (token,)).fetchall()]
        if not rows:
            return handled
        price = get_mark_price(token)
        if not price or price <= 0:
            print(f"[supertrend] {token} 平仓失败：缺少标记价")
            return False
        closed_any = False
        for pos in rows:
            entry = float(pos.get("entry_price") or pos.get("limit_price") or price)
            qty = float(pos.get("quantity") or 0)
            closed_qty = float(pos.get("closed_qty") or 0)
            open_qty = max(qty - closed_qty, 0)
            if open_qty <= 0:
                continue
            realized = float(pos.get("realized_pnl") or 0)
            if pos.get("side") == "SHORT":
                realized += (entry - price) * open_qty
            else:
                realized += (price - entry) * open_qty
            margin = float(pos.get("margin_amount") or 1)
            advice = ("移除监控：按市价平仓" if reason == "REMOVED"
                      else f"超势策略平仓（{reason}）")
            cur = conn.execute(
                "UPDATE trade_positions SET status='CLOSED', current_price=?, "
                "closed_qty=?, realized_pnl=?, unrealized_pnl=0, "
                "pnl_pct=?, advice=?, closed_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status IN ('OPEN','PARTIAL')",
                (price, qty, realized, (realized / margin) * 100, advice, pos["id"]),
            )
            if cur.rowcount == 0:
                continue
            storage.trade_signal_lock_release(conn, token, "supertrend")
            print(f"[supertrend] {token} 平仓（{reason}）@ {price} "
                  f"realized={realized:.4f}")
            record_close_snap(token, pos["id"])
            closed_any = True
        return closed_any or handled


def record_close_snap(token: str, position_id: int) -> None:
    """平仓时记录当时的 15m 趋势轨道快照（status 表最近信号，≤60s 新鲜）。
    页面"扫损重开历史"展示用。失败静默（展示数据，不影响主流程）。"""
    try:
        with storage.get_conn(config.SUPERTREND_DB) as conn_st:
            row = conn_st.execute(
                "SELECT trend, upper_band, lower_band FROM supertrend_status "
                "WHERE token=?", (token,)).fetchone()
            if not row:
                return
            conn_st.execute("""
                INSERT INTO supertrend_close_snaps
                (position_id, token, trend, upper_band, lower_band, created_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(position_id) DO UPDATE SET
                    trend=excluded.trend, upper_band=excluded.upper_band,
                    lower_band=excluded.lower_band, created_at=excluded.created_at
            """, (position_id, token, row["trend"], row["upper_band"],
                  row["lower_band"], int(time.time())))
    except Exception as e:
        print(f"[supertrend] 平仓快照记录失败 {token}: {e}")


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
    state = pos.get("_state") if pos else "FLAT"
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


def _process_token(conn, conn_st, token: str, tol_sl_pct: float,
                   tol_order_pct: float, notional: float, leverage: float,
                   direction: str = "BOTH", flip_market_open: bool = False,
                   flip_tol_pct: float | None = None) -> None:
    """单币处理：拉信号 → 取价 → 兜底合并同向多笔 →
    有持仓 → 新K线更新 SL（只朝有利）；有挂单 → 方向受限且趋势反转则取消，
    否则新K线更新挂单价+SL（无条件跟随）；持仓挂单并存时两者都处理；
    无仓无单 → 等新闭合K线才挂单（首次加入先记基准，扫损后等下一根闭合，
    期间 state=WAITING；方向受限且趋势不符 → state=WAIT_DIR；
    flip_market_open=1 且受限方向反转到允许方向 → 市价开仓 + 同轮挂单；
    flip_tol_pct>0 时市价那笔要过容忍：现价超出「反转前趋势线×(1±容忍%)」→ 只挂单）。
    flip_tol_pct=None = **调用方漏传**（不是"显式不设限"，显式不设限传 0）：补 0 并告警，
    否则容忍闸门会静默失效——那正是最容易在"新加一个调用点"时埋下的坑。
    独立 try/except 由调用方保证。"""
    if flip_tol_pct is None:
        print(f"[supertrend] {token} [WARN] flip_tol_pct 未传（调用点漏参数）→ "
              f"本轮按 0（不设限）处理，请补上调用参数")
        flip_tol_pct = 0.0
    sig = _fetch_okx_supertrend(token)
    if sig is None:
        _mark_error(conn_st, token, "okx_fetch_failed")
        return
    price = get_mark_price(token)
    if not price:
        _mark_error(conn_st, token, "mark_price_failed")
        return

    # 注：同向多笔的合并已上移到 _run_round 循环开头（取数/暂停判断之前），
    # 这里不再重复调用（合并不依赖信号，取数失败也要合并）

    pos = conn.execute(
        "SELECT * FROM trade_positions WHERE token=? AND strategy='supertrend' "
        "AND status IN ('OPEN','PARTIAL') ORDER BY id DESC LIMIT 1",
        (token,),
    ).fetchone()
    pos = dict(pos) if pos else None
    pending = conn.execute(
        "SELECT * FROM trade_positions WHERE token=? AND strategy='supertrend' "
        "AND status='PENDING' ORDER BY id DESC LIMIT 1",
        (token,),
    ).fetchone()
    pending = dict(pending) if pending else None

    # 新闭合 K 线判定（轨道值变了才改，用 status 表 last_candle_ts 去重）
    # 轨道值一并取出：反转市价容忍要用「反转前那一轮画的线」，即上一轮写入的轨道
    old_row = conn_st.execute(
        "SELECT last_candle_ts, state, trend, upper_band, lower_band "
        "FROM supertrend_status WHERE token=?",
        (token,),
    ).fetchone()
    old_ts = old_row["last_candle_ts"] if old_row else None
    old_state = old_row["state"] if old_row else None
    old_trend = old_row["trend"] if old_row else None
    new_candle = old_ts != sig["ts"]

    # 趋势反转 TG 通知（所有监控币 UP↔DOWN 翻转；首次无旧趋势不算反转）
    if old_trend is not None and new_candle and old_trend != sig["trend"]:
        try:
            import tg_notify
            tg_notify.send_supertrend_reversal(
                token, old_trend, sig["trend"], sig["upper_band"],
                sig["lower_band"], price, sig["ts"])
        except Exception as e:
            print(f"[supertrend] {token} 反转 TG 发送失败: {e}")

    waiting = False
    dir_wait = False
    direction_ok = (direction == "BOTH"
                    or (direction == "LONG_ONLY" and sig["trend"] == "UP")
                    or (direction == "SHORT_ONLY" and sig["trend"] == "DOWN"))
    dir_label = {"LONG_ONLY": "多", "SHORT_ONLY": "空"}.get(direction, "")
    wait_trend = "上升" if direction == "LONG_ONLY" else "下降"
    # 反转判定：上一轮趋势是受限方向，新K线翻到允许方向（仅方向限制的币）
    allowed_trend = {"LONG_ONLY": "UP", "SHORT_ONLY": "DOWN"}.get(direction)
    was_blocked = (allowed_trend is not None and old_trend is not None
                   and old_trend != allowed_trend)
    flip_open = (bool(flip_market_open) and direction_ok and was_blocked and new_candle)

    # 反转市价容忍：反向反转时价格要穿过的那条轨道（DOWN 趋势画上轨、UP 趋势画下轨），
    # 取上一轮写入的值 = 反转前那一轮的画线。容忍 0 = 不设限（与旧行为一致）；
    # 轨道脏数据（NULL/0）也按不设限处理——不拿脏数据拦真交易。
    flip_ok = True
    flip_note = ""
    # old_row 必须是"紧邻的上一根 K 线"留下的。重启 / 解除暂停 / 切实盘后 status 可能
    # 停更很久（那几轮都不写 _write_status），旧轨道是几小时前的另一条线——拿它当
    # "反转前趋势线"等于随机门闸。
    ref_usable = (old_row is not None and old_ts is not None
                  and old_ts == sig["ts"] - CANDLE_MS)
    if flip_open and flip_tol_pct > 0 and not ref_usable:
        # 参考线不可信 → **不市价追高**，退化成只挂限价单。
        # 不能 fail-open：这些场景（暂停恢复 / OKX 连续取数失败 / 重启 / 切实盘）恰好
        # 就是价格已经跑远的场景，放行等于闸门在最该生效的时候失效。
        flip_ok = False
        flip_note = "反转市价跳过（参考线不可用：状态表停更超过一根K线），仅挂单"
    elif flip_open and flip_tol_pct > 0 and ref_usable:
        flip_ref = float(old_row["upper_band" if old_trend == "DOWN"
                                 else "lower_band"] or 0)
        if flip_ref > 0:
            if sig["trend"] == "UP":
                thr = flip_ref * (1 + flip_tol_pct / 100)
                flip_ok = price <= thr
            else:
                thr = flip_ref * (1 - flip_tol_pct / 100)
                flip_ok = price >= thr
            if not flip_ok:
                flip_note = (f"反转市价跳过（现价 {price:.6g} 超出容忍线 {thr:.6g}"
                             f"={flip_ref:.6g}×(1{'+' if sig['trend'] == 'UP' else '-'}"
                             f"{flip_tol_pct}%)）")

    if pos:
        if new_candle:
            _update_sl(conn, pos, sig, tol_sl_pct)
    if pending:
        if not direction_ok:
            # 方向限制：趋势翻到受限方向 → 取消挂单（与 1s 循环成交竞态：rowcount 谁先谁赢）
            cur = conn.execute(
                "UPDATE trade_positions SET status='CANCELED', advice=?, "
                "closed_at=CURRENT_TIMESTAMP WHERE id=? AND status='PENDING'",
                (f"方向限制（只做{dir_label}）：趋势反转，挂单已取消，等待{wait_trend}趋势",
                 pending["id"]),
            )
            if cur.rowcount > 0:
                storage.trade_signal_lock_release(conn, token, "supertrend")
                print(f"[supertrend] {token} 方向限制：趋势反转，挂单已取消（等{wait_trend}趋势）")
            dir_wait = True
        elif new_candle:
            _update_order(conn, pending, sig, price, tol_order_pct, tol_sl_pct)
    if not pos and not pending:
        now_ms = int(time.time() * 1000)
        if now_ms - sig["ts"] > DATA_FRESH_MAX_AGE_MS:
            _mark_error(conn_st, token, "stale_data")
            return  # 不挂单，保持旧状态（不写 _write_status 以免覆盖错误标记）
        if flip_open and flip_ok:
            # 趋势反转到允许方向：立即市价开一笔 + 同轮照常挂单（挂单价已到位则不挂）
            ok = _place_order(conn, token, sig, price, tol_order_pct, tol_sl_pct,
                              notional, leverage, force_market=True,
                              allow_with_open=True)
            if ok:
                # 挂单可能因"价格已到位"被跳过（_place_order 返回 False），日志要如实
                pend_ok = _place_order(conn, token, sig, price, tol_order_pct,
                                       tol_sl_pct, notional, leverage,
                                       allow_with_open=True)
                print(f"[supertrend] {token} 趋势反转到允许方向：市价开仓"
                      + (" + 挂单（等回调加仓）" if pend_ok else "（挂单价已到位，不再挂第二笔）"))
            else:
                waiting = True
        elif flip_open:
            # 现价已跑离反转点超出容忍 → 不打市价单、只挂限价单等回调。
            # 原因写进挂单 advice，页面在挂单那行下方显示（否则用户看不出来为什么没市价进）
            ok = _place_order(conn, token, sig, price, tol_order_pct, tol_sl_pct,
                              notional, leverage, note=flip_note)
            if not ok:
                waiting = True
            print(f"[supertrend] {token} {flip_note}，仅挂单")
        elif not direction_ok:
            dir_wait = True
            if old_state != "WAIT_DIR":  # 仅进入等待时打印一次
                print(f"[supertrend] {token} 方向限制（只做{dir_label}）：等待{wait_trend}趋势后挂单")
        elif old_ts is not None and new_candle:
            ok = _place_order(conn, token, sig, price, tol_order_pct, tol_sl_pct,
                              notional, leverage)
            if not ok:
                waiting = True
        else:
            # 必须等新闭合K线：首次加入先记基准，扫损后等下一根闭合（防插针后立即用旧轨道重挂）
            waiting = True
            if old_state != "WAITING":  # 仅进入等待时打印一次，避免每轮 60s 日志噪音
                print(f"[supertrend] {token} 等待15m K线闭合后挂单（趋势 {sig['trend']}）")

    # 持仓优先（并存时 state=HOLDING，与页面 position 字段口径一致）
    pos = conn.execute(
        "SELECT stop_loss_price, status FROM trade_positions WHERE token=? AND "
        "strategy='supertrend' AND status IN ('OPEN','PARTIAL') "
        "ORDER BY id DESC LIMIT 1",
        (token,),
    ).fetchone()
    if not pos:
        pos = conn.execute(
            "SELECT stop_loss_price, status FROM trade_positions WHERE token=? AND "
            "strategy='supertrend' AND status='PENDING' "
            "ORDER BY id DESC LIMIT 1",
            (token,),
        ).fetchone()
    if pos:
        pos_ref = {"stop_loss_price": pos["stop_loss_price"],
                   "_state": "PENDING" if pos["status"] == "PENDING" else "HOLDING"}
    elif waiting:
        pos_ref = {"_state": "WAITING"}
    elif dir_wait:
        pos_ref = {"_state": "WAIT_DIR"}
    else:
        pos_ref = None
    _write_status(conn_st, token, sig, price, pos_ref)


def _run_round() -> None:
    """单轮：读设置（热生效）→ 开关 False 直接返回 → 清理 REMOVED → 遍历 ACTIVE。"""
    with storage.get_conn() as main_conn:
        settings = storage.trading_settings_get(main_conn)
        enabled = bool(settings.get("supertrend_enabled", True))
        # 容忍 0 是合法值（SL=轨道本身），不能用 `or` 默认（0 是 falsy 会被吞掉）
        raw_tol = settings.get("supertrend_sl_tolerance_pct")
        tol = float(raw_tol) if raw_tol is not None else config.SUPERTREND_SL_TOLERANCE_PCT
        raw_order_tol = settings.get("supertrend_order_tolerance_pct")
        order_tol = (float(raw_order_tol) if raw_order_tol is not None
                     else config.SUPERTREND_ORDER_TOLERANCE_PCT)
        # getattr：config.py 不入库（.gitignore），部署时可能没跟着更新——
        # 直接用 config.X 会 AttributeError，被 supertrend_loop 的 except 吞掉 → 整策略停摆
        raw_flip_tol = settings.get("supertrend_flip_tolerance_pct")
        flip_tol = (float(raw_flip_tol) if raw_flip_tol is not None
                    else getattr(config, "SUPERTREND_FLIP_TOLERANCE_PCT", 0.0))
        notional = float(settings.get("supertrend_notional_usd")
                         or config.SUPERTREND_NOTIONAL_USD)
        leverage = float(settings.get("supertrend_leverage")
                         or config.SUPERTREND_LEVERAGE)
    # 防御异常值（DB 被直接改坏/历史脏数据）：容忍负数会让 SL 算到错误方向
    if not (0 <= tol <= 20):
        tol = config.SUPERTREND_SL_TOLERANCE_PCT
    if not (0 <= order_tol <= 20):
        order_tol = config.SUPERTREND_ORDER_TOLERANCE_PCT
    if not (0 <= flip_tol <= 20):
        flip_tol = getattr(config, "SUPERTREND_FLIP_TOLERANCE_PCT", 0.0)
    if notional <= 0:
        notional = config.SUPERTREND_NOTIONAL_USD
    if not (1 <= leverage <= 125):
        leverage = config.SUPERTREND_LEVERAGE

    # A. 清理已移除币种持仓（安全网；web API 已即时平仓，这里是兜底）。
    # **放在总开关判断之前**：关掉策略只是"不再开新仓"，不该让"已移除币的残留仓"
    # 永远没人清——那正是这个兜底存在的意义（web API 那一刀可能失败）。
    with storage.get_conn(config.SUPERTREND_DB) as conn_st:
        removed = [dict(r) for r in conn_st.execute(
            "SELECT * FROM supertrend_tokens WHERE status='REMOVED'")]
    for t in removed:
        try:
            close_supertrend_position(t["token"], "REMOVED")
        except Exception as e:
            print(f"[supertrend] 清理 {t['token']} 异常: {e}")

    if not enabled:
        # 总开关关闭：不新开仓，但实盘对账不能停——否则币安 SL 触发/手动平仓后
        # 纸盘与 live 记录长期不收敛（页面/统计全错），直到重新开启。
        # 另：已挂出的单**有意不撤**（用户语义：停策略≠撤单）——挂单仍会被 1s 循环
        # 成交、成交后仍照常镜像实盘，别把这里改成"关闭就撤单/不镜像"
        try:
            import supertrend_live
            supertrend_live.sync_supertrend_live()
        except Exception as e:
            print(f"[supertrend] live 镜像同步异常: {e}")
        return

    with storage.get_conn(config.SUPERTREND_DB) as conn_st:
        active = [dict(r) for r in conn_st.execute(
            "SELECT * FROM supertrend_tokens WHERE status='ACTIVE'")]

    # B. 遍历活跃币种（单币失败不拖累其他币）
    for t in active:
        # per-token 覆盖（NULL=用全局），脏值/越界回退全局值（全局已防御过）。
        # 不能用裸 float()：脏字符串抛异常会跳过整币，导致持仓 SL 停更
        def _num(v, fallback):
            if v is None:
                return fallback
            try:
                return float(v)
            except (TypeError, ValueError):
                return fallback

        tok_dir = t.get("direction") or "BOTH"
        if tok_dir not in ("BOTH", "LONG_ONLY", "SHORT_ONLY"):
            tok_dir = "BOTH"  # 防 DB 手改非法值导致永不挂单
        tok_notional = _num(t.get("notional_usd"), notional)
        tok_tol = _num(t.get("sl_tolerance_pct"), tol)
        tok_order_tol = _num(t.get("order_tolerance_pct"), order_tol)
        tok_lev = _num(t.get("leverage"), leverage)
        if not (0 <= tok_tol <= 20):
            tok_tol = tol
        if not (0 <= tok_order_tol <= 20):
            tok_order_tol = order_tol
        if tok_notional <= 0:
            tok_notional = notional
        if not (1 <= tok_lev <= 125):
            tok_lev = leverage
        tok_flip = t.get("flip_market_open") in (1, "1", True)
        tok_flip_tol = _num(t.get("flip_market_tolerance_pct"), flip_tol)
        if not (0 <= tok_flip_tol <= 20):
            tok_flip_tol = flip_tol
        # 兜底合并同向多笔：纯 DB 操作、不依赖 OKX/标记价，所以放在取数之前。
        # 暂停币也要合并（暂停期间挂单仍可能成交产生第二笔）→ 放在 paused 判断之前
        try:
            with storage.get_conn() as _merge_conn:
                _merge_positions(_merge_conn, t["token"])
        except Exception as e:
            print(f"[supertrend] {t['token']} 合并异常: {e}")
        if t.get("paused") in (1, "1", True):
            continue  # 暂停监控：不拉信号不处理；持仓/挂单由 1s 循环照常扫损/成交
        try:
            with storage.get_conn() as main_conn:
                with storage.get_conn(config.SUPERTREND_DB) as conn_st:
                    _process_token(main_conn, conn_st, t["token"],
                                   tok_tol, tok_order_tol, tok_notional,
                                   tok_lev, tok_dir, tok_flip, tok_flip_tol)
        except Exception as e:
            print(f"[supertrend] {t['token']} 处理异常: {e}")
        # 币间间隔：平滑 OKX 调用峰值（限额 5次/秒，20币也仅 0.3次/秒，此为保险）
        time.sleep(0.3)

    # C. 实盘镜像对账（live_mode=1 的币；异常不拖累下一轮）
    try:
        import supertrend_live
        supertrend_live.sync_supertrend_live()
    except Exception as e:
        print(f"[supertrend] live 镜像同步异常: {e}")


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
