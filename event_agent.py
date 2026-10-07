"""加密事件驱动（独立进程）：抓金十快讯 → 识别币种 → AI 判利多/利空 → 纸盘开仓。

三个循环（同进程、各自线程）：

  ① 抓取  每 EVENT_POLL_SECONDS 拉一次 t.me/s/<channel>；只取 msg_id > 水位。
          **首轮只记水位、绝不回填** —— 否则第一次跑就把 8 天历史全灌进来，
          几百条 × 每条一次 AI，还会拿一堆过期事件去开仓。
  ② 分析  取 status='pending' 的**事件**（一条消息一次请求，不跨消息合并），
          并发池跑（EVENT_AI_MAX_WORKERS），回写判定。key 空 → 整体跳过。
  ③ 交易  取 ok + 利多/利空 + 未消费 + **新鲜**（EVENT_FRESH_MINUTES 内）
          → 纸盘开仓 → 记 traded。护栏见 trade_once。

日志走 stdout（manage_processes 会落到 logs/event_agent/YYYYMMDD.log）。
"""
import json
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import config
import event_coins
import event_scrape
import stock_coins
import storage

_lock = threading.Lock()

# 可交易集合（币安合约 symbol 去掉 USDT）：决定"英文 ticker 算不算币"
_TRADABLE: set = set()
_TRADABLE_TS = 0.0
_TRADABLE_TTL = 6 * 3600


def _log(msg: str) -> None:
    print(f"[event] {msg}", flush=True)


# ---------------------------------------------------------------- 可交易集合
def tradable_set() -> set:
    """币安合约可交易币种集合（大写 ticker）。失败返回上次的缓存（可能为空）。

    空集合不会放行英文 ticker（`event_coins.recognize` 的约定）：宁可只认中文，
    也不能把 `BGBTC`/`ETF` 这类当成币去开仓。"""
    global _TRADABLE, _TRADABLE_TS
    now = time.time()
    if _TRADABLE and now - _TRADABLE_TS < _TRADABLE_TTL:
        return _TRADABLE
    try:
        import exchange
        r = exchange._signed_request("GET", "/fapi/v1/exchangeInfo")
        syms = {s["symbol"][:-4] for s in (r or {}).get("symbols", [])
                if str(s.get("symbol", "")).endswith("USDT")
                and s.get("contractType") == "PERPETUAL"
                and s.get("status") == "TRADING"}
        if syms:
            _TRADABLE, _TRADABLE_TS = syms, now
            _log(f"可交易集合已更新：{len(syms)} 个币")
    except Exception as e:
        _log(f"拉币安合约列表失败（本轮沿用旧集合，{len(_TRADABLE)} 个）: {e}")
    return _TRADABLE


# ---------------------------------------------------------------- ① 抓取
def scrape_once() -> int:
    """拉最新一页 → 两条管线各写自己的库（加密库 / 美股商品独立库）。

    抓取只做一次（同一批消息），落库各自独立：美股那路失败不影响加密这路。
    返回**加密库**的新增条数（保持原返回值语义，测试/日志都按它）。"""
    msgs = event_scrape.parse_messages(event_scrape.fetch_page(config.EVENT_CHANNEL))
    if not msgs:
        return 0
    n = _ingest_crypto(msgs)
    try:
        _ingest_stock(msgs)
    except Exception as e:
        _log(f"[stock] 入库异常（不影响加密管线）: {type(e).__name__}: {e}")
    return n


def _ingest_crypto(msgs: list) -> int:
    """加密管线入库：只收到币种的消息；首轮只记水位、不回填。"""
    with storage.get_conn(config.EVENT_DB_PATH) as conn:
        wm = storage.event_meta_get(conn, "watermark_msg_id", "")
    watermark = int(wm) if wm.isdigit() else None
    newest = max(m["msg_id"] for m in msgs)
    if watermark is None:
        # 首轮：只记水位，不回填（设计约定，别改）
        with storage.get_conn(config.EVENT_DB_PATH) as conn:
            storage.event_meta_set(conn, "watermark_msg_id", newest)
            storage.event_meta_set(conn, "first_run_at", _now_iso())
        _log(f"首轮建立水位 msg_id={newest}（**不回填历史**）")
        return 0
    tradable = tradable_set()
    fresh = [m for m in msgs if m["msg_id"] > watermark]
    n = 0
    with storage.get_conn(config.EVENT_DB_PATH) as conn:
        for m in fresh:
            coins = event_coins.recognize(m["text"], tradable)
            if not coins:
                # **只收录明确指向某个币种的消息**（用户要求）：没有币种的直接不落库。
                # 水位照常推进 → 不会每轮重复扫同一条。
                # 留一行日志：定期 review 能看出**该补哪些中文别名**（不然漏了也不知道）
                _log(f"未识别到币种，跳过：{m['text'][:60]}")
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO crypto_event_raw (msg_id, ts, text, coins) "
                "VALUES (?,?,?,?)",
                (m["msg_id"], m["ts"], m["text"],
                 json.dumps(coins, ensure_ascii=False)))
            if cur.rowcount:
                for c in coins:
                    conn.execute("INSERT OR IGNORE INTO crypto_event_ai "
                                 "(event_id, coin, status) VALUES (?,?, 'pending')",
                                 (cur.lastrowid, c))
                n += 1
        storage.event_meta_set(conn, "watermark_msg_id", newest)
        storage.event_meta_set(conn, "last_poll_at", _now_iso())
    if n:
        _log(f"抓取：新增 {n} 条（水位 → {newest}）")
    return n


def _ingest_stock(msgs: list) -> int:
    """美股/商品管线入库（**独立库、独立水位**）：只收识别出标的的消息。

    未识别的**不逐条打日志**（93% 的消息都不含美股/商品，会刷屏）——
    改成每轮一行汇总 + 把最后 5 条样本存进 meta，页面/日志能据此补别名。
    """
    cfg = st_settings()
    with storage.get_conn(config.STOCK_EVENT_DB_PATH) as conn:
        wm = storage.stock_event_meta_get(conn, "watermark_msg_id", "")
    watermark = int(wm) if wm.isdigit() else None
    newest = max(m["msg_id"] for m in msgs)
    if watermark is None:
        with storage.get_conn(config.STOCK_EVENT_DB_PATH) as conn:
            storage.stock_event_meta_set(conn, "watermark_msg_id", newest)
            storage.stock_event_meta_set(conn, "first_run_at", _now_iso())
        _log(f"[stock] 首轮建立水位 msg_id={newest}（**不回填历史**）")
        return 0
    fresh = [m for m in msgs if m["msg_id"] > watermark]
    if not fresh:
        return 0
    n = 0
    unmatched: list = []
    with storage.get_conn(config.STOCK_EVENT_DB_PATH) as conn:
        for m in fresh:
            try:
                tickers = stock_coins.identify(m["text"], cfg["max_tickers"])
            except Exception as e:
                # 单条消息的识别异常不能卡住整条管线：水位要照常推进，
                # 否则这一条每轮重试 → 之后的快讯永远进不来（边界数据必须防）
                _log(f"[stock] 识别异常（跳过该条）: {type(e).__name__}: {e}")
                tickers = []
            if not tickers:
                unmatched.append(m["text"][:120])
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO stock_event_raw (msg_id, ts, text, tickers) "
                "VALUES (?,?,?,?)",
                (m["msg_id"], m["ts"], m["text"],
                 json.dumps(tickers, ensure_ascii=False)))
            if cur.rowcount:
                for t in tickers:
                    conn.execute("INSERT OR IGNORE INTO stock_event_ai "
                                 "(event_id, ticker, status) VALUES (?,?, 'pending')",
                                 (cur.lastrowid, t))
                n += 1
        storage.stock_event_meta_set(conn, "watermark_msg_id", newest)
        storage.stock_event_meta_set(conn, "last_poll_at", _now_iso())
        if unmatched:
            storage.stock_event_meta_set(
                conn, "unmatched_samples",
                json.dumps(unmatched[-5:], ensure_ascii=False))
    _log(f"[stock] 本轮 {len(fresh)} 条新消息，识别到 {n} 条（未识别 {len(unmatched)}）")
    return n


# ---------------------------------------------------------------- ② AI 分析
_SYS = (
    "你是加密事件判别器。对给定事件涉及的每个币种，判断它对**该币种价格**的影响，"
    "只能取四类之一：\n"
    "  利多 —— 明确利好的消息（机构采用、监管放行、大额买入、生态重大升级等）\n"
    "  利空 —— 明确利空的消息（被调查、被盗、大额转入交易所、解锁抛压等）\n"
    "  中性 —— 有信息量但对方向没有明确含义（数据描述、行情复盘、观点）\n"
    "  无关 —— 与价格无关（交易所活动/空投/理财、上新下架公告、榜单、广告、科普）\n"
    "只输出一行 JSON 数组，每项 {\"coin\":\"币种\",\"impact\":\"利多|利空|中性|无关\","
    "\"reason\":\"不超过30字的依据\",\"confidence\":0-100}，不要任何多余文字。"
)


def _strip_fence(s: str) -> str:
    """去掉 ```json ... ``` 之类的代码块包裹（模型很爱加）。"""
    s = (s or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    return (m.group(1) if m else s).strip()


def parse_ai_reply(raw: str, keys: list, key_field: str = "coin") -> list:
    """AI 回复 → `[{<key_field>, impact, reason, confidence}]`。

    容错：只保留**在本次请求的标的列表里**、且 impact 合法的项（模型有时会多回
    一个没问它的，或把 impact 写成别的词）。解析不出来返回 []。
    key_field：加密管线用 "coin"，美股/商品管线用 "ticker"（同构，只是键名不同）。"""
    try:
        data = json.loads(_strip_fence(raw))
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    # 加密管线产出"利多"，美股/商品管线产出"利好"（同义）→ 解析层都收，
    # 各自的下单闸门再按自己那套过滤（crypto: 利多/利空；stock: 利好/利空）
    ok = {"利多", "利空", "利好", "中性", "无关"}
    want = {str(c).upper() for c in keys}
    out, seen = [], set()
    for it in data:
        if not isinstance(it, dict):
            continue
        key = str(it.get(key_field) or "").strip().upper()
        impact = str(it.get("impact") or "").strip()
        if key not in want or key in seen or impact not in ok:
            continue
        try:
            conf = int(float(it.get("confidence") or 0))
        except Exception:
            conf = 0
        seen.add(key)
        out.append({key_field: key, "impact": impact,
                    "reason": str(it.get("reason") or "")[:200],
                    "confidence": max(0, min(conf, 100))})
    return out


def _ai_chat(sys_prompt: str, user_content: str) -> str | None:
    """DeepSeek 通用调用（加密/美股两条管线共用）。返回正文；key 空/失败 → None。"""
    if not config.EVENT_AI_API_KEY:
        return None
    body = {
        "model": config.EVENT_AI_MODEL,
        "messages": [{"role": "system", "content": sys_prompt},
                     {"role": "user", "content": user_content}],
        # max_tokens 是"推理 + 正文"的**总预算**：deepseek 系是推理模型，开思考后
        # 光推理就能烧掉几百到上千（实测 reasoning_effort=max 用了 854），预算给小了
        # 正文直接返回空（实测 120 → content=None）。所以按是否思考分开给。
        "max_tokens": 4000 if getattr(config, "EVENT_AI_THINKING", False) else 800,
        "temperature": 0.2,
        "stream": False,
    }
    if getattr(config, "EVENT_AI_THINKING", False):
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = getattr(config, "EVENT_AI_REASONING_EFFORT", "high")
    url = config.EVENT_AI_API_BASE.rstrip("/") + "/chat/completions"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {config.EVENT_AI_API_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=config.EVENT_AI_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:200]
        _log(f"AI 调用失败 HTTP {e.code}: {detail}")
        return None
    except Exception as e:
        _log(f"AI 调用异常: {type(e).__name__}: {e}")
        return None


def ai_classify(text: str, coins: list) -> list | None:
    """一条消息一次请求（不跨消息合并）。返回判定列表；拿不到（key 空/失败）返回 None。"""
    raw = _ai_chat(_SYS, f"币种：{'、'.join(coins)}\n事件：{text}")
    if raw is None:
        return None
    got = parse_ai_reply(raw, coins)
    if not got:
        _log(f"AI 回复无法解析（前 120 字）：{raw[:120]!r}")
    return got


def analyze_once() -> int:
    """把 pending 的事件按**消息**分组，并发跑 AI，回写判定。返回处理的消息数。"""
    if not config.EVENT_AI_API_KEY:
        with storage.get_conn(config.EVENT_DB_PATH) as conn:
            storage.event_meta_set(conn, "ai_state", "未配置 key（分析暂停）")
        return 0
    with storage.get_conn(config.EVENT_DB_PATH) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT ai.id ai_id, ai.coin, raw.id event_id, raw.text "
            "FROM crypto_event_ai ai JOIN crypto_event_raw raw ON raw.id = ai.event_id "
            "WHERE ai.status='pending' ORDER BY ai.id LIMIT 40")]
    if not rows:
        return 0
    by_event: dict = {}
    for r in rows:
        by_event.setdefault(r["event_id"], []).append(r)

    def _one(ev: int, items: list) -> tuple:
        got = ai_classify(items[0]["text"], [i["coin"] for i in items])
        return ev, got

    done = 0
    fail = 0
    with ThreadPoolExecutor(
            max_workers=max(1, int(getattr(config, "EVENT_AI_MAX_WORKERS", 4)))) as pool:
        for ev, got in pool.map(lambda kv: _one(*kv), list(by_event.items())):
            with storage.get_conn(config.EVENT_DB_PATH) as conn:
                if got is None:
                    fail += 1
                    # 失败：次数 +1；超过 3 次标 failed（不无限重试烧配额）
                    conn.execute(
                        "UPDATE crypto_event_ai SET attempts=attempts+1, "
                        "status=CASE WHEN attempts+1 >= 3 THEN 'failed' ELSE 'pending' END "
                        "WHERE event_id=? AND status='pending'", (ev,))
                else:
                    done += 1
                    for it in got:
                        conn.execute(
                            "UPDATE crypto_event_ai SET impact=?, reason=?, confidence=?, "
                            "model=?, status='ok', attempts=attempts+1, analyzed_at=? "
                            "WHERE event_id=? AND coin=? AND status='pending'",
                            (it["impact"], it["reason"], it["confidence"],
                             config.EVENT_AI_MODEL, _now_iso(), ev, it["coin"]))
                    # 没被 AI 覆盖到的币种（模型漏答）也算这轮处理过，别卡着
                    conn.execute(
                        "UPDATE crypto_event_ai SET attempts=attempts+1, status='failed' "
                        "WHERE event_id=? AND status='pending'", (ev,))
                storage.event_meta_set(conn, "last_ai_at", _now_iso())
                state = (f"正常（本轮 {done} 条成功 / {fail} 条失败）" if not fail
                         else f"有失败：本轮 {done} 成功 / {fail} 失败")
                storage.event_meta_set(conn, "ai_state", state)
    if done or fail:
        _log(f"AI 分析：{done} 条成功 / {fail} 条失败")
    return done


# ---------------------------------------------------------------- ③ 交易

# ---------------- 设置项（走 trading_settings：设置页可改、热生效）----------------
_EV_DEFAULTS = {"min_conf": ("event_min_confidence", 70),
                "fresh": ("event_fresh_minutes", 10),
                "cool": ("event_cooldown_minutes", 30),
                "daily": ("event_daily_max", 12),
                "poll": ("event_poll_seconds", 60)}


def ev_settings() -> dict:
    """每轮读一次：设置页改完立即生效，不用重启（读不到就回退 config 默认）。"""
    try:
        with storage.get_conn() as conn:
            st = storage.trading_settings_get(conn)
    except Exception:
        st = {}
    out = {}
    for k, (key, dflt) in _EV_DEFAULTS.items():
        v = st.get(key, dflt)
        if v is None or v == "":
            v = dflt
        try:
            v = float(v)
            v = int(v) if float(v).is_integer() else v
        except (TypeError, ValueError):
            v = dflt
        out[k] = v
    out["enabled"] = bool(st.get("event_strategy_enabled", True))
    return out


def _fresh(ts: str, minutes: int) -> bool:
    """事件是否够新（超过就不开仓——新闻已经过期）。ts 是 ISO/UTC。"""
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
    except Exception:
        return False
    return datetime.now(timezone.utc) - t <= timedelta(minutes=minutes)


def trade_once() -> int:
    """消费 AI 判定 → 纸盘开仓。返回开仓笔数。

    护栏（少一条就会被垃圾事件刷单）：
      · 只看 利多/利空（中性/无关不开）
      · 置信度 ≥ EVENT_MIN_CONFIDENCE
      · 事件新鲜度 ≤ EVENT_FRESH_MINUTES
      · 同币**本策略**单仓（其它策略的仓不管——策略之间互相独立）
      · 同币冷却 EVENT_COOLDOWN_MINUTES
      · 每日上限 EVENT_DAILY_MAX
      · traded 标记：一条判定只消费一次"""
    import trade_logic
    from market import get_market_snapshot
    cfg = ev_settings()
    opened = 0
    with storage.get_conn(config.EVENT_DB_PATH) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT ai.id ai_id, ai.coin, ai.impact, ai.confidence, raw.id event_id, "
            "raw.ts, raw.text FROM crypto_event_ai ai "
            "JOIN crypto_event_raw raw ON raw.id = ai.event_id "
            "WHERE ai.status='ok' AND ai.traded=0 AND ai.impact IN ('利多','利空') "
            "AND ai.confidence >= ? ORDER BY ai.id", (cfg["min_conf"],))]
    for r in rows:
        coin, impact = r["coin"], r["impact"]
        def _skip(reason):
            with storage.get_conn(config.EVENT_DB_PATH) as conn:
                conn.execute("UPDATE crypto_event_ai SET traded=1 WHERE id=?", (r["ai_id"],))
                conn.execute("INSERT INTO crypto_event_trade (event_id, coin, side, ok, reason) "
                             "VALUES (?,?,?,0,?)", (r["event_id"], coin, impact, reason))
            _log(f"{coin} {impact} 跳过：{reason}")

        if not _fresh(r["ts"], cfg["fresh"]):
            _skip(f"事件过期（>{cfg['fresh']} 分钟）")
            continue
        side = "SHORT" if impact == "利空" else "LONG"
        with storage.get_conn() as main_conn:      # 主库：仓位/设置都在这
            settings = storage.trading_settings_get(main_conn)
            if not cfg["enabled"]:
                _skip("策略开关关闭")
                continue
            act = main_conn.execute(
                "SELECT id FROM trade_positions WHERE token=? AND strategy='crypto_event' "
                "AND status IN ('OPEN','PARTIAL') LIMIT 1", (coin,)).fetchone()
            if act:
                _skip("本策略已有该币活跃仓")
                continue
            cool = main_conn.execute(
                "SELECT closed_at FROM trade_positions WHERE token=? "
                "AND strategy='crypto_event' AND closed_at IS NOT NULL "
                "ORDER BY id DESC LIMIT 1", (coin,)).fetchone()
            if cool and cool["closed_at"]:
                try:
                    t = datetime.fromisoformat(
                        str(cool["closed_at"]).replace("Z", "+00:00"))
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) - t < timedelta(
                            minutes=cfg["cool"]):
                        _skip(f"冷却中（{cfg['cool']} 分钟内开过）")
                        continue
                except Exception:
                    pass
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            n_today = main_conn.execute(
                "SELECT COUNT(*) n FROM trade_positions WHERE strategy='crypto_event' "
                "AND date(created_at) = ?", (today,)).fetchone()["n"]
            if n_today >= cfg["daily"]:
                _skip(f"今日已达上限 {cfg['daily']} 笔")
                continue
        snap = get_market_snapshot(coin, heavy=False) if coin else None
        mark = float((snap or {}).get("mark_price") or 0)
        if mark <= 0:
            _skip("拿不到标记价（可能不是币安合约币）")
            continue
        candidate = {"token": coin, "passed": True, "tier": "half",
                     "has_active_position": False, "price": mark,
                     "market": {"snapshot": snap, "analysis": {}},
                     "realtime": {}, "direction": side.lower()}
        try:
            with storage.get_conn() as main_conn:
                settings = storage.trading_settings_get(main_conn)
                ok = trade_logic.open_paper_position(
                    main_conn, candidate, settings, strategy="crypto_event", side=side)
                pid = None
                if ok:
                    row = main_conn.execute(
                        "SELECT id FROM trade_positions WHERE token=? "
                        "AND strategy='crypto_event' ORDER BY id DESC LIMIT 1",
                        (coin,)).fetchone()
                    pid = row["id"] if row else None
            with storage.get_conn(config.EVENT_DB_PATH) as conn:
                conn.execute("UPDATE crypto_event_ai SET traded=1 WHERE id=?", (r["ai_id"],))
                conn.execute("INSERT INTO crypto_event_trade "
                             "(event_id, coin, position_id, side, ok, reason) "
                             "VALUES (?,?,?,?,?,?)",
                             (r["event_id"], coin, pid, side, 1 if ok else 0,
                              "" if ok else "风控/仓位计算拒绝"))
            if ok:
                opened += 1
                _log(f"事件开仓：{coin} {side}（{impact} conf={r['confidence']}）"
                     f"#{pid} 「{r['text'][:40]}」")
            else:
                _log(f"事件开仓被拒：{coin} {side}（风控/仓位计算）")
        except Exception as e:
            _log(f"事件开仓异常 {coin}: {type(e).__name__}: {e}")
    return opened


# ================================================================ 美股/商品事件
# 独立库（db/stock_event.db）+ 独立 AI prompt + 独立护栏；与加密管线共用抓取和 AI 配置。
# 标的 = 币安 TradFi 永续里的**美股个股 + 商品**（不含指数/ETF，用户 2026-10-02 定）。
_STOCK_SYS = (
    "你是美股/商品事件判别器。对给定事件涉及的每个标的（美股代码或商品代码），"
    "判断它对**该标的价格**的影响，只能取四类之一：\n"
    "  利好 —— 明确利好（业绩超预期、上调指引、回购/分红、并购、监管放行、评级上调等）\n"
    "  利空 —— 明确利空（业绩不及预期、下调指引、被调查/诉讼、评级下调、增发摊薄等）\n"
    "  中性 —— 有信息量但方向不明（数据描述、行情复盘、观点、未证实传闻）\n"
    "  无关 —— 与价格无关（人物言行不涉及公司经营、科普、只是顺带提到该公司）\n"
    "注意：① 人物（马斯克/黄仁勋等）的言行本身不算信号，除非直接涉及公司经营/财务；"
    "② 加密货币语境里顺带提到的公司（如交易所提币提到 Coinbase）通常判无关；"
    "③ \"据悉/传闻\"类消息置信度要低。\n"
    "只输出一行 JSON 数组，每项 {\"ticker\":\"代码\",\"impact\":\"利好|利空|中性|无关\","
    "\"reason\":\"不超过30字的依据\",\"confidence\":0-100}，不要任何多余文字。"
)


def stock_ai_classify(text: str, tickers: list) -> list | None:
    """美股/商品：一条消息一次请求。返回判定列表；key 空/失败 → None。"""
    raw = _ai_chat(_STOCK_SYS, f"标的：{'、'.join(tickers)}\n事件：{text}")
    if raw is None:
        return None
    got = parse_ai_reply(raw, tickers, key_field="ticker")
    if not got:
        _log(f"[stock] AI 回复无法解析（前 120 字）：{raw[:120]!r}")
    return got


def stock_analyze_once() -> int:
    """美股/商品：把 pending 按消息分组、并发跑 AI、回写判定。返回处理的消息数。"""
    if not config.EVENT_AI_API_KEY:
        with storage.get_conn(config.STOCK_EVENT_DB_PATH) as conn:
            storage.stock_event_meta_set(conn, "ai_state", "未配置 key（分析暂停）")
        return 0
    with storage.get_conn(config.STOCK_EVENT_DB_PATH) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT ai.id ai_id, ai.ticker, raw.id event_id, raw.text "
            "FROM stock_event_ai ai JOIN stock_event_raw raw ON raw.id = ai.event_id "
            "WHERE ai.status='pending' ORDER BY ai.id LIMIT 20")]
    if not rows:
        return 0
    by_event: dict = {}
    for r in rows:
        by_event.setdefault(r["event_id"], []).append(r)

    def _one(ev: int, items: list) -> tuple:
        got = stock_ai_classify(items[0]["text"], [i["ticker"] for i in items])
        return ev, got

    done = 0
    fail = 0
    with ThreadPoolExecutor(
            max_workers=max(1, int(getattr(config, "EVENT_AI_MAX_WORKERS", 4)))) as pool:
        for ev, got in pool.map(lambda kv: _one(*kv), list(by_event.items())):
            with storage.get_conn(config.STOCK_EVENT_DB_PATH) as conn:
                if got is None:
                    fail += 1
                    conn.execute(
                        "UPDATE stock_event_ai SET attempts=attempts+1, "
                        "status=CASE WHEN attempts+1 >= 3 THEN 'failed' ELSE 'pending' END "
                        "WHERE event_id=? AND status='pending'", (ev,))
                else:
                    done += 1
                    for it in got:
                        conn.execute(
                            "UPDATE stock_event_ai SET impact=?, reason=?, confidence=?, "
                            "model=?, status='ok', attempts=attempts+1, analyzed_at=? "
                            "WHERE event_id=? AND ticker=? AND status='pending'",
                            (it["impact"], it["reason"], it["confidence"],
                             config.EVENT_AI_MODEL, _now_iso(), ev, it["ticker"]))
                    # 模型漏答的标的也算处理过，别卡着
                    conn.execute(
                        "UPDATE stock_event_ai SET attempts=attempts+1, status='failed' "
                        "WHERE event_id=? AND status='pending'", (ev,))
                storage.stock_event_meta_set(conn, "last_ai_at", _now_iso())
                state = (f"正常（本轮 {done} 条成功 / {fail} 条失败）" if not fail
                         else f"有失败：本轮 {done} 成功 / {fail} 失败")
                storage.stock_event_meta_set(conn, "ai_state", state)
    if done or fail:
        _log(f"[stock] AI 分析：{done} 条成功 / {fail} 条失败")
    return done


_ST_DEFAULTS = {"min_conf": ("stock_event_min_confidence", 75),
                "fresh": ("stock_event_fresh_minutes", 15),
                "cool": ("stock_event_cooldown_minutes", 120),
                "daily": ("stock_event_daily_max", 6),
                "max_tickers": ("stock_event_max_tickers", 3)}


def st_settings() -> dict:
    """美股/商品事件：每轮读一次设置（设置页可改、热生效）。"""
    try:
        with storage.get_conn() as conn:
            st = storage.trading_settings_get(conn)
    except Exception:
        st = {}
    out = {}
    for k, (key, dflt) in _ST_DEFAULTS.items():
        v = st.get(key, dflt)
        if v is None or v == "":
            v = dflt
        try:
            v = float(v)
            v = int(v) if float(v).is_integer() else v
        except (TypeError, ValueError):
            v = dflt
        out[k] = v
    out["enabled"] = bool(st.get("stock_event_enabled", True))
    out["trade_enabled"] = bool(st.get("stock_event_trade_enabled", True))
    return out


def stock_trade_once() -> int:
    """美股/商品：消费 AI 判定 → 纸盘开仓（strategy='stock_event'）。返回开仓笔数。

    护栏与加密事件同构（少一条就会被垃圾事件刷单）：
      · 只看 利好/利空；置信度 ≥ stock_event_min_confidence；新鲜度 ≤ fresh
      · 同标的**本策略**单仓；同标的冷却 cool；每日上限 daily
      · traded 标记：一条判定只消费一次；总开关/交易开关都要开"""
    import trade_logic
    from market import get_market_snapshot, get_mark_price
    cfg = st_settings()
    if not cfg["trade_enabled"]:
        return 0
    opened = 0
    with storage.get_conn(config.STOCK_EVENT_DB_PATH) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT ai.id ai_id, ai.ticker, ai.impact, ai.confidence, raw.id event_id, "
            "raw.ts, raw.text FROM stock_event_ai ai "
            "JOIN stock_event_raw raw ON raw.id = ai.event_id "
            "WHERE ai.status='ok' AND ai.traded=0 "
            # 模型偶尔按加密管线的档位答"利多"（解析层已收）→ 下单也要认，不能静默丢信号
            "AND ai.impact IN ('利好','利空','利多') "
            "AND ai.confidence >= ? ORDER BY ai.id", (cfg["min_conf"],))]
    for r in rows:
        ticker, impact = r["ticker"], r["impact"]

        def _skip(reason):
            with storage.get_conn(config.STOCK_EVENT_DB_PATH) as conn:
                conn.execute("UPDATE stock_event_ai SET traded=1 WHERE id=?",
                             (r["ai_id"],))
                conn.execute("INSERT INTO stock_event_trade "
                             "(event_id, ticker, side, ok, reason) VALUES (?,?,?,0,?)",
                             (r["event_id"], ticker, impact, reason))
            _log(f"[stock] {ticker} {impact} 跳过：{reason}")

        if not _fresh(r["ts"], cfg["fresh"]):
            _skip(f"事件过期（>{cfg['fresh']} 分钟）")
            continue
        side = "SHORT" if impact == "利空" else "LONG"
        with storage.get_conn() as main_conn:
            settings = storage.trading_settings_get(main_conn)
            if not cfg["enabled"]:
                _skip("策略开关关闭")
                continue
            act = main_conn.execute(
                "SELECT id FROM trade_positions WHERE token=? AND strategy='stock_event' "
                "AND status IN ('OPEN','PARTIAL') LIMIT 1", (ticker,)).fetchone()
            if act:
                _skip("本策略已有该标的活跃仓")
                continue
            cool = main_conn.execute(
                "SELECT closed_at FROM trade_positions WHERE token=? "
                "AND strategy='stock_event' AND closed_at IS NOT NULL "
                "ORDER BY id DESC LIMIT 1", (ticker,)).fetchone()
            if cool and cool["closed_at"]:
                try:
                    t = datetime.fromisoformat(
                        str(cool["closed_at"]).replace("Z", "+00:00"))
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) - t < timedelta(minutes=cfg["cool"]):
                        _skip(f"冷却中（{cfg['cool']} 分钟内开过）")
                        continue
                except Exception:
                    pass
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            n_today = main_conn.execute(
                "SELECT COUNT(*) n FROM trade_positions WHERE strategy='stock_event' "
                "AND date(created_at) = ?", (today,)).fetchone()["n"]
            if n_today >= cfg["daily"]:
                _skip(f"今日已达上限 {cfg['daily']} 笔")
                continue
        snap = get_market_snapshot(ticker, heavy=False) if ticker else None
        mark = float((snap or {}).get("mark_price") or 0)
        if mark <= 0:
            # TradFi 币种在快照链路拿不到时用标记价兜底（premiumIndex 对它们有效）
            mark = float(get_mark_price(ticker) or 0)
        if mark <= 0:
            _skip("拿不到价格（币安无该 TradFi 永续？）")
            continue
        candidate = {"token": ticker, "passed": True, "tier": "half",
                     "has_active_position": False, "price": mark,
                     "market": {"snapshot": snap or {}, "analysis": {}},
                     "realtime": {}, "direction": side.lower()}
        try:
            with storage.get_conn() as main_conn:
                settings = storage.trading_settings_get(main_conn)
                ok = trade_logic.open_paper_position(
                    main_conn, candidate, settings, strategy="stock_event", side=side)
                pid = None
                if ok:
                    row = main_conn.execute(
                        "SELECT id FROM trade_positions WHERE token=? "
                        "AND strategy='stock_event' ORDER BY id DESC LIMIT 1",
                        (ticker,)).fetchone()
                    pid = row["id"] if row else None
            with storage.get_conn(config.STOCK_EVENT_DB_PATH) as conn:
                conn.execute("UPDATE stock_event_ai SET traded=1 WHERE id=?",
                             (r["ai_id"],))
                conn.execute("INSERT INTO stock_event_trade "
                             "(event_id, ticker, position_id, side, ok, reason) "
                             "VALUES (?,?,?,?,?,?)",
                             (r["event_id"], ticker, pid, side, 1 if ok else 0,
                              "" if ok else "风控/仓位计算拒绝"))
            if ok:
                opened += 1
                _log(f"[stock] 事件开仓：{ticker} {side}（{impact} "
                     f"conf={r['confidence']}）#{pid} 「{r['text'][:40]}」")
            else:
                _log(f"[stock] 事件开仓被拒：{ticker} {side}（风控/仓位计算）")
        except Exception as e:
            _log(f"[stock] 事件开仓异常 {ticker}: {type(e).__name__}: {e}")
    return opened


# ---------------------------------------------------------------- 主循环
def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _loop(name: str, fn, interval: float):
    while True:
        try:
            fn()
        except Exception as e:
            _log(f"{name} 循环异常: {type(e).__name__}: {e}")
        time.sleep(interval)


def _scrape_loop():
    """抓取循环：**每轮重新读间隔**（设置页改了不用重启），下限 10 秒防手滑填 0。"""
    while True:
        try:
            scrape_once()
        except Exception as e:
            _log(f"抓取循环异常: {type(e).__name__}: {e}")
        try:
            iv = float(ev_settings().get("poll") or config.EVENT_POLL_SECONDS)
        except Exception:
            iv = config.EVENT_POLL_SECONDS
        time.sleep(max(10.0, iv))


def main():
    storage.init_event_db()
    storage.init_stock_event_db()      # 美股/商品独立库（同进程、独立表）
    tradable_set()          # 启动先拿一次可交易集合（认英文 ticker 用）
    n = tradable_set()
    _log(f"启动：频道={config.EVENT_CHANNEL} 轮询={config.EVENT_POLL_SECONDS}s "
         f"模型={config.EVENT_AI_MODEL} key={'已配置' if config.EVENT_AI_API_KEY else '未配置'}"
         f" 可交易币={len(n)}")
    _log(f"[stock] 美股/商品事件已启用（独立库 {config.STOCK_EVENT_DB_PATH}，"
         f"标的=美股个股+商品，不含指数/ETF）")
    if not config.EVENT_AI_API_KEY:
        _log("⚠ EVENT_AI_API_KEY 为空 → 只抓取入库、不做 AI 分析（页面会提示）")
    threading.Thread(target=_scrape_loop, daemon=True).start()
    threading.Thread(target=_loop, args=("分析", analyze_once, 5), daemon=True).start()
    threading.Thread(target=_loop, args=("交易", trade_once, 10), daemon=True).start()
    # 美股/商品管线：独立线程（各自 try/except，互不拖累）
    threading.Thread(target=_loop, args=("股票分析", stock_analyze_once, 5),
                     daemon=True).start()
    threading.Thread(target=_loop, args=("股票交易", stock_trade_once, 10),
                     daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
