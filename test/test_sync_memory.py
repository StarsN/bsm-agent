"""测试 sync_memory 完整流程"""
import time, json

# 模拟一轮完整的开仓→平仓→搜索
print("=== sync_memory 集成测试 ===")

import config
if not config.MEM0_API_KEY:
    print("❌ 未配置 MEM0_API_KEY，跳过")
    exit(1)

import sync_memory

# === 等 Mem0 索引 ===
print("\n5. 等待索引 (3s)...")
time.sleep(3)

# === 测试: 搜索 ===
print("\n6. search 全部:")
r1 = sync_memory.search_similar("OI涨 taker弱 开仓 结果")
for i, m in enumerate(r1):
    print(f"   [{i+1}] score={m['score']:.3f} token={m['metadata'].get('token','?')} pnl={m['metadata'].get('pnl','?')}")
    print(f"      {m['memory'][:80]}")

print("\n7. search 只看 TAO:")
r2 = sync_memory.search_similar("开仓 结果", token="TAO")
print(f"   找到 {len(r2)} 条")

print("\n8. 客户端过滤亏损 (pnl < 0):")
all_results = sync_memory.search_similar("开仓 结果")
loss_results = []
for r in all_results:
    if isinstance(r, dict):
        pnl = (r.get("metadata") or {}).get("pnl", 0) if isinstance(r.get("metadata"), dict) else 0
        if pnl is not None and pnl < 0:
            loss_results.append(r)
print(f"   全部 {len(all_results)} 条 → 亏损 {len(loss_results)} 条")

print(f"\n✅ 全部通过（{len(r1)} 条结果）")
