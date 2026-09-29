"""
Binance 全市场强平数据流 — WebSocket !forceOrder@arr
后台 daemon 线程聚合，worker 每轮末 get_and_reset()
"""
import asyncio
import json
import threading
import time
import logging

try:
    import websockets
except ImportError:
    websockets = None

logger = logging.getLogger("liquidation")

# ── 按 token 聚合的周期数据 ──
_period_data: dict[str, dict[str, float]] = {}
_lock = threading.Lock()
_running = False
_thread = None


def _process_message(msg):
    """将一条强平消息累加到 _period_data"""
    try:
        data = json.loads(msg) if isinstance(msg, str) else msg
        order = data.get("o", {})
        symbol = order.get("s", "")
        if not symbol or not symbol.endswith("USDT"):
            return
        token = symbol.replace("USDT", "")
        side = "short" if order.get("S") == "SELL" else "long"
        try:
            price = float(order.get("p", 0) or 0)
            qty = float(order.get("q", 0) or 0)
        except (ValueError, TypeError):
            return
        usd = price * qty
        if usd <= 0:
            return
        with _lock:
            entry = _period_data.setdefault(token, {
                "liq_long_usd": 0.0, "liq_long_count": 0.0,
                "liq_short_usd": 0.0, "liq_short_count": 0.0,
            })
            entry[f"liq_{side}_usd"] += usd
            entry[f"liq_{side}_count"] += 1
    except Exception:
        pass


async def _ws_handler():
    """WebSocket 主循环，自动重连"""
    url = "wss://fstream.binance.com/ws/!forceOrder@arr"
    while _running:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                logger.info("爆仓 WS 已连接")
                while _running:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=60)
                        _process_message(msg)
                    except asyncio.TimeoutError:
                        pass
        except Exception as e:
            logger.warning(f"爆仓 WS 断开: {e}，5s 后重连")
            if _running:
                await asyncio.sleep(5)


def _run_loop():
    """daemon 线程入口"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_ws_handler())
    except Exception:
        pass


def start():
    """启动爆仓数据流（worker 初始化时调用）"""
    global _running, _thread
    if websockets is None:
        logger.warning("websockets 库未安装，跳过爆仓流")
        return
    if _running:
        return
    _running = True
    _thread = threading.Thread(target=_run_loop, daemon=True, name="liq-ws")
    _thread.start()
    logger.info("爆仓 WS 线程已启动")


def stop():
    """停止爆仓数据流"""
    global _running
    _running = False


def get_and_reset() -> dict[str, dict[str, float]]:
    """worker 每轮结束时调用，取走当前周期数据并清空"""
    global _period_data
    with _lock:
        data = dict(_period_data)
        _period_data.clear()
    return data
