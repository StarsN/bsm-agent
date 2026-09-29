"""回归：TRADIFI_PERPETUAL（CL/NVDA/XAU 这类美股/商品永续）在跟单链路必须可见。

2026-09-28 事故：交易员开 CLUSDT 空，自动跟单正常（纸盘 + 实盘都开了、还加到 7.75），
但用户点「⚡快速跟随」被**静默跳过**：quick_follow 拿 has_perpetual() 判"币安有没有
这个永续"，而它只认 contractType=PERPETUAL —— CL 是 TRADIFI_PERPETUAL（币安实测
206 个这种符号），于是被当成"币安无该永续"：不建纸盘、不认领实盘、日志里一个字没有。

不联网：用假 exchangeInfo 顶替 market._http_get。钉住三条不变量：
  ① get_futures_symbols() 不含 TRADIFI（200+ 股票/ETF 不能涌进热度榜/自动交易/链上扫描）
  ② has_tradable_perp() 认两类；has_perpetual() 保持原语义（只管纯加密）
  ③ alphafox_trade 的两个调用点用的就是 has_tradable_perp（防回退）
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name}  {extra}")


FAKE_INFO = {"symbols": [
    {"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT",
     "contractType": "PERPETUAL", "status": "TRADING", "onboardDate": 1569398400000},
    {"symbol": "ETHUSDT", "baseAsset": "ETH", "quoteAsset": "USDT",
     "contractType": "PERPETUAL", "status": "SETTLING"},
    {"symbol": "CLUSDT", "baseAsset": "CL", "quoteAsset": "USDT",
     "contractType": "TRADIFI_PERPETUAL", "status": "TRADING"},
    {"symbol": "AAPLUSDT", "baseAsset": "AAPL", "quoteAsset": "USDT",
     "contractType": "TRADIFI_PERPETUAL", "status": "TRADING"},
    {"symbol": "XAUUSDT", "baseAsset": "XAU", "quoteAsset": "USDT",
     "contractType": "TRADIFI_PERPETUAL", "status": "PENDING_TRADING"},
    {"symbol": "BTCUSDT_260327", "baseAsset": "BTC", "quoteAsset": "USDT",
     "contractType": "CURRENT_QUARTER", "status": "TRADING"},
]}

calls = []


def fake_http(url, params=None, timeout=None, **kw):
    calls.append(url)
    return FAKE_INFO


market._http_get = fake_http
market._FUTURES_SYMBOLS_CACHE = {"ts": 0.0, "symbols": set()}
market._TRADIFI_SYMBOLS_CACHE = {"ts": 0.0, "symbols": set()}
market._LISTING_CACHE = {"ts": 0.0, "open_times": {}}

fut = market.get_futures_symbols()
check("纯加密永续照旧进 symbols", fut == {"BTC"}, str(fut))
check("非 TRADING 的永续不收（SETTLING 的 ETH）", "ETH" not in fut)
check("交割合约（CURRENT_QUARTER）不收", len(fut) == 1)

check("has_perpetual 语义不变：TRADIFI 仍为 False",
      market.has_perpetual("CL") is False)
check("TRADIFI 单独收一份（CL/AAPL，不含未上市的 XAU）",
      market.get_tradifi_symbols() == {"CL", "AAPL"},
      str(market.get_tradifi_symbols()))
check("has_tradable_perp：TRADIFI 为 True", market.has_tradable_perp("CL") is True)
check("has_tradable_perp：纯加密为 True", market.has_tradable_perp("BTC") is True)
check("has_tradable_perp：没上市的仍为 False",
      market.has_tradable_perp("XAU") is False
      and market.has_tradable_perp("DOGE") is False)
check("两份缓存共用一次 exchangeInfo（零额外请求）", len(calls) == 1, str(calls))
check("TRADIFI 不漏进 get_futures_symbols（热度榜/自动交易不受污染）",
      market.get_futures_symbols() == {"BTC"})

import alphafox_trade as aft  # noqa: E402  （放在缓存断言之后：导入即有网络/DB 风险）

# 源码读 __source__ 优先：变异框架把"改过的源码"挂在模块上（磁盘不动），
# 直接 inspect.getsource 会读到磁盘原文 → 对变异视而不见（假绿灯）
src = getattr(aft, "__source__", None) or inspect.getsource(aft)
check("alphafox_trade 已不再引用 has_perpetual（两处调用点都换了）",
      "has_perpetual" not in src)
# getattr 兜底：变异掉这条 import 时是"没有这个属性"，不该把测试崩掉当抓住
check("alphafox_trade 用的是 market.has_tradable_perp（同一个函数对象）",
      getattr(aft, "has_tradable_perp", None) is market.has_tradable_perp)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
