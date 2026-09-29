"""发测试消息到「交易员动作流」的 TG（真函数、真配置，格式与线上完全一致）。

用法：python extra/_send_tg_test.py
先发一条显眼的测试抬头，再用真函数发一条样例，方便确认端到端通没通。
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)

import config  # noqa: E402
import tg_notify  # noqa: E402

bot = config.ALPHAFOX_TRADER_TG_BOT_TOKEN
chat = config.ALPHAFOX_TRADER_TG_CHAT_ID
print(f"bot id={bot.split(':')[0]}  chat={chat}")

# ① 显眼的测试抬头（直接发，不经业务函数）
r = tg_notify.requests.post(
    tg_notify.TG_API.format(token=bot),
    json={"chat_id": chat, "parse_mode": "HTML",
          "text": "🔧 <b>测试消息</b>\n下面这条是「交易员动作流」的样例格式"},
    timeout=10)
print("① 测试抬头:", r.status_code, r.text[:160])

# ② 用真函数发样例（走的就是线上那条路径）
SIGS = [
    {"action": "OPEN", "executed": True, "label": "熬鹰资本", "symbol": "ETHUSDT",
     "side": "LONG", "price": 2783.2, "trader_notional": 967172},
    {"action": "ADD", "executed": False, "label": "熬鹰资本", "symbol": "SOLUSDT",
     "side": "LONG", "price": 2, "trader_notional": 200},
    {"action": "SKIP", "executed": False, "label": "熬鹰资本", "symbol": "DOGEUSDT",
     "side": "SHORT", "price": 0.21, "trader_notional": 12000},
]
tg_notify.send_alphafox_trader_actions(SIGS)
print("② 样例已发（若失败，上面会打印被拒原因）")
