"""
实盘全流程单元测试 — SOL token（dry-run，不实际下单）
测试: get_market_snapshot → 仓位计算 → exchange 连接验证
"""
import os, sys

sys.path.insert(0, os.path.dirname(__file__))

import config
# 环境变量方式在服务器 config.py 尚未部署，直接覆盖
config.BINANCE_API_KEY = ""
config.BINANCE_API_SECRET = ""
os.environ["BINANCE_API_KEY"] = config.BINANCE_API_KEY
os.environ["BINANCE_API_SECRET"] = config.BINANCE_API_SECRET

import config
from market import get_market_snapshot, get_klines_1h, has_perpetual
import exchange
import risk

TOKEN = "SOL"

print(f"=== 实盘全流程测试: {TOKEN} ===\n")

# 1. 检查合约
print("1. 检查币安合约...")
if not has_perpetual(TOKEN):
    print(f"   ❌ {TOKEN} 无币安合约，退出")
    sys.exit(1)
print(f"   ✅ {TOKEN} 有永续合约")

# 2. 获取行情
print("\n2. 获取行情快照...")
snap = get_market_snapshot(TOKEN, heavy=False)
if not snap or not snap.get("mark_price"):
    print(f"   ❌ 无法获取行情")
    sys.exit(1)
mark = snap["mark_price"]
print(f"   标记价: ${mark:.4f}")
print(f"   资金费率: {snap.get('funding_rate', 'N/A')}")

# 3. 测试 exchange 连接
print("\n3. 测试币安连接...")
bal = exchange.get_balance()
print(f"   USDT 余额: ${bal:.2f}")
positions = exchange.get_positions()
print(f"   当前持仓: {len(positions)} 个")
orders = exchange.get_open_orders()
print(f"   挂单: {len(orders)} 个")

# 4. 模拟仓位计算（不开仓，只走计算逻辑）
print("\n4. 模拟仓位计算...")
from trade_logic import _build_account_context
import storage
# 打开真实的纸盘连接来计算账户
with storage.get_conn() as test_conn:
    account = _build_account_context(test_conn, "onchain")
    settings = storage.trading_settings_get(test_conn)
print(f"   账户权益: ${account.equity:.2f}")
print(f"   可用余额: ${account.available_balance:.2f}")

klines = get_klines_1h(TOKEN, limit=30)
stop_pct, stop_mode = risk.compute_stop_distance_pct(klines)
print(f"   ATR 止损: {stop_pct:+.2f}% ({stop_mode})")

entry = mark * (1 + config.TRADING_ASSUMED_SLIPPAGE_PCT / 100)
sl = entry * (1 + stop_pct / 100)
leverage = config.TRADING_LEVERAGE
sizing = risk.compute_position_size(account, entry, sl, leverage, "half", side="LONG")
print(f"   入口价(含滑点): ${entry:.4f}")
print(f"   止损价: ${sl:.4f}")
print(f"   数量: {sizing.get('quantity', 0):.4f}")
print(f"   名义价值: ${sizing.get('notional', 0):.2f}")
print(f"   保证金: ${sizing.get('margin', 0):.2f}")

# 5. 测试 quantity rounding
print("\n5. 测试数量精度...")
try:
    from exchange import _round_qty
    raw_qty = sizing.get("quantity", 0)
    rounded = _round_qty(f"{TOKEN}USDT", raw_qty)
    print(f"   原始数量: {raw_qty}")
    print(f"   stepSize 对齐后: {rounded}")
except ImportError:
    print(f"   _round_qty 未部署（服务器 exchange.py 为旧版）")
    rounded = sizing.get("quantity", 0)

# 6. 总结
print("\n=== 测试结果 ===")
if bal > 0 and snap and sizing.get("quantity", 0) > 0:
    print(f"✅ 全流程通过 — 可正常开仓 {TOKEN} (quantity={rounded}, notional=${sizing['notional']:.0f})")
else:
    errors = []
    if bal <= 0: errors.append("余额为0")
    if not snap: errors.append("无行情")
    if sizing.get("quantity", 0) <= 0: errors.append(f"仓位计算: {sizing.get('note')}")
    print(f"❌ 失败: {', '.join(errors)}")
