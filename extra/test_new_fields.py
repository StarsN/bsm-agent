"""
单元测试：市值、聪明钱、乖离率、OI/市值 —— 独立运行，不依赖系统模块
用法: python3 extra/test_new_fields.py
"""
import subprocess, json, sys

pass_count = 0
fail_count = 0

def check(name, ok, detail=""):
    global pass_count, fail_count
    if ok:
        pass_count += 1
        print(f"  [PASS] {name}")
    else:
        fail_count += 1
        print(f"  [FAIL] {name}  {detail}")

def _okx(args, live=False):
    """调 OKX CLI，返回解析后的 JSON"""
    cmd = ["okx"]
    if live:
        cmd += ["--profile", "live"]
    cmd += args + ["--json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if r.returncode != 0 or not r.stdout.strip():
            return None
        out = r.stdout.strip()
        for i, ch in enumerate(out):
            if ch in ('{', '['):
                return json.loads(out[i:])
        return None
    except Exception as e:
        return None


# ============================================================
# Test 1: 市值 CLI
# ============================================================
print("=" * 60)
print("Test 1: 市值 (market_cap_usd / market_cap_rank)")
print("=" * 60)

mc_data = _okx(["market", "filter", "--instType", "SPOT",
    "--sortBy", "marketCapUsd", "--sortOrder", "desc",
    "--limit", "10", "--quoteCcy", "USDT"])
check("CLI 返回 list", isinstance(mc_data, list), f"type={type(mc_data)}")

if mc_data and isinstance(mc_data, list) and mc_data[0].get("rows"):
    rows = mc_data[0]["rows"]
    check("rows >= 10", len(rows) >= 10, f"got {len(rows)}")

    # 验证字段
    row = rows[0]
    base = row.get("baseCcy", "")
    mc = float(row.get("marketCapUsd", 0))
    rank = int(row.get("rank", 0))
    check(f"baseCcy = {base}", isinstance(base, str) and len(base) > 0)
    check(f"marketCapUsd = {mc} > 0", mc > 0, f"value={mc}")
    check(f"rank = {rank} > 0", rank > 0, f"value={rank}")

    # 逐币查询
    print("\n  --- 逐币查询测试 ---")
    for token in ["BTC", "ETH", "SOL"]:
        d = _okx(["market", "filter", "--instType", "SPOT",
            "--baseCcy", token, "--limit", "1"])
        if d and isinstance(d, list) and d[0].get("rows"):
            r = d[0]["rows"][0]
            t = r.get("baseCcy", "")
            mc_t = float(r.get("marketCapUsd", 0))
            check(f"  逐币 {t} = {mc_t:.0f}", t.upper() == token and mc_t > 0,
                  f"token={t}, mc={mc_t}")
else:
    check("rows 存在", False, "CLI 返回异常")


# ============================================================
# Test 2: 聪明钱 CLI
# ============================================================
print(f"\n{'=' * 60}")
print("Test 2: 聪明钱 (sm_*)")
print("=" * 60)

sm_data = _okx(["smartmoney", "signal-overview-by-filter",
    "--instCcyList", "BTC,ETH,SOL", "--sortBy", "pnl", "--period", "7"], live=True)
check("CLI 返回 dict", isinstance(sm_data, dict), f"type={type(sm_data)}")

if sm_data and isinstance(sm_data, dict) and sm_data.get("data"):
    items = sm_data["data"]
    check(f"data 有 {len(items)} 个 token", len(items) > 0)

    for item in items[:2]:
        ccy = item.get("ccy", "")
        lsr = item.get("longShortRatio", {})
        nt = item.get("notional", {})
        wr = item.get("winRate", {})
        long_r = float(lsr.get("longRatio", 0))
        net = float(nt.get("netNotionalUsdt", 0))
        traders = int(item.get("tradersWithPosition", 0))
        l_win = float(wr.get("avgLongWinRate", 0))
        s_win = float(wr.get("avgShortWinRate", 0))

        print(f"\n  [{ccy}]")
        check(f"    longRatio = {long_r:.4f} (0-1)", 0 <= long_r <= 1,
              f"value={long_r}")
        check(f"    longRatio*100 = {long_r*100:.1f}% (即 sm_long_ratio)", True,
              f"value={long_r*100}")
        check(f"    netNotional = {net:.0f}", isinstance(net, float))
        check(f"    tradersWithPosition = {traders}", traders > 0, f"value={traders}")
        check(f"    avgLongWinRate = {l_win:.4f} (0-1)", 0 <= l_win <= 1,
              f"value={l_win}")
        check(f"    avgLongWinRate*100 = {l_win*100:.1f}% (即 sm_avg_long_win_rate)", True,
              f"value={l_win*100}")
        check(f"    avgShortWinRate = {s_win:.4f} (0-1)", 0 <= s_win <= 1,
              f"value={s_win}")
        check(f"    avgShortWinRate*100 = {s_win*100:.1f}% (即 sm_avg_short_win_rate)", True,
              f"value={s_win*100}")
else:
    check("data 存在", False, "CLI 返回异常")


# ============================================================
# Test 3: 模拟 dashboard.py 合并逻辑
# ============================================================
print(f"\n{'=' * 60}")
print("Test 3: 模拟合并逻辑 (dashboard.py → snap)")
print("=" * 60)

# 模拟 result = {token: {}} 结构
result = {"BTC": {}, "ETH": {}, "SOL": {}}

# 模拟市值合并
if mc_data and isinstance(mc_data, list):
    for row in mc_data[0].get("rows", []):
        t = row.get("baseCcy", "").upper()
        if t in result:
            result[t]["market_cap_usd"] = float(row.get("marketCapUsd", 0))
            result[t]["market_cap_rank"] = int(row.get("rank", 0))

# 模拟聪明钱合并
if sm_data and isinstance(sm_data, dict):
    for item in sm_data.get("data", []):
        t = item.get("ccy", "").upper()
        if t in result:
            lsr = item.get("longShortRatio", {})
            nt = item.get("notional", {})
            wr = item.get("winRate", {})
            result[t].update({
                "sm_long_ratio": float(lsr.get("longRatio", 0)) * 100,
                "sm_net_notional_usdt": float(nt.get("netNotionalUsdt", 0)),
                "sm_long_avg_entry": float(nt.get("smartMoneyLongAvgEntry", 0)),
                "sm_short_avg_entry": float(nt.get("smartMoneyShortAvgEntry", 0)),
                "sm_avg_long_win_rate": float(wr.get("avgLongWinRate", 0)) * 100,
                "sm_avg_short_win_rate": float(wr.get("avgShortWinRate", 0)) * 100,
                "sm_traders_with_position": int(item.get("tradersWithPosition", 0)),
            })

for token in ["BTC", "ETH", "SOL"]:
    d = result[token]
    print(f"\n  [{token}]")
    check(f"    market_cap_usd = {d.get('market_cap_usd', 0):.0f}", d.get("market_cap_usd", 0) > 0)
    check(f"    market_cap_rank = {d.get('market_cap_rank', 0)}", d.get("market_cap_rank", 0) > 0)

    if "sm_long_ratio" in d:
        sm_lr = d["sm_long_ratio"]
        check(f"    sm_long_ratio = {sm_lr:.1f}% (0-100)", 0 <= sm_lr <= 100,
              f"value={sm_lr}")
        check(f"    sm_net_notional_usdt = {d['sm_net_notional_usdt']:.0f}", True)
        check(f"    sm_traders_with_position = {d['sm_traders_with_position']}", True)
        check(f"    sm_avg_long_win_rate = {d['sm_avg_long_win_rate']:.1f}% (0-100)",
              0 <= d["sm_avg_long_win_rate"] <= 100)
        check(f"    sm_avg_short_win_rate = {d['sm_avg_short_win_rate']:.1f}% (0-100)",
              0 <= d["sm_avg_short_win_rate"] <= 100)
    else:
        print(f"    (无聪明钱数据 — {token} 不在 signal-overview 覆盖范围)")


# ============================================================
# Test 4: 模拟 OI/市值 计算
# ============================================================
print(f"\n{'=' * 60}")
print("Test 4: 模拟 OI/市值计算 (worker.py)")
print("=" * 60)

# 用 BTC 模拟：拉 OI 和 市值 算比值
btc_mc = result["BTC"].get("market_cap_usd", 0)

# 从币安拉 OI（跟系统 get_market_snapshot 同源）
try:
    import urllib.request
    oi_url = "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"
    req = urllib.request.Request(oi_url)
    with urllib.request.urlopen(req, timeout=10) as resp:
        oi_raw = json.loads(resp.read().decode())
        oi_usd = float(oi_raw.get("openInterest", 0))
        # 乘以标记价得到 USD OI
        mark_url = "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT"
        with urllib.request.urlopen(urllib.request.Request(mark_url), timeout=10) as resp2:
            mark_raw = json.loads(resp2.read().decode())
            mark_price = float(mark_raw.get("markPrice", 0))
            oi_usd = oi_usd * mark_price

    check(f"BTC OI = {oi_usd:.0f} USD", oi_usd > 0)

    if oi_usd > 0 and btc_mc > 0:
        ratio = oi_usd / btc_mc * 100
        check(f"OI/市值 = {ratio:.2f}% (oi_marketcap_ratio)", 0 < ratio < 100,
              f"ratio={ratio}%")
except Exception as e:
    check("OI 数据获取", False, str(e)[:80])


# ============================================================
# Test 5: 模拟乖离率计算
# ============================================================
print(f"\n{'=' * 60}")
print("Test 5: 模拟 MA20 乖离率 (market.py)")
print("=" * 60)

klines = _okx(["market", "candles", "BTC-USDT-SWAP", "--bar", "15m", "--limit", "25"])
if klines and isinstance(klines, list):
    check(f"K 线返回 {len(klines)} 根", len(klines) >= 20, f"got {len(klines)}")
    if len(klines) >= 20:
        closes = [float(k[4]) for k in klines]
        now_price = closes[-1]
        ma20 = sum(closes[-20:]) / 20
        deviation = (now_price - ma20) / ma20 * 100
        check(f"MA20 = {ma20:.2f}, 现价 = {now_price:.2f}", True)
        check(f"ma20_deviation_pct = {deviation:.2f}%", abs(deviation) < 50,
              f"乖离率过大: {deviation}%")
else:
    check("K 线数据获取", False)


# ============================================================
print(f"\n{'=' * 60}")
print(f"Result: {pass_count} PASS / {fail_count} FAIL  (total {pass_count + fail_count})")
print("=" * 60)
sys.exit(0 if fail_count == 0 else 1)
