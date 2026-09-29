#!/usr/bin/env python3
"""直测 V3 AI Regime LLM — 用 config 里的 AI_REGIME_API_KEY"""
import json, sys, os, time, urllib.request, urllib.error
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

key = getattr(config, "AI_REGIME_API_KEY", "")
base = getattr(config, "AI_REGIME_API_BASE", "")
model = getattr(config, "AI_REGIME_MODEL", "")

print(f"Key: {key[:20]}...")
print(f"Base: {base}")
print(f"Model: {model}")

if not key:
    print("FAIL: AI_REGIME_API_KEY 为空")
    sys.exit(1)

system = "你是加密货币分析师。用 JSON 回复：{\"regime\":\"CHOP\",\"conf\":50,\"judgment\":\"测试通过\"}"
user = "Alpha池: 上涨14%, 中位-2.6%, BTC 0.3%。请给出市场研判。"

body = json.dumps({
    "model": model,
    "messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ],
    "max_tokens": 1024,
    "temperature": 0.3,
    "reasoning_effort": "max",
}).encode("utf-8")

url = f"{base.rstrip('/')}/chat/completions"
req = urllib.request.Request(url, data=body, headers={
    "Content-Type": "application/json",
    "Authorization": f"Bearer {key}",
})

print(f"\nPOST {url}")
t0 = time.time()
try:
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8")
        elapsed = time.time() - t0
        print(f"HTTP {resp.status}, {len(raw)} bytes, {elapsed:.1f}s")
        data = json.loads(raw)
        if "choices" in data and data["choices"]:
            content = data["choices"][0]["message"]["content"]
            print(f"\n--- 返回内容 ---\n{content}")
        else:
            print(f"\n异常响应:\n{json.dumps(data, ensure_ascii=False, indent=2)[:500]}")
except urllib.error.HTTPError as e:
    body_str = e.read().decode("utf-8", errors="replace")[:500]
    elapsed = time.time() - t0
    print(f"HTTP {e.code}, {elapsed:.1f}s\n{body_str}")
except Exception as e:
    elapsed = time.time() - t0
    print(f"FAIL: {type(e).__name__}: {e} ({elapsed:.1f}s)")
