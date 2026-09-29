"""测试币安 API Key 是否有效"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))

# 加载环境变量
for _key in ("BINANCE_API_KEY", "BINANCE_API_SECRET"):
    if not os.environ.get(_key):
        try:
            with open("/etc/environment") as _f:
                for _line in _f:
                    if _line.startswith(_key + "="):
                        os.environ[_key] = _line.strip().split("=", 1)[1].strip('"')
        except Exception:
            pass

import exchange

print("=== 测试币安 API Key ===")
print(f"Key: {os.environ.get('BINANCE_API_KEY', 'N/A')[:8]}...")

# 1. 测试余额查询
print("\n1. 查询 USDT 余额...")
bal = exchange.get_balance()
if bal > 0:
    print(f"   ✅ 成功: ${bal:.2f} USDT")
else:
    print(f"   ⚠️ 余额为 0 或查询失败")

# 2. 测试持仓查询
print("\n2. 查询当前持仓...")
positions = exchange.get_positions()
if positions:
    for p in positions:
        print(f"   {p['symbol']}: qty={p['quantity']} entry={p['entry_price']} pnl={p['unrealized_pnl']}")
else:
    print("   无当前持仓")

# 3. 测试挂单查询
print("\n3. 查询挂单...")
orders = exchange.get_open_orders()
print(f"   当前 {len(orders)} 个挂单")

print("\n=== 结论 ===")
if bal >= 0 and positions is not None:
    print("✅ API Key 认证成功，可以正常调用币安接口")
else:
    print("❌ API Key 认证失败，请检查配置")
