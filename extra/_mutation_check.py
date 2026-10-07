"""变异测试：逐个把本轮修复改回 bug 版，确认测试能抓住（有牙齿）。

每个变异期望对应测试脚本退出码 != 0（被抓住）。全部抓住 = 修复有回归保护。
"""
import io
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

MUTATIONS = [
    {
        "name": "M1 补建闸门失效（任何交易所持仓都被认领）",
        "module": "alphafox_live",
        "old": "        if not _looks_like_ours(ex_row, ref_qty, live_leverage):",
        "new": "        if False:  # MUT",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "21 手动仓防护",
    },
    {
        "name": "M2 不用 _write_live_open 复用（INSERT OR IGNORE 静默丢弃）",
        "module": "alphafox_live",
        "old": """    vals = dict(values)
    if row:""",
        "new": """    vals = dict(values)
    if False:  # MUT""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "25 CLOSED 残留不自动重开",
    },
    {
        "name": "M3 平仓失败/零成交也标 CLOSED（不看重试）",
        "module": "alphafox_live",
        "old": """            if not r:
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
            if filled < qty * 0.999:""",
        "new": """            r = r or {"price": None, "quantity": qty}  # MUT 无返回也当成交
            filled = qty  # MUT 不看成交量
            if False:""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "26 平仓 API 无返回 / 45 零成交不标已平",
    },
    {
        "name": "M4 交易所持仓消失不收敛（幽灵记录）",
        "module": "alphafox_live",
        "old": """        print(f"[af-live] {token} 交易所持仓消失（手动平仓/强平？）→ 实盘记录 + "
              f"纸盘这一笔一起收尾（之后不再跟这笔，等交易员开新仓）")""",
        "new": """        return  # MUT 旧行为：不收敛
        print(f"[af-live] {token} 交易所持仓消失（手动平仓/强平？）→ 实盘记录 + "
              f"纸盘这一笔一起收尾（之后不再跟这笔，等交易员开新仓）")""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "27 记录收敛 CLOSED",
    },
    {
        "name": "M5 豁免仓不查清单（对账照改交易所）",
        "module": "alphafox_live",
        "old": """    if _exempt_has(token, side):
        return  # 豁免仓：实盘不处理（仅 CLOSE 跟随解除）""",
        "new": """    pass  # MUT 不查豁免""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "28 豁免仓对账不动交易所",
    },
    {
        "name": "M6 对账查纸盘仓策略名写回 'alphofox'（本轮根因）",
        "module": "alphafox_live",
        "old": """                "SELECT * FROM trade_positions WHERE strategy='alphafox' "
                "AND status IN ('OPEN','PARTIAL')")]""",
        "new": """                "SELECT * FROM trade_positions WHERE strategy='alphofox' "
                "AND status IN ('OPEN','PARTIAL')")]""",
        "script": "extra/_e2e_alphafox_live.py",
        "caught_by": "端到端对账收敛",
    },
    {
        "name": "M8 REDUCE 不检查平仓返回值（记账先减）",
        "module": "alphafox_live",
        "old": """                r = exchange.close_position_market(token, close_qty, position_side=side)
                if not r:
                    print(f"[af-live] {token} 实盘减仓无返回，本轮不改记账"
                          f"（下轮重试）")
                    return
                filled = float(r.get("quantity") or 0)""",
        "new": """                exchange.close_position_market(token, close_qty, position_side=side)
                filled = close_qty  # MUT""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "30 减仓失败不改记账",
    },
    {
        "name": "M9 手动平仓不解除豁免键（永久挡住重开）",
        "module": "alphafox_live",
        "old": """    if handled:
        # 解除豁免键（必须在 with 块外：嵌在主库写事务里再开写连接会被锁住）
        for pos in rows:
            _exempt_remove_token_side(token, pos.get("side") or "LONG")
            # 与 _close_paper_leg 同理：这一笔结束了，"没跟上"的原因也别留着，
            # 否则交易员重开同币时会短暂显示上一笔的原因
            _clear_skip(token, pos.get("side") or "LONG")""",
        "new": """    if handled:
        for pos in rows:      # MUT 不解除豁免键
            _clear_skip(token, pos.get("side") or "LONG")""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "32 手动平仓解除豁免",
    },
    {
        "name": "M10 手动平仓无行情兜底（卡死不平）",
        "module": "alphafox_live",
        "old": """        if not mark or mark <= 0:
            # 行情不可用 → 用纸盘现价/入场价兜底（否则纸盘平不掉，
            # 实盘页兜底却已平交易所 → 纸盘与实盘长期不一致）
            for pos in rows:
                mark = float(pos.get("current_price")
                             or pos.get("entry_price") or 0)
                if mark > 0:
                    break""",
        "new": """        if not mark or mark <= 0:
            pass  # MUT""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "31 手动平仓兜底价",
    },
    {
        "name": "M11 空单平仓用卖出价（成本方向错）",
        "module": "alphafox_trade",
        "old": """        exit_px = (_buy_price(mark, params) if side == "SHORT"
                   else _sell_price(mark, params))
        entry = float(pos.get("entry_price") or 0)""",
        "new": """        exit_px = _sell_price(mark, params)  # MUT
        entry = float(pos.get("entry_price") or 0)""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "28 空单全平按买回价",
    },
    {
        "name": "M12 同时改量+杠杆时 margin 用旧持仓量（少算）",
        "module": "alphafox_trade",
        "old": """                "margin_amount=?, updated_at=CURRENT_TIMESTAMP WHERE id=? "
                "AND status IN ('OPEN','PARTIAL')" + _ACT_GUARD_ALIGN,
                (target_lev, post_open * post_entry / target_lev, pos["id"]))""",
        "new": """                "margin_amount=?, updated_at=CURRENT_TIMESTAMP WHERE id=? "
                "AND status IN ('OPEN','PARTIAL')" + _ACT_GUARD_ALIGN,
                (target_lev, open_qty * entry / target_lev, pos["id"]))  # MUT""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "29 变化后持仓量算 margin",
    },
    {
        "name": "M13 超势对齐不做 step 量化（振荡/减成全平）",
        "module": "supertrend_live",
        "old": "    paper_qty = _round_qty_ex(token, _open_qty(paper_open))",
        "new": "    paper_qty = _open_qty(paper_open)  # MUT",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "21 数量量化不振荡",
    },
    {
        "name": "M14 超势补/减仓后不写回 live 记录数量（账本漂移→残仓）",
        "module": "supertrend_live",
        "old": """        fields = {"exchange_orders_json": json.dumps(orders)}
        if qty_changed:
            # 补/减仓后把数量写回记录（否则记录长期小于实际持仓，
            # 平仓时"只平我们的部分"会留下残仓）
            fields["quantity"] = paper_qty
        storage.live_position_update(lc, live_open["id"], fields)""",
        "new": """        storage.live_position_update(lc, live_open["id"], {
            "exchange_orders_json": json.dumps(orders)})  # MUT""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "7 纸盘平仓全收敛",
    },
    {
        "name": "M15 超势不按方向匹配 live 记录（换向误平新仓）",
        "module": "supertrend_live",
        "old": """    live_open = next((r for r in live_rows
                      if r["status"] in ("OPEN", "PARTIAL")
                      and (r.get("side") or "LONG") == side), None)""",
        "new": """    live_open = next((r for r in live_rows
                      if r["status"] in ("OPEN", "PARTIAL")), None)  # MUT""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "22 换向不误平新仓",
    },
    {
        "name": "M16 超势认领非镜像仓（手动仓被接管/挂 SL）",
        "module": "supertrend_live",
        "old": """                if not _looks_like_ours(ex_row, _open_qty(paper_open),
                                        paper_open.get("leverage")):
                    print(f"[st-live] {token} 补建记录跳过：交易所持仓 "
                          f"{abs(float(ex_row.get('quantity') or 0)):.6g} 不像镜像仓")
                    return""",
        "new": """                pass  # MUT 不校验""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "23 手动仓防护",
    },
    {
        "name": "M17 超势平仓只平一行（反转并存窗口残留）",
        "module": "supertrend_strategy",
        "old": """            closed_any = True""",
        "new": """            closed_any = True
            break  # MUT 只平一行""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "24 多行活跃仓全平",
    },
    {
        "name": "M18 挂单不预挂 SL（回到成交后 60s 空窗）",
        "module": "supertrend_live",
        "old": """    if sl > 0 and qty > 0:
        _place_pending_sl(token, side, qty, sl, orders)""",
        "new": """    if False:  # MUT 不预挂
        _place_pending_sl(token, side, qty, sl, orders)""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "3 挂单同时预挂 SL",
    },
    {
        "name": "M19 换价时不撤旧 SL（挂着错量）",
        "module": "supertrend_live",
        "old": """        cancel_failed = False
        for key in ("limit", "sl"):
            oid = orders.get(key)
            if not oid:
                continue""",
        "new": """        cancel_failed = False
        for key in ():  # MUT 不撤旧单
            oid = orders.get(key)
            if not oid:
                continue""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "3 换价后仍 1 张 SL",
    },
    {
        "name": "M20 部分成交不按实际成交量重挂 SL",
        "module": "supertrend_live",
        "old": """            if orders.get("sl") and abs(pend_qty - leg_qty) > 1e-8:""",
        "new": """            if False:  # MUT""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "25 部分成交 SL 校正",
    },
    {
        "name": "M21 切实盘不清交易所残留持仓",
        "module": "supertrend_live",
        "old": """            # 重新拉一次持仓：上面刚平过的仓不能再平第二遍（旧快照会重复下单）
            ex_positions = _ex_positions()
            if ex_positions:""",
        "new": """            ex_positions = _ex_positions()
            if False:  # MUT 不清残留""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "26 切实盘清全部持仓",
    },
    {
        "name": "M22 合并不兜底（取数失败/暂停币不合并 → 反复补单）",
        "module": "supertrend_strategy",
        "old": """        try:
            with storage.get_conn() as _merge_conn:
                _merge_positions(_merge_conn, t["token"])
        except Exception as e:
            print(f"[supertrend] {t['token']} 合并异常: {e}")""",
        "new": """        pass  # MUT 不合并""",
        "script": "extra/test_supertrend_logic.py",
        "caught_by": "30 取数失败/暂停币也合并",
    },
    {
        "name": "M23 挂单 SL 只在缺失时补挂（仅纸盘 SL 变化时不跟随 → 脱节）",
        "module": "supertrend_live",
        "old": """            stale_sl = (sl > 0 and (
                not orders.get("sl")
                or abs(float(orders.get("sl_price") or 0) - sl) > 1e-9))
            if stale_sl:""",
        "new": """            if sl > 0 and not orders.get("sl"):  # MUT 只看缺失""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "27 仅 SL 变化时跟随",
    },
    # M24（平仓无返回也标 CLOSED）已删除：F5 修复加的 `filled <= 0 → return`
    # 把"无返回"这一种也覆盖了，所以它不再是独立的行为差异（由 M143 代表）
    {
        "name": "M25 挂单不查已有 live 记录（每轮重复挂单 → 仓位成倍）",
        "module": "supertrend_live",
        "old": """    if live_pend is None:
        # 该纸盘挂单已有 live 记录（可能已转 OPEN：交易所成交但纸盘还没转）
        # → 不再重复挂单，否则每轮挂一张新限价单+SL，成交后实盘仓位成倍放大
        row = lc.execute("SELECT status FROM live_positions WHERE paper_position_id=? "
                         "LIMIT 1", (paper_pend["id"],)).fetchone()
        if row:
            print(f"[st-live] {token} 挂单已有 live 记录（{row['status']}），"
                  f"跳过重复挂单")
            return""",
        "new": """    pass  # MUT 不查重复""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "29 不重复挂单",
    },
    {
        "name": "M26 1s 循环旧快照复活 CANCELED 行（数量/盈亏双计）",
        "module": "trade_logic",
        "old": """            if storage.trade_position_update(
                    conn, pos["id"], fields,
                    status_in=("OPEN", "PARTIAL")) == 0:
                continue
            storage.trade_signal_lock_release(conn, pos["token"], pos["strategy"])
            # 写平仓 journal（supertrend 跳过：不污染 agent 复盘数据）""",
        "new": """            storage.trade_position_update(conn, pos["id"], fields)
            storage.trade_signal_lock_release(conn, pos["token"], pos["strategy"])
            # 写平仓 journal（supertrend 跳过：不污染 agent 复盘数据）""",
        "script": "extra/test_supertrend_logic.py",
        "caught_by": "31 不复活 CANCELED 行",
    },
    {
        "name": "M28 对账补开不查进程启动时刻（发版/重启补开历史仓）",
        "module": "alphafox_live",
        "old": """    if _pre_startup(pp) and _key_of(token, side) not in _QF_EXPLICIT:
        _note_skip(token, side, "补开跳过：该仓早于本次进程启动（发版/重启后只跟新仓）")
        return""",
        "new": """    pass  # MUT 不查存量""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "34 存量仓不补开",
    },
    {
        "name": "M29 信号驱动也不查存量（ADD 把历史仓开成实盘）",
        "module": "alphafox_live",
        "old": """            if not live and _pre_startup(ppos) \\
                    and _key_of(token, side) not in _QF_EXPLICIT:
                print(f"[af-live] {token} 存量仓（早于本次启动创建）："
                      f"{sig['action']} 不镜像（只跟新仓）")
                return""",
        "new": """            pass  # MUT 不查存量""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "34 存量仓 ADD 不镜像",
    },
    {
        "name": "M30 存量闸门提前到认领分支之前（孤儿仓永远无记录）",
        "module": "alphafox_live",
        "old": """    if _exempt_key_of(pp) in _exempt_keys():
        _note_skip(token, side, "补开跳过：存量仓豁免（开关开启前的仓，只跟新开仓）")
        return""",
        "new": """    if _exempt_key_of(pp) in _exempt_keys():
        _note_skip(token, side, "补开跳过：存量仓豁免（开关开启前的仓，只跟新开仓）")
        return
    if _pre_startup(pp):  # MUT 闸门提前
        return""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "35 孤儿仓补建记录",
    },
    {
        "name": "M31 减仓越界到手动仓（按交易所总量减，不定我们那份）",
        "module": "alphafox_live",
        "old": """            close_qty = min(max(ex_qty - target_qty, 0.0),
                            max(our_qty - target_qty, 0.0))""",
        "new": """            close_qty = max(ex_qty - target_qty, 0.0)  # MUT 不看我们那份""",
        "script": "extra/_scenario_alphafox_live.py",
        "caught_by": "S4 混有手动仓时只减我们自己那份",
    },
    {
        "name": "M33 全局 live 模式不排除自镜像策略（误平跟单/超势纸盘仓）",
        "module": "trade_logic",
        "old": """        self_mirrored = ("alphafox", "supertrend")
        synced = [p for p in positions if p.get("strategy") not in self_mirrored]
        positions = [p for p in positions if p.get("strategy") in self_mirrored]""",
        "new": """        self_mirrored = ("alphafox", "supertrend")
        synced = positions          # MUT 不排除（全部按实盘同步处理）
        positions = []""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "33 自镜像策略纸盘仓不被误平",
    },
    {
        "name": "M34 映射表加载失败置空字典（永久 identity → 老仓孤儿）",
        "module": "alphafox_trade",
        "old": """        _map_cache = None
        print(f"[alphafox] 映射表加载失败（下次重试）: {e}")""",
        "new": """        _map_cache = {}  # MUT
        print(f"[alphafox] 映射表加载失败（下次重试）: {e}")""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "32 失败后保持未加载",
    },
    {
        "name": "M35 首轮孤儿立即平（首轮快照抖动误杀仓位）",
        "module": "alphafox_trade",
        "old": """        _orphan_pending[trader_id] = _first_round_orphans(trader_id, cur)""",
        "new": """        for _k in _first_round_orphans(trader_id, cur):  # MUT 首轮立即平
            _close_our_position(trader_id, _k, None, params)""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "11b 首轮只识别不动作",
    },
    {
        "name": "M36 残留 live 记录不收敛（重开时被当加仓 → 实盘翻倍）",
        "module": "alphafox_live",
        "old": """        if live is not None and action in ("OPEN", "ADD"):""",
        "new": """        if False:  # MUT 不收敛残留""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "39 残留记录收敛",
    },
    {
        "name": "M37 取消跟单不转非托管（仓位仍被交易员信号驱动）",
        "module": "alphafox_trade",
        "old": """        n = _batch_managed(trader_id, MANAGED_OFF)""",
        "new": """        n = 0  # MUT 不取消托管""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "40 取消跟单仓位保留但转非托管",
    },
    {
        "name": "M38 全局 live 模式直接 return（自镜像策略纸盘仓冻结 → 只减不增）",
        "module": "trade_logic",
        "old": """        if not positions:
            return  # 实盘不跑本地止盈止损逻辑""",
        "new": """        return  # MUT 实盘直接返回（自镜像策略纸盘仓冻结）""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "33 纸盘行仍被刷新",
    },
    {
        "name": "M39 豁免键不带纸盘仓 id（新仓继承豁免 → 该币永不镜像）",
        "module": "alphafox_live",
        "old": """    return f"{_key_of(pos['token'], pos.get('side') or 'LONG')}#{pos.get('id')}\"""",
        "new": """    return _key_of(pos['token'], pos.get('side') or 'LONG')  # MUT 不带 id""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "16/17/28 豁免仓语义",
    },
    {
        "name": "M40 跟单状态不校验（取消跟单后本轮仍开仓 + 写回旧基线）",
        "module": "alphafox_trade",
        "old": """    return bool(row and row["enabled"])""",
        "new": """    return True  # MUT 不校验跟单状态""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "34 取消跟单竞态",
    },
    {
        "name": "M41 落库前不校验跟单状态（取价窗口内被取消 → 裸仓）",
        "module": "alphafox_trade",
        "old": """        # 取标记价是网络调用（耗时）→ 落库前再校验一次跟单状态
        if not _is_enabled(trader_id):
            return False
        ok = storage.trade_position_insert(conn, position)""",
        "new": """        ok = storage.trade_position_insert(conn, position)  # MUT 不再校验""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "35 取价期间被取消",
    },
    {
        "name": "M42 残留收敛失败仍清 live（在未平的仓上再叠一份 → 双倍敞口）",
        "module": "alphafox_live",
        "old": """                if not _close_exchange(lc, token, live, ex_positions,
                                       "对应纸盘仓已结束，残留记录收敛"):""",
        "new": """                if False:  # MUT 收敛失败也继续开仓
                    _close_exchange(lc, token, live, ex_positions,
                                    "对应纸盘仓已结束，残留记录收敛")""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "43 收敛失败不开新仓",
    },
    {
        "name": "M43 托管状态不返回警示文案（交易员已平的红字丢失）",
        "module": "web",
        "old": """        if snap is not None and key not in snap:
            return ["已取消托管", "red", "交易员已平该仓，我们仍持仓（无退出信号）"]""",
        "new": """        if snap is not None and key not in snap:  # MUT 无警示
            return ["已取消托管", "muted", ""]""",
        "script": "extra/_check_af_board.py",
        "caught_by": "取消托管 + 交易员已平该仓 → 红字警示",
    },
    {
        "name": "M44 本轮被取消仍写回基线（重新跟单首轮按旧快照补开）",
        "module": "alphafox_trade",
        "old": """        # 本轮执行期间被取消 → 不写回（写回会抵消 set_follow 的清基线：
        # 重新跟单首轮不重建 → 按停用前的旧快照补开仓/推过期信号）
        if _is_enabled(trader_id):
            # 判为抖动的仓：基线里**保留旧值**（不能抹掉）——抹掉的话下一轮快照
            # 恢复时同一个老仓会变成"cur 有 prev 无" → 被当新开仓真下单
            _prev_snap[trader_id] = {**cur, **{k: prev[k] for k in held_back}}
        else:
            _prev_snap.pop(trader_id, None)""",
        "new": """        _prev_snap[trader_id] = cur  # MUT 不校验跟单状态""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "35 取价期间被取消",
    },
    {
        "name": "M45 平仓不看成交量（零成交也标已平 → 假平仓+裸仓）",
        "module": "alphafox_live",
        "old": """            filled = float(r.get("quantity") or 0)
            if filled <= 0:
                print(f"[af-live] {token} 实盘平仓零成交（{reason}）："
                      f"记录不收敛，下轮重试")
                return False""",
        "new": """            filled = qty  # MUT 不看成交量""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "45 零成交不标已平",
    },
    {
        "name": "M46 对账拿错记录（残留行永不改指当前纸盘仓）",
        "module": "alphafox_live",
        "old": """                    if lv.get("paper_position_id") != pp["id"]:""",
        "new": """                    if False:  # MUT 不改指（记录永挂旧纸盘 id）""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "46 对账认错记录",
    },
    {
        "name": "M47 孤儿确认失败丢候选（该仓永远没人平）",
        "module": "alphafox_trade",
        "old": """        except Exception as e:
            # 任何失败（标记价不可用、SQLite 写锁超时等）都必须把候选留回下一轮：
            # 丢了就再没人管这个仓——该 key 不在 prev 也不在 cur，三阶段永远
            # 不会再碰到它，纸盘仓和实盘镜像会无限期挂着（跟单仓没有止损）
            _orphan_pending[trader_id] = pend""",
        "new": """        except Exception as e:
            pass  # MUT 丢掉孤儿候选""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "36 孤儿确认遇价格不可用",
    },
    {
        "name": "M48 TG 推未执行成的信号（幽灵加/减/平）",
        "module": "tg_notify",
        "old": """        if not s.get("executed"):
            # 没真执行成的不推：非归属仓（多交易员同币）、未跟随仓、已平仓等
            # 都会返回 executed=False，推出去就是"幽灵加/减/平"
            continue""",
        "new": """        if False:  # MUT 未执行也推
            continue""",
        "script": "extra/_check_tg_signals.py",
        "caught_by": "TG 只推执行成的信号",
    },
    {
        "name": "M49 双向 live 记录只平一侧却全标 CLOSED（另一侧永久裸仓）",
        "module": "supertrend_live",
        "old": """    groups: dict[str, list[dict]] = {}
    for lr in live_rows:
        groups.setdefault(lr.get("side") or "LONG", []).append(lr)
    if not groups and paper_close_row:
        groups[paper_close_row.get("side") or "LONG"] = []
    for side, rows in groups.items():""",
        "new": """    _sd = (live_rows[0].get("side") if live_rows else None) or "LONG"
    if paper_close_row:
        _sd = paper_close_row.get("side") or _sd
    groups: dict[str, list[dict]] = {_sd: live_rows}
    for side, rows in groups.items():  # MUT 只按一侧收敛""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "31 双向收敛",
    },
    {
        "name": "M50 残留同向记录不比对纸盘仓 id（新仓被当'持仓消失'强平）",
        "module": "supertrend_live",
        "old": """            if live_open is not None and \\
                    live_open.get("paper_position_id") == paper_open["id"]:""",
        "new": """            if live_open is not None:  # MUT 不看 paper_position_id""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "32 残留同向记录",
    },
    {
        "name": "M51 换价撤单失败仍丢 id（孤儿限价单 → 双倍名义）",
        "module": "supertrend_live",
        "old": """        cancel_failed = False
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
            return""",
        "new": """        for key in ("limit", "sl"):  # MUT 撤单失败也丢 id
            if orders.get(key):
                try:
                    exchange.cancel_order(token, orders[key])
                except Exception:
                    pass
                orders[key] = None
        _save_orders(lc, live_pend, orders)""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "33 换价撤单失败",
    },
    {
        "name": "M52 挂单记录预置 sl_price（预挂被拒时谎报止损已挂）",
        "module": "supertrend_live",
        "old": """    orders = {"open": None, "sl": None, "sl_price": None,
              "limit": r.get("order_id"), "limit_price": price, "limit_qty": qty}""",
        "new": """    orders = {"open": None, "sl": None, "sl_price": sl,  # MUT 预置
              "limit": r.get("order_id"), "limit_price": price, "limit_qty": qty}""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "34 预挂 SL 被拒",
    },
    {
        "name": "M53 _align_open 只看 sl_price 不看 sl id（止损被拒后永不补挂）",
        "module": "supertrend_live",
        "old": """    if sl > 0 and (not orders.get("sl")
                   or abs(float(orders.get("sl_price") or 0) - sl) > 1e-9
                   or orders.get("sl_qty") is None
                   or abs(float(orders.get("sl_qty") or 0) - paper_qty)
                   > _QTY_MIN_DIFF):
        _resync_sl(paper_qty)""",
        "new": """    if sl > 0 and abs(float(orders.get("sl_price") or 0) - sl) > 1e-9:
        _resync_sl(paper_qty)  # MUT 只看 sl_price""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "35 历史记录补挂止损",
    },
    {
        "name": "M54 双向持仓也合并（空单并进多单，吞掉一侧盈亏）",
        "module": "supertrend_strategy",
        "old": """    if len({r.get("side") or "LONG" for r in rows}) > 1:
        print(f"[supertrend] {token} 同时存在多空双向持仓，跳过合并（请人工处理）")
        return""",
        "new": """    pass  # MUT 双向也合并""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "36 双向持仓不合并",
    },
    {
        "name": "M55 宏观事件翻译不兜底（表外事件名显示成空白）",
        "module": "dashboard",
        "old": """    for pat, tpl in _MACRO_EVENT_CN_RULES:
        m = pat.match(name.strip())
        if m:
            return tpl.format(*m.groups())
    return name""",
        "new": """    for pat, tpl in _MACRO_EVENT_CN_RULES:
        m = pat.match(name.strip())
        if m:
            return tpl.format(*m.groups())
    return ""  # MUT 不兜底""",
        "script": "extra/_check_macro_cn.py",
        "caught_by": "未知事件名原样返回英文",
    },
    {
        "name": "M56 核心判定吃中文译名（关键词表全部失效）",
        "module": "dashboard",
        "old": """            tag = "核心" if _is_core_event(name) else "观察\"""",
        "new": """            tag = "核心" if _is_core_event(_event_cn(name)) else "观察\"""",
        "script": "extra/_check_macro_cn.py",
        "caught_by": "核心标记仍按英文原名给出",
    },
    {
        "name": "M57 对账无容忍带（差 1 个 step 也真成交）",
        "module": "alphafox_live",
        "old": """    band = _REBAL_MIN_RATIO * target_qty
    _step = _step_of(token)
    if _step > 0:
        band = max(band, 1.5 * _step)""",
        "new": """    band = 0.0  # MUT 无容忍带""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "48 差 1 个 step 不补仓",
    },
    {
        "name": "M58 对账成交不记账（实盘少了钱查不到出处）",
        "module": "alphafox_live",
        "old": """            storage.live_trade_log_insert(lc, {
                "token": token, "strategy": "alphafox",
                "action": "rebalance_reduce", "price": mark, "quantity": -diff,""",
        "new": """            storage.live_trade_log_insert(lc, {
                "token": token, "strategy": "alphafox",
                "action": "MUT_NOLOG", "price": mark, "quantity": -diff,""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "48 对账成交写进 live_trade_log",
    },
    {
        "name": "M59 跟单减份不记账",
        "module": "alphafox_live",
        "old": """                storage.live_trade_log_insert(lc, {
                    "token": token, "strategy": "alphafox",
                    "action": "reduce",""",
        "new": """                storage.live_trade_log_insert(lc, {
                    "token": token, "strategy": "alphafox",
                    "action": "MUT_NOLOG",""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "49 加/减份记账",
    },
    {
        "name": "M60 容忍带不随目标缩放（目标大时吞掉真变动）",
        "module": "alphafox_live",
        "old": """    band = _REBAL_MIN_RATIO * target_qty""",
        "new": """    band = 0.05  # MUT 写死绝对值""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "50 目标 30 时差 3.0（10%）仍要补",
    },
    {
        "name": "M61 页面聚合不做类型转换（字符串 pnl → 页面静默定格）",
        "module": "web",
        "old": """            _p = _af_num(it.get("pnl"))
            a["pnl"] += _p
            if _p > 0:
                a["wins"] += 1
            elif _p < 0:
                a["losses"] += 1""",
        "new": """            a["pnl"] += it.get("pnl") or 0  # MUT 不转换
            if (it.get("pnl") or 0) > 0:
                a["wins"] += 1
            elif (it.get("pnl") or 0) < 0:
                a["losses"] += 1""",
        "script": "extra/_check_af_payload.py",
        "caught_by": "脏数据聚合不抛异常",
    },
    {
        "name": "M62 快照解析无逐条隔离（一条脏数据废掉整个交易员）",
        "module": "alphafox_trade",
        "old": """        except (AttributeError, TypeError, ValueError) as e:
            degraded = True
            print(f"[alphafox] {label} 持仓项解析失败（跳过该条，本轮不平仓）: {e} "
                  f"{str(p)[:120]}")""",
        "new": """        except ():  # MUT 不隔离，异常抛给调用方
            print(f"[alphafox] {label} 持仓项解析失败: {e}")""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "37 上游脏数据隔离",
    },
    {
        "name": "M63 快照 key 用原始拼写（上游换写法就平仓重开）",
        "module": "alphafox_trade",
        "old": """            key = (f"{_token_of(raw_sym)}USDT", side)""",
        "new": """            key = (raw_sym, side)  # MUT 用原始拼写""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "38 上游换拼写不换手",
    },
    {
        "name": "M64 对齐写回无状态守卫（并发平仓后被回写污染已实现盈亏）",
        "module": "alphafox_trade",
        "old": """                "updated_at=CURRENT_TIMESTAMP WHERE id=? "
                "AND status IN ('OPEN','PARTIAL')" + _ACT_GUARD_ALIGN,
                (new_total, new_entry, new_open * new_entry,
                 new_open * new_entry / lev, trade_price, pos["id"]))
            if cur.rowcount == 0:""",
        "new": """                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (new_total, new_entry, new_open * new_entry,
                 new_open * new_entry / lev, trade_price, pos["id"]))
            if False:""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "39 并发平仓不回写",
    },
    {
        "name": "M65 映射表不可用仍处理（静默丢弃退出信号）",
        "module": "alphafox_trade",
        "old": """    if _map_cache is None:
        reload_symbol_map()
    if _map_cache is None:
        print(f"[alphafox] {trader_id} 映射表不可用，本轮跳过（不推进基线）")
        return []""",
        "new": """    pass  # MUT 映射表不可用也照常处理""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "40 映射表不可用跳过本轮",
    },
    {
        "name": "M66 条件单查错端点（实盘页挂单永远空）",
        "module": "exchange",
        "old": """    r = _signed_request("GET", "/fapi/v1/openAlgoOrders", params)""",
        "new": """    r = _signed_request("GET", "/fapi/v1/algoOpenOrders", params)  # MUT""",
        "script": "extra/_check_exchange_orders.py",
        "caught_by": "条件单用正确端点",
    },
    {
        "name": "M67 非 JSON 错误页直接抛（异常把整页挂单数据吃掉）",
        "module": "exchange",
        "old": """            body = e.read().decode("utf-8", "replace")
            try:
                err = json.loads(body)
                code = err.get("code", 0)
                msg = err.get("msg", "")
            except (json.JSONDecodeError, ValueError):
                code, msg = 0, body.strip()[:80]
                print(f"[exchange] {method} {path} HTTP {e.code} 非 JSON 错误页: "
                      f"{msg}")""",
        "new": """            body = e.read().decode("utf-8", "replace")
            err = json.loads(body)  # MUT 非 JSON 直接抛
            code = err.get("code", 0)
            msg = err.get("msg", "")""",
        "script": "extra/_check_exchange_orders.py",
        "caught_by": "非 JSON 不抛异常",
    },
    {
        "name": "M68 错误信息不落全局（所有失败原因打印为空）",
        "module": "exchange",
        "old": """    global _last_error_msg     # 少了这行，下面所有赋值都只写局部变量 →""",
        "new": """    pass  # MUT 不声明 global""",
        "script": "extra/_check_exchange_orders.py",
        "caught_by": "错误信息被记录",
    },
    # 注：Python 层的两个托管守卫（_close_our_position / _align_our_position）与
    # SQL 层的 _ACT_GUARD_* 是双重保护，单独去掉任一层的行为由 SQL 层兜住 →
    # 只对"取价之前提前返回"这一点做变异（M70），SQL 层由 M82/M83 覆盖
    {
        "name": "M70 非托管仓也去取价（无价时整轮 retry 拖住基线）",
        "module": "alphafox_trade",
        "old": """        if _managed(pos) not in (MANAGED_ON, MANAGED_REATTACH):""",
        "new": """        if False:  # MUT 非托管也走取价/加减仓""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "44 非托管+无价不 retry",
    },
    {
        "name": "M71 恢复托管时交易员已无该仓 → 直接平掉（决策1：应只标记已脱离）",
        "module": "alphafox_trade",
        "old": """            _reattach_missing.pop(mk, None)
            if _set_managed(pos["id"], MANAGED_UNLINKED, MANAGED_REATTACH):""",
        "new": """            _reattach_missing.pop(mk, None)  # MUT 交易员已无 → 直接平掉
            _set_managed(pos["id"], MANAGED_ON, MANAGED_REATTACH)
            _close_our_position(trader_id, key, None, params)
            if False:""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "41h 交易员已平的仓 → 已脱离（不平）",
    },
    {
        "name": "M72 重新跟单不回置恢复托管（仓位永远停在非托管）",
        "module": "alphafox_trade",
        "old": """    n = _batch_managed(trader_id, MANAGED_REATTACH,
                       (MANAGED_OFF, MANAGED_UNLINKED))""",
        "new": """    n = 0  # MUT 不回置恢复托管""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "41g 批量重新跟单：2 笔进恢复中",
    },
    {
        "name": "M73 非托管仓仍走对账对齐（下单改用户仓位）",
        "module": "alphafox_live",
        "old": """                if _managed_of(pp) != MANAGED_ON:
                    # 非托管（取消托管/恢复中/已脱离）：不下任何单、不做数量对齐，
                    # 只把实盘记录同步成交易所事实（用户手动平/加减仓后页面不撒谎）
                    _reconcile_unmanaged(lc, pp, ex_positions)
                    continue""",
        "new": """                if False:  # MUT 非托管也镜像/对齐
                    _reconcile_unmanaged(lc, pp, ex_positions)
                    continue""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "51 非托管：不做数量对齐、不下单",
    },
    {
        "name": "M74 非托管仓交易所空了只收尾实盘记录（纸盘留幽灵）",
        "module": "alphafox_live",
        "old": """        cur = conn.execute(
            "UPDATE trade_positions SET status='CLOSED', closed_qty=?, "
            "realized_pnl=?, unrealized_pnl=0, pnl_pct=?, advice=?, "
            "current_price=?, closed_at=CURRENT_TIMESTAMP, "
            "updated_at=CURRENT_TIMESTAMP WHERE id=? "
            "AND status IN ('OPEN','PARTIAL')",
            (qty, realized, realized / margin * 100, advice, mark, pos["id"]))""",
        "new": """        cur = conn.execute("SELECT 1")  # MUT 纸盘不收尾（幽灵）""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "51 交易所空了：纸盘仓一起标平",
    },
    {
        "name": "M75 恢复托管不解豁免（该币实盘此后永不镜像）",
        "module": "alphafox_live",
        "old": """    _exempt_remove_token_side(token, side)
    storage.init_onchain_live_db()""",
        "new": """    pass  # MUT 不解豁免
    storage.init_onchain_live_db()""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "52 恢复托管：解除豁免",
    },
    {
        "name": "M76 恢复中(2)的仓被当'手动平仓'收尾（恢复动作静默回滚）",
        "module": "alphafox_live",
        "old": """        if _managed_of(pos) not in (MANAGED_OFF, MANAGED_UNLINKED):
            return   # 托管中(1)/恢复中(2)：不走"手动平仓"收尾""",
        "new": """        if _managed_of(pos) == MANAGED_ON:  # MUT 恢复中也收尾
            return""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "53 恢复中：纸盘仓不被收尾",
    },
    {
        "name": "M81 孤儿判定不归一 symbol（存量 BTC 行被误判孤儿平仓）",
        "module": "alphafox_trade",
        "old": """        key = _pos_key(pos)""",
        "new": """        key = (pos.get("symbol"), pos.get("side"))  # MUT 不归一""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "42 拼写不同的存量仓不被当孤儿",
    },
    {
        "name": "M82 平仓写入无托管守卫（取价窗口内被取消托管仍平仓）",
        "module": "alphafox_trade",
        "old": """_ACT_GUARD_ON = f" AND IFNULL(managed,{MANAGED_ON})={MANAGED_ON}\"""",
        "new": """_ACT_GUARD_ON = ""  # MUT 无守卫""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "43 平仓窗口被取消托管仍落库",
    },
    {
        "name": "M83 对齐写入无托管守卫（取价窗口内被取消托管仍加减仓）",
        "module": "alphafox_trade",
        "old": """_ACT_GUARD_ALIGN = (f" AND IFNULL(managed,{MANAGED_ON})"
                    f" IN ({MANAGED_ON},{MANAGED_REATTACH})")""",
        "new": """_ACT_GUARD_ALIGN = ""  # MUT 无守卫""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "43 加仓窗口被取消托管仍落库",
    },
    {
        "name": "M84 恢复托管缺失单轮即判已脱离（单条脏快照永久脱管）",
        "module": "alphafox_trade",
        "old": """            miss = _reattach_missing.get(mk, 0) + 1
            if miss < 2:""",
        "new": """            miss = _reattach_missing.get(mk, 0) + 1
            if False:  # MUT 单轮即确认""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "41d 单轮缺失不落已脱离",
    },
    {
        "name": "M85 托管页 join 不归一 symbol（实盘列显示'—'）",
        "module": "web",
        "old": """        key = alphafox_trade._pos_key(pos)      # 归一化：老行 symbol 拼写不一""",
        "new": """        key = (pos.get("symbol"), pos.get("side"))  # MUT 不归一""",
        "script": "extra/_check_af_board.py",
        "caught_by": "DDD 行 symbol 未归一也能 join",
    },
    {
        "name": "M86 时间不转北京时间（sqlite3 不做 TIMESTAMP 解析，显示早 8 小时）",
        "module": "web",
        "old": """        return str(datetime.fromisoformat(str(v).replace("Z", ""))
                   + timedelta(hours=8))""",
        "new": """        return str(datetime.fromisoformat(str(v).replace("Z", "")))  # MUT 不转时区""",
        "script": "extra/_check_af_board.py",
        "caught_by": "历史时间按北京时间输出",
    },
    {
        "name": "M87 manage 缺省 managed 当成取消托管（少传字段静默停托管）",
        "module": "web",
        "old": """    if "managed" not in body:
        # 缺省必须报错：bool(None)=False 会把少传字段的请求静默当成"取消托管"
        raise HTTPException(400, "managed 必填（true=恢复托管 / false=取消托管）")""",
        "new": """    if False:  # MUT 缺省当成取消托管
        raise HTTPException(400, "managed 必填")""",
        "script": "extra/_check_af_board.py",
        "caught_by": "manage 缺省 managed 报错",
    },
    {
        "name": "M77 恢复托管不校验交易员跟单状态（永远卡在恢复中）",
        "module": "alphafox_trade",
        "old": """        if not trader_id or not _is_enabled(trader_id):
            return {"ok": False,
                    "msg": "该交易员的跟单已关闭，请先在跟单雷达开启跟单再恢复托管"}""",
        "new": """        if False:  # MUT 不校验跟单状态
            return {"ok": False, "msg": ""}""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "41f 交易员未跟单：拒绝恢复托管",
    },
    {
        "name": "M78 批量托管不筛归属（取消 A 的跟单连带清掉 B 的仓）",
        "module": "alphafox_trade",
        "old": """        ids = [r["id"] for r in rows
               if _owned_by(r, trader_id)
               and (only_in is None or _managed(r) in only_in)]""",
        "new": """        ids = [r["id"] for r in rows  # MUT 不筛归属
               if (only_in is None or _managed(r) in only_in)]""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "42 取消跟单只清该交易员的仓",
    },
    {
        "name": "M79 非托管仓不做事实同步（记录与交易所长期不一致）",
        "module": "alphafox_live",
        "old": """            ex_qty = abs(float(ex_row.get("quantity") or 0))
            if abs(float(lv.get("quantity") or 0) - ex_qty) > _QTY_MIN_DIFF:
                storage.live_position_update(lc, lv["id"], {"quantity": ex_qty})
                print(f"[af-live] {token} 非托管仓：实盘数量同步为 {ex_qty:.6g}"
                      f"（记录 ← 交易所，不下单）")""",
        "new": """            pass  # MUT 不做事实同步""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "51 非托管：交易所手动加仓 → 记录事实同步",
    },
    {
        "name": "M80 托管页不带跟单员归属（页面看不出是谁的仓）",
        "module": "web",
        "old": """            "trader_label": labels.get(tid) or tid or "—",
            "managed": managed, "state": state, "state_cls": cls, "warn": warn,""",
        "new": """            "trader_label": "", "managed": managed,
            "state": state, "state_cls": cls, "warn": warn,  # MUT 无归属""",
        "script": "extra/_check_af_board.py",
        "caught_by": "board 持仓带跟单员名",
    },
    {
        "name": "M88 托管状态变更不清缺失计数（正常启停后单轮就落已脱离）",
        "module": "alphafox_trade",
        "old": """    for _id in ids:
        _reattach_missing.pop((trader_id, _id), None)
    return len(ids)""",
        "new": """    return len(ids)  # MUT 不清计数""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "45 启停后第一轮缺失不落已脱离",
    },
    {
        "name": "M89 恢复托管不做竞态复查（仓停在'恢复中'死状态）",
        "module": "alphafox_trade",
        "old": """    if not _is_enabled(trader_id):
        _set_managed(position_id, MANAGED_OFF, MANAGED_REATTACH)
        return {"ok": False,
                "msg": "该交易员刚刚被取消跟单，该仓保持'取消托管'"}""",
        "new": """    if False:  # MUT 不复查
        return {"ok": False, "msg": ""}""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "46 竞态不留死状态",
    },
    {
        "name": "M90 恢复托管不冻结比例 k（下一轮按旧目标把手动改动抹掉）",
        "module": "alphafox_live",
        "old": """        orders["k"] = round(k, 12)
        storage.live_position_update(lc, lv["id"], {
            "exchange_orders_json": json.dumps(orders)})""",
        "new": """        pass  # MUT 不冻结 k""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "52 恢复托管：k 按现状冻结",
    },
    {
        "name": "M91 apply_signal 不校验托管（在飞信号给未托管仓下单）",
        "module": "alphafox_live",
        "old": """            if _managed_of(ppos) != MANAGED_ON:
                # 纸盘写入与实盘下单之间隔着若干网络调用：用户在这窗口里点了
                # "取消托管/取消跟单" → 不下单（与纸盘层守卫对称）
                print(f"[af-live] {token} 跳过镜像：该仓已取消托管")
                return""",
        "new": """            if False:  # MUT 不校验托管
                return""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "56 未托管不镜像",
    },
    {
        "name": "M92 减仓不看成交回报（零成交也记账 → 份数漂移）",
        "module": "alphafox_live",
        "old": """                filled = float(r.get("quantity") or 0)
                if filled <= 0:
                    # 零成交（被拒/EXPIRED 但返回 200）：按委托量记账会让份数漂移
                    # 甚至把 live 记录标 CLOSED 而实盘还在（假平仓+裸仓）
                    print(f"[af-live] {token} 实盘减仓零成交，本轮不改记账（下轮重试）")
                    return""",
        "new": """                filled = close_qty  # MUT 不看成交量
                if False:
                    return""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "57 减仓零成交不记账",
    },
    {
        "name": "M93 兜底平仓不看成交（零成交也把记录标平 → 裸仓）",
        "module": "web",
        "old": """            if float((r or {}).get("quantity") or 0) <= 0:
                closed_ok = False""",
        "new": """            if False:  # MUT 不看成交量
                closed_ok = False""",
        "script": "extra/_check_live_close.py",
        "caught_by": "零成交：live 记录保持 OPEN",
    },
    {
        "name": "M94 平仓接口跳过交易所可用性探测（查询失败仍当成功）",
        "module": "web",
        "old": """        ex_probe = exchange.get_positions()
        api_ok = isinstance(ex_probe, list) and not exchange.get_last_error()""",
        "new": """        api_ok = True  # MUT 不探测""",
        "script": "extra/_check_live_close.py",
        "caught_by": "查询失败：抛出 HTTPException(503)",
    },
    {
        "name": "M95 账户取数失败也标 ok（页面显示 $0 且无提示）",
        "module": "web",
        "old": """        acct_ok = bool(bal)""",
        "new": """        acct_ok = True  # MUT 静默失败也当成功""",
        "script": "extra/_check_live_close.py",
        "caught_by": "账户静默失败 + 余额为 0：ok=False",
    },
    {
        "name": "M96 恢复托管只看旧记录量（同一窗口的手动改动被下一轮抹掉）",
        "module": "alphafox_live",
        "old": """        if ex_row and tq > 0:
            ex_qty = abs(float(ex_row.get("quantity") or 0))
            if ex_qty > 0:
                k = ex_qty / tq""",
        "new": """        if False and ex_row and tq > 0:  # MUT 只看记录量
            ex_qty = abs(float(ex_row.get("quantity") or 0))
            if ex_qty > 0:
                k = ex_qty / tq""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "55 记录未同步时按交易所现状",
    },
    {
        "name": "M97 兜底未成交也返回 ok（页面弹'已平仓'而仓还在）",
        "module": "web",
        "old": """        return {"ok": False, "token": token, "handled": handled,
                "detail": "平仓单未全部成交（交易所已持仓查询正常），"
                          "已跳过记录收敛，请稍后重试或在币安确认"}""",
        "new": """        return {"ok": True, "token": token, "handled": handled}  # MUT 假装成功""",
        "script": "extra/_check_live_close.py",
        "caught_by": "零成交：接口如实返回 ok=False",
    },
    {
        "name": "M98 孤儿候选只在价格异常时保住（DB 写锁即永久丢退出信号）",
        "module": "alphafox_trade",
        "old": """        except Exception as e:
            # 任何失败（标记价不可用、SQLite 写锁超时等）都必须把候选留回下一轮：
            # 丢了就再没人管这个仓——该 key 不在 prev 也不在 cur，三阶段永远
            # 不会再碰到它，纸盘仓和实盘镜像会无限期挂着（跟单仓没有止损）
            _orphan_pending[trader_id] = pend""",
        "new": """        except _DataUnavailable as e:  # MUT 只保价格异常
            _orphan_pending[trader_id] = pend""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "47 非价格异常候选保住",
    },
    {
        "name": "M99 marginBalance 裸 float（脏值废掉整个交易员整轮）",
        "module": "alphafox_trade",
        "old": """    try:
        # 上游会发脏值（实测有 "1,000.50" 这类写法）：裸 float() 抛异常会让本轮
        # 整个不处理（连交易员的平仓信号一起丢掉）。转成 0 只会挡"开新仓"
        # （开仓需要保证金余额），平仓/对齐照常
        margin_balance = float(data.get("marginBalance") or 0)
    except (TypeError, ValueError):
        margin_balance = 0.0
        print(f"[alphafox] {trader_id} marginBalance 异常"
              f"（{data.get('marginBalance')!r}）→ 本轮按 0 处理（只影响开新仓）")""",
        "new": """    margin_balance = float(data.get("marginBalance") or 0)  # MUT 不兜底""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "48 脏 marginBalance 不废整轮",
    },
    {
        "name": "M100 批量重新跟单不复查（与取消跟单竞态后停在恢复中）",
        "module": "alphafox_trade",
        "old": """    if n and not _is_enabled(trader_id):""",
        "new": """    if False:  # MUT 不复查""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "49 批量竞态不留死状态",
    },
    {
        "name": "M101 补建记录写死数量 1.0（下一轮把实盘镜像砍到 1）",
        "module": "alphafox_live",
        "old": """            "quantity": ex_qty,
            "leverage": lev,
            "margin_usd": ex_qty * float(ex_row.get("mark_price") or mark) / lev,""",
        "new": """            "quantity": 1.0,  # MUT 写死
            "leverage": lev,
            "margin_usd": ex_qty * float(ex_row.get("mark_price") or mark) / lev,""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "58 补建数量按交易所现状",
    },
    {
        "name": "M102 豁免清单不过滤已有镜像的仓（开关关再开后加仓不再跟）",
        "module": "web",
        "old": """                keys = [f"{r['token']}|{r['side']}#{r['id']}" for r in rows
                        if r["id"] not in mirrored]""",
        "new": """                keys = [f"{r['token']}|{r['side']}#{r['id']}" for r in rows]  # MUT 不筛""",
        "script": "extra/_check_exempt_recorder.py",
        "caught_by": "已有实盘镜像的不入豁免清单",
    },
    {
        "name": "M103 单笔上限不从设置读（写死 0 = 上限形同虚设）",
        "module": "web",
        "old": """                live_notional = float(ts.get("alphafox_live_max_notional") or 0)""",
        "new": """                live_notional = 0.0  # MUT 不读设置""",
        "script": "extra/_check_exempt_recorder.py",
        "caught_by": "探测线程从设置读「单笔上限」并传给对账",
    },
    {
        "name": "M104 遗留表 DELETE 不兜底（全新库/换库时 init_db 崩、起不来）",
        "module": "storage",
        "old": """    try:
        conn.execute("DELETE FROM round_candidates")
    except sqlite3.OperationalError:
        pass""",
        "new": """    conn.execute("DELETE FROM round_candidates")  # MUT 不兜底""",
        "script": "extra/_check_storage_init.py",
        "caught_by": "全新库首次 init_db 不报错",
    },
    {
        "name": "M105 快照降级仍按'交易员已平仓'平仓（字段一改名就误平真仓）",
        "module": "alphafox_trade",
        "old": """        for key in (set() if degraded else sorted(set(prev) - set(cur))):""",
        "new": """        for key in sorted(set(prev) - set(cur)):  # MUT 降级也平仓""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "50 快照降级不平仓",
    },
    {
        "name": "M106 absQty 缺失当 0（走'减仓至零'把真仓平掉）",
        "module": "alphafox_trade",
        "old": """            if p.get("absQty") is None:
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
                qty = float(p.get("absQty"))""",
        "new": """            qty = float(p.get("absQty") or 0)  # MUT 缺失当 0""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "50 缺 absQty 沿用上轮数量",
    },
    {
        "name": "M108 孤儿确认平仓不产出信号（TG/日志全静默）",
        "module": "alphafox_trade",
        "old": """                        _log_signal(st_conn, sig)
                        signals.append(sig)""",
        "new": """                        _log_signal(st_conn, sig)  # MUT 不产出信号""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "52 孤儿平仓产出 CLOSE 信号",
    },
    {
        "name": "M109 retry 轮把已成交信号也标 retry（TG 永久漏报真成交）",
        "module": "alphafox_trade",
        "old": """        for s in signals:
            if not s.get("executed"):
                s["retry"] = True""",
        "new": """        for s in signals:
            s["retry"] = True  # MUT 全标""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "53 已执行信号不带 retry",
    },
    {
        "name": "M110 恢复托管缺 k_qty 也标托管中（页面显示在跟、实际永不动作）",
        "module": "alphafox_trade",
        "old": """        try:
            if float(json.loads(pos.get("signal_snapshot") or "{}")
                     .get("k_qty") or 0) <= 0:""",
        "new": """        try:
            if False:  # MUT 缺 k_qty 也当托管中""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "54 缺 k_qty 标已脱离",
    },
    {
        "name": "M111 复用 live 行重开时清零 realized（上一段真实盈亏从统计消失）",
        "module": "alphafox_live",
        "old": """        vals.update({"closed_at": None, "created_at": "__CURRENT_TIMESTAMP__"})""",
        "new": """        vals.update({"closed_at": None, "realized_pnl": 0,
                     "created_at": "__CURRENT_TIMESTAMP__"})  # MUT 清零""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "59 复用重开不清零 realized",
    },
    {
        "name": "M112 交易所持仓消失收敛时不估算 realized（0 混进胜率）",
        "module": "alphafox_live",
        "old": """        _e = float(live.get("entry_price") or 0)
        _q = float(live.get("quantity") or 0)
        _px = float(pp.get("current_price") or 0)
        _r = (((_e - _px) if side == "SHORT" else (_px - _e)) * _q
              if (_e > 0 and _px > 0) else 0.0)""",
        "new": """        _r = 0.0  # MUT 不估算""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "60 收敛时写 realized 估算",
    },
    {
        "name": "M113 兜底平仓不估算 realized（真实平仓按 0 计）",
        "module": "web",
        "old": """                _r = (((_e - _px) if (lr.get("side") or "LONG") == "SHORT"
                       else (_px - _e)) * _q if (_e > 0 and _px > 0) else 0.0)""",
        "new": """                _r = 0.0  # MUT 不估算""",
        "script": "extra/_check_live_close.py",
        "caught_by": "成交：写了 realized 估算",
    },
    {
        "name": "M114 空单实盘数量不取绝对值（页面显示负数）",
        "module": "web",
        "old": """            "ex_quantity": (abs(float(ex["quantity"]))
                            if ex.get("quantity") is not None else None),""",
        "new": """            "ex_quantity": ex.get("quantity"),  # MUT 不取绝对值""",
        "script": "extra/_check_af_board.py",
        "caught_by": "空单实盘数量显示为绝对值",
    },
    {
        "name": "M115 交易所取数失败时实盘口径给 0（页面与账户区互相矛盾）",
        "module": "web",
        "old": """    _ex_ok = bool(_acct.get("ok", True))""",
        "new": """    _ex_ok = True  # MUT 失败也给 0""",
        "script": "extra/_check_af_board.py",
        "caught_by": "交易所取数失败给 None",
    },
    {
        "name": "M116 探测轮次失败不置回 _inited（DB 丢失后永久停摆）",
        "module": "web",
        "old": """            _inited = False
            print(f"[alphafox] 探测轮次准备失败（跳过本轮，下轮重建）: {e}")""",
        "new": """            print(f"[alphafox] 探测轮次准备失败（跳过本轮，下轮重建）: {e}")  # MUT""",
        "script": "extra/_check_exempt_recorder.py",
        "caught_by": "失败后下轮重建（_inited 置回）",
    },
    {
        "name": "M117 交易员减到零仍按'减'上报（用户以为还剩一部分）",
        "module": "alphafox_trade",
        "old": """                if not _st:
                    sig["action"] = "CLOSE"
            _log_signal(st_conn, sig)""",
        "new": """                if False:  # MUT 减到零仍报"减"
                    sig["action"] = "CLOSE"
            _log_signal(st_conn, sig)""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "25 信号按'平'上报",
    },
    {
        "name": "M118 反转市价容忍判定失效（超容忍也市价追高）",
        "module": "supertrend_strategy",
        "old": """    elif flip_open and flip_tol_pct > 0 and ref_usable:""",
        "new": """    elif False:  # MUT 容忍判定失效""",
        "script": "extra/test_supertrend_logic.py",
        "caught_by": "32b 超容忍只挂单",
    },
    {
        "name": "M122 留痕写回 advice（1s 循环会秒冲掉）",
        "module": "supertrend_strategy",
        "old": """        "open_reason": (f"超级趋势 15m {sig['trend']} 挂{'多' if side == 'LONG' else '空'}单"
                        + (f"，{note}" if note else "")),
        "advice": f"挂单中：{'多' if side == 'LONG' else '空'} @ ${limit_price:.6g}，止损 @ ${sl:.6g}",""",
        "new": """        "open_reason": f"超级趋势 15m {sig['trend']} 挂{'多' if side == 'LONG' else '空'}单",
        "advice": (f"挂单中：{'多' if side == 'LONG' else '空'} @ ${limit_price:.6g}，"
                   f"止损 @ ${sl:.6g}" + (f"，{note}" if note else "")),""",
        "script": "extra/_check_flip_wiring.py",
        "caught_by": "1s 循环后留痕仍在",
    },
    {
        "name": "M123 基准不校验新鲜度（拿几小时前的旧轨道当闸门）",
        "module": "supertrend_strategy",
        "old": """    ref_usable = (old_row is not None and old_ts is not None
                  and old_ts == sig["ts"] - CANDLE_MS)""",
        "new": """    ref_usable = old_row is not None  # MUT 不校验基准新鲜度""",
        "script": "extra/_check_flip_wiring.py",
        "caught_by": "基准过期按不设限",
    },
    {
        "name": "M124 trading_settings defaults 漏注册新键",
        "module": "storage",
        "old": """        "supertrend_order_tolerance_pct": getattr(config, "SUPERTREND_ORDER_TOLERANCE_PCT", 1.0),
        "supertrend_flip_tolerance_pct": getattr(config, "SUPERTREND_FLIP_TOLERANCE_PCT", 0.0),""",
        "new": """        "supertrend_order_tolerance_pct": getattr(config, "SUPERTREND_ORDER_TOLERANCE_PCT", 1.0),""",
        "script": "extra/_check_flip_wiring.py",
        "caught_by": "defaults 含该键",
    },
    {
        "name": "M119 参考线取错轨道（翻多取了下轨）",
        "module": "supertrend_strategy",
        "old": """        flip_ref = float(old_row["upper_band" if old_trend == "DOWN"
                                 else "lower_band"] or 0)""",
        "new": """        flip_ref = float(old_row["lower_band" if old_trend == "DOWN"
                                 else "upper_band"] or 0)  # MUT 取反轨道""",
        "script": "extra/test_supertrend_logic.py",
        "caught_by": "32a 容忍内市价开",
    },
    {
        "name": "M120 跳过分支仍强制市价（等于没有容忍）",
        "module": "supertrend_strategy",
        "old": """            ok = _place_order(conn, token, sig, price, tol_order_pct, tol_sl_pct,
                              notional, leverage, note=flip_note)""",
        "new": """            ok = _place_order(conn, token, sig, price, tol_order_pct, tol_sl_pct,
                              notional, leverage, force_market=True, note=flip_note)  # MUT""",
        "script": "extra/test_supertrend_logic.py",
        "caught_by": "32b 超容忍无市价仓",
    },
    {
        "name": "M121 跳过原因不写进挂单（页面无留痕）",
        "module": "supertrend_strategy",
        "old": """            ok = _place_order(conn, token, sig, price, tol_order_pct, tol_sl_pct,
                              notional, leverage, note=flip_note)""",
        "new": """            ok = _place_order(conn, token, sig, price, tol_order_pct, tol_sl_pct,
                              notional, leverage)  # MUT 不传 note""",
        "script": "extra/test_supertrend_logic.py",
        "caught_by": "32b advice 留痕",
    },
    {
        "name": "M125 并存窗口误判挂单成交（只看交易所有持仓）",
        "module": "supertrend_live",
        "old": """    if live_pend and ex_row and filled:""",
        "new": """    if live_pend and ex_row:  # MUT 只看持仓""",
        "script": "extra/test_double_sl.py",
        "caught_by": "⑥ 并存未成交不动",
    },
    {
        "name": "M128 部分成交当没成交（SL 停在挂单量、与实际持仓不符）",
        "module": "supertrend_live",
        "old": """            return (exq > 0), exq""",
        "new": """            return False, 0.0  # MUT 部分成交当没成交""",
        "script": "extra/test_coexist_sync.py",
        "caught_by": "1-h 部分成交判已成交",
    },
    {
        "name": "M129 成交分支用整仓量（不复用这条挂单自己的量）",
        "module": "supertrend_live",
        "old": """        if leg_qty <= 0:                      # 整张成交但记录里没存 limit_qty 的兜底
            leg_qty = abs(float(ex_row.get("quantity") or qty))""",
        "new": """        leg_qty = abs(float(ex_row.get("quantity") or qty))  # MUT 用整仓量""",
        "script": "extra/test_double_sl.py",
        "caught_by": "②-a SL 不超额 / ⑧-a 记录量",
    },
    {
        "name": "M130 同向重复 OPEN 记录不收敛（实盘重复记账）",
        "module": "supertrend_live",
        "old": """                for r in dups:""",
        "new": """                for r in []:  # MUT 不收敛重复记录""",
        "script": "extra/test_double_sl.py",
        "caught_by": "②-b 只剩 1 条 OPEN",
    },
    {
        "name": "M131 收敛重复记录前不做 SL 交接（总覆盖变欠额）",
        "module": "supertrend_live",
        "old": """            if dups and not _handover_sl(lc, token, preferred, side):""",
        "new": """            if dups and not True:  # MUT 不交接 SL 覆盖量""",
        "script": "extra/test_double_sl.py",
        "caught_by": "②-a SL 总覆盖",
    },
    {
        "name": "M126 孤儿挂单记录不收敛（留僵尸 PENDING）",
        "module": "supertrend_live",
        "old": """    if (_is_orphan_live_pend(paper_pend["id"] if paper_pend else None, live_pend)
            and live_open is not None):""",
        "new": """    if False:  # MUT 不收敛孤儿""",
        "script": "extra/test_coexist_sync.py",
        "caught_by": "4.1-d 无僵尸",
    },
    {
        "name": "M127 合并后不回写记录数量（合法减仓被误跳过）",
        "module": "supertrend_live",
        "old": """    if not qty_changed and _need_qty_writeback(""",
        "new": """    if False and _need_qty_writeback(  # MUT 不回写""",
        "script": "extra/test_coexist_sync.py",
        "caught_by": "4.1-e / 4.5 记录数量",
    },
    {
        "name": "M132 合并时 extras 不复查状态（keep 数量永久虚增）",
        "module": "supertrend_strategy",
        "old": """    extras = []
    for r in rows[1:]:
        cur_row = conn.execute(
            "SELECT * FROM trade_positions WHERE id=? AND "
            "status IN ('OPEN','PARTIAL')", (r["id"],)).fetchone()
        if cur_row:
            extras.append(dict(cur_row))""",
        "new": """    extras = rows[1:]  # MUT 不复查状态""",
        "script": "extra/test_supertrend_logic.py",
        "caught_by": "34 合并竞态",
    },
    {
        "name": "M133 参考线不可用时 fail-open（闸门在最该生效时失效）",
        "module": "supertrend_strategy",
        "old": """    if flip_open and flip_tol_pct > 0 and not ref_usable:""",
        "new": """    if False:  # MUT 参考线不可用就放行""",
        "script": "extra/test_supertrend_logic.py",
        "caught_by": "33 参考线不可用只挂单",
    },
    {
        "name": "M134 挂单处理不过跟单占用闸门（成交分支照撤单/重挂 SL）",
        "module": "supertrend_live",
        "old": """    if _alphafox_occupied(token):
        print(f"[st-live] {token} 挂单处理跳过：被跟单实盘占用（释放后自动接管）")
        return""",
        "new": """    if False:  # MUT 占用闸门失效
        return""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "38 占用期间不碰交易所",
    },
    {
        "name": "M135 重复记录收敛不过跟单占用闸门",
        "module": "supertrend_live",
        "old": """    if ((paper_open is not None or paper_pend is not None)
            and not _alphafox_occupied(token)):""",
        "new": """    if (paper_open is not None or paper_pend is not None):  # MUT 不过闸门""",
        "script": "extra/test_double_sl.py",
        "caught_by": "⑨ 占用期间不收敛不重挂",
    },
    {
        "name": "M136 flip_tol_pct 漏传静默按 0（闸门无声失效）",
        "module": "supertrend_strategy",
        "old": """    if flip_tol_pct is None:
        print(f"[supertrend] {token} [WARN] flip_tol_pct 未传（调用点漏参数）→ "
              f"本轮按 0（不设限）处理，请补上调用参数")
        flip_tol_pct = 0.0""",
        "new": """    if flip_tol_pct is None:
        flip_tol_pct = 0.0  # MUT 静默""",
        "script": "extra/test_supertrend_logic.py",
        "caught_by": "36 漏传要告警",
    },
    {
        "name": "M137 总开关关闭时跳过已移除币清理（残留仓永远没人管）",
        "module": "supertrend_strategy",
        "old": """    with storage.get_conn(config.SUPERTREND_DB) as conn_st:
        removed = [dict(r) for r in conn_st.execute(
            "SELECT * FROM supertrend_tokens WHERE status='REMOVED'")]
    for t in removed:
        try:
            close_supertrend_position(t["token"], "REMOVED")
        except Exception as e:
            print(f"[supertrend] 清理 {t['token']} 异常: {e}")

    if not enabled:""",
        "new": """    if not enabled:""",
        "script": "extra/test_supertrend_logic.py",
        "caught_by": "35 开关关闭也清移除币",
    },
    {
        "name": "M138 收敛时撤单失败也标 CLOSED（孤儿单永久无人管）",
        "module": "supertrend_live",
        "old": """    if cancel_failed:
        # 还有单没撤掉 → **不标 CLOSED**（持仓已经平掉了，不急）：记录一旦离开
        # _live_rows 就再没人重试撤单，那张孤儿条件单会留在交易所 —— 日后可能在
        # 错误价位平掉新开的仓。下轮会再来一次：先撤单，成功了才收敛
        print(f"[st-live] {token} {side} 有 {cancel_failed} 笔单未撤掉，"
              f"本轮不标 CLOSED（下轮重试撤单）")
        return
""",
        "new": """""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "39 撤单失败不收敛",
    },
    {
        "name": "M139 close_live 只看纸盘（实盘没平也报成功）",
        "module": "supertrend_live",
        "old": """    return paper_closed and live_ok""",
        "new": """    return paper_closed  # MUT 不看实盘结果""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "40 不谎报成功",
    },
    {
        "name": "M140 live_position_insert 冲突也恒返 True",
        "module": "storage",
        "old": """        return cur.rowcount > 0""",
        "new": """        return True  # MUT 冲突也当成功""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "41 冲突返回 False",
    },
    {
        "name": "M141 撤单被拒时把「已成交的单」也当失败（记录永久卡死）",
        "module": "exchange",
        "old": """    if not isinstance(normal, list) or not isinstance(algos, list):
        return False
    try:
        want = int(order_id)
        for o in normal:
            if int(o.get("orderId") or 0) == want:
                return False
        for o in algos:
            if int(o.get("algoId") or o.get("orderId") or 0) == want:
                return False
    except (TypeError, ValueError):
        return False
    return True          # 两个列表里都没有 → 已经消失（成交/被撤）""",
        "new": """    return False  # MUT 撤单被拒一律当失败""",
        "script": "extra/_check_exchange_orders.py",
        "caught_by": "撤单被拒但单子已不在 → True",
    },
    {
        "name": "M142 孤儿挂单记录撤单失败也收敛（孤儿单无人管）",
        "module": "supertrend_live",
        "old": """        if _orphan_fail:
            # 撤不掉就不收敛（同上）：记录离开 _live_rows 后那张单再没人管
            print(f"[st-live] {token} 孤儿挂单记录 #{live_pend['id']} 撤单失败，"
                  f"保留记录下轮重试")
        else:""",
        "new": """        if False:
            pass
        else:""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "39 撤单失败不收敛",
    },
    {
        "name": "M143 平仓不校验成交量（部分成交也标 CLOSED，残仓无人管）",
        "module": "supertrend_live",
        "old": """                filled = float(r.get("quantity") or 0)
                if filled <= 0:
                    print(f"[st-live] {token} 实盘平仓零成交（{reason}）："
                          f"记录不收敛（下轮重试）")
                    return
                if filled < qty * 0.999:""",
        "new": """                if False:
                    return
                if False:""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "44 平仓部分成交不收敛",
    },
    {
        "name": "M144 复用 live 记录时继续往下挂新单（F3，喂 OPEN 行挂错单）",
        "module": "supertrend_live",
        "old": """    if reuse_only:""",
        "new": """    if False:  # MUT 不拦""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "45 不挂新单",
    },
    {
        "name": "M145 撤不掉残留单也照样补开（F4，单号蒸发）",
        "module": "supertrend_live",
        "old": """    if cancel_failed:
        # 有撤不掉的残留单 → **推迟本轮补开**：下面无论走复用分支（新 orders 覆盖
        # 记录）还是补建分支，都会让那些单号从 DB 里蒸发，孤儿单再没人认识。
        # 先把它清干净，下一轮再来（F1 修好后，撤一张已成交的单算成功，不会卡住）
        print(f"[st-live] {token} 补开推迟：有 {cancel_failed} 笔残留单撤不掉"
              f"（先清干净，下轮再来）")
        return""",
        "new": """    if False:  # MUT 不推迟
        return""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "46 撤单失败保留记录",
    },
    {
        "name": "M146 跟单占用查询失败即放行（安全闸门 fail-open 会平别人的仓）",
        "module": "supertrend_live",
        "old": """        print(f"[st-live] {token} 跟单占用查询失败，按「被占用」处理（本轮不动）: {e}")
        return True""",
        "new": """        return False  # MUT fail-open""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "47 查询失败按占用",
    },
    {
        "name": "M147 close_live/switch 不先置 paused（窗口内可重开新仓）",
        "module": "supertrend_live",
        "old": """    # **先暂停监控再动手**：下面关纸盘/平交易所要跑好几秒 API，窗口里策略线程
    # 可能又开一个新纸盘仓 → 被同步层镜像到刚清空的交易所上（等于没平干净）
    try:
        with storage.get_conn(config.SUPERTREND_DB) as conn_st:
            conn_st.execute(
                "UPDATE supertrend_tokens SET paused=1 WHERE token=?", (token,))
    except Exception as e:
        print(f"[st-live] {token} 手动平仓：置暂停失败: {e}")
""",
        "new": """""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "48 先置 paused",
    },
    {
        "name": "M148 挂单没成交也 return（补建成死代码，记录缺口永远没人管）",
        "module": "supertrend_live",
        "old": """                    if _mirror_pending(lc, token, paper_open, live_pend,
                                       ex_positions, reuse_only=True):
                        return""",
        "new": """                    _mirror_pending(lc, token, paper_open, live_pend,
                                    ex_positions, reuse_only=True)
                    return  # MUT 无条件 return""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "50 补建可达",
    },
    {
        "name": "M149 live 记录撞上 CLOSED 残留行就静默丢弃（补建每轮重复挂 SL）",
        "module": "supertrend_live",
        "old": """    vals = dict(values)
    # 复用旧行：时间戳重置（"刚开仓"保护按新仓算）；realized_pnl **不清零**——
    # 同一纸盘仓可能平了又开，清掉会让上一段的真实盈亏从统计里消失
    vals.update({"closed_at": None, "created_at": "__CURRENT_TIMESTAMP__"})
    storage.live_position_update(lc, row["id"], vals)""",
        "new": """    return None  # MUT 冲突不复用（等于旧行为：静默丢记录）""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "49 冲突要复用旧行",
    },
    {
        "name": "M150 SL 交接失败照样收敛重复记录（止损覆盖归零）",
        "module": "supertrend_live",
        "old": """            if dups and not _handover_sl(lc, token, preferred, side):
                print(f"[st-live] {token} SL 交接未完成，本轮不收敛重复记录"
                      f"（保留它们的止损覆盖，下轮重试）")
                dups = []""",
        "new": """            _handover_sl(lc, token, preferred, side)  # MUT 不看交接结果""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "51 交接失败不收敛",
    },
    {
        "name": "M151 平仓量四舍五入 + minQty 兜底（残仓被放大成超量单）",
        "module": "exchange",
        "old": """    qty = _round_qty_down(symbol, quantity)
    if qty <= 0:
        return None""",
        "new": """    qty = _round_qty(symbol, quantity)  # MUT 放大""",
        "script": "extra/_check_exchange_orders.py",
        "caught_by": "残仓不足一个 step 不发单",
    },
    {
        "name": "M152 平仓不量化（残仓被放大到 minQty，超量被拒 = 永久卡住）",
        "module": "supertrend_live",
        "old": """            if qty > 0:
                # 平仓量只许向下量化，绝不放大：放大到 minQty 的超量单会被交易所
                # 拒掉，记录就永久"不收敛"重试（而 SL 已经在上面撤了 = 裸仓）
                qty = _close_qty_ex(token, qty)""",
        "new": """            if qty > 0:
                qty = qty  # MUT 不量化""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "54 残仓不放大",
    },
    {
        "name": "M153 _align_open 不设跟单闸门（SL 会去平跟单的仓）",
        "module": "supertrend_live",
        "old": """    if _alphafox_occupied(token):
        # 被跟单实盘占用（币种级互斥）→ 撤单/挂 SL/补减仓一律不碰：下面会把 SL
        # 按纸盘量重挂，那个量在互斥共存时可能覆盖到跟单的仓位（STOP_MARKET 没有
        # reduceOnly → 会去平别人的仓）。释放后下一轮自愈。
        print(f"[st-live] {token} 持仓对齐跳过：被跟单实盘占用（释放后自动接管）")
        return""",
        "new": """    if False:  # MUT 不设闸门
        return""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "52 占用期间不碰交易所",
    },
    {
        "name": "M154 手动平仓不传纸盘平仓行（realized 回落 0）",
        "module": "supertrend_live",
        "old": """                    _close_exchange_and_mark(lc, token, live_rows, ex_positions,
                                             _latest_closed_paper_row(token),
                                             "手动平仓")""",
        "new": """                    _close_exchange_and_mark(lc, token, live_rows, ex_positions,
                                             None, "手动平仓")""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "53 close_live 传纸盘平仓行",
    },
    {
        "name": "M155 孤儿挂单记录不撤单（限价已成交时预挂 SL 变无主单）",
        "module": "supertrend_live",
        "old": """        _orphan_fail = _cancel_our_orders(token, [live_pend], lc=lc,
                                          reason="孤儿挂单记录收敛撤单")""",
        "new": """        _orphan_fail = 0  # MUT 不撤单（旧行为：限价已成交就不撤）""",
        "script": "extra/_scenario_supertrend_cycle.py",
        "caught_by": "B3 只剩 1 张 SL / 四项一致性",
    },
    {
        "name": "M156 对账不看 SL 覆盖量（半仓裸奔到下一根K线）",
        "module": "supertrend_live",
        "old": """                   or orders.get("sl_qty") is None
                   or abs(float(orders.get("sl_qty") or 0) - paper_qty)
                   > _QTY_MIN_DIFF):""",
        "new": """                   ):  # MUT 不看覆盖量""",
        "script": "extra/_scenario_supertrend_cycle.py",
        "caught_by": "B3 四项一致性（SL 覆盖 == 持仓）",
    },
    {
        "name": "M157 重挂 SL 不记覆盖量（每轮都重挂 = 单号churn）",
        "module": "supertrend_live",
        "old": """                orders["sl_price"] = sl
                orders["sl_qty"] = qty
                _log_event(lc, token, "sl_update", price=sl, qty=qty,
                           paper_id=paper_open.get("id"),
                           order_id=r.get("order_id"), reason="止损重挂")
                print(f"[st-live] {token} SL 更新 @ {sl} qty={qty}")""",
        "new": """                orders["sl_price"] = sl
                _log_event(lc, token, "sl_update", price=sl, qty=qty,
                           paper_id=paper_open.get("id"),
                           order_id=r.get("order_id"), reason="止损重挂")
                print(f"[st-live] {token} SL 更新 @ {sl} qty={qty}")""",
        "script": "extra/_scenario_supertrend_cycle.py",
        "caught_by": "A4b 重复轮次不重挂 SL",
    },
    {
        "name": "M158 对账用本轮开头的旧 orders 快照（撤已作废单号 → 两张 SL 并存）",
        "module": "supertrend_live",
        "old": """    _fresh_row = lc.execute(
        "SELECT exchange_orders_json FROM live_positions WHERE id=?",
        (live_open["id"],)).fetchone()
    if _fresh_row is not None:
        live_open = dict(live_open)
        live_open["exchange_orders_json"] = _fresh_row["exchange_orders_json"]""",
        "new": """    pass  # MUT 用旧快照""",
        "script": "extra/test_double_sl.py",
        "caught_by": "②-a SL 总覆盖（不是超额）",
    },
    {
        "name": "M159 减仓被「疑似手动仓」守卫跳过时提前 return（连带丢掉 SL 维护）",
        "module": "supertrend_live",
        "old": """            print(f"[st-live] {token} 对账减仓跳过：交易所 {ex_qty:.6g} 远超"
                  f"镜像记录 {our_qty:.6g}（可能有手动仓合并）")
        elif close_qty > _QTY_MIN_DIFF:""",
        "new": """            print(f"[st-live] {token} 对账减仓跳过：交易所 {ex_qty:.6g} 远超"
                  f"镜像记录 {our_qty:.6g}（可能有手动仓合并）")
            return
        elif close_qty > _QTY_MIN_DIFF:""",
        "script": "extra/_scenario_supertrend_cycle.py",
        "caught_by": "D2-b SL 照常重挂",
    },
    {
        "name": "M160 止损触发价只 round(6) 不按 tickSize 对齐（币安 -1111 拒单 = 实盘裸奔）",
        "module": "exchange",
        "old": """        "type": "STOP_MARKET", "algoType": "CONDITIONAL",
        "quantity": qty, "triggerPrice": _round_price(symbol, stop_price),""",
        "new": """        "type": "STOP_MARKET", "algoType": "CONDITIONAL",
        "quantity": qty, "triggerPrice": round(stop_price, 6),""",
        "script": "extra/_check_exchange_orders.py",
        "caught_by": "止损触发价按 tickSize 对齐",
    },
    {
        "name": "M161 止盈触发价只 round(6) 不按 tickSize 对齐",
        "module": "exchange",
        "old": """        "type": "TAKE_PROFIT_MARKET", "algoType": "CONDITIONAL",
        "quantity": qty, "triggerPrice": _round_price(symbol, stop_price),""",
        "new": """        "type": "TAKE_PROFIT_MARKET", "algoType": "CONDITIONAL",
        "quantity": qty, "triggerPrice": round(stop_price, 6),""",
        "script": "extra/_check_exchange_orders.py",
        "caught_by": "止盈触发价按 tickSize 对齐",
    },
    {
        "name": "M162 实盘仓消失只收尾记录、纸盘那一笔继续跟（手动平仓后又跟着交易员加仓）",
        "module": "alphafox_live",
        "old": """        _close_paper_leg(token, side, "实盘持仓消失（手动平仓/强平），不再跟这一笔")""",
        "new": """        pass  # MUT 纸盘仓继续挂托管""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "27 纸盘这一笔也一起收尾",
    },
    {
        "name": "M163 一次净变化只记 1 笔加仓（交易员连加 3 笔只算 1 笔）",
        "module": "alphafox_trade",
        "old": """                _n_add = _new_action_orders(trader_id, symbol, "ADD", data)
                _n_red = _new_action_orders(trader_id, symbol, "REDUCE", data)
                _net = _n_add - _n_red
                _want = abs(_net) if (_net > 0) == (action == "ADD") else 1""",
        "new": """                _want = 1  # MUT 不补记真实笔数""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "55-a 还原成 3 条 ADD 信号",
    },
    {
        "name": "M164 流水不去重（同一批订单每轮重复补记加仓）",
        "module": "alphafox_trade",
        "old": """        k = _order_key(o)
        if k is None or k in seen:
            continue""",
        "new": """        pass  # MUT 不去重（同一批订单每轮都算新增）""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "55-c 重复轮询不再重复计数",
    },
    {
        "name": "M165 比例 k 不冻结（按当前余额重算 → 余额一变就调仓）",
        "module": "alphafox_live",
        "old": """    if k > 0:
        return k
    if not live:
        return 0.0""",
        "new": """    _b, _m = available_balance_cached(), _trader_margin_of(pp)
    if _b > 0 and _m > 0:
        return _b / _m          # MUT 每次按当前余额重算
    if not live:
        return 0.0""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "37 余额变了已有仓也不动",
    },
    {
        "name": "M166 每币杠杆覆盖不生效（永远用全局）",
        "module": "alphafox_live",
        "old": """            v = float(row["live_leverage"] or 0)
            if v > 0:
                lv = v""",
        "new": """            v = float(row["live_leverage"] or 0)
            if False:  # MUT 每币覆盖不生效
                lv = v""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "61 每币覆盖的杠杆生效",
    },
    {
        "name": "M167 老记录不补写冻结 k（下次对账又按记录量重推）",
        "module": "alphafox_live",
        "old": """    k = q / tq
    orders["k"] = round(k, 12)
    orders.setdefault("model", "ratio")""",
        "new": """    k = q / tq
    orders.setdefault("model", "ratio")  # MUT 不补写 k""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "61 老记录补写冻结 k",
    },
    {
        "name": "M168 交易员卡片没探测也谎报空仓（stale 不标、positions 给空数组）",
        "module": "web",
        "old": """            "stale": raw is None,
            "polled_at": c.get("polledAt"),
            "pos_count": len(items),
            "positions": None if raw is None else items,""",
        "new": """            "stale": False,
            "polled_at": c.get("polledAt"),
            "pos_count": len(items),
            "positions": items,""",
        "script": "extra/_check_af_board.py",
        "caught_by": "6 stale + positions=None",
    },
    {
        "name": "M169 币种设置：杠杆越界不校验（0/999 也写进覆盖）",
        "module": "web",
        "old": """        if not (1 <= lv <= 125):
            raise HTTPException(400, "杠杆需在 1~125 之间")""",
        "new": """        if False:  # MUT 不校验越界
            raise HTTPException(400, "unreachable")""",
        "script": "extra/_check_af_board.py",
        "caught_by": "7 越界杠杆被拒（400）",
    },
    {
        "name": "M170 流水水位不每轮推进（历史单/加了又减的单下次被重复计数）",
        "module": "alphafox_trade",
        "old": """    _seen_orders_advance(trader_id, data)""",
        "new": """    pass  # MUT 不推进水位""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "56-b 只算 1 笔",
    },
    {
        "name": "M171 减仓不按笔数还原（加仓按笔数、减仓按净额 → 份数单向偏高）",
        "module": "alphafox_trade",
        "old": """                _n_add = _new_action_orders(trader_id, symbol, "ADD", data)
                _n_red = _new_action_orders(trader_id, symbol, "REDUCE", data)
                _net = _n_add - _n_red
                _want = abs(_net) if (_net > 0) == (action == "ADD") else 1""",
        "new": """                _net = (_new_action_orders(trader_id, symbol, action, data)
                        if action == "ADD" else 0)  # MUT 减仓不还原
                _want = abs(_net) if _net > 0 else 1""",
        "script": "extra/_scenario_alphafox_cycle.py",
        "caught_by": "C1 两笔减仓都还原",
    },
    {
        "name": "M172 交易所已无持仓时 realized 记 0（实盘口径漏记这类平仓）",
        "module": "alphafox_live",
        "old": """        close_price = _paper_mark(token, side)
        if close_price:""",
        "new": """        close_price = None  # MUT 不估算
        if close_price:""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "62 realized 估算",
    },
    {
        "name": "M173 趋势实盘页漏掉挂单腿（live PENDING 记录整行消失）",
        "module": "web",
        "old": """            live_open = [dict(r) for r in lc.execute(
                "SELECT * FROM live_positions WHERE strategy='supertrend' "
                "AND status IN ('OPEN','PARTIAL','PENDING')").fetchall()]""",
        "new": """            live_open = [dict(r) for r in lc.execute(
                "SELECT * FROM live_positions WHERE strategy='supertrend' "
                "AND status IN ('OPEN','PARTIAL')").fetchall()]  # MUT 漏挂单腿""",
        "script": "extra/_check_st_board.py",
        "caught_by": "2 挂单腿 1 笔 / 3 BBB 挂单腿 join",
    },
    {
        "name": "M174 交易所取不到数时实盘浮盈写 0（页面把查不到显示成真值）",
        "module": "web",
        "old": """        "ex_pnl": round(ex_pnl_sum, 2) if ex_ok else None,""",
        "new": """        "ex_pnl": round(ex_pnl_sum, 2),  # MUT 失败也写 0""",
        "script": "extra/_check_st_board.py",
        "caught_by": "6 实盘列给 None",
    },
    {
        "name": "M175 挂单腿保证金也算进占用（未成交就占地，页面虚高）",
        "module": "web",
        "old": """        sum(float(r.get("margin_usd") or 0) for r in live_open
            if r.get("status") != "PENDING"), 2)""",
        "new": """        sum(float(r.get("margin_usd") or 0) for r in live_open), 2)  # MUT""",
        "script": "extra/_check_st_board.py",
        "caught_by": "2 占用保证金只算已成交",
    },
    {
        "name": "M176 overview 不走缓存（两个页面各自轮询 → 币安标记价调用翻倍）",
        "module": "web",
        "old": """    ov = _cached("supertrend_overview", 30, _supertrend_overview_compute)""",
        "new": """    ov = _supertrend_overview_compute()  # MUT 不缓存""",
        "script": "extra/_check_st_board.py",
        "caught_by": "1 第二次调用命中缓存",
    },
    {
        "name": "M177 写接口不让 overview 缓存失效（改完参数页面还是旧值）",
        "module": "web",
        "old": """    _cache_invalidate("supertrend_overview")""",
        "new": """    pass  # MUT 不清缓存""",
        "script": "extra/_check_st_board.py",
        "caught_by": "7 五个写接口都让缓存失效",
    },
    {
        "name": "M178 补开市价仓不记挂止损日志（实盘动作在页面看不见）",
        "module": "supertrend_live",
        "old": """                _log_event(lc, token, "sl_set", price=sl, qty=actual_qty,
                           paper_id=paper_open.get("id"),
                           order_id=r.get("order_id"), reason="补开挂止损")""",
        "new": """                pass  # MUT 不记挂止损日志""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "55 市价补开：日志 sl_set + open",
    },
    {
        "name": "M179 对账补仓不记日志（交割对不上的时候无从查证）",
        "module": "supertrend_live",
        "old": """                _log_event(lc, token, "rebalance_add",
                           price=result.get("price"), qty=result.get("quantity"),
                           paper_id=paper_open.get("id"),
                           order_id=result.get("order_id"),
                           reason="交易所少于纸盘，补仓对齐")""",
        "new": """                pass  # MUT 不记补仓日志""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "55 rebalance_add",
    },
    {
        "name": "M180 挂单腿预挂止损不记日志（挂单腿的止损没人看见）",
        "module": "supertrend_live",
        "old": """        if orders.get("sl"):
            _log_event(lc, token, "sl_set", price=sl, qty=qty,
                       paper_id=paper_pend["id"], order_id=orders.get("sl"),
                       reason="挂单腿预挂止损")""",
        "new": """        if orders.get("sl"):
            pass  # MUT 不记预挂日志""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "55 限价挂单腿：日志 sl_set + open",
    },
    {
        "name": "M181 日志动作缺中文映射（页面显示英文 action 名）",
        "module": "web",
        "old": """const ACT_CN = {open:'开仓', close:'平仓', sl_set:'挂止损', sl_update:'改止损',""",
        "new": """const ACT_CN = {open:'开仓', close:'平仓', sl_set:'', sl_update:'改止损',""",
        "script": "extra/_check_pages_js.py",
        "caught_by": "渲染冒烟 ⑥ 日志：动作中文映射",
    },
    {
        "name": "M182 页面内联 onclick 的引号被 Python 吃掉一层（整段 script 语法错误 → 页面全白）",
        "module": "web",
        "old": """        return '<tr data-tok="' + esc(c.token) + '" onclick="location.href=\\\\'/supertrend\\\\'">'""",
        "new": """        return '<tr data-tok="' + esc(c.token) + '" onclick="location.href=\\'/supertrend\\'">'""",
        "script": "extra/_check_pages_js.py",
        "caught_by": "SUPERTREND_LIVE_HTML JS 语法检查",
    },
    {
        "name": "M183 日志写失败不兜异常（把仓位状态机带崩）",
        "module": "supertrend_live",
        "old": """    try:
        storage.live_trade_log_insert(lc, {
            "token": token, "strategy": "supertrend", "action": action,
            "price": price, "quantity": qty,
            "paper_position_id": paper_id,
            "exchange_order_id": str(order_id or ""),
            "details_json": json.dumps({"reason": reason}) if reason else None,
        })
    except Exception as e:
        print(f"[st-live] {token} 交易日志写入失败（不影响交易）: {e}")""",
        "new": """    storage.live_trade_log_insert(lc, {
        "token": token, "strategy": "supertrend", "action": action,
        "price": price, "quantity": qty,
        "paper_position_id": paper_id,
        "exchange_order_id": str(order_id or ""),
        "details_json": json.dumps({"reason": reason}) if reason else None,
    })  # MUT 不兜异常""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "56 日志写失败：状态机照常收敛",
    },
    {
        "name": "M184 撤单不记日志（孤儿单/残留单被撤用户看不见）",
        "module": "supertrend_live",
        "old": """                if lc is not None:
                    _log_event(lc, token,
                               "limit_cancel" if key == "limit" else "sl_cancel",
                               order_id=oid,
                               paper_id=lr.get("paper_position_id"),
                               reason=reason)""",
        "new": """                pass  # MUT 撤单不记日志""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "56 平仓收敛撤掉的止损单也入日志",
    },
    {
        "name": "M185 实盘孤儿记录（无纸盘仓）不上报：卡住的收敛在页面隐形",
        "module": "web",
        "old": """    _orphan_live = [r for r in live_open
                    if r.get("paper_position_id") not in _active_pids]""",
        "new": """    _orphan_live = []  # MUT 不查孤儿""",
        "script": "extra/_check_st_board.py",
        "caught_by": "3b 孤儿实盘记录计入 orphan_count",
    },
    {
        "name": "M186 覆盖量交接不记日志（撤旧+重挂对用户隐形）",
        "module": "supertrend_live",
        "old": """    _log_event(lc, token, "sl_update", price=sl_price, qty=target,
               paper_id=live_row.get("paper_position_id"),
               order_id=r.get("order_id"),
               reason="重复记录收敛：止损覆盖量交接重挂")""",
        "new": """    pass  # MUT 交接不记日志""",
        "script": "extra/test_double_sl.py",
        "caught_by": "⑨-e 覆盖量交接留痕",
    },
    {
        "name": "M187 挂单腿只改止损时不重挂实盘 SL（成交那刻止损停在旧轨道）",
        "module": "supertrend_live",
        "old": """                _place_pending_sl(token, side, qty, sl, orders)
                if orders.get("sl"):
                    _log_event(lc, token, "sl_set", price=sl, qty=qty,
                               paper_id=live_pend.get("paper_position_id"),
                               order_id=orders.get("sl"),
                               reason=("挂单腿止损改价重挂" if had_sl
                                       else "挂单腿补挂止损"))""",
        "new": """                if False:  # MUT 挂单腿止损改价不重挂
                    _place_pending_sl(token, side, qty, sl, orders)""",
        "script": "extra/test_supertrend_live.py",
        "caught_by": "57 实盘预挂止损改到新价",
    },
    {
        "name": "M188 总开关关闭时不做实盘对账（纸盘/live 长期不收敛）",
        "module": "supertrend_strategy",
        "old": """        try:
            import supertrend_live
            supertrend_live.sync_supertrend_live()
        except Exception as e:
            print(f"[supertrend] live 镜像同步异常: {e}")
        return""",
        "new": """        return  # MUT 关闭时不对账""",
        "script": "extra/_scenario_supertrend_cycle.py",
        "caught_by": "F2 关闭期间扫损：live 记录收敛",
    },
    {
        "name": "M189 流水动作按内部口径比（平台真词表是 open_long/increase_long → 永不生效）",
        "module": "alphafox_trade",
        "old": """        ca = str(o.get("canonicalAction") or "").strip().lower()
        if not ((want_inc and ca in _INC_ACTIONS)
                or (not want_inc and ca in _DEC_ACTIONS)):
            continue""",
        "new": """        ca = str(o.get("canonicalAction") or "").strip().upper()
        if ca != action:      # MUT 按内部口径比（词表对不上 → 笔数还原空转）
            continue""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "55-a 一次净变化还原成 3 条 ADD",
    },
    {
        "name": "M190 首轮不推进流水水位（重启后把几小时历史流水算成新单 → 疯狂加仓）",
        "module": "alphafox_trade",
        "old": """        _seen_orders_advance(trader_id, data)
        print(f"[alphafox] {label} 首轮基线建立（{len(cur)} 个持仓，\"""",
        "new": """        print(f"[alphafox] {label} 首轮基线建立（{len(cur)} 个持仓，\"""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "57-e 重启后第一次真加仓只算 1 笔",
    },
    {
        "name": "M191 流水 symbol 不归一（真样本 BTC / XYZ:SKHX 与内部 key 对不上 → 永不计数）",
        "module": "alphafox_trade",
        "old": """        if _norm_symbol(o.get("symbol")) != symbol:
            continue""",
        "new": """        if o.get("symbol") != symbol:      # MUT 直接比原串
            continue""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "57-a 开仓 3 笔成交（其中一笔 symbol 不带 USDT）",
    },
    {
        "name": "M192 开仓不补记多笔成交（交易员拆成 N 笔开仓 → 只跟 1 份）",
        "module": "alphafox_trade",
        "old": """            if ok:
                _n_inc = _new_action_orders(trader_id, symbol, "ADD", data)
                _n_dec = _new_action_orders(trader_id, symbol, "REDUCE", data)
                for _ in range(max(_n_inc - _n_dec, 1) - 1):
                    _s = dict(sig)
                    _s["action"] = "ADD"
                    _log_signal(st_conn, _s)
                    signals.append(_s)""",
        "new": """            pass  # MUT 开仓不补记多笔""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "57-a 开仓 3 笔成交 → 1 OPEN + 2 ADD",
    },
    {
        "name": "M193 脏流水也计数（算不出内容键 → 每轮都当新单，无限加份）",
        "module": "alphafox_trade",
        "old": """        k = _order_key(o)
        if k is None or k in seen:
            continue""",
        "new": """        k = _order_key(o)
        if k in seen:      # MUT 脏键(None)也计数
            continue""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "57-f 脏流水不计入笔数",
    },
    {
        "name": "M194 首次见到流水也计数（几小时历史单一次当新增加十几份）",
        "module": "alphafox_trade",
        "old": """    if not seen:
        # 这个交易员的流水我们一次都没见过（刚跟单 / 前几轮 payload 没带
        # latestOrders）→ 列表里全是历史单，一条都不算：宁可这一轮少算，
        # 也不能把几小时的历史成交当成"本轮新增"一次加十几份
        return 0""",
        "new": """    if False:      # MUT 首次见到也照算
        return 0""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "57-g 首次见到流水时不算新单",
    },
    {
        "name": "M195 交易员持仓的空头负名义不取绝对（页面显示 -$450 被读成负仓位）",
        "module": "web",
        "old": """                "notional": abs(_af_num(p.get("notional"))),""",
        "new": """                "notional": _af_num(p.get("notional")),  # MUT 不取绝对""",
        "script": "extra/_check_af_board.py",
        "caught_by": "6 空头负名义被取绝对",
    },
    {
        "name": "M196 交易员持仓明细缺强平价/最后下单（用户要求信息别省略）",
        "module": "web",
        "old": """                "liq_price": _af_num(p.get("estimatedLiqPrice")),
                "last_order_time": p.get("lastOrderTime"),""",
        "new": """                "liq_price": None, "last_order_time": None,  # MUT 省字段""",
        "script": "extra/_check_af_board.py",
        "caught_by": "6 卡片明细带强平价与最后下单时间",
    },
    {
        "name": "M197 交易员持仓明细截断回省略号（信息被吃掉）",
        "module": "web",
        "old": """.trd-pos{font-size:11px;line-height:1.6;border-top:1px solid #21262d;padding:6px 0;
         white-space:normal;overflow-wrap:anywhere}""",
        "new": """.trd-pos{font-size:11px;line-height:1.6;border-top:1px solid #21262d;padding:6px 0;
         white-space:nowrap;overflow:hidden;text-overflow:ellipsis}""",
        "script": "extra/_check_af_board.py",
        "caught_by": "7 持仓明细不再省略号截断",
    },
    {
        "name": "M198 币种设置不渲染可点选按钮（用户以为没得选）",
        "module": "web",
        "old": """    cw.innerHTML = opts.length
      ? opts.map(t => '<button class="btn ts-tok" data-chip="' + esc(t) + '">'
                      + esc(t) + '</button>').join('')
      : '<span class="muted" style="font-size:11px">（还没有交易记录，直接手输）</span>';""",
        "new": """    cw.innerHTML = '';  # MUT 不渲染按钮""",
        "script": "extra/_check_pages_js.py",
        "caught_by": "跟单托管页渲染冒烟 ⑦ 已交易过的币是可点选按钮",
    },
    {
        "name": "M199 页面 JS 引用不存在的元素 id（运行时 TypeError → 整页卡在加载中）",
        "module": "web",
        "old": """document.getElementById('ts-chips').addEventListener('click', e => {""",
        "new": """document.getElementById('ts-chipz').addEventListener('click', e => {""",
        "script": "extra/_check_pages_js.py",
        "caught_by": "ALPHAFOX_LIVE_HTML JS 引用的元素 id 都存在",
    },
    {
        "name": "M200 历史/日志不按上限截断（一次把全部行渲染出来，分页失效）",
        "module": "web",
        "old": """function renderHistory(rows) {
  const w = document.getElementById('his-wrap');
  const show = rows.slice(0, _hisLimit);""",
        "new": """function renderHistory(rows) {
  const w = document.getElementById('his-wrap');
  const show = rows;  // MUT 不截断""",
        "script": "extra/_check_pages_js.py",
        "caught_by": "跟单托管页渲染冒烟 ⑤ 历史持仓分页",
    },
    {
        "name": "M201 不到一页也显示「加载更多」（列表短时按钮还在）",
        "module": "web",
        "old": """  if (fetched < limit) { w.innerHTML = ''; return; }        // 已到底，不显示按钮""",
        "new": """  if (false) { w.innerHTML = ''; return; }  // MUT 到底也显示按钮""",
        "script": "extra/_check_pages_js.py",
        "caught_by": "跟单托管页渲染冒烟 ⑤⑥ 短列表时不显示",
    },
    {
        "name": "M202 点「加载更多」撞上在飞请求时被吞掉（趋势页最多 30s 才生效，像卡死）",
        "module": "web",
        "old": """  if (loading) { _reloadPending = true; return; }""",
        "new": """  load();  // MUT 不补拉""",
        "script": "extra/_check_af_board.py",
        "caught_by": "5 分页：点击撞上在飞请求时补拉",
    },
    {
        "name": "M203 容忍带不设 step 下限（量化噪声 ±1 step 每次都触发真成交 = 反复对账）",
        "module": "alphafox_live",
        "old": """    _step = _step_of(token)
    if _step > 0:
        band = max(band, 1.5 * _step)""",
        "new": """    pass  # MUT 容忍带不设 step 下限""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "64 量化噪声不再触发对账成交",
    },
    {
        "name": "M204 加仓不看交易所现仓（直接按目标量再买一份 → 实盘翻倍）",
        "module": "alphafox_live",
        "old": """            lot_qty = target_qty - ex_qty""",
        "new": """            lot_qty = target_qty  # MUT 不看现仓""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "2 加仓到 0.004（0.04 × k=0.1）",
    },
    {
        "name": "M205 目标量忘乘 k（按交易员量全量跟 → 规模差几个数量级）",
        "module": "alphafox_live",
        "old": """    return _round_qty_ex(token, tq * k)""",
        "new": """    return _round_qty_ex(token, tq)  # MUT 忘乘 k""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "1 按比例开仓 0.002",
    },
    {
        "name": "M206 目标量不按交易所 step 量化（每轮差半个 step → 反复反向成交）",
        "module": "alphafox_live",
        "old": """    return _round_qty_ex(token, tq * k)""",
        "new": """    return tq * k  # MUT 不量化""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "29 连续 4 轮对账不振荡",
    },
    {
        "name": "M207 交易员量按累计量算（不减 closed_qty → 减仓后实盘不跟减）",
        "module": "alphafox_live",
        "old": """    net = (float(pp.get("quantity") or 0)
           - float(pp.get("closed_qty") or 0))
    return max(net, 0.0) / kq""",
        "new": """    return max(float(pp.get("quantity") or 0), 0.0) / kq  # MUT 用累计量""",
        "script": "extra/_scenario_alphafox_cycle.py",
        "caught_by": "C3 记录量 = 交易员 15 × k（走查的减仓段）",
    },
    {
        "name": "M208 开仓不冻结比例 k（老记录只能靠事后回填，口径不稳）",
        "module": "alphafox_live",
        "old": """                    "exchange_orders_json": json.dumps(
                        {"model": "ratio", "k": round(k, 12),
                         "alpha": round(alpha, 6),
                         "balance_ref": round(bal_ref, 8),
                         "trader_margin_ref": round(margin_ref, 8)}),""",
        "new": """                    "exchange_orders_json": json.dumps(
                        {"model": "ratio"}),  # MUT 不冻结 k""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "1 记录冻结比例 k 与两个参考值",
    },
    {
        "name": "M209 资金约束不生效（余额不够也加仓）",
        "module": "alphafox_live",
        "old": """    need = add_notional / lev
    if need > bal + 1e-9:
        return False, (f"可用余额 {bal:.2f}U 不够：本次需保证金 {need:.2f}U")
    return True, \"\"""",
        "new": """    return True, ""  # MUT 不看余额""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "66 余额不够：不加仓 / 69 第 6 次加不动",
    },
    {
        "name": "M210 单笔上限不生效（名义超限照加）",
        "module": "alphafox_live",
        "old": """    if max_notional and max_notional > 0 and add_notional > max_notional * 1.000001:
        return False, (f"单笔上限 {max_notional:.6g}U：本次下单 "
                       f"{add_notional:.2f}U 超限")""",
        "new": """    if False:  # MUT 不看上限
        return False, "unreachable\"""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "67 超上限：不加仓",
    },
    {
        "name": "M219 可用余额用光了当成「读失败」（继续加仓 → 超支）",
        "module": "alphafox_live",
        "old": """    if bal < 0:
        # 读不到余额（接口故障）：不阻断 —— 交易所是最后一道闸（保证金不够会拒单，
        # 下轮重试），而按 0 处理会把它当成"余额用光了"永远加不了仓
        return True, \"\"""",
        "new": """    if bal <= 0:  # MUT 0 也当读失败
        return True, \"\"""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "69 第 6 次：可用余额 0 → 加不动",
    },
    {
        "name": "M220 资金约束按「目标保证金」算（双重计算 → 比该停的早停）",
        "module": "alphafox_live",
        "old": """    add_qty = max(tq - float(cur_qty or 0), 0.0)""",
        "new": """    add_qty = tq          # MUT 按整仓目标算（把已占的再加一遍）""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "69 第 2 次：余额 160 时仍要能加到 4.0",
    },
    {
        "name": "M221 快速跟随不看超势占用（页面报「新建」而实盘静默不动）",
        "module": "alphafox_live",
        "old": """    if _supertrend_occupied(token):
        # 主流趋势占用该币实盘时对账会跳过镜像 —— 不在这里说清楚的话，用户会看到
        # "实盘: 新建"但实盘什么都没发生（对账那行只 print、不落日志 = 静默）
        return {"ok": False, "action": "跳过",
                "reason": "该币被主流趋势实盘占用（先处理它再跟随）"}""",
        "new": """    if False:  # MUT 不看超势占用
        return {"ok": False, "action": "跳过", "reason": "unreachable"}""",
        "script": "extra/_scenario_alphafox_quickfollow.py",
        "caught_by": "Q11 被主流趋势占用 → 说清楚",
    },
    {
        "name": "M222 存量仓闸门不认「显式快速跟随」（跟了一周的仓点了没反应）",
        "module": "alphafox_live",
        "old": """    if _pre_startup(pp) and _key_of(token, side) not in _QF_EXPLICIT:""",
        "new": """    if _pre_startup(pp):  # MUT 不认显式清单""",
        "script": "extra/_scenario_alphafox_quickfollow.py",
        "caught_by": "Q12 存量仓 + 快速跟随 → 越过闸门",
    },
    {
        "name": "M224 映射比例 α 不生效（设了 0.5 还是全额开）",
        "module": "alphafox_live",
        "old": """                alpha = _af_live_ratio()
                k = (alpha * bal_ref / margin_ref) if (tq0 > 0 and bal_ref > 0
                                                       and margin_ref > 0) else 0.0""",
        "new": """                alpha = _af_live_ratio()
                k = (bal_ref / margin_ref) if (tq0 > 0 and bal_ref > 0
                                               and margin_ref > 0) else 0.0""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "70 α=0.5：开仓量是全额的一半",
    },
    {
        "name": "M229 认领参考量用可用余额（自家残留仓被自己占的保证金判成「不像」）",
        "module": "alphafox_live",
        "old": """        _eq0 = account_equity_cached()
        if _eq0 > 0 and margin0 > 0 and tq0 > 0:
            ref_qty = tq0 * (alpha * _eq0 / margin0)""",
        "new": """        _eq0 = 0.0  # MUT 不用权益
        if False:
            ref_qty = tq0 * (alpha * _eq0 / margin0)""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "71 权益口径判「像我们的」→ 认领",
    },
    {
        "name": "M230 快速跟随闸门用可用余额（同一个币权益口径放行、可用口径误拦）",
        "module": "alphafox_live",
        "old": """        _eq = account_equity_cached()
        if _eq <= 0:
            _eq = available_balance_cached()    # 读不到权益 → 退回可用口径""",
        "new": """        _eq = available_balance_cached()  # MUT 用可用余额""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "71 快速跟随闸门同口径（权益口径放行）",
    },
    {
        "name": "M234 信号减仓不记已实现盈亏（战绩少算减仓那段）",
        "module": "alphafox_live",
        "old": """                    "exchange_orders_json": json.dumps(orders),
                    "realized_pnl": round(float(live.get("realized_pnl") or 0)
                                          + _realized_delta(live, _close_px, filled), 6),
                })""",
        "new": """                    "exchange_orders_json": json.dumps(orders),
                })  # MUT 不记 realized""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "74 信号减仓：平掉 1.0 记了 +10",
    },
    {
        "name": "M235 对账减仓不记已实现盈亏",
        "module": "alphafox_live",
        "old": """                "margin_usd": target_qty * mark / lev,
                "realized_pnl": round(float(live.get("realized_pnl") or 0)
                                      + _realized_delta(live, mark, _fq), 6),
            })""",
        "new": """                "margin_usd": target_qty * mark / lev,
            })  # MUT 不记 realized""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "74 对账减仓：减掉 0.2 记了 +2",
    },
    {
        "name": "M236 已实现盈亏方向反了（空头当多头算）",
        "module": "alphafox_live",
        "old": """    if (live.get("side") or "LONG") == "SHORT":
        return (entry - px) * q
    return (px - entry) * q""",
        "new": """    return (px - entry) * q  # MUT 都按多头算""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "74 空头减仓方向也对",
    },
    {
        "name": "M231 CLOSE 兜底平仓参考量给 0（只按杠杆判 → 误平用户同向同杠杆手动仓）",
        "module": "alphafox_live",
        "old": """                if ex_row and _looks_like_ours(ex_row, _implied_live_qty(token, side),
                                               lev):""",
        "new": """                if ex_row and _looks_like_ours(ex_row, 0, lev):  # MUT 只按杠杆""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "72 同向同杠杆的用户手动仓不许被误平",
    },
    {
        "name": "M232 兜底参考量用净持仓（纸盘已 CLOSE → 净量 0 → 退化成只按杠杆）",
        "module": "alphafox_live",
        "old": """    qty = float(pp.get("quantity") or 0)          # 总，不是净""",
        "new": """    qty = max(float(pp.get("quantity") or 0)
              - float(pp.get("closed_qty") or 0), 0.0)  # MUT 用净量""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "72 同向同杠杆的用户手动仓不许被误平（纸盘已 CLOSE）",
    },
    {
        "name": "M233 快速跟随不查反方向持仓（报「新建」而实盘不会开）",
        "module": "alphafox_live",
        "old": """    if _foreign_exposure(ex_positions, token, side, 0.0, lev, any_side=True):
        return {"ok": False, "action": "跳过",
                "reason": "交易所该币有反方向持仓（先处理它再跟随）"}""",
        "new": """    if False:  # MUT 不查反方向
        return {"ok": False, "action": "跳过", "reason": "unreachable"}""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "73 反方向有手动仓 → 如实回报",
    },
    {
        "name": "M228 不拦「低于最小下单量」的下单（被抬到 minQty → 开出放大仓）",
        "module": "alphafox_live",
        "old": """            _raw_inc = _target_qty_raw(ppos, k) - ex_qty
            _mn = _min_qty_of(token)
            if _mn > 0 and _QTY_MIN_DIFF < _raw_inc < _mn:
                print(f"[af-live] {token} 跳过：本次要下 {_raw_inc:.6g} 低于交易所最小"
                      f"下单量 {_mn:.6g}（映射比例/交易员增量过小）")
                return""",
        "new": """            pass  # MUT 不看最小下单量""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "70 α 过小（目标 < minQty）：不下单 / 增量低于 minQty：跳过加仓",
    },
    {
        "name": "M225 认领已有仓时也乘 α（记录丢失后立刻砍成 α 折）",
        "module": "alphafox_live",
        "old": """        k = (ex_qty / tq0) if tq0 > 0 else 0.0""",
        "new": """        k = (_af_live_ratio() * ex_qty / tq0) if tq0 > 0 else 0.0  # MUT 认领也乘""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "58/70 认领按现状：目标==现状、零下单",
    },
    {
        "name": "M227 映射比例不校验边界（1.5 也能存进设置 → 放大风险）",
        "module": "web",
        "old": """    if "alphafox_live_ratio" in fields \\
            and not (0.01 <= fields["alphafox_live_ratio"] <= 1.0):
        # 只允许"缩小"：>1 是放大实盘风险，与这个参数的目的相反
        raise HTTPException(400, "实盘映射比例需在 0.01 ~ 1.0 之间（1 = 完全按比例跟）")""",
        "new": """    if False:  # MUT 不校验映射比例
        raise HTTPException(400, "unreachable")""",
        "script": "extra/_check_af_board.py",
        "caught_by": "映射比例边界：>1 与 0 都被 400 拒",
    },
    {
        "name": "M226 补开新仓不乘 α（快速跟随/对账补开按全额开）",
        "module": "alphafox_live",
        "old": """    alpha = _af_live_ratio()
    k = (alpha * bal0 / margin0) if (bal0 > 0 and margin0 > 0 and tq0 > 0) else 0.0""",
        "new": """    alpha = _af_live_ratio()
    k = (bal0 / margin0) if (bal0 > 0 and margin0 > 0 and tq0 > 0) else 0.0""",
        "script": "extra/_scenario_alphafox_quickfollow.py",
        "caught_by": "Q16 快速跟随实盘按半量开",
    },
    {
        "name": "M223 快速跟随新建时不预检资金（报「新建」但实盘开不动）",
        "module": "alphafox_live",
        "old": """            _ok, _why = _funds_check(token, _mk0, _target_qty(token, pp, _k0),
                                     lev, 0.0, 0.0)
            if not _ok:
                return {"ok": False, "action": "跳过",
                        "reason": f"实盘资金不够：{_why}"}""",
        "new": """            pass  # MUT 不预检资金""",
        "script": "extra/_scenario_alphafox_quickfollow.py",
        "caught_by": "Q13 交易员杠杆更高 → 如实回报资金不够",
    },
    {
        "name": "M211 可用余额缓存失效（每笔都重读 → 连开时比例越开越小）",
        "module": "alphafox_live",
        "old": """    if _BAL_CACHE["v"] > 0 and now - _BAL_CACHE["ts"] < _BAL_CACHE_TTL:
        return float(_BAL_CACHE["v"])""",
        "new": """    if False:  # MUT 不用缓存
        return float(_BAL_CACHE["v"])""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "68 窗口内仍用第一次的读数",
    },
    {
        "name": "M212 快速跟随不拦杠杆不一致（改到用户手动仓的杠杆/强平价）",
        "module": "alphafox_live",
        "old": """        if ex_lev > 0 and abs(ex_lev - lev) > 0.5:
            return _fail(f"交易所该币杠杆 {ex_lev:g} ≠ 设置杠杆 {lev}（先处理它再跟随）")
        # "不像我们的"：参考量取"现在会开多少"——用**权益**（不是可用余额，理由同
        # 认领判定：这笔仓自己占着保证金，用可用会把参考量压小）；
        # 算不出（权益/保证金未知）→ 只按杠杆判（上面那道闸门）""",
        "new": """        # MUT 不拦杠杆（原判断整段删掉）""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "65 杠杆不一致 → 失败",
    },
    {
        "name": "M213 快速跟随被拒不留痕（页面无原因、事后查不到）",
        "module": "alphafox_live",
        "old": """                storage.live_trade_log_insert(lc, {
                    "token": token, "strategy": "alphafox",
                    "action": "quick_follow_blocked",
                    "paper_position_id": pp["id"],
                    "details_json": json.dumps({"reason": reason})})""",
        "new": """                pass  # MUT 不记日志""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "65 被拒写 live_trade_log",
    },
    {
        "name": "M214 残留记录收敛后不作废旧快照（本轮白跳过、实盘晚一轮才开）",
        "module": "alphafox_live",
        "old": """                live = None
                # ex_row 是本轮开头取的快照，刚被上面平掉 → 必须作废：否则目标
                # 差额会减掉一个已经不存在的仓位，本轮白跳过（比例模型按目标量
                # 下单，旧模型按"份"增量下单时看不出来）
                ex_row = None""",
        "new": """                live = None  # MUT 不作废快照""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "39 残留记录被收敛后新开一份",
    },
    {
        "name": "M215 快速跟随不认领已有记录（每次点都当新建 → 纸盘/记录对不上）",
        "module": "alphafox_live",
        "old": """        if lv:
            # 已有记录：用（或补写）它**开仓时冻结的** k —— 随后那轮对账会把""",
        "new": """        if False and lv:  # MUT 不认领
            # 已有记录：用（或补写）它**开仓时冻结的** k —— 随后那轮对账会把""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "65 已有记录 → 已跟（幂等）",
    },
    {
        "name": "M216 快速跟随后又按现状冻 k（目标钉死在旧规模 → 点了等于没点）",
        "module": "alphafox_trade",
        "old": """            lr = alphafox_live.quick_follow_live(token, side, qty)
            r["live"] = lr.get("reason") if not lr.get("ok") else lr.get("action")
            results.append(r)""",
        "new": """            lr = alphafox_live.quick_follow_live(token, side, qty)
            if lr.get("ok"):
                alphafox_live.on_reattach(token, side)  # MUT 按现状重冻
            r["live"] = lr.get("reason") if not lr.get("ok") else lr.get("action")
            results.append(r)""",
        "script": "extra/_scenario_alphafox_quickfollow.py",
        "caught_by": "Q4 交易员加仓后再点：实盘补齐到 0.25",
    },
    {
        "name": "M217 快速跟随不解豁免（存量仓此后永不镜像）",
        "module": "alphafox_live",
        "old": """    _exempt_remove_token_side(token, side)
    _QF_EXPLICIT.add(_key_of(token, side))""",
        "new": """    pass  # MUT 不解豁免
    _QF_EXPLICIT.add(_key_of(token, side))""",
        "script": "extra/_scenario_alphafox_quickfollow.py",
        "caught_by": "Q9 豁免仓快速跟随：解豁免并真跟",
    },
    {
        "name": "M218 快速跟随不看实盘开关（关着也报已跟、编造实盘动作）",
        "module": "alphafox_live",
        "old": """    if not _af_live_enabled():
        # 实盘开关关着：纸盘照建（调用方已经建好了），实盘什么都不做。
        # 这里**不写 blocked 日志**——不是异常，是用户自己的开关状态；
        # 也不解豁免（关着的时候建出来的仓本来就该进豁免清单）
        return {"ok": False, "action": "跳过",
                "reason": "实盘开关关闭（开启后再点一次即可跟）"}""",
        "new": """    if False:  # MUT 不看开关
        return {"ok": False, "action": "跳过", "reason": "unreachable"}""",
        "script": "extra/_scenario_alphafox_quickfollow.py",
        "caught_by": "Q10 开关关着：实盘如实回报",
    },
    {
        "name": "M237 _ex_match 不判 None（交易所不可用被当'确实没这笔仓'）",
        "module": "alphafox_live",
        "old": """    if not ex_positions:
        return None   # None = API 不可用（与空列表"确实没仓"区分），一律当"查不到\"""",
        "new": """    pass  # MUT 去掉 None 守卫""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "71 交易所快照为 None/空 → _ex_match 不抛",
    },
    {
        "name": "M238 恢复托管遇交易所不可用也落定（旧 k 继续生效 → 砍手动仓）",
        "module": "alphafox_live",
        "old": """        if ex_positions is None:
            return False    # ★ 交易所不可用 → 不落定（见 docstring）""",
        "new": """        if ex_positions is None:
            pass  # MUT 不可用也照常往下走（落定）""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "71 交易所查询不可用：on_reattach 返回 False",
    },
    {
        "name": "M239 实盘侧未就绪也转托管中（k 没冻成，状态却已落定、永远修不回）",
        "module": "alphafox_trade",
        "old": """        if not ready:
            print(f"[alphafox] 恢复托管 {token} 实盘侧未就绪（交易所查询不可用？）"
                  f"→ 留在恢复中，下轮重试")
            continue""",
        "new": """        if False:  # MUT 未就绪也落定
            continue""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "59 实盘侧未就绪：仍是恢复中(2)",
    },
    {
        "name": "M240 apply_signal 不看 executed（纸盘没执行/被拒的信号也去动实盘）",
        "module": "alphafox_live",
        "old": """    if not sig.get("executed"):
        return  # 纸盘未执行（SKIP/失败）→ 实盘不动""",
        "new": """    if False:  # MUT 不看 executed
        return""",
        "script": "extra/_scenario_alphafox_cycle.py",
        "caught_by": "I5 恢复中遇交易员减仓：实盘零下单（旧 k 不参与下单）",
    },
    {
        "name": "M241 历史分页不切片（把整份历史都发下来）",
        "module": "web",
        "old": """    return {**ov, "history": (ov.get("history") or [])[:n]}""",
        "new": """    return ov  # MUT 不切片""",
        "script": "extra/_check_st_board.py",
        "caught_by": "默认一页 20 条（不是把全部历史都发下来）",
    },
    {
        "name": "M242 历史分页原地改缓存（小页请求把共享缓存截短）",
        "module": "web",
        "old": """    return {**ov, "history": (ov.get("history") or [])[:n]}""",
        "new": """    ov["history"] = (ov.get("history") or [])[:n]   # MUT 原地改缓存对象
    return ov""",
        "script": "extra/_check_st_board.py",
        "caught_by": "小页请求不会把缓存截短（再要 40 条仍是 40）",
    },
    {
        "name": "M243 历史分页不夹取（limit 想给多少给多少）",
        "module": "web",
        "old": """    n = max(_ST_HIS_STEP, min(int(history_limit or _ST_HIS_STEP), _ST_HIS_MAX))""",
        "new": """    n = int(history_limit or _ST_HIS_STEP)   # MUT 不夹取""",
        "script": "extra/_check_st_board.py",
        "caught_by": "超上限封顶 200 条 / 低于下限回落成一页",
    },
    {
        "name": "M244 实盘跳过只打日志不记原因（页面看不出实盘那一腿没建）",
        "module": "alphafox_live",
        "old": """    _live_skip[_key_of(token, side)] = (why, time.time())
    print(f"[af-live] {token} {why}")""",
        "new": """    print(f"[af-live] {token} {why}")  # MUT 不记原因""",
        "script": "extra/_check_af_board.py",
        "caught_by": "托管中 + 无实盘记录 → 带出实盘层的跳过原因",
    },
    {
        "name": "M245 实盘建起来后不清原因（页面一直挂着过期的「实盘未建立」）",
        "module": "alphafox_live",
        "old": """    _clear_skip(vals.get("token") or "", vals.get("side") or "LONG")""",
        "new": """    pass  # MUT 不清原因""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "75 实盘记录建起来 → 原因自动清掉",
    },
    {
        "name": "M246 live 库读不到时按空集算（把「查不到」误报成「实盘未建立」）",
        "module": "web",
        "old": """    live_active_pids = None      # None = 查不到 → **不提示**（空集会把"读不到"误报成"没建"）""",
        "new": """    live_active_pids = set()     # MUT 读不到也当空集""",
        "script": "extra/_check_af_board.py",
        "caught_by": "9 live 库读不到 → 一行都不提示",
    },
    {
        "name": "M247 纸盘收尾不清「实盘没跟上」的原因（重开同币会显示上一笔的原因）",
        "module": "alphafox_live",
        "old": """        _clear_skip(token, side)
        print(f"[af-live] {token} {side} 纸盘仓收尾（{advice}）\"""",
        "new": """        print(f"[af-live] {token} {side} 纸盘仓收尾（{advice}）\"""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "75 纸盘那一笔收尾 → 原因一起清掉",
    },
    {
        "name": "M248 实盘页手动平仓不清「实盘没跟上」的原因（同 M247，另一条平仓路径）",
        "module": "alphafox_live",
        "old": """            _clear_skip(token, pos.get("side") or "LONG")""",
        "new": """            pass  # MUT 不清原因""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "32 实盘页手动平仓 → 「没跟上」的原因也清掉",
    },
    {
        "name": "M249 交易员动作流也过滤 executed（又变回「只推我们执行了的」）",
        "module": "tg_notify",
        "old": """        if s.get("retry"):
            continue          # 每轮重发 → 刷屏；执行成功那轮会正常推""",
        "new": """        if s.get("retry") or not s.get("executed"):   # MUT 也过滤未执行
            continue""",
        "script": "extra/_check_tg_signals.py",
        "caught_by": "未执行成的 CLOSE 也推",
    },
    {
        "name": "M250 交易员动作流不拦 retry（同一笔每轮重发 → 刷屏）",
        "module": "tg_notify",
        "old": """        if s.get("retry"):
            continue          # 每轮重发 → 刷屏；执行成功那轮会正常推""",
        "new": """        pass  # MUT 不拦 retry""",
        "script": "extra/_check_tg_signals.py",
        "caught_by": "retry 仍不推",
    },
    {
        "name": "M251 交易员动作流丢掉 SKIP（交易员开了、我们跳过 → 完全不推）",
        "module": "tg_notify",
        "old": """    act_icon = {"OPEN": "🚀", "ADD": "➕", "REDUCE": "➖", "CLOSE": "✅",
                "SKIP": "⏭️"}""",
        "new": """    act_icon = {"OPEN": "🚀", "ADD": "➕", "REDUCE": "➖", "CLOSE": "✅"}  # MUT 不推 SKIP""",
        "script": "extra/_check_tg_signals.py",
        "caught_by": "SKIP 也推（= 交易员开仓、我们跳过）",
    },
    {
        "name": "M252 币种识别不验证可交易集合（BGBTC 这种假 token 也当币开仓）",
        "module": "event_coins",
        "old": """        if tk in seen or tk not in tradable:
            continue""",
        "new": """        if tk in seen:   # MUT 不验证可交易
            continue""",
        "script": "extra/test_crypto_event.py",
        "caught_by": "2 假 token（BGBTC 不在可交易集合）被挡掉",
    },
    {
        "name": "M253 首轮就回填历史（几百条过期事件全灌进来 + 每条一次 AI）",
        "module": "event_agent",
        "old": """    watermark = int(wm) if wm.isdigit() else None""",
        "new": """    watermark = int(wm) if wm.isdigit() else 0   # MUT 首轮也回填""",
        "script": "extra/test_crypto_event.py",
        "caught_by": "6 首轮：只记水位、不回填",
    },
    {
        "name": "M254 不看事件新鲜度（过期新闻照样开仓）",
        "module": "event_agent",
        "old": """        if not _fresh(r["ts"], cfg["fresh"]):
            _skip(f"事件过期（>{cfg['fresh']} 分钟）")
            continue""",
        "new": """        if False:  # MUT 不看新鲜度
            continue""",
        "script": "extra/test_crypto_event.py",
        "caught_by": "7 只开了 1 笔（中性/低置信度/过期都被挡）",
    },
    {
        "name": "M255 页面少了 <script> 标签（JS 当正文渲染 → 满屏乱码）",
        "module": "web",
        "old": """</div>
<script>
\"\"\" \"\"\"
const EV_STEP = 20;""",
        "new": """</div>
\"\"\" \"\"\"
const EV_STEP = 20;""",
        "script": "extra/_check_pages_js.py",
        "caught_by": "页面检查：有 JS 却没有 <script> 标签",
    },
    {
        "name": "M256 TRADIFI 永续不进符号表（CL/NVDA 又被当成「币安无该永续」）",
        "module": "market",
        "old": """    tradifi = {s.get("baseAsset", "").upper()
               for s in data.get("symbols", [])
               if (s.get("contractType") == "TRADIFI_PERPETUAL"
                   and s.get("quoteAsset") == "USDT"
                   and s.get("status") == "TRADING")}""",
        "new": """    tradifi = set()  # MUT 不收 TRADIFI""",
        "script": "extra/test_market_symbols.py",
        "caught_by": "TRADIFI 单独收一份 / has_tradable_perp：TRADIFI 为 True",
    },
    {
        "name": "M257 跟单又拿 has_perpetual 判可交易（快速跟随静默跳过 CL）",
        "module": "alphafox_trade",
        "old": """from market import get_mark_price, has_tradable_perp""",
        "new": """from market import get_mark_price, has_tradable_perp as has_perpetual  # MUT""",
        "script": "extra/test_market_symbols.py",
        "caught_by": "alphafox_trade 已不再引用 has_perpetual（两处调用点都换了）",
    },
    {
        "name": "M258 快速跟随的资金预检拦到认领路径（可用余额被压干 → 收编不了已有的仓）",
        "module": "alphafox_live",
        "old": """        if lv is None and ex_row is None:""",
        "new": """        if lv is None:   # MUT 不区分认领""",
        "script": "extra/test_alphafox_live.py",
        "caught_by": "交易所有我们的仓 + 可用余额不足：仍认领",
    },
    {
        "name": "M259 阶段1 不查平仓证据（平台丢行/空包被当成交易员平仓 → 误平+重开）",
        "module": "alphafox_trade",
        "old": """            if not _tp_closed(data, key):
                # 平台成交流水里没有平仓点 → 判为快照抖动（丢行/空包），不许平：
                # 误平是真金白银（2026-09-29 CL 事故）。等证据（下轮证据到了正常平），
                # 超时只发 TG 提醒；本仓保留在基线里（见函数尾部），
                # 否则快照恢复时同一个老仓会变成"cur 有 prev 无" → 被当新开仓真下单
                held_back.add(key)
                _note_jitter(trader_id, key)
                continue""",
        "new": """            if False:   # MUT 不查平仓证据（2026-09-29 事故版）
                continue""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "60 抖动：不发平仓信号 / 60 抖动：纸盘仓还在",
    },
    {
        "name": "M260 判抖动的仓照样从基线里抹掉（恢复后重开真下单）",
        "module": "alphafox_trade",
        "old": """            _prev_snap[trader_id] = {**cur, **{k: prev[k] for k in held_back}}""",
        "new": """            _prev_snap[trader_id] = cur   # MUT 抖动仓也被抹掉""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "60 恢复后不重开（基线没被清空）",
    },
    {
        "name": "M261 孤儿确认不查平仓证据（抖动期间误平孤儿仓）",
        "module": "alphafox_trade",
        "old": """        if not _tp_closed(data or {}, key):
            _note_jitter(trader_id, key)
            continue
        if _close_our_position(trader_id, key, None, params):""",
        "new": """        if False:   # MUT 孤儿确认不查平仓证据
            continue
        if _close_our_position(trader_id, key, None, params):""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "63 抖动：不确认平仓",
    },
    {
        "name": "M262 恢复托管不查平仓证据（抖动期间把仓标成已脱离）",
        "module": "alphafox_trade",
        "old": """            if not _tp_closed(data or {}, key):
                # 平台成交流水里没有平仓点 → 快照抖动：不标"已脱离"、也不喂给下面的
                # 连续缺失计数（标记后守卫全拦，只有用户再点一次才能解除）
                _note_jitter(trader_id, key)
                continue
            mk = (trader_id, pos["id"])""",
        "new": """            if False:   # MUT 恢复托管不查平仓证据
                continue
            mk = (trader_id, pos["id"])""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "62 抖动不标已脱离",
    },
    {
        "name": "M263 抖动逃逸阀静默（扣留超时也不提醒，用户永远不知道）",
        "module": "alphafox_trade",
        "old": """    try:
        import tg_notify
        threading.Thread(
            target=tg_notify.send_alphafox_jitter_alert,
            args=(_token_of(key[0]), key[1], mins), daemon=True).start()
    except Exception as e:
        print(f"[alphafox] 抖动提醒发送失败: {e}")""",
        "new": """    pass  # MUT 逃逸阀不发提醒""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "61 超时 → 发一条 TG 提醒",
    },
    {
        "name": "M264 旁证判据不防脏包（tradePoints 给成 list → AttributeError 打断整轮）",
        "module": "alphafox_trade",
        "old": """    if not isinstance(tps, dict):
        # 脏包（tradePoints 给成 list/str 之类）：当"没有旁证"处理——
        # 直接 .get 会抛 AttributeError 把整轮 process 打断（上游什么都可能给）
        return False""",
        "new": """    # MUT 不防脏包""",
        "script": "extra/test_alphafox_trade.py",
        "caught_by": "66 脏包不抛异常",
    },
    {
        "name": "M265 美股标的不过 TradFi 白名单（未上市的公司也开仓）",
        "module": "stock_coins",
        "old": """        if not tk or tk in seen or not _tradable(tk):
            return""",
        "new": """        if not tk or tk in seen:   # MUT 不验 TradFi 白名单
            return""",
        "script": "extra/test_stock_event.py",
        "caught_by": "京东/百度/蔚来/波音/星巴克（白名单外）被挡",
    },
    {
        "name": "M266 指数被收进来（标普→SPY，违背「不做指数」）",
        "module": "stock_coins",
        "old": """    "泡泡玛特": "POPMART",""",
        "new": """    "泡泡玛特": "POPMART",
    "标普": "SPY", "标普500": "SPY",   # MUT 把指数也映射了""",
        "script": "extra/test_stock_event.py",
        "caught_by": "标普/纳指/道琼斯不映射",
    },
    {
        "name": "M267 每条消息的标的上限失效（要闻整理一条能带出 7 个）",
        "module": "stock_coins",
        "old": """    return [t for _, _, t in hits[:n]]""",
        "new": """    return [t for _, _, t in hits]   # MUT 不限个数""",
        "script": "extra/test_stock_event.py",
        "caught_by": "≤3 个上限 / max_n 生效",
    },
    {
        "name": "M268 英文名回退 \\b 词边界（MetaMask 被当成 Meta；中文句尾也匹配不到）",
        "module": "stock_coins",
        "old": """    return re.compile(r"(?<![A-Za-z])" + re.escape(name) + r"(?![A-Za-z])",
                      re.IGNORECASE)""",
        "new": """    return re.compile(r"\\b" + re.escape(name) + r"\\b", re.IGNORECASE)   # MUT""",
        "script": "extra/test_stock_event.py",
        "caught_by": "MetaMask ≠ Meta / 中文句子里的 AMD 能认",
    },
    {
        "name": "M269 美股事件交易门槛失效（低置信度判定也开仓）",
        "module": "event_agent",
        # 锚点必须带上一行"利好/利空"：加密管线的 SQL 尾部与它逐字相同，
        # 不带就成了"改加密那处"（M269 第一版就是这么被漏掉的）
        "old": """            "AND ai.impact IN ('利好','利空','利多') "
            "AND ai.confidence >= ? ORDER BY ai.id", (cfg["min_conf"],))]""",
        "new": """            "AND ai.impact IN ('利好','利空','利多') "
            "AND ai.confidence >= 0 ORDER BY ai.id", ())]  # MUT 门槛失效""",
        "script": "extra/test_stock_event.py",
        "caught_by": "护栏：过期 + 低置信度各留一条未下单原因",
    },
    {
        "name": "M270 美股事件首轮就回填历史（几百条过期快讯全灌进来）",
        "module": "event_agent",
        "old": """    with storage.get_conn(config.STOCK_EVENT_DB_PATH) as conn:
        wm = storage.stock_event_meta_get(conn, "watermark_msg_id", "")
    watermark = int(wm) if wm.isdigit() else None""",
        "new": """    with storage.get_conn(config.STOCK_EVENT_DB_PATH) as conn:
        wm = storage.stock_event_meta_get(conn, "watermark_msg_id", "")
    watermark = int(wm) if wm.isdigit() else 0   # MUT 首轮也回填""",
        "script": "extra/test_stock_event.py",
        "caught_by": "首轮只记水位、不回填",
    },
    {
        "name": "M271 商品区间去重失效（「布伦特原油」算成 BZ+CL，一处引用开两个仓）",
        "module": "stock_coins",
        "old": """        if any(span[0] < c[1] and c[0] < span[1] for c in claimed):
            continue""",
        "new": """        if False:   # MUT 不去重
            continue""",
        "script": "extra/test_stock_event.py",
        "caught_by": "「布伦特原油」只是一处引用 → 只算 BZ",
    },
    {
        "name": "M272 中文短名的下一个字排除失效（高通胀→高通、现代化→现代汽车）",
        "module": "stock_coins",
        "old": """_NEG_NEXT = {"高通": "胀", "现代": "化"}""",
        "new": """_NEG_NEXT: dict = {}   # MUT 不排除""",
        "script": "extra/test_stock_event.py",
        "caught_by": "「高通胀」不是高通 / 「现代化」不是现代汽车",
    },
    {
        "name": "M273 反转平仓开关失效（设置关也市价平仓）",
        "module": "supertrend_strategy",
        "old": """    if close_on_reverse and active_side is not None:""",
        "new": """    if active_side is not None:  # MUT 开关失效""",
        "script": "extra/test_supertrend_logic.py",
        "caught_by": "37 设置关=原逻辑（翻转不主动平仓）",
    },
    {
        "name": "M274 反转平仓方向判反（顺向翻转也平仓）",
        "module": "supertrend_strategy",
        "old": """        against = ((active_side == "LONG" and sig["trend"] == "DOWN")
                   or (active_side == "SHORT" and sig["trend"] == "UP"))""",
        "new": """        against = ((active_side == "LONG" and sig["trend"] == "UP")
                   or (active_side == "SHORT" and sig["trend"] == "DOWN"))  # MUT 判反""",
        "script": "extra/test_supertrend_logic.py",
        "caught_by": "37 顺向不平 / 逆势平仓",
    },
    {
        "name": "M275 反转平仓不带假设滑点（纸面按现价平、高估收益）",
        "module": "supertrend_strategy",
        "old": """                slip_pct=config.TRADING_STOP_SLIPPAGE_PCT, price=price)""",
        "new": """                slip_pct=0.0, price=price)  # MUT 去掉滑点""",
        "script": "extra/test_supertrend_logic.py",
        "caught_by": "37 平仓价含 0.15% 滑点",
    },
    {
        "name": "M276 反转平仓加回新K线门槛（存量逆势仓/失败重试要等下一根K线）",
        "module": "supertrend_strategy",
        "old": """    reversed_closed = False
    active_side = pos["side"] if pos else (pending["side"] if pending else None)
    if close_on_reverse and active_side is not None:""",
        "new": """    reversed_closed = False
    active_side = pos["side"] if pos else (pending["side"] if pending else None)
    if close_on_reverse and new_candle and active_side is not None:  # MUT 加回门槛""",
        "script": "extra/test_supertrend_logic.py",
        "caught_by": "37 存量补平（同K线内）+ 取价恢复同K线重试",
    },
    {
        "name": "M277 web 保存接口漏写 close_on_reverse 列（开关静默不生效）",
        "module": "web",
        "old": "flip_market_tolerance_pct=?, close_on_reverse=? ",
        "new": "flip_market_tolerance_pct=? ",
        "script": "extra/_check_flip_wiring.py",
        "caught_by": "8 保存 API UPDATE 含该列",
    },
    {
        "name": "M278 每币 close_on_reverse 不读取（接线断：列存了也不生效）",
        "module": "supertrend_strategy",
        "old": """        tok_cor = t.get("close_on_reverse") in (1, "1", True)""",
        "new": """        tok_cor = False  # MUT 不读取""",
        "script": "extra/_check_flip_wiring.py",
        "caught_by": "8 端到端：反转后市价平仓（DB 列 → _run_round）",
    },
    {
        "name": "M279 平仓后不清陈旧活跃行（翻转轮的反手市价开被吞）",
        "module": "supertrend_strategy",
        "old": """                    pos = pending = None""",
        "new": """                    _ = 0  # MUT 不清陈旧活跃行""",
        "script": "extra/test_supertrend_logic.py",
        "caught_by": "37 翻转轮同轮反手（市价开空+挂单并存）",
    },
]

def run_one(m: dict) -> tuple:
    """跑一条变异（每个变异用独立临时库，可并行）"""
    r = subprocess.run(
        [sys.executable, "extra/_mut_runner.py", json.dumps(m, ensure_ascii=False)],
        capture_output=True, timeout=900)
    out = (r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace"))
    if "[MUT-EXEC-FAIL]" in out:
        # 变异后的模块**根本 exec 不起来**（变异文本写错/语法不对）→ 不能算"被抓住"：
        # import 型测试会因模块空壳直接崩，rc=1 是假阳性。按 rc=2 报成 [ERR]
        return m, 2, out.count("[FAIL]")
    return m, r.returncode, out.count("[FAIL]")

# 只跑名字里含指定关键词的变异（如 `python extra/_mutation_check.py M55 M56`）——
# 新增修复后先验这一条，不必等全量 56 条
# `--check-anchors`：只校验每个变异的 old 串在对应模块里还能找到（秒级）。
# 改了代码后先跑这个——锚点失效的变异会以 [ERR] 静默退化成"没测"，
# 不必等 5 分钟跑完全量才发现。
if "--check-anchors" in sys.argv:
    _mods = {}
    bad = []
    broken = []
    for _m in MUTATIONS:
        mod = _m["module"]
        if mod not in _mods:
            try:
                _mods[mod] = open(f"{mod}.py", encoding="utf-8").read()
            except OSError:
                _mods[mod] = ""
        if _m["old"] not in _mods[mod]:
            bad.append(_m["name"])
            continue
        # 变异后的源码必须还能编译：替换文本写错（少缩进/改成 if 没 body）会让模块
        # exec 失败 → import 型测试直接崩 → rc=1 被记成"被抓住" = 假阳性。
        # 这里秒级拦下（真跑一遍要 20 分钟才发现）。
        _mutated = _mods[mod].replace(_m["old"], _m["new"], 1)
        try:
            compile(_mutated, f"{mod}.py", "exec")
        except SyntaxError as e:
            broken.append(f"{_m['name']} —— 变异后语法错误: {e.msg}（第 {e.lineno} 行）")
    print(f"锚点校验: {len(MUTATIONS) - len(bad) - len(broken)}/{len(MUTATIONS)} 有效")
    for b in bad:
        print(f"  [失效] {b}")
    for b in broken:
        print(f"  [语法坏] {b}")
    sys.exit(1 if (bad or broken) else 0)

_filters = [a for a in sys.argv[1:] if a.strip()]
selected = [m for m in MUTATIONS
            if not _filters or any(f in m["name"] for f in _filters)]
if _filters:
    print(f"只跑匹配 {_filters} 的 {len(selected)}/{len(MUTATIONS)} 条变异\n")
if not selected:
    print("没有匹配的变异")
    sys.exit(2)

caught = 0
# 并行度：每条变异要起一个子进程跑整套测试，串行要 20+ 分钟。
# 默认 8（可 MUT_WORKERS=4 降回去压机器/排查偶发超时）
_workers = int(os.environ.get("MUT_WORKERS", "8"))
with ThreadPoolExecutor(max_workers=_workers) as pool:
    for m, rc, fails in pool.map(run_one, selected):
        if rc == 2:
            print(f"[ERR ] {m['name']} —— 变异未应用（代码已变，需更新用例）")
        elif rc != 0:
            caught += 1
            print(f"[抓住] {m['name']}（测试 exit={rc}, FAIL 断言={fails}）")
        else:
            print(f"[漏掉] {m['name']} —— 测试未抓住！需要补测试")
        sys.stdout.flush()

print(f"\n变异测试: {caught}/{len(selected)} 被测试抓住")
sys.exit(0 if caught == len(selected) else 1)
