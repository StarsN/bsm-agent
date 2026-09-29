"""找到美股详情页 URL 规则并进入，录全部数据接口。"""
import io
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
                rows.append((r.status, r.url, r.text()[:260]))
            except Exception:
                pass

        page.on("response", rec)
        page.goto("https://finance.baidu.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        try:
            page.locator("input").first.click(timeout=5000)
            page.locator("input").first.type("AAPL", delay=90)
            page.wait_for_timeout(3500)
            sug = page.evaluate("""() => {
                const out = [];
                document.querySelectorAll('a[href]').forEach(a => {
                    const t = (a.innerText||'').trim();
                    if (/AAPL|苹果/i.test(t)) out.push({t: t.slice(0,40), h: a.href});
                });
                return out.slice(0, 6);
            }""")
            print("下拉建议:", sug)
            if sug:
                page.goto(sug[0]["h"], wait_until="domcontentloaded", timeout=40000)
                page.wait_for_timeout(8000)
                print("详情页 URL:", page.url)
                print("标题:", page.title())
                txt = page.inner_text("body")[:6000]
                kws = ["市盈率", "市净率", "总市值", "每股收益", "股息", "财报", "营收", "净利润",
                       "盘前", "盘后", "成交量", "振幅", "52周", "总股本", "机构", "分析师",
                       "目标价", "评级", "分时", "新闻", "公告", "股东", "行业", "概念"]
                print("详情页字段关键词:", " | ".join(k for k in kws if k in txt))
        except Exception as e:  # noqa: BLE001
            print("搜索失败:", str(e)[:150])
        ctx.close()
        b.close()

    print(f"\n=== 捕获 {len(rows)} 个接口 ===")
    seen = set()
    for st, u, t in rows:
        path = u.split("finance.pae.baidu.com")[-1]
        key = path.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        print(f"\n[{st}] {path[:165]}")
        print(f"     {t[:200]}".replace("\n", " "))


main()
