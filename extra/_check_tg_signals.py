"""验证跟单 TG 推送只推"真执行成"的信号。

覆盖：executed=False 的信号（非归属仓/未跟随仓/已平仓）不能推出去——
否则 TG 上会出现"平仓/加仓"但实际什么都没发生（幽灵信号）。
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)

import config
import tg_notify

config.ALPHAFOX_TG_BOT_TOKEN = "test-token"
config.ALPHAFOX_TG_CHAT_ID = "test-chat"

sent = []


class _Resp:
    status_code = 200


def _fake_post(url, json=None, timeout=None):
    sent.append({**(json or {}), "_url": url})
    return _Resp()


tg_notify.requests.post = _fake_post

SIGS = [
    {"action": "OPEN", "executed": True, "label": "L", "symbol": "BTCUSDT",
     "side": "LONG", "price": 60000, "trader_notional": 100},
    {"action": "ADD", "executed": True, "label": "L", "symbol": "SOLUSDT",
     "side": "LONG", "price": 2, "trader_notional": 200},
    {"action": "CLOSE", "executed": False, "label": "L", "symbol": "ETHUSDT",
     "side": "LONG", "price": 3000, "trader_notional": 50},
    {"action": "ADD", "executed": False, "label": "L", "symbol": "ADAUSDT",
     "side": "LONG", "price": 1, "trader_notional": 50},
    {"action": "REDUCE", "executed": False, "retry": True, "label": "L",
     "symbol": "XRPUSDT", "side": "LONG", "price": 1, "trader_notional": 50},
    {"action": "SKIP", "executed": False, "label": "L", "symbol": "DOGEUSDT",
     "side": "LONG", "price": 1, "trader_notional": 50},
]

tg_notify.send_alphafox_signals(SIGS)
text = sent[0]["text"] if sent else ""
print("推送内容:\n" + text)

checks = [
    ("推送有发出（执行成的信号触发推送）", len(sent) == 1),
    ("执行成的 OPEN 推了", "BTCUSDT" in text),
    ("执行成的 ADD 推了", "SOLUSDT" in text),
    ("未执行成的 CLOSE 不推（幽灵平仓）", "ETHUSDT" not in text),
    ("未执行成的 ADD 不推（幽灵加仓）", "ADAUSDT" not in text),
    ("retry 信号不推", "XRPUSDT" not in text),
    ("SKIP 信号不推", "DOGEUSDT" not in text),
]

fails = 0
for name, ok in checks:
    print(("[PASS] " if ok else "[FAIL] ") + name)
    if not ok:
        fails += 1

# 全部未执行 → 一条都不推
sent.clear()
tg_notify.send_alphafox_signals([SIGS[2], SIGS[3]])
ok = len(sent) == 0
print(("[PASS] " if ok else "[FAIL] ") + "全部未执行 → 不推送")
if not ok:
    fails += 1

# ---------------------------------------------------------------- 第二条流
# 交易员动作流：**只看交易员干了什么**，不看我们跟没跟（executed 不过滤）
print("\n== 第二条流：交易员动作（不过滤 executed）==")
config.ALPHAFOX_TRADER_TG_CHAT_ID = ""          # 先验证"没配 = 不生效"
sent.clear()
tg_notify.send_alphafox_trader_actions(SIGS)
checks2 = [("chat_id 留空 → 整条流不生效（部署上去零变化）", len(sent) == 0)]

config.ALPHAFOX_TRADER_TG_BOT_TOKEN = ""        # 留空 → 回退用跟单 bot
config.ALPHAFOX_TRADER_TG_CHAT_ID = "trader-chat"
sent.clear()
tg_notify.send_alphafox_trader_actions(SIGS)
text2 = sent[0]["text"] if sent else ""
print("推送内容:\n" + text2)
checks2 += [
    ("未执行成的 CLOSE 也推（交易员平了）", "ETHUSDT" in text2),
    ("未执行成的 ADD 也推（交易员加了）", "ADAUSDT" in text2),
    ("SKIP 也推（= 交易员开仓、我们跳过）", "DOGEUSDT" in text2),
    ("retry 仍不推（每轮重发会刷屏）", "XRPUSDT" not in text2),
    ("执行成的信号照推", "BTCUSDT" in text2 and "SOLUSDT" in text2),
    ("抬头区别于跟单流", "交易员动作" in text2 and "跟单信号" not in text2),
    ("发到新 chat", bool(sent) and sent[0].get("chat_id") == "trader-chat"),
    ("token 留空 → 回退用跟单 bot", bool(sent) and "test-token" in sent[0]["_url"]),
]
# 全部未执行 → 跟单流一条都不推，交易员流**照样推**
sent.clear()
tg_notify.send_alphafox_trader_actions([SIGS[2], SIGS[3]])
checks2.append(("全部未执行 → 交易员动作流照样推", len(sent) == 1))
# 只有 LEV 之类的信号 → 两边都不推
sent.clear()
tg_notify.send_alphafox_trader_actions(
    [{"action": "LEV", "executed": True, "label": "L", "symbol": "BTCUSDT"}])
checks2.append(("只有 LEV → 不推（不在开/加/减/平里）", len(sent) == 0))
# 抖动提醒（平台快照消失但成交流水没有平仓记录）：只提醒、不动仓位
sent.clear()
tg_notify.send_alphafox_jitter_alert("CL", "SHORT", 7)
checks2.append(("抖动提醒：发出去了", len(sent) == 1))
_txt = sent[0]["text"] if sent else ""
checks2.append(("抖动提醒：带币种/方向/分钟",
                "CL" in _txt and "空单" in _txt and "7 分钟" in _txt))
checks2.append(("抖动提醒：说清「没有平仓记录 + 仓位未动 + 人工确认」",
                "没有平仓记录" in _txt and "仓位未动" in _txt
                and "手动平掉" in _txt))
checks2.append(("抖动提醒：走跟单 bot（不是交易员动作 bot）",
                bool(sent) and "test-token" in sent[0]["_url"]))

for name, ok in checks2:
    print(("[PASS] " if ok else "[FAIL] ") + name)
    if not ok:
        fails += 1

sys.exit(1 if fails else 0)
