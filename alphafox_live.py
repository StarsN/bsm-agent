"""跟单（alphafox）实盘镜像层。

纸盘（trade_positions, strategy='alphafox'）仍是信号执行主体，本层只做
纸盘 → 币安实盘 的镜像：

- 信号驱动：探测线程 process 返回信号后调 apply_signal（executed 信号即时镜像）
- 对账兜底：每轮 poll 末尾调 sync（纸盘 vs live 记录 vs 交易所三方对账，
  覆盖镜像失败/重启/手动操作后的收敛）
- 跟单无 SL/TP 条件单（唯一退出=交易员平仓），实盘只做开/平/差额
- 互斥：与主流趋势（supertrend）实盘币种级互斥——被占用则跳过实盘（纸盘照跑）
- 开关语义：live_enabled=False → OPEN/ADD 不镜像；CLOSE/REDUCE 仍同步平
  （已有实盘仓继续跟随交易员退出，不平仓冻结）
- 托管语义：纸盘仓 managed≠1（取消托管/恢复中/已脱离）→ 实盘不下任何单，
  只做事实同步（记录 ← 交易所）；交易所侧已空则实盘记录 + 纸盘仓一起收尾
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import config
import exchange
import storage

SYNC_FRESH_MAX_AGE = 90   # 纸盘行新鲜度上限（1s 循环持续刷新）
_QTY_MIN_DIFF = 1e-8
# 漂移修正的容忍带：差额不到目标数量的这个比例就不动（价格小幅波动会造成
# 1-2 个 step 的来回成交，白付手续费且实盘数量看着"自己变"）。
# 份数变化（OPEN/ADD/REDUCE）一次就差一整份，远大于此阈值，不受影响。
_REBAL_MIN_RATIO = 0.05

_rounds_done = 0
MIN_SYNC_ROUNDS = 2       # 启动冷窗：前 N 轮不做补开（等纸盘引擎稳定）
# 进程启动时刻：早于它创建的纸盘仓视为"存量仓"，不自动补开到实盘
# （重启/发版后只跟启动之后新开的仓；已有实盘记录的仓照常对账）
_STARTED_AT = datetime.now(timezone.utc)
STARTUP_GRACE_SECONDS = 5   # 时钟/写入顺序容差


# ---- 工具 ----

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(v) -> datetime | None:
    if not v:
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v, tz=timezone.utc)
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (ValueError, TypeError):
        return None


def _fresh(paper_row: dict) -> bool:
    age = _parse_ts(paper_row.get("updated_at"))
    if age is None:
        return False
    return (_now_utc() - age).total_seconds() <= SYNC_FRESH_MAX_AGE


# 用户点过「⚡ 快速跟随」的币（token|side 键）。存量仓闸门（发版/重启后只跟新仓）
# 是防**自动**把历史纸盘仓补成实盘仓的；显式点击是另一回事，必须放行。
_QF_EXPLICIT: set = set()

# token|side → (原因, 时间戳)：实盘这一腿"最近一次没跟上"的原因（内存，页面显示用）。
# 背景：纸盘可以"托管中"而实盘根本没建起来（例如交易所有手动仓、杠杆不符），
# 每轮对账都会跳过——以前只有日志里能看到，页面显示的还是"托管中"，用户以为在跟。
_live_skip: dict = {}


def _pre_startup(paper_row: dict) -> bool:
    """该纸盘仓是否早于本次进程启动创建（存量仓）。
    存量仓不自动补开到实盘——发版/重启后只跟启动之后新开的仓，否则会把
    历史纸盘仓一次性镜像到实盘（尤其开关打开时镜像层还没生效的场景）。"""
    created = _parse_ts(paper_row.get("created_at"))
    if created is None:
        return False
    return created < _STARTED_AT - timedelta(seconds=STARTUP_GRACE_SECONDS)


def _ex_positions() -> list[dict] | None:
    """交易所持仓快照。None=API 不可用（与空列表区分，防误判）"""
    try:
        exchange._last_error_msg = ""
        r = exchange.get_positions()
        if exchange.get_last_error():
            return None
        return r if isinstance(r, list) else []
    except Exception:
        return None


def _ex_match(ex_positions: list[dict], token: str, side: str) -> dict | None:
    if not ex_positions:
        return None   # None = API 不可用（与空列表"确实没仓"区分），一律当"查不到"
    symbol = f"{token}USDT"
    for p in ex_positions:
        if p["symbol"] != symbol or abs(float(p.get("quantity") or 0)) <= _QTY_MIN_DIFF:
            continue
        pside = p.get("position_side") or "BOTH"
        if pside == "BOTH":
            if (float(p["quantity"]) > 0) == (side == "LONG"):
                return p
        elif pside == side:
            return p
    return None


def _round_qty_ex(token: str, qty: float) -> float:
    """按交易所 stepSize 量化数量（过滤器不可用时原样返回）"""
    try:
        return exchange._round_qty(f"{token.upper()}USDT", qty)
    except Exception:
        return qty


def _step_of(token: str) -> float:
    """交易所该币 stepSize（拿不到 → 0 = 不额外设下限）"""
    try:
        return float((exchange._fetch_symbol_filters(f"{token.upper()}USDT")
                      or {}).get("stepSize") or 0)
    except Exception:
        return 0.0


def _min_qty_of(token: str) -> float:
    """交易所该币 minQty（拿不到 → 0）：差额比它还小就别下单（否则被抬到
    minQty 变成超量买入）"""
    try:
        return float((exchange._fetch_symbol_filters(f"{token.upper()}USDT")
                      or {}).get("minQty") or 0)
    except Exception:
        return 0.0


def _load_orders(live: dict) -> dict:
    try:
        return json.loads(live.get("exchange_orders_json") or "{}")
    except Exception:
        return {}


# 托管状态枚举，与 alphafox_trade.MANAGED_* 一致（本层只读，避免跨模块导入）
MANAGED_ON = 1          # 托管中
MANAGED_OFF = 0         # 已取消托管
MANAGED_REATTACH = 2    # 恢复中（待追平）
MANAGED_UNLINKED = 3    # 已脱离（交易员已无此仓）


def _eff_live_params(token: str, g_lev: float) -> float:
    """该币实盘**杠杆**的生效值：每币覆盖优先，未设置/脏值 → 全局。

    2026-09-17 比例模型：规模不再来自"每份名义"（改成"我的可用余额 ÷ 交易员
    保证金余额"的比例 k，开仓时冻结进记录）；每币覆盖只剩杠杆一项。
    读配置失败 → 回退全局（跟单不能因为配置表读不到就停摆）。"""
    try:
        with storage.get_conn(config.ALPHAFOX_DB) as conn:
            row = conn.execute(
                "SELECT live_leverage FROM alphafox_token_settings "
                "WHERE token=?", (token,)).fetchone()
    except Exception:
        row = None
    lv = float(g_lev or 0)
    if row:
        try:
            v = float(row["live_leverage"] or 0)
            if v > 0:
                lv = v
        except (TypeError, ValueError):
            pass
    return lv


def _load_fresh_orders(lc, live_row: dict) -> dict:
    """从 DB 重读该记录**最新**的 orders（而不是用调用方手里的旧快照）。

    这两个冻结值（lot_notional / lot_qty）是"读—改—写"：拿旧快照写回会把期间
    刚补写的另一个字段冲掉（实测：lot_notional 刚补写就被 lot_qty 的写回覆盖）。"""
    try:
        row = lc.execute("SELECT exchange_orders_json FROM live_positions WHERE id=?",
                         (live_row["id"],)).fetchone()
        if row is not None:
            return _load_orders({"exchange_orders_json": row["exchange_orders_json"]})
    except Exception:
        pass
    return _load_orders(live_row)


_BAL_CACHE_TTL = 60        # 可用余额读数复用窗口（"连开时用第一次读到的"）
_BAL_CACHE: dict = {"ts": 0.0, "v": 0.0, "eq": 0.0}


def available_balance_cached() -> float:
    """我的币安**可用余额**（60 秒内复用第一次读到的值）。

    **读不到 → 返回 -1**（不是 0）：0 是个合法读数（保证金用满了 = 加不动了），
    调用方必须能区分"余额真的是 0"和"接口没读到" —— 混在一起会让余额为 0 的
    账户被当成读失败，继续加仓（测试 69 的第 6 次抓到）。

    用户语义（2026-09-17）：实盘比例 `k = 我的可用余额 ÷ 交易员保证金余额`，
    在**开仓那一刻**冻结；同一批/同一波连开**共用第一次读到的读数**，避免
    "开一笔占一份保证金 → 下一笔比例变小"的递减。"""
    now = time.time()
    if _BAL_CACHE["v"] > 0 and now - _BAL_CACHE["ts"] < _BAL_CACHE_TTL:
        return float(_BAL_CACHE["v"])
    v = -1.0
    eq = 0.0
    try:
        acc = exchange.get_account_info()
        if acc:
            v = float(acc.get("available_balance") or 0)
            eq = float(acc.get("equity") or 0)
        if v <= 0:
            v = float(exchange.get_balance() or 0)
    except Exception as e:
        print(f"[af-live] 读可用余额失败（本轮算不出比例，跳过）: {e}")
        return -1.0
    if v > 0:
        _BAL_CACHE.update({"ts": now, "v": v, "eq": eq})
    return v


def account_equity_cached() -> float:
    """账户**权益**（含未实现盈亏；读不到 → 返回 0，调用方回退可用余额）。

    只在"判这笔交易所持仓像不像我们开的"时用（认领判定 / 快速跟随闸门）：
    待判的那笔仓**本身已经把保证金占掉了**，拿可用余额当分子会把参考量压小
    → 仓越大（越可能是我们自己的残留仓）越判"不像"，正好把要认领的挡在门外
    （用户 2026-09-18 指出）。权益**不依赖被判仓的大小**，是稳定上界。

    能不能开仓/加仓仍然看**可用余额**（那是真能花的钱）——两个口径别混。"""
    now = time.time()
    if _BAL_CACHE["ts"] > 0 and now - _BAL_CACHE["ts"] < _BAL_CACHE_TTL:
        eq = float(_BAL_CACHE.get("eq") or 0)
        if eq > 0:
            return eq
    available_balance_cached()          # 顺带刷新（同一次账户读，不额外打接口）
    return float(_BAL_CACHE.get("eq") or 0)


def _trader_qty_of(pp: dict) -> float:
    """交易员**当前持仓量** —— 从纸盘行反推（纸盘持仓量 = 交易员量 × k_qty）。

    纸盘侧每个信号都按 `cv.qty × k_qty` 对齐，所以这与快照里的 absQty 等价，
    且**不需要额外请求平台**。k_qty 缺失（历史/外部写入的行）→ 返回 0（调用方跳过）。

    必须用**净持仓**（quantity − closed_qty）：系统口径里 `quantity` 是累计总量，
    减仓只累加 `closed_qty`。用累计量会把交易员减仓看成没减 → 目标量永不缩小
    → 实盘跟着不减仓（2026-09-17 走查抓到）。"""
    try:
        kq = float((json.loads(pp.get("signal_snapshot") or "{}") or {})
                   .get("k_qty") or 0)
    except (TypeError, ValueError):
        kq = 0.0
    if kq <= 0:
        return 0.0
    net = (float(pp.get("quantity") or 0)
           - float(pp.get("closed_qty") or 0))
    return max(net, 0.0) / kq


def _trader_margin_of(pp: dict) -> float:
    """交易员**保证金余额**（纸盘侧开仓/对齐时存进 signal_snapshot.trader_margin）。

    k = 我的可用余额 ÷ 这个值；快照里拿不到 → 0（调用方跳过或按现状定 k）。"""
    try:
        return float((json.loads(pp.get("signal_snapshot") or "{}") or {})
                     .get("trader_margin") or 0)
    except (TypeError, ValueError):
        return 0.0


def _pos_ratio(lc, live: dict, pp: dict) -> float:
    """该仓**开仓时冻结**的比例 k =（当时我的可用余额 ÷ 交易员保证金余额）。

    老记录（份数模型写的、没有 k）→ 按"记录量 ÷ 交易员当前量"反推一次并冻结：
    这样目标量 == 现状 → **零下单**，之后改设置/价格波动都不影响它。
    拿不到（纸盘缺 k_qty / 记录量为 0）→ 返回 0，调用方跳过对账。"""
    orders = _load_fresh_orders(lc, live) if live else {}
    try:
        k = float(orders.get("k") or 0)
    except (TypeError, ValueError):
        k = 0.0
    if k > 0:
        return k
    if not live:
        return 0.0
    tq = _trader_qty_of(pp)
    q = float(live.get("quantity") or 0)
    if tq <= 0 or q <= 0:
        return 0.0
    k = q / tq
    orders["k"] = round(k, 12)
    orders.setdefault("model", "ratio")
    storage.live_position_update(lc, live["id"], {
        "exchange_orders_json": json.dumps(orders)})
    print(f"[af-live] {live.get('token')} 老记录补写比例 k={k:.8g}"
          f"（= 记录量/交易员量，目标==现状，零下单）")
    return k


def _target_qty_raw(pp: dict, k: float) -> float:
    """**量化前**的目标量 = 交易员当前量 × k。

    专门给"低于交易所最小下单量"这类判断用：`_round_qty` 会把不足 minQty 的量
    **抬到 minQty**（不是抹成 0），所以拿量化后的目标去比 `target < minQty`
    永远为假 —— 而抬上去的结果可能是预期量的几十上百倍（映射比例 α 越小越夸张）。"""
    if k <= 0:
        return 0.0
    return max(_trader_qty_of(pp) * k, 0.0)


def _target_qty(token: str, pp: dict, k: float) -> float:
    """我们的目标量 = 交易员当前量 × k（按 stepSize 量化）"""
    tq = _trader_qty_of(pp)
    if tq <= 0 or k <= 0:
        return 0.0
    return _round_qty_ex(token, tq * k)


def _funds_check(token: str, mark: float, target_qty: float, lev: float,
                 max_notional: float, cur_qty: float = 0.0) -> tuple:
    """加仓前的两条约束（用户 2026-09-18 定的口径）：

    ① **本次新增**的保证金 ≤ 我的可用余额。用户例子：本金 200U、5x，交易员每加
       10w 我们加 200U（占用 40U），可用余额 200 → 160 → 120 → 80 → 40 → 0，
       直到不够下一次的 40U 为止 → 只跳过加仓，减仓/平仓照跟。
       注意**不能**把本仓/其他仓已占的保证金再加进来 —— 币安的"可用余额"口径
       已经把全部已用保证金扣掉了，加回来就是双重计算（会比该停的时候早停）。
    ② 单笔上限：**本次下单**的名义 ≤ max_notional（留空/0 = 不限）。

    cur_qty = 交易所该币当前持仓量（我们记录在案的），用于算"本次新增"。
    返回 (放行?, 原因)。"""
    tq = float(target_qty or 0)
    if tq <= 0 or mark <= 0:
        return True, ""
    add_qty = max(tq - float(cur_qty or 0), 0.0)
    if add_qty <= 0:
        return True, ""                 # 本来就不需要加
    lev = max(float(lev or 1), 1)
    add_notional = add_qty * mark
    if max_notional and max_notional > 0 and add_notional > max_notional * 1.000001:
        return False, (f"单笔上限 {max_notional:.6g}U：本次下单 "
                       f"{add_notional:.2f}U 超限")
    bal = available_balance_cached()
    if bal < 0:
        # 读不到余额（接口故障）：不阻断 —— 交易所是最后一道闸（保证金不够会拒单，
        # 下轮重试），而按 0 处理会把它当成"余额用光了"永远加不了仓
        return True, ""
    need = add_notional / lev
    if need > bal + 1e-9:
        return False, (f"可用余额 {bal:.2f}U 不够：本次需保证金 {need:.2f}U")
    return True, ""


def _mark_capped(lc, live, token: str, mark: float, target_qty: float,
                 why: str) -> None:
    """资金约束挡下一次开/加仓：记状态 + 写一次日志（状态没变不重复记 → 不刷日志）。

    live=None（新开被挡、还没有记录行）→ 只在日志留痕：每次 OPEN 信号本来就是
    一次独立事件（纸盘只在交易员真开新仓时发），不会每轮重复。"""
    detail = json.dumps({"reason": f"加仓跳过（{why}）——继续监控减仓/平仓"})
    if live is None:
        storage.live_trade_log_insert(lc, {
            "token": token, "strategy": "alphafox", "action": "ratio_capped",
            "price": mark, "quantity": target_qty,
            "details_json": json.dumps({"reason": f"开仓跳过（{why}）"})})
        print(f"[af-live] {token} 开仓跳过：{why}（继续监控）")
        return
    orders = _load_orders(live)
    if orders.get("capped") == why:
        return                                  # 同一个原因 → 不重复记
    orders["capped"] = why
    storage.live_position_update(lc, live["id"], {
        "exchange_orders_json": json.dumps(orders)})
    storage.live_trade_log_insert(lc, {
        "token": token, "strategy": "alphafox", "action": "ratio_capped",
        "price": mark, "quantity": target_qty,
        "paper_position_id": live.get("paper_position_id"),
        "details_json": detail})
    print(f"[af-live] {token} 加仓跳过：{why}（继续监控减/平）")


def _implied_live_qty(token: str, side: str) -> float:
    """这笔纸盘仓**最多可能镜像过的实盘量**（量化前）——给"实盘记录丢了、要不要兜底
    平仓"当参考量用：`_looks_like_ours` 的 ref_qty 传 0 会退化成"只按杠杆判" →
    **同向同杠杆的用户手动仓会被误判成我们的、被平掉**。

    用纸盘行的**总数量**（quantity）反推，**不是净持仓**：交易员平仓走到这条分支时
    纸盘那笔已经 CLOSE（净量 = quantity − closed_qty = 0），用净量会得到 0、
    判据又退化成只看杠杆。总量是"我们最多镜像过多少"的上界，正合适当参考量。
    用**权益**口径（待判的那笔仓自己占着保证金，用可用余额会把参考量压小）。
    算不出（没有纸盘行 / 缺 k_qty / 读不到权益）→ 返回 0（退回只按杠杆判）。"""
    try:
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM trade_positions WHERE token=? AND side=? "
                "AND strategy='alphafox' ORDER BY id DESC LIMIT 1",
                (token, side)).fetchone()
    except Exception:
        return 0.0
    if not row:
        return 0.0
    pp = dict(row)
    try:
        kq = float((json.loads(pp.get("signal_snapshot") or "{}") or {})
                   .get("k_qty") or 0)
    except (TypeError, ValueError):
        kq = 0.0
    qty = float(pp.get("quantity") or 0)          # 总，不是净
    mg = _trader_margin_of(pp)
    eq = account_equity_cached()
    if qty <= 0 or kq <= 0 or mg <= 0 or eq <= 0:
        return 0.0
    return max((qty / kq) * (_af_live_ratio() * eq / mg), 0.0)


def _realized_delta(live: dict, close_price: float, qty: float) -> float:
    """平掉 qty 的已实现盈亏（方向感知）。

    减仓路径必须自己算这一段：页面的"实盘累计盈亏/胜率"读的是记录的
    `realized_pnl`，而 `_close_exchange` 只在**全平**时按"剩余量"算一次 ——
    部分减仓那段盈亏不记账就会永久丢失（用户 2026-09-18 走查抓到）。
    价格/数量缺失 → 0（宁少算，不编造）。"""
    entry = float(live.get("entry_price") or 0)
    if entry <= 0 or float(close_price or 0) <= 0 or float(qty or 0) <= 0:
        return 0.0
    px = float(close_price)
    q = float(qty)
    if (live.get("side") or "LONG") == "SHORT":
        return (entry - px) * q
    return (px - entry) * q


def _managed_of(pos: dict) -> int:
    """读托管状态（老行无值 → 视为托管中，与纸盘层同语义）"""
    v = pos.get("managed")
    return MANAGED_ON if v is None else int(v)


def _live_rows(lc, token: str) -> list[dict]:
    return [dict(r) for r in lc.execute(
        "SELECT * FROM live_positions WHERE token=? AND strategy='alphafox' "
        "AND status IN ('OPEN','PARTIAL') ORDER BY id DESC",
        (token,)).fetchall()]


def _write_live_open(lc, paper_position_id: int, values: dict) -> None:
    """写 live 持仓记录（重开语义）。同 paper_position_id 已有行（含 CLOSED
    残留）→ 复用更新：paper_position_id 是 UNIQUE，直接 INSERT 会被
    storage.live_position_insert 的 OR IGNORE 静默丢弃 → 记录写不进去，
    下轮对账又开一份，实盘仓位无限膨胀。"""
    row = lc.execute("SELECT id FROM live_positions WHERE paper_position_id=?",
                     (paper_position_id,)).fetchone()
    vals = dict(values)
    if row:
        # 复用旧行：时间戳也要重置，否则"刚开仓 90s 保护"失效（旧 created_at
        # 会让对账以为仓位已存在很久，行情 API 抖动时会误判持仓消失）。
        # **realized_pnl 不清零**：同一纸盘仓可能"平掉→交易员又加回来→再平"，
        # 清掉会让上一段的真实盈亏从统计里消失（_close_exchange 是累加的）
        vals.update({"closed_at": None, "created_at": "__CURRENT_TIMESTAMP__"})
        storage.live_position_update(lc, row["id"], vals)
    else:
        storage.live_position_insert(
            lc, {"paper_position_id": paper_position_id, **vals})
    # 实盘这一腿建起来了 / 被认领了 → 清掉"没跟上"的原因（页面提示随之消失）。
    # 放在这里而不是各调用点：所有建记录的路径（信号开仓、补开、认领）都过这个函数
    _clear_skip(vals.get("token") or "", vals.get("side") or "LONG")


def _looks_like_ours(ex_row: dict, ref_qty: float, ref_leverage) -> bool:
    """交易所持仓是否像我们的镜像仓（防把用户手动仓认领/平仓）：

    **杠杆一致（差 ≤0.5）且 数量 ≤ 参考量×1.25**。

    2026-09-17 比例模型：原来的"名义≈整数倍单份名义(±25%)"依赖已作废的
    「每份名义」设置 → 换成"与参考量（我们记录的数量 / 预期目标量）比"，
    作用不变（混了手动仓时数量明显更大 → 判"不像"）。"""
    if not ex_row:
        return False
    try:
        qty = abs(float(ex_row.get("quantity") or 0))
        lev = float(ex_row.get("leverage") or 0)
        ref_lev = float(ref_leverage or 0)
        ref_qty = float(ref_qty or 0)
    except (TypeError, ValueError):
        return False
    if qty <= 0:
        return False
    if lev > 0 and ref_lev > 0 and abs(lev - ref_lev) > 0.5:
        return False
    if ref_qty <= 0:
        return True          # 没有参考量（如首次开仓前）→ 只按杠杆判
    return qty <= ref_qty * 1.25 + _QTY_MIN_DIFF


def _foreign_exposure(ex_positions, token: str, side: str,
                      ref_qty: float, ref_leverage,
                      any_side: bool = False) -> dict | None:
    """交易所该币已有"不是我们的"持仓 → 返回它（镜像跳过，纸盘照跑）。
    是我们形态的返回 None。any_side=True 时任意方向都算：首次开仓会先
    set_leverage（币安杠杆按交易对生效）→ 会改动该币手动仓的强平价。"""
    if not ex_positions:
        return None
    for s in (("LONG", "SHORT") if any_side else (side,)):
        ex_row = _ex_match(ex_positions, token, s)
        if ex_row and not _looks_like_ours(ex_row, ref_qty, ref_leverage):
            return ex_row
    return None


# ---- 互斥 ----

def alphafox_occupied(token: str) -> bool:
    """跟单是否占用该币实盘（live 记录活跃）"""
    try:
        storage.init_onchain_live_db()
        with storage.get_conn(config.LIVE_DB_PATH) as lc:
            row = lc.execute(
                "SELECT 1 FROM live_positions WHERE token=? AND strategy='alphafox' "
                "AND status IN ('OPEN','PARTIAL') LIMIT 1",
                (token.upper(),)).fetchone()
            return row is not None
    except Exception:
        return False


def _key_of(token: str, side: str) -> str:
    return f"{token}|{side}"


def _exempt_keys() -> set:
    """实盘开关 0→1 时记录的存量纸盘仓豁免清单（token|side#纸盘仓id 键）。
    豁免仓的所有动作（加/减/平）实盘都不处理，只镜像开关开启后全新开的仓。
    带 id 的意义：新仓 id 不同 → 天然不继承豁免；旧格式（不带 id）的键不会被
    匹配到，会在下一轮对账随 active_keys 交集被自动剪掉。"""
    try:
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM trading_settings "
                "WHERE key='alphafox_live_exempt_paper_ids'").fetchone()
        if row and row["value"]:
            return set(json.loads(row["value"]))
    except Exception:
        pass
    return set()


def _exempt_write(keys: set) -> None:
    try:
        with storage.get_conn() as conn:
            storage.trading_settings_update(
                conn, {"alphafox_live_exempt_paper_ids":
                       json.dumps(sorted(keys))})
    except Exception:
        pass


def _exempt_key_of(pos: dict) -> str:
    """豁免键：token|side#纸盘仓id。带 id → 新仓（新 id）天然不继承豁免，
    陈旧键也不会误伤重开的仓位"""
    return f"{_key_of(pos['token'], pos.get('side') or 'LONG')}#{pos.get('id')}"


def _exempt_has(token: str, side: str) -> bool:
    """该 token+side 下是否有豁免仓（不关心具体是哪一笔）"""
    prefix = f"{_key_of(token, side)}#"
    return any(k.startswith(prefix) for k in _exempt_keys())


def _exempt_remove_token_side(token: str, side: str) -> None:
    """交易员平掉豁免仓 → 移除该 token+side 的全部豁免键（之后重开视为新仓）"""
    try:
        prefix = f"{_key_of(token, side)}#"
        exempt = _exempt_keys()
        remain = {k for k in exempt if not k.startswith(prefix)}
        if remain != exempt:
            _exempt_write(remain)
    except Exception:
        pass


def _supertrend_occupied(token: str) -> bool:
    """主流趋势是否占用该币实盘：live_mode=1 且（超势有活跃纸盘仓/挂单
    或超势 live 记录活跃）"""
    token = token.upper()
    try:
        with storage.get_conn(config.SUPERTREND_DB) as st_conn:
            row = st_conn.execute(
                "SELECT live_mode FROM supertrend_tokens WHERE token=? "
                "AND status='ACTIVE'", (token,)).fetchone()
        if not row or not row["live_mode"]:
            return False
        with storage.get_conn() as conn:
            act = conn.execute(
                "SELECT 1 FROM trade_positions WHERE token=? AND strategy='supertrend' "
                "AND status IN ('OPEN','PARTIAL','PENDING') LIMIT 1",
                (token,)).fetchone()
            if act:
                return True
        storage.init_onchain_live_db()
        with storage.get_conn(config.LIVE_DB_PATH) as lc:
            live = lc.execute(
                "SELECT 1 FROM live_positions WHERE token=? AND strategy='supertrend' "
                "AND status IN ('OPEN','PARTIAL','PENDING') LIMIT 1",
                (token,)).fetchone()
            return live is not None
    except Exception:
        return False


# ---- 执行动作 ----

def _market_open(token: str, side: str, qty: float, leverage: int) -> bool:
    if not exchange.set_leverage(f"{token}USDT", leverage):
        print(f"[af-live] {token} 设杠杆 {leverage}x 失败")
        return False
    result = (exchange.market_open_long(token, qty) if side == "LONG"
              else exchange.market_open_short(token, qty))
    return bool(result and result.get("status") in ("FILLED", "PARTIALLY_FILLED", "NEW"))


def _paper_mark(token: str, side: str) -> float:
    """纸盘该币该方向最新一行的现价（给"拿不到成交价"的场合做估算用）。
    1s 循环持续刷新 current_price；查不到 → 0（调用方别估算）。"""
    try:
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT current_price, entry_price FROM trade_positions "
                "WHERE token=? AND side=? AND strategy='alphafox' "
                "ORDER BY id DESC LIMIT 1", (token, side)).fetchone()
    except Exception:
        return 0.0
    if not row:
        return 0.0
    return float(row["current_price"] or row["entry_price"] or 0)


def _close_exchange(lc, token: str, live: dict, ex_positions, reason: str) -> bool:
    """平交易所持仓 + live CLOSED + realized 入账（方向感知）。
    ex_positions=None（API 不可用）→ 不动作不标记，下轮重试。
    只平我们记录的数量：交易所若混有手动仓（同币同向会合并），不越界。
    返回是否已收敛（调用方据此决定能否把该记录当"已消失"）。"""
    if ex_positions is None:
        print(f"[af-live] {token} 收敛推迟：交易所持仓查询不可用")
        return False
    side = live.get("side") or "LONG"
    close_price = None
    our_qty = float(live.get("quantity") or 0)
    closed_qty = our_qty
    ex_row = _ex_match(ex_positions, token, side)
    if ex_row:
        ex_qty = abs(float(ex_row.get("quantity") or 0))
        qty = min(ex_qty, our_qty) if our_qty > 0 else ex_qty
        if ex_qty > our_qty * 1.5 + _QTY_MIN_DIFF:
            print(f"[af-live] {token} 交易所持仓 {ex_qty:.6g} 远超镜像记录 "
                  f"{our_qty:.6g}，仅平我们的部分（可能有手动仓合并）")
        if qty > 0:
            pos_side = ex_row.get("position_side") or side
            try:
                r = exchange.close_position_market(token, qty, position_side=pos_side)
            except Exception as e:
                print(f"[af-live] {token} 实盘平仓失败（{reason}）: {e}")
                return False  # 不标 CLOSED，下轮重试
            if not r:
                print(f"[af-live] {token} 实盘平仓无返回（{reason}），"
                      f"记录不收敛，下轮重试")
                return False
            # 以实际成交量（executedQty）为准：只看"有没有返回"会把零成交/被拒
            # 的平仓单当成已平 → 记录 CLOSED 而实盘还在（假平仓+裸仓）
            filled = float(r.get("quantity") or 0)
            if filled <= 0:
                print(f"[af-live] {token} 实盘平仓零成交（{reason}）："
                      f"记录不收敛，下轮重试")
                return False
            if filled < qty * 0.999:
                # 部分成交：记录改为剩余量，下轮继续收敛（不标 CLOSED）
                left = max(our_qty - filled, 0)
                storage.live_position_update(lc, live["id"], {"quantity": left})
                print(f"[af-live] {token} 实盘平仓部分成交 {filled:.6g}/{qty:.6g}，"
                      f"记录余 {left:.6g}（{reason}），下轮继续")
                return False
            close_price = r.get("price") or ex_row.get("mark_price")
            closed_qty = filled
    else:
        # 交易所已无该方向持仓 → 这一笔是**别人平的**（用户在币安手动平/强平），
        # 我们没有任何下单返回值 → 拿不到真实成交价。按"纸盘最新现价"估算，
        # 别记 0：记 0 会让实盘口径的累计盈亏/胜率漏掉这一笔（纸盘侧照常记，
        # 两列会互相矛盾）。要拿真实成交价得查 userTrades，代价是额外接口调用
        # 且同币混有手动单时难匹配（用户 2026-09-14 选择估算）。
        close_price = _paper_mark(token, side)
        if close_price:
            print(f"[af-live] {token} 交易所已无持仓（{reason}）："
                  f"按纸盘现价 {close_price:.6g} 估算 realized")
    entry = float(live.get("entry_price") or 0)
    qty = closed_qty
    realized = 0.0
    if close_price and entry:
        realized = ((entry - close_price) if side == "SHORT"
                    else (close_price - entry)) * qty
    # 累加：同一纸盘仓可能"平掉 → 交易员又加回来 → 再平"，覆盖写会让上一段
    # 真实盈亏从统计里消失（页面"实盘累计盈亏"只读这一列）
    total = float(live.get("realized_pnl") or 0) + realized
    storage.live_position_update(lc, live["id"], {
        "status": "CLOSED",
        "closed_at": "__CURRENT_TIMESTAMP__",
        "realized_pnl": round(total, 6),
    })
    if realized:
        print(f"[af-live] {token} 本次平仓 realized={realized:+.4f}"
              f"（该记录累计 {total:+.4f}）")
    storage.live_trade_log_insert(lc, {
        "token": token, "strategy": "alphafox", "action": "close",
        "price": close_price, "quantity": qty,
        "paper_position_id": live.get("paper_position_id"),
        "details_json": json.dumps({"reason": reason}),
    })
    print(f"[af-live] {token} 实盘平仓 qty={qty} @ {close_price}（{reason}）")
    return True


def apply_signal(sig: dict, live_enabled: bool, max_notional: float,
                 live_leverage: float) -> None:
    """信号执行后的实盘镜像（web 探测线程调用）。

    **比例模型（2026-09-17）**：每仓规模 = 交易员持仓量 × k，k = 开仓那一刻
    "我的可用余额 ÷ 交易员保证金余额"（冻结在记录里）。开仓/加仓按目标量补齐，
    减仓按比例减（交易员 −50% 我们就 −50%），CLOSE 全平。
    OPEN/ADD 受 live_enabled 与互斥约束；CLOSE/REDUCE 始终跟随（退出保护）。
    max_notional = 单笔上限（0/空 = 不限），超限只跳过加仓、减仓照跟。"""
    action = sig.get("action")
    if action not in ("OPEN", "CLOSE", "ADD", "REDUCE", "LEV"):
        return
    if not sig.get("executed"):
        return  # 纸盘未执行（SKIP/失败）→ 实盘不动
    symbol = sig.get("symbol") or ""
    token = None
    try:
        import alphafox_trade
        token = alphafox_trade._token_of(symbol)
    except Exception:
        return
    if not token or not token.isalnum():
        return
    side = sig.get("side") or "LONG"
    lev = max(int(float(live_leverage or 1)), 1)
    storage.init_onchain_live_db()

    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        live_rows = _live_rows(lc, token)
        # 按 side 匹配：换向场景（平多开空）旧方向的 live 残留不能接新方向的信号
        live = next((r for r in live_rows
                     if (r.get("side") or "LONG") == side), None)
        ex_positions = _ex_positions()
        ex_row = (_ex_match(ex_positions, token, side)
                  if ex_positions is not None else None)
        # 残留 live 记录（其 paper_position_id 对应的纸盘仓已不活跃）→ 先收敛：
        # 否则交易员重开同币同向时，新纸盘仓的 OPEN/ADD 会被当成"加仓"接上旧记录
        # → 旧残留 + 新一份 叠加 → 实盘数量翻倍。
        # 仅对 OPEN/ADD 做：CLOSE/REDUCE 有自己的退出分支（避免重复平仓）
        if live is not None and action in ("OPEN", "ADD"):
            with storage.get_conn() as _conn:
                _still = _conn.execute(
                    "SELECT 1 FROM trade_positions WHERE id=? AND strategy='alphafox' "
                    "AND status IN ('OPEN','PARTIAL')",
                    (live.get("paper_position_id"),)).fetchone()
            if not _still:
                if not _close_exchange(lc, token, live, ex_positions,
                                       "对应纸盘仓已结束，残留记录收敛"):
                    # 没真平掉（平仓被拒/无返回/持仓查询不可用）→ 本轮不镜像：
                    # 继续开仓会在未平的仓上再叠一份（实盘双倍敞口）
                    return
                live = None
                # ex_row 是本轮开头取的快照，刚被上面平掉 → 必须作废：否则目标
                # 差额会减掉一个已经不存在的仓位，本轮白跳过（比例模型按目标量
                # 下单，旧模型按"份"增量下单时看不出来）
                ex_row = None

        # 每币覆盖的**杠杆**（不设置用全局）；比例 k 在开仓那一刻冻结进记录
        # （比例模型，2026-09-17）
        _eff_l = _eff_live_params(token, live_leverage)
        if live is not None:
            lev = max(int(float(live.get("leverage") or _eff_l or 1)), 1)
        else:
            lev = max(int(float(_eff_l or 1)), 1)

        if action in ("OPEN", "ADD") and not live_enabled:
            print(f"[af-live] {token} 实盘开关关闭：跳过新开/加仓镜像")
            return
        if action in ("OPEN", "ADD") and _supertrend_occupied(token):
            print(f"[af-live] {token} 被主流趋势实盘占用：跳过镜像（纸盘照跑）")
            return
        if action == "LEV":
            if live and ex_positions is not None:
                try:
                    if exchange.set_leverage(f"{token}USDT", lev):
                        # 记录也要跟上：否则对账的归属校验（杠杆比对）把自家仓
                        # 当成"非镜像仓"→ 减仓被永久跳过。
                        # 保证金占用由下一轮对账按新杠杆重算（margin_usd 随时可重算）
                        storage.live_position_update(lc, live["id"], {
                            "leverage": lev})
                except Exception as e:
                    print(f"[af-live] {token} 杠杆同步异常: {e}")
            return

        if action in ("OPEN", "ADD"):
            with storage.get_conn() as conn:
                ppos = conn.execute(
                    "SELECT * FROM trade_positions WHERE token=? AND strategy='alphafox' "
                    "AND status IN ('OPEN','PARTIAL') ORDER BY id DESC LIMIT 1",
                    (token,)).fetchone()
            if not ppos:
                return
            ppos = dict(ppos)
            if _managed_of(ppos) != MANAGED_ON:
                # 纸盘写入与实盘下单之间隔着若干网络调用：用户在这窗口里点了
                # "取消托管/取消跟单" → 不下单（与纸盘层守卫对称）
                print(f"[af-live] {token} 跳过镜像：该仓已取消托管")
                return
            # 存量豁免仓：加/减/平都不镜像（用户语义：开关前旧数据完全不处理，
            # 只跟开关后全新开的仓）
            if _exempt_key_of(ppos) in _exempt_keys():
                print(f"[af-live] {token} 豁免仓 {sig['action']} 不镜像（只跟新开仓）")
                return
            if not _fresh(ppos):
                print(f"[af-live] {token} 镜像跳过：纸盘持仓过旧")
                return
            # 一份的币安侧价格：交易所 mark 优先 → 信号价格 → 纸盘 current/entry
            lot_price = None
            if ex_row:
                lot_price = float(ex_row.get("mark_price") or 0)
            if not lot_price:
                lot_price = float(sig.get("price") or 0)
            if not lot_price:
                lot_price = float(ppos.get("current_price")
                                  or ppos.get("entry_price") or 0)
            if not lot_price or lot_price <= 0:
                return
            # 比例 k：已有仓用记录里冻结的；新仓用"我的可用余额 ÷ 交易员保证金余额"
            # 此刻算出并冻结（用户 2026-09-17 定的比例模型），再乘**全局映射比例
            # α**（用户 2026-09-18：只按 α 的资金量跟，用来控实盘风险）
            if live is not None:
                k = _pos_ratio(lc, live, ppos)
                bal_ref = margin_ref = 0.0
                alpha = 0.0
            else:
                tq0 = _trader_qty_of(ppos)
                bal_ref = available_balance_cached()
                margin_ref = float(sig.get("trader_margin") or 0)
                alpha = _af_live_ratio()
                k = (alpha * bal_ref / margin_ref) if (tq0 > 0 and bal_ref > 0
                                                       and margin_ref > 0) else 0.0
            if k <= 0:
                print(f"[af-live] {token} 跳过镜像：算不出比例 k"
                      f"（纸盘缺 k_qty / 读不到余额 / 交易员保证金未知）")
                return
            target_qty = _target_qty(token, ppos, k)
            if target_qty <= 0:
                return
            ex_qty = abs(float(ex_row.get("quantity") or 0)) if ex_row else 0.0
            lot_qty = target_qty - ex_qty
            if lot_qty <= _QTY_MIN_DIFF:
                # 交易所已到/超过目标（比如刚被对账补齐）→ 不下单，交给对账收敛
                print(f"[af-live] {token} 镜像跳过：交易所 "
                      f"{ex_qty:.6g} 已不低于目标 {target_qty:.6g}")
                return
            # 本次要下的量（**量化前**）低于交易所最小下单量 → 不下单：`_round_qty`
            # 会把它抬到 minQty 而不是抹成 0，α 越小越容易开出比目标大几个数量级的仓
            _raw_inc = _target_qty_raw(ppos, k) - ex_qty
            _mn = _min_qty_of(token)
            if _mn > 0 and _QTY_MIN_DIFF < _raw_inc < _mn:
                print(f"[af-live] {token} 跳过：本次要下 {_raw_inc:.6g} 低于交易所最小"
                      f"下单量 {_mn:.6g}（映射比例/交易员增量过小）")
                return
            # 该币已有"不是我们的"交易所持仓（用户手动仓等）→ 开仓会与其合并、
            # 还会把该币杠杆改成实盘设置 → 跳过实盘（纸盘照跑）
            if not live and _foreign_exposure(ex_positions, token, side,
                                              target_qty, lev,
                                              any_side=True):
                print(f"[af-live] {token} 交易所已有非镜像持仓：跳过实盘镜像（纸盘照跑）")
                return
            # 无实盘记录的存量仓（早于本次进程启动创建）→ 不镜像：发版/重启后
            # 只跟新开的仓，避免把历史纸盘仓一次性开成实盘仓。
            # 用户显式点过快速跟随的币放行（与对账路径同一条，见 _QF_EXPLICIT）
            if not live and _pre_startup(ppos) \
                    and _key_of(token, side) not in _QF_EXPLICIT:
                print(f"[af-live] {token} 存量仓（早于本次启动创建）："
                      f"{sig['action']} 不镜像（只跟新仓）")
                return
            # 资金约束（与对账路径同一条）：本次新增保证金 ≤ 可用余额、
            # 本次下单名义 ≤ 单笔上限。不满足 → 只跳过这次开/加仓（减仓/平仓照跟）
            _ok, _why = _funds_check(token, lot_price, target_qty, lev,
                                     max_notional, ex_qty)
            if not _ok:
                _mark_capped(lc, live, token, lot_price, target_qty, _why)
                return
            if not _market_open(token, side, lot_qty, lev):
                print(f"[af-live] {token} 实盘开仓失败: {exchange.get_last_error()}")
                return
            if live:
                # 加仓：按比例补齐到目标量。杠杆也写回：热改"实盘杠杆"后加仓会把
                # 交易所杠杆改成新值，记录不跟上 → 对账归属校验（杠杆比对）会
                # 把自家仓判成"非镜像仓" → 该减不减
                orders = _load_orders(live)
                orders.pop("capped", None)
                storage.live_position_update(lc, live["id"], {
                    "quantity": target_qty,
                    "leverage": lev,
                    "margin_usd": target_qty * lot_price / max(lev, 1),
                    "exchange_orders_json": json.dumps(orders),
                })
                storage.live_trade_log_insert(lc, {
                    "token": token, "strategy": "alphafox",
                    "action": "add", "price": lot_price, "quantity": lot_qty,
                    "paper_position_id": live.get("paper_position_id"),
                    "details_json": json.dumps(
                        {"reason": "跟单加仓（按比例 k 补齐）",
                         "target": target_qty}),
                })
                print(f"[af-live] {token} 实盘加仓 +{lot_qty:.6g} → {target_qty:.6g}"
                      f"（k={k:.8g}）")
            else:
                # 新开：按比例算出目标量并**冻结 k**（同纸盘 id 若已有 CLOSED
                # 残留行 → 复用更新）
                _write_live_open(lc, ppos["id"], {
                    "token": token, "symbol": f"{token}USDT", "side": side,
                    "strategy": "alphafox",
                    "entry_price": lot_price,
                    "quantity": target_qty,
                    "leverage": lev,
                    "margin_usd": target_qty * lot_price / max(lev, 1),
                    # 冻结比例 k（= 映射比例α × 我的可用余额 ÷ 交易员保证金余额，
                    # 此刻）。之后目标量只随"交易员持仓 × k"变：价格怎么动都不调仓，
                    # 交易员减 50% 我们就减 50%
                    "exchange_orders_json": json.dumps(
                        {"model": "ratio", "k": round(k, 12),
                         "alpha": round(alpha, 6),
                         "balance_ref": round(bal_ref, 8),
                         "trader_margin_ref": round(margin_ref, 8)}),
                    "tp1_done": 0, "tp2_done": 0, "status": "OPEN",
                })
                storage.live_trade_log_insert(lc, {
                    "token": token, "strategy": "alphafox", "action": "open",
                    "price": lot_price, "quantity": lot_qty,
                    "paper_position_id": ppos["id"],
                    "details_json": json.dumps(
                        {"reason": f"跟单开仓（比例 k={k:.8g} = "
                                   f"余额{bal_ref:.2f}U/交易员保证金"
                                   f"{margin_ref:.0f}U"
                                   + (f"，映射比例 {alpha:g}" if alpha < 1 else "")
                                   + "）"}),
                })
                print(f"[af-live] {token} 实盘开仓 {side} {lot_qty:.6g} @ "
                      f"{lot_price}（k={k:.8g}"
                      + (f" α={alpha:g}" if alpha < 1 else "") + "）")
            return

        # CLOSE / REDUCE：跟随退出（开关关闭也照常）
        if action == "CLOSE":
            if _exempt_has(token, side):
                # 豁免仓平仓：从清单移除 → 之后交易员重开视为新仓正常镜像。
                # 若已有 live 实盘（关开关再开场景）→ 照常跟随平仓
                _exempt_remove_token_side(token, side)
                if live:
                    _close_exchange(lc, token, live, ex_positions, "交易员平仓")
                else:
                    print(f"[af-live] {token} 豁免仓平仓：无实盘，豁免解除（重开视为新仓）")
                return
            if live:
                _close_exchange(lc, token, live, ex_positions, "交易员平仓")
            elif ex_positions is not None:
                # live 记录丢失（DB 清理/重建）→ 按交易所实际持仓兜底平。
                #
                # **参考量不能给 0**：给 0 会退化成"只看杠杆"，那么**同向同杠杆的
                # 用户手动仓会被当成我们的平掉**（用户 setup：他在币安手动开了
                # BTC 空单，同时跟单的交易员也在 BTC 做空但我们的实盘被闸门挡着没建
                # 过仓 → 交易员一平仓，这条兜底就把他的手动仓平了）。
                # 用这笔纸盘仓反推的**实盘目标量**（交易员量 × α × 权益/保证金，
                # 与认领判定同一条公式）当参考量
                ex_row = _ex_match(ex_positions, token, side)
                # 兜底平仓的形态判据也要用"每币生效值"（不是全局），否则每币覆盖过
                # 名义/杠杆的币会被判成"不像我们的"→ 记录丢失时不平、留裸仓
                if ex_row and _looks_like_ours(ex_row, _implied_live_qty(token, side),
                                               lev):
                    qty = abs(float(ex_row.get("quantity") or 0))
                    pos_side = ex_row.get("position_side") or side
                    try:
                        exchange.close_position_market(
                            token, qty, position_side=pos_side)
                        print(f"[af-live] {token} CLOSE 兜底平仓 qty={qty}（live 记录缺失）")
                    except Exception as e:
                        print(f"[af-live] {token} CLOSE 兜底平仓异常: {e}")
                elif ex_row:
                    print(f"[af-live] {token} CLOSE 无 live 记录且交易所持仓不像"
                          f"镜像仓，不兜底平仓（如需平仓请在实盘页手动操作）")
            return

        # REDUCE：减一份；纸盘已无活跃（减到零）→ 全平
        if action == "REDUCE":
            if not live:
                return
            with storage.get_conn() as conn:
                ppos = conn.execute(
                    "SELECT * FROM trade_positions WHERE token=? AND strategy='alphafox' "
                    "AND status IN ('OPEN','PARTIAL') ORDER BY id DESC LIMIT 1",
                    (token,)).fetchone()
            if not ppos:
                # 纸盘已全平（减到零时 _align_our_position 直接 CLOSED）→ 平实盘
                _close_exchange(lc, token, live, ex_positions, "交易员减仓至零")
                return
            # 必须转 dict：下面按比例减要读 signal_snapshot（sqlite3.Row 没有 .get）
            ppos = dict(ppos)
            # **按比例减**（用户 2026-09-17）：目标量 = 交易员当前量 × k（冻结在该仓），
            # 平掉差额（上限不超过交易所持仓；混有手动仓时不越界）。
            # 例：交易员 1 枚 → 0.5 枚，我们 0.002 → 减 0.001
            orders = _load_orders(live)
            k = _pos_ratio(lc, live, ppos)
            target_qty = _target_qty(token, ppos, k) if k > 0 else 0.0
            ex_qty = abs(float(ex_row.get("quantity") or 0)) if ex_row else 0.0
            if target_qty <= 0 or ex_qty <= 0:
                # 交易员减到零（纸盘会直接 CLOSED）或算不出目标 → 实盘全平
                print(f"[af-live] {token} 目标归零 → 实盘全平")
                _close_exchange(lc, token, live, ex_positions, "交易员减仓至零")
                return
            # 该减多少 = **我们自己那一份**（记录量 − 目标量），再受"交易所现在有多少"
            # 约束。不能直接减到 ex−target：交易所可能混着你手动开的仓（同币同向会
            # 合并成一个持仓），按总量减会把你的手动仓一起平掉（真金白银）
            our_qty = float(live.get("quantity") or 0)
            close_qty = min(max(ex_qty - target_qty, 0.0),
                            max(our_qty - target_qty, 0.0))
            if close_qty <= _QTY_MIN_DIFF:
                return      # 已在目标（或低于最小下单量）→ 不下单
            try:
                r = exchange.close_position_market(token, close_qty, position_side=side)
                if not r:
                    print(f"[af-live] {token} 实盘减仓无返回，本轮不改记账"
                          f"（下轮重试）")
                    return
                filled = float(r.get("quantity") or 0)
                if filled <= 0:
                    # 零成交（被拒/EXPIRED 但返回 200）：按委托量记账会让份数漂移
                    # 甚至把 live 记录标 CLOSED 而实盘还在（假平仓+裸仓）
                    print(f"[af-live] {token} 实盘减仓零成交，本轮不改记账（下轮重试）")
                    return
                _close_px = float((ex_row or {}).get("mark_price") or 0)
                if filled < close_qty * 0.999:
                    # 部分成交：按实际成交量记账，份数不动（下轮对账继续收敛）。
                    # 平掉那部分的已实现盈亏要累加（否则这一段永久丢失）
                    print(f"[af-live] {token} 实盘减仓部分成交 {filled:.6g}/"
                          f"{close_qty:.6g}，按实际量记账")
                    storage.live_position_update(lc, live["id"], {
                        "quantity": float(live.get("quantity") or 0) - filled,
                        "realized_pnl": round(float(live.get("realized_pnl") or 0)
                                              + _realized_delta(live, _close_px, filled), 6),
                    })
                    return
                storage.live_position_update(lc, live["id"], {
                    "quantity": target_qty,
                    "margin_usd": target_qty * _close_px / max(lev, 1),
                    "exchange_orders_json": json.dumps(orders),
                    "realized_pnl": round(float(live.get("realized_pnl") or 0)
                                          + _realized_delta(live, _close_px, filled), 6),
                })
                storage.live_trade_log_insert(lc, {
                    "token": token, "strategy": "alphafox",
                    "action": "reduce",
                    # ex_row 此刻必然非空（上面 ex_qty<=0 已 return），仍留兜底防改动踩空
                    "price": float((ex_row or {}).get("mark_price") or 0) or None,
                    "quantity": close_qty,
                    "paper_position_id": live.get("paper_position_id"),
                    "details_json": json.dumps(
                        {"reason": "跟单减仓（交易员减仓，按比例 k 跟减）",
                         "target": target_qty, "k": k}),
                })
                print(f"[af-live] {token} 实盘减仓 -{close_qty:.6g} → "
                      f"{target_qty:.6g}（k={k:.8g}）")
            except Exception as e:
                print(f"[af-live] {token} 实盘减仓异常: {e}")


def sync_alphafox_live(live_enabled: bool, max_notional: float,
                       live_leverage: float) -> None:
    """对账兜底（探测线程每轮 poll 末尾调用）：
    纸盘活跃仓 vs live 记录 vs 交易所。
    - 纸盘有、live 无 → 补开（冷窗+新鲜度+互斥+开关闸门）
    - live 有、纸盘无 → 平交易所收敛（退出保护，开关关闭也照常）
    - 数量对齐（目标量 = 交易员当前量 × 冻结比例 k）
    max_notional：**单笔上限**（0/空 = 不限）—— 加仓时超限只跳过加仓，
    减仓/平仓照跟（用户 2026-09-17 的语义）。"""
    global _rounds_done
    _rounds_done += 1
    try:
        with storage.get_conn() as conn:
            paper_rows = [dict(r) for r in conn.execute(
                "SELECT * FROM trade_positions WHERE strategy='alphafox' "
                "AND status IN ('OPEN','PARTIAL')")]
    except Exception:
        return
    # 同币同向不可能有两条活跃行：partial unique index
    # idx_trade_one_active_token 在库层就挡住了（alphafox 不在 supertrend 豁免之列）
    ex_positions = _ex_positions()
    if ex_positions is None:
        print("[af-live] 交易所持仓查询失败，本轮对账跳过")
        return
    try:
        storage.init_onchain_live_db()
        with storage.get_conn(config.LIVE_DB_PATH) as lc:
            live_all = [dict(r) for r in lc.execute(
                "SELECT * FROM live_positions WHERE strategy='alphafox' "
                "AND status IN ('OPEN','PARTIAL')")]
            # live 有、纸盘无 → 收敛（交易员平仓后纸盘已 CLOSED；按 token+side 键）
            paper_keys = {_key_of(r["token"], r.get("side") or "LONG")
                          for r in paper_rows}
            for lv in live_all:
                if _key_of(lv["token"], lv.get("side") or "LONG") not in paper_keys:
                    _close_exchange(lc, lv["token"], lv, ex_positions,
                                    "纸盘已无活跃仓")
            # 纸盘有、live 无 → 补开（闸门）；live 有 → 数量对齐（按 side 匹配）
            for pp in paper_rows:
                token = pp["token"]
                side = pp.get("side") or "LONG"
                if _managed_of(pp) != MANAGED_ON:
                    # 非托管（取消托管/恢复中/已脱离）：不下任何单、不做数量对齐，
                    # 只把实盘记录同步成交易所事实（用户手动平/加减仓后页面不撒谎）
                    _reconcile_unmanaged(lc, pp, ex_positions)
                    continue
                # 每币覆盖（不设置用全局）：只对"新开/补开"生效；已有仓的目标量
                # 在 _align_qty 里用记录冻结的比例 k 算（见 _pos_ratio）
                tok_l = _eff_live_params(token, live_leverage)
                token_live = [lv for lv in live_all
                              if lv["token"] == token
                              and (lv.get("side") or "LONG") == side]
                if token_live:
                    # 优先认领本纸盘仓的记录；只有残留行（交易所侧是我们的仓，
                    # 但记录还挂在旧纸盘 id 上）时改指到当前仓，否则对账会长期
                    # 拿错对象（份数/数量都按错的那行算）
                    lv = next((x for x in token_live
                               if x.get("paper_position_id") == pp["id"]),
                              token_live[0])
                    if lv.get("paper_position_id") != pp["id"]:
                        # paper_position_id 有 UNIQUE 约束：仅当该纸盘仓连 CLOSED
                        # 行都没有时才改指（有 CLOSED 行说明仍受"不自动重开"保护）
                        taken = lc.execute(
                            "SELECT 1 FROM live_positions WHERE paper_position_id=?"
                            " LIMIT 1", (pp["id"],)).fetchone()
                        if not taken:
                            print(f"[af-live] {token} 实盘记录改指当前纸盘仓 "
                                  f"#{lv.get('paper_position_id')} → #{pp['id']}")
                            storage.live_position_update(
                                lc, lv["id"], {"paper_position_id": pp["id"]})
                            lv["paper_position_id"] = pp["id"]
                    _align_qty(lc, token, pp, lv, ex_positions,
                               live_enabled, max_notional, tok_l)
                else:
                    _mirror_missing(lc, token, pp, ex_positions, live_enabled,
                                    max_notional, tok_l)
            # 豁免清单维护：豁免 key 的仓已平（CLOSE 信号丢失场景兜底）→ 移除
            exempt = _exempt_keys()
            if exempt:
                active_keys = {_exempt_key_of(r) for r in paper_rows}
                remaining = exempt & active_keys
                if remaining != exempt:
                    _exempt_write(remaining)
    except Exception as e:
        print(f"[af-live] 对账异常: {e}")


def _align_qty(lc, token: str, pp: dict, live: dict, ex_positions,
               live_enabled: bool, max_notional: float,
               live_leverage: float) -> None:
    """对齐：目标量 = 交易员当前量 × 冻结比例 k（比例模型）。加仓受"占用保证金
    ≤ 可用余额"与"单笔上限"约束，超限只跳过加仓、减仓照跟。"""
    side = pp.get("side") or "LONG"
    if _exempt_has(token, side):
        return  # 豁免仓：实盘不处理（仅 CLOSE 跟随解除）
    ex_row = _ex_match(ex_positions, token, side)
    if not ex_row:
        # 交易所持仓消失（手动平仓/强平）→ 实盘记录 + **纸盘这一笔一起收尾**。
        # 用户语义（2026-09-14 确认）：手动平掉这一笔 = 这笔不再跟单，交易员之后
        # 再加减仓都不跟（等交易员开新的一笔）。只收敛实盘记录、纸盘继续挂托管的话，
        # 交易员加仓会把纸盘越加越大，而这边每轮都跳过重开 → 两边永久分叉。
        # 刚建仓 <90s 视为行情 API 延迟，不动。
        created = _parse_ts(live.get("created_at"))
        if created and (_now_utc() - created).total_seconds() < 90:
            return
        print(f"[af-live] {token} 交易所持仓消失（手动平仓/强平？）→ 实盘记录 + "
              f"纸盘这一笔一起收尾（之后不再跟这笔，等交易员开新仓）")
        # 估算已实现盈亏（按纸盘现价）——不留 0 的话，页面"实盘累计盈亏/胜率"
        # 会把这类平仓当成 0 盈亏的胜负判定
        _e = float(live.get("entry_price") or 0)
        _q = float(live.get("quantity") or 0)
        _px = float(pp.get("current_price") or 0)
        _r = (((_e - _px) if side == "SHORT" else (_px - _e)) * _q
              if (_e > 0 and _px > 0) else 0.0)
        storage.live_position_update(lc, live["id"], {
            "status": "CLOSED", "closed_at": "__CURRENT_TIMESTAMP__",
            "realized_pnl": round(float(live.get("realized_pnl") or 0) + _r, 6),
        })
        if _r:
            print(f"[af-live] {token} 记录收敛 realized≈{_r:+.4f}（按纸盘现价估算）")
        _close_paper_leg(token, side, "实盘持仓消失（手动平仓/强平），不再跟这一笔")
        return
    mark = float(ex_row.get("mark_price") or 0)
    if mark <= 0:
        return
    orders = _load_orders(live)
    # 目标量 = 交易员当前量 × **开仓时冻结的比例 k**（比例模型，用户 2026-09-17 定）：
    # 交易员减 50% 我们就减 50%；加仓/减仓/平仓全部按同一条公式，不再有"份数"概念。
    # k 由开仓时的"我的可用余额 ÷ 交易员保证金余额"算出，之后不再重算（价格、
    # 余额怎么变都不影响已有仓）；老记录按"记录量/交易员量"反推一次。
    k = _pos_ratio(lc, live, pp)
    if k <= 0:
        return          # 纸盘缺 k_qty（无法反推交易员量）→ 本轮不动
    # 目标按交易所 stepSize 量化后再比对：否则"目标非 step 整数倍"会导致
    # 每轮差半个 step → round 后被抬到 minQty → 反复反向成交（振荡+手续费流失）
    target_qty = _target_qty(token, pp, k)
    if target_qty <= 0:
        return
    ex_qty = abs(float(ex_row.get("quantity") or 0))
    diff = target_qty - ex_qty
    if abs(diff) <= _QTY_MIN_DIFF:
        # 已到目标（量化后）→ 仅把记录对齐到真实值
        if abs(float(live.get("quantity") or 0) - target_qty) > _QTY_MIN_DIFF:
            storage.live_position_update(lc, live["id"], {"quantity": target_qty})
        return
    # 容忍带（比例模型，2026-09-17）：目标量现在只随"交易员持仓 × k"变，不再随
    # 现价漂 → 带子只需要挡住三类噪声：
    #   ① 交易所对我们下单量的取整（≤1 个 step）→ 下限 1.5×step
    #   ② 比交易所最小下单量还小的差额 → 下限 minQty（否则会被抬到 minQty 超买）
    #   ③ 极小的成交流（<2% 目标）→ 交给手续费/噪音，不值得为它下单
    band = _REBAL_MIN_RATIO * target_qty
    _step = _step_of(token)
    if _step > 0:
        band = max(band, 1.5 * _step)
    _minq = _min_qty_of(token)
    if _minq > 0:
        band = max(band, _minq)
    if abs(diff) < band:
        # 容忍带内（价格微小波动）→ 不动交易所，记录对齐到真实持仓
        if abs(float(live.get("quantity") or 0) - ex_qty) > _QTY_MIN_DIFF:
            storage.live_position_update(lc, live["id"], {"quantity": ex_qty})
        return
    if diff > _QTY_MIN_DIFF:
        if not live_enabled:
            print(f"[af-live] {token} 对账补仓跳过：实盘开关关闭")
            return
        # 防"加在别人的仓上"：交易所量明显多于我们的记录 → 同币同向已混入手动仓，
        # 再补会把它喂得更大。注意**不能**用 _foreign_exposure 判形态：补仓路径
        # 本来就要容忍非整数倍的仓（部分成交/记录与交易所不一致时的自愈），
        # 用它会把正常的对账补仓一起挡死（测试 28 覆盖）
        our_qty = float(live.get("quantity") or 0)
        if our_qty > 0 and ex_qty > our_qty * 1.25 + _QTY_MIN_DIFF:
            print(f"[af-live] {token} 对账补仓跳过：交易所持仓 {ex_qty:.6g} 多于记录 "
                  f"{our_qty:.6g}（可能混有手动仓），不越界加仓")
            return
        lev = max(int(float(live_leverage or 1)), 1)
        # 资金约束（用户 2026-09-18）：本次新增保证金 ≤ 可用余额、本次下单名义 ≤
        # 单笔上限。不满足 → **只跳过这次加仓**，减仓/平仓照跟（"加不动就不加"）
        _ok, _why = _funds_check(token, mark, target_qty, lev, max_notional, ex_qty)
        if not _ok:
            _mark_capped(lc, live, token, mark, target_qty, _why)
            return
        if _market_open(token, side, diff, lev):
            _o = _load_orders(live)
            _o.pop("capped", None)            # 加成功了 → 清掉"加不动"标记
            storage.live_position_update(lc, live["id"], {
                "quantity": target_qty,
                "margin_usd": target_qty * mark / lev,
                "exchange_orders_json": json.dumps(_o),
            })
            storage.live_trade_log_insert(lc, {
                "token": token, "strategy": "alphafox",
                "action": "rebalance_add", "price": mark, "quantity": diff,
                "paper_position_id": live.get("paper_position_id"),
                "details_json": json.dumps(
                    {"reason": "对账补仓（按比例 k 对齐交易员仓位）",
                     "target": target_qty}),
            })
            print(f"[af-live] {token} 对账补仓 +{diff:.6g} → {target_qty}")
    else:
        # 归属校验（用户 2026-09-17 定的口径）：**杠杆一致 且 数量 ≤ 记录量×1.25**
        # 才认；否则视为交易所混了手动仓 → 不越界减仓（防误平你的仓）
        our_qty = float(live.get("quantity") or 0)
        ref_lev = float(live.get("leverage") or live_leverage or 0)
        if not _looks_like_ours(ex_row, our_qty, ref_lev):
            print(f"[af-live] {token} 对账减仓跳过：交易所持仓 {ex_qty:.6g} 与记录 "
                  f"{our_qty:.6g}/杠杆 {ref_lev:g} 不匹配（可能混有手动仓），不越界减仓")
            return
        lev = max(int(ref_lev or 1), 1)
        try:
            r = exchange.close_position_market(token, -diff, position_side=side)
            if not r:
                print(f"[af-live] {token} 对账减仓无返回，本轮不改记账")
                return
            # 平掉那部分的已实现盈亏要累加（与信号驱动的减仓同一条口径）：
            # 只按"剩余量"在全平时算一次的话，减仓这段的盈亏会永久丢失
            _fq = float(r.get("quantity") or 0) or abs(diff)
            storage.live_position_update(lc, live["id"], {
                "quantity": target_qty,
                "margin_usd": target_qty * mark / lev,
                "realized_pnl": round(float(live.get("realized_pnl") or 0)
                                      + _realized_delta(live, mark, _fq), 6),
            })
            storage.live_trade_log_insert(lc, {
                "token": token, "strategy": "alphafox",
                "action": "rebalance_reduce", "price": mark, "quantity": -diff,
                "paper_position_id": live.get("paper_position_id"),
                "details_json": json.dumps(
                    {"reason": "对账减仓（按比例 k 对齐交易员仓位）",
                     "target": target_qty}),
            })
            print(f"[af-live] {token} 对账减仓 {-diff:.6g} → {target_qty}")
        except Exception as e:
            print(f"[af-live] {token} 对账减仓异常: {e}")


def live_skip_reason(token: str, side: str) -> str:
    """该币实盘**最近一次没跟上的原因**（内存，供页面显示；空串 = 没有记录过）。

    只在 `_mirror_missing` 这条路径上写（纸盘有活跃仓、但没有实盘记录时每轮都跑），
    建起来 / 认领成功就清掉。页面据此提示"实盘未建立：<原因>"——否则用户只能翻日志
    才知道实盘那腿没跟上。"""
    v = _live_skip.get(_key_of(token, side))
    return v[0] if v else ""


def _note_skip(token: str, side: str, why: str) -> None:
    """记下"这一笔实盘为什么没建起来" + 打日志（一处都别漏，页面和日志同源）"""
    _live_skip[_key_of(token, side)] = (why, time.time())
    print(f"[af-live] {token} {why}")


def _clear_skip(token: str, side: str) -> None:
    _live_skip.pop(_key_of(token, side), None)


def _mirror_missing(lc, token: str, pp: dict, ex_positions, live_enabled: bool,
                    max_notional: float, live_leverage: float) -> None:
    """纸盘有活跃仓但 live 无记录 → 补开（多闸门）。

    **比例模型**：目标量 = 交易员当前量 × k；k 优先用记录冻结值，没有（老记录/
    记录丢失）→ 用"我的可用余额 ÷ 交易员保证金余额"现算（拿不到就按"交易所现状
    ÷ 交易员量"定 k，保证零下单）。

    每个"跳过"分支都走 `_note_skip`：原因同时进日志和内存（页面提示用）。"""
    global _rounds_done
    side = pp.get("side") or "LONG"
    if _rounds_done <= MIN_SYNC_ROUNDS:
        _note_skip(token, side, f"补开跳过：启动冷窗（第{_rounds_done}轮）")
        return
    if _exempt_key_of(pp) in _exempt_keys():
        _note_skip(token, side, "补开跳过：存量仓豁免（开关开启前的仓，只跟新开仓）")
        return
    # 该纸盘仓的实盘记录已平（此前镜像结束/交易所持仓曾消失）→ 不自动重开，
    # 防强平/手动平仓后每轮无脑重开；信号驱动的 OPEN/ADD 仍可重建
    if lc.execute("SELECT 1 FROM live_positions WHERE paper_position_id=? "
                  "AND status='CLOSED' LIMIT 1", (pp["id"],)).fetchone():
        _note_skip(token, side, "补开跳过：该纸盘仓的实盘记录已平（不自动重开）")
        return
    if not _fresh(pp):
        _note_skip(token, side, "补开跳过：纸盘持仓过旧")
        return
    if not live_enabled:
        _note_skip(token, side, "补开跳过：实盘开关关闭")
        return
    if _supertrend_occupied(token):
        _note_skip(token, side, "补开跳过：被主流趋势实盘占用")
        return
    mark = float(pp.get("current_price")
                 or pp.get("entry_price") or 0)
    if mark <= 0:
        _note_skip(token, side, "补开跳过：缺少标记价")
        return
    lev = max(int(float(live_leverage or 1)), 1)
    tq0 = _trader_qty_of(pp)
    bal0 = available_balance_cached()
    margin0 = _trader_margin_of(pp)
    # 新开一笔 → 乘全局映射比例 α（"只按 α 的资金量跟"）。
    # 下面"交易所已有我们的仓 → 认领"那条走"按现状冻结 k"，**不乘 α**：
    # 乘了会立刻把手里的仓砍成 α 倍（那是用户没要求的减仓）
    alpha = _af_live_ratio()
    k = (alpha * bal0 / margin0) if (bal0 > 0 and margin0 > 0 and tq0 > 0) else 0.0
    ex_row = _ex_match(ex_positions, token, side)
    if ex_row:
        # 交易所已有持仓：像我们的镜像仓（记录丢失）→ 补建记录不重复开；
        # 不像（用户手动仓/其他来源）→ 不认领、不开仓
        ex_qty = abs(float(ex_row.get("quantity") or 0))
        # 参考量用**权益**算（不是可用余额）：待判的这笔仓已占掉保证金，用可用会
        # 把参考量压小 → 越像自家残留仓的越判"不像"（用户 2026-09-18 指出）
        _eq0 = account_equity_cached()
        if _eq0 > 0 and margin0 > 0 and tq0 > 0:
            ref_qty = tq0 * (alpha * _eq0 / margin0)
        elif k > 0:
            ref_qty = tq0 * k      # 读不到权益 → 退回可用余额口径（改动前行为）
        else:
            ref_qty = ex_qty       # 算不出比例时以现状为参考（只按杠杆判形态）
        if not _looks_like_ours(ex_row, ref_qty, live_leverage):
            _note_skip(token, side, f"补开跳过：交易所已有非镜像持仓（{ex_qty:.6g}）")
            return
        # 比例以**现状**冻结：目标 == 交易所现有量 → 下一轮对账零下单。
        # （不能按余额重算：记录丢失期间交易所可能被手动加/减过仓，
        #   重算会立刻把它市价抹平）
        k = (ex_qty / tq0) if tq0 > 0 else 0.0
        if k <= 0:
            _note_skip(token, side, "补建跳过：纸盘缺 k_qty，量不出比例")
            return
        _write_live_open(lc, pp["id"], {
            "token": token, "symbol": f"{token}USDT", "side": side,
            "strategy": "alphafox",
            "entry_price": ex_row.get("entry_price"),
            "quantity": ex_qty,
            "leverage": lev,
            "margin_usd": ex_qty * float(ex_row.get("mark_price") or mark) / lev,
            "exchange_orders_json": json.dumps(
                {"model": "ratio", "k": round(k, 12), "rebuilt": True}),
            "tp1_done": 0, "tp2_done": 0, "status": "OPEN",
        })
        # 顺带报"这笔相当于几折跟"：现状比例 ÷（余额÷保证金）。>1 = 比映射比例
        # 该有的大（认领的是别人的/旧的仓），之后加仓也按这个现状比例走
        _ae = (k / (bal0 / margin0)) if (bal0 > 0 and margin0 > 0 and k > 0) else 0.0
        print(f"[af-live] {token} 补建 live 记录（交易所已有持仓 "
              f"{ex_qty:.6g}，比例按现状 k={k:.8g}，零下单"
              + (f"，相当于 α={_ae:.2f}" if _ae > 0 else "") + "）")
        return
    if k <= 0:
        _note_skip(token, side, "补开跳过：算不出比例 k"
                   f"（可用余额 {bal0:.2f}U / 交易员保证金 {margin0:.2f}U / "
                   f"纸盘 k_qty 缺失 → 未知）")
        return
    target_qty = _target_qty(token, pp, k)
    if target_qty <= 0:
        _note_skip(token, side, "补开跳过：目标量为 0")
        return
    # 目标量（**量化前**）低于交易所最小下单量 → 不下单（`_round_qty` 会把它
    # 抬到 minQty，开出比目标大几倍的仓；映射比例 α 小的时候尤其容易踩到）
    _raw0 = _target_qty_raw(pp, k)
    _mn = _min_qty_of(token)
    if _mn > 0 and _raw0 < _mn:
        _note_skip(token, side, f"补开跳过：目标量 {_raw0:.6g} 低于交易所最小"
                   f"下单量 {_mn:.6g}（映射比例/交易员仓位过小）")
        return
    if _foreign_exposure(ex_positions, token, side, target_qty,
                         live_leverage, any_side=True):
        # 同币反方向有手动仓：开仓前 set_leverage 会改动其强平价 → 不镜像
        _note_skip(token, side, "补开跳过：交易所该币存在非镜像持仓（反方向手动仓）")
        return
    # 存量仓闸门放在最后：只拦"真去交易所开新仓"，不拦上面"交易所已有我们持仓
    # → 补建 live 记录"的路径（否则记录丢失的孤儿仓永远没人对账/没人平）。
    # 用户显式点过快速跟随的币放行（见 _QF_EXPLICIT）
    if _pre_startup(pp) and _key_of(token, side) not in _QF_EXPLICIT:
        _note_skip(token, side, "补开跳过：该仓早于本次进程启动（发版/重启后只跟新仓）")
        return
    # 补开是全新一笔 → cur_qty=0（本次新增 = 全部目标量）
    _ok, _why = _funds_check(token, mark, target_qty, lev, max_notional, 0.0)
    if not _ok:
        _note_skip(token, side, f"补开跳过：{_why}")
        return
    if not _market_open(token, side, target_qty, lev):
        _note_skip(token, side, f"补开失败: {exchange.get_last_error()}")
        return
    _write_live_open(lc, pp["id"], {
        "token": token, "symbol": f"{token}USDT", "side": side,
        "strategy": "alphafox",
        "entry_price": mark,
        "quantity": target_qty,
        "leverage": lev,
        "margin_usd": target_qty * mark / lev,
        "exchange_orders_json": json.dumps(
            {"model": "ratio", "k": round(k, 12),
             "alpha": round(alpha, 6),
             "balance_ref": round(bal0, 8),
             "trader_margin_ref": round(margin0, 8)}),
        "tp1_done": 0, "tp2_done": 0, "status": "OPEN",
    })
    storage.live_trade_log_insert(lc, {
        "token": token, "strategy": "alphafox", "action": "open",
        "price": mark, "quantity": target_qty,
        "paper_position_id": pp["id"],
        "details_json": json.dumps(
            {"reason": f"对账补开（比例 k={k:.8g}"
                       + (f"，映射比例 {alpha:g}" if alpha < 1 else "") + "）"}),
    })
    print(f"[af-live] {token} 对账补开 {side} {target_qty:.6g}（k={k:.8g}"
          + (f" α={alpha:g}" if alpha < 1 else "") + "）")


def _reconcile_unmanaged(lc, pp: dict, ex_positions) -> None:
    """非托管仓的实盘侧：只做事实同步（记录 ← 交易所），**绝不下单**。
    - 交易所已无该仓（手动平/强平）→ 记录 + 纸盘仓一起收尾（页面不留幽灵）
    - 交易所数量与记录不符（手动加减）→ 记录数量对齐到交易所事实
    开关关闭也照常跑：这里不产生任何交易，只是让记录不撒谎。"""
    token = pp["token"]
    side = pp.get("side") or "LONG"
    live_rows = [lv for lv in _live_rows(lc, token)
                 if (lv.get("side") or "LONG") == side]
    if not live_rows or ex_positions is None:
        return   # 无记录无事可做；交易所查询不可用时不凭猜测收尾
    ex_row = _ex_match(ex_positions, token, side)
    for lv in live_rows:
        if ex_row is None:
            created = _parse_ts(lv.get("created_at"))
            if created and (_now_utc() - created).total_seconds() < 90:
                continue   # 刚建仓 90s 内视为行情 API 延迟，不误判
            _settle_unmanaged(lc, token, side)
        else:
            ex_qty = abs(float(ex_row.get("quantity") or 0))
            if abs(float(lv.get("quantity") or 0) - ex_qty) > _QTY_MIN_DIFF:
                storage.live_position_update(lc, lv["id"], {"quantity": ex_qty})
                print(f"[af-live] {token} 非托管仓：实盘数量同步为 {ex_qty:.6g}"
                      f"（记录 ← 交易所，不下单）")


def _close_paper_leg(token: str, side: str, advice: str) -> float | None:
    """把该币该方向**活跃的纸盘仓**按标记价收尾，返回用到的标记价（无仓 / 已平完
    返回 None）。用于"实盘那边的这一笔已经没了"的两条路：
      · 非托管仓收尾（`_settle_unmanaged`）
      · 托管中但交易所持仓消失（手动平仓/强平）→ 见 `_align_qty`

    用户语义（2026-09-14 确认）：**手动平掉这一笔 = 这笔不再跟单**，交易员之后
    再加减仓都不跟，要等交易员开新的一笔。所以这里连纸盘那一笔一起收尾 ——
    否则纸盘挂托管、交易员加仓把纸盘越加越大，而实盘被"已平记录不自动重开"
    闸门挡住 → 两边永久分叉（BUG 2 的现象）。
    realized 按标记价估算：拿不到手动平仓的真实成交价。"""
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM trade_positions WHERE token=? AND side=? "
            "AND strategy='alphafox' AND status IN ('OPEN','PARTIAL') "
            "ORDER BY id DESC LIMIT 1", (token, side)).fetchone()
        if not row:
            return None
        pos = dict(row)
        entry = float(pos.get("entry_price") or 0)
        qty = float(pos.get("quantity") or 0)
        open_qty = max(qty - float(pos.get("closed_qty") or 0), 0)
        if open_qty <= 0:
            return None
        mark = float(pos.get("current_price") or entry or 0)
        realized = float(pos.get("realized_pnl") or 0)
        if mark > 0 and entry > 0:
            realized += ((entry - mark) if side == "SHORT"
                         else (mark - entry)) * open_qty
        margin = float(pos.get("margin_amount") or 1)
        cur = conn.execute(
            "UPDATE trade_positions SET status='CLOSED', closed_qty=?, "
            "realized_pnl=?, unrealized_pnl=0, pnl_pct=?, advice=?, "
            "current_price=?, closed_at=CURRENT_TIMESTAMP, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=? "
            "AND status IN ('OPEN','PARTIAL')",
            (qty, realized, realized / margin * 100, advice, mark, pos["id"]))
        if cur.rowcount == 0:
            return None      # 并发被别人平了 → 不重复收尾（调用方也别按它算）
        # 这一笔结束了 → 连"实盘没跟上"的原因一起清掉：否则交易员之后重开同币时，
        # 新纸盘仓在下一轮对账跑到之前会把**上一笔**的原因显示出来
        _clear_skip(token, side)
        print(f"[af-live] {token} {side} 纸盘仓收尾（{advice}）"
              f"realized≈{realized:.4f} 估算")
        return mark


def _settle_unmanaged(lc, token: str, side: str) -> None:
    """非托管仓：交易所侧已无持仓 → 实盘记录 + 纸盘仓一起收尾（页面不留幽灵）。
    **重新读纸盘行判定托管状态**（不信任本轮开头的 paper_rows 快照）：用户可能在
    sync 中途点了"恢复托管"（managed=2），按旧快照收尾会把他的动作静默回滚。
    只对 managed 0/3 收尾。"""
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM trade_positions WHERE token=? AND side=? "
            "AND strategy='alphafox' AND status IN ('OPEN','PARTIAL') "
            "ORDER BY id DESC LIMIT 1", (token, side)).fetchone()
        if not row:
            return
        pos = dict(row)
        if _managed_of(pos) not in (MANAGED_OFF, MANAGED_UNLINKED):
            return   # 托管中(1)/恢复中(2)：不走"手动平仓"收尾
    mark = _close_paper_leg(token, side, "手动平仓（已取消托管）")
    if mark is None:
        return
    for lv in _live_rows(lc, token):
        if (lv.get("side") or "LONG") != side:
            continue
        l_entry = float(lv.get("entry_price") or 0)
        l_qty = float(lv.get("quantity") or 0)
        l_realized = 0.0
        if mark > 0 and l_entry > 0:
            l_realized = ((l_entry - mark) if side == "SHORT"
                          else (mark - l_entry)) * l_qty
        storage.live_position_update(lc, lv["id"], {
            "status": "CLOSED", "closed_at": "__CURRENT_TIMESTAMP__",
            "realized_pnl": round(l_realized, 6),
        })
        storage.live_trade_log_insert(lc, {
            "token": token, "strategy": "alphafox", "action": "close",
            "price": mark or None, "quantity": l_qty,
            "paper_position_id": lv.get("paper_position_id"),
            "details_json": json.dumps(
                {"reason": "非托管仓：交易所已无持仓，记录收尾（价格按标记价估算）"}),
        })
        print(f"[af-live] {token} 非托管仓：实盘记录收尾"
              f"（realized≈{l_realized:.4f} 估算）")


def on_reattach(token: str, side: str) -> bool:
    """恢复托管时实盘侧的准备（alphafox_trade._resolve_reattach 调用）：
    ① 解除豁免——实盘开关 0→1 时记下的豁免键会让该币此后永不镜像；
    ② 按**现状**把比例 k 冻结：k = 交易所当前量 ÷ 交易员当前量——未托管期间你在
       币安手动加减过仓的话，k 如果还是开仓时的旧值，下一轮对账会按旧目标市价把
       你的手动改动抹掉（真金白银）；按现状冻结后目标 == 现状 → 零下单。
    **绝不按纸盘名义重算**：纸盘量与实盘量刻意解耦（差 2~3 个数量级）。

    返回**能否落定**（调用方据此把仓转"托管中"）：交易所查询不可用 → False。
    观察不到现状就不冻结任何东西——否则状态已转托管、旧 k 继续生效，而对账只在
    "托管中"跑、"恢复中"的过滤条件再也进不来 → 旧目标把你手动加减的仓市价抹平。"""
    _exempt_remove_token_side(token, side)
    storage.init_onchain_live_db()
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        lv = next((r for r in _live_rows(lc, token)
                   if (r.get("side") or "LONG") == side), None)
        if not lv:
            return True     # 从未开过实盘 → 没有 k 要冻（下轮由补开路径新建）
        with storage.get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM trade_positions WHERE token=? AND side=? "
                "AND strategy='alphafox' AND status IN ('OPEN','PARTIAL') "
                "ORDER BY id DESC LIMIT 1", (token, side)).fetchone()
        if not row:
            return True     # 纸盘无活跃行（并发平仓）→ 实盘侧无事可做
        pp = dict(row)
        tq = _trader_qty_of(pp)
        # 优先交易所**现状**：未托管期间记录每轮都被事实同步，但"手动改仓 + 马上点
        # 恢复托管"会落在同一轮询窗口里（记录还是旧值）→ 用旧记录冻结会把手动改动
        # 在下一轮市价抹平
        ex_positions = _ex_positions()
        if ex_positions is None:
            return False    # ★ 交易所不可用 → 不落定（见 docstring）
        ex_row = _ex_match(ex_positions, token, side)
        k = 0.0
        if ex_row and tq > 0:
            ex_qty = abs(float(ex_row.get("quantity") or 0))
            if ex_qty > 0:
                k = ex_qty / tq
        if k <= 0:
            # 读取成功但没这笔（并发平仓/灰尘量）：没有"现状"可冻结 → 不写 k，
            # 交给下一轮对账的"交易所持仓消失"分支收尾（它会平纸盘这笔）
            return True
        orders = _load_fresh_orders(lc, lv)
        orders["model"] = "ratio"
        orders["k"] = round(k, 12)
        storage.live_position_update(lc, lv["id"], {
            "exchange_orders_json": json.dumps(orders)})
        print(f"[af-live] {token} 恢复托管：比例按现状冻结 k={k:.8g}"
              f"（目标==现状，不下单）")
        return True


def close_live(token: str) -> bool:
    """页面手动平仓（实盘页按钮）：关纸盘（按 mark 价结算）→
    平交易所 → live 收敛。仅对有 alphafox 活跃纸盘仓的币生效。"""
    import alphafox_trade
    token = token.upper()
    handled = False
    with storage.get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM trade_positions WHERE token=? AND strategy='alphafox' "
            "AND status IN ('OPEN','PARTIAL')", (token,)).fetchall()]
        if not rows:
            return False
        mark = alphafox_trade.get_mark_price(token)
        if not mark or mark <= 0:
            # 行情不可用 → 用纸盘现价/入场价兜底（否则纸盘平不掉，
            # 实盘页兜底却已平交易所 → 纸盘与实盘长期不一致）
            for pos in rows:
                mark = float(pos.get("current_price")
                             or pos.get("entry_price") or 0)
                if mark > 0:
                    break
        if not mark or mark <= 0:
            print(f"[af-live] {token} 手动平仓失败：缺少标记价")
            return False
        for pos in rows:
            entry = float(pos.get("entry_price") or 0)
            qty = float(pos.get("quantity") or 0)
            closed_qty = float(pos.get("closed_qty") or 0)
            open_qty = max(qty - closed_qty, 0)
            if open_qty <= 0:
                continue
            realized = float(pos.get("realized_pnl") or 0)
            if pos.get("side") == "SHORT":
                realized += (entry - mark) * open_qty
            else:
                realized += (mark - entry) * open_qty
            margin = float(pos.get("margin_amount") or 1)
            conn.execute(
                "UPDATE trade_positions SET status='CLOSED', current_price=?, "
                "closed_qty=?, realized_pnl=?, unrealized_pnl=0, "
                "pnl_pct=?, advice='手动平仓（实盘页）', closed_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status IN ('OPEN','PARTIAL')",
                (mark, qty, realized, realized / margin * 100, pos["id"]))
            handled = True
        if handled:
            print(f"[af-live] {token} 跟单纸盘手动平仓 @ {mark}")
    if handled:
        # 解除豁免键（必须在 with 块外：嵌在主库写事务里再开写连接会被锁住）
        for pos in rows:
            _exempt_remove_token_side(token, pos.get("side") or "LONG")
            # 与 _close_paper_leg 同理：这一笔结束了，"没跟上"的原因也别留着，
            # 否则交易员重开同币时会短暂显示上一笔的原因
            _clear_skip(token, pos.get("side") or "LONG")
    try:
        storage.init_onchain_live_db()
        with storage.get_conn(config.LIVE_DB_PATH) as lc:
            live_rows = _live_rows(lc, token)
            if live_rows:
                ex_positions = _ex_positions()
                # 逐个方向平（换向残留场景可能 LONG+SHORT 两条 live）
                for lv in live_rows:
                    _close_exchange(lc, token, lv, ex_positions, "手动平仓")
    except Exception as e:
        print(f"[af-live] {token} 手动平仓：实盘侧异常: {e}")
    return handled


# ---- 快速跟随（用户 2026-09-17 需求）----

def quick_follow_live(token: str, side: str, trader_qty: float,
                      max_notional: float = 0.0) -> dict:
    """页面「⚡ 快速跟随」的实盘侧：把该币实盘**纳入跟随**（不直接下单，
    由随后触发的一轮对账把交易所补到目标量）。

    闸门（用户定的 d 等）：
      · 交易所已有该币持仓、杠杆 ≠ 该币生效杠杆 → **失败**（记日志，不动它）
      · 交易所已有该币持仓、且"不像我们的" → 失败（防改到手动仓）
      · 币安无该币永续/无标记价 → 失败
    返回 {"ok": bool, "action": "新建"/"已跟"/"失败", "reason": str}
    """
    storage.init_onchain_live_db()
    with storage.get_conn() as conn:
        pp = conn.execute(
            "SELECT * FROM trade_positions WHERE token=? AND side=? "
            "AND strategy='alphafox' AND status IN ('OPEN','PARTIAL') "
            "ORDER BY id DESC LIMIT 1", (token, side)).fetchone()
    if not pp:
        return {"ok": False, "action": "失败", "reason": "纸盘无该币活跃仓"}
    pp = dict(pp)

    def _fail(reason: str) -> dict:
        try:
            with storage.get_conn(config.LIVE_DB_PATH) as lc:
                storage.live_trade_log_insert(lc, {
                    "token": token, "strategy": "alphafox",
                    "action": "quick_follow_blocked",
                    "paper_position_id": pp["id"],
                    "details_json": json.dumps({"reason": reason})})
        except Exception as e:
            print(f"[af-live] {token} 快速跟随失败日志写入异常: {e}")
        print(f"[af-live] {token} 快速跟随失败: {reason}")
        return {"ok": False, "action": "失败", "reason": reason}

    if not _af_live_enabled():
        # 实盘开关关着：纸盘照建（调用方已经建好了），实盘什么都不做。
        # 这里**不写 blocked 日志**——不是异常，是用户自己的开关状态；
        # 也不解豁免（关着的时候建出来的仓本来就该进豁免清单）
        return {"ok": False, "action": "跳过",
                "reason": "实盘开关关闭（开启后再点一次即可跟）"}
    if _supertrend_occupied(token):
        # 主流趋势占用该币实盘时对账会跳过镜像 —— 不在这里说清楚的话，用户会看到
        # "实盘: 新建"但实盘什么都没发生（对账那行只 print、不落日志 = 静默）
        return {"ok": False, "action": "跳过",
                "reason": "该币被主流趋势实盘占用（先处理它再跟随）"}
    ex_positions = _ex_positions()
    if ex_positions is None:
        return _fail("交易所查询不可用，稍后重试")
    lev = max(int(float(_eff_live_params(token, _af_global_leverage()) or 1)), 1)
    ex_row = _ex_match(ex_positions, token, side)
    if ex_row:
        ex_lev = float(ex_row.get("leverage") or 0)
        if ex_lev > 0 and abs(ex_lev - lev) > 0.5:
            return _fail(f"交易所该币杠杆 {ex_lev:g} ≠ 设置杠杆 {lev}（先处理它再跟随）")
        # "不像我们的"：参考量取"现在会开多少"——用**权益**（不是可用余额，理由同
        # 认领判定：这笔仓自己占着保证金，用可用会把参考量压小）；
        # 算不出（权益/保证金未知）→ 只按杠杆判（上面那道闸门）
        _tq = _trader_qty_of(pp)
        _eq = account_equity_cached()
        if _eq <= 0:
            _eq = available_balance_cached()    # 读不到权益 → 退回可用口径
        _mg = _trader_margin_of(pp)
        _ref = (_tq * _af_live_ratio() * _eq / _mg) \
            if (_tq > 0 and _eq > 0 and _mg > 0) else 0.0
        if not _looks_like_ours(ex_row, _ref, lev):
            return _fail("交易所该币已有非镜像持仓（可能手动仓），不认领")
    # 反方向有"不是我们的"持仓：对账补开会跳过（开仓前 set_leverage 会改它的强平价）
    # → 现在如实说清楚，别让页面报"新建"而实盘什么都不发生
    if _foreign_exposure(ex_positions, token, side, 0.0, lev, any_side=True):
        return {"ok": False, "action": "跳过",
                "reason": "交易所该币有反方向持仓（先处理它再跟随）"}
    # 闸门都过了（用户显式动作）：
    # ① 解豁免——存量仓豁免键会让该币此后永不镜像（"开关开启前的仓"），
    #    用户点了快速跟随 = 明确要求现在就跟这个币
    # ② 记进"显式清单"——存量仓闸门（发版/重启后只跟新仓）同样要放行：
    #    跟了一周的纸盘仓 + 刚开实盘开关，正是这个按钮的典型用法，
    #    被那个闸门挡住的话点了像没反应
    # ③ 冷窗立即放行（不等待 2 轮）
    _exempt_remove_token_side(token, side)
    _QF_EXPLICIT.add(_key_of(token, side))
    global _rounds_done
    _rounds_done = max(_rounds_done, MIN_SYNC_ROUNDS + 1)
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        lv = next((r for r in _live_rows(lc, token)
                   if (r.get("side") or "LONG") == side), None)
        if lv is None and ex_row is None:
            # 没有记录**且交易所也没仓** = 这次要真去开新的一笔：先把"会失败"的原因
            # 在这一步说清楚，否则页面报"新建"而实盘什么都不发生（对账那轮才挡下
            # 并记日志）。交易所有我们的仓时走的是**认领**（零下单、一分钱不用）——
            # 那种情况跑资金/最小量预检会误拦：可用余额被别的仓压干时认领照样成立，
            # 却被报成"实盘资金不够"（2026-09-29 CL 场景）
            _tq0 = _trader_qty_of(pp)
            _bal0 = available_balance_cached()
            _mg0 = _trader_margin_of(pp)
            if _tq0 <= 0 or _bal0 <= 0 or _mg0 <= 0:
                return {"ok": False, "action": "跳过",
                        "reason": "算不出比例（读不到余额 / 纸盘缺 k_qty 或交易员保证金）"}
            _mk0 = float(pp.get("current_price") or pp.get("entry_price") or 0)
            if _mk0 <= 0:
                return {"ok": False, "action": "跳过", "reason": "缺少标记价，稍后重试"}
            _k0 = _af_live_ratio() * _bal0 / _mg0
            _tgt0 = _target_qty_raw(pp, _k0)      # 量化前（量化会把不足 minQty 抬上去）
            _mn0 = _min_qty_of(token)
            if _tgt0 <= 0 or (_mn0 > 0 and _tgt0 < _mn0):
                # 目标量在交易所最小下单量之下：开不出（强开会被抬到 minQty
                # → 比 α 口径大好几倍），如实说清楚，别让页面报"新建"
                return {"ok": False, "action": "跳过",
                        "reason": f"目标量 {_tgt0:.6g} 低于交易所最小下单量"
                                  f"（映射比例/交易员仓位过小）"}
            _ok, _why = _funds_check(token, _mk0, _target_qty(token, pp, _k0),
                                     lev, 0.0, 0.0)
            if not _ok:
                return {"ok": False, "action": "跳过",
                        "reason": f"实盘资金不够：{_why}"}
        if lv:
            # 已有记录：用（或补写）它**开仓时冻结的** k —— 随后那轮对账会把
            # 交易所补到"交易员当前量 × k"（这正是快速跟随要的"补齐"）。
            # **不能**按交易所现状重冻（那是"恢复托管"的语义）：那样目标==现状，
            # 实盘永远停在旧规模上，点了等于没点
            k = _pos_ratio(lc, lv, pp)
            storage.live_position_update(lc, lv["id"], {
                "paper_position_id": pp["id"]})
            return {"ok": True, "action": "已跟", "k": k}
    # 交易所有该币持仓、但没有实盘记录 → 随后那轮对账会走**认领**（k 按现状冻结、
    # 零下单），不是"新建"。如实回报，页面才不会让人以为会新开一笔
    return {"ok": True, "action": "认领" if ex_row else "新建", "k": 0.0}


def _af_live_ratio() -> float:
    """全局**实盘映射比例** α（0.01~1，默认 1 = 完全按比例跟）。

    用户 2026-09-18：`目标量 = 交易员量 × k × α`，等价于"我只用 α 比例的资金
    跟他"。它和杠杆不是一回事——杠杆只改保证金占用（敞口不变、强平更近），
    α 是直接把敞口/保证金/盈亏**同步缩小**，这才是降风险。
    只在"冻结新 k"时读一次（开仓那一刻），之后记录里的 k 已经含着它。
    读不到/脏值 → 1.0（不因为配置表脏了就缩仓或爆仓）。"""
    try:
        with storage.get_conn() as conn:
            v = float(storage.trading_settings_get(conn)
                      .get("alphafox_live_ratio") or 1.0)
    except Exception:
        return 1.0
    return v if 0 < v <= 1 else 1.0


def _af_live_enabled() -> bool:
    """实盘开关（默认关：与探测线程同口径——设置缺失时绝不能当成"开着"）"""
    try:
        with storage.get_conn() as conn:
            return bool(storage.trading_settings_get(conn)
                        .get("alphafox_live_enabled", False))
    except Exception:
        return False


def _af_global_leverage() -> float:
    """全局实盘杠杆（设置项；读不到用 config 默认）"""
    try:
        with storage.get_conn() as conn:
            return float(storage.trading_settings_get(conn)
                         .get("alphafox_live_leverage")
                         or getattr(config, "ALPHAFOX_LIVE_LEVERAGE", 5.0))
    except Exception:
        return float(getattr(config, "ALPHAFOX_LIVE_LEVERAGE", 5.0))
