"""超级趋势实盘镜像层。

纸盘（trade_positions, strategy='supertrend'）仍是策略引擎，本层只做
纸盘 → 币安实盘 的对账式镜像：

- 每轮 sync：对每个 live_mode=1 的币做三方对账（纸盘 vs live 记录 vs 交易所）
- 补开/挂单镜像受双重闸门保护（启动冷窗 + 纸盘行新鲜度），平仓类不受限
- SL 由币安 STOP_MARKET 条件单托管（毫秒级触发），纸盘 1s 扫损照跑，时间差由对账收敛
- live 记录复用 onchain_live.db（strategy='supertrend'）
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import config
import exchange
import storage

SYNC_FRESH_MAX_AGE = 90      # 纸盘行 updated_at 新鲜度上限（秒）：引擎活着时 1s 循环持续刷新
MIN_SYNC_ROUNDS = 2          # 启动冷窗：前 N 轮不做补开/挂单镜像（等纸盘自我修正）
_QTY_MIN_DIFF = 1e-8

_rounds_done = 0


# ---- 工具 ----

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(v) -> datetime | None:
    """SQLite TIMESTAMP 字符串（UTC）→ aware datetime"""
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
    """纸盘行新鲜度：updated_at 距今 ≤ SYNC_FRESH_MAX_AGE。
    1s 循环每 tick 刷新 updated_at → 正常 ≤1s；引擎停摆/旧快照恢复则冻结。"""
    age = _parse_ts(paper_row.get("updated_at"))
    if age is None:
        return False
    return (_now_utc() - age).total_seconds() <= SYNC_FRESH_MAX_AGE


def _open_qty(pos: dict) -> float:
    qty = float(pos.get("quantity") or 0)
    closed = float(pos.get("closed_qty") or 0)
    return max(qty - closed, 0)


def _load_orders(live: dict) -> dict:
    try:
        return json.loads(live.get("exchange_orders_json") or "{}")
    except Exception:
        return {}


def _save_orders(lc, live: dict, orders: dict) -> None:
    storage.live_position_update(lc, live["id"], {
        "exchange_orders_json": json.dumps(orders),
    })


def _write_live_record(lc, values: dict) -> int | None:
    """写 live 记录，返回记录 id（写不进去返回 None）。

    paper_position_id 是 UNIQUE 且**跨所有状态**——`_live_rows` 只返回活跃行，
    看不到 CLOSED 行。直接 INSERT OR IGNORE 撞上一条 CLOSED 残留行会被静默丢弃：
    实盘有仓、记录写不进去 → 下一轮补建分支再挂一张 SL，单号没人记 → 越积越多
    （触发时 N 张 STOP_MARKET 同时平仓）。冲突时复用旧行，照
    `alphafox_live._write_live_open` 的做法。"""
    if storage.live_position_insert(lc, values):
        row = lc.execute("SELECT id FROM live_positions WHERE paper_position_id=?",
                         (values.get("paper_position_id"),)).fetchone()
        return int(row["id"]) if row else None
    row = lc.execute("SELECT id, status FROM live_positions WHERE paper_position_id=?",
                     (values.get("paper_position_id"),)).fetchone()
    if not row:
        print(f"[st-live] {values.get('token')} live 记录写入失败、也查不到同归属的旧行，"
              f"本轮不继续（下轮重试）")
        return None
    vals = dict(values)
    # 复用旧行：时间戳重置（"刚开仓"保护按新仓算）；realized_pnl **不清零**——
    # 同一纸盘仓可能平了又开，清掉会让上一段的真实盈亏从统计里消失
    vals.update({"closed_at": None, "created_at": "__CURRENT_TIMESTAMP__"})
    storage.live_position_update(lc, row["id"], vals)
    print(f"[st-live] {values.get('token')} live 记录 #{row['id']} 复用更新"
          f"（旧状态 {row['status']} → {values.get('status')}）")
    return int(row["id"])


def _warn_sl_rejected(token: str, where: str) -> None:
    """SL 挂单被拒**必须出声**：静默失败会让实盘仓位裸奔好几天没人发现
    （2026-09-14 实测：`triggerPrice` 没按 tickSize 对齐被币安 -1111 拒了 267 轮，
    日志里一条告警都没有，ETH 实盘仓一直裸奔）。"""
    print(f"[st-live] {token} SL 挂单被拒（{where}）: {exchange.get_last_error()}"
          f" —— 实盘可能裸奔中，下轮重试")


def _place_pending_sl(token: str, side: str, qty: float, sl: float,
                      orders: dict) -> None:
    """挂单时预挂 SL（币安条件单触发前不依赖持仓，可无仓预挂）。
    失败只告警：成交后 worker 下一轮仍会按实际持仓补挂，行为不劣于改动前。"""
    try:
        r = exchange.stop_loss_order(token, _close_side(side), qty, sl,
                                     position_side=side)
        if r:
            orders["sl"] = r.get("order_id")
            orders["sl_price"] = sl
            orders["sl_qty"] = qty     # SL 覆盖量，_align_open 据此发现覆盖不足
        else:
            print(f"[st-live] {token} 挂单预挂 SL 被拒"
                  f"（{exchange.get_last_error()}），成交后补挂")
    except Exception as e:
        print(f"[st-live] {token} 挂单预挂 SL 异常（成交后仍会补挂）: {e}")


def _limit_fill_state(token: str, orders: dict) -> tuple[bool, float]:
    """我们那张限价单的成交状态：(是否已有成交, **这条挂单自己**成交的量)。

    判"挂单成交"必须看这张单自己的状态，不能只看"交易所有没有持仓"——反转市价开的
    并存窗口里那个持仓是**市价腿**的，只看持仓会每隔一轮把自己的挂单误判成
    "已成交"并撤掉（那正是要等回调的那张单），同时多记一条 OPEN（实盘盈亏双计）。

    返回的量也**必须**是这条挂单自己的量：用整个交易所持仓量会把市价腿的量算进来
    （并存时 SL 会超额、记录量也错）。

    判据：
      1. 没存单号 / 查单失败 → (False, 0)：当"没成交"，本轮不动，下轮重试。
         绝不能把"查不到"当成"成交了"——那正是原来的 bug 形态
      2. 单子还在挂 + executedQty > 0 → (True, executedQty)：**部分成交**
         （未成交部分仍在挂，但已产生持仓，SL 要按实际成交的量校正）
      3. 单子不在挂单列表 → (True, limit_qty)：整张已成交（或被撤）
      4. 单子还在挂 + executedQty == 0 → (False, 0)：没成交（持仓是市价腿的）
    """
    oid = (orders or {}).get("limit")
    if not oid:
        return False, 0.0
    try:
        want = int(oid)
    except (TypeError, ValueError):
        return False, 0.0
    try:
        rows = exchange.get_open_orders_or_none(token)
    except Exception as e:
        print(f"[st-live] {token} 查挂单异常，本轮不判成交（下轮重试）: {e}")
        return False, 0.0
    if rows is None:
        print(f"[st-live] {token} 查挂单失败，本轮不判成交（下轮重试）")
        return False, 0.0
    for r in rows:
        if int(r.get("orderId") or 0) == want:
            exq = float(r.get("executedQty") or 0)
            return (exq > 0), exq
    return True, float((orders or {}).get("limit_qty") or 0)


def _is_orphan_live_pend(paper_pend_id, live_pend: dict | None) -> bool:
    """live 挂单记录指向的纸盘行是否已不是当前活跃 PENDING（= 这条记录结束了）。

    成交后被 `_merge_positions` 合并、或纸盘挂单被撤，都会让纸盘行离开 PENDING。
    """
    if live_pend is None:
        return False
    return live_pend.get("paper_position_id") != paper_pend_id


def _need_qty_writeback(have, want, ex_exists: bool, fresh: bool) -> bool:
    """交易所已与纸盘一致（无需下单）时，记录里的数量是否仍需要刷新。

    原逻辑只在"发生了补/减仓动作"时才把数量写回；而反转并存合并后交易所天然
    就是整仓，diff=0 永远不触发动作 → 记录停在市价腿的量 → 之后合法减仓会被
    _align_open 的"交易所远超镜像记录（疑似手动仓）"守卫跳过。
    """
    if not (ex_exists and fresh):
        return False
    return abs(float(have or 0) - float(want or 0)) > 1e-9


def _handover_sl(lc, token: str, live_row: dict, side: str) -> bool:
    """把代表记录的 SL 按**纸盘活跃量**撤旧挂新（收敛重复记录的覆盖量交接）。

    收敛重复记录时 `_cancel_our_orders` 会把它的单（含预挂 SL）一起撤掉——
    那部分覆盖量必须先转移到代表记录上，否则总覆盖会不足（实测：从"超额 0.001"
    变成"欠额 0.001"，价格打到轨道只平一半）。

    返回 True = 交接完成（调用方可以安全收敛重复记录）；False = 没交接成，
    重复记录的单**必须留着**（那些 SL 是当前仅有的止损覆盖）。"""
    with storage.get_conn() as conn:
        row = conn.execute("SELECT * FROM trade_positions WHERE id=?",
                           (live_row.get("paper_position_id"),)).fetchone()
    if not row:
        print(f"[st-live] {token} SL 交接跳过：找不到纸盘行 "
              f"#{live_row.get('paper_position_id')}")
        return False
    row = dict(row)
    target = _open_qty(row)
    sl_price = float(row.get("stop_loss_price") or 0)
    if target <= 0 or sl_price <= 0:
        print(f"[st-live] {token} SL 交接跳过：纸盘量 {target:.6g} / "
              f"止损价 {sl_price}")
        return False
    orders = _load_orders(live_row)
    if orders.get("sl"):
        old_sl_id = orders["sl"]
        try:
            if not exchange.cancel_order(token, old_sl_id):
                print(f"[st-live] {token} SL 交接撤旧单失败，本轮不重挂（下轮重试）")
                return False
        except Exception as e:
            print(f"[st-live] {token} SL 交接撤旧单异常: {e}")
            return False
        orders["sl"] = None
        # 撤旧成功必须**立刻落盘**：下面挂新单可能失败直接 return，那时 DB 里若还留着
        # 旧的 sl id + sl_price，_align_open 会判"SL 已就位" → 实盘持仓零止损覆盖
        # （直到下一根 15m K 线 SL 价变动才自愈）
        storage.live_position_update(lc, live_row["id"], {
            "exchange_orders_json": json.dumps(orders),
        })
        _log_event(lc, token, "sl_cancel", order_id=old_sl_id,
                   paper_id=live_row.get("paper_position_id"),
                   reason="重复记录收敛：交接撤旧止损")
    try:
        r = exchange.stop_loss_order(token, _close_side(side), target, sl_price,
                                     position_side=side)
    except Exception as e:
        print(f"[st-live] {token} SL 交接挂新单异常: {e}")
        return False
    if not r:
        print(f"[st-live] {token} SL 交接挂新单被拒（下轮重试）")
        return False
    orders["sl"] = r.get("order_id")
    orders["sl_price"] = sl_price
    orders["sl_qty"] = target
    storage.live_position_update(lc, live_row["id"], {
        "exchange_orders_json": json.dumps(orders),
    })
    _log_event(lc, token, "sl_update", price=sl_price, qty=target,
               paper_id=live_row.get("paper_position_id"),
               order_id=r.get("order_id"),
               reason="重复记录收敛：止损覆盖量交接重挂")
    print(f"[st-live] {token} 代表记录 #{live_row['id']} SL 重挂到 {target:.6g}"
          f"（交接重复记录的覆盖量）")
    return True


def _cancel_our_orders(token: str, live_rows: list[dict],
                       lc=None, reason: str = "") -> int:
    """撤销 live 记录里我们挂的单（限价/algo 条件）。返回失败笔数。
    失败时不丢弃 id（调用方若落盘则下轮重试），并告警——静默丢 id 会留下
    无人管理的孤儿条件单。

    lc 给了就顺手记一条撤单日志（孤儿单/残留单被撤是用户最想知道的事之一）；
    可选参数是为了不打扰只做纯撤单校验的单元测试。"""
    failed = 0
    for lr in live_rows:
        orders = _load_orders(lr)
        for key in ("limit", "sl"):
            oid = orders.get(key)
            if not oid:
                continue
            try:
                ok = exchange.cancel_order(token, oid)
            except Exception as e:
                ok = False
                print(f"[st-live] {token} 撤单异常（{key} {oid}）: {e}")
            if ok:
                orders[key] = None
                if lc is not None:
                    _log_event(lc, token,
                               "limit_cancel" if key == "limit" else "sl_cancel",
                               order_id=oid,
                               paper_id=lr.get("paper_position_id"),
                               reason=reason)
            else:
                failed += 1
                print(f"[st-live] {token} 撤单失败（{key} {oid}）："
                      f"交易所可能残留条件单，请留意")
    return failed


def _paper_rows(token: str) -> tuple[dict | None, dict | None]:
    with storage.get_conn() as conn:
        open_row = conn.execute(
            "SELECT * FROM trade_positions WHERE token=? AND strategy='supertrend' "
            "AND status IN ('OPEN','PARTIAL') ORDER BY id DESC LIMIT 1",
            (token,)).fetchone()
        pend_row = conn.execute(
            "SELECT * FROM trade_positions WHERE token=? AND strategy='supertrend' "
            "AND status='PENDING' ORDER BY id DESC LIMIT 1",
            (token,)).fetchone()
    return (dict(open_row) if open_row else None,
            dict(pend_row) if pend_row else None)


def _latest_closed_paper_row(token: str) -> dict | None:
    """该币最新的已平纸盘行（收敛时用来取纸盘口径 realized）。
    切换实盘/手动平仓这两条路以前传 None → 交易所持仓已消失时 realized 回落到
    记录原值（0），实盘统计少记这些平仓的盈亏。"""
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM trade_positions WHERE token=? AND strategy='supertrend' "
            "AND status='CLOSED' ORDER BY id DESC LIMIT 1", (token,)).fetchone()
    return dict(row) if row else None


def _live_rows(lc, token: str) -> list[dict]:
    return [dict(r) for r in lc.execute(
        "SELECT * FROM live_positions WHERE token=? AND strategy='supertrend' "
        "AND status IN ('OPEN','PARTIAL','PENDING') ORDER BY id DESC",
        (token,)).fetchall()]


def _ex_match(ex_positions: list[dict], token: str, side: str) -> dict | None:
    """交易所持仓匹配（Hedge 模式按 positionSide，兼容 BOTH）"""
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


def _close_qty_ex(token: str, qty: float) -> float:
    """平仓量按 stepSize **向下**取整（过滤器不可用时原样返回）。
    返回 0 = 数量不足一个 step（残仓小于最小下单量），市价单下不出去。"""
    try:
        return exchange._round_qty_down(f"{token.upper()}USDT", qty)
    except Exception:
        return qty


def _looks_like_ours(ex_row: dict, ref_qty: float, ref_leverage) -> bool:
    """交易所持仓是否像我们的镜像仓：杠杆一致 且 数量与参考量同量级（±25%）。
    同币同向的手动仓会与镜像仓合并，用于避免把它当成我们的仓去平/去对齐。"""
    if not ex_row or ref_qty <= 0:
        return False
    try:
        ex_qty = abs(float(ex_row.get("quantity") or 0))
        lev = float(ex_row.get("leverage") or 0)
        ref_lev = float(ref_leverage or 0)
    except (TypeError, ValueError):
        return False
    if ex_qty <= 0:
        return False
    if lev > 0 and ref_lev > 0 and abs(lev - ref_lev) > 0.5:
        return False
    return abs(ex_qty - ref_qty) <= 0.25 * ref_qty


def _ex_positions() -> list[dict] | None:
    """交易所持仓快照。None=API 不可用（与空列表区分：
    失败返回 [] 会被误判"持仓消失"误平纸盘/漏平残留）。

    用 get_positions_or_none（失败返 None），**不要**退回
    `get_positions() + get_last_error()`：`_last_error_msg` 是模块级共享变量，
    跟单/实盘页也在清它，多线程下会被别人清空 → 失败结果被当成有效 → 误判持仓消失
    → 平纸盘 + 记录 CLOSED + 实盘裸仓。"""
    try:
        return exchange.get_positions_or_none()
    except Exception:
        return None


def _alphafox_occupied(token: str) -> bool:
    """跟单（alphafox）是否占用该币实盘（币种级互斥：同一币种同一时刻
    只允许一个策略用实盘）。

    判不出来时**当占用**（fail-closed）：这是道安全闸门，放行的代价是"我们的
    STOP_MARKET 覆盖到跟单的仓、把别人的仓位平掉"——比"这一轮不动"严重得多。
    查询失败会在日志里留痕，不会静默。
    """
    try:
        import alphafox_live
        return alphafox_live.alphafox_occupied(token)
    except Exception as e:
        print(f"[st-live] {token} 跟单占用查询失败，按「被占用」处理（本轮不动）: {e}")
        return True


def _close_side(side: str) -> str:
    return "SELL" if side == "LONG" else "BUY"


def _log_event(lc, token: str, action: str, *, price=None, qty=None,
               paper_id=None, order_id=None, reason: str = "") -> None:
    """细粒度实盘动作日志（页面「交易日志」面板）。

    只在**动作真的发生**时调用（挂/改/撤成功之后），不写"本轮检查过没问题"这类
    轮询噪音 —— 对账每轮都跑，记流水会把有用的行淹没。

    **写失败只告警、绝不抛出**：日志插在状态转换中间（如"止损已挂到交易所、
    live 记录还没落库"），抛出去会被 sync 的逐币 except 吞成一行"同步异常"并
    中断本轮 —— 记录没写而交易所已有仓，下一轮 `_mirror_open` 会再市价开一张
    （重复开仓）。记流水的重要性不该高过仓位状态机。
    """
    try:
        storage.live_trade_log_insert(lc, {
            "token": token, "strategy": "supertrend", "action": action,
            "price": price, "quantity": qty,
            "paper_position_id": paper_id,
            "exchange_order_id": str(order_id or ""),
            "details_json": json.dumps({"reason": reason}) if reason else None,
        })
    except Exception as e:
        print(f"[st-live] {token} 交易日志写入失败（不影响交易）: {e}")


def _log_close(lc, token: str, live: dict, price, reason: str,
               realized: float, paper_id=None) -> None:
    _log_event(lc, token, "close", price=price, qty=live.get("quantity"),
               paper_id=paper_id or live.get("paper_position_id"),
               reason=reason)


# ---- 平仓/收敛 ----

def _close_exchange_and_mark(lc, token: str, live_rows: list[dict],
                             ex_positions: list[dict] | None,
                             paper_close_row: dict | None,
                             reason: str) -> None:
    """收敛：**按方向分组** → 撤该组的单 → 平该组的交易所持仓 → 该组 live CLOSED。
    纸盘已平（paper_close_row=该币最新 CLOSED 行）时 realized 直接用纸盘口径。
    ex_positions=None（API 不可用）→ 不动作不标记，下轮重试。
    一个币可能同时有 LONG/SHORT 两条 live 记录（换向后旧方向保留等其 SL 触发，
    见 _sync_token 的 stale 分支）。必须按方向分别收敛：只平一侧却把两侧都标
    CLOSED，另一侧的交易所持仓会变成"记录已平 + SL 已撤"的永久裸仓，且该记录
    不再被 _live_rows 返回 → 永远不会有人再管它。"""
    if ex_positions is None:
        print(f"[st-live] {token} 收敛推迟：交易所持仓查询不可用")
        return
    groups: dict[str, list[dict]] = {}
    for lr in live_rows:
        groups.setdefault(lr.get("side") or "LONG", []).append(lr)
    if not groups and paper_close_row:
        groups[paper_close_row.get("side") or "LONG"] = []
    for side, rows in groups.items():
        _close_side_and_mark(lc, token, side, rows, ex_positions,
                             paper_close_row, reason)


def _close_side_and_mark(lc, token: str, side: str, live_rows: list[dict],
                         ex_positions: list[dict], paper_close_row: dict | None,
                         reason: str) -> None:
    """单方向收敛：撤单 → 平该方向持仓 → 标 CLOSED + realized。
    平仓失败（异常/无返回）→ 不标 CLOSED，下轮重试（SL 已撤+持仓仍在，
    标平仓就是裸仓且永不重试）。撤单失败同样不标 CLOSED（持仓照平，但孤儿单
    要有主，见下）。"""
    cancel_failed = _cancel_our_orders(token, live_rows, lc=lc, reason="平仓收敛撤单")

    close_price = None
    if ex_positions is not None:
        ex_row = _ex_match(ex_positions, token, side)
        if ex_row:
            ex_qty = abs(float(ex_row.get("quantity") or 0))
            # 只平我们记录的数量：交易所若混有手动仓（同币同向合并）不越界
            our_qty = sum(float(lr.get("quantity") or 0) for lr in live_rows)
            qty = min(ex_qty, our_qty) if our_qty > 0 else ex_qty
            if our_qty <= 0 and not _looks_like_ours(
                    ex_row, float((paper_close_row or {}).get("quantity") or ex_qty),
                    (paper_close_row or {}).get("leverage")):
                # 无 live 记录且持仓不像我们的（手动仓/其他来源）→ 不动交易所
                print(f"[st-live] {token} 交易所持仓 {ex_qty:.6g} 不像镜像仓，"
                      f"不代平（如需平仓请手动处理）")
                qty = 0.0
            elif ex_qty > qty * 1.5 + _QTY_MIN_DIFF:
                print(f"[st-live] {token} 交易所持仓 {ex_qty:.6g} 远超镜像记录，"
                      f"仅平我们的部分（可能有手动仓合并）")
            if qty > 0:
                # 平仓量只许向下量化，绝不放大：放大到 minQty 的超量单会被交易所
                # 拒掉，记录就永久"不收敛"重试（而 SL 已经在上面撤了 = 裸仓）
                qty = _close_qty_ex(token, qty)
                if qty <= 0:
                    # 残仓不足最小下单量：市价单根本下不出去 → 记录照常收敛
                    # （否则该币永远卡在活跃状态、被互斥闸门挡着用不了），
                    # 残留量告警交人工在币安清理
                    print(f"[st-live] {token} 交易所残仓 {ex_qty:.6g} 不足最小下单量，"
                          f"无法自动平仓（{reason}）：记录照常收敛，请到币安手动清理")
            if qty > 0:
                pos_side = ex_row.get("position_side") or side
                try:
                    r = exchange.close_position_market(
                        token, qty, position_side=pos_side)
                except Exception as e:
                    print(f"[st-live] {token} 实盘平仓失败（{reason}）: {e}，"
                          f"记录不收敛（下轮重试）")
                    return  # 不标 CLOSED：SL 已撤+持仓仍在，标平仓=裸仓且永不重试
                if not r:
                    print(f"[st-live] {token} 实盘平仓无返回（{reason}），"
                          f"记录不收敛（下轮重试）")
                    return
                filled = float(r.get("quantity") or 0)
                if filled <= 0:
                    print(f"[st-live] {token} 实盘平仓零成交（{reason}）："
                          f"记录不收敛（下轮重试）")
                    return
                if filled < qty * 0.999:
                    # 部分成交：**不标 CLOSED、也不改记录数量**。下轮重算时
                    # qty=min(交易所剩余, 记录量) 自然会继续收敛；这里若按纸盘量
                    # 改记录，会把没平掉的那部分也当成平了 → 交易所留残仓无人管
                    print(f"[st-live] {token} 实盘平仓部分成交 {filled:.6g}/{qty:.6g}"
                          f"（{reason}），记录不收敛（下轮继续平剩余）")
                    return
                close_price = r.get("price") or ex_row.get("mark_price")
                print(f"[st-live] {token} 实盘平仓 {side} qty={qty} "
                      f"@ {close_price}（{reason}）")

    if cancel_failed:
        # 还有单没撤掉 → **不标 CLOSED**（持仓已经平掉了，不急）：记录一旦离开
        # _live_rows 就再没人重试撤单，那张孤儿条件单会留在交易所 —— 日后可能在
        # 错误价位平掉新开的仓。下轮会再来一次：先撤单，成功了才收敛
        print(f"[st-live] {token} {side} 有 {cancel_failed} 笔单未撤掉，"
              f"本轮不标 CLOSED（下轮重试撤单）")
        return

    for lr in live_rows:
        realized = float(lr.get("realized_pnl") or 0)
        if paper_close_row is not None and \
                paper_close_row.get("id") == lr.get("paper_position_id"):
            # 仅当这条 live 记录对应的纸盘行就是该平仓行时，才沿用纸盘口径 realized
            # （否则会把上一笔已平仓的 realized 复制到本条 → 幽灵盈亏）
            realized = float(paper_close_row.get("realized_pnl") or 0)
        elif close_price and lr.get("entry_price"):
            qty = float(lr.get("quantity") or 0)
            entry = float(lr.get("entry_price") or 0)
            realized = ((entry - close_price) if lr.get("side") == "SHORT"
                        else (close_price - entry)) * qty
        storage.live_position_update(lc, lr["id"], {
            "status": "CLOSED",
            "closed_at": "__CURRENT_TIMESTAMP__",
            "realized_pnl": round(realized, 6),
        })
        _log_close(lc, token, lr, close_price, reason, realized)


def _close_paper_and_mark(lc, token: str, live_rows: list[dict],
                          paper_open: dict, ex_positions, reason: str) -> None:
    """交易所持仓已消失（币安 SL 已触发）→ 纸盘同步 CLOSED + live 收敛。
    rowcount 条件防与 1s 循环竞态（谁先平谁赢）。"""
    import supertrend_strategy
    price = supertrend_strategy.get_mark_price(token)
    if not price or price <= 0:
        price = float(paper_open.get("current_price")
                      or paper_open.get("stop_loss_price") or 0)
    entry = float(paper_open.get("entry_price") or 0)
    qty = _open_qty(paper_open)
    realized = ((entry - price) if paper_open.get("side") == "SHORT"
                else (price - entry)) * qty
    margin = float(paper_open.get("margin_amount") or 1)
    with storage.get_conn() as conn:
        cur = conn.execute(
            "UPDATE trade_positions SET status='CLOSED', current_price=?, "
            "closed_qty=?, realized_pnl=?, unrealized_pnl=0, "
            "pnl_pct=?, advice=?, closed_at=CURRENT_TIMESTAMP "
            "WHERE id=? AND status IN ('OPEN','PARTIAL')",
            (price, float(paper_open.get("quantity") or 0), realized,
             realized / margin * 100,
             "币安 SL 条件单已触发平仓（实盘镜像收敛）", paper_open["id"]),
        )
        if cur.rowcount > 0:
            storage.trade_signal_lock_release(conn, token, "supertrend")
            print(f"[st-live] {token} 币安 SL 已触发 → 纸盘同步平仓 @ {price}")
            try:
                supertrend_strategy.record_close_snap(token, paper_open["id"])
            except Exception:
                pass
    # live 收敛用刚算的 realized 口径（paper_open 是 OPEN 行，realized=0）
    close_row = dict(paper_open)
    close_row["realized_pnl"] = realized
    _close_exchange_and_mark(lc, token, live_rows, ex_positions,
                             close_row, reason)


# ---- 开仓/挂单镜像（受闸门保护） ----

def _mirror_open(lc, token: str, paper_open: dict, live_rows: list[dict],
                 ex_positions: list[dict]) -> None:
    """补开：纸盘 OPEN、live 无 OPEN 记录 → 币安市价开 + 挂 SL 条件单。
    闸门：启动冷窗 + 纸盘行新鲜度（防重启/停摆后以旧仓追价）。
    live 有 PENDING 记录（限价单未成交而纸盘已转市价开）→ 复用该记录转 OPEN
    （paper_position_id UNIQUE，不能 INSERT 同 id 新行）。"""
    global _rounds_done
    if _rounds_done <= MIN_SYNC_ROUNDS:
        print(f"[st-live] {token} 补开跳过：启动冷窗（第{_rounds_done}轮）")
        return
    if not _fresh(paper_open):
        print(f"[st-live] {token} 补开跳过：纸盘持仓过旧，不同步实盘")
        return
    if _alphafox_occupied(token):
        print(f"[st-live] {token} 补开跳过：被跟单实盘占用（释放后自动接管）")
        return
    side = paper_open.get("side") or "LONG"
    qty = _open_qty(paper_open)
    lev = int(float(paper_open.get("leverage") or 1))
    symbol = f"{token}USDT"
    # 只撤"交易所已无持仓"的残留记录的挂单（旧方向若仍有持仓，保留其 SL 托管）
    cancel_failed = _cancel_our_orders(
        token,
        [r for r in live_rows
         if _ex_match(ex_positions, token, r.get("side") or "LONG") is None],
        lc=lc, reason="补开前清理残留单")
    if cancel_failed:
        # 有撤不掉的残留单 → **推迟本轮补开**：下面无论走复用分支（新 orders 覆盖
        # 记录）还是补建分支，都会让那些单号从 DB 里蒸发，孤儿单再没人认识。
        # 先把它清干净，下一轮再来（F1 修好后，撤一张已成交的单算成功，不会卡住）
        print(f"[st-live] {token} 补开推迟：有 {cancel_failed} 笔残留单撤不掉"
              f"（先清干净，下轮再来）")
        return
    if not exchange.set_leverage(symbol, lev):
        print(f"[st-live] {token} 补开失败：设杠杆 {lev}x 失败")
        return
    result = (exchange.market_open_long(token, qty) if side == "LONG"
              else exchange.market_open_short(token, qty))
    if not result or result.get("status") not in ("FILLED", "PARTIALLY_FILLED", "NEW"):
        print(f"[st-live] {token} 补开失败: {result} {exchange.get_last_error()}")
        return
    fill = result.get("price") or float(paper_open.get("entry_price") or 0)
    actual_qty = result.get("quantity") or qty
    orders = {"open": result.get("order_id"), "sl": None, "sl_price": None,
              "limit": None, "limit_price": None, "limit_qty": None}
    sl = float(paper_open.get("stop_loss_price") or 0)
    if sl > 0 and actual_qty > 0:
        try:
            r = exchange.stop_loss_order(
                token, _close_side(side), actual_qty, sl, position_side=side)
            if r:
                orders["sl"] = r.get("order_id")
                orders["sl_price"] = sl
                orders["sl_qty"] = actual_qty
                _log_event(lc, token, "sl_set", price=sl, qty=actual_qty,
                           paper_id=paper_open.get("id"),
                           order_id=r.get("order_id"), reason="补开挂止损")
            else:
                _warn_sl_rejected(token, "补开")
        except Exception as e:
            print(f"[st-live] {token} 补开挂 SL 异常: {e}")
    reuse = next((r for r in live_rows if r["status"] == "PENDING"
                  and (r.get("side") or "LONG") == side), None)
    if reuse:
        fields = {
            "status": "OPEN", "entry_price": fill, "quantity": actual_qty,
            "leverage": lev, "margin_usd": paper_open.get("margin_amount"),
            "exchange_orders_json": json.dumps(orders),
        }
        # 归属必须改指到当前纸盘仓：不改的话记录还挂在旧行上（旧行可能已
        # CLOSED/CANCELED），之后"交易所持仓消失 → 纸盘同步收敛"的归属比对永远不成立
        # （走成市价重建）、realized 也算到错的行上。
        # paper_position_id 有 UNIQUE 约束 → 仅当该纸盘仓连 CLOSED 行都没有时才改指
        if reuse.get("paper_position_id") != paper_open["id"]:
            taken = lc.execute("SELECT 1 FROM live_positions WHERE "
                               "paper_position_id=? LIMIT 1",
                               (paper_open["id"],)).fetchone()
            if taken:
                print(f"[st-live] {token} 补开复用记录 #{reuse['id']}：纸盘仓 "
                      f"#{paper_open['id']} 已另有记录，归属保持 "
                      f"#{reuse.get('paper_position_id')}")
            else:
                print(f"[st-live] {token} 补开复用记录 #{reuse['id']}：归属改指 "
                      f"#{reuse.get('paper_position_id')} → #{paper_open['id']}")
                fields["paper_position_id"] = paper_open["id"]
        storage.live_position_update(lc, reuse["id"], fields)
    else:
        # 只关"交易所已无持仓"的残留记录；旧方向仍有持仓的（等其 SL 触发）保留。
        # （撤单失败已在函数开头提前 return，这里一定是清干净的）
        for r in live_rows:
            if _ex_match(ex_positions, token, r.get("side") or "LONG"):
                continue
            storage.live_position_update(lc, r["id"], {
                "status": "CLOSED", "closed_at": "__CURRENT_TIMESTAMP__",
            })
        if _write_live_record(lc, {
                "paper_position_id": paper_open["id"],
                "token": token, "symbol": symbol, "side": side,
                "strategy": "supertrend",
                "entry_price": fill, "quantity": actual_qty,
                "leverage": lev, "margin_usd": paper_open.get("margin_amount"),
                "exchange_orders_json": json.dumps(orders),
                "tp1_done": 0, "tp2_done": 0, "status": "OPEN"}) is None:
            print(f"[st-live] {token} 实盘已补开但 live 记录写不进去（见上条告警），"
                  f"下轮按补建处理")
    _log_event(lc, token, "open", price=fill, qty=actual_qty,
               paper_id=paper_open["id"], order_id=orders.get("open"),
               reason="市价补开")
    print(f"[st-live] {token} 实盘补开 {side} qty={actual_qty} @ ${fill} SL={sl}")


def _mirror_pending(lc, token: str, paper_pend: dict,
                    live_pend: dict | None, ex_positions: list[dict],
                    reuse_only: bool = False) -> None:
    """挂单镜像：纸盘 PENDING → 币安 LIMIT GTC 单 + 同时预挂 STOP_MARKET 止损。
    条件单触发前不依赖持仓 → 成交瞬间即有 SL 托管，不再等 worker 下一轮补挂。
    已有 live 挂单 → limit_price/qty 变了撤旧挂新（SL 一起撤旧挂新）。
    交易所出现持仓（限价单已成交）→ live 转 OPEN + 按实际成交量校正 SL。

    reuse_only=True（`_mirror_open` 把**纸盘 OPEN 行**拿来复用 live_pend 时）：
    只做上面那条"转 OPEN"，**绝不往下走挂单逻辑** —— 本函数其余部分全程假设入参是
    PENDING 行（会用 limit_price 当挂单价），喂 OPEN 行会挂出一张按纸盘全量的错单。

    返回 True = 这条 live_pend 已转 OPEN（记录已由本函数接管）；其余路径返回假值。
    调用方（`_sync_token` 的"交易所已有持仓但无 OPEN 记录"分支）据此决定是否
    落到补建逻辑。"""
    global _rounds_done
    side = paper_pend.get("side") or "LONG"
    price = float(paper_pend.get("limit_price") or 0)
    qty = float(paper_pend.get("quantity") or 0)
    sl = float(paper_pend.get("stop_loss_price") or 0)
    symbol = f"{token}USDT"
    ex_row = _ex_match(ex_positions, token, side)

    # 被跟单实盘占用（币种级互斥）→ 撤单/挂单/改 SL 一律不碰，等释放后下一轮自愈。
    # 必须放在**成交分支之前**：那个分支会撤残留限价单、并按量重挂一张 SL，
    # 而那个量在互斥共存时可能覆盖到跟单的仓位（我们的止损会去平别人的仓）。
    if _alphafox_occupied(token):
        print(f"[st-live] {token} 挂单处理跳过：被跟单实盘占用（释放后自动接管）")
        return

    # 先算这张挂单自己的成交状态（返回的量是**这条挂单自己**成交的量，
    # 不能用整个交易所持仓量——反转并存时那个量还含市价腿的）
    filled, leg_qty = (_limit_fill_state(token, _load_orders(live_pend))
                       if live_pend else (False, 0.0))
    if live_pend and ex_row and filled:
        # 这张限价单确实成交了 + 交易所有持仓 → live 转 OPEN + SL 按这条腿的量校正。
        # 注意不能只凭 ex_row：并存窗口里那个持仓是市价腿的（filled 挡住）
        orders = _load_orders(live_pend)
        if leg_qty <= 0:                      # 整张成交但记录里没存 limit_qty 的兜底
            leg_qty = abs(float(ex_row.get("quantity") or qty))
        if orders.get("limit"):
            try:
                if exchange.cancel_order(token, orders["limit"]):
                    orders["limit"] = None
                else:
                    print(f"[st-live] {token} 撤残留限价单失败，请留意（可能二次成交）")
            except Exception as e:
                print(f"[st-live] {token} 撤残留限价单异常: {e}")
        if sl > 0 and leg_qty > 0:
            pend_qty = float(orders.get("limit_qty") or 0)
            # 部分成交：预挂 SL 的数量与实际成交量不符 → 撤旧挂新（全量成交零动作）
            if orders.get("sl") and abs(pend_qty - leg_qty) > 1e-8:
                try:
                    if exchange.cancel_order(token, orders["sl"]):
                        orders["sl"] = None
                except Exception as e:
                    print(f"[st-live] {token} 撤旧 SL 异常: {e}")
            if not orders.get("sl"):
                try:
                    r = exchange.stop_loss_order(
                        token, _close_side(side), leg_qty, sl, position_side=side)
                    if r:
                        orders["sl"] = r.get("order_id")
                        orders["sl_price"] = sl
                        orders["sl_qty"] = leg_qty
                        _log_event(lc, token, "sl_set", price=sl, qty=leg_qty,
                                   paper_id=live_pend.get("paper_position_id"),
                                   order_id=r.get("order_id"),
                                   reason="挂单成交补挂")
                    else:
                        _warn_sl_rejected(token, "挂单成交转 OPEN")
                except Exception as e:
                    print(f"[st-live] {token} 挂单成交补 SL 异常: {e}")
        storage.live_position_update(lc, live_pend["id"], {
            "status": "OPEN",
            "entry_price": ex_row.get("entry_price"),
            "quantity": leg_qty,
            "exchange_orders_json": json.dumps(orders),
        })
        _log_event(lc, token, "open", price=ex_row.get("entry_price"),
                   qty=leg_qty,
                   paper_id=live_pend.get("paper_position_id"),
                   order_id=orders.get("limit") or "",
                   reason="限价单成交（挂单腿转入持仓）")
        print(f"[st-live] {token} 限价单已成交 → live 转 OPEN"
              f"（本腿 {leg_qty:.6g}，SL 已托管）")
        return True

    if reuse_only:
        # 只做"复用 live_pend 转 OPEN"，到此为止：调用方给的是**纸盘 OPEN 行**
        # （limit_price=开仓价），继续往下会按它挂出一张错的限价单。
        # 返回 False：这条 live_pend 还挂着（没成交），记录没被接管 —— 调用方
        # 需要继续走补建，否则"纸盘 OPEN + 交易所有仓 + 只有 PENDING 记录"永远没人管
        return False

    if price <= 0 or qty <= 0:
        return
    if live_pend is None:
        # 该纸盘挂单已有 live 记录（可能已转 OPEN：交易所成交但纸盘还没转）
        # → 不再重复挂单，否则每轮挂一张新限价单+SL，成交后实盘仓位成倍放大
        row = lc.execute("SELECT status FROM live_positions WHERE paper_position_id=? "
                         "LIMIT 1", (paper_pend["id"],)).fetchone()
        if row:
            print(f"[st-live] {token} 挂单已有 live 记录（{row['status']}），"
                  f"跳过重复挂单")
            return
    if _rounds_done <= MIN_SYNC_ROUNDS:
        print(f"[st-live] {token} 挂单镜像跳过：启动冷窗（第{_rounds_done}轮）")
        return
    if not _fresh(paper_pend):
        print(f"[st-live] {token} 挂单镜像跳过：纸盘挂单过旧")
        return
    # （被跟单占用的闸门已在函数开头统一挡掉——那之后的所有撤单/挂单/改 SL 都不该发生）

    buy_sell = "BUY" if side == "LONG" else "SELL"
    if live_pend:
        orders = _load_orders(live_pend)
        same_price = (orders.get("limit") and orders.get("limit_price")
                      and abs(float(orders["limit_price"]) - price) < 1e-9
                      and abs(float(orders.get("limit_qty") or 0) - qty) < 1e-8)
        if same_price:
            # 价格/数量未变：SL 缺失（预挂被拒）或与纸盘不一致（仅纸盘 SL 变了、
            # 挂单价没变）→ 撤旧挂新，防实盘止损停在旧价
            stale_sl = (sl > 0 and (
                not orders.get("sl")
                or abs(float(orders.get("sl_price") or 0) - sl) > 1e-9))
            if stale_sl:
                had_sl = bool(orders.get("sl"))
                if had_sl:
                    old_sl_id = orders["sl"]
                    try:
                        ok = exchange.cancel_order(token, old_sl_id)
                    except Exception as e:
                        ok = False
                        print(f"[st-live] {token} 撤旧 SL 异常: {e}")
                    if not ok:
                        # 撤不掉就别挂新的（否则两张 SL 并存，旧价可能先触发）
                        print(f"[st-live] {token} 撤旧 SL 失败，本轮不重挂（下轮重试）")
                        return
                    orders["sl"] = None
                    orders["sl_price"] = None
                    _log_event(lc, token, "sl_cancel", order_id=old_sl_id,
                               paper_id=live_pend.get("paper_position_id"),
                               reason="挂单腿止损改价撤旧")
                _place_pending_sl(token, side, qty, sl, orders)
                if orders.get("sl"):
                    _log_event(lc, token, "sl_set", price=sl, qty=qty,
                               paper_id=live_pend.get("paper_position_id"),
                               order_id=orders.get("sl"),
                               reason=("挂单腿止损改价重挂" if had_sl
                                       else "挂单腿补挂止损"))
                _save_orders(lc, live_pend, orders)
            return
        # 换价：撤旧限价单 + 旧 SL。撤失败必须保留 id 并放弃本轮换价——静默丢 id
        # 会留下孤儿限价单（价格回到旧价时新旧两张都成交 → 实盘名义翻倍），
        # 与 _cancel_our_orders 的策略一致
        cancel_failed = False
        for key in ("limit", "sl"):
            oid = orders.get(key)
            if not oid:
                continue
            try:
                ok = exchange.cancel_order(token, oid)
            except Exception as e:
                ok = False
                print(f"[st-live] {token} 撤旧单异常（{key} {oid}）: {e}")
            if ok:
                orders[key] = None
                if key == "sl":
                    orders["sl_price"] = None
                _log_event(lc, token,
                           "limit_cancel" if key == "limit" else "sl_cancel",
                           order_id=oid,
                           paper_id=live_pend.get("paper_position_id"),
                           reason="挂单换价撤旧")
            else:
                cancel_failed = True
                print(f"[st-live] {token} 撤旧单失败（{key} {oid}），本轮不换价"
                      f"（交易所可能残留旧单，请留意）")
        _save_orders(lc, live_pend, orders)
        if cancel_failed:
            return
    r = exchange.limit_open(token, buy_sell, qty, price, position_side=side)
    if not r:
        print(f"[st-live] {token} 限价挂单失败: {exchange.get_last_error()}")
        return
    # sl_price 不能预置：预挂被拒时只有 sl 为 None 而 sl_price 有值，
    # 成交后 _align_open 会认为 SL 已就位 → 实盘持仓无止损裸奔
    # （_mirror_open 同样是只在挂单成功后才写 sl_price）
    orders = {"open": None, "sl": None, "sl_price": None,
              "limit": r.get("order_id"), "limit_price": price, "limit_qty": qty}
    if sl > 0 and qty > 0:
        _place_pending_sl(token, side, qty, sl, orders)
        if orders.get("sl"):
            _log_event(lc, token, "sl_set", price=sl, qty=qty,
                       paper_id=paper_pend["id"], order_id=orders.get("sl"),
                       reason="挂单腿预挂止损")
    if live_pend:
        storage.live_position_update(lc, live_pend["id"], {
            "quantity": qty, "exchange_orders_json": json.dumps(orders),
        })
        _log_event(lc, token, "open", price=price, qty=qty,
                   paper_id=paper_pend["id"], order_id=r.get("order_id"),
                   reason="限价挂单改价")
        print(f"[st-live] {token} 限价单更新 @ {price} qty={qty}")
    else:
        if _write_live_record(lc, {
                "paper_position_id": paper_pend["id"],
                "token": token, "symbol": symbol, "side": side,
                "strategy": "supertrend",
                "entry_price": None, "quantity": qty,
                "leverage": int(float(paper_pend.get("leverage") or 1)),
                "margin_usd": paper_pend.get("margin_amount"),
                "exchange_orders_json": json.dumps(orders),
                "tp1_done": 0, "tp2_done": 0, "status": "PENDING"}) is None:
            print(f"[st-live] {token} 限价单已挂但 live 记录写不进去（见上条告警），"
                  f"该单下轮无人认领")
        _log_event(lc, token, "open", price=price, qty=qty,
                   paper_id=paper_pend["id"], order_id=r.get("order_id"),
                   reason="限价挂单")
        print(f"[st-live] {token} 限价挂单 {side} {qty} @ {price}")


def _align_open(lc, token: str, paper_open: dict, live_open: dict,
                ex_positions: list[dict]) -> None:
    """持仓对齐：SL 变了（新K线）撤旧挂新；qty 变了（合并）差额补/减。
    纸盘不新鲜 → SL 不可信，跳过（币安旧 SL 单继续托管，无害）。
    补/减仓后 SL 数量重挂到新总持仓（防部分仓位裸奔）。"""
    if _alphafox_occupied(token):
        # 被跟单实盘占用（币种级互斥）→ 撤单/挂 SL/补减仓一律不碰：下面会把 SL
        # 按纸盘量重挂，那个量在互斥共存时可能覆盖到跟单的仓位（STOP_MARKET 没有
        # reduceOnly → 会去平别人的仓）。释放后下一轮自愈。
        print(f"[st-live] {token} 持仓对齐跳过：被跟单实盘占用（释放后自动接管）")
        return
    if not _fresh(paper_open):
        return
    side = paper_open.get("side") or "LONG"
    ex_row = _ex_match(ex_positions, token, side)
    if not ex_row:
        return
    # live_open 是 _sync_token 开头加载的**旧快照**：本轮前面的步骤（SL 交接、孤儿
    # 收敛、限价单转 OPEN）可能已经改过它。拿旧 orders 对账会撤一个已作废的单号、
    # 再挂一张新的 → 交易所两张 SL 并存（超覆盖：价格打到时同时触发 = 超量平仓/反向开仓）
    _fresh_row = lc.execute(
        "SELECT exchange_orders_json FROM live_positions WHERE id=?",
        (live_open["id"],)).fetchone()
    if _fresh_row is not None:
        live_open = dict(live_open)
        live_open["exchange_orders_json"] = _fresh_row["exchange_orders_json"]
    orders = _load_orders(live_open)
    changed = False
    qty_changed = False       # 是否发生了补/减仓（决定要不要把数量写回记录）
    sl = float(paper_open.get("stop_loss_price") or 0)

    def _resync_sl(qty: float) -> None:
        """撤旧 SL 挂新（价格/数量以当前为准）。失败保留旧 sl_price → 下轮差异重试。"""
        nonlocal orders, changed
        if sl <= 0 or qty <= 0:
            return
        if orders.get("sl"):
            try:
                ok = exchange.cancel_order(token, orders["sl"])
            except Exception as e:
                ok = False
                print(f"[st-live] {token} 撤旧 SL 异常: {e}")
            if not ok:
                # 撤不掉就不要再挂新的（否则两张 SL 并存，旧价可能先触发）
                print(f"[st-live] {token} 撤旧 SL 失败，本轮不更新（下轮重试）")
                return
        orders["sl"] = None
        orders["sl_qty"] = None
        try:
            r = exchange.stop_loss_order(
                token, _close_side(side), qty, sl, position_side=side)
            if r:
                orders["sl"] = r.get("order_id")
                orders["sl_price"] = sl
                orders["sl_qty"] = qty
                _log_event(lc, token, "sl_update", price=sl, qty=qty,
                           paper_id=paper_open.get("id"),
                           order_id=r.get("order_id"), reason="止损重挂")
                print(f"[st-live] {token} SL 更新 @ {sl} qty={qty}")
            else:
                _warn_sl_rejected(token, "对账重挂")
        except Exception as e:
            print(f"[st-live] {token} SL 更新异常: {e}")
        changed = True

    # 目标数量按交易所 stepSize 量化：否则纸盘量非 step 整数倍时每轮差半个
    # step，经 _round_qty(取整+minQty 兜底) 被放大成整步 → 反复补/减甚至全平
    paper_qty = _round_qty_ex(token, _open_qty(paper_open))
    ex_qty = abs(float(ex_row.get("quantity") or 0))
    diff = paper_qty - ex_qty
    if abs(diff) > _QTY_MIN_DIFF and diff > 0:
        try:
            result = (exchange.market_open_long(token, diff) if side == "LONG"
                      else exchange.market_open_short(token, diff))
            if result and result.get("status") in ("FILLED", "PARTIALLY_FILLED", "NEW"):
                print(f"[st-live] {token} 实盘补仓 +{result.get('quantity')}")
                _log_event(lc, token, "rebalance_add",
                           price=result.get("price"), qty=result.get("quantity"),
                           paper_id=paper_open.get("id"),
                           order_id=result.get("order_id"),
                           reason="交易所少于纸盘，补仓对齐")
                changed = True
                qty_changed = True
                # 补仓后 SL 数量同步到新总持仓
                _resync_sl(paper_qty)
        except Exception as e:
            print(f"[st-live] {token} 实盘补仓异常: {e}")
    elif abs(diff) > _QTY_MIN_DIFF:
        # 交易所持仓远超镜像记录（混入手动仓）→ 不越界减仓
        our_qty = float(live_open.get("quantity") or 0)
        close_qty = min(-diff, ex_qty)
        # 这里**不能提前 return**：下面还要按 SL 的覆盖量/价格重挂止损，
        # 不能因为交易所混了一笔手动仓就不管我们的止损了
        if our_qty > 0 and ex_qty > our_qty * 1.25 + _QTY_MIN_DIFF:
            print(f"[st-live] {token} 对账减仓跳过：交易所 {ex_qty:.6g} 远超"
                  f"镜像记录 {our_qty:.6g}（可能有手动仓合并）")
        elif close_qty > _QTY_MIN_DIFF:
            try:
                result = exchange.close_position_market(
                    token, close_qty, position_side=side)
                filled = float((result or {}).get("quantity") or 0)
                if result and filled <= 0:
                    print(f"[st-live] {token} 实盘减仓零成交，本轮不改记录（下轮重试）")
                elif result and filled < close_qty * 0.999:
                    # 部分成交：本轮**不按纸盘量改记录**（那会把没减掉的也算成减了），
                    # 保留原值让下轮按剩余差额继续对齐
                    print(f"[st-live] {token} 实盘减仓部分成交 {filled:.6g}/"
                          f"{close_qty:.6g}，记录留原值（下轮继续）")
                elif result:
                    print(f"[st-live] {token} 实盘减仓 -{filled:.6g}")
                    _log_event(lc, token, "rebalance_reduce", qty=filled,
                               paper_id=paper_open.get("id"),
                               order_id=(result or {}).get("order_id"),
                               reason="交易所多于纸盘，减仓对齐")
                    changed = True
                    qty_changed = True
                    _resync_sl(paper_qty)
            except Exception as e:
                print(f"[st-live] {token} 实盘减仓异常: {e}")
        # close_qty 太小时无事可做（同样不 return）

    # 重挂 SL 的三个理由：
    #   · sl 单 id 缺失（预挂被拒/记录来自旧版代码）——只看 sl_price 会把"价格没变"
    #     当成"止损已挂好"，实盘裸奔
    #   · sl 价变了（新 K 线轨道移动）
    #   · **SL 覆盖量 ≠ 当前持仓量**（并存合并后，代表作记录那张 SL 可能只覆盖被并掉
    #     的那半；只靠价格变化要等下一根 K 线才发现，期间半仓裸奔）。旧记录没存
    #     sl_qty → 当"不符"处理，重挂一次即自愈
    # 位置必须在补/减仓**之后**：上面若刚补/减过仓，它已经按 paper_qty 重挂过一次 SL；
    # 放在前面的话同一轮会 cancel+place 两遍（白费一对 API，且中间有一瞬 SL 比持仓大）
    # 数量**不能用交易所该方向全量**：那含用户手动加的仓/跟单的仓，STOP_MARKET 没有
    # reduceOnly，会连带把别人的仓一起平掉（_mirror_pending 为此设了闸门，这里同等对待）
    if sl > 0 and (not orders.get("sl")
                   or abs(float(orders.get("sl_price") or 0) - sl) > 1e-9
                   or orders.get("sl_qty") is None
                   or abs(float(orders.get("sl_qty") or 0) - paper_qty)
                   > _QTY_MIN_DIFF):
        _resync_sl(paper_qty)

    # 交易所已与纸盘一致（diff 在阈值内 → 一个动作都没做）时，**记录本身**可能仍是
    # 旧值：反转并存合并后交易所天然就是整仓（市价腿 + 成交的挂单腿），diff=0 永远
    # 不触发补/减仓 → qty_changed 恒 False → 记录停在市价腿的量 → 之后合法减仓会被
    # 上面那句"交易所远超镜像记录（疑似手动仓）"守卫跳过。所以这里补一次数量回写。
    if not qty_changed and _need_qty_writeback(
            live_open.get("quantity") or 0, paper_qty,
            ex_row is not None, _fresh(paper_open)):
        qty_changed = True
        changed = True

    if changed:
        fields = {"exchange_orders_json": json.dumps(orders)}
        if qty_changed:
            # 补/减仓后把数量写回记录（否则记录长期小于实际持仓，
            # 平仓时"只平我们的部分"会留下残仓）
            fields["quantity"] = paper_qty
        storage.live_position_update(lc, live_open["id"], fields)


def _sync_token(lc, token: str, live_rows: list[dict], ex_positions: list[dict]) -> None:
    paper_open, paper_pend = _paper_rows(token)
    side = ((paper_open or paper_pend) or {}).get("side") or "LONG"
    ex_row = _ex_match(ex_positions, token, side)
    # 按方向匹配：换向场景（纸盘平多开空）旧方向的 live 残留不能当成当前仓
    live_open = next((r for r in live_rows
                      if r["status"] in ("OPEN", "PARTIAL")
                      and (r.get("side") or "LONG") == side), None)
    live_pend = next((r for r in live_rows if r["status"] == "PENDING"
                      and (r.get("side") or "LONG") == side), None)

    # live 挂单记录"孤儿"收敛：它指向的纸盘行已经不是活跃 PENDING 了（挂单成交后
    # 被 _merge_positions 合并、或纸盘挂单被撤）。下面"复用 live_pend 转 OPEN"那条
    # 路径有 `live_open is None` 的前提，反转并存时到不了 → 不收敛就会留下僵尸
    # PENDING 记录，之后被 `live_pend`（按 status 取第一条）当成"已有的实盘挂单"用错。
    # 持仓已由 live_open 代表 → 收敛这条记录，**它的单（限价 + 预挂 SL）一律撤掉**。
    # 撤单不能加"限价单没成交才撤"的条件：成交时那张预挂 STOP_MARKET 还挂在交易所，
    # 记录一标 CLOSED 就再没人认识它 —— 日后在旧价位平掉新仓，或与代表作记录的 SL
    # 叠加成超覆盖（两张同时触发 = 超量平仓/反向开仓）。撤一张已成交的单是幂等的
    # （exchange.cancel_order 查不到即算成功）。
    # live_open 为空时不收敛：交给下面复用的路径转 OPEN（保持原行为）
    if (_is_orphan_live_pend(paper_pend["id"] if paper_pend else None, live_pend)
            and live_open is not None):
        _orphan_fail = _cancel_our_orders(token, [live_pend], lc=lc,
                                          reason="孤儿挂单记录收敛撤单")
        if _orphan_fail:
            # 撤不掉就不收敛（同上）：记录离开 _live_rows 后那张单再没人管
            print(f"[st-live] {token} 孤儿挂单记录 #{live_pend['id']} 撤单失败，"
                  f"保留记录下轮重试")
        else:
            storage.live_position_update(lc, live_pend["id"], {
                "status": "CLOSED", "closed_at": "__CURRENT_TIMESTAMP__"})
            print(f"[st-live] {token} 实盘挂单记录 #{live_pend['id']} 收敛"
                  f"（纸盘挂单已结束，持仓由 OPEN 记录代表）")
            live_rows = [r for r in live_rows if r["id"] != live_pend["id"]]
            live_pend = None

    # 同向重复 OPEN 记录收敛 + SL 交接：反转并存时两条腿各有记录，纸盘合并成
    # 一笔后，被并掉那条腿的记录就成了孤儿（指向的纸盘行已不活跃）。现有 stale
    # 清理会因"交易所仍有持仓"把它当旧方向残留保留 → 实盘两条 OPEN 记录 = 重复记账。
    # 这里只处理**同方向**的重复（反方向残留是换向场景，必须保留等它自己扫损收敛）。
    if ((paper_open is not None or paper_pend is not None)
            and not _alphafox_occupied(token)):
        # 被跟单占用的币跳过：这一步会撤单 + 按量重挂 SL，而互斥共存时那个量
        # 可能覆盖到跟单的仓位（同上）。释放后下一轮自愈。
        with storage.get_conn() as conn:
            active_ids = {r["id"] for r in conn.execute(
                "SELECT id FROM trade_positions WHERE token=? AND "
                "strategy='supertrend' AND status IN ('OPEN','PARTIAL','PENDING')",
                (token,))}
        opens = [r for r in live_rows if r["status"] in ("OPEN", "PARTIAL")
                 and (r.get("side") or "LONG") == side]
        if len(opens) > 1:
            preferred = next((r for r in opens
                              if r.get("paper_position_id") in active_ids), None)
            dups = ([r for r in opens if r["id"] != preferred["id"]
                     and r.get("paper_position_id") not in active_ids]
                    if preferred is not None else [])
            # 交接必须放在收敛**之前**：重复记录的单马上要被撤掉，它占的 SL 覆盖量
            # 得先转移到代表记录上。交接没成 → **本轮不收敛**：撤掉它们的单会让止损
            # 覆盖归零（代表记录此刻没有 SL）= 全仓裸奔，留着多覆盖好过没覆盖
            if dups and not _handover_sl(lc, token, preferred, side):
                print(f"[st-live] {token} SL 交接未完成，本轮不收敛重复记录"
                      f"（保留它们的止损覆盖，下轮重试）")
                dups = []
            if dups:
                for r in dups:
                    if _cancel_our_orders(token, [r], lc=lc,
                                          reason="同向重复记录收敛撤单"):
                        print(f"[st-live] {token} 同向重复记录 #{r['id']} 撤单失败，"
                              f"保留记录下轮重试")
                        continue
                    storage.live_position_update(lc, r["id"], {
                        "status": "CLOSED", "closed_at": "__CURRENT_TIMESTAMP__"})
                    print(f"[st-live] {token} 同向重复记录 #{r['id']} 收敛"
                          f"（纸盘行已合并，持仓由 #{preferred['id']} 代表）")
                    live_rows = [x for x in live_rows if x["id"] != r["id"]]
                # live_open 上面已解析过（那时重复记录还在，可能正好选中它）
                # → 收敛后必须重解析，否则后面的对齐/SL 全拿已关闭的记录当对象
                live_open = next((r for r in live_rows
                                  if r["status"] in ("OPEN", "PARTIAL")
                                  and (r.get("side") or "LONG") == side), None)

    # 清理纸盘行已不存在的多余 live 记录（合并/换向后残留）。
    # 旧方向若在交易所仍有持仓（SL 未触发）→ 保留记录与 SL 单，等它自行扫损收敛
    stale = [r for r in live_rows
             if r is not live_open and r is not live_pend]
    for r in stale:
        if _ex_match(ex_positions, token, r.get("side") or "LONG"):
            print(f"[st-live] {token} 旧方向 live 记录保留"
                  f"（交易所仍有持仓，等 SL 触发后收敛）")
            continue
        if _cancel_our_orders(token, [r], lc=lc, reason="残留记录收敛撤单"):
            # 撤不掉任何一张 → **不标 CLOSED**：记录一旦离开 _live_rows 就再没人重试
            # 撤单，那张孤儿单会一直挂在交易所（限价单日后成交 = 凭空多出无记录的仓位；
            # 残留 SL 会在旧价位平掉将来的新仓）
            print(f"[st-live] {token} 残留记录 #{r['id']} 撤单失败，保留记录下轮重试")
            continue
        storage.live_position_update(lc, r["id"], {
            "status": "CLOSED", "closed_at": "__CURRENT_TIMESTAMP__",
        })

    if paper_open is None and paper_pend is None:
        # 纸盘已无活跃 → 收敛实盘（平仓类不受闸门限制）
        if live_rows:
            _close_exchange_and_mark(lc, token, live_rows, ex_positions,
                                     _latest_closed_paper_row(token), "纸盘已平仓")
        return

    if paper_open:
        if ex_row:
            if live_open:
                _align_open(lc, token, paper_open, live_open, ex_positions)
            else:
                # 交易所已有持仓但 live 无 OPEN 记录：
                # live_pend 成交转 OPEN 或 补建记录（不市价开，防重复仓）
                if live_pend:
                    # reuse_only：这里给的是**纸盘 OPEN 行**，只允许它做"复用 live_pend
                    # 转 OPEN"；不给这个开关的话，函数后半段会拿 OPEN 行的 limit_price
                    # (=开仓价) 当挂单价，挂出一张按纸盘全量的错单。
                    # 返回真 = 真转成 OPEN（记录已接管）→ 本轮到此为止；限价单还挂着
                    # 没成交时必须继续往下补建，否则"纸盘在市价持仓 + 交易所有仓 +
                    # 只有 PENDING 记录"永远没人处理（这个 return 曾把补建挡成死代码）
                    if _mirror_pending(lc, token, paper_open, live_pend,
                                       ex_positions, reuse_only=True):
                        return
                if _alphafox_occupied(token):
                    # 交易所持仓可能是跟单的 → 不认领
                    print(f"[st-live] {token} 补建记录跳过：被跟单实盘占用")
                    return
                if not _fresh(paper_open):
                    print(f"[st-live] {token} live 记录缺失且纸盘过旧，跳过补建")
                    return
                # 交易所持仓不像我们的（手动仓/其他来源）→ 不认领、不挂 SL
                if not _looks_like_ours(ex_row, _open_qty(paper_open),
                                        paper_open.get("leverage")):
                    print(f"[st-live] {token} 补建记录跳过：交易所持仓 "
                          f"{abs(float(ex_row.get('quantity') or 0)):.6g} 不像镜像仓")
                    return
                lev = int(float(paper_open.get("leverage") or 1))
                sl = float(paper_open.get("stop_loss_price") or 0)
                orders = {"open": None, "sl": None, "sl_price": None,
                          "limit": None, "limit_price": None, "limit_qty": None}
                actual_qty = abs(float(ex_row.get("quantity") or 0))
                # **先落记录再挂 SL**：反过来的话，记录写不进去（UNIQUE 撞上 CLOSED
                # 残留行、被 OR IGNORE 吞掉）时这张 SL 的单号没人记 → 每轮补建再挂
                # 一张，止损单越积越多（价格打到触发时 N 张同时平仓）
                rid = _write_live_record(lc, {
                    "paper_position_id": paper_open["id"],
                    "token": token, "symbol": f"{token}USDT", "side": side,
                    "strategy": "supertrend",
                    "entry_price": ex_row.get("entry_price"),
                    "quantity": actual_qty,
                    "leverage": lev, "margin_usd": paper_open.get("margin_amount"),
                    "exchange_orders_json": json.dumps(orders),
                    "tp1_done": 0, "tp2_done": 0, "status": "OPEN",
                })
                if rid is None:
                    return
                if sl > 0 and actual_qty > 0:
                    try:
                        r = exchange.stop_loss_order(
                            token, _close_side(side), actual_qty, sl,
                            position_side=side)
                        if r:
                            orders["sl"] = r.get("order_id")
                            orders["sl_price"] = sl
                            orders["sl_qty"] = actual_qty
                            _log_event(lc, token, "sl_set", price=sl,
                                       qty=actual_qty,
                                       paper_id=paper_open.get("id"),
                                       order_id=r.get("order_id"),
                                       reason="补建记录挂止损")
                        else:
                            _warn_sl_rejected(token, "补建记录")
                    except Exception as e:
                        print(f"[st-live] {token} 补建记录挂 SL 异常: {e}")
                    # 挂失败也要落盘：orders['sl'] 仍是 None，下轮 _align_open
                    # 按"缺 SL"补挂
                    storage.live_position_update(lc, rid, {
                        "exchange_orders_json": json.dumps(orders),
                    })
                _log_event(lc, token, "rebuild", price=ex_row.get("entry_price"),
                           qty=actual_qty, paper_id=paper_open.get("id"),
                           reason="交易所已有持仓，重建镜像记录")
                print(f"[st-live] {token} 补建 live 记录（交易所已有持仓）")
        else:
            # 交易所无持仓：
            # - 本方向 live 有 OPEN 记录**且它对应的就是本纸盘仓** → 币安 SL 已触发，
            #   纸盘同步收敛
            # - live 仅 PENDING（限价单未成交，纸盘已转市价开）→ 撤单市价补开
            # - 无 live 记录 → 正常补开（闸门）
            # 注意①：只按"本方向的" live 记录判断，否则换向后旧方向的残留 OPEN
            # 记录会把新开的纸盘仓误判成"交易所持仓消失"而强平
            # 注意②：还要比对 paper_position_id——旧纸盘仓刚被 1s 循环扫损、同一轮里
            # 又开了新仓（60s 内没有任何一轮看到"纸盘全空"）时，旧记录仍是本方向
            # 的 live_open，会把崭新的仓按现价强平（假盈亏 + churn）
            if live_open is not None and \
                    live_open.get("paper_position_id") == paper_open["id"]:
                _close_paper_and_mark(lc, token, [live_open], paper_open,
                                      ex_positions, "交易所持仓消失")
                return
            if live_open is not None:
                print(f"[st-live] {token} 实盘 OPEN 记录属旧纸盘仓 "
                      f"#{live_open.get('paper_position_id')}（交易所已无该方向持仓）"
                      f"→ 按残留处理，当前仓 #本仓照常补开")
            _mirror_open(lc, token, paper_open, live_rows, ex_positions)

    if paper_pend:
        _mirror_pending(lc, token, paper_pend, live_pend, ex_positions)


# ---- 对外入口 ----

def sync_supertrend_live() -> None:
    """每轮对账（_run_round 末尾调用）。交易所不可用 → 本轮跳过，下轮重试。
    包含 REMOVED 币：纸盘已无活跃 → 残留实盘收敛（remove API 之外的安全网）。"""
    global _rounds_done
    _rounds_done += 1
    try:
        with storage.get_conn(config.SUPERTREND_DB) as st_conn:
            tokens = [dict(r) for r in st_conn.execute(
                "SELECT * FROM supertrend_tokens "
                "WHERE status IN ('ACTIVE','REMOVED')")]
    except Exception:
        return
    ex_positions = _ex_positions()
    if ex_positions is None:
        print("[st-live] 交易所持仓查询失败，本轮跳过")
        return
    try:
        storage.init_onchain_live_db()
    except Exception as e:
        print(f"[st-live] live DB 初始化失败: {e}")
        return
    with storage.get_conn(config.LIVE_DB_PATH) as lc:
        for t in tokens:
            token = t["token"]
            live_mode = t.get("live_mode") in (1, "1", True)
            try:
                live_rows = _live_rows(lc, token)
                if not live_mode:
                    # 切回纸盘后残留的实盘记录 → 收敛
                    if live_rows:
                        _close_exchange_and_mark(lc, token, live_rows,
                                                 ex_positions, None,
                                                 "已切回纸盘")
                    continue
                _sync_token(lc, token, live_rows, ex_positions)
            except Exception as e:
                print(f"[st-live] {token} 同步异常: {e}")


def switch_live_mode(token: str, live: bool) -> dict:
    """切换实盘模式：清监控状态 + 收敛旧侧持仓，两边互斥从零开始。
    两个方向都**先关纸盘再平交易所**（防两步之间的窗口被 60s 同步重建，见下），
    最后清 status 重新走首次流程（记基准 → 等闭合 → 挂单）。"""
    import supertrend_strategy
    token = token.upper()

    # 0. **先暂停监控**：下面关纸盘/平交易所要跑好几秒 API，窗口里策略线程可能
    # 又开一个新纸盘仓（此时 live_mode 还是 1）→ 同步层会把它镜像到刚清空的交易所上，
    # "从零开始"的语义被破坏。置 paused 让 _process_token 直接跳过（不拉信号不动手）
    try:
        with storage.get_conn(config.SUPERTREND_DB) as conn_st:
            conn_st.execute(
                "UPDATE supertrend_tokens SET paused=1 WHERE token=?", (token,))
    except Exception as e:
        print(f"[st-live] {token} 切换实盘：置暂停失败: {e}")

    # 1. 纸盘侧收敛（平仓/取消挂单）——**必须排在实盘之前**（照 close_live 的顺序）：
    # 反过来的话，两步之间的窗口里 60s 同步会看到"纸盘还 OPEN 且新鲜 + live 记录已空"
    # → _mirror_open 把刚平的仓市价重建，随后纸盘才被平 → 实盘裸持到下一轮。
    try:
        supertrend_strategy.close_supertrend_position(token, "SWITCH_LIVE")
    except Exception as e:
        print(f"[st-live] {token} 切换实盘：纸盘收敛异常: {e}")

    # 2. 实盘侧收敛：平掉该币交易所全部持仓（含未跟踪/手动仓）+ 撤全部挂单。
    # 用户语义：切换实盘 = 该币从零开始，旧持仓与挂单清理干净（防遗留仓干扰镜像）
    try:
        storage.init_onchain_live_db()
        with storage.get_conn(config.LIVE_DB_PATH) as lc:
            live_rows = _live_rows(lc, token)
            ex_positions = _ex_positions()
            if live_rows:
                _close_exchange_and_mark(lc, token, live_rows, ex_positions,
                                         _latest_closed_paper_row(token),
                                         "切换实盘模式")
            # 未跟踪残留（live 记录丢失/手动仓）→ 一并平掉（两个方向各查一次）。
            # 重新拉一次持仓：上面刚平过的仓不能再平第二遍（旧快照会重复下单）
            ex_positions = _ex_positions()
            if ex_positions:
                for sd in ("LONG", "SHORT"):
                    ex_row = _ex_match(ex_positions, token, sd)
                    if not ex_row:
                        continue
                    qty = abs(float(ex_row.get("quantity") or 0))
                    if qty <= 0:
                        continue
                    try:
                        exchange.close_position_market(
                            token, qty, position_side=ex_row.get("position_side") or sd)
                        print(f"[st-live] {token} 切换实盘：平掉残留持仓 "
                              f"{sd} qty={qty}")
                    except Exception as e:
                        print(f"[st-live] {token} 切换实盘：平残留 {sd} 失败: {e}")
    except Exception as e:
        print(f"[st-live] {token} 切换实盘：实盘侧收敛异常: {e}")
    try:
        exchange.cancel_all_orders(token)
    except Exception:
        pass

    # 3. 清监控状态 → 重新走首次流程（记基准 → 等闭合 → 挂单）
    # 切实盘时顺带恢复监控（暂停状态下实盘不会开仓，避免用户困惑）
    with storage.get_conn(config.SUPERTREND_DB) as conn_st:
        conn_st.execute("DELETE FROM supertrend_status WHERE token=?", (token,))
        if live:
            conn_st.execute(
                "UPDATE supertrend_tokens SET live_mode=1, paused=0 WHERE token=?",
                (token,))
        else:
            conn_st.execute(
                "UPDATE supertrend_tokens SET live_mode=0 WHERE token=?",
                (token,))
    print(f"[st-live] {token} 切换为{'实盘' if live else '纸盘'}，监控状态已重置")
    return {"ok": True, "token": token, "live_mode": bool(live)}


def close_live(token: str) -> bool:
    """页面手动平仓（实盘页按钮）：先关纸盘（防 sync 竞态重开）→
    平交易所 → live 收敛。同时暂停监控（永远持仓策略平了会重开）。
    仅对 live_mode=1 的币生效——纸盘模式的纸盘仓不受实盘页平仓按钮影响。"""
    import supertrend_strategy
    token = token.upper()
    with storage.get_conn(config.SUPERTREND_DB) as conn_st:
        row = conn_st.execute(
            "SELECT live_mode FROM supertrend_tokens WHERE token=?",
            (token,)).fetchone()
    is_live = bool(row and row["live_mode"])
    if not is_live:
        return False
    # **先暂停监控再动手**：下面关纸盘/平交易所要跑好几秒 API，窗口里策略线程
    # 可能又开一个新纸盘仓 → 被同步层镜像到刚清空的交易所上（等于没平干净）
    try:
        with storage.get_conn(config.SUPERTREND_DB) as conn_st:
            conn_st.execute(
                "UPDATE supertrend_tokens SET paused=1 WHERE token=?", (token,))
    except Exception as e:
        print(f"[st-live] {token} 手动平仓：置暂停失败: {e}")
    paper_closed = False
    try:
        paper_closed = supertrend_strategy.close_supertrend_position(
            token, "MANUAL")
    except Exception as e:
        print(f"[st-live] {token} 手动平仓：纸盘平仓异常: {e}")
    live_ok = True
    try:
        storage.init_onchain_live_db()
        with storage.get_conn(config.LIVE_DB_PATH) as lc:
            live_rows = _live_rows(lc, token)
            if live_rows:
                ex_positions = _ex_positions()
                if ex_positions is None:
                    # 交易所查询不可用 → _close_exchange_and_mark 会提前返回，
                    # 实盘仓其实还在。不能对外报"平成功"（页面会显示已平），
                    # 下一轮 sync 会收敛，这里如实告诉调用方
                    live_ok = False
                    print(f"[st-live] {token} 手动平仓：交易所查询不可用，"
                          f"实盘仓未平（下一轮对账会收敛）")
                else:
                    _close_exchange_and_mark(lc, token, live_rows, ex_positions,
                                             _latest_closed_paper_row(token),
                                             "手动平仓")
    except Exception as e:
        live_ok = False
        print(f"[st-live] {token} 手动平仓：实盘侧异常: {e}")
    # 暂停监控防重开（恢复=卡片上取消暂停）
    try:
        with storage.get_conn(config.SUPERTREND_DB) as conn_st:
            conn_st.execute(
                "UPDATE supertrend_tokens SET paused=1 WHERE token=?",
                (token,))
    except Exception:
        pass
    # 返回值要反映**两侧都收敛**：只看纸盘会在实盘仓还在时报"平成功"
    return paper_closed and live_ok
