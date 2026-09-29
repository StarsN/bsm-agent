"""金十快讯（t.me/s/jin10light）近 24h「按币种提及」统计。

只读公开预览页（不登录）：每页 20 条，用 ?before=<最小id> 往前翻，直到早于 24h 前。
每页之间 sleep 0.8s（Telegram 预览页有限流）；只抓取不写任何东西。

输出：
  1) 近 24h 消息总数 + 时间范围 + 抓了几页
  2) 指定币种（BCH / UNI 等）的提及条数 + 样例
  3) 通用扫描：所有"独立大写 2~6 字母"token 的出现频次 Top N（数据驱动，不用手工列币表）
"""
import html as H
import io
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

CHANNEL = "jin10light"
BASE = f"https://t.me/s/{CHANNEL}"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
WINDOW_H = 24
MAX_PAGES = 30
SLEEP = 0.8

# 关注的具体币种（用户举例的两个 + 主流）
WATCH = ["BCH", "UNI", "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX",
         "LINK", "LTC", "TON", "SUI", "HYPE", "PEPE", "TRX", "DOT", "ARB", "OP",
         "ATOM", "NEAR", "APT", "FIL", "SHIB", "AAVE", "ENA", "WLFI", "TRUMP"]

# 通用扫描时要排除的非币种大写词
STOP = {"API", "ETF", "SEC", "AI", "US", "USA", "CEO", "CFO", "GDP", "CPI", "PPI",
        "FED", "FOMC", "USD", "USDT", "USDC", "PMI", "OPEC", "IPO", "ATH", "ATL",
        "TVL", "DEX", "CEX", "NFT", "DAO", "DeFi", "KYC", "AML", "IRS", "EU", "UK",
        "CN", "JP", "HK", "UN", "IMF", "WTO", "NATO", "OPEC", "WHO", "UAE", "BTCETF",
        "BTC", "ETH"}   # BTC/ETH 单独在 WATCH 里统计，通用榜里去掉避免刷屏


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers=UA)
    return urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")


def parse(h: str) -> list:
    out = []
    for b in re.split(r'<div class="tgme_widget_message_wrap', h)[1:]:
        pid = re.search(r'data-post="[^/]+/(\d+)"', b)
        dt = re.search(r'<time datetime="([^"]+)"', b)
        t = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                      b, re.S)
        if not (pid and dt):
            continue
        txt = H.unescape(re.sub(r"<[^>]+>", " ", t.group(1))).strip() if t else ""
        out.append({"id": int(pid.group(1)), "dt": dt.group(1), "text": txt})
    return out


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


def hit(text: str, sym: str) -> bool:
    """币种匹配：sym 前后不能紧邻 ASCII 字母/数字（中文可以紧贴）。
    否则 'UNI' 会命中 UNITED，'币安UNI空投' 又该命中。"""
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(sym)}(?![A-Za-z0-9])", text) is not None


print(f"抓取 {BASE} …")
msgs: dict[int, dict] = {}
h0 = fetch(BASE)
page = parse(h0)
if not page:
    print("首页没解析出消息——页面结构可能变了")
    sys.exit(1)
for m in page:
    msgs[m["id"]] = m
pages = 1
cutoff = datetime.now(timezone.utc) - timedelta(hours=WINDOW_H)

while pages < MAX_PAGES:
    oldest = min(msgs)
    if ts(max(m["dt"] for m in page)) < cutoff and \
       ts(min(m["dt"] for m in page)) < cutoff:
        break                      # 这一页已经整体早于窗口 → 够了
    time.sleep(SLEEP)
    page = parse(fetch(f"{BASE}?before={oldest}"))
    if not page:
        break
    pages += 1
    for m in page:
        msgs[m["id"]] = m
    print(f"  第 {pages} 页 … 累计 {len(msgs)} 条，最旧 {min(m['dt'] for m in msgs.values())}")

win = sorted((m for m in msgs.values() if ts(m["dt"]) >= cutoff),
             key=lambda x: x["dt"])
print(f"\n===== 近 {WINDOW_H} 小时 =====")
print(f"抓了 {pages} 页，解析 {len(msgs)} 条，落在窗口内 {len(win)} 条")
if win:
    print(f"时间范围: {win[0]['dt']}  ~  {win[-1]['dt']}（UTC）")

print("\n===== 关注币种的提及条数 =====")
rows = []
for sym in WATCH:
    hits = [m for m in win if hit(m["text"], sym)]
    rows.append((sym, len(hits), hits))
for sym, n, hits in sorted(rows, key=lambda x: -x[1]):
    if n:
        print(f"  {sym:<6} {n:>3} 条")
none = [s for s, n, _ in rows if not n]
print(f"  （未提及: {' '.join(none) if none else '无'}）")

for sym in ("BCH", "UNI"):
    hits = next((h for s, _, h in rows if s == sym), [])
    print(f"\n----- {sym} 的 {len(hits)} 条原文 -----")
    for m in hits:
        print(f"  [{m['dt'][:16]}] #{m['id']} {m['text'][:150]}")

print("\n===== 通用扫描：独立大写词 Top 25（数据驱动，未过滤全部噪声）=====")
freq: dict[str, int] = {}
for m in win:
    for tok in set(re.findall(r"(?<![A-Za-z0-9])[A-Z]{2,6}(?![A-Za-z0-9])", m["text"])):
        if tok in STOP:
            continue
        freq[tok] = freq.get(tok, 0) + 1
for tok, c in sorted(freq.items(), key=lambda x: -x[1])[:25]:
    print(f"  {tok:<8} {c:>3}")
