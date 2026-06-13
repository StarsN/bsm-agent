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
    for v in vol_data:
        token = v["token"]
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


if __name__ == "__main__":
    scores = fetch_okx_heat_scores()
    print(f"\nTotal: {len(scores)} tokens")
