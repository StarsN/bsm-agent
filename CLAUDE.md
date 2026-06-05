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

- `web.py` — FastAPI server (~3000 lines, includes inline HTML/CSS/JS for three pages: `/`, `/agent`, `/settings`). Settings page uses `trading_settings` DB table. Collector thread starts at module load.
- `worker.py` — Scraping main loop. After 300s scrape, bulk-inserts posts, computes heat, refreshes market snapshots. May trigger Agent in `token_heat_history` mode.
- `storage.py` — All SQLite DDL and CRUD. `trading_settings` is key-value with `allowed` whitelist for writes.
- `trade_logic.py` — Position lifecycle: `evaluate_candidate()`, `open_paper_position()`, `update_paper_positions()`, `_build_close_snap()`. Close snapshots use Chinese flat format matching open dimension_data.
- `risk.py` — `evaluate_entry_quality()` with 7 core conditions + hard blocks + tier decision. Currently long-only.
- `agent-trade/scripts/extract_market_data.py` — Agent's data entry point. Branches by `AGENT_DATA_SOURCE` config.
- `agent-review/scripts/extract_review_data.py` — Review data extractor with noise filter + orphan filter.
- `manage_processes.py` — Process manager. `restart` does SIGKILL + wait 180s + start. Called by `/api/system/restart`.

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
- `/settings` — Trading parameters, Agent data source, save/reset/restart buttons
