"""Alphafox 跟单信号接入：交易员开/加/减/平 → 等比例镜像到系统仓位。

架构：
- 仓位写主库 trade_positions（strategy='alphafox'，SL/TP 全 NULL，唯一退出=交易员平仓）
- 跟单交易员配置/信号日志在独立 DB config.ALPHAFOX_DB
- 页面"跟单按钮"控制每交易员 enabled；设置页总开关/总额/成本参数热生效
- 信号检测在 web.py 探测线程 polledAt 变化时调用 process()

核心规则：
- 开仓：k_qty = 预算 / 交易员保证金余额（开仓时锁定），目标 qty = 交易员 qty × k_qty
- 三阶段：先平仓 → 再开仓 → 后对齐（对齐式自愈，失败下轮重试差额仍在）
- 同币任意方向已有仓 → 开仓信号跳过（方案B）；该仓成为"未跟随仓"（加/减只识别不操作）
- 首轮：纯基线 + 孤儿平仓（交易员已平的仓我们平掉），不产生信号
"""
import json
import time

import config
import storage
from market import get_mark_price


def _now():
    return int(time.time())


_TRADE_DDL = """
CREATE TABLE IF NOT EXISTS alphafox_traders (
    trader_id   TEXT PRIMARY KEY,
    label       TEXT,
    signal_type TEXT,
    enabled     INTEGER DEFAULT 0,
    updated_at  INTEGER
);

CREATE TABLE IF NOT EXISTS alphafox_signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trader_id       TEXT,
    trader_label    TEXT,
    action          TEXT,          -- OPEN / ADD / REDUCE / CLOSE / SKIP
    symbol          TEXT,
    side            TEXT,
    trader_qty      REAL,          -- 交易员当前数量
    trader_notional REAL,          -- 交易员名义
    price           REAL,          -- 交易员价格（avg/mark）
    executed        INTEGER DEFAULT 0,  -- 系统是否执行了仓位操作
    created_at      INTEGER
);

CREATE TABLE IF NOT EXISTS alphafox_symbol_map (
    src_symbol  TEXT PRIMARY KEY,   -- 剥前缀后的 ticker 统一大写（SKHX / XXX 等，跨 builder 前缀通用）
    token       TEXT NOT NULL,      -- 币安 token（SKHYNIX）；无对应存原 ticker（开仓时币安无价则跳过）
    note        TEXT,
    created_at  INTEGER
);
"""

# Hyperliquid HIP-3 合约 → 币安 token 的显式映射（与币安 ticker 不同的特例）。
# 其余同名标的（MSTR/SNDK/HOOD/INTC/MU/PLTR/TSM 等）剥前缀后同名即币安 token，
# 由同步函数自动入库；此表只维护不同名特例，同步时不被覆盖。
_SEED_SYMBOL_MAP = [
    ("SKHX", "SKHYNIX", "Hyperliquid Trade.xyz SK海力士韩股永续 → 币安 SKHYNIX"),
]

# 首次种入的默认跟单交易员（INSERT OR IGNORE：用户已移除跟单不会被复活）
DEFAULT_TRADERS = [
    ("bn-5075281354358777856", "熬鹰资本"),
    ("hl-0x354dd2b1719ee86a2f4e04befa137c1cbe80c667", "熬鹰链上fomo"),
]

# prev 持仓快照：{trader_id: {(symbol, side): {qty, leverage, notional, avg_price, mark_price}}}
_prev_snap: dict = {}
# 快照异常确认：{trader_id: 连续异常轮数}（空/骤降连续 2 轮才确认真实变化，
# 防 alphofox 快照偶发抖动导致误判平仓）
_anomaly_rounds: dict = {}


class _DataUnavailable(Exception):
    """信号执行所需数据不可用（标记价失败/保证金异常等）。
    信号执行中止，prev 不更新，下轮重试。"""


def init_db():
    """建表 + 幂等种入默认跟单交易员（enabled=1）+ 符号映射种子"""
    with storage.get_conn(config.ALPHAFOX_DB) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(_TRADE_DDL)
        now = _now()
        for tid, label in DEFAULT_TRADERS:
            conn.execute(
                "INSERT OR IGNORE INTO alphafox_traders "
                "(trader_id, label, signal_type, enabled, updated_at) "
                "VALUES (?, ?, '', 1, ?)",
                (tid, label, now),
            )
            conn.execute(
                "UPDATE alphafox_traders SET label=? WHERE trader_id=? AND label=''",
                (label, tid),
            )
        # 信号日志保留 30 天（防无限增长）
        conn.execute(
            "DELETE FROM alphafox_signals WHERE created_at < ?",
            (now - 30 * 86400,),
        )
        # 符号映射种子（INSERT OR IGNORE：手工/同步过的行不被覆盖）
        for src, token, note in _SEED_SYMBOL_MAP:
            conn.execute(
                "INSERT OR IGNORE INTO alphafox_symbol_map "
                "(src_symbol, token, note, created_at) VALUES (?,?,?,?)",
                (src, token, note, now),
            )
    reload_symbol_map()


def set_follow(trader_id: str, label: str, signal_type: str, enabled: bool) -> None:
    """页面跟单按钮：启用/移除该交易员的跟单"""
    with storage.get_conn(config.ALPHAFOX_DB) as conn:
        conn.execute("""
            INSERT INTO alphafox_traders (trader_id, label, signal_type, enabled, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(trader_id) DO UPDATE SET
                label=excluded.label, signal_type=excluded.signal_type,
                enabled=excluded.enabled, updated_at=excluded.updated_at
        """, (trader_id, label, signal_type, 1 if enabled else 0, _now()))
    if not enabled:
        # 取消跟单清基线：重新跟单时首轮重建（不补开、不推过期信号）
        _prev_snap.pop(trader_id, None)
        _anomaly_rounds.pop(trader_id, None)


def following_ids() -> list[str]:
    """当前启用跟单的交易员 id 列表"""
    with storage.get_conn(config.ALPHAFOX_DB) as conn:
        rows = conn.execute(
            "SELECT trader_id FROM alphafox_traders WHERE enabled=1").fetchall()
        return [r["trader_id"] for r in rows]


def following_map() -> dict:
    """trader_id -> enabled（页面按钮状态用）"""
    with storage.get_conn(config.ALPHAFOX_DB) as conn:
        rows = conn.execute("SELECT trader_id, enabled FROM alphafox_traders").fetchall()
        return {r["trader_id"]: bool(r["enabled"]) for r in rows}


def trader_label(trader_id: str) -> str:
    """DB 中用户确认的交易员名字（跟单按钮保存的 label）。
    alphofox API 的 traderName 会间歇性串到别的账户，信号/TG 一律以 DB 为准。"""
    try:
        with storage.get_conn(config.ALPHAFOX_DB) as conn:
            row = conn.execute(
                "SELECT label FROM alphafox_traders WHERE trader_id=?",
                (trader_id,)).fetchone()
            if row and row["label"]:
                return row["label"]
    except Exception:
        pass
    return ""


def _log_signal(conn, sig: dict):
    conn.execute("""
        INSERT INTO alphafox_signals
        (trader_id, trader_label, action, symbol, side, trader_qty,
         trader_notional, price, executed, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (sig.get("trader_id"), sig.get("label"), sig.get("action"),
          sig.get("symbol"), sig.get("side"), sig.get("trader_qty"),
          sig.get("trader_notional"), sig.get("price"),
          1 if sig.get("executed") else 0, _now()))


# 符号映射内存缓存：None=未加载；init_db/同步后刷新
_map_cache: dict | None = None


def reload_symbol_map() -> None:
    """从 DB 重载映射表到内存缓存（init/同步/管理变更后调用）"""
    global _map_cache
    _map_cache = {}
    try:
        with storage.get_conn(config.ALPHAFOX_DB) as conn:
            for r in conn.execute("SELECT src_symbol, token FROM alphafox_symbol_map"):
                _map_cache[r["src_symbol"]] = r["token"]
    except Exception as e:
        print(f"[alphafox] 映射表加载失败: {e}")


def _token_of(symbol: str) -> str:
    """symbol → 币安 token：剥 HIP-3 builder 前缀（xyz:/km:/cash: 等）+ USDT 后缀
    → 查映射表 → 未收录回退原 ticker（币安无价时开仓跳过）"""
    s = (symbol or "").split(":")[-1]
    s = s[:-4] if s.endswith("USDT") else s
    t = s.upper()
    if _map_cache is None:
        reload_symbol_map()
    return (_map_cache or {}).get(t, t)


def sync_symbols_from_hl() -> dict:
    """从 Hyperliquid meta API 同步全部 HIP-3 合约符号到映射表（设置页按钮）。
    新标的自动入库（token=剥前缀同名），已存在的行（含手工特例映射）不覆盖。"""
    import requests
    resp = requests.post(
        "https://api.hyperliquid.xyz/info",
        json={"type": "meta"}, timeout=15)
    resp.raise_for_status()
    universe = (resp.json().get("universe") or [])
    added, skipped = [], 0
    now = int(time.time())
    with storage.get_conn(config.ALPHAFOX_DB) as conn:
        for item in universe:
            name = str(item.get("name") or "")
            if ":" not in name:
                continue  # 只同步 HIP-3 builder 合约（带前缀）
            src = name.split(":")[-1].upper()  # 存剥前缀后的 ticker（查询键一致）
            token = src
            cur = conn.execute(
                "INSERT OR IGNORE INTO alphafox_symbol_map "
                "(src_symbol, token, note, created_at) VALUES (?,?,?,?)",
                (src, token, "自动同步（Hyperliquid meta）", now))
            if cur.rowcount > 0:
                added.append(src)
            else:
                skipped += 1
    reload_symbol_map()
    print(f"[alphafox] 符号同步完成：新增 {len(added)}，已存在跳过 {skipped}")
    return {"ok": True, "added": len(added), "skipped": skipped,
            "total": len(added) + skipped}


def _buy_price(mark: float, params: dict) -> float:
    return mark * (1 + params["slip"] / 100) * (1 + params["fee"] / 100)


def _sell_price(mark: float, params: dict) -> float:
    return mark * (1 - params["slip"] / 100) * (1 - params["fee"] / 100)


def _our_position(conn, token: str, side: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM trade_positions WHERE token=? AND strategy='alphafox' "
        "AND side=? AND status IN ('OPEN','PARTIAL') ORDER BY id DESC LIMIT 1",
        (token, side),
    ).fetchone()
    return dict(row) if row else None


def _owned_by(pos: dict, trader_id: str) -> bool:
    try:
        snap = json.loads(pos.get("signal_snapshot") or "{}")
        return snap.get("trader_id") == trader_id
    except Exception:
        return False


def _open_our_position(trader_id, label, key, cv, margin_balance, params) -> bool:
    """开仓：锁定 k_qty，按币安标记价（含成本）买入。已有同币任意方向仓 → 跳过。"""
    symbol, side = key
    token = _token_of(symbol)
    if not margin_balance or margin_balance <= 0:
        raise _DataUnavailable(f"{symbol}: marginBalance 异常")
    with storage.get_conn() as conn:
        if storage.trade_has_active(conn, token, "alphafox"):
            return False  # 同币任意方向已有仓 → 跳过（方案B，预期行为）
        mark = get_mark_price(token)
        if not mark or mark <= 0:
            # 币安无此交易对 → 不跟单开仓，输出日志（平仓/对齐仍可快照价兜底）
            print(f"[alphafox] {label} 开仓跳过 {symbol}：币安无交易对 {token}（映射表可维护）")
            return False
        k_qty = params["budget"] / margin_balance
        target_qty = cv["qty"] * k_qty
        if target_qty <= 0:
            return False
        # 成交价方向：多=买入价（mark×成本上浮），空=卖出价（mark×成本下浮）
        entry = _buy_price(mark, params) if side == "LONG" else _sell_price(mark, params)
        leverage = max(cv["leverage"], 0.1)
        position = {
            "token": token,
            "symbol": symbol,
            "side": side,
            "status": "OPEN",
            "mode": "paper",
            "margin_amount": target_qty * entry / leverage,
            "leverage": leverage,
            "notional": target_qty * entry,
            "quantity": target_qty,
            "entry_price": entry,
            "limit_price": entry,
            "current_price": entry,
            "stop_loss_price": None,
            "tp1_price": None,
            "tp2_price": None,
            "highest_price": entry if side == "LONG" else None,
            "lowest_price": entry if side == "SHORT" else None,
            "trailing_stop_price": None,
            "signal_snapshot": json.dumps(
                {"trader_id": trader_id, "k_qty": k_qty, "label": label},
                ensure_ascii=False),
            "open_reason": f"跟单 {label} 开{'多' if side == 'LONG' else '空'} {symbol}",
            "advice": f"跟单 {label}（唯一退出=交易员平仓）",
            "strategy": "alphafox",
        }
        ok = storage.trade_position_insert(conn, position)
        if ok:
            print(f"[alphafox] 跟单 {label} 开{side} {symbol} "
                  f"qty={target_qty:.6g} @ {entry} 杠杆{leverage}x")
        return ok


def _close_our_position(trader_id, key, pv, params) -> bool:
    """平仓：归属匹配才平；幂等。"""
    symbol, side = key
    token = _token_of(symbol)
    with storage.get_conn() as conn:
        pos = _our_position(conn, token, side)
        if not pos:
            return False
        if not _owned_by(pos, trader_id):
            return False
        mark = get_mark_price(token)
        if not mark or mark <= 0:
            # 币安无此合约 → 用交易员快照价格兜底（平仓计价）
            mark = float((pv or {}).get("mark_price")
                         or (pv or {}).get("avg_price") or 0)
        if not mark or mark <= 0:
            raise _DataUnavailable(f"{token}: 标记价不可用")
        sell = _sell_price(mark, params)
        entry = float(pos.get("entry_price") or 0)
        qty = float(pos.get("quantity") or 0)
        closed_qty = float(pos.get("closed_qty") or 0)
        open_qty = max(qty - closed_qty, 0)
        if open_qty <= 0:
            return False
        realized = float(pos.get("realized_pnl") or 0)
        if side == "SHORT":
            realized += (entry - sell) * open_qty
        else:
            realized += (sell - entry) * open_qty
        cur = conn.execute(
            "UPDATE trade_positions SET status='CLOSED', current_price=?, "
            "closed_qty=?, realized_pnl=?, unrealized_pnl=0, "
            "pnl_pct=?, advice='跟单交易员平仓', closed_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status IN ('OPEN','PARTIAL')",
            (sell, qty, realized,
             realized / float(pos.get("margin_amount") or 1) * 100, pos["id"]),
        )
        if cur.rowcount == 0:
            return False
        print(f"[alphafox] 跟单平仓 {symbol} @ {sell} realized={realized:.4f}")
        return True


def _align_our_position(trader_id, key, cv, params) -> bool:
    """对齐：目标 qty = 交易员 qty × k_qty(锁定)。买/卖差额 + 杠杆同步。"""
    symbol, side = key
    token = _token_of(symbol)
    with storage.get_conn() as conn:
        pos = _our_position(conn, token, side)
        if not pos:
            return False  # 未跟随仓 → 识别不操作
        if not _owned_by(pos, trader_id):
            return False  # 归属其他交易员 → 不操作
        try:
            snap = json.loads(pos.get("signal_snapshot") or "{}")
            k_qty = float(snap.get("k_qty") or 0)
        except Exception:
            k_qty = 0
        if k_qty <= 0:
            return False
        # 系统口径：quantity=总数量，closed_qty=已平数量，open_qty = quantity - closed_qty
        total_qty = float(pos.get("quantity") or 0)
        closed_qty = float(pos.get("closed_qty") or 0)
        open_qty = max(total_qty - closed_qty, 0)
        entry = float(pos.get("entry_price") or 0)
        realized = float(pos.get("realized_pnl") or 0)
        mark = get_mark_price(token)
        if not mark or mark <= 0:
            # 币安无此合约 → 用交易员快照价格兜底（对齐计价）
            mark = float(cv.get("mark_price") or cv.get("avg_price") or 0)
        if not mark or mark <= 0:
            raise _DataUnavailable(f"{token}: 标记价不可用")
        changed = False

        target_qty = cv["qty"] * k_qty
        diff = target_qty - open_qty
        if diff > 1e-8:
            # 加仓：按方向成交价加权均价（total_qty 增加，margin 同步）
            trade_price = (_buy_price(mark, params) if side == "LONG"
                           else _sell_price(mark, params))
            new_total = total_qty + diff
            new_open = open_qty + diff
            new_entry = (entry * open_qty + trade_price * diff) / new_open
            lev = float(pos.get("leverage") or 1)
            conn.execute(
                "UPDATE trade_positions SET quantity=?, entry_price=?, "
                "notional=?, margin_amount=?, current_price=?, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (new_total, new_entry, new_open * new_entry,
                 new_open * new_entry / lev, trade_price, pos["id"]))
            changed = True
            print(f"[alphafox] 跟单加仓 {symbol} +{diff:.6g} @ {trade_price}（均价 {new_entry:.6g}）")
        elif diff < -1e-8:
            # 减仓：卖差额结算（closed_qty 增加，total_qty 不变，margin 同步）
            sell = _sell_price(mark, params)
            delta = min(-diff, open_qty)
            if delta > 1e-8:
                if side == "SHORT":
                    realized += (entry - sell) * delta
                else:
                    realized += (sell - entry) * delta
                new_open = open_qty - delta
                lev = float(pos.get("leverage") or 1)
                conn.execute(
                    "UPDATE trade_positions SET closed_qty=?, realized_pnl=?, "
                    "notional=?, margin_amount=?, current_price=?, "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (closed_qty + delta, realized,
                     new_open * entry, new_open * entry / lev, sell, pos["id"]))
                changed = True
                print(f"[alphafox] 跟单减仓 {symbol} -{delta:.6g} @ {sell}")

        # 杠杆同步（qty 不变也检查）
        target_lev = max(cv["leverage"], 0.1)
        if abs(target_lev - float(pos.get("leverage") or 0)) > 1e-6:
            new_open = max(total_qty - closed_qty, 0) - (0 if diff >= 0 else min(-diff, open_qty))
            conn.execute(
                "UPDATE trade_positions SET leverage=?, "
                "margin_amount=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (target_lev, new_open * entry / target_lev, pos["id"]))
            changed = True
        return changed


def _first_round_cleanup(trader_id: str, cur: dict, params: dict):
    """首轮基线：我们归属该交易员的仓，交易员已无 → 平仓（孤儿清理）"""
    with storage.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trade_positions WHERE strategy='alphafox' "
            "AND status IN ('OPEN','PARTIAL')").fetchall()
        for pos in rows:
            pos = dict(pos)
            if not _owned_by(pos, trader_id):
                continue
            key = (pos.get("symbol"), pos.get("side"))
            if key in cur:
                continue
            _close_our_position(trader_id, key, None, params)


def _default_params() -> dict:
    return {"budget": 1000.0, "slip": 0.05, "fee": 0.04}


def process(trader_id: str, snapshot: dict, params: dict) -> list[dict]:
    """信号处理主入口（web 探测线程 polledAt 变化时调用）。
    三阶段：平仓 → 开仓 → 对齐。返回信号列表（供 TG 推送）。
    """
    data = snapshot.get("data") or {}
    positions = data.get("positions") or []
    margin_balance = float(data.get("marginBalance") or 0)
    label = trader_label(trader_id) or data.get("traderName") or trader_id

    prev = _prev_snap.get(trader_id)

    cur = {}
    for p in positions:
        side = p.get("side")
        if side not in ("LONG", "SHORT"):
            continue
        key = (p.get("symbol"), side)
        if not key[0]:
            continue
        cur[key] = {
            "qty": float(p.get("absQty") or 0),
            "notional": float(p.get("notional") or 0),
            "leverage": float(p.get("leverage") or 1),
            "avg_price": float(p.get("avgPrice") or 0),
            "mark_price": float(p.get("markPrice") or 0),
        }

    # 快照异常确认：positions 空/骤降可能是 alphofox 数据抖动，也可能是交易员真实清仓。
    # 连续 2 轮异常才确认真实变化（平仓确认延迟一轮 30-60s，防误平优先）。
    # 计数用过滤后的 cur（剔除 side 非法等脏数据项）
    anomaly = False
    if prev:
        prev_cnt = len(prev)
        cur_cnt = len(cur)
        if cur_cnt == 0 and prev_cnt > 0:
            anomaly = True
        elif prev_cnt >= 3 and cur_cnt * 3 < prev_cnt * 2:
            # 骤降超过 1/3（3丢2、5丢3、8丢5 等）
            anomaly = True
    if anomaly:
        rounds = _anomaly_rounds.get(trader_id, 0) + 1
        _anomaly_rounds[trader_id] = rounds
        if rounds < 2:
            print(f"[alphafox] {label} 快照异常（第 {rounds} 轮），待下轮确认")
            return []
        print(f"[alphafox] {label} 快照异常连续 2 轮，确认真实变化")
    else:
        _anomaly_rounds.pop(trader_id, None)

    signals = []

    if prev is None:
        # 首轮：基线 + 孤儿清理，无信号
        _first_round_cleanup(trader_id, cur, params)
        _prev_snap[trader_id] = cur
        print(f"[alphafox] {label} 首轮基线建立（{len(cur)} 个持仓）")
        return signals

    retry = False  # 本轮有价格失败信号 → prev 不更新，下轮重试
    with storage.get_conn(config.ALPHAFOX_DB) as st_conn:
        # 阶段1：平仓（prev有 cur无）
        for key in set(prev) - set(cur):
            symbol, side = key
            pv = prev[key]
            sig = {"trader_id": trader_id, "label": label, "action": "CLOSE",
                   "symbol": symbol, "side": side,
                   "trader_qty": 0, "trader_notional": pv.get("notional"),
                   "price": pv.get("avg_price") or pv.get("mark_price")}
            try:
                sig["executed"] = _close_our_position(trader_id, key, pv, params)
            except _DataUnavailable:
                sig["executed"] = False
                sig["retry"] = True
                retry = True
            _log_signal(st_conn, sig)
            signals.append(sig)

        # 阶段2：开仓（prev无 cur有）
        for key in set(cur) - set(prev):
            symbol, side = key
            cv = cur[key]
            sig = {"trader_id": trader_id, "label": label, "action": "OPEN",
                   "symbol": symbol, "side": side,
                   "trader_qty": cv["qty"], "trader_notional": cv["notional"],
                   "price": cv["avg_price"] or cv["mark_price"]}
            try:
                ok = _open_our_position(trader_id, label, key, cv,
                                        margin_balance, params)
            except _DataUnavailable:
                ok = False
                sig["retry"] = True
                retry = True
            if not ok:
                sig["action"] = "SKIP"
            sig["executed"] = ok
            _log_signal(st_conn, sig)
            signals.append(sig)

        # 阶段3：对齐（qty/杠杆变化）
        for key in set(prev) & set(cur):
            pv, cv = prev[key], cur[key]
            if abs(cv["qty"] - pv["qty"]) <= 1e-8 \
                    and abs(cv["leverage"] - pv["leverage"]) <= 1e-8:
                continue
            symbol, side = key
            delta_qty = cv["qty"] - pv["qty"]
            if delta_qty > 1e-8:
                action = "ADD"
            elif delta_qty < -1e-8:
                action = "REDUCE"
            else:
                action = "LEV"  # 仅杠杆变化（不推 TG）
            sig = {"trader_id": trader_id, "label": label, "action": action,
                   "symbol": symbol, "side": side,
                   "trader_qty": cv["qty"], "trader_notional": cv["notional"],
                   "price": cv["mark_price"] or cv["avg_price"]}
            try:
                sig["executed"] = _align_our_position(trader_id, key, cv, params)
            except _DataUnavailable:
                sig["executed"] = False
                sig["retry"] = True
                retry = True
            _log_signal(st_conn, sig)
            signals.append(sig)

    if not retry:
        _prev_snap[trader_id] = cur
    else:
        # prev 不更新（下轮重试），本轮全部信号标 retry 防止 TG 重复推送刷屏
        for s in signals:
            s["retry"] = True
        print(f"[alphafox] {label} 本轮有价格失败信号，保留基线下轮重试")
    return signals
