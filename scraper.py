"""
币安广场抓取器（持续抓取版）

一次 scrape_once() 持续 config.SCRAPE_ROUND_SECONDS 秒：
- 打开一次浏览器
- 不断滚动
- 每滚动 SCROLL_RESET_EVERY 次刷新一次页面，避免懒加载卡死
- 时间到自动关闭

这样一轮能抓到的帖子比"打开-滚几十次-关闭"多几倍。
"""
import asyncio
import os
import random
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from playwright.async_api import async_playwright, Response
import config


FEED_API_KEYWORD = "pgc/feed"
SQUARE_URL = "https://www.binance.com/en/square"
USER_DATA_DIR = Path(__file__).parent / "user_data"


def _utcnow():
    return datetime.now(timezone.utc)


def _build_chrome_args() -> list[str]:
    args = ["--disable-blink-features=AutomationControlled"]
    if getattr(config, "LOW_MEMORY_MODE", False):
        args.extend([
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-component-extensions-with-background-pages",
            "--disable-sync",
            "--no-sandbox",
            "--renderer-process-limit=1",
            "--js-flags=--max-old-space-size=256",
        ])
    return args


class SquareScraper:
    def __init__(self):
        self.captured_posts = []
        self.captured_authors = {}

    async def _handle_response(self, response: Response):
        url = response.url
        if FEED_API_KEYWORD not in url:
            return
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        try:
            data = await response.json()
        except Exception:
            return
        self._scan_for_posts(data)

    def _scan_for_posts(self, node):
        if isinstance(node, dict):
            for key in ("vos", "list", "items", "feedList", "posts"):
                val = node.get(key)
                if isinstance(val, list) and val:
                    if any(self._looks_like_post(x) for x in val[:3]):
                        for post in val:
                            if isinstance(post, dict):
                                try:
                                    self._process_post(post)
                                except Exception:
                                    pass
            for v in node.values():
                self._scan_for_posts(v)
        elif isinstance(node, list):
            for item in node:
                self._scan_for_posts(item)

    @staticmethod
    def _looks_like_post(obj) -> bool:
        if not isinstance(obj, dict):
            return False
        has_content = any(k in obj for k in ("content", "title", "text"))
        has_author = any(k in obj for k in ("authorName", "squareAuthorId", "username", "authorId"))
        has_engagement = any(k in obj for k in ("likeCount", "commentCount", "viewCount"))
        return has_content and has_author and has_engagement

    def _process_post(self, raw: dict):
        post_id = str(
            raw.get("id") or raw.get("contentId") or raw.get("postId")
            or raw.get("handWork") or ""
        )
        if not post_id:
            return

        user_id = str(raw.get("squareAuthorId") or raw.get("authorId") or "")
        if not user_id:
            return

        content = raw.get("content") or ""
        title = raw.get("title") or ""
        full_text = (title + "\n" + content).strip() if title else content

        posted_ts = raw.get("date") or raw.get("createTime")
        if isinstance(posted_ts, (int, float)):
            if posted_ts > 1e11:
                posted_at = datetime.fromtimestamp(posted_ts / 1000, tz=timezone.utc)
            else:
                posted_at = datetime.fromtimestamp(posted_ts, tz=timezone.utc)
        else:
            posted_at = _utcnow()

        likes = int(raw.get("likeCount") or 0)
        comments = int(raw.get("commentCount") or 0)
        shares = int(raw.get("quoteCount") or raw.get("shareCount") or 0)

        tokens = self._extract_tokens_from_post(raw)
        followers = self._extract_followers(raw.get("userLabels") or [])

        self.captured_authors[user_id] = {
            "user_id": user_id,
            "username": raw.get("authorName") or raw.get("username") or "",
            "followers": followers,
            "following": 0,
            "account_created": None,
        }

        self.captured_posts.append({
            "post_id": post_id,
            "user_id": user_id,
            "content": full_text[:2000],
            "likes": likes,
            "comments": comments,
            "shares": shares,
            "posted_at": posted_at,
            "fetched_at": _utcnow(),
            "tokens": tokens,
        })

    @staticmethod
    def _extract_tokens_from_post(raw: dict) -> set[str]:
        tokens = set()
        for tp in (raw.get("tradingPairs") or []):
            code = tp.get("code")
            if code:
                tokens.add(code.upper())
        for tp in (raw.get("tradingPairsV2") or []):
            code = tp.get("code")
            if code:
                tokens.add(code.upper())
        for item in (raw.get("coinPairList") or []):
            if isinstance(item, str):
                sym = item.strip().lstrip("$").strip().upper()
                if sym:
                    tokens.add(sym)
        return tokens

    @staticmethod
    def _extract_followers(user_labels: list) -> int:
        for label in user_labels:
            name = (label.get("name") or "").strip()
            if "follower" not in name.lower():
                continue
            parts = name.split()
            if not parts:
                continue
            num_str = parts[0].replace(",", "")
            try:
                if num_str.lower().endswith("k"):
                    return int(float(num_str[:-1]) * 1000)
                if num_str.lower().endswith("m"):
                    return int(float(num_str[:-1]) * 1_000_000)
                return int(float(num_str))
            except ValueError:
                continue
        return 0

    async def scrape_continuous(self, duration_seconds: int,
                                 progress_cb=None) -> tuple[list[dict], dict[str, dict]]:
        self.captured_posts = []
        self.captured_authors = {}

        # 浏览器缓存 >200MB 就清理
        try:
            if USER_DATA_DIR.exists():
                total = 0
                for root, _, files in os.walk(str(USER_DATA_DIR)):
                    for f in files:
                        try:
                            total += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
                if total > 200 * 1024 * 1024:
                    shutil.rmtree(str(USER_DATA_DIR), ignore_errors=True)
        except Exception:
            pass

        start = time.time()
        scrolls = 0
        next_reset_at = random.randint(
            getattr(config, "SCROLL_RESET_EVERY_MIN", 30),
            getattr(config, "SCROLL_RESET_EVERY_MAX", 55),
        )
        burst_seconds = getattr(config, "SCRAPE_BURST_SECONDS", 50)
        idle_seconds = getattr(config, "SCRAPE_IDLE_SECONDS", 45)

        # 清理 Windows Chromium 锁文件，防止 launch_persistent_context 卡死
        if USER_DATA_DIR.exists():
            for lock_name in ("SingletonLock", "SingletonSocket"):
                lock_path = USER_DATA_DIR / lock_name
                try:
                    lock_path.unlink(missing_ok=True)
                except Exception:
                    pass

        async with async_playwright() as p:
            context = await asyncio.wait_for(
                p.chromium.launch_persistent_context(
                    user_data_dir=str(USER_DATA_DIR),
                    headless=config.HEADLESS,
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/126.0.0.0 Safari/537.36"),
                    viewport={"width": 1440, "height": 900},
                    args=_build_chrome_args(),
                ),
                timeout=getattr(config, "BROWSER_LAUNCH_TIMEOUT", 120),
            )
            page = context.pages[0] if context.pages else await context.new_page()
            page.on("response", self._handle_response)

            try:
                goto_timeout = getattr(config, "PAGE_GOTO_TIMEOUT", 90000)
                for goto_attempt in range(3):
                    try:
                        await page.goto(SQUARE_URL, wait_until="domcontentloaded",
                                        timeout=goto_timeout)
                        break
                    except Exception as e:
                        if "Target crashed" in str(e):
                            raise
                        if goto_attempt < 2:
                            await page.wait_for_timeout(3000)
                        else:
                            raise
                await page.wait_for_timeout(random.randint(2500, 4500))

                burst_start = time.time()
                while time.time() - start < duration_seconds:
                    # --- burst / idle 切换：模仿用户"看一看然后放下" ---
                    if time.time() - burst_start >= burst_seconds:
                        idle = random.uniform(idle_seconds * 0.7, idle_seconds * 1.3)
                        if progress_cb:
                            progress_cb(time.time() - start, scrolls,
                                        len(self.captured_posts))
                        await page.wait_for_timeout(int(idle * 1000))
                        burst_start = time.time()

                    # --- 随机滚动距离和方向 ---
                    dist_min = getattr(config, "SCROLL_DISTANCE_MIN", 2500)
                    dist_max = getattr(config, "SCROLL_DISTANCE_MAX", 5500)
                    scroll_amount = random.randint(dist_min, dist_max)
                    # 偶尔微小幅度的反向滚动（10% 概率），模拟用户回看
                    if random.random() < 0.08:
                        scroll_amount = -random.randint(200, 800)
                    await page.mouse.wheel(0, scroll_amount)
                    scrolls += 1

                    # --- 随机等待间隔 ---
                    pause_min = getattr(config, "SCROLL_PAUSE_MIN", 2.0)
                    pause_max = getattr(config, "SCROLL_PAUSE_MAX", 5.0)
                    pause = random.uniform(pause_min, pause_max)
                    await page.wait_for_timeout(int(pause * 1000))

                    # --- 偶尔移动鼠标到随机位置（15% 概率），模拟真实用户 ---
                    if random.random() < 0.15:
                        mx = random.randint(200, 1200)
                        my = random.randint(100, 700)
                        await page.mouse.move(mx, my)

                    # --- 随机间隔刷新页面 ---
                    if scrolls >= next_reset_at:
                        try:
                            await page.goto(SQUARE_URL, wait_until="domcontentloaded",
                                            timeout=getattr(config, "PAGE_GOTO_TIMEOUT", 90000))
                            await page.wait_for_timeout(random.randint(2000, 4000))
                        except Exception:
                            pass
                        next_reset_at = scrolls + random.randint(
                            getattr(config, "SCROLL_RESET_EVERY_MIN", 30),
                            getattr(config, "SCROLL_RESET_EVERY_MAX", 55),
                        )

                    if progress_cb:
                        progress_cb(time.time() - start, scrolls, len(self.captured_posts))
            except Exception as e:
                print(f"[scraper] 出错：{e}")
            finally:
                await context.close()

        # 去重
        seen = set()
        unique_posts = []
        for p in self.captured_posts:
            if p["post_id"] in seen:
                continue
            seen.add(p["post_id"])
            unique_posts.append(p)

        return unique_posts, self.captured_authors
