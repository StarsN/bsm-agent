#!/usr/bin/env python3
"""排查 dashboard 风控数据异常 — 分步测试每个 OKX CLI 调用"""
import subprocess, json, sys

def okx_raw(args, live=False):
    """直调 OKX CLI，返回完整 stdout/stderr 不做任何过滤"""
    cmd = ["okx"]
    if live:
        cmd += ["--profile", "live"]
    cmd += args + ["--json"]
    print(f"\n{'='*60}")
    print(f"CMD: {' '.join(cmd)}")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        print(f"RC={r.returncode}")
        if r.stderr.strip():
            print(f"STDERR: {r.stderr.strip()[:500]}")
        stdout = r.stdout.strip()
        if not stdout:
            print("STDOUT: (empty)")
            return None
        print(f"STDOUT (前300字): {stdout[:300]}")
        # 试解析
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            # 找第一个 { 或 [
            for i, ch in enumerate(stdout):
                if ch in ('{', '['):
                    try:
                        return json.loads(stdout[i:])
                    except json.JSONDecodeError as e2:
                        print(f"JSON parse from offset {i} also failed: {e2}")
                        break
            print("JSON parse failed for all offsets")
            return None
    except Exception as ex:
        print(f"EXCEPTION: {ex}")
        return None

def test(name, result, check_fn=None):
    """报告单测结果"""
    if result is None:
        print(f"[FAIL] {name} — 无数据")
        return False
    if isinstance(result, dict) and "error" in result:
        print(f"[FAIL] {name} — 返回 error")
        return False
    if check_fn:
        ok = check_fn(result)
        status = "[OK]" if ok else "[WARN]"
        print(f"{status} {name}")
        return ok
    print(f"[OK] {name}")
    return True

print("=" * 60)
print("dashboard 风控数据诊断")
print("=" * 60)

# 1. orderbook
ob = okx_raw(["market", "orderbook", "BTC-USDT-SWAP", "--sz", "20"])
if isinstance(ob, dict):
    bids = ob.get("bids", [])
    asks = ob.get("asks", [])
    print(f"  bids数: {len(bids)}, asks数: {len(asks)}")
    if bids and asks:
        bid0 = float(bids[0][0])
        ask0 = float(asks[0][0])
        spread = (ask0 - bid0) / bid0 * 100
        bid_depth = sum(float(b[1]) * float(b[0]) for b in bids[:20])
        ask_depth = sum(float(a[1]) * float(a[0]) for a in asks[:20])
        imbalance = bid_depth / ask_depth if ask_depth else 0
        total_m = (bid_depth + ask_depth) / 1e6
        print(f"  bid0={bid0:.2f} ask0={ask0:.2f} spread={spread:.4f}%")
        print(f"  bid_depth={bid_depth:.0f} ask_depth={ask_depth:.0f} imbalance={imbalance:.2f}")
        print(f"  z_depth={total_m:.2f}M")
elif isinstance(ob, list):
    print(f"  WARNING: orderbook 返回列表而非字典, 长度={len(ob)}")
    if ob: print(f"  首个元素: {json.dumps(ob[0], ensure_ascii=False)[:200]}")

# 2. candles 1h
c1h = okx_raw(["market", "candles", "BTC-USDT-SWAP", "--bar", "1H", "--limit", "4"])
if isinstance(c1h, list) and len(c1h) >= 2:
    cur = float(c1h[0][4])
    prev = float(c1h[1][4])
    chg_1h = (cur - prev) / prev * 100
    cur_vol = float(c1h[0][5])
    prev_vol = float(c1h[1][5])
    vol_trend = "up" if cur_vol/prev_vol>1.3 else "down" if cur_vol/prev_vol<0.7 else "stable"
    print(f"  chg_1h={chg_1h:.3f}% vol_trend={vol_trend}")
    if len(c1h) >= 4:
        p3 = float(c1h[3][4])
        chg_3h = (cur - p3) / p3 * 100
        print(f"  chg_3h={chg_3h:.3f}%")
elif isinstance(c1h, dict):
    print(f"  WARNING: candles 返回字典: {json.dumps(c1h, ensure_ascii=False)[:200]}")

# 3. candles 15m
c15 = okx_raw(["market", "candles", "BTC-USDT-SWAP", "--bar", "15m", "--limit", "2"])
if isinstance(c15, list) and len(c15) >= 2:
    v0, v1 = float(c15[0][5]), float(c15[1][5])
    vol_ratio = v0 / v1 if v1 else 1
    print(f"  vol_15m_ratio={vol_ratio:.2f}")

# 4. 最终结果
print(f"\n{'='*60}")
print("完整 get_risk_metrics() 输出:")
print("=" * 60)
sys.path.insert(0, ".")
from dashboard import get_risk_metrics
result = get_risk_metrics()
print(json.dumps(result, ensure_ascii=False, indent=2))
