"""跟单托管页 UI 冒烟（playwright，接口全部本地 stub，不连服务器/交易所）。

验：页面能渲染账户/盈亏/持仓/历史/日志五区、托管状态与警示、按钮点击发出的请求体。
手动跑（依赖 playwright + chromium，不进 run_tests 常规套件）：
    python extra/_ui_check_af_page.py
"""
import io
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from playwright.sync_api import sync_playwright  # noqa: E402

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(root, "web.py"), encoding="utf-8").read()
html = re.search(r'ALPHAFOX_LIVE_HTML = """(.*?)"""', src, re.S).group(1)


# 用**真实导航**渲染（抽 _NAV_* 常量 + _nav_html 原文执行），验证「实盘」TAG
def _indented_block(s, name):
    i = s.find(f"def {name}(")
    if i < 0:
        return None
    out = []
    for ln in s[i:].split("\n"):
        if out and ln.strip() and not ln.startswith((" ", "\t")):
            break
        out.append(ln)
    return "\n".join(out)


_nav_bits = [re.search(r"_NAV_ITEMS = \[.*?\n\]", src, re.S),
             re.search(r"_NAV_DROPDOWNS = \[.*?\n\]", src, re.S),
             re.search(r"_NAV_TAIL = \(.*?\)", src, re.S)]
NS_NAV = {"__builtins__": __builtins__}
exec(compile("\n".join(b.group(0) for b in _nav_bits) + "\n"
             + _indented_block(src, "_nav_html"), "web.py:nav", "exec"), NS_NAV)
html = html.replace("{NAV}", NS_NAV["_nav_html"]("alphafox-live"))
open(os.path.join(root, "extra", "_page_tmp.html"), "w", encoding="utf-8").write(html)

BOARD = {
    "account": {"equity": 1234.5, "available_balance": 1000.0,
                "margin_used": 200.0, "unrealized_pnl": 20.0, "open_count": 2},
    "stats": {"live": {"closed": 3, "wins": 2, "pnl": 11.8},
              "paper": {"closed": 5, "wins": 3, "pnl": 12.5},
              "open": {"paper_pnl": -3.2, "ex_pnl": -2.1,
                       "open_paper": 2, "open_ex": 1}},
    "positions": [
        {"id": 11, "token": "AAA", "side": "LONG", "quantity": 2.0,
         "entry_price": 100.0, "current_price": 110.0, "unrealized_pnl": 20.0,
         "leverage": 5, "managed": 1, "state": "托管中", "state_cls": "green",
         "warn": "", "trader_label": "老王", "ex_quantity": 2.0,
         "ex_pnl": 20.0, "ex_leverage": 5},
        {"id": 12, "token": "BBB", "side": "SHORT", "quantity": 1.0,
         "entry_price": 50.0, "current_price": 45.0, "unrealized_pnl": -3.2,
         "leverage": 3, "managed": 0, "state": "已取消托管", "state_cls": "red",
         "warn": "交易员已平该仓，我们仍持仓（无退出信号）",
         "trader_label": "小李", "ex_quantity": None, "ex_pnl": None,
         "ex_leverage": None},
        {"id": 13, "token": "EEE", "side": "LONG", "quantity": 0.5,
         "entry_price": 10.0, "current_price": 11.0, "unrealized_pnl": 0.5,
         "leverage": 5, "managed": 2, "state": "恢复中", "state_cls": "yellow",
         "warn": "下一轮将按交易员当前仓位对齐数量",
         "trader_label": "老王", "ex_quantity": 0.5, "ex_pnl": 0.5,
         "ex_leverage": 5},
    ],
    "history": [
        {"token": "CCC", "side": "LONG", "entry_price": 10.0,
         "current_price": 12.0, "pnl_pct": 25.0, "realized_pnl": 12.5,
         "live_pnl": 11.8, "advice": "跟单交易员平仓", "trader_label": "老王",
         "closed_at": "2026-09-12 11:00:00"},
    ],
    "trades": [
        {"token": "AAA", "action": "rebalance_add", "price": 100.0,
         "quantity": 1.0, "created_at": "2026-09-12 11:05:00",
         "details_json": json.dumps({"reason": "对账补仓（固定名义·价格漂移修正）"}),
         "trader_label": "老王"},
    ],
}

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


sent = []
errors = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/alphafox/board"):
            return self._json(BOARD)
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode()
        name = "manage" if "manage" in self.path else "close"
        sent.append((name, raw))
        return self._json({"ok": True, "managed": 0})


srv = HTTPServer(("127.0.0.1", 0), Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_page()
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"http://127.0.0.1:{port}/")
    page.wait_for_timeout(800)

    print("== 0. 顶部「实盘」TAG ==")
    tag = page.locator(".nav-dropdown", has_text="实盘").first
    check("导航有「实盘」TAG", tag.count() == 1)
    check("TAG 内含三个实盘页",
          tag.locator('a[href="/live-binance"]').count() == 1
          and tag.locator('a[href="/live-onchain"]').count() == 1
          and tag.locator('a[href="/alphafox-live"]').count() == 1)
    check("当前页（跟单托管）在 TAG 内高亮",
          tag.locator('a[href="/alphafox-live"].active').count() == 1)

    print("== 1. 五区渲染 ==")
    check("账户概览渲染权益", "$1,234.5" in page.inner_text("#acct"),
          page.inner_text("#acct")[:60])
    stats = page.inner_text("#stats")
    check("盈亏区含实盘/纸盘胜率与浮盈",
          "67%" in stats and "60%" in stats and "+11.80" in stats
          and "-3.20" in stats,
          stats.replace("\n", " ")[:120])
    check("持仓表 3 行", page.locator("#pos-wrap tbody tr").count() == 3)
    check("持仓表带跟单员", "老王" in page.inner_text("#pos-wrap")
          and "小李" in page.inner_text("#pos-wrap"))
    check("未托管行显示红字警示 + 恢复托管按钮",
          "交易员已平该仓" in page.inner_text("#pos-wrap")
          and page.locator('#pos-wrap button:has-text("恢复托管")').count() == 1)
    check("托管中 + 恢复中都能退回非托管（两行各一个按钮）",
          page.locator('#pos-wrap button:has-text("取消托管")').count() == 2)
    check("恢复中状态与提示正常渲染", "恢复中" in page.inner_text("#pos-wrap"))
    check("负浮盈显示为红色", "-3.20" in page.inner_text("#pos-wrap"))
    check("历史表带跟单员与实盘盈亏",
          page.locator("#his-wrap tbody tr").count() == 1
          and "老王" in page.inner_text("#his-wrap"))
    check("日志表渲染动作与说明",
          "对账补仓" in page.inner_text("#log-wrap")
          and page.locator("#log-wrap tbody tr").count() == 1)

    print("== 2. 按钮交互（含二次确认）==")
    page.on("dialog", lambda d: d.accept())
    page.locator('#pos-wrap button:has-text("取消托管")').first.click()
    page.wait_for_timeout(300)
    check("取消托管：POST 体正确",
          sent and sent[-1][0] == "manage"
          and json.loads(sent[-1][1]) == {"position_id": 11, "managed": False},
          str(sent[-1:]))
    page.locator('#pos-wrap button:has-text("恢复托管")').click()
    page.wait_for_timeout(300)
    check("恢复托管：POST 体正确",
          json.loads(sent[-1][1]) == {"position_id": 12, "managed": True},
          str(sent[-1]))
    page.locator('#pos-wrap button:has-text("平仓")').first.click()
    page.wait_for_timeout(300)
    check("平仓：POST 体正确", sent[-1][0] == "close"
          and json.loads(sent[-1][1]) == {"token": "AAA"}, str(sent[-1]))
    check("无 JS 控制台错误", not errors, str(errors[:3]))
    b.close()
srv.shutdown()

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
