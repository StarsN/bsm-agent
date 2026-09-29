"""搜 AAPL → 进美股个股详情页，录全部数据接口（美股数据最全的入口）。"""
import io
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
from playwright.sync_api import sync_playwright  # noqa: E402

rows = []


def main():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                                        "Chrome/126.0.0.0 Safari/537.36"),
                            locale="zh-CN", viewport={"width": 1500, "height": 950})
        page = ctx.new_page()

        def rec(r):
            if "finance.pae.baidu.com" not in r.url:
                return
            if "json" not in (r.headers or {}).get("content-type", ""):
                return
            try:
                rows.append((r.status, r.url, r.text()[:300]))
            except Exception:
                pass

        page.on("response", rec)
        page.goto("https://finance.baidu.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)

        # 找搜索框并搜 AAPL
        filled = False
        for sel in ("input[type=text]", "input[placeholder*='搜索']", "input"):
            try:
                loc = page.locator(sel).first
                if loc.count():
                    loc.click(timeout=5000)
                    loc.fill("AAPL")
                    page.keyboard.press("Enter")
                    filled = True
                    print("已在搜索框输入 AAPL 并回车")
                    break
            except Exception:
                continue
        if not filled:
            print("没找到搜索框，直接尝试美股详情 URL")
            for u in ("https://finance.baidu.com/stock/us/AAPL",
                      "https://finance.baidu.com/us/AAPL",
                      "https://finance.baidu.com/stock/AAPL"):
                try:
                    page.goto(u, wait_until="domcontentloaded", timeout=30000)
                    if "AAPL" in page.url:
                        break
                except Exception:
                    pass
        page.wait_for_timeout(8000)
        print("当前 URL:", page.url)
        print("标题:", page.title())

        # 关键词探测：页面上有哪些美股字段
        try:
            txt = page.inner_text("body")[:4000]
            kws = ["市盈", "市值", "每股收益", "股息", "财报", "营收", "净利润", "盘前", "盘后",
                   "成交量", "振幅", "52周", "市盈率", "市净率", "总股本", "机构", "分析师",
                   "目标价", "评级", "分时", "K线", "新闻", "公告", "股东"]
            hit = [k for k in kws if k in txt]
            print("页面出现的数据关键词:", " | ".join(hit))
        except Exception as e:  # noqa: BLE001
            print("读取正文失败", e)
        ctx.close()
        b.close()

    print(f"\n=== 详情页捕获 {len(rows)} 个接口 ===")
    seen = set()
    for st, u, t in rows:
        path = u.split("finance.pae.baidu.com")[-1]
        key = path.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        print(f"\n[{st}] {path[:170]}")
        print(f"     {t[:210]}".replace("\n", " "))


main()
