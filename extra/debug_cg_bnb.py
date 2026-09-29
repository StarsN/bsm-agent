#!/usr/bin/env python3
"""独立脚本：查询 BNB 的 CoinGecko 完整数据，分析提取逻辑是否有遗漏。不依赖项目代码。"""
import json
from urllib.request import Request, urlopen
from urllib.parse import quote

HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch(url, label=""):
    print(f"[{label}] GET {url[:90]}...")
    req = Request(url, headers=HEADERS)
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


# Step 1: 搜索 BNB
search_data = fetch(
    f"https://api.coingecko.com/api/v3/search?query={quote('BNB')}", "search"
)

coins = search_data.get("coins", [])
print(f"\n搜索返回 {len(coins)} 个结果:")
for i, c in enumerate(coins[:10]):
    print(f"  [{i}] id={c['id']:30s} symbol={c.get('symbol','?')}  name={c.get('name','?')}")

# 精确匹配 symbol
token = "BNB"
exact = [c for c in coins if c.get("symbol", "").upper() == token.upper()]
if exact:
    coin_id = exact[0]["id"]
    print(f"\n精确匹配 BNB -> coin_id = {coin_id}")
else:
    # 无精确匹配时兜底取第一个（当前代码已删除此逻辑，这里仅展示）
    coin_id = coins[0]["id"]
    print(f"\n无精确匹配，兜底第一个 -> coin_id = {coin_id}")

# Step 2: 获取代币详情
meta_data = fetch(
    f"https://api.coingecko.com/api/v3/coins/{coin_id}"
    "?localization=false&tickers=false&community_data=false"
    "&developer_data=false&sparkline=false",
    "coin",
)

# ========== 分析 ==========
print("\n" + "=" * 60)
print("1. detail_platforms (所有链的合约地址)")
print("=" * 60)
platforms = meta_data.get("detail_platforms", {})
print(f"共 {len(platforms)} 条:")
for chain_name, info in platforms.items():
    addr = (
        info.get("contract_address", "-")
        if isinstance(info, dict)
        else str(info)
    )
    print(f"  {chain_name:35s} -> {addr}")

print("\n" + "=" * 60)
print("2. 市值数据")
print("=" * 60)
market_data = meta_data.get("market_data", {})
mcap = market_data.get("market_cap", {}).get("usd")
fdv = market_data.get("fully_diluted_valuation", {}).get("usd")
print(f"  market_cap_usd = {mcap}")
print(f"  fdv_usd        = {fdv}")

print("\n" + "=" * 60)
print("3. asset_platform_id (原生链标识)")
print("=" * 60)
print(f"  asset_platform_id = {meta_data.get('asset_platform_id')}")

print("\n" + "=" * 60)
print("4. platforms (旧字段，与 detail_platforms 对比)")
print("=" * 60)
old_platforms = meta_data.get("platforms", {})
for k, v in sorted((old_platforms or {}).items()):
    print(f"  {k:35s} -> {v}")

print("\n" + "=" * 60)
print("5. 模拟当前代码的选取逻辑")
print("=" * 60)

# === 当前代码逻辑 (market.py line 650-665) ===
_GMGN_CHAINS = ["solana", "binance-smart-chain", "base", "ethereum"]

best = None
fallback = None
for chain_name, info in platforms.items():
    if isinstance(info, dict) and info.get("contract_address"):
        if fallback is None:
            fallback = (chain_name, info["contract_address"])
        if chain_name in _GMGN_CHAINS:
            if best is None or _GMGN_CHAINS.index(chain_name) < _GMGN_CHAINS.index(
                best[0]
            ):
                best = (chain_name, info["contract_address"])

chosen = best or fallback
print(f"  best     = {best}")
print(f"  fallback = {fallback}")
print(f"  chosen   = {chosen}")

if chosen:
    chain, addr = chosen
    print(f"  -> chain={chain}, contract_address={addr}")
    _CG_TO_GMGN = {
        "solana": "sol",
        "ethereum": "eth",
        "binance-smart-chain": "bsc",
        "base": "base",
    }
    _VALID = {"sol", "bsc", "base", "eth", "monad"}
    mapped = _CG_TO_GMGN.get(chain, chain)
    supported = mapped in _VALID
    print(f"  -> GMGN mapped: {mapped} ({'supported' if supported else 'NOT supported'})")

print("\n" + "=" * 60)
print("6. 潜在问题检查清单")
print("=" * 60)

issues = []

# 检查1: BNB 原生链是 BSC
if best and best[0] == "binance-smart-chain":
    print("  [OK] BNB 正确选取了 BSC 链")
elif best:
    msg = f"选的是 {best[0]}，BNB 原生链是 BSC"
    print(f"  [WARN] {msg}")
    issues.append(msg)
else:
    print(f"  [WARN] best=None, fallback={fallback}")

# 检查2: 旧字段 vs 新字段
detail_keys = set(platforms.keys())
old_keys = set(k for k, v in old_platforms.items() if v)
missing_in_detail = old_keys - detail_keys
if missing_in_detail:
    msg = f"旧 platforms 有，detail_platforms 缺失: {missing_in_detail}"
    print(f"  [WARN] {msg}")
    issues.append(msg)
extra_in_detail = detail_keys - old_keys
if extra_in_detail:
    msg = f"detail_platforms 比旧 platforms 多: {extra_in_detail}"
    print(f"  [INFO] {msg}")

# 检查3: 跨链桥合约地址
for chain_name, info in platforms.items():
    if isinstance(info, dict) and info.get("contract_address"):
        addr = info["contract_address"]
        if chain_name not in _GMGN_CHAINS:
            # 不在优先级池中，但可能被 fallback 选中
            pass

# 检查4: BNB 在各链上的存在情况
print(f"\n  BNB 所在的链: {list(platforms.keys())}")
bsc = platforms.get("binance-smart-chain", {})
eth = platforms.get("ethereum", {})
print(f"  BSC 合约地址: {bsc.get('contract_address') if isinstance(bsc, dict) else bsc}")
print(f"  ETH 合约地址: {eth.get('contract_address') if isinstance(eth, dict) else eth}")

# 检查5: 代码只读 detail_platforms，忽略旧 platforms
if not platforms and old_platforms:
    msg = "detail_platforms 为空但 platforms 有数据 -- 代码取不到任何链信息！"
    print(f"  [CRITICAL] {msg}")
    issues.append(msg)

# 检查6: 原生币没有 contract_address 的情况
# 比如 BNB 在 BSC 上作为原生币，contract_address 可能为空
if isinstance(bsc, dict):
    bsc_addr = bsc.get("contract_address")
    if not bsc_addr:
        msg = "BSC 上 BNB 的 contract_address 为空 -- 原生币无合约地址！"
        print(f"  [WARN] {msg}")
        issues.append(msg)

print("\n" + "=" * 60)
print(f"结论: 发现 {len(issues)} 个问题" if issues else "结论: 未发现问题")
if issues:
    for i, msg in enumerate(issues, 1):
        print(f"  {i}. {msg}")

# 额外: dump 完整的 platforms JSON 供手动检查
print("\n" + "=" * 60)
print("附: detail_platforms 完整 JSON")
print("=" * 60)
print(json.dumps(platforms, indent=2, ensure_ascii=False))
