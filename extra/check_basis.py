#!/usr/bin/env python3
"""查询 Binance premiumIndex，验证 indexPrice 和基差计算。不依赖项目代码。"""
from urllib.request import Request, urlopen
import json, sys

FAPI = "https://fapi.binance.com/fapi/v1"

# 当前热度榜常见代币
tokens = ["BTC", "ETH", "BNB", "SOL", "DOGE", "PEPE", "WIF", "BONK", "RENDER", "FET"]

for token in tokens:
    symbol = f"{token}USDT"
    try:
        req = Request(
            f"{FAPI}/premiumIndex?symbol={symbol}",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"{token}: 查询失败 - {e}")
        continue

    mark = data.get("markPrice")
    idx = data.get("indexPrice")

    if mark and idx:
        try:
            mark_f = float(mark)
            idx_f = float(idx)
            basis = round((mark_f - idx_f) / idx_f * 100, 4)
            print(f"{token}: mark={mark_f:.4f}  index={idx_f:.4f}  basis={basis}%")
        except (ValueError, TypeError) as e:
            print(f"{token}: 计算失败 - {e}")
    else:
        print(f"{token}: 缺字段 markPrice={mark} indexPrice={idx}")
        print(f"  完整响应: {json.dumps(data, indent=2)[:200]}")
