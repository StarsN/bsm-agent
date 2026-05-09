"""Mem0 测试：metadata filter"""
import config, time, json

print("1. 连接 Mem0...")
from mem0 import MemoryClient

client = MemoryClient(api_key=config.MEM0_API_KEY)
user_id = "test-filter"

# 测试1: 不过滤
r1 = client.search("OI", filters={"user_id": user_id})
print(f"\n不过滤: {len(r1.get('results',[]))} 条")

# 测试2: 只查 CHIP（2种写法）
r2a = client.search("OI", filters={"user_id": user_id, "metadata": {"token": "CHIP"}})
print(f"metadata子结构: {len(r2a.get('results',[]))} 条")

# 打印CHIP那条的metadata
for r in r2a.get("results", []):
    m = r.get("metadata", {}) if isinstance(r, dict) else {}
    print(f"  token={m.get('token')} pnl={m.get('pnl')}")

print("\n✅ 测试通过")
