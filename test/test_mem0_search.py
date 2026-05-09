"""对比 Mem0 向量搜索 vs 客户端关键词过滤"""
import config, json

if not config.MEM0_API_KEY:
    print("未配置 MEM0_API_KEY")
    exit(1)

from mem0 import MemoryClient
client = MemoryClient(api_key=config.MEM0_API_KEY)

from sync_memory import search_similar, _USER_ID

query = "ZEC"
print(f"=== 向量搜索 '{query}'（Mem0 原生）===")
r1 = client.search(query, filters={"user_id": _USER_ID}, top_k=10)
items = r1.get("results", []) if isinstance(r1, dict) else []
for i, item in enumerate(items):
    mem = item.get("memory", "") if isinstance(item, dict) else str(item)
    meta = item.get("metadata", {}) if isinstance(item, dict) else {}
    has_zec = "ZEC" in mem.upper() if mem else False
    print(f"  [{i+1}] score={item.get('score',0):.3f} token={meta.get('token','?')} has_ZEC={has_zec}")
    print(f"       {mem[:80]}")

print(f"\n=== 客户端关键词过滤 ===")
r2 = search_similar(query)
filtered = [m for m in r2 if query.upper() in m.get("memory", "").upper()]
print(f"  向量返回 {len(r2)} 条 → 关键词过滤后 {len(filtered)} 条")
for i, m in enumerate(filtered[:5]):
    print(f"  [{i+1}] {m['metadata'].get('token','?')}: {m['memory'][:80]}")

print(f"\n=== metadata 精确过滤 ===")
r3 = search_similar(query, token="ZEC")
print(f"  找到 {len(r3)} 条")
for i, m in enumerate(r3[:5]):
    print(f"  [{i+1}] {m['memory'][:80]}")
