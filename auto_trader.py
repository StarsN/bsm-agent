"""Automatic trading loop — Agent decision execution mode.

Reads decisions from pending_decisions table (written by Agent via SKILL),
validates through risk.py, and executes paper trades.

Default mode is paper trading.

Run:
    python auto_trader.py
"""
from __future__ import annotations

import json
import sqlite3
import signal
import time
from datetime import datetime, timezone

from rich.console import Console

import config
import risk
import storage
import trade_logic
from market import has_perpetual


console = Console()
_running = True
_last_lock_log_at = 0.0


def stop(*_):
    global _running
    _running = False
    console.print("\n[yellow]收到退出信号，自动交易循环准备停止...[/yellow]")


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)


def execute_open(conn, decision: dict, settings: dict) -> dict:
    """执行 Agent 的开仓决策 —— 经过 risk.py 兜底检查"""
    token = decision["token"]

    # 防重复
    if storage.trade_has_active(conn, token):
        return {"ok": False, "reason": f"{token} 已有持仓"}

    # 合约过滤
    if not has_perpetual(token):
        return {"ok": False, "reason": f"{token} 无合约"}

    # signal_lock 防重
    signal_key = storage.leaderboard_signal_key(conn)
    if not storage.trade_signal_lock_acquire(conn, token, signal_key):
        return {"ok": False, "reason": f"{token} signal_lock 已占用"}

    mode = settings.get("mode") or "paper"

    # 实盘模式用 exchange 余额覆盖账户权益
    account = trade_logic._build_account_context(conn)
    if mode == "live":
        import exchange
        live_balance = exchange.get_balance()
        if live_balance > 0:
            locked = account.equity - account.available_balance
            account.equity = live_balance
            account.available_balance = max(live_balance - locked, 0)

    risk_decision = risk.check_account_risk(account, token)
    if not risk_decision.allowed:
        return {"ok": False, "reason": f"风控拒绝: {risk_decision.reason}"}

    # 实时价：先读 WebSocket 缓存，没有就调 REST 接口拿
    realtime = trade_logic._load_realtime(conn, token)
    price = float(realtime["mark_price"]) if realtime.get("mark_price") else None
    if not price:
        from market import get_mark_price
        price = get_mark_price(token)
    if not price or price <= 0:
        return {"ok": False, "reason": f"{token} 无行情"}

    # 系统用 ATR 自适应止损
    from market import get_klines_1h
    klines = get_klines_1h(token, limit=30)
    stop_pct, stop_mode = risk.compute_stop_distance_pct(klines)
    entry = price * (1 + config.TRADING_ASSUMED_SLIPPAGE_PCT / 100)
    sl = entry * (1 + stop_pct / 100)

    # 系统计算止盈
    risk_per_unit = entry - sl
    tp1 = entry + risk_per_unit * config.TRADING_TP1_R
    tp2 = entry + risk_per_unit * config.TRADING_TP2_R

    # 仓位 sizing
    tier = decision.get("tier", "full")
    leverage = float(settings.get("leverage") or config.TRADING_LEVERAGE)
    sizing = risk.compute_position_size(account, entry, sl, leverage, tier)

    if sizing.get("quantity", 0) <= 0:
        return {"ok": False, "reason": f"仓位计算: {sizing.get('note')}"}

    # === 实盘下单 ===
    exchange_order_id = None
    exchange_sl_id = None
    exchange_tp1_id = None
    exchange_tp2_id = None

    if mode == "live":
        import exchange
        sym = f"{token}USDT"
        qty = round(sizing["quantity"], 6)

        # 设杠杆
        if not exchange.set_leverage(sym, int(leverage)):
            return {"ok": False, "reason": "设置杠杆失败"}

        # 市价开多
        result = exchange.market_open_long(token, qty)
        if not result or result.get("status") != "FILLED":
            return {"ok": False, "reason": f"市价开仓失败: {result}"}

        exchange_order_id = result["order_id"]
        fill_price = result["price"] or price
        entry = fill_price * (1 + config.TRADING_ASSUMED_SLIPPAGE_PCT / 100)
        sl = entry * (1 + stop_pct / 100)
        tp1 = entry + (entry - sl) * config.TRADING_TP1_R
        tp2 = entry + (entry - sl) * config.TRADING_TP2_R

        # 挂止损止盈
        total_qty = float(result.get("quantity", qty))

        # TP1: 30%
        tp1_qty = round(total_qty * config.TRADING_TP1_CLOSE_PCT / 100, 6)
        if tp1_qty > 0:
            r1 = exchange.take_profit_order(token, "SELL", tp1_qty, tp1)
            if r1:
                exchange_tp1_id = r1.get("order_id")
                console.print(f"[green]  挂 TP1: {tp1_qty} @ {tp1}[/green]")

        # TP2: 30%
        tp2_qty = round(total_qty * config.TRADING_TP2_CLOSE_PCT / 100, 6)
        if tp2_qty > 0:
            r2 = exchange.take_profit_order(token, "SELL", tp2_qty, tp2)
            if r2:
                exchange_tp2_id = r2.get("order_id")

        # SL: 100%（TP1/TP2 触发后会自动减少，剩余仓位止损）
        sl_qty = total_qty
        r3 = exchange.stop_loss_order(token, "SELL", sl_qty, sl)
        if r3:
            exchange_sl_id = r3.get("order_id")
            console.print(f"[green]  挂 SL: {sl_qty} @ {sl}[/green]")

        console.print(f"[green]✓ 实盘开仓: {token} qty={total_qty} @ {fill_price}[/green]")

    # === 写 DB ===
    position = {
        "token": token,
        "symbol": f"{token}USDT",
        "side": "LONG",
        "status": "OPEN",
        "mode": mode,
        "margin_amount": sizing["margin"],
        "leverage": leverage,
        "notional": sizing["notional"],
        "quantity": sizing["quantity"],
        "entry_price": entry,
        "limit_price": entry,
        "current_price": price,
        "stop_loss_price": sl,
        "tp1_price": tp1,
        "tp2_price": tp2,
        "highest_price": price,
        "trailing_stop_price": None,
        "signal_snapshot": json.dumps({
            "source": "agent",
            "agent_decision": decision,
            "risk_meta": {
                "tier": tier,
                "stop_distance_pct": sizing.get("stop_distance_pct"),
                "risk_amount": sizing.get("risk_amount"),
                "sector": risk.sector_of(token),
            },
            "exchange": {
                "order_id": exchange_order_id,
                "sl_order_id": exchange_sl_id,
                "tp1_order_id": exchange_tp1_id,
                "tp2_order_id": exchange_tp2_id,
            },
        }, ensure_ascii=False),
        "open_reason": f"Agent决策 tier={tier} | {decision.get('reason', '')}",
        "advice": decision.get("reason", ""),
        "exchange_order_id": json.dumps({
            "open": exchange_order_id,
            "sl": exchange_sl_id,
            "tp1": exchange_tp1_id,
            "tp2": exchange_tp2_id,
        }) if mode == "live" else None,
    }

    ok = storage.trade_position_insert(conn, position)
    if ok:
        pos_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        storage.journal_add_open(
            conn, position_id=pos_id, token=token, price=entry, tier=tier,
            stop_loss=sl, tp1_price=tp1, tp2_price=tp2,
            reason=decision.get("reason", "Agent决策"),
            dimension_data=decision.get("dimension_data"),
            market_overview=decision.get("market_overview"),
            lesson_checked=decision.get("lesson_checked"),
            pending_decision_id=decision.get("id"),
            source_round=decision.get("source_round"),
            social_score=decision.get("social_score"),
        )
        # 回填系统实际执行价到 pending_decisions
        dec_id = decision.get("id")
        if dec_id:
            conn.execute(
                """UPDATE pending_decisions
                   SET entry_price = ?, stop_loss = ?, tp1_price = ?, tp2_price = ?
                   WHERE id = ?""",
                (entry, sl, tp1, tp2, dec_id),
            )
    return {"ok": ok}


def execute_close(conn, decision: dict) -> dict:
    """执行 Agent 的平仓决策"""
    token = decision["token"]
    positions = [
        p for p in storage.trade_open_positions(conn)
        if p["token"].upper() == token.upper()
    ]

    if not positions:
        return {"ok": False, "reason": f"{token} 无持仓可平"}

    # 获取当前价
    snap_row = storage.snapshot_get(conn, token)
    price = None
    if snap_row:
        snap = json.loads(snap_row["snapshot"])
        price = snap.get("mark_price")
    if not price:
        from market import get_mark_price
        price = get_mark_price(token)
    if not price:
        return {"ok": False, "reason": f"{token} 无价格"}

    for pos in positions:
        entry = float(pos.get("entry_price", 0))
        qty = float(pos.get("quantity", 0))
        closed_qty = float(pos.get("closed_qty", 0))
        open_qty = max(qty - closed_qty, 0)
        pnl = (price - entry) * open_qty
        realized = float(pos.get("realized_pnl", 0)) + pnl
        margin = float(pos.get("margin_amount", 1))

        storage.trade_position_update(conn, pos["id"], {
            "status": "CLOSED",
            "current_price": price,
            "closed_qty": qty,
            "realized_pnl": realized,
            "unrealized_pnl": 0,
            "pnl_pct": (realized / margin) * 100 if margin else 0,
            "advice": f"Agent平仓: {decision.get('close_reason', '')}",
            "closed_at": "__CURRENT_TIMESTAMP__",
        })

        # 写平仓 journal
        pnl_pct_val = (realized / margin) * 100 if margin else 0
        realtime = trade_logic._load_realtime(conn, token)
        close_snap = json.dumps({
            "price": price,
            "market": json.loads(snap_row["snapshot"]) if snap_row else None,
            "realtime": realtime,
        }, default=str, ensure_ascii=False)
        hold = None
        if pos.get("created_at"):
            try:
                from datetime import datetime, timezone
                created = datetime.fromisoformat(str(pos["created_at"]).replace("Z", "+00:00"))
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                delta = datetime.now(timezone.utc) - created
                hours, rem = divmod(int(delta.total_seconds()), 3600)
                mins = rem // 60
                hold = f"{hours}h{mins}m" if hours else f"{mins}m"
            except Exception:
                pass
        storage.journal_add_close(
            conn, order_id=pos["id"], token=token, price=price,
            reason=decision.get("reason", decision.get("close_reason", "Agent平仓")),
            pnl_pct=pnl_pct_val,
            close_reason=decision.get("close_reason", "agent"),
            hold_duration=hold, dimension_data=close_snap,
            market_overview=decision.get("market_overview"),
            pending_decision_id=decision.get("id"),
        )

        # 止损归档（复用 trade_logic 的标签逻辑）
        entry_snapshot = {}
        if pos.get("signal_snapshot"):
            try:
                entry_snapshot = json.loads(pos["signal_snapshot"])
            except Exception:
                pass
        exit_market = {}
        if snap_row:
            exit_market = {"snapshot": json.loads(snap_row["snapshot"])}
        tags = trade_logic._failure_tags(entry_snapshot, exit_market, {})
        storage.trade_loss_archive_add(conn, {
            "position_id": pos.get("id"),
            "token": token,
            "symbol": f"{token}USDT",
            "entry_price": entry,
            "exit_price": price,
            "realized_pnl": pnl,
            "pnl_pct": (pnl / margin) * 100 if margin else 0,
            "failed_reason": decision.get("close_reason", "Agent平仓"),
            "reason_tags": json.dumps(tags, ensure_ascii=False),
            "entry_snapshot": pos.get("signal_snapshot"),
            "exit_snapshot": json.dumps({
                "agent_close": True,
                "close_reason": decision.get("close_reason"),
                "exit_price": price,
            }, default=str, ensure_ascii=False),
        })


    return {"ok": True}


def one_scan():
    """每2秒执行一次：读 Agent 决策 → 风控检查 → 执行"""
    with storage.get_conn() as conn:
        settings = storage.trading_settings_get(conn)

    # 1. 先过期清理旧决策（避免积压的过期决策被错误执行）
    with storage.get_conn() as conn:
        expired = conn.execute(
            "UPDATE pending_decisions SET status = 'expired' "
            "WHERE status = 'pending' "
            "AND created_at < datetime('now', '-10 minutes')"
        ).rowcount
        if expired:
            console.print(f"[dim]已过期 {expired} 条旧决策[/dim]")

    # 2. 更新现有持仓（TP/SL/跟踪止盈）
    with storage.get_conn() as conn:
        trade_logic.update_paper_positions(conn)

    if not settings.get("enabled"):
        return {"opened": 0, "enabled": False}


    # 3. 从 pending_decisions 表取待执行决策
    with storage.get_conn() as conn:
        pending = conn.execute(
            "SELECT * FROM pending_decisions "
            "WHERE status = 'pending' "
            "ORDER BY created_at ASC "
            "LIMIT 5"
        ).fetchall()

    executed = 0
    for dec in pending:
        dec = dict(dec)
        with storage.get_conn() as conn:
            if dec["action"] == "open_long":
                result = execute_open(conn, dec, settings)
            elif dec["action"] == "close":
                result = execute_close(conn, dec)
            else:
                result = {"ok": False, "reason": f"未知action: {dec['action']}"}

            # 更新决策状态
            new_status = "consumed" if result.get("ok") else "rejected"
            conn.execute(
                "UPDATE pending_decisions "
                "SET status = ?, consumed_at = datetime('now'), reject_reason = ? "
                "WHERE id = ?",
                (new_status, result.get("reason", ""), dec["id"]),
            )

            if result.get("ok"):
                executed += 1
                console.print(
                    f"[green]✓ 执行: {dec['action']} {dec['token']} "
                    f"— {dec.get('reason', '')[:60]}[/green]"
                )
            else:
                console.print(
                    f"[yellow]✗ 拒绝: {dec['token']} — {result.get('reason', '')}[/yellow]"
                )

    return {"opened": executed, "enabled": True}


def main():
    storage.init_db()
    console.print("[green]=== 自动交易循环启动（Agent决策执行模式） ===[/green]")
    console.print("[dim]等待 Agent 写入 pending_decisions，自动执行开仓/平仓。[/dim]")
    console.print("[dim]持仓管理（TP/SL/跟踪止盈）由系统自动处理。[/dim]")
    last_cleanup_at = 0.0
    while _running:
        try:
            result = one_scan()
            if result.get("enabled") and result.get("opened"):
                console.print(f"[green]本轮执行 {result['opened']} 条 Agent 决策[/green]")

            # 每小时清理一次旧的 signal lock
            now = time.time()
            if now - last_cleanup_at >= 3600:
                try:
                    with storage.get_conn() as conn:
                        deleted = storage.trade_signal_lock_cleanup(
                            conn, config.TRADING_SIGNAL_LOCK_RETENTION_HOURS)
                    if deleted:
                        console.print(f"[dim]已清理 {deleted} 条过期 signal lock[/dim]")
                except Exception as e:
                    console.print(f"[dim]signal lock 清理失败: {e}[/dim]")
                last_cleanup_at = now
        except sqlite3.OperationalError as e:
            global _last_lock_log_at
            if "database is locked" not in str(e).lower():
                console.print(f"[red]自动交易循环错误: {e}[/red]")
                time.sleep(3)
                continue
            now = time.time()
            if now - _last_lock_log_at >= 30:
                console.print("[yellow]数据库正忙，本轮自动交易跳过，稍后重试。[/yellow]")
                _last_lock_log_at = now
        except Exception as e:
            console.print(f"[red]自动交易循环错误: {e}[/red]")
            import traceback
            traceback.print_exc()
            time.sleep(3)
            continue
        time.sleep(2)
    console.print("[green]自动交易循环已退出[/green]")


if __name__ == "__main__":
    main()
