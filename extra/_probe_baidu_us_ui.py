"""走真实 UI 抓百度 FinScope 的美股数据接口（点"美股"入口，录它自己发的请求）。"""
import io
import json
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

        def on_resp(r):
            if "finance.pae.baidu.com" not in r.url:
                return
            ct = (r.headers or {}).get("content-type", "")
            if "json" not in ct:
                return
            try:
                t = r.text()
            except Exception:
                return
            rows.append((r.status, r.url, t[:400]))

        page.on("response", on_resp)
        page.goto("https://finance.baidu.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        # 打印导航区文字，找到"美股"入口
        try:
            nav = page.eval_on_selector_all(
                "div,span,li,a",
                "els => els.filter(e => e.innerText && e.innerText.trim().length<8 "
                "&& /美股|港股|沪深|A股|自选|榜单|行情/.test(e.innerText)).slice(0,25)"
                ".map(e => e.innerText.trim())")
            print("导航候选:", " | ".join(dict.fromkeys(nav))[:300])
        except Exception as e:  # noqa: BLE001
            print("导航读取失败", e)

        for label in ("美股", "行情"):
            try:
                loc = page.get_by_text(label, exact=True).first
                if loc.count():
                    loc.click(timeout=8000)
                    print(f"点击了 {label!r}，等待加载…")
                    page.wait_for_timeout(6000)
                    print("  当前 URL:", page.url)
                    break
            except Exception as e:  # noqa: BLE001
                print(f"点击 {label!r} 失败: {str(e)[:100]}")

        # 页面可见的板块标题（了解有哪些美股模块）
        try:
            heads = page.eval_on_selector_all(
                "h1,h2,h3,.title,[class*=title]",
                "els => els.map(e => e.innerText.trim()).filter(t => t && t.length<20).slice(0,40)")
            print("\n页面模块标题:", " | ".join(dict.fromkeys(heads))[:600])
        except Exception:
            pass
        ctx.close()
        b.close()

    print(f"\n=== 捕获 {len(rows)} 个接口响应 ===")
    seen = set()
    for st, u, t in rows:
        key = re.sub(r"[?&](ts|_|callback)=[^&]*", "", u.split("?")[0] + "?" +
                     "&".join(sorted(re.findall(r"([a-zA-Z]+)=", u))))
        if key in seen:
            continue
        seen.add(key)
        path = u.split("finance.pae.baidu.com")[-1][:150]
        print(f"\n[{st}] {path}")
        print(f"     {t[:230]}".replace("\n", " "))


main()
