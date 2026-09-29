"""侦察 finance.baidu.com 的美股数据接口（只读浏览，不登录不写数据）。

用真浏览器渲染 → 记录所有 JSON 接口 → 输出"美股相关接口 + 字段"清单。
"""
import io
import json
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
from playwright.sync_api import sync_playwright  # noqa: E402

TARGETS = [
    "https://finance.baidu.com/",
    "https://gushitong.baidu.com/",
]
hits = []


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            "--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"),
            locale="zh-CN", viewport={"width": 1440, "height": 900})
        page = ctx.new_page()

        def on_resp(resp):
            u = resp.url
            if not re.search(r"(pae|api|vapi|quotation|market|stock)", u, re.I):
                return
            ct = (resp.headers or {}).get("content-type", "")
            if "json" not in ct and "javascript" not in ct:
                return
            try:
                body = resp.text()[:600]
            except Exception:
                return
            hits.append((resp.status, u[:220], body[:300]))

        page.on("response", on_resp)
        for url in TARGETS:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(6000)
                title = page.title()
                print(f"=== {url} → title={title!r} 当前URL={page.url}")
                # 找页面上的"美股"入口
                for kw in ("美股", "US", "行情"):
                    try:
                        el = page.locator(f"text={kw}").first
                        if el.count():
                            print(f"    命中入口文字: {kw}")
                    except Exception:
                        pass
                # 打印可见的板块文字（前 40 个链接）
                links = page.eval_on_selector_all(
                    "a", "els => els.slice(0,40).map(e => e.innerText.trim()).filter(Boolean)")
                print("    链接:", " | ".join(dict.fromkeys(links))[:400])
            except Exception as e:  # noqa: BLE001
                print(f"=== {url} 失败: {type(e).__name__} {str(e)[:120]}")
        browser.close()

    print(f"\n=== 捕获到 {len(hits)} 个 JSON 响应 ===")
    seen = set()
    for st, u, b in hits:
        key = u.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        print(f"\n[{st}] {u}")
        print(f"      {b[:260]}".replace("\n", " "))


main()
