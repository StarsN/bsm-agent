#!/usr/bin/env python3
"""验证新增字段数据正确性 — 分模块测试，逐字段对比"""
import sys, json, os, time

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)
os.chdir(PROJECT)

print("=" * 60)
print("P0-P3 新增字段验证")
print("=" * 60)

# ── 1. market.py: vol_oi_ratio + listing_age_days + sector/chain ──
print("\n── 1. market.py 新增字段 ──")
try:
    from market import get_market_snapshot, get_listing_age_days, get_token_tags, get_futures_symbols
    syms = get_futures_symbols()
    print(f"  get_futures_symbols(): {len(syms)} symbols")
    print(f"  BTC in symbols: {'BTC' in syms}")
    print(f"  ETH in symbols: {'ETH' in syms}")

    # listing_age
    btc_age = get_listing_age_days("BTC")
    print(f"  get_listing_age_days('BTC'): {btc_age}")

    # tags
    btc_tags = get_token_tags("BTC")
    wif_tags = get_token_tags("WIF")
    unknown = get_token_tags("ZZZNOTHING")
    print(f"  get_token_tags('BTC'): {btc_tags}")
    print(f"  get_token_tags('WIF'): {wif_tags}")
    print(f"  get_token_tags('ZZZNOTHING'): {unknown}")

    # full snapshot
    print("  拉 BTC 快照 (heavy=False)...")
    snap = get_market_snapshot("BTC", heavy=False)
    if snap:
        for k in ["vol_oi_ratio", "listing_age_days", "sector", "chain"]:
            v = snap.get(k)
            print(f"    snap['{k}'] = {v}")
        # 确认 vol_oi_ratio 计算正确
        vol = snap.get("volume_24h_usd")
        oi = snap.get("oi_usd")
        expected = vol / oi if vol and oi else None
        print(f"    vol_oi_ratio 验证: volume={vol} / oi={oi} → snap={snap.get('vol_oi_ratio')} expected={expected}")
    else:
        print("  [FAIL] BTC snapshot 返回 None")
except Exception as e:
    print(f"  [FAIL] {e}")
    import traceback; traceback.print_exc()

# ── 2. dashboard.py: fear_greed ──
print("\n── 2. dashboard.py 新增字段 (risk-metrics) ──")
try:
    from dashboard import get_risk_metrics, _get_fear_greed
    fng = _get_fear_greed()
    print(f"  _get_fear_greed() raw: {fng}")

    rm = get_risk_metrics()
    if rm:
        fng_metric = rm.get("metrics", {}).get("fear_greed")
        print(f"  risk_metrics.fear_greed: {fng_metric}")
        # 检查所有 metric key
        print(f"  metrics keys: {list(rm['metrics'].keys())}")
        print(f"  metrics count: {len(rm['metrics'])}")
    else:
        print("  [FAIL] get_risk_metrics() 返回 None")
except Exception as e:
    print(f"  [FAIL] {e}")
    import traceback; traceback.print_exc()

# ── 3. dashboard.py: multi-token OKX ──
print("\n── 3. dashboard.py OKX 三件套 ──")
try:
    from dashboard import get_multi_token_okx_metrics, _okx

    # 先验证 OKX CLI 基础连通性
    print("  测试 OKX tickers SWAP...")
    raw = _okx(["market", "tickers", "SWAP"])
    if raw and isinstance(raw, list):
        print(f"    OKX tickers SWAP: {len(raw)} 个合约")
        btc_item = next((t for t in raw if t.get("instId") == "BTC-USDT-SWAP"), None)
        if btc_item:
            print(f"    BTC-USDT-SWAP keys: {list(btc_item.keys())[:6]}")
            print(f"    BTC 合约价: {btc_item.get('last')}")
    else:
        print(f"    [WARN] OKX tickers SWAP 返回异常: {type(raw)}")

    # 测试 OKX 现货 tickers
    print("  测试 OKX tickers USDT...")
    raw_s = _okx(["market", "tickers", "USDT"])
    if raw_s and isinstance(raw_s, list):
        btc_spot = next((t for t in raw_s if t.get("instId") == "BTC-USDT"), None)
        if btc_spot:
            print(f"    BTC-USDT 现货价: {btc_spot.get('last')}")
    else:
        print(f"    [WARN] OKX tickers USDT 返回异常: {type(raw_s)}")

    # 测试多 token 批量
    print("  测试 get_multi_token_okx_metrics(['BTC','ETH'], heavy=True)...")
    okx = get_multi_token_okx_metrics(["BTC", "ETH"], heavy=True)
    print(f"    keys: {list(okx.keys())}")
    for t in ["BTC", "ETH"]:
        td = okx.get(t, {})
        print(f"    {t}: {json.dumps(td, default=str, ensure_ascii=False)}")
        # 验证关键字段
        for k in ["basis_pct", "okx_funding_pct", "funding_history"]:
            v = td.get(k)
            if k == "funding_history":
                print(f"      {k}: {'有' if v else '无'} ({len(v) if v else 0} 条)")
            else:
                print(f"      {k}: {v}")
except Exception as e:
    print(f"  [FAIL] {e}")
    import traceback; traceback.print_exc()

# ── 4. liquidation.py 模块加载 ──
print("\n── 4. liquidation.py ──")
try:
    import liquidation
    print(f"  liquidation module: OK")
    print(f"  _running: {liquidation._running}")
    # 不实际启动 WS（可能无网络），只验证 get_and_reset
    data = liquidation.get_and_reset()
    print(f"  get_and_reset() (no data): {data}")
except Exception as e:
    print(f"  [FAIL] {e}")
    import traceback; traceback.print_exc()

# ── 5. 字段名一致性检查 ──
print("\n── 5. 字段名一致性 ──")

# 模拟 worker.py 的 merge 流程
snap_fields = {
    "market.py": ["vol_oi_ratio", "listing_age_days", "sector", "chain",
                   "mark_price", "funding_rate_pct", "oi_usd", "volume_24h_usd",
                   "bid_ask_spread_pct", "depth_bid_1pct_usd", "depth_ask_1pct_usd",
                   "depth_imbalance_pct", "klines_15m_count"],
}
okx_fields = {"dashboard.py": ["basis_pct", "okx_funding_pct", "funding_history"]}
liq_fields = {"liquidation.py": ["liq_long_usd", "liq_long_count", "liq_short_usd", "liq_short_count"]}
fng_fields = {"dashboard.py risk_metrics": ["fear_greed"]}

# 检查是否有命名冲突
all_fields = {}
for src, fields in {**snap_fields, **okx_fields, **liq_fields}.items():
    for f in fields:
        if f in all_fields:
            print(f"  [CONFLICT] '{f}' 在 {src} 和 {all_fields[f]} 重名!")
        else:
            all_fields[f] = src
print(f"  无命名冲突，共 {len(all_fields)} 个字段")
for k, v in sorted(all_fields.items()):
    print(f"    {k:30s} ← {v}")

print("\n" + "=" * 60)
print("验证完成")
print("=" * 60)
