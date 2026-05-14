"""
后台工作进程：
- 5 分钟持续抓取广场
- 抓完一轮后计算榜单
- 给榜单上有合约的代币 + 观察列表的代币 刷新行情快照，写入数据库
- Web 进程读数据库展示

运行：python worker.py
"""
import asyncio
import json
import signal
import sys
import time
from datetime import datetime, timezone

from rich.console import Console

import config
import storage
from scraper import SquareScraper
from analyzer import extract_tokens_from_text, compute_short_scores, compute_composite_scores
from filters import is_likely_human, post_passes_quality
from market import has_perpetual, get_market_snapshot, get_futures_symbols
from signals import analyze as analyze_signals
from trade_logic import evaluate_candidate


def _loads(raw, default=None):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


console = Console()
_running = True


def stop(*_):
    global _running
    _running = False
    console.print("\n[yellow]收到退出信号，抓完当前轮后停止...[/yellow]")


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)


def _utcnow():
    return datetime.now(timezone.utc)


def refresh_market_snapshots(tokens_to_check: list[str], watchlist: list[str] = None,
                            heavy: bool = True):
    """对给定代币列表，拉取行情 + 综合打分 + 存数据库
    如果代币在 watchlist 里，还会追加 followup 记录 + 更新浮亏极值 + 检查是否触发归档

    性能优化（v2）：
      - 每个 token 用独立的短事务，不持有长时间写锁
      - 网络请求发生在事务外，不阻塞 web 端读取
      - 失败重试不阻塞整个流程
    """
    if not tokens_to_check:
        return 0
    try:
        futures_set = get_futures_symbols()
    except Exception as e:
        console.print(f"[red]   获取合约列表失败: {e}[/red]")
        return 0

    watch_set = set(t.upper() for t in (watchlist or []))
    updated = 0
    archived_tokens = []

    # Step 1：一次性读好需要的基础数据（短事务）
    with storage.get_conn() as conn:
        short_scores = compute_short_scores(conn)
        social_map = {s["token"]: s["score"] for s in short_scores}

    # Step 2：逐个 token 处理，网络请求在事务外，写库用独立小事务
    for token in tokens_to_check:
        up = token.upper()
        if up not in futures_set:
            continue

        # === 网络请求（事务外，不占 DB 锁）===
        try:
            snap = get_market_snapshot(token, heavy=heavy)
        except Exception as e:
            console.print(f"   [dim][red]{token} 抓取失败: {e}[/red][/dim]")
            continue
        if not snap:
            continue

        social_score = social_map.get(token, 0.0)
        try:
            analysis = analyze_signals(snap, social_score)
        except Exception as e:
            console.print(f"   [dim][red]{token} 分析失败: {e}[/red][/dim]")
            continue

        snap_json = json.dumps(snap, default=str, ensure_ascii=False)
        ana_json = json.dumps(analysis, default=str, ensure_ascii=False)

        # === 写入（短事务，就这个 token 的几条数据）===
        try:
            with storage.get_conn() as conn:
                storage.snapshot_upsert(conn, token, snap_json, ana_json)

                # 收藏代币的额外处理
                if up in watch_set:
                    entry = storage.entry_get(conn, up)
                    if entry is None:
                        price = snap.get("mark_price") or 0
                        if price > 0:
                            storage.entry_upsert(conn, up, price, snap_json, ana_json)
                    else:
                        cur_price = snap.get("mark_price") or 0
                        anchor = entry.get("anchor_price") or 0
                        if cur_price > 0 and anchor > 0:
                            pnl = (cur_price - anchor) / anchor * 100
                            storage.followup_add(conn, up, cur_price, pnl, snap_json, ana_json)
                            storage.entry_update_extremes(conn, up, pnl)

                            if (pnl <= config.LOSS_ARCHIVE_THRESHOLD_PCT
                                    and not entry.get("archived")):
                                storage.archive_loss_sample(conn, up, cur_price, pnl)
                                archived_tokens.append((up, pnl))
            updated += 1
        except Exception as e:
            console.print(f"   [dim][red]{token} 入库失败: {e}[/red][/dim]")
            continue

        # 节流（事务外，web 读取不受影响）
        time.sleep(0.4)

    if archived_tokens:
        for t, pnl in archived_tokens:
            console.print(f"   [yellow]⚠ {t} 浮亏 {pnl:.1f}% 已归档为学习样本[/yellow]")

    return updated


def _write_status(**fields):
    """辅助函数：快速写一次 worker 状态"""
    try:
        with storage.get_conn() as conn:
            storage.status_update(conn, **fields)
    except Exception:
        pass


_ROUND_NUMBER = 0


async def one_round(scraper: SquareScraper):
    """一轮：持续抓取 + 存库 + 刷新榜单和观察列表的合约数据"""
    global _ROUND_NUMBER
    _ROUND_NUMBER += 1
    round_start = time.time()
    console.print(f"[blue]=> 开始抓取轮 #{_ROUND_NUMBER}... {datetime.now():%H:%M:%S} "
                  f"（持续 {config.SCRAPE_ROUND_SECONDS}s）[/blue]")

    _write_status(
        stage="scraping",
        detail=f"正在抓取广场帖子（0s / {config.SCRAPE_ROUND_SECONDS}s）",
        round_start=datetime.now().isoformat(timespec="seconds"),
        round_number=_ROUND_NUMBER,
        posts_this_round=0,
        saved_this_round=0,
    )

    def progress(elapsed, scrolls, posts_so_far):
        # 实时写状态到数据库，web 读取就能显示
        _write_status(
            stage="scraping",
            detail=f"抓取中 {int(elapsed)}s / {config.SCRAPE_ROUND_SECONDS}s，"
                   f"滚动 {scrolls} 次，累计 {posts_so_far} 条",
            posts_this_round=posts_so_far,
        )
        if scrolls % 10 == 0:
            console.print(f"   [dim]...{int(elapsed)}s / {config.SCRAPE_ROUND_SECONDS}s, "
                          f"滚动 {scrolls} 次，累计 {posts_so_far} 条[/dim]")

    # streaming 模式下，抓取过程中收集出现过的 token
    _round_tokens_seen = set()  # streaming 模式用
    _candidate_mode = getattr(config, "AGENT_CANDIDATE_MODE", "batch")

    def progress_with_candidates(elapsed, scrolls, posts_so_far):
        progress(elapsed, scrolls, posts_so_far)
        # streaming 模式：每 5 次滚动，从内存中的帖子提取 token
        if _candidate_mode == "streaming" and scrolls % 5 == 0:
            from filters import extract_tokens_from_text
            excluded = config.EXCLUDED_TOKENS or set()
            for post in scraper.captured_posts:
                tokens = post.get("tokens") or set()
                if not tokens:
                    tokens = extract_tokens_from_text(post.get("content", ""))
                for t in tokens:
                    if t not in excluded:
                        _round_tokens_seen.add(t.upper())

    posts, authors = await scraper.scrape_continuous(
        duration_seconds=config.SCRAPE_ROUND_SECONDS,
        progress_cb=progress_with_candidates,
    )
    console.print(f"   本轮捕获 {len(posts)} 条帖子，{len(authors)} 个作者")

    _write_status(
        stage="saving",
        detail=f"处理帖子入库... 捕获 {len(posts)} 条",
        posts_this_round=len(posts),
    )

    # ============================================================
    # 事务 1：作者 + 帖子入库（短，就入数据）
    # ============================================================
    with storage.get_conn() as conn:
        human_count = 0
        for user_id, a in authors.items():
            a["is_human"] = 1 if is_likely_human(a) else 0
            a["post_count_24h"] = 0
            a["last_seen"] = _utcnow()
            storage.upsert_author(conn, a)
            if a["is_human"]:
                human_count += 1

        saved = 0
        saved_by_kol = 0
        saved_by_engagement = 0
        excluded = config.EXCLUDED_TOKENS or set()
        for post in posts:
            author = authors.get(post["user_id"])
            if not author:
                continue
            if not post_passes_quality(post, author):
                continue

            from filters import is_verified_kol
            if is_verified_kol(author):
                saved_by_kol += 1
            else:
                saved_by_engagement += 1

            tokens = post.get("tokens") or set()
            if not tokens:
                tokens = extract_tokens_from_text(post.get("content", ""))
            if config.TRACKED_TOKENS:
                tokens = {t for t in tokens if t in config.TRACKED_TOKENS}
            tokens = {t for t in tokens if t not in excluded}

            post_for_db = {k: v for k, v in post.items() if k != "tokens"}
            storage.upsert_post(conn, post_for_db)
            if tokens:
                storage.insert_mentions(conn, post["post_id"], tokens)
            saved += 1
    # 事务 1 结束 —— 释放写锁，web 端此时可以自由读取

    # ============================================================
    # 事务 2：清理旧数据（只在每 N 轮跑，不是每轮）
    # ============================================================
    if _ROUND_NUMBER % 20 == 0:
        try:
            with storage.get_conn() as conn:
                storage.purge_old(conn, days=7)
            console.print(f"   [dim]已清理 7 天前的老帖子[/dim]")
        except Exception as e:
            console.print(f"   [dim]purge 失败: {e}[/dim]")

    # ============================================================
    # 事务 3：统计计数（只读，短）
    # ============================================================
    with storage.get_conn() as conn:
        humans = conn.execute("SELECT COUNT(*) FROM authors WHERE is_human=1").fetchone()[0]
        total_posts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        total_authors = conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0]
    console.print(
        f"   本轮入库 {saved} 条（大V {saved_by_kol} + 高互动 {saved_by_engagement}）"
    )
    console.print(f"   [dim]累计：大V {humans}, 帖子总数 {total_posts}[/dim]")

    # ============================================================
    # 事务 4：状态更新（短）
    # ============================================================
    with storage.get_conn() as conn:
        storage.status_update(
            conn,
            saved_this_round=saved,
            total_posts=total_posts,
            total_authors=total_authors,
        )

    # ============================================================
    # 事务 5：算热度榜 + 写历史（读多写少）
    # ============================================================
    with storage.get_conn() as conn:
        short_scores = compute_short_scores(conn)
        if short_scores:
            storage.heat_history_add(conn, _ROUND_NUMBER, short_scores)

        # === Agent 候选币收集 ===
        console.print(f"   候选币池: {len(short_scores)} 个 token")

        # 观察列表跟踪数据每 50 轮清一次（保留 3 天）
        if _ROUND_NUMBER % 50 == 0:
            storage.watchlist_followups_purge_old(conn, days=3)

        # 热度历史清理改为每 100 轮（约 8 小时）
        if _ROUND_NUMBER % 100 == 0:
            storage.heat_history_purge_old(conn, keep_last_rounds=200)
        watchlist = storage.watchlist_get_all(conn)

    console.print(f"   15 分钟榜代币数: {len(short_scores)}")
    for i, s in enumerate(short_scores[:10], 1):
        console.print(f"     {i}. {s['token']}  热度={s['score']:.1f}  帖子={s['unique_posts']}")

    # ============================================================
    # HTTP 密集型：拉合约快照
    # 这部分可能要 30-60 秒（30 个代币 × 多个端点），必须在事务外。
    # refresh_market_snapshots 内部自己管连接。
    # ============================================================
    if config.ENABLE_MARKET_ANALYSIS:
        top_tokens = [s["token"] for s in short_scores[:config.MARKET_ANALYSIS_MAX]]
        combined = list(dict.fromkeys(top_tokens + watchlist))
        _write_status(
            stage="market",
            detail=f"查询合约数据（榜单 {len(top_tokens)} + 观察 {len(watchlist)} = {len(combined)} 代币）",
        )
        console.print(f"[blue]=> 刷新合约数据（榜单 {len(top_tokens)} + 观察列表 {len(watchlist)} = {len(combined)} 去重）...[/blue]")
        heavy_this_round = (_ROUND_NUMBER % config.MARKET_HEAVY_INTERVAL_ROUNDS == 0
                            or _ROUND_NUMBER == 1)
        updated = refresh_market_snapshots(combined, watchlist=watchlist,
                                           heavy=heavy_this_round)
        console.print(f"   已更新 {updated} 个代币的合约快照"
                      + (" (全量)" if heavy_this_round else " (轻量)"))

        # === 构建 Agent 候选币池（面板"合约扫描与操作建议"同源）===
        try:
            with storage.get_conn() as conn:
                scored = compute_composite_scores(conn, short_scores,
                                                  config.COMPOSITE_HISTORY_WINDOW)
                watchlist_set = set(watchlist)
                leaderboard = []
                for s in scored:
                    snap_row = storage.snapshot_get(conn, s["token"])
                    if not snap_row:
                        continue
                    snap_data = _loads(snap_row.get("snapshot"), {})
                    if not snap_data.get("mark_price"):
                        continue
                    rt_row = storage.realtime_get(conn, s["token"])
                    realtime = _loads(rt_row.get("snapshot"), {}) if rt_row else {}
                    market = {
                        "snapshot": snap_data,
                        "analysis": _loads(snap_row.get("analysis"), {}),
                        "updated_at": snap_row.get("updated_at"),
                    }
                    verdict = market["analysis"].get("verdict", "")
                    leaderboard.append((s, market, realtime, verdict))

                VERDICT_ORDER = {
                    "✅ 看起来健康": 0, "🎯 值得留意": 1, "⚠️ 过热预警": 2,
                    "📉 信号偏弱": 3, "⚪ 中性": 4, "数据不足": 5,
                }
                leaderboard.sort(key=lambda x: (
                    VERDICT_ORDER.get(x[3], 99),
                    -(x[0].get("composite_score") or 0),
                    -(x[0].get("score") or 0),
                ))

                signal_key = storage.leaderboard_signal_key(conn)
                items = []
                for rank, (score_row, market, realtime, _) in enumerate(leaderboard, 1):
                    result = evaluate_candidate(score_row, rank, market, realtime)
                    if not result.get("passed"):
                        continue  # 只存 passed 的，与面板"合约扫描与操作建议"对齐
                    snap = market["snapshot"]
                    analysis = market.get("analysis") or {}
                    data_blob = {
                        "token": score_row["token"],
                        "social_score": score_row.get("score", 0),
                        "mentions": score_row.get("mentions", 0),
                        "price": snap.get("mark_price"),
                        "15m": snap.get("change_15m_pct"),
                        "1h": snap.get("change_1h_pct"),
                        "4h": snap.get("change_4h_pct"),
                        "24h": snap.get("change_24h_pct"),
                        "oi_15m": snap.get("oi_change_15m_pct"),
                        "oi_1h": snap.get("oi_change_1h_pct"),
                        "oi_4h": snap.get("oi_change_4h_pct"),
                        "oi_48h": snap.get("oi_change_48h_pct"),
                        "funding": snap.get("funding_rate_pct"),
                        "lsr": snap.get("long_short_ratio"),
                        "top_lsr": snap.get("top_trader_ls_ratio"),
                        "taker": snap.get("taker_buy_sell_ratio"),
                        "taker_pct": snap.get("taker_buy_pct"),
                        "taker_trend": snap.get("taker_trend_pct"),
                        "spread": snap.get("bid_ask_spread_pct"),
                        "depth_bid": snap.get("depth_bid_1pct_usd"),
                        "depth_ask": snap.get("depth_ask_1pct_usd"),
                        "imbalance": snap.get("depth_imbalance_pct"),
                        "vol_24h": snap.get("volume_24h_usd"),
                        "oi_usd": snap.get("oi_usd"),
                        "chg_48h": snap.get("change_48h_pct"),
                        "oi_divergence": analysis.get("oi_divergence"),
                        "verdict": analysis.get("verdict"),
                        "direction": analysis.get("direction"),
                        "tags": analysis.get("tags", []),
                        "notes": analysis.get("notes", []),
                        "age": (
                            None if not snap.get("klines_15m_count") else
                            ">1d" if snap["klines_15m_count"] >= 96 else
                            f"{snap['klines_15m_count'] * 15 // 60 // 24}d"
                            f"{snap['klines_15m_count'] * 15 // 60 % 24}h"
                        ),
                        "realtime": realtime,
                        "tier": result["tier"],
                        "passed": result["passed"],
                        "hard_block": result["hard_block"],
                        "pass_count": result["pass_count"],
                        "suggestion": result["suggestion"],
                        "reasons": result["reasons"],
                    }
                    items.append({
                        "token": score_row["token"],
                        "data": json.dumps(data_blob, default=str, ensure_ascii=False),
                        "tier": result["tier"],
                        "passed": 1 if result["passed"] else 0,
                        "hard_blocks": json.dumps(result["hard_block"], ensure_ascii=False),
                        "pass_count": result["pass_count"],
                        "signal_key": signal_key,
                    })

                storage.agent_candidates_insert_batch(conn, _ROUND_NUMBER, items)
                console.print(f"   候选币池已入库: {len(items)} 个")

            # 清理旧数据
            if _ROUND_NUMBER % 20 == 0:
                with storage.get_conn() as conn:
                    storage.agent_candidates_purge_old(conn, keep_last_rounds=20)
        except Exception as e:
            console.print(f"   [yellow]候选币池构建失败: {e}[/yellow]")

    elapsed = time.time() - round_start
    console.print(f"[green]本轮 #{_ROUND_NUMBER} 总耗时 {elapsed:.0f}s[/green]\n")

    # 触发 Agent 交易决策（有候选币数据才触发，间隔从 DB 设置读取）
    if getattr(config, "AGENT_AUTO_TRIGGER", False):
        agent_interval = getattr(config, "AGENT_TRIGGER_INTERVAL", 3)
        candidate_count = 0
        try:
            with storage.get_conn() as conn:
                settings = storage.trading_settings_get(conn)
                agent_interval = int(settings.get("agent_trigger_interval", agent_interval))
            if _ROUND_NUMBER % agent_interval != 0:
                candidate_count = -1  # 未到触发轮次
        except Exception:
            pass
        if candidate_count == -1:
            pass  # 未到轮次，不触发
        elif candidate_count == 0:
            # 到轮次但需检查是否有候选数据
            try:
                with storage.get_conn() as conn:
                    lookback = agent_interval - 1
                    row = conn.execute(
                        "SELECT COUNT(*) AS cnt FROM agent_candidates a "
                        "INNER JOIN ("
                        "  SELECT token, MAX(id) AS max_id FROM agent_candidates "
                        f"  WHERE round_number >= (SELECT COALESCE(MAX(round_number), 0) - {lookback} FROM agent_candidates) "
                        "  AND passed = 1"
                        "  GROUP BY token"
                        ") b ON a.id = b.max_id"
                    ).fetchone()
                    candidate_count = row["cnt"] if row else 0
            except Exception:
                pass
        if candidate_count is not None and candidate_count > 0:
            try:
                import subprocess
                job_id = getattr(config, "AGENT_HERMES_JOB_ID", "")
                subprocess.run(
                    ["hermes", "cron", "run", job_id],
                    timeout=300, capture_output=True, text=True,
                )
                console.print(f"[dim]已触发 Agent (round {_ROUND_NUMBER}, {candidate_count} 个候选)[/dim]")
            except Exception as e:
                console.print(f"[dim]Agent 触发失败: {e}[/dim]")

    _write_status(
        stage="idle",
        detail=f"本轮完成（入库 {saved} 条 · 耗时 {elapsed:.0f}s）· 即将开始下一轮",
    )


def _resume_round_number():
    """重启时从 token_heat_history 最大轮次续上"""
    global _ROUND_NUMBER
    try:
        with storage.get_conn() as conn:
            r = conn.execute(
                "SELECT MAX(round_number) FROM token_heat_history"
            ).fetchone()
            if r and r[0]:
                _ROUND_NUMBER = r[0]
    except Exception:
        pass


async def main():
    storage.init_db()
    scraper = SquareScraper()
    _resume_round_number()

    console.print("[green]=== 币安广场监控 Worker 启动 ===[/green]")
    console.print(f"   续上次轮次：从第 {_ROUND_NUMBER + 1} 轮开始")
    console.print(f"   每轮持续抓取：{config.SCRAPE_ROUND_SECONDS}s")
    console.print(f"   15 分钟榜单窗口：{config.SHORT_WINDOW_MINUTES} 分钟")
    console.print(f"   粉丝阈值：{config.MIN_FOLLOWERS}")
    console.print(f"   Web 仪表盘：请另开一个终端运行 python web.py")
    console.print()

    while _running:
        try:
            round_timeout = config.SCRAPE_ROUND_SECONDS + 120
            await asyncio.wait_for(one_round(scraper), timeout=round_timeout)
        except asyncio.TimeoutError:
            console.print(f"[red]本轮超时（>{round_timeout}s），跳过[/red]")
        except Exception as e:
            console.print(f"[red]本轮出错：{e}[/red]")
            import traceback
            traceback.print_exc()
            # 出错等 30 秒再试
            for _ in range(30):
                if not _running:
                    break
                await asyncio.sleep(1)

    console.print("[green]已退出[/green]")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
