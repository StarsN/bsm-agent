"""测试：确认 pop 掉 OKX_API_KEY 后 _okx 能正常工作"""
import sys, os, subprocess, json

# 模拟当前 web 进程环境（有 OnchainOS 的 key）
env = os.environ.copy()
print("当前环境 OKX_API_KEY:", env.get("OKX_API_KEY", "(无)")[:20] + "...")

# Test 1: 继承环境（当前 _okx 的行为）
print("\n=== Test 1: 继承环境（现状） ===")
r = subprocess.run(
    ["okx", "--profile", "live", "news", "economic-calendar",
     "--region", "united_states", "--importance", "3", "--limit", "2", "--json"],
    capture_output=True, text=True, timeout=20)
print("rc:", r.returncode)
if r.returncode != 0:
    print("stderr:", r.stderr.strip()[:200])
else:
    print("stdout:", r.stdout[:200])

# Test 2: pop 掉后（修复方案）
print("\n=== Test 2: pop OKX_API_KEY ===")
env2 = os.environ.copy()
env2.pop("OKX_API_KEY", None)
env2.pop("OKX_SECRET_KEY", None)
env2.pop("OKX_PASSPHRASE", None)
r = subprocess.run(
    ["okx", "--profile", "live", "news", "economic-calendar",
     "--region", "united_states", "--importance", "3", "--limit", "2", "--json"],
    capture_output=True, text=True, timeout=20, env=env2)
print("rc:", r.returncode)
if r.returncode != 0:
    print("stderr:", r.stderr.strip()[:200])
else:
    data = json.loads(r.stdout)
    print("events:", len(data) if isinstance(data, list) else data)
    if isinstance(data, list) and data:
        print("first:", data[0].get("event"))

# Test 3: 模拟 dashboard _okx 调用
print("\n=== Test 3: 模拟 get_macro_events ===")
sys.path.insert(0, "/root/binance-monitor/bsm-agent")
from dashboard import get_macro_events
r = get_macro_events()
print("events:", len(r.get("all_events", [])))
print("next_event:", r.get("next_event"))
