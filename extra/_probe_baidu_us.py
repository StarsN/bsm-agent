"""探 finance.baidu.com（百度 FinScope）的美股数据接口清单。

在页面上下文里 fetch（带同源 cookie），逐个打印返回结构与关键字段。
只读探测，不写数据、不登录。
"""
import io
import json
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
from playwright.sync_api import sync_playwright  # noqa: E402

BASE = "https://finance.pae.baidu.com"
# 候选接口（market=us / market_type=us / 美股 tag）
PROBES = [
    ("美股个股行情", "/vapi/v1/getquotation?srcid=5353&all=1&code=AAPL&market_type=us&group=quotation_minute_ab&finClientType=pc"),
    ("美股榜单-涨幅", "/vapi/v1/ranks?bizType=stock_rank&market=us&pn=0&rn=5&sortKey=pxChangeRate&sortType=desc&fieldsType=base&finClientType=pc"),
    ("美股榜单-成交额", "/vapi/v1/ranks?bizType=stock_rank&market=us&pn=0&rn=5&sortKey=amount&sortType=desc&fieldsType=base&finClientType=pc"),
    ("美股热度榜", "/vapi/v1/hotrank?tn=wisexmlnew&dsp=iphone&product=stock&style=tablelist&type=hour&day=20260912&hour=1&pn=0&rn=5&finClientType=pc&market=us"),
    ("美股板块热力图", "/vapi/v2/blocks?style=heatmap&market=us&typeCode=HY&sortKey=amount&sortType=desc&pn=0&rn=8&finClientType=pc"),
    ("美股异动信号", "/sapi/v1/marketquote?anomalyType=all&bizType=anomalySignal&market=us&signalType=market&signalTarget=block&pn=0&rn=2&finClientType=pc"),
    ("美股指数", "/vapi/v1/getquotation?srcid=5353&all=1&code=.IXIC&market_type=us&group=quotation_minute_ab&finClientType=pc"),
    ("7x24快讯(美股)", "/selfselect/expressnews?rn=4&pn=0&tag=%E7%BE%8E%E8%82%A1&finClientType=pc"),
    ("美国宏观指标", "/sapi/v1/financelsegmacro/hotmetrics?country=%E7%BE%8E%E5%9B%BD&finClientType=pc"),
]


def brief(obj, depth=0):
    """把 JSON 压成"字段: 样例值"清单"""
    if isinstance(obj, dict):
        return {k: brief(v, depth + 1) for k, v in list(obj.items())[:14]}
    if isinstance(obj, list):
        return [brief(obj[0], depth + 1)] if obj else []
    s = str(obj)
    return s[:40]


with sync_playwright() as p:
    b = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
    ctx = b.new_context(user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/126.0.0.0 Safari/537.36"),
                        locale="zh-CN")
    page = ctx.new_page()
    page.goto("https://finance.baidu.com/", wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(4000)
    for name, path in PROBES:
        try:
            txt = page.evaluate(
                """async (u) => {
                    try {
                      const r = await fetch(u, {headers: {'Accept':'application/json'}});
                      const t = await r.text();
                      return r.status + '|' + t.slice(0, 1200);
                    } catch (e) { return 'ERR|' + e; }
                }""", BASE + path)
            st, body = txt.split("|", 1)
            print(f"\n=== {name}  [{st}]  {path[:90]}")
            if st != "200":
                print("    ", body[:200])
                continue
            try:
                j = json.loads(body)
                print("    字段:", json.dumps(brief(j), ensure_ascii=False)[:900])
            except Exception:
                print("    非JSON:", body[:200])
        except Exception as e:  # noqa: BLE001
            print(f"\n=== {name} 探测失败: {type(e).__name__} {str(e)[:100]}")
    b.close()
