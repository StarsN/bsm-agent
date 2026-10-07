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
- 托管状态（trade_positions.managed，见下方 MANAGED_* 常量）：非托管仓不跟随
  交易员任何动作（纸盘不动、实盘只做事实同步）；取消跟单/取消托管**不平仓**
"""
import json
import threading
import time

import config
import storage
from market import get_mark_price, has_tradable_perp


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

CREATE TABLE IF NOT EXISTS alphafox_token_settings (
    token         TEXT PRIMARY KEY,
    live_notional REAL,          -- 每币"实盘每份名义"覆盖；NULL/<=0 = 用全局
    live_leverage REAL,          -- 每币"实盘杠杆"覆盖；NULL/<=0 = 用全局
    updated_at    INTEGER
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

# 已见过的平台成交（trader_id → {内容键: True}）：用于把"一次净变化"还原成
# "交易员实际加了几笔"（探测间隔 15s 内的多次加仓只能看到一次净变化）
_seen_orders: dict = {}
# 首轮识别的孤儿候选：{trader_id: {(symbol, side)}}，下一轮再确认（连续两轮
# 快照都没有才平），防首轮快照抖动误平
_orphan_pending: dict = {}
# 快照异常确认：{trader_id: {"rounds": 连续异常轮数, "ts": 最近异常时间}}
# 空/骤降连续 2 轮才确认真实变化，防 alphofox 快照偶发抖动导致误判平仓；
# 超过 ANOMALY_MAX_GAP 秒的间隔视为新事件重新计数（防停更多天后恢复首轮抖动误判）
_anomaly_rounds: dict = {}
ANOMALY_MAX_GAP = 600  # 秒
# 恢复托管时"交易员已无该仓"的连续确认：{(trader_id, 纸盘仓id): 连续缺失轮数}。
# 与孤儿/异常同一套路：上游单条脏数据会被逐条跳过（缺失 ≤1/3 不触发异常闸门），
# 单轮判定会把仓误标"已脱离"——标记后守卫全拦，只有用户再点一次才能解除
_reattach_missing: dict = {}

# 快照抖动扣留：{(trader_id, symbol, side): [首次扣留时间, 最近一次扣留时间]}。
# 平台丢行/空包时 tradePoints 里没有 CLOSE 点 → 判为抖动：暂不平仓、基线里保留旧值；
# 扣留超时只发 TG 提醒（**不动仓位**）——宁可人工判断，也不许误平/误开。
_jitter_since: dict = {}
_jitter_notified: dict = {}      # 同键 → 上次提醒时间（同一笔每小时最多一条）
JITTER_NOTIFY_AFTER = 300        # 秒：扣留多久后发 TG 提醒
JITTER_NOTIFY_EVERY = 3600       # 秒：之后重复提醒的最小间隔
JITTER_STALE_AFTER = 600         # 秒：这么久没再被扣留 → 忘掉上一次计时

# 托管状态（trade_positions.managed）：跟单仓是否被系统管理。
# 只有 MANAGED_ON 的仓才跟随交易员动作；其余状态一律不动作（仓位保留）
MANAGED_ON = 1          # 托管中
MANAGED_OFF = 0         # 已取消托管（取消跟单/页面取消托管）
MANAGED_REATTACH = 2    # 恢复中：下轮追平到交易员当前仓 → 1（交易员已无则 → 3）
MANAGED_UNLINKED = 3    # 已脱离：交易员已无此仓，此后不再受其信号影响


def _managed(pos: dict) -> int:
    """读托管状态（老行无值是历史遗留 → 视为托管中）"""
    v = pos.get("managed")
    return MANAGED_ON if v is None else int(v)


def _tp_closed(data: dict, key: tuple) -> bool:
    """平台成交流水（tradePoints）里，这个币**最近一笔是 CLOSE**（= 交易员真的平过）。

    这是"交易员平仓"的**旁证**：快照丢行/空包时 tradePoints 里没有这一笔
    （或最后一笔还是 OPEN），一律当成平台抖动、不许平（2026-09-29 CL 事故）。
    实测 7/7：交易员平过的币都有 CLOSE 点、还持有的一律没有。

    取"最近一笔"而不是"有没有 CLOSE"：他平了又开（last=OPEN）时必须算"没平"。
    比 timeMs 而非靠数组顺序（平台给的是新→旧，但别赌顺序）。"""
    symbol, _side = key
    tok = _token_of(symbol)
    tps = (data or {}).get("tradePoints") or {}
    if not isinstance(tps, dict):
        # 脏包（tradePoints 给成 list/str 之类）：当"没有旁证"处理——
        # 直接 .get 会抛 AttributeError 把整轮 process 打断（上游什么都可能给）
        return False
    pts = tps.get(symbol)
    if pts is None:
        pts = tps.get(tok)
    if pts is None:      # 拼写兜底：平台有 BTC / BTCUSDT / xyz:BTC 多种写法
        for k, v in tps.items():
            if _token_of(str(k)) == tok:
                pts = v
                break
    if not isinstance(pts, list):
        return False
    latest_t, latest_act = -1, ""
    for p in pts:
        if not isinstance(p, dict):
            continue
        try:
            t = int(p.get("timeMs") or 0)
        except (TypeError, ValueError):
            continue
        if t > latest_t:
            latest_t, latest_act = t, str(p.get("action") or "").upper()
    return latest_act == "CLOSE"


def _tp_qty_ok(pts, qty):
    """数量旁证（**只记诊断、不做闸门**）：成交流水的 Σ(OPEN)−Σ(CLOSE) 是否等于快照数量。

    实测 CL：平台 8 笔 OPEN 加起来正好 21535.00，与快照一致。
    将来若要防"平台吐错数量"这类抖动，靠的就是这个不变量；现在先攒数据。
    数据不全（缺 quantity/action）→ None（无法判断，别当 False 误导）。"""
    try:
        net, n = 0.0, 0
        for p in pts or []:
            if not isinstance(p, dict):
                continue
            act = str(p.get("action") or "").upper()
            if act not in ("OPEN", "CLOSE"):
                continue
            try:
                q = abs(float(p.get("quantity") or 0))
            except (TypeError, ValueError):
                return None
            net += q if act == "OPEN" else -q
            n += 1
        if not n or not qty:
            return None
        return abs(abs(net) - abs(float(qty))) <= max(abs(float(qty)), 1.0) * 0.001
    except Exception:
        return None


def _note_jitter(trader_id: str, key: tuple) -> None:
    """该仓本轮判为"平台数据抖动"→ 暂不平仓，只计时；超时发一条 TG 提醒。

    **不做任何仓位操作**：系统不替人做"平台就是不给平仓点"这种极端判断。"""
    now = time.time()
    # 先清理陈旧键：> JITTER_STALE_AFTER 没再被扣留 → 上一次的事已经结束
    # （不清的话，同一个币很久以后再抖一次会拿着旧计时立刻触发提醒）
    for k, v in list(_jitter_since.items()):
        if k[0] == trader_id and now - v[1] > JITTER_STALE_AFTER:
            _jitter_since.pop(k, None)
            _jitter_notified.pop(k, None)
    kk = (trader_id, key[0], key[1])
    rec = _jitter_since.get(kk)
    if rec is None:
        print(f"[alphafox] 抖动扣留：{key[0]} {key[1]} 平台成交流水无平仓记录"
              f" → 本轮不平（等证据）")
        rec = [now, now]
    else:
        rec = [rec[0], now]
    _jitter_since[kk] = rec
    held = now - rec[0]
    if held < JITTER_NOTIFY_AFTER:
        return
    if now - _jitter_notified.get(kk, 0.0) < JITTER_NOTIFY_EVERY:
        return
    _jitter_notified[kk] = now
    mins = int(held // 60)
    print(f"[AFLIVE-ESCAPE] {key[0]} {key[1]} 已扣留 {mins} 分钟且始终无平仓记录"
          f" → 发 TG 提醒（未动仓位）")
    try:
        import tg_notify
        threading.Thread(
            target=tg_notify.send_alphafox_jitter_alert,
            args=(_token_of(key[0]), key[1], mins), daemon=True).start()
    except Exception as e:
        print(f"[alphafox] 抖动提醒发送失败: {e}")


# 动作写入的托管守卫（拼进 UPDATE 的 WHERE）：读状态到写库之间隔着 get_mark_price
# 网络调用（超时 5s），用户在这个窗口里点"取消托管/取消跟单"的话，只靠函数开头那次
# 读就会照旧平仓/加减仓 —— 与用户拍板的"取消托管一律不平仓"相悖
_ACT_GUARD_ON = f" AND IFNULL(managed,{MANAGED_ON})={MANAGED_ON}"
_ACT_GUARD_ALIGN = (f" AND IFNULL(managed,{MANAGED_ON})"
                    f" IN ({MANAGED_ON},{MANAGED_REATTACH})")


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


def _batch_managed(trader_id: str, value: int, only_in=None) -> int:
    """批量改该交易员归属仓的托管状态（归属取 signal_snapshot.trader_id）。
    only_in 非空时只改当前状态在其中的行。返回改动笔数。"""
    with storage.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, signal_snapshot, managed FROM trade_positions "
            "WHERE strategy='alphafox' AND status IN ('OPEN','PARTIAL')").fetchall()]
        ids = [r["id"] for r in rows
               if _owned_by(r, trader_id)
               and (only_in is None or _managed(r) in only_in)]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        conn.execute(f"UPDATE trade_positions SET managed=? WHERE id IN ({marks})",
                     [value, *ids])
    # 托管状态一变，"连续缺失"计数就从这一轮重新开始：旧计数属于上一次"恢复中"，
    # 留着会让下一次恢复托管时单轮缺失就落"已脱离"（正常启停就能触发）
    for _id in ids:
        _reattach_missing.pop((trader_id, _id), None)
    return len(ids)


def set_follow(trader_id: str, label: str, signal_type: str,
               enabled: bool) -> dict:
    """页面跟单按钮：启用/移除该交易员的跟单（交易员级 = 批量作用于其持仓）。
    取消跟单 = 名下持仓批量"取消托管"（**不平仓**，仓位保留由用户处理）；
    重新跟单 = 这些仓批量转"恢复中"，下轮 process 追平到交易员当前仓位。"""
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
        _orphan_pending.pop(trader_id, None)
        _seen_orders.pop(trader_id, None)
        n = _batch_managed(trader_id, MANAGED_OFF)
        if n:
            print(f"[alphafox] 取消跟单 {trader_id}：{n} 笔持仓取消托管"
                  f"（仓位保留，实盘侧只做事实同步）")
        return {"managed_off": n}
    n = _batch_managed(trader_id, MANAGED_REATTACH,
                       (MANAGED_OFF, MANAGED_UNLINKED))
    if n and not _is_enabled(trader_id):
        # 与单仓恢复同款竞态：批量置 2 之后交易员可能刚被停用（读 enabled 在
        # alphafox.db、写在主库，跨库无法原子）→ 退回非托管，别停在"恢复中"
        _batch_managed(trader_id, MANAGED_OFF, (MANAGED_REATTACH,))
        print(f"[alphafox] 重新跟单 {trader_id}：与取消跟单竞态，已退回非托管")
        return {"reattach": 0, "race_reverted": n}
    if n:
        print(f"[alphafox] 重新跟单 {trader_id}：{n} 笔持仓进入恢复托管（下轮追平）")
    return {"reattach": n}


def set_position_managed(position_id: int, on: bool) -> dict:
    """页面持仓行的托管开关。
    取消托管：只停止动作（交易员加减平一律不跟），**不平仓**；
    恢复托管：置"恢复中"，下轮追平到交易员当前仓位（他已无此仓 → 标记已脱离）。"""
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM trade_positions WHERE id=? AND strategy='alphafox' "
            "AND status IN ('OPEN','PARTIAL')", (position_id,)).fetchone()
        if not row:
            return {"ok": False, "msg": "该仓已结束（可能刚被平掉）"}
        pos = dict(row)
        trader_id = _trader_of(pos)
        if not on:
            _reattach_missing.pop((trader_id, position_id), None)
            cur = conn.execute(
                "UPDATE trade_positions SET managed=? WHERE id=? "
                "AND status IN ('OPEN','PARTIAL')", (MANAGED_OFF, position_id))
            if cur.rowcount == 0:
                return {"ok": False, "msg": "该仓已结束（可能刚被平掉）"}
            return {"ok": True, "managed": MANAGED_OFF,
                    "msg": "已取消托管：仓位保留，不再跟随该交易员"}
        if not trader_id or not _is_enabled(trader_id):
            return {"ok": False,
                    "msg": "该交易员的跟单已关闭，请先在跟单雷达开启跟单再恢复托管"}
        _reattach_missing.pop((trader_id, position_id), None)
        cur = conn.execute(
            "UPDATE trade_positions SET managed=? WHERE id=? "
            "AND status IN ('OPEN','PARTIAL')", (MANAGED_REATTACH, position_id))
        if cur.rowcount == 0:
            return {"ok": False, "msg": "该仓已结束（可能刚被平掉）"}
    # 与"取消跟单"的竞态：读 enabled（alphafox.db）与写 managed（主库）跨库、无法
    # 原子。若这中间该交易员刚被停用，仓会永远停在"恢复中"（process 不再被调度）
    # → 复查一次，被停用就退回非托管，别让页面显示一个永远不会发生的"恢复中"
    if not _is_enabled(trader_id):
        _set_managed(position_id, MANAGED_OFF, MANAGED_REATTACH)
        return {"ok": False,
                "msg": "该交易员刚刚被取消跟单，该仓保持'取消托管'"}
    return {"ok": True, "managed": MANAGED_REATTACH,
            "msg": "恢复中：下一轮将按交易员当前仓位对齐"}


def _is_enabled(trader_id: str) -> bool:
    """该交易员当前是否仍在跟单（探测线程与本函数之间的竞态闸门）"""
    with storage.get_conn(config.ALPHAFOX_DB) as conn:
        row = conn.execute(
            "SELECT enabled FROM alphafox_traders WHERE trader_id=?",
            (trader_id,)).fetchone()
    return bool(row and row["enabled"])


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
    """从 DB 重载映射表到内存缓存（init/同步/管理变更后调用）。
    失败时保持"未加载"（None）→ 下次调用会重试；若置空字典则永久回退 identity，
    已存仓 token 与新信号 token 会不一致（归属匹配不上 → 老仓孤儿）。"""
    global _map_cache
    try:
        cache = {}
        with storage.get_conn(config.ALPHAFOX_DB) as conn:
            for r in conn.execute("SELECT src_symbol, token FROM alphafox_symbol_map"):
                cache[r["src_symbol"]] = r["token"]
        _map_cache = cache
    except Exception as e:
        _map_cache = None
        print(f"[alphafox] 映射表加载失败（下次重试）: {e}")


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


def _trader_of(pos: dict) -> str:
    """仓位归属的交易员 id（开仓时写入 signal_snapshot，平仓不改写 → 历史也认得出）"""
    try:
        snap = json.loads(pos.get("signal_snapshot") or "{}")
        return snap.get("trader_id") or ""
    except Exception:
        return ""


def _owned_by(pos: dict, trader_id: str) -> bool:
    return _trader_of(pos) == trader_id


def _pos_key(pos: dict) -> tuple:
    """库存行的快照 key（规范化：token+USDT, side）。
    历史行的 symbol 存的是上游原始拼写（实测 BTC / ETHUSDT / XYZ:SKHX 三种都出现过），
    而快照侧的 key 恒为 TOKENUSDT —— 不归一会让首轮孤儿流程把这些仓误判成
    "交易员已无此仓"，第二轮直接平掉（实盘跟着平，且无通知）。"""
    sym = pos.get("symbol") or pos.get("token") or ""
    return (f"{_token_of(sym)}USDT", pos.get("side") or "LONG")


# 平台流水的 canonicalAction **实际词表**（服务器真样本实测 + 前端 actLabel 同一套）：
#   open_long / open_short / increase_long / increase_short   → 增加
#   close_long / close_short / decrease_long / decrease_short → 减少
# 2026-09-14 踩过：代码里按 OPEN/ADD/REDUCE/CLOSE 比，词表根本对不上 →
# 笔数还原在生产里**静默空转**（测试夹具也是按错词表造的，所以测试全绿）。
_INC_ACTIONS = ("open_long", "open_short", "increase_long", "increase_short")
_DEC_ACTIONS = ("close_long", "close_short", "decrease_long", "decrease_short")


def _norm_symbol(raw) -> str:
    """流水/持仓里的 symbol 拼写很杂（真样本：'BTC' / 'ETHUSDT' / 'XYZ:SKHX'）→
    统一成内部 key（映射表 token + USDT）。对不上就返回空串（调用方不计这笔）。"""
    try:
        return f"{_token_of(str(raw))}USDT" if raw else ""
    except Exception:
        return ""


def _new_action_orders(trader_id: str, symbol: str, action: str,
                       data: dict, cap: int = 20) -> int:
    """本次快照里**新出现的**该币增加/减少成交笔数（0 = 数据不可用/没有新成交）。

    信号本身是按"持仓净变化"识别的：交易员在探测间隔（默认 15s）内连着加仓，
    我们只能看到一次净变化 → 实盘只"加一份"。平台快照的 `latestOrders` 每笔都带
    `canonicalAction`，按内容键去重后就能数出真实笔数，调用方据此补记信号
    （实盘每笔各加一份）。

    action 传 "ADD"（=增加，含开仓那一笔）或 "REDUCE"（=减少）；OPEN 与 ADD 同向，
    所以调用方对 OPEN 也用 "ADD" 取数。
    symbol 是我们归一化后的 key（token+USDT）——流水里的拼写必须同样归一才比得上。

    **只数、不标记**：水位由 `_seen_orders_advance` 每轮无条件推进 —— 否则
    "同一轮里加了又减（净额没变）"或"跟单之前就存在的历史订单"会一直留在未见集合里，
    下次真加仓时被重复计数（实盘会多加几份）。
    字段脏到算不出内容键 → **不计**（宁可少算一笔，也绝不算成"每轮都是新单"）。
    """
    orders = (data or {}).get("latestOrders")
    if not isinstance(orders, list):
        return 0
    want_inc = action in ("OPEN", "ADD")
    seen = _seen_orders.setdefault(trader_id, {})
    if not seen:
        # 这个交易员的流水我们一次都没见过（刚跟单 / 前几轮 payload 没带
        # latestOrders）→ 列表里全是历史单，一条都不算：宁可这一轮少算，
        # 也不能把几小时的历史成交当成"本轮新增"一次加十几份
        return 0
    n = 0
    for o in orders:
        if not isinstance(o, dict):
            continue
        ca = str(o.get("canonicalAction") or "").strip().lower()
        if not ((want_inc and ca in _INC_ACTIONS)
                or (not want_inc and ca in _DEC_ACTIONS)):
            continue
        if _norm_symbol(o.get("symbol")) != symbol:
            continue
        k = _order_key(o)
        if k is None or k in seen:
            continue
        n += 1
    return min(n, cap)


def _order_key(o: dict):
    """流水内容键（去重/水位用）。字段脏 → None（调用方跳过）"""
    try:
        return (o.get("symbol"), str(o.get("canonicalAction") or "").upper(),
                int(o.get("time") or 0), round(float(o.get("executedQty") or 0), 8))
    except (TypeError, ValueError):
        return None


def _seen_orders_advance(trader_id: str, data: dict, keep: int = 300) -> None:
    """把本轮可见的**全部**流水标为已见（每轮调用一次，在信号处理之后）。

    必须每轮无条件推进：只数不标的话，"加了又减留下净额不变"的单子、以及
    跟单开始前就存在的历史单，会在下一次净加仓时被当成新增笔数重复计数。"""
    orders = (data or {}).get("latestOrders")
    if not isinstance(orders, list):
        return
    seen = _seen_orders.setdefault(trader_id, {})
    for o in orders:
        if not isinstance(o, dict):
            continue
        k = _order_key(o)
        if k is not None:
            seen[k] = True
    while len(seen) > keep:      # 只留最近 300 个键，防长期运行内存涨
        seen.pop(next(iter(seen)))


def _open_our_position(trader_id, label, key, cv, margin_balance, params) -> bool:
    """开仓：锁定 k_qty，按币安标记价（含成本）买入。已有同币任意方向仓 → 跳过。"""
    symbol, side = key
    token = _token_of(symbol)
    if not params.get("allow_open", True):
        return False  # 跟单开关关闭：只跟平仓/对齐，不跟新仓
    if not margin_balance or margin_balance <= 0:
        raise _DataUnavailable(f"{symbol}: marginBalance 异常")
    with storage.get_conn() as conn:
        if storage.trade_has_active(conn, token, "alphafox"):
            return False  # 同币任意方向已有仓 → 跳过（方案B，预期行为）
        mark = get_mark_price(token)
        if not mark or mark <= 0:
            if has_tradable_perp(token):
                # 有交易对但价格拉取失败（API 抖动）→ 重试下轮（prev 不更新）
                raise _DataUnavailable(f"{symbol}: 标记价暂不可用")
            # 币安无此交易对 → 永久跳过（prev 更新不再每轮重试，映射表维护后可跟）
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
                # trader_margin：实盘比例模型 k 的分母（我的可用余额 ÷ 这个值），
                # 实盘层算 k 时从这里读（见 alphafox_live._trader_margin_of）
                {"trader_id": trader_id, "k_qty": k_qty, "label": label,
                 "trader_margin": margin_balance}, ensure_ascii=False),
            "open_reason": f"跟单 {label} 开{'多' if side == 'LONG' else '空'} {symbol}",
            "advice": f"跟单 {label}（唯一退出=交易员平仓）",
            "strategy": "alphafox",
        }
        # 取标记价是网络调用（耗时）→ 落库前再校验一次跟单状态
        if not _is_enabled(trader_id):
            return False
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
        if _managed(pos) != MANAGED_ON:
            # 非托管（取消托管/恢复中/已脱离）→ 不跟随交易员平仓，仓位保留。
            # 本函数是三阶段 CLOSE 与孤儿确认平仓的唯一出口，一处守卫全覆盖
            return False
        mark = get_mark_price(token)
        if not mark or mark <= 0:
            # 币安无此合约 → 用交易员快照价格兜底（平仓计价）
            mark = float((pv or {}).get("mark_price")
                         or (pv or {}).get("avg_price") or 0)
        if not mark or mark <= 0:
            raise _DataUnavailable(f"{token}: 标记价不可用")
        # 平仓成交价按方向：多单卖出（_sell_price）、空单买回（_buy_price）
        exit_px = (_buy_price(mark, params) if side == "SHORT"
                   else _sell_price(mark, params))
        entry = float(pos.get("entry_price") or 0)
        qty = float(pos.get("quantity") or 0)
        closed_qty = float(pos.get("closed_qty") or 0)
        open_qty = max(qty - closed_qty, 0)
        if open_qty <= 0:
            return False
        realized = float(pos.get("realized_pnl") or 0)
        if side == "SHORT":
            realized += (entry - exit_px) * open_qty
        else:
            realized += (exit_px - entry) * open_qty
        cur = conn.execute(
            "UPDATE trade_positions SET status='CLOSED', current_price=?, "
            "closed_qty=?, realized_pnl=?, unrealized_pnl=0, "
            "pnl_pct=?, advice='跟单交易员平仓', closed_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status IN ('OPEN','PARTIAL')" + _ACT_GUARD_ON,
            (exit_px, qty, realized,
             realized / float(pos.get("margin_amount") or 1) * 100, pos["id"]),
        )
        if cur.rowcount == 0:
            return False
        print(f"[alphafox] 跟单平仓 {symbol} @ {exit_px} realized={realized:.4f}")
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
        if _managed(pos) not in (MANAGED_ON, MANAGED_REATTACH):
            # 非托管（取消托管/已脱离）→ 不跟随交易员加减仓。
            # 恢复中(2) 例外：那是"待追平"状态，追平与随后的对齐本就是同一动作
            return False
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
        closed_here = False            # 本次是否已全平（全平后不再做杠杆同步）
        post_open = open_qty           # 变化后的剩余持仓量（供杠杆同步算保证金）
        post_entry = entry             # 变化后的均价

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
            cur = conn.execute(
                "UPDATE trade_positions SET quantity=?, entry_price=?, "
                "notional=?, margin_amount=?, current_price=?, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=? "
                "AND status IN ('OPEN','PARTIAL')" + _ACT_GUARD_ALIGN,
                (new_total, new_entry, new_open * new_entry,
                 new_open * new_entry / lev, trade_price, pos["id"]))
            if cur.rowcount == 0:
                # 读快照(430行)到写库之间隔着 get_mark_price 网络调用，行可能已被
                # 并发平掉（取消跟单/实盘页平仓）→ 不回写过期数量
                return False
            post_open, post_entry = new_open, new_entry
            changed = True
            print(f"[alphafox] 跟单加仓 {symbol} +{diff:.6g} @ {trade_price}（均价 {new_entry:.6g}）")
        elif diff < -1e-8:
            # 减仓：按平仓方向成交价结算（多单卖出 / 空单买回）
            exit_px = (_buy_price(mark, params) if side == "SHORT"
                       else _sell_price(mark, params))
            delta = min(-diff, open_qty)
            if delta > 1e-8:
                if side == "SHORT":
                    realized += (entry - exit_px) * delta
                else:
                    realized += (exit_px - entry) * delta
                new_open = open_qty - delta
                lev = float(pos.get("leverage") or 1)
                if new_open <= 1e-8:
                    # 交易员减到 0（快照行还在）→ 我们全平：
                    # 否则 closed_qty==quantity 但 status 仍 OPEN → 页面幽灵仓位
                    margin = float(pos.get("margin_amount") or 1)
                    cur = conn.execute(
                        "UPDATE trade_positions SET closed_qty=?, "
                        "realized_pnl=?, unrealized_pnl=0, status='CLOSED', "
                        "pnl_pct=?, advice=?, current_price=?, "
                        "closed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP "
                        "WHERE id=? AND status IN ('OPEN','PARTIAL')"
                        + _ACT_GUARD_ALIGN,
                        (total_qty, realized, realized / margin * 100,
                         "跟单交易员减仓至零，全部平仓", exit_px, pos["id"]))
                    if cur.rowcount == 0:
                        return False   # 已被并发平掉 → 不覆盖它的 realized/closed_qty
                    closed_here = True
                    print(f"[alphafox] 跟单减仓 {symbol} 至零 → 平仓 realized={realized:.4f}")
                else:
                    cur = conn.execute(
                        "UPDATE trade_positions SET closed_qty=?, realized_pnl=?, "
                        "notional=?, margin_amount=?, current_price=?, "
                        "updated_at=CURRENT_TIMESTAMP WHERE id=? "
                        "AND status IN ('OPEN','PARTIAL')" + _ACT_GUARD_ALIGN,
                        (closed_qty + delta, realized,
                         new_open * entry, new_open * entry / lev, exit_px, pos["id"]))
                    if cur.rowcount == 0:
                        return False   # 已被并发平掉 → 不覆盖它的 realized/closed_qty
                    post_open = new_open
                    print(f"[alphafox] 跟单减仓 {symbol} -{delta:.6g} @ {exit_px}")
                changed = True

        # 杠杆同步（qty 不变也检查；用变化后的持仓量算保证金，防同时改量+改杠杆时算小）
        target_lev = max(cv["leverage"], 0.1)
        if not closed_here and abs(target_lev - float(pos.get("leverage") or 0)) > 1e-6:
            cur = conn.execute(
                "UPDATE trade_positions SET leverage=?, "
                "margin_amount=?, updated_at=CURRENT_TIMESTAMP WHERE id=? "
                "AND status IN ('OPEN','PARTIAL')" + _ACT_GUARD_ALIGN,
                (target_lev, post_open * post_entry / target_lev, pos["id"]))
            if cur.rowcount == 0:
                return False
            changed = True
        return changed


def _first_round_orphans(trader_id: str, cur: dict) -> set:
    """首轮只**识别**归属该交易员、交易员已无的仓（孤儿候选），不动作。
    真正平仓放到下一轮：首轮快照可能抖动（缺项/空包），此时平仓会误杀仓位，
    且后轮 key 常驻基线不会再发 OPEN → 仓位永久消失。"""
    with storage.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trade_positions WHERE strategy='alphafox' "
            "AND status IN ('OPEN','PARTIAL')").fetchall()
    out = set()
    for row in rows:
        pos = dict(row)
        if not _owned_by(pos, trader_id):
            continue
        key = _pos_key(pos)
        if key not in cur:
            out.add(key)
    return out


def _confirm_orphans(trader_id: str, keys: set, cur: dict, params: dict,
                     data: dict | None = None) -> list:
    """第二轮确认孤儿：两轮快照都没有该 key → 平仓（交易员已真的没有）。
    平台的成交流水里没有平仓点 → 判快照抖动，本轮不确认（下轮再看）。
    返回真的平掉的 key 列表（调用方要落信号日志 + 推 TG：这里平的是真钱）"""
    closed = []
    for key in sorted(keys):
        if key in cur:
            continue
        if not _tp_closed(data or {}, key):
            _note_jitter(trader_id, key)
            continue
        if _close_our_position(trader_id, key, None, params):
            closed.append(key)
    return closed


def _set_managed(position_id: int, value: int, only_if=None) -> bool:
    """按 id 改托管状态；only_if 给定值时只在该行仍是那个状态时改（防覆盖并发操作）"""
    sql = "UPDATE trade_positions SET managed=? WHERE id=? AND status IN ('OPEN','PARTIAL')"
    args: list = [value, position_id]
    if only_if is not None:
        sql += " AND managed=?"
        args.append(only_if)
    with storage.get_conn() as conn:
        return conn.execute(sql, args).rowcount > 0


def _resolve_reattach(trader_id: str, cur: dict, params: dict,
                      data: dict | None = None) -> int:
    """消费"恢复中"(managed=2) 的仓（重新跟单/恢复托管后每个轮次都会跑，直到落定）：
    - 交易员快照里仍有该 key → 追平数量/杠杆 → 托管中（并重算实盘份数）
    - 交易员已无该 key → **不交易**，标记"已脱离"（页面提示用户自行处理）
    放在三阶段之前：追平后本轮 diff 对该仓不再产生动作（基线/数量已一致）。"""
    with storage.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM trade_positions WHERE strategy='alphafox' "
            "AND status IN ('OPEN','PARTIAL')").fetchall()]
    pend = [r for r in rows
            if _managed(r) == MANAGED_REATTACH and _owned_by(r, trader_id)]
    if not pend:
        return 0
    done = 0
    for pos in pend:
        token = pos.get("token")
        side = pos.get("side") or "LONG"
        key = (f"{token}USDT", side)
        try:
            if float(json.loads(pos.get("signal_snapshot") or "{}")
                     .get("k_qty") or 0) <= 0:
                # 没有 k_qty 就没法等比跟随（历史/外部写入的行）：标"已脱离"而不是
                # 假装托管中 —— 否则之后交易员的加/减/平全部 executed=False，
                # 页面显示在跟、实际永不动作
                _reattach_missing.pop((trader_id, pos["id"]), None)
                if _set_managed(pos["id"], MANAGED_UNLINKED, MANAGED_REATTACH):
                    print(f"[alphafox] 恢复托管 {token} {side}：缺 k_qty，无法等比"
                          f"跟随 → 标记已脱离（请自行处理）")
                    done += 1
                continue
        except Exception:
            pass
        cv = cur.get(key)
        if cv is None:
            if not _tp_closed(data or {}, key):
                # 平台成交流水里没有平仓点 → 快照抖动：不标"已脱离"、也不喂给下面的
                # 连续缺失计数（标记后守卫全拦，只有用户再点一次才能解除）
                _note_jitter(trader_id, key)
                continue
            mk = (trader_id, pos["id"])
            miss = _reattach_missing.get(mk, 0) + 1
            if miss < 2:
                _reattach_missing[mk] = miss
                print(f"[alphafox] 恢复托管 {token} {side}：交易员本轮无此仓，"
                      f"待下轮确认（防单条脏快照误标已脱离）")
                continue
            _reattach_missing.pop(mk, None)
            if _set_managed(pos["id"], MANAGED_UNLINKED, MANAGED_REATTACH):
                print(f"[alphafox] 恢复托管 {token} {side}：交易员已无此仓 → "
                      f"标记已脱离（不交易，请自行处理）")
                done += 1
            continue
        _reattach_missing.pop((trader_id, pos["id"]), None)
        try:
            aligned = _align_our_position(trader_id, key, cv, params)
        except _DataUnavailable as e:
            # 价格不可用：留在"恢复中"，下轮重试（不能标托管中——没追平就转托管
            # 会让后续信号建立在错误数量上）
            print(f"[alphafox] 恢复托管 {token} 追平推迟（价格不可用）: {e}")
            continue
        # 实盘侧准备（解豁免 + 按交易所现状冻结比例 k）必须**先成功**再转"托管中"：
        # 对账只在"托管中"跑，而"恢复中"的过滤条件转托管后再也进不来——k 没冻成就
        # 落定的话，下一轮对账拿旧 k 把你手动加减的仓按旧目标市价抹平，永远修不回
        try:
            import alphafox_live
            ready = alphafox_live.on_reattach(token, side)
        except Exception as e:
            print(f"[alphafox] 恢复托管 {token} 实盘侧处理失败: {e}")
            ready = False
        if not ready:
            print(f"[alphafox] 恢复托管 {token} 实盘侧未就绪（交易所查询不可用？）"
                  f"→ 留在恢复中，下轮重试")
            continue
        if _set_managed(pos["id"], MANAGED_ON, MANAGED_REATTACH):
            done += 1
            print(f"[alphafox] 恢复托管 {token} {side}："
                  + ("已对齐到交易员当前仓位" if aligned
                     else "已恢复托管（数量本就在目标，或缺 k_qty 未调整）"))
    return done


def quick_follow(trader_id: str, snapshot: dict, params: dict,
                 dry_run: bool = False) -> dict:
    """「⚡ 快速跟随」（用户 2026-09-17 需求）：把该交易员**当前全部持仓**跟进来。

    纸盘：无 → 建（k_qty 按当前保证金锁定）；有（且归属本交易员）→ 追平当前量；
         两者都置【托管 ON】。
    实盘：交给 `alphafox_live.quick_follow_live` 过闸门（**交易所杠杆≠设置杠杆 →
    该币失败**、非镜像持仓 → 失败），通过后由随后触发的一轮对账补到目标量。
    dry_run=True → 只预览（不写库不下单），供页面先展示再确认。
    """
    if not _is_enabled(trader_id):
        return {"ok": False, "msg": "该交易员未在跟单（先点跟单）", "results": []}
    data = (snapshot or {}).get("data") or snapshot or {}
    positions = data.get("positions") or []
    try:
        margin = float(data.get("marginBalance") or 0)
    except (TypeError, ValueError):
        margin = 0.0
    label = trader_label(trader_id) or data.get("traderName") or trader_id
    import alphafox_live
    results = []
    _seen: set = set()          # 平台同一个仓可能给两种 symbol 拼写（BTC / BTCUSDT
    for p in positions:         # / xyz:BTC）→ 归一后是同一个 token，别列两遍

        raw = p.get("symbol") or p.get("baseAsset")
        side = p.get("side")
        try:
            qty = abs(float(p.get("absQty") or 0))
        except (TypeError, ValueError):
            qty = 0.0
        if not raw or side not in ("LONG", "SHORT") or qty <= 0:
            continue
        token = _token_of(raw)
        if (token, side) in _seen:
            continue
        _seen.add((token, side))
        key = (f"{token}USDT", side)
        r = {"token": token, "side": side, "trader_qty": qty,
             "trader_avg": p.get("avgPrice")}
        try:
            if not has_tradable_perp(token) or not get_mark_price(token):
                # 这行以前静默跳过：页面上只看到"跳过"，日志里什么都没有，
                # 排查时无从下手（2026-09-28 CL 就是栽在这条路径上）
                print(f"[alphafox] {label} 快速跟随跳过 {token}：币安无该合约/无标记价")
                r.update({"action": "跳过", "reason": "币安无该永续/无标记价"})
                results.append(r)
                continue
            cv = {"qty": qty, "notional": float(p.get("notional") or 0),
                  "leverage": float(p.get("leverage") or 1),
                  "avg_price": float(p.get("avgPrice") or 0),
                  "mark_price": float(p.get("markPrice") or 0)}
            with storage.get_conn() as conn:
                row = conn.execute(
                    "SELECT * FROM trade_positions WHERE token=? AND "
                    "strategy='alphafox' AND status IN ('OPEN','PARTIAL') "
                    "ORDER BY id DESC LIMIT 1", (token,)).fetchone()
            row = dict(row) if row else None
            if dry_run:
                r["action"] = "补齐" if row else "新建"
                results.append(r)
                continue
            if row is None:
                ok = _open_our_position(trader_id, label, key, cv, margin, params)
                r["action"] = "新建" if ok else "失败"
                if not ok:
                    # 纸盘都没建起来 → 实盘无事可做（继续往下走只会拿到一句
                    # "纸盘无该币活跃仓"，把真正的原因盖掉）
                    r["reason"] = "纸盘开仓未执行（预算/价格闸门）"
                    results.append(r)
                    continue
            elif not _owned_by(row, trader_id):
                r.update({"action": "跳过",
                          "reason": "该币已由另一个交易员的跟单仓占用"})
                results.append(r)
                continue
            else:
                try:
                    _align_our_position(trader_id, key, cv, params)
                except _DataUnavailable as e:
                    r.update({"action": "失败", "reason": f"价格不可用: {e}"})
                    results.append(r)
                    continue
                _set_managed(row["id"], MANAGED_ON)
                r["action"] = "补齐"
            # 实盘侧：过闸门（d：杠杆不一致 → 失败并记日志）+ 解豁免；
            # 真正的下单交给随后那一轮对账（只有一条下单路径）
            #
            # 这里**不能**调 on_reattach：它是"恢复托管"的语义（按交易所现状冻结
            # 比例 k、零下单），而快速跟随要的是"补齐到交易员当前量 × k"——
            # 按现状冻结会把目标钉在旧规模上，点了等于没点（走查 Q4 抓到）
            lr = alphafox_live.quick_follow_live(token, side, qty)
            r["live"] = lr.get("reason") if not lr.get("ok") else lr.get("action")
            results.append(r)
        except Exception as e:
            r.update({"action": "失败", "reason": f"{type(e).__name__}: {e}"})
            results.append(r)
    if not dry_run:
        try:
            with storage.get_conn() as conn:
                _ts = storage.trading_settings_get(conn)
            alphafox_live.sync_alphafox_live(
                # 默认必须与探测线程一致（False）：设置缺失时 quick_follow 若按 True
                # 触发对账 = 用户没开实盘却下了真单
                bool(_ts.get("alphafox_live_enabled", False)),
                float(_ts.get("alphafox_live_max_notional") or 0),
                float(_ts.get("alphafox_live_leverage") or 5.0))
        except Exception as e:
            print(f"[alphafox] 快速跟随：触发对账失败（下轮轮询会补）: {e}")
    return {"ok": True, "label": label, "results": results}


def _default_params() -> dict:
    return {"budget": 1000.0, "slip": 0.05, "fee": 0.04}


def _anomaly_diag(trader_id: str, label: str, data: dict, prev: dict, cur: dict,
                  rounds: int) -> None:
    """快照异常（清空 / 骤降 >1/3）时打一行**结构化诊断**，供事后定案。

    2026-09-18 UNI 被误判"交易员平仓"（平台连续两轮 positions 不带 UNI、50 秒后
    又恢复）——当时没落任何证据，只能靠推断。这行日志把下次定案要的字段全记下：

      raw_n / raw_syms  原始 positions 数组（**解析前**）：空数组 vs 有条目但解析失败
      sC / tN / uP      平台自己的聚合值：positions 空而聚合值非零 = 数据自相矛盾
      gone[]            消失的那些币在 **tradePoints** 里最后一笔的 action：
                        OPEN（没有 CLOSE 点）= 他根本没平 → 抖动
      polledAt          这一轮快照的版本时间戳（判断是不是旧包/重复包）
    整行以 `[AFLIVE-ANOMALY]` 开头，直接 grep 这个标记即可。"""
    try:
        raw = data.get("positions") or []
        tps = data.get("tradePoints") or {}
        gone = []
        for sym, side in list(set(prev) - set(cur))[:10]:
            tok = sym[:-4] if sym.endswith("USDT") else sym
            pts = tps.get(sym) or tps.get(tok) or []
            # 最新一笔按 timeMs 取大（平台数组是"新→旧"，但别赌顺序；
            # 原实现取 pts[-1] 拿到的是**最老**那笔，一直误导排查）
            newest = None
            for p in pts:
                if not isinstance(p, dict):
                    continue
                if newest is None or (int(p.get("timeMs") or 0)
                                      > int(newest.get("timeMs") or 0)):
                    newest = p
            gone.append({
                "sym": sym, "side": side, "tp_n": len(pts),
                "tp_last": (f"{newest.get('action')}@{newest.get('timeMs')}"
                            if isinstance(newest, dict) else None),
                # 数量旁证：ΣOPEN−ΣCLOSE 是否等于快照数量（只记录，攒证据用）
                "qty_ok": _tp_qty_ok(pts, prev.get((sym, side), {}).get("qty")),
            })
        rec = {
            "label": label, "trader_id": trader_id, "event": "anomaly",
            "round": rounds, "prev_cnt": len(prev), "cur_cnt": len(cur),
            "raw_n": len(raw),
            "raw_syms": [str((p or {}).get("symbol") or (p or {}).get("baseAsset"))
                         for p in raw[:10]],
            "sC": data.get("symbolCount"), "tN": data.get("totalNotional"),
            "uP": data.get("totalUnrealizedPnl"),
            "orders_n": len(data.get("latestOrders") or []),
            "gone": gone, "polledAt": data.get("polledAt"),
        }
        print("[AFLIVE-ANOMALY] " + json.dumps(rec, ensure_ascii=False))
    except Exception as e:      # 诊断日志绝不能影响主流程
        print(f"[alphafox] {label} 异常诊断日志失败: {e}")


def process(trader_id: str, snapshot: dict, params: dict) -> list[dict]:
    """信号处理主入口（web 探测线程 polledAt 变化时调用）。
    三阶段：平仓 → 开仓 → 对齐。返回信号列表（供 TG 推送）。
    """
    # 与"取消跟单"竞态：本轮快照在用户点取消之前就取好了，url 请求期间状态已变。
    # 不校验的话：①为已不再跟单的交易员开新仓（此后不再轮询 → 无人管理的裸仓）
    # ②把 _prev_snap 写回，抵消取消时的清基线（重新跟单首轮不重建 → 推过期信号）
    if not _is_enabled(trader_id):
        _prev_snap.pop(trader_id, None)
        _orphan_pending.pop(trader_id, None)
        return []
    # 映射表不可用（alphafox.db 读取失败）→ 本轮不处理：_token_of 会退化成 identity，
    # key 与库存 position 的 token 对不上 → 平仓信号 executed=False 却照常推进基线
    # = 该仓永久失去交易员退出信号（跟单仓没止损 = 裸仓）
    if _map_cache is None:
        reload_symbol_map()
    if _map_cache is None:
        print(f"[alphafox] {trader_id} 映射表不可用，本轮跳过（不推进基线）")
        return []
    data = snapshot.get("data") or {}
    positions = data.get("positions") or []
    try:
        # 上游会发脏值（实测有 "1,000.50" 这类写法）：裸 float() 抛异常会让本轮
        # 整个不处理（连交易员的平仓信号一起丢掉）。转成 0 只会挡"开新仓"
        # （开仓需要保证金余额），平仓/对齐照常
        margin_balance = float(data.get("marginBalance") or 0)
    except (TypeError, ValueError):
        margin_balance = 0.0
        print(f"[alphafox] {trader_id} marginBalance 异常"
              f"（{data.get('marginBalance')!r}）→ 本轮按 0 处理（只影响开新仓）")
    label = trader_label(trader_id) or data.get("traderName") or trader_id

    prev = _prev_snap.get(trader_id)

    cur = {}
    degraded = False   # 本轮有行解析失败/关键字段缺失 → 不许据此平仓（见下）
    for p in positions:
        # 逐条隔离：上游快照是第三方接口，单条脏数据（字段变字符串、元素不是对象）
        # 不能废掉整个交易员——异常抛出去会让本轮的开/平/对齐全部不执行，
        # 而交易员若持续发脏值，就变成"不跟开仓也不跟平仓"（实盘仓无人管）
        try:
            side = p.get("side")
            if side not in ("LONG", "SHORT"):
                degraded = True
                print(f"[alphafox] {label} 持仓项 side 非法（整条跳过）: {str(p)[:100]}")
                continue
            # 上游 payload 里 symbol 可能缺（它自己用 baseAsset||symbol 渲染）→ 兜底取
            raw_sym = p.get("symbol") or p.get("baseAsset")
            if not raw_sym:
                degraded = True
                print(f"[alphafox] {label} 持仓项无 symbol/baseAsset（整条跳过，"
                      f"本轮不平仓）: {str(p)[:100]}")
                continue
            # key 用**规范化** symbol（token+USDT）。上游同一币的拼写会变——实测近 7 天
            # BTC/BTCUSDT、ZEC/ZECUSDT 两种写法都出现过；用原始串当 key 会变成
            # "旧 key 消失 + 新 key 出现" = 平掉真实仓位再重开（白付手续费 + 白白空仓一段）
            key = (f"{_token_of(raw_sym)}USDT", side)
            if p.get("absQty") is None:
                # 字段缺失/改名 ≠ 减到零。当 0 处理会走"减仓至零全平"直接平掉真仓。
                # 有上一轮就沿用上一轮数量（= 本轮不产生动作），没有就不认这条并降级
                pv = (prev or {}).get(key)
                if pv is None:
                    degraded = True
                    print(f"[alphafox] {label} {key[0]} {side} 缺 absQty 且无历史"
                          f"（整条跳过，本轮不平仓）")
                    continue
                print(f"[alphafox] {label} {key[0]} {side} 缺 absQty → 沿用上轮数量")
                qty = float(pv.get("qty") or 0)
            else:
                qty = float(p.get("absQty"))
            if qty < 0:
                # 只挡负数（absQty 是绝对值，负数=脏数据）。**0 不能挡**：快照行还在
                # 但数量归零 = 交易员减到零的合法状态，由 _align_our_position 走
                # "减仓至零全平"（测试 25 覆盖）；挡掉会变成"这条消失"，被异常闸门
                # 拦下延后一轮、信号也从 REDUCE 变成 CLOSE
                degraded = True
                print(f"[alphafox] {label} 忽略脏持仓 {key[0]} {side} qty={qty}")
                continue
            cur[key] = {
                "qty": qty,
                "notional": float(p.get("notional") or 0),
                "leverage": float(p.get("leverage") or 1),
                "avg_price": float(p.get("avgPrice") or 0),
                "mark_price": float(p.get("markPrice") or 0),
            }
        except (AttributeError, TypeError, ValueError) as e:
            degraded = True
            print(f"[alphafox] {label} 持仓项解析失败（跳过该条，本轮不平仓）: {e} "
                  f"{str(p)[:120]}")

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
        now_ts = int(time.time())
        entry = _anomaly_rounds.get(trader_id)
        if entry and now_ts - entry["ts"] <= ANOMALY_MAX_GAP:
            rounds = entry["rounds"] + 1
        else:
            rounds = 1  # 与上次异常间隔过久 → 视为新事件重新计数
        _anomaly_rounds[trader_id] = {"rounds": rounds, "ts": now_ts}
        _anomaly_diag(trader_id, label, data, prev, cur, rounds)
        if rounds < 2:
            print(f"[alphafox] {label} 快照异常（第 {rounds} 轮），待下轮确认")
            return []
        print(f"[alphafox] {label} 快照异常连续 2 轮，确认真实变化")
    else:
        _prev_anom = _anomaly_rounds.pop(trader_id, None)
        # 只报"刚刚还在异常、这一轮恢复"的（旧计数过期清掉不算恢复，别刷日志）：
        # 恢复得越快越说明那是平台抖动，而不是交易员真的清仓
        if _prev_anom and int(time.time()) - _prev_anom["ts"] <= ANOMALY_MAX_GAP:
            print("[AFLIVE-ANOMALY] "
                  + json.dumps({"label": label, "trader_id": trader_id,
                                "event": "recovered",
                                "anomaly_rounds": _prev_anom.get("rounds"),
                                "cur_cnt": len(cur),
                                "polledAt": data.get("polledAt")},
                               ensure_ascii=False))

    # 恢复托管（managed=2）：追平到交易员当前仓 → 托管中；交易员已无 → 已脱离。
    # 放三阶段之前，且首轮也要跑（重新跟单落到首轮）
    try:
        _resolve_reattach(trader_id, cur, params, data)
    except Exception as e:
        print(f"[alphafox] {label} 恢复托管处理异常: {e}")

    signals = []

    if prev is None:
        # 首轮：只建基线 + 记录孤儿候选（不动作）。孤儿确认放到下一轮，
        # 要求"两轮快照都没有"——首轮快照抖动时不得误平仓位
        _orphan_pending[trader_id] = _first_round_orphans(trader_id, cur)
        if _is_enabled(trader_id):
            _prev_snap[trader_id] = cur
        else:
            _prev_snap.pop(trader_id, None)
        # 水位也必须在这里推进：本轮不产生信号，但流水列表是全量历史（真样本 40+
        # 条、跨度几小时）——不推的话下一轮"真加仓"时会把这几小时的历史单
        # 全算成"本次新增"→ 实盘一次多加十几份
        _seen_orders_advance(trader_id, data)
        print(f"[alphafox] {label} 首轮基线建立（{len(cur)} 个持仓，"
              f"孤儿候选 {len(_orphan_pending[trader_id])} 个待下轮确认）")
        return signals

    # 上一轮记录的孤儿候选 → 本轮再确认（连续两轮都没有才平）
    pend = _orphan_pending.pop(trader_id, None)
    if pend:
        try:
            closed_keys = _confirm_orphans(trader_id, pend, cur, params, data)
            if closed_keys:
                print(f"[alphafox] {label} 孤儿仓确认平仓 {len(closed_keys)} 笔"
                      f"（连续两轮快照无此仓）")
                # 这条路径原来完全静默（不落日志、不推 TG、实盘只能等本轮末尾
                # 对账收敛）——平的是真仓，必须让用户看得见
                with storage.get_conn(config.ALPHAFOX_DB) as st_conn:
                    for key in closed_keys:
                        sig = {"trader_id": trader_id, "label": label,
                               "action": "CLOSE", "symbol": key[0], "side": key[1],
                               "trader_qty": 0, "trader_notional": None,
                               "price": None, "executed": True}
                        _log_signal(st_conn, sig)
                        signals.append(sig)
        except Exception as e:
            # 任何失败（标记价不可用、SQLite 写锁超时等）都必须把候选留回下一轮：
            # 丢了就再没人管这个仓——该 key 不在 prev 也不在 cur，三阶段永远
            # 不会再碰到它，纸盘仓和实盘镜像会无限期挂着（跟单仓没有止损）
            _orphan_pending[trader_id] = pend
            print(f"[alphafox] {label} 孤儿确认推迟（{type(e).__name__}: {e}）")

    # 快照降级（有行解析失败/关键字段缺失）→ 本轮不推进基线（下轮重试），
    # 且整个平仓阶段跳过：把"解析失败"当成"交易员已平仓"会市价平掉真实仓位
    retry = degraded
    if degraded:
        print(f"[alphafox] {label} 快照降级（有持仓项解析失败）→ 本轮跳过平仓阶段、"
              f"基线不推进，下轮重试")
    held_back: set = set()   # 判为平台抖动、本轮不平的仓（基线里保留旧值）
    with storage.get_conn(config.ALPHAFOX_DB) as st_conn:
        # 阶段1：平仓（prev有 cur无）——顺序固定（排序）避免 set 迭代随机
        for key in (set() if degraded else sorted(set(prev) - set(cur))):
            symbol, side = key
            if not _tp_closed(data, key):
                # 平台成交流水里没有平仓点 → 判为快照抖动（丢行/空包），不许平：
                # 误平是真金白银（2026-09-29 CL 事故）。等证据（下轮证据到了正常平），
                # 超时只发 TG 提醒；本仓保留在基线里（见函数尾部），
                # 否则快照恢复时同一个老仓会变成"cur 有 prev 无" → 被当新开仓真下单
                held_back.add(key)
                _note_jitter(trader_id, key)
                continue
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

        # 阶段2：开仓（prev无 cur有）——**排序**：对冲交易员同币同时持多空时，
        # 方案B只放行一笔，按 set 随机顺序会让"跟成哪个方向"随重启变化
        for key in sorted(set(cur) - set(prev)):
            symbol, side = key
            cv = cur[key]
            sig = {"trader_id": trader_id, "label": label, "action": "OPEN",
                   "symbol": symbol, "side": side,
                   "trader_qty": cv["qty"], "trader_notional": cv["notional"], "trader_margin": margin_balance,
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
            # 开仓这一笔也可能压着好几笔成交：真样本里交易员一次开仓 3 分钟打了 5 笔
            # open_long（我们只看到"新键出现"= 一次净变化）→ 只开 1 份等于少跟 4 份，
            # 正是用户 2026-09-14 报的那条。差额补成 **ADD**：镜像层按 ADD 加"一份"，
            # 再发 OPEN 会被当成新仓去重开。
            if ok:
                _n_inc = _new_action_orders(trader_id, symbol, "ADD", data)
                _n_dec = _new_action_orders(trader_id, symbol, "REDUCE", data)
                for _ in range(max(_n_inc - _n_dec, 1) - 1):
                    _s = dict(sig)
                    _s["action"] = "ADD"
                    _log_signal(st_conn, _s)
                    signals.append(_s)

        # 阶段3：对齐（qty/杠杆变化）
        for key in sorted(set(prev) & set(cur)):
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
                   "trader_qty": cv["qty"], "trader_notional": cv["notional"], "trader_margin": margin_balance,
                   "price": cv["mark_price"] or cv["avg_price"]}
            try:
                sig["executed"] = _align_our_position(trader_id, key, cv, params)
            except _DataUnavailable:
                sig["executed"] = False
                sig["retry"] = True
                retry = True
            if sig["executed"] and action == "REDUCE":
                # 交易员减到零时 _align_our_position 直接把仓全平了：对外（TG/日志）
                # 按"平"报，别报成"减"（用户会以为还剩一部分）
                with storage.get_conn() as _c2:
                    _st = _c2.execute(
                        "SELECT 1 FROM trade_positions WHERE token=? AND side=? "
                        "AND strategy='alphafox' AND status IN ('OPEN','PARTIAL') "
                        "LIMIT 1", (_token_of(symbol), side)).fetchone()
                if not _st:
                    sig["action"] = "CLOSE"
            _log_signal(st_conn, sig)
            signals.append(sig)
            # 一次净变化可能压着交易员连着加的好几笔（探测间隔内多次加仓只能看到一次
            # 净变化）→ 按 latestOrders 的真实笔数补记 ADD 信号：实盘才会每笔各加一份、
            # 页面/日志的次数才与交易员一致。数据不可用时返回 0 → 一条都不补（同旧行为）
            if sig["executed"] and action in ("ADD", "REDUCE") \
                    and sig["action"] != "CLOSE":
                # 笔数还原（对称）：一轮里"连加/连减/又加又减"都只形成一次**净变化**，
                # 只按净额记会让份数单向偏高（每出现一次"加多笔 + 减 1 笔"就高 1 份）。
                # 真实净笔数 = 增加笔数 − 减少笔数；主信号已经占了这个方向的 1 条，
                # 差额补记。口径打架（净笔数方向与持仓净变化方向不一致，通常是上游
                # 流水被截断）→ 保守只记主信号那 1 条。
                _n_add = _new_action_orders(trader_id, symbol, "ADD", data)
                _n_red = _new_action_orders(trader_id, symbol, "REDUCE", data)
                _net = _n_add - _n_red
                _want = abs(_net) if (_net > 0) == (action == "ADD") else 1
                for _ in range(max(_want, 1) - 1):
                    _log_signal(st_conn, dict(sig))
                    signals.append(dict(sig))

    # 流水水位每轮无条件推进（在信号处理之后）：只数不标的话，"加了又减留下净额
    # 不变"的单子、跟单开始前就存在的历史单，会在下一次净加仓时被重复计数
    _seen_orders_advance(trader_id, data)

    if not retry:
        # 本轮执行期间被取消 → 不写回（写回会抵消 set_follow 的清基线：
        # 重新跟单首轮不重建 → 按停用前的旧快照补开仓/推过期信号）
        if _is_enabled(trader_id):
            # 判为抖动的仓：基线里**保留旧值**（不能抹掉）——抹掉的话下一轮快照
            # 恢复时同一个老仓会变成"cur 有 prev 无" → 被当新开仓真下单
            _prev_snap[trader_id] = {**cur, **{k: prev[k] for k in held_back}}
        else:
            _prev_snap.pop(trader_id, None)
    else:
        # prev 不更新（下轮重试）。**只给没执行成的信号标 retry**：已执行成功的
        # （真开了/真平了）必须照常推 TG —— 全标 retry 会让这笔真实成交永远不通知
        # （下一轮重算同一 diff 时它已 executed=False，再也不会补推）
        for s in signals:
            if not s.get("executed"):
                s["retry"] = True
        print(f"[alphafox] {label} 本轮有价格失败信号，保留基线下轮重试")
    return signals
