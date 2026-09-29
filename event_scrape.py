"""金十快讯抓取：Telegram **公开预览页** `https://t.me/s/<channel>`。

不需要登录 / bot token / 客户端：每页 20 条，`?before=<msg_id>` 往前翻，翻页之间不重叠。
每条带 **消息 id**（去重键 + 游标）和 **ISO 8601 / UTC** 时间戳。

解析单独成函数（纯字符串进、结构出），方便照真样本做夹具测试；
抓取只在 `fetch_page` 里，带 UA、超时、单次调用。
"""
import html as H
import re
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/126.0 Safari/537.36"}
BASE = "https://t.me/s/"
_TIMEOUT = 25

_BLOCK = re.compile(r'<div class="tgme_widget_message_wrap')
_POST = re.compile(r'data-post="[^/]+/(\d+)"')
_TIME = re.compile(r'<time datetime="([^"]+)"')
_TEXT = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                   re.S)


def parse_messages(html_text: str) -> list[dict]:
    """预览页 HTML → `[{msg_id, ts, text}]`（按 msg_id 升序，缺时间/正文的块跳过）。

    只认**有正文**的消息：纯图片/纯链接的块 `message_text` 缺失，丢弃比入库空文本好。"""
    out = []
    for b in _BLOCK.split(html_text or "")[1:]:
        m_post, m_time = _POST.search(b), _TIME.search(b)
        if not (m_post and m_time):
            continue
        m_text = _TEXT.search(b)
        if not m_text:
            continue
        # 正文里可能有 <br>/<a>：去标签 + 反转义，保留可读文本
        txt = H.unescape(re.sub(r"<br\s*/?>", " ", m_text.group(1)))
        txt = H.unescape(re.sub(r"<[^>]+>", "", txt)).strip()
        if not txt:
            continue
        out.append({"msg_id": int(m_post.group(1)),
                    "ts": m_time.group(1),
                    "text": txt})
    out.sort(key=lambda x: x["msg_id"])
    return out


def fetch_page(channel: str, before: int | None = None) -> str:
    """拉一页预览页 HTML。before=<msg_id> → 取比它更早的一页（None = 最新页）。"""
    url = f"{BASE}{channel}" + (f"?before={int(before)}" if before else "")
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")
