"""
链上监控实盘镜像桥接层。
纸盘逻辑不动，这一层只做：纸盘 → 币安实盘 的镜像同步。
"""
from __future__ import annotations

import json
import time
import config
import storage
import exchange


def _side_info(pos: dict) -> tuple:
    """从纸盘/实盘记录读取方向，返回 (close_side, position_side)"""
    is_short = pos.get("side", "LONG") == "SHORT"
    return ("BUY", "SHORT") if is_short else ("SELL", "LONG")


def mirror_open(paper_position: dict) -> bool:
    """纸盘开仓成功 → 币安同步开仓 + 挂 SL/TP 单。返回 True/False。"""
    token = paper_position["token"]
    symbol = f"{token}USDT"
    qty = float(paper_position.get("quantity") or 0)
    entry = float(paper_position.get("entry_price") or 0)
    sl = float(paper_position.get("stop_loss_price") or 0)
    tp1 = float(paper_position.get("tp1_price") or 0)
    tp2 = float(paper_position.get("tp2_price") or 0)
    leverage = int(paper_position.get("leverage") or 1)
    margin = float(paper_position.get("margin_amount") or 0)

    if qty <= 0 or entry <= 0:
        print(f"[live] mirror_open: {token} 参数无效 qty={qty} entry={entry}")
        return False

    orders = {"open": None, "sl": None, "tp1": None, "tp2": None}
    close_side, pos_side = _side_info(paper_position)

    try:
        # 1. 设杠杆
        if not exchange.set_leverage(symbol, leverage):
            err = exchange.get_last_error()
            print(f"[live] {token} 设杠杆失败: {err}")
            return False

        # 2. 市价开仓
        if pos_side == "SHORT":
            result = exchange.market_open_short(token, qty)
        else:
            result = exchange.market_open_long(token, qty)
        if not result or result.get("status") not in ("FILLED", "PARTIALLY_FILLED", "NEW"):
            err = exchange.get_last_error()
            print(f"[live] {token} 市价开仓失败: {result} {err}")
            return False
        orders["open"] = result["order_id"]
        # 市价单有时返回 NEW + 零值，从实际持仓回补
        fill_price = result.get("price") or 0
        actual_qty = result.get("quantity") or 0
        if fill_price <= 0 or actual_qty <= 0:
            positions = exchange.get_positions()
            ep = [p for p in positions if p["symbol"] == symbol]
            if ep:
                fill_price = ep[0].get("entry_price") or entry
                actual_qty = abs(ep[0].get("quantity") or qty)
            else:
                fill_price = entry
                actual_qty = qty
        print(f"[live] ✓ {token} 实盘开仓 qty={actual_qty} @ ${fill_price}")
    except Exception as e:
        print(f"[live] {token} mirror_open 开仓异常: {e}")
        return False

    # 3-5. SL/TP 单（best-effort，失败不影响）
    try:
        tp1_qty = round(actual_qty * config.TRADING_TP1_CLOSE_PCT / 100, 6)
        if tp1_qty > 0 and tp1 > 0:
            time.sleep(0.3)
            r1 = exchange.take_profit_order(token, close_side, tp1_qty, tp1, position_side=pos_side)
            if r1:
                orders["tp1"] = r1.get("order_id")
                print(f"[live]   TP1: {tp1_qty} @ {tp1}")
    except Exception as e:
        print(f"[live] {token} TP1 挂单异常: {e}")

    try:
        tp2_qty = round(actual_qty * config.TRADING_TP2_CLOSE_PCT / 100, 6)
        if tp2_qty > 0 and tp2 > 0:
            time.sleep(0.3)
            r2 = exchange.take_profit_order(token, close_side, tp2_qty, tp2, position_side=pos_side)
            if r2:
                orders["tp2"] = r2.get("order_id")
                print(f"[live]   TP2: {tp2_qty} @ {tp2}")
    except Exception as e:
        print(f"[live] {token} TP2 挂单异常: {e}")

    try:
        if sl > 0:
            time.sleep(0.3)
            r3 = exchange.stop_loss_order(token, close_side, actual_qty, sl, position_side=pos_side)
            if r3:
                orders["sl"] = r3.get("order_id")
                print(f"[live]   SL: {actual_qty} @ {sl}")
    except Exception as e:
        print(f"[live] {token} SL 挂单异常: {e}")

    # 6. 写 live DB
    try:
        storage.init_onchain_live_db()
        with storage.get_conn(config.LIVE_DB_PATH) as lc:
            storage.live_position_insert(lc, {
                "paper_position_id": paper_position["id"],
                "token": token, "symbol": symbol,
                "side": paper_position.get("side", "LONG"),
                "strategy": paper_position.get("strategy", "onchain"),
                "entry_price": fill_price, "quantity": actual_qty,
                "leverage": leverage, "margin_usd": margin,
                "exchange_orders_json": json.dumps(orders),
                "tp1_done": 0, "tp2_done": 0, "status": "OPEN",
            })
            storage.live_trade_log_insert(lc, {
                "token": token, "strategy": "onchain",
                "action": "open", "price": fill_price, "quantity": actual_qty,
                "paper_position_id": paper_position["id"],
                "exchange_order_id": str(orders["open"]),
            })

        # 每日快照
        _snapshot_daily(token, fill_price, actual_qty)
    except Exception as e:
        print(f"[live] {token} live DB 写入失败: {e}")

    return True


def sync_orders(paper_conn):
    """每轮调用：将纸盘状态变化同步到币安订单。"""
    try:
        live_mode = _get_live_setting(paper_conn)
        if live_mode != "live":
            return

        # 读取所有 onchain 策略的活跃纸盘持仓
        paper_positions = paper_conn.execute(
            "SELECT * FROM trade_positions WHERE strategy='onchain' AND status IN ('OPEN','PARTIAL','PENDING')"
        ).fetchall()

        with storage.get_conn(config.LIVE_DB_PATH) as lc:
            # 一次性查所有交易所持仓
            ex_positions = None
            try:
                ex_positions = exchange.get_positions()
            except Exception:
                pass

            for pp in paper_positions:
                pp = dict(pp)
                pid = pp["id"]
                live = storage.live_position_get_by_paper(lc, pid)
                if not live:
                    # 可能 mirror_open DB 写入失败 → 尝试恢复
                    _recover_live_record(lc, pp, ex_positions)
                    continue  # 尚未镜像，等待 mirror_open 或下轮恢复

                token = pp["token"]

                # --- 纸盘 CLOSED → 币安全平 ---
                if pp["status"] == "CLOSED" and live["status"] != "CLOSED":
                    _close_exchange_and_mark(lc, live, token, pp)
                    continue

                # --- 纸盘 TP2/追迹 → 全平 ---
                qty = float(pp.get("quantity") or 1)
                closed_qty = float(pp.get("closed_qty") or 0)
                if closed_qty >= qty * 0.99 and live["status"] != "CLOSED":
                    _close_exchange_and_mark(lc, live, token, pp)
                    continue

                # --- TP1 刚触发 → SL 移到保本 ---
                tp1_pct = config.TRADING_TP1_CLOSE_PCT / 100
                if closed_qty >= qty * tp1_pct - 1e-6 and not live["tp1_done"]:
                    _recreate_sl_breakeven(lc, live, token, pp)
                    continue

                # --- TP2 刚触发 → 挂追迹止盈 ---
                tp2_pct = (config.TRADING_TP1_CLOSE_PCT + config.TRADING_TP2_CLOSE_PCT) / 100
                if (closed_qty >= qty * tp2_pct - 1e-6
                        and live["tp1_done"]
                        and not live["tp2_done"]
                        and float(pp.get("trailing_stop_price") or 0) > 0):
                    _recreate_trailing_stop(lc, live, token, pp)
                    continue

                # --- 检查交易所持仓是否还在 ---
                try:
                    if ex_positions is not None:
                        ex_pos = next((p for p in ex_positions
                                       if p["symbol"] == f"{token}USDT"
                                       and abs(p["quantity"]) > 0), None)
                        if not ex_pos and live["status"] != "CLOSED":
                            print(f"[live] {token} 交易所持仓已消失，标记纸盘平仓")
                            _close_exchange_and_mark(lc, live, token, pp)
                except Exception as e:
                    print(f"[live] {token} 检查交易所持仓异常: {e}")

                # --- 安全网：缺少 SL 保护时补挂 ---
                if live["status"] != "CLOSED":
                    orders = _load_orders(live)
                    has_sl = orders.get("sl") or orders.get("trailing")
                    if not has_sl:
                        sl_price = float(pp.get("stop_loss_price") or 0)
                        remaining = float(pp.get("quantity") or 0) - float(pp.get("closed_qty") or 0)
                        if remaining > 0 and sl_price > 0:
                            try:
                                _cs, _ps = _side_info(pp)
                                r = exchange.stop_loss_order(token, _cs, remaining, sl_price, position_side=_ps)
                                if r:
                                    orders["sl"] = r.get("order_id")
                                    storage.live_position_update(lc, live["id"], {
                                        "exchange_orders_json": json.dumps(orders),
                                    })
                                    print(f"[live] {token} 安全网: 补挂 SL {remaining} @ {sl_price}")
                            except Exception as e:
                                print(f"[live] {token} 安全网补挂 SL 异常: {e}")

    except Exception as e:
        print(f"[live] sync_orders 异常: {e}")


def close_live_position(token: str):
    """手动平仓：撤销币安挂单 + 市价平仓。"""
    try:
        exchange.cancel_all_orders(token)
        time.sleep(0.3)
        # 从 live DB 读取持仓方向
        pos_side = "LONG"
        try:
            with storage.get_conn(config.LIVE_DB_PATH) as lc:
                row = lc.execute(
                    "SELECT side FROM live_positions WHERE token=? AND status IN ('OPEN','PARTIAL') ORDER BY id DESC LIMIT 1",
                    (token,),
                ).fetchone()
                if row:
                    pos_side = row["side"] or "LONG"
        except Exception:
            pass

        positions = exchange.get_positions()
        ex_pos = next((p for p in positions
                       if p["symbol"] == f"{token}USDT"
                       and abs(p["quantity"]) > 0), None)
        if ex_pos:
            qty = abs(float(ex_pos["quantity"]))
            result = exchange.close_position_market(token, qty, position_side=pos_side)
            if result:
                print(f"[live] ✓ {token} 实盘平仓 qty={result.get('quantity')} @ ${result.get('price')}")
                _record_close_in_db(token, ex_pos, result)
            else:
                print(f"[live] {token} 实盘平仓失败")
        else:
            print(f"[live] {token} 无交易所持仓，无需平仓")
    except Exception as e:
        print(f"[live] close_live_position {token} 异常: {e}")


# ---- internal helpers ----

def _get_live_setting(conn):
    try:
        row = conn.execute(
            "SELECT value FROM trading_settings WHERE key='onchain_live_mode'"
        ).fetchone()
        if row:
            return row["value"]
    except Exception:
        pass
    return config.ONCHAIN_LIVE_MODE


def _close_exchange_and_mark(lc, live, token, paper_pos):
    """撤销币安挂单 + 标记 live CLOSED"""
    try:
        exchange.cancel_all_orders(token)
        time.sleep(0.3)
        # 如果交易所仍有持仓，市价平仓
        ex_positions = exchange.get_positions()
        ex_pos = next((p for p in ex_positions
                       if p["symbol"] == f"{token}USDT"
                       and abs(p["quantity"]) > 0), None)
        if ex_pos:
            qty = abs(float(ex_pos["quantity"]))
            _, _ps = _side_info(live)
            r = exchange.close_position_market(token, qty, position_side=_ps)
            if r:
                print(f"[live] {token} 补平仓 qty={qty}")
    except Exception:
        pass
    storage.live_position_update(lc, live["id"], {
        "status": "CLOSED", "closed_at": "__CURRENT_TIMESTAMP__",
    })
    storage.live_trade_log_insert(lc, {
        "token": token, "strategy": "onchain",
        "action": "close",
        "price": paper_pos.get("current_price"),
        "quantity": paper_pos.get("closed_qty") or paper_pos.get("quantity"),
        "paper_position_id": paper_pos["id"],
        "details_json": json.dumps({"reason": "纸盘已平仓"}),
    })
    # 同步清理可建仓池 + 移除名单
    try:
        import config
        with storage.get_conn(config.ONCHAIN_DB) as oc:
            storage.onchain_pool_archive(oc, token, "closed")
            storage.onchain_removed_add(oc, token, "closed")
    except Exception:
        pass


def _recreate_sl_breakeven(lc, live, token, paper_pos):
    """TP1 触发 → 取消旧 SL → 重新挂 SL@保本价"""
    orders = _load_orders(live)
    old_sl = orders.get("sl")
    if old_sl:
        try:
            exchange.cancel_order(token, old_sl)
            orders["sl"] = None  # 标记已取消
        except Exception:
            pass

    qty = float(paper_pos.get("quantity") or 0)
    closed = float(paper_pos.get("closed_qty") or 0)
    remaining = round(qty - closed, 6)
    entry = float(paper_pos.get("entry_price") or 0)
    _cs, _ps = _side_info(paper_pos)

    if remaining > 0 and entry > 0:
        time.sleep(0.3)
        r = exchange.stop_loss_order(token, _cs, remaining, entry, position_side=_ps)
        if r:
            orders["sl"] = r.get("order_id")
            storage.live_position_update(lc, live["id"], {
                "tp1_done": 1,
                "status": "PARTIAL",
                "exchange_orders_json": json.dumps(orders),
            })
        else:
            # 新 SL 失败（如 minNotional）→ 保存已取消状态，让安全网能检测到
            storage.live_position_update(lc, live["id"], {
                "exchange_orders_json": json.dumps(orders),
            })
    else:
        storage.live_position_update(lc, live["id"], {"tp1_done": 1, "status": "PARTIAL"})
    print(f"[live] {token} TP1 触发 → SL 移到保本 ${entry}")


def _recreate_trailing_stop(lc, live, token, paper_pos):
    """TP2 触发 → 取消 SL → 挂追迹止盈"""
    orders = _load_orders(live)
    old_sl = orders.get("sl")
    if old_sl:
        try:
            exchange.cancel_order(token, old_sl)
            orders["sl"] = None  # 标记已取消，防止安全网误判
        except Exception:
            pass

    qty = float(paper_pos.get("quantity") or 0)
    closed = float(paper_pos.get("closed_qty") or 0)
    remaining = round(qty - closed, 6)

    if remaining > 0:
        time.sleep(0.3)
        _cs, _ps = _side_info(paper_pos)
        r = exchange.trailing_stop_order(
            token, _cs, remaining,
            callback_rate=config.TRADING_TRAIL_CALLBACK_PCT,
            position_side=_ps,
        )
        if r:
            orders["sl"] = r.get("order_id")  # 覆盖 SL 为 trailing
            orders["trailing"] = True
            storage.live_position_update(lc, live["id"], {
                "tp2_done": 1,
                "status": "PARTIAL",
                "exchange_orders_json": json.dumps(orders),
            })
        else:
            # 追迹失败 → 保存已取消状态，让安全网检测
            storage.live_position_update(lc, live["id"], {
                "exchange_orders_json": json.dumps(orders),
            })
    else:
        storage.live_position_update(lc, live["id"], {
            "tp2_done": 1, "status": "PARTIAL",
        })
    print(f"[live] {token} TP2 触发 → 追迹止盈 {config.TRADING_TRAIL_CALLBACK_PCT}%")


def _load_orders(live: dict) -> dict:
    try:
        return json.loads(live.get("exchange_orders_json") or "{}")
    except Exception:
        return {}


def _recover_live_record(lc, pp: dict, ex_positions: list | None):
    """mirror_open DB 写入失败时的恢复：交易所已有持仓 → 补建 live 记录"""
    token = pp["token"]
    symbol = f"{token}USDT"
    # 已有记录则跳过
    existing = storage.live_position_get_by_paper(lc, pp["id"])
    if existing:
        return
    if ex_positions:
        ex_pos = next((p for p in ex_positions
                       if p["symbol"] == symbol and abs(p["quantity"]) > 0), None)
        if ex_pos:
            try:
                storage.live_position_insert(lc, {
                    "paper_position_id": pp["id"],
                    "token": token, "symbol": symbol,
                    "side": pp.get("side", "LONG"),
                    "strategy": pp.get("strategy", "onchain"),
                    "entry_price": ex_pos.get("entry_price") or pp.get("entry_price"),
                    "quantity": abs(float(ex_pos.get("quantity", 0))),
                    "leverage": int(ex_pos.get("leverage", pp.get("leverage", 1))),
                    "margin_usd": pp.get("margin_amount", 0),
                    "exchange_orders_json": json.dumps({"open": None, "sl": None, "tp1": None, "tp2": None}),
                    "tp1_done": 0, "tp2_done": 0, "status": "OPEN",
                })
                storage.live_trade_log_insert(lc, {
                    "token": token, "strategy": "onchain",
                    "action": "open", "price": ex_pos.get("entry_price"),
                    "quantity": abs(float(ex_pos.get("quantity", 0))),
                    "paper_position_id": pp["id"],
                    "details_json": json.dumps({"reason": "恢复记录"}),
                })
                print(f"[live] {token} 恢复 live 记录（DB 写入曾失败）")
            except Exception as e:
                print(f"[live] {token} 恢复 live 记录失败: {e}")


def _record_close_in_db(token, ex_pos, result):
    try:
        with storage.get_conn(config.LIVE_DB_PATH) as lc:
            symbol = f"{token}USDT"
            live_rows = lc.execute(
                "SELECT * FROM live_positions WHERE token=? AND symbol=? AND status!='CLOSED'",
                (token, symbol),
            ).fetchall()
            for lr in live_rows:
                storage.live_position_update(lc, lr["id"], {
                    "status": "CLOSED", "closed_at": "__CURRENT_TIMESTAMP__",
                })
            storage.live_trade_log_insert(lc, {
                "token": token, "strategy": "onchain",
                "action": "close",
                "price": result.get("price"),
                "quantity": result.get("quantity"),
                "exchange_order_id": str(result.get("order_id")),
                "details_json": json.dumps({"reason": "手动平仓"}),
            })
    except Exception as e:
        print(f"[live] _record_close_in_db 异常: {e}")


def _snapshot_daily(token: str, price: float, qty: float):
    try:
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        bal = exchange.get_balance()
        positions = exchange.get_positions()
        pos = next((p for p in positions if p["symbol"] == f"{token}USDT"), None)
        margin_used = abs(float(pos["quantity"])) * price if pos else 0

        with storage.get_conn(config.LIVE_DB_PATH) as lc:
            storage.live_daily_snapshot_upsert(lc, {
                "date": today,
                "strategy": "onchain",
                "equity": bal,
                "available_balance": bal - margin_used,
                "margin_used": margin_used,
                "unrealized_pnl": float(pos.get("unrealized_pnl", 0)) if pos else 0,
                "realized_pnl": 0,
                "daily_pnl": 0,
            })
    except Exception:
        pass
