# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Binance Square social heat monitor + Agent-based crypto futures trading system. The system scrapes Binance Square posts, ranks tokens by social engagement, enriches with on-chain futures data (OI, funding rate, taker ratio, depth), and either auto-trades based on rules or passes candidates to an LLM Agent for discretionary decisions.

Deployment path: `/root/binance-monitor/bsm-agent/` on a Linux server with Japanese IP.

## Key architectural patterns

**Agent vs System decision boundary**: The Agent (via hermes SKILL files) makes discretionary "should I open" decisions based on raw market data and historical lessons. The system (`risk.py` + `trade_logic.py`) enforces hard entry blocks (chase-protection, funding caps), calculates position sizing, and manages TP/SL/trailing stops. The Agent never sets entry price or stop loss — those are system-computed from real-time prices.

**Data pipeline duality**: There are two data source modes controlled by `AGENT_DATA_SOURCE` in config:
- `"agent_candidates"`: web.py collector thread runs `_build_leaderboard_items()` + `build_trade_candidates_from_leaderboard(passed_only=True)` every 3s, stores passed tokens in `agent_candidates` table. Every N minutes, flushes and triggers Agent. Agent reads from time-based window.
- `"token_heat_history"`: worker computes heat ranks per round, triggers Agent every N rounds. Agent reads from `token_heat_history` + `market_snapshots` JOIN.

**Collector cache sharing**: Panel `/api/trading` and collector thread share `_cached("candidates_scan", TTL)`. Whoever wakes first computes, the other hits cache. TTL configured via `AGENT_COLLECT_CACHE_TTL`.

**Lesson system design**: Lessons use three-part patterns (情境+倾向+策略) — not hard thresholds. Review SKILL classifies each stop-loss as "逻辑研判错误" or "市场随机噪音". Noise entries have `rule_update=""` and are hidden from trading Agent by the extract script's `WHERE rule_update != ''` filter.

**Noise stop filtering in extract**: `extract_review_data.py` scores each sl_hit on 5 dimensions (OI momentum, OI acceleration, taker, loss %, depth). Score ≥5 and no single dimension ≤-2 → auto-marked reviewed=1, excluded from Agent review.

**Round-based vs time-based**: Worker runs 5-minute scrape rounds continuously. Each round computes heat scores, refreshes market snapshots for top tokens, and writes to `token_heat_history`. The collector (in `agent_candidates` mode) is time-driven, not round-driven.

## Critical files

- `web.py` — FastAPI server (~5000 lines, includes inline HTML/CSS/JS for ~10 pages). Settings page uses `trading_settings` DB table. Collector thread starts at module load.
- `worker.py` — Scraping main loop. After 300s scrape, bulk-inserts posts, computes heat, refreshes market snapshots. May trigger Agent in `token_heat_history` mode.
- `storage.py` — All SQLite DDL and CRUD. `trading_settings` is key-value with `allowed` whitelist for writes.
- `trade_logic.py` — Position lifecycle: `evaluate_candidate()`, `open_paper_position()`, `update_paper_positions()`, `_build_close_snap()`. Close snapshots use Chinese flat format matching open dimension_data.
- `risk.py` — `evaluate_entry_quality()` with 7 core conditions + hard blocks + tier decision. Currently long-only.
- `agent-trade/scripts/extract_market_data.py` — Agent's data entry point. Branches by `AGENT_DATA_SOURCE` config.
- `agent-review/scripts/extract_review_data.py` — Review data extractor with noise filter + orphan filter.
- `manage_processes.py` — Process manager. `restart` does SIGKILL + wait 180s + start. Called by `/api/system/restart`.
- `event_agent.py` — 事件驱动独立进程（三条管线：加密 `crypto_event` + 美股/商品 `stock_event` + 科技股动态 `tech_news`）。抓取一次写三个库；AI 走 DeepSeek（`EVENT_AI_*`）；只做纸盘（科技股动态不交易，纯新闻雷达）。
- `event_coins.py` / `stock_coins.py` — 两条事件管线的标的识别（币种别名 / 美股+商品 ticker 映射，后者必须在币安 TradFi 永续名单里；**不做指数/ETF**）。
- `tech_stocks.py` — 科技股动态的拆条 + 三层识别（公司含未上市AI档/高管名 → 行业词 → 科技综合兜底）；**不设可交易白名单**，护栏只控节奏不丢消息。

## Config parameters worth knowing

- `AGENT_DATA_SOURCE` — switches between collector mode and heat-history mode
- `AGENT_COLLECT_INTERVAL_MINUTES` — collector flush interval (also on settings page)
- `AGENT_TRIGGER_INTERVAL` — rounds between Agent triggers (heat_history mode)
- `TRADING_TP1_R=1.0, TP1_CLOSE_PCT=80` — backtest-optimized: lock 80% at +1R
- `TRADING_STOP_LOSS_MIN_PCT=-2.0` — floor that's working (only 1 trade in -2~-3% range)

## Run commands

```bash
# Start all processes
python manage_processes.py start

# Restart (SIGKILL + wait 180s)
python manage_processes.py restart

# Stop
python manage_processes.py stop

# Reset review state (mark all lessons learned=1, journal reviewed=1)
python reset_review.py

# Direct DB query
sqlite3 db/binance_square.db "SELECT * FROM trading_settings"
```

## Agent skill system

Skills live in `/root/.hermes/skills/agent-trade/` and `/root/.hermes/skills/agent-review/`. Each has `SKILL.md` (instructions), `references/` (field docs, trap patterns), `scripts/` (data extraction), `assets/` (JSON formats).

The trading SKILL's decision JSON must use envelope format `{"market_read": "...", "decisions": [...]}`. The review SKILL writes lessons as `{"lessons": [...], "deprecate_ids": [...]}`.

## Pages

- `/` — Market monitoring (worker status, heat leaderboard, trading positions, watchlist)
- `/agent` — Agent panel (account, timeline, lessons)
- `/agent-heat` — Heat Agent panel
- `/agent-heat-lessons` — Heat Agent with lessons panel
- `/agent-nl` — No-lessons Agent panel
- `/agent-kol` — KOL Agent monitor
- `/agent-kol-snapshot` — KOL cognitive snapshot monitor
- `/agent-short` — Short Agent panel
- `/crypto-events` — 加密事件展览（金十快讯里指向某个币种的消息）
- `/crypto-event-ai` — 加密事件 AI 判定（利多/利空 + 该判定开的纸盘仓）
- `/stock-events` — 美股/商品事件展览（指向美股个股/商品标的的消息；不含指数/ETF）
- `/stock-event-ai` — 美股/商品 AI 判定（利好/利空 + 纸盘仓）
- `/tech-news` — 科技股动态（金十消息拆条→三层识别→Google搜索+AI 图解；分类/股票卡/行业卡）
- `/strategies` — Multi-strategy overview
- `/dashboard` — Market panorama dashboard
- `/settings` — Trading parameters, Agent switches, intervals

## 项目记忆（所有会话共享）

本项目的历史决策、改版记录与协作偏好存放在 **`.claude/memory/`**（`MEMORY.md` 是索引）。

- **开始工作前先读 `.claude/memory/MEMORY.md`**，按索引展开相关的 `project_*` / `feedback_*` 文件；
- 学到新的项目决策或用户偏好时，写进对应文件并同步更新 `MEMORY.md` 索引（一条一行）；
- **记忆拓扑（2026-10-12 实测）**：宿主机上该目录是指向 CLI 自动记忆目录
  （`~/.claude/projects/…/memory/`）的**目录联接**。**E 盘官方版桌面客户端（E:\software\claude）
  与 CLI 共用同一套 `~/.claude`（记忆读写 / 会话记录 / 命令执行已三项实测通过）——同一份库，
  无需任何同步操作。** 仅 **3p 定制版**（MSIX）例外：它会话里的 `.claude/memory` 被应用映射到
  自己的「空间记忆库」，是**两个库**，需用 `extra\_sync_memory_desktop.bat` 双向同步（双击跑，
  "新的覆盖旧的"）；在 3p 版写完记忆后跑一次该脚本，CLI 侧才可见；反向同理。
