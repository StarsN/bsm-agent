"""
OKX 数据源：成交量 Top50 + 社交情绪 → 热度榜
替代币安广场抓取，输出格式与 compute_short_scores 一致
"""
from __future__ import annotations
import os
import json
import platform
import subprocess
import config

# CLI 路径（Windows 需要完整路径，Linux 直接用命令名）
if platform.system() == "Windows":
    _NPM = os.environ.get("APPDATA", "") + r"\npm"
    _OKX_CMD = os.path.join(_NPM, "okx.cmd")
    _ONCHAINOS_CMD = os.path.join(_NPM, "onchainos.exe")
else:
    _OKX_CMD = "okx"
    _ONCHAINOS_CMD = "onchainos"

# 非加密资产黑名单（贵金属 / 股票代币）
NON_CRYPTO_BASE = {
    "XAU", "XAG",
    "SNDK", "MU", "MRVL", "SPCX", "INTC", "CBRS", "CRCL", "ORCL",
    "AMD", "SOXL", "BZ",
}


def _run_okx(args: list[str], timeout: int = 30) -> dict | None:
    """调用 OKX/OnchainOS CLI，返回解析后的 JSON"""
    try:
        env = os.environ.copy()
        if platform.system() == "Windows":
            # 确保 npm 全局目录和 node 在 PATH 中
            node_dirs = []
            for p in env.get("PATH", "").split(os.pathsep):
                if "node" in p.lower():
                    node_dirs.append(p)
            extra = [_NPM] + node_dirs
            env["PATH"] = os.pathsep.join(extra) + os.pathsep + env.get("PATH", "")
        proc = subprocess.run(
            args, timeout=timeout, capture_output=True, text=True, env=env,
        )
        if proc.returncode != 0:
            err = (proc.stderr or "").strip()
            # 过滤 npm update 提示（不是真正的错误）
            if err:
                real_errs = [l for l in err.split("\n")
                           if "Update available" not in l and "npm install" not in l]
                if real_errs:
                    print(f"  [OKX] CLI error: {' | '.join(real_errs)[:150]}")
            if not proc.stdout or not proc.stdout.strip():
                return None
        # 跳过 npm update 提示行，找到 JSON 起始位置
        stdout = proc.stdout or ""
        lines = stdout.split("\n")
        start = 0
        for i, line in enumerate(lines):
            s = line.strip()
            if s.startswith("[") or s.startswith("{"):
                start = i
                break
        return json.loads("\n".join(lines[start:]))
    except json.JSONDecodeError as e:
        print(f"  [OKX] JSON parse: {e}")
        return None
    except subprocess.TimeoutExpired:
        print(f"  [OKX] timeout ({timeout}s): {' '.join(args[:4])}")
        return None
    except Exception as e:
        print(f"  [OKX] error: {e}")
        return None


def fetch_top_swap_tokens(limit: int = 50) -> list[dict]:
    """获取 USDT 永续合约成交量 Top N（已过滤非加密资产）"""
    data = _run_okx([
        _OKX_CMD, "market", "filter",
        "--instType", "SWAP",
        "--sortBy", "volUsd24h",
        "--sortOrder", "desc",
        "--limit", str(limit),
        "--quoteCcy", "USDT",
        "--json",
    ], timeout=30)
    if not data:
        return []

    # 兼容多种 JSON 结构: [{rows: [...]}] / {rows: [...]} / [...]
    rows = data
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        rows = data[0].get("rows", data[0].get("data", []))
    elif isinstance(data, dict):
        rows = data.get("rows", data.get("data", []))
    if not isinstance(rows, list):
        return []

    result = []
    for r in rows:
        base = (r.get("baseCcy") or "").upper()
        if base in NON_CRYPTO_BASE:
            continue
        result.append({
            "token": base,
            "vol_rank": int(r.get("rank") or 0),
            "last": r.get("last"),
            "chg24h_pct": r.get("chg24hPct"),
            "funding_rate": r.get("fundingRate"),
            "oi_usd": r.get("oiUsd"),
            "vol_usd_24h": r.get("volUsd24h"),
        })
    return result


def fetch_sentiment_batch(tokens: list[str]) -> dict[str, dict]:
    """批量获取社交情绪，返回 {TOKEN: {mention_count, ...}}，最多 20 个/批"""
    result = {}
    for i in range(0, len(tokens), 20):
        batch = tokens[i:i + 20]
        data = _run_okx([
            _ONCHAINOS_CMD, "social", "sentiment-symbol",
            "--token-symbols", ",".join(batch),
            "--time-frame", "3",
        ], timeout=30)
        if not data:
            continue
        details = data
        if isinstance(data, dict):
            # 支持 {ok:true, data:{details:[...]}} 和 {details:[...]}
            inner = data.get("data", data)
            details = inner.get("details", []) if isinstance(inner, dict) else inner
        if not isinstance(details, list):
            continue
        for d in details:
            sym = (d.get("tokenSymbol") or "").upper()
            if not sym:
                continue
            sent = d.get("sentiment") or {}
            result[sym] = {
                "mention_count": int(d.get("mentionCount", 0)),
                "x_mention_count": int(d.get("xMentionCount", 0)),
                "news_mention_count": int(d.get("newsMentionCount", 0)),
                "bullish_cnt": int(sent.get("bullishCnt", 0)),
                "bullish_ratio": float(sent.get("bullishRatio", 0)),
                "bearish_cnt": int(sent.get("bearishCnt", 0)),
                "bearish_ratio": float(sent.get("bearishRatio", 0)),
                "neutral_cnt": int(sent.get("neutralCnt", 0)),
                "neutral_ratio": 1.0 - float(sent.get("bullishRatio", 0))
                                - float(sent.get("bearishRatio", 0)),
                "label": sent.get("label", ""),
            }
    return result


def build_heat_scores(
    vol_data: list[dict], sentiment_data: dict[str, dict]
) -> list[dict]:
    """混合成交量排名 + 社交情绪 → 热度榜，格式与 compute_short_scores 一致"""
    max_mentions = 1
    for s in sentiment_data.values():
        if s.get("mention_count", 0) > max_mentions:
            max_mentions = s["mention_count"]

    vol_w = getattr(config, "OKX_VOLUME_WEIGHT", 0.4)
    sent_w = getattr(config, "OKX_SENTIMENT_WEIGHT", 0.6)

    scores = []
    excluded = getattr(config, "EXCLUDED_TOKENS", set())
    for v in vol_data:
        token = v["token"]
        if token in excluded:
            continue
        sent = sentiment_data.get(token, {})

        vol_rank = int(v.get("vol_rank", 50))
        vol_score = max((51 - vol_rank) * 2, 2) if vol_rank else 2

        mc = sent.get("mention_count", 0)
        if max_mentions > 0 and mc > 0:
            mention_score = (mc / max_mentions) * 100
        else:
            mention_score = vol_score

        bias = sent.get("bullish_ratio", 0) + 0.5 * sent.get("neutral_ratio", 0)
        score = vol_score * vol_w + mention_score * bias * sent_w

        scores.append({
            "token": token,
            "score": round(score, 1),
            "mentions": mc,
            "unique_posts": sent.get("x_mention_count", 0)
                          + sent.get("news_mention_count", 0),
            "total_likes": sent.get("bullish_cnt", 0),
            "total_comments": sent.get("bearish_cnt", 0)
                            + sent.get("neutral_cnt", 0),
            "total_shares": 0,
            "unique_authors": 0,
            "raw_score": round(score, 1),
            "author_capped_posts": 0,
            "similar_posts": 0,
        })

    scores.sort(key=lambda x: x["score"], reverse=True)
    return scores


def fetch_okx_heat_scores(top_n: int = None) -> list[dict]:
    """OKX 数据源主入口：拉数据 → 算分 → 返回热度榜"""
    limit = top_n or getattr(config, "OKX_TOP_N", 50)
    print(f"[okx] 获取成交量 Top{limit}...", flush=True)
    vol_data = fetch_top_swap_tokens(limit=limit)
    if not vol_data:
        print("[okx] 成交量数据获取失败", flush=True)
        return []

    tokens = [v["token"] for v in vol_data]
    print(f"[okx] 获取 {len(tokens)} 个代币的社交情绪...", flush=True)
    sentiment_data = fetch_sentiment_batch(tokens)

    scores = build_heat_scores(vol_data, sentiment_data)
    print(f"[okx] 热度榜: {len(scores)} 个代币", flush=True)
    for s in scores[:5]:
        print(f"  {s['token']:8s}  score={s['score']:6.1f}  "
              f"mentions={s['mentions']:5d}  posts={s['unique_posts']:4d}  "
              f"likes={s['total_likes']:5d}", flush=True)
    return scores


# ============================================================
# 链上监控 — OnchainOS CLI Wrappers
# ============================================================

def fetch_hot_tokens(chain: str = None, mc_min: float = 500000,
                     limit: int = 50, price_min: float = None,
                     price_max: float = None) -> list[dict]:
    """Stage 1 Step A: 扫描横盘代币。chain=None=全链"""
    args = [
        _ONCHAINOS_CMD, "token", "hot-tokens",
        "--rank-by", "5",
        "--time-frame", "4",
        "--limit", str(limit),
        "--market-cap-min", str(int(mc_min)),
        "--risk-filter", "true",
    ]
    if chain:
        args += ["--chain", chain]
    if price_min is not None:
        args += ["--price-change-min", str(int(price_min))]
    if price_max is not None:
        args += ["--price-change-max", str(int(price_max))]
    data = _run_okx(args, timeout=30)
    if not data or not data.get("ok"):
        return []
    result = []
    for t in data.get("data", []):
        try:
            result.append({
                "token": (t.get("tokenSymbol") or "").upper(),
                "token_address": t.get("tokenContractAddress", ""),
                "token_name": t.get("tokenName", ""),
                "chain": _chain_name(t.get("chainIndex", "")),
                "hot_rank": t.get("cursor"),
                "top10_hold_pct": float(t.get("top10HoldPercent") or 0),
                "holders_count": int(t.get("holders") or 0),
                "market_cap_usd": float(t.get("marketCap") or 0),
            })
        except (ValueError, TypeError):
            continue
    return result


def fetch_cluster_overview(token_address: str, chain: str = "bsc") -> dict | None:
    """返回聚类概览：clusterConcentration, holderSameFundSourcePercent 等"""
    data = _run_okx([
        _ONCHAINOS_CMD, "token", "cluster-overview",
        "--address", token_address,
        "--chain", chain,
    ], timeout=20)
    if not data or not data.get("ok"):
        return None
    d = data.get("data", {})
    # OnchainOS cluster-overview 返回小数 (0-1)，统一转为百分比 (0-100)
    return {
        "cluster_concentration": d.get("clusterConcentration", ""),
        "top100_holdings_pct": _pct(d.get("top100HoldingsPercent")),
        "holder_same_fund_pct": _pct(d.get("holderSameFundSourcePercent")),
        "holder_new_addr_pct": _pct(d.get("holderNewAddressPercent")),
        "holder_same_creation_pct": _pct(d.get("holderSameCreationTimePercent")),
        "rug_pull_pct": _pct(d.get("rugPullPercent")),
    }


def fetch_cluster_list(token_address: str, chain: str = "bsc") -> list[dict] | None:
    """返回所有聚类详情，包含 per-address isExchange/isContract/isKol"""
    data = _run_okx([
        _ONCHAINOS_CMD, "token", "cluster-list",
        "--address", token_address,
        "--chain", chain,
    ], timeout=20)
    if not data or not data.get("ok"):
        return None
    clusters = data.get("data", {}).get("clusterList", [])
    result = []
    for c in clusters:
        addrs = []
        for a in c.get("clusterAddressList", []):
            addrs.append({
                "address": a.get("address", ""),
                "address_rank": a.get("addressRank"),
                "holding_percent": _safe_float(a.get("holdingPercent")),
                "is_exchange": a.get("isExchange", False),
                "is_contract": a.get("isContract", False),
                "is_kol": a.get("isKol", False),
            })
        result.append({
            "holding_percent": _safe_float(c.get("holdingPercent")),
            "trend_type": c.get("trendType", []),
            "pnl_percent": _safe_float(c.get("pnlPercent")),
            "addresses": addrs,
        })
    return result


def fetch_advanced_info(token_address: str, chain: str = "bsc") -> dict | None:
    """返回代币详细信息：top10HoldPercent, riskControlLevel, tokenTags 等"""
    data = _run_okx([
        _ONCHAINOS_CMD, "token", "advanced-info",
        "--address", token_address,
        "--chain", chain,
    ], timeout=20)
    if not data or not data.get("ok"):
        return None
    d = data.get("data", {})
    return {
        "top10_hold_pct": _safe_float(d.get("top10HoldPercent")),
        "dev_hold_pct": _safe_float(d.get("devHoldingPercent")),
        "bundle_hold_pct": _safe_float(d.get("bundleHoldingPercent")),
        "risk_level": d.get("riskControlLevel", ""),
        "token_tags": d.get("tokenTags", []),
    }


def fetch_liquidity(token_address: str, chain: str = "bsc", token_symbol: str = None) -> dict | None:
    """返回 LP 流动性池详情（用于跟踪储备变化），可选过滤目标代币"""
    data = _run_okx([
        _ONCHAINOS_CMD, "token", "liquidity",
        "--address", token_address,
        "--chain", chain,
    ], timeout=20)
    if not data or not data.get("ok"):
        return None
    pools = data.get("data", [])
    total_token_amount = 0.0
    for p in pools:
        for amt in p.get("liquidityAmount", []):
            # 若指定了 symbol，只累加匹配的 token 数量（避免 WBNB/USDT 混入）
            if token_symbol and amt.get("tokenSymbol", "").upper() != token_symbol.upper():
                continue
            total_token_amount += _safe_float(amt.get("tokenAmount")) or 0.0
    return {
        "total_token_amount": total_token_amount,
        "pool_count": len(pools),
        "raw_pools": pools,
    }


def _chain_name(idx) -> str:
    """chainIndex 数字 → 链名"""
    return {
        "1": "ethereum", "56": "bsc", "137": "polygon",
        "42161": "arbitrum", "10": "optimism", "8453": "base",
        "501": "solana", "196": "xlayer", "324": "zksync",
        "43114": "avalanche", "100": "gnosis", "1101": "polygon_zkevm",
        "5000": "mantle", "59144": "linea", "204": "opbnb",
        "81457": "blast", "1088": "metis", "534352": "scroll",
        "17000": "holesky", "11155111": "sepolia",
    }.get(str(idx), str(idx))


def _safe_float(val) -> float | None:
    """安全转 float，遇空值和 '--' 返回 None"""
    if val is None or val == "" or val == "--":
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _pct(val) -> float | None:
    """安全转百分比：小数 (0-1) → 百分比 (0-100)"""
    v = _safe_float(val)
    if v is None:
        return None
    return round(v * 100, 2)


if __name__ == "__main__":
    scores = fetch_okx_heat_scores()
    print(f"\nTotal: {len(scores)} tokens")
