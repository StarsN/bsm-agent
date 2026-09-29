#!/usr/bin/env python3
"""全字段验证 — 独立脚本，不依赖项目代码"""
import json, subprocess, time, asyncio, sys
from urllib.request import Request, urlopen
from datetime import datetime, timezone, timedelta

OK, FAIL, WARN = "  ✓", "  ✗", "  △"
passed = 0; failed = 0; warnings = 0

def check(name, ok, detail=""):
    global passed, failed, warnings
    if ok is True:
        print(f"{OK} {name} {detail}")
        passed += 1
    elif ok is False:
        print(f"{FAIL} {name} {detail}")
        failed += 1
    else:
        print(f"{WARN} {name} {detail}")
        warnings += 1

def okx(args):
    cmd = ["okx"] + args + ["--json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if r.returncode != 0: return None
        out = r.stdout.strip()
        if not out: return None
        for i, ch in enumerate(out):
            if ch in ('{', '['):
                try: return json.loads(out[i:])
                except json.JSONDecodeError: break
        return None
    except Exception: return None

print("=" * 55)
print("  P0-P3 全字段验证")
print("=" * 55)

# ════════════════════════════════════════════════════════════
print("\n── P0 #3  vol/OI ──")
try:
    # 直接拉 BTC 的 volume + OI 算 vol_oi_ratio
    from urllib.request import Request, urlopen
    req = Request("https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT",
                  headers={"User-Agent": "Mozilla/5.0"})
    prem = json.loads(urlopen(req, timeout=8).read())
    mark = float(prem.get("markPrice", 0))

    req2 = Request("https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT",
                   headers={"User-Agent": "Mozilla/5.0"})
    oi_raw = json.loads(urlopen(req2, timeout=8).read())
    oi_usd = float(oi_raw.get("openInterest", 0)) * mark

    req3 = Request("https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=15m&limit=97",
                   headers={"User-Agent": "Mozilla/5.0"})
    kl = json.loads(urlopen(req3, timeout=8).read())
    vol_24h = sum(float(k[7]) for k in kl[-96:]) if len(kl) >= 97 else 0

    vol_oi = vol_24h / oi_usd if oi_usd else None
    check("BTC vol_oi_ratio", vol_oi is not None and vol_oi > 0,
          f"vol={vol_24h:.0f} oi={oi_usd:.0f} → {vol_oi:.2f}" if vol_oi else "N/A")
except Exception as e:
    check("vol_oi_ratio", False, str(e))

# ════════════════════════════════════════════════════════════
print("\n── P0 #16 恐惧贪婪 ──")
try:
    req = Request("https://api.alternative.me/fng/?limit=2",
                  headers={"User-Agent": "Mozilla/5.0"})
    fng = json.loads(urlopen(req, timeout=8).read())
    latest = fng["data"][0]
    value = int(latest.get("value", 0))
    classification = latest.get("value_classification", "")
    check("F&G API", value > 0, f"value={value} ({classification})")
except Exception as e:
    check("F&G API", False, str(e))

# ════════════════════════════════════════════════════════════
print("\n── P1 #22 上线时间 (onboardDate) ──")
try:
    req = Request("https://fapi.binance.com/fapi/v1/exchangeInfo",
                  headers={"User-Agent": "Mozilla/5.0"})
    ex = json.loads(urlopen(req, timeout=10).read())
    sample = {}
    for s in ex.get("symbols", []):
        if s.get("contractType") == "PERPETUAL" and s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING":
            t = s.get("baseAsset", "").upper()
            od = s.get("onboardDate")
            if od and t in ("BTC", "ETH", "SOL", "PEPE", "SUI"):
                age = (time.time() * 1000 - int(od)) / (86400 * 1000)
                sample[t] = f"{age:.0f}d"
    check("onboardDate", len(sample) >= 3, str(sample))
except Exception as e:
    check("onboardDate", False, str(e))

# ════════════════════════════════════════════════════════════
print("\n── P1 #6 Basis 基差 ──")
swap = okx(["market", "tickers", "SWAP"])
spot = okx(["market", "tickers", "SPOT"])
if swap and isinstance(swap, list) and spot and isinstance(spot, list):
    swap_map = {}
    for t in swap:
        i = t.get("instId", "")
        if i.endswith("-USDT-SWAP"):
            swap_map[i.replace("-USDT-SWAP", "")] = float(t.get("last", 0) or 0)
    spot_map = {}
    for t in spot:
        i = t.get("instId", "")
        if i.endswith("-USDT"):
            spot_map[i.replace("-USDT", "")] = float(t.get("last", 0) or 0)
    results = {}
    for tk in ("BTC", "ETH", "SOL"):
        s, p = swap_map.get(tk), spot_map.get(tk)
        if s and p and p > 0:
            results[tk] = f"{(s-p)/p*100:.4f}% (swap={s:.0f} spot={p:.0f})"
    check("Basis", len(results) >= 2, str(results))
else:
    check("Basis", False, f"swap={type(swap).__name__} spot={type(spot).__name__}")

# ════════════════════════════════════════════════════════════
print("\n── P1 #4 OKX 费率对比 ──")
fr_raw = okx(["market", "funding-rate", "BTC-USDT-SWAP"])
if fr_raw and isinstance(fr_raw, list) and fr_raw:
    fr_data = fr_raw[0]
    okx_fr = float(fr_data.get("fundingRate", 0)) * 100
    check("funding-rate BTC", okx_fr > -10,
          f"{okx_fr:.4f}%  (list unwrap: {len(fr_raw)} items)")

    fr_raw2 = okx(["market", "funding-rate", "ETH-USDT-SWAP"])
    okx_fr2 = float(fr_raw2[0].get("fundingRate", 0)) * 100 if fr_raw2 and isinstance(fr_raw2, list) and fr_raw2 else None
    check("funding-rate ETH", okx_fr2 is not None, f"{okx_fr2:.4f}%")
else:
    check("funding-rate", False, type(fr_raw).__name__)

# ════════════════════════════════════════════════════════════
print("\n── P1 #5 费率历史 ──")
hist = okx(["market", "funding-rate", "BTC-USDT-SWAP", "--history", "--limit", "10"])
if hist and isinstance(hist, list) and len(hist) >= 3:
    rates = [float(h.get("fundingRate", 0)) * 100 for h in hist[:3]]
    check("funding-rate history BTC", True,
          f"{len(hist)}条 → [{', '.join(f'{r:.4f}%' for r in rates)}]")
else:
    check("funding-rate history", False, f"got {type(hist).__name__} {len(hist) if isinstance(hist,list) else ''}")

hist2 = okx(["market", "funding-rate", "ETH-USDT-SWAP", "--history", "--limit", "5"])
check("funding-rate history ETH", isinstance(hist2, list) and len(hist2) >= 3,
      f"{len(hist2) if isinstance(hist2,list) else 0}条")

# ════════════════════════════════════════════════════════════
print("\n── P3 #12 爆仓 WS (fstream) ──")
try:
    import websockets
except ImportError:
    websockets = None
    check("websockets库", False, "未安装")

if websockets:
    collected = {}
    async def run_ws():
        urls = [
            "wss://fstream.binance.com/ws/!forceOrder@arr",
            "wss://fstream.binance.com/stream?streams=!forceOrder@arr",
        ]
        for url in urls:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20,
                                              close_timeout=5, max_size=2**20) as ws:
                    print(f"    已连接: {url.split('//')[1].split('/')[0]}")
                    deadline = time.time() + 15
                    while time.time() < deadline:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=5)
                            data = json.loads(msg)
                            # fstream 的 multistream 格式: {"stream":"...","data":{...}}
                            if "data" in data:
                                data = data["data"]
                            o = data.get("o", {})
                            symbol = o.get("s", "")
                            if symbol.endswith("USDT"):
                                tk = symbol.replace("USDT", "")
                                side = "S" if o.get("S") == "SELL" else "L"
                                usd = float(o.get("p", 0)) * float(o.get("q", 0))
                                e = collected.setdefault(tk, {"L": 0, "S": 0, "$": 0})
                                e[side] += 1; e["$"] += usd
                        except asyncio.TimeoutError: pass
                    print(f"    成功: {url.split('//')[1].split('/')[0]}")
                    return
            except Exception as e2:
                print(f"    失败: {url.split('//')[1].split('/')[0]} → {e2}")
    try:
        asyncio.run(run_ws())
    except Exception: pass

    total = sum(c["L"]+c["S"] for c in collected.values())
    check("爆仓 WS", total > 0,
          f"{len(collected)}币 {total}笔 " +
          ",".join(f"{t}:{c['L']+c['S']}" for t,c in sorted(collected.items(), key=lambda x:-x[1]['$'])[:5]))
else:
    check("爆仓 WS", "skip", "websockets未安装，跳过")

# ════════════════════════════════════════════════════════════
print(f"\n{'='*55}")
print(f"  结果: {passed}✓  {failed}✗  {warnings}△")
print(f"{'='*55}")
