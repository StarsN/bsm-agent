"""Automatic trading loop.

Default mode is paper trading. It evaluates the existing leaderboard and market
cache, creates paper limit orders when all entry rules pass, and manages the
-2% stop loss plus staged take profit.

Run:
    python auto_trader.py
"""
from __future__ import annotations

import sqlite3
import signal
import time

from rich.console import Console

import config
import storage
import trade_logic


console = Console()
_running = True
_last_lock_log_at = 0.0


def stop(*_):
    global _running
    _running = False
    console.print("\n[yellow]收到退出信号，自动交易循环准备停止...[/yellow]")


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)


def one_scan():
    with storage.get_conn() as conn:
        settings = storage.trading_settings_get(conn)

    with storage.get_conn() as conn:
        trade_logic.update_paper_positions(conn)

    if not settings.get("enabled"):
        return {"opened": 0, "enabled": False}

    if settings.get("mode") != "paper":
        # Live trading is deliberately not enabled in this implementation.
        return {"opened": 0, "enabled": True, "live_blocked": True}

    with storage.get_conn() as conn:
        candidates = trade_logic.build_trade_candidates(
            conn, limit=config.COMPOSITE_HEAT_TOP_N, passed_only=True)

    opened = 0
    for candidate in candidates:
        if not candidate.get("has_active_position"):
            with storage.get_conn() as conn:
                if trade_logic.open_paper_position(conn, candidate, settings):
                    opened += 1
                    console.print(
                        f"[green]模拟市价开多信号: {candidate['token']} "
                        f"price={candidate['price']:.8g}[/green]"
                    )
    return {"opened": opened, "enabled": True}


def main():
    storage.init_db()
    console.print("[green]=== 自动交易循环启动（默认模拟交易） ===[/green]")
    console.print("[dim]Web 面板里打开自动交易后，才会按规则生成模拟市价多单。[/dim]")
    last_cleanup_at = 0.0
    while _running:
        try:
            result = one_scan()
            if result.get("live_blocked"):
                console.print("[yellow]当前设置为 live，但实盘下单尚未启用；本轮跳过。[/yellow]")
            elif result.get("enabled") and result.get("opened"):
                console.print(f"[green]本轮新增模拟订单 {result['opened']} 个[/green]")

            # 每小时清理一次旧的 signal lock，防止表无限膨胀
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
            time.sleep(3)
            continue
        time.sleep(2)
    console.print("[green]自动交易循环已退出[/green]")


if __name__ == "__main__":
    main()
