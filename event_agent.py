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
    """拉最新一页，只入库比水位新的；首轮只记水位。返回新入库条数。"""
    with storage.get_conn(config.EVENT_DB_PATH) as conn:
        wm = storage.event_meta_get(conn, "watermark_msg_id", "")
    watermark = int(wm) if wm.isdigit() else None
    msgs = event_scrape.parse_messages(event_scrape.fetch_page(config.EVENT_CHANNEL))
    if not msgs:
        return 0
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


def parse_ai_reply(raw: str, coins: list) -> list:
    """AI 回复 → `[{coin, impact, reason, confidence}]`。

    容错：只保留**在本次请求的币种列表里**、且 impact 合法的项（模型有时会多回
    一个没问它的币，或把 impact 写成别的词）。解析不出来返回 []。"""
    try:
        data = json.loads(_strip_fence(raw))
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    ok = {"利多", "利空", "中性", "无关"}
    want = {str(c).upper() for c in coins}
    out, seen = [], set()
    for it in data:
        if not isinstance(it, dict):
            continue
        coin = str(it.get("coin") or "").strip().upper()
        impact = str(it.get("impact") or "").strip()
        if coin not in want or coin in seen or impact not in ok:
            continue
        try:
            conf = int(float(it.get("confidence") or 0))
        except Exception:
            conf = 0
        seen.add(coin)
        out.append({"coin": coin, "impact": impact,
                    "reason": str(it.get("reason") or "")[:200],
                    "confidence": max(0, min(conf, 100))})
    return out


def ai_classify(text: str, coins: list) -> list | None:
    """一条消息一次请求（不跨消息合并）。返回判定列表；拿不到（key 空/失败）返回 None。"""
    if not config.EVENT_AI_API_KEY:
        return None
    body = {
        "model": config.EVENT_AI_MODEL,
        "messages": [{"role": "system", "content": _SYS},
                     {"role": "user",
                      "content": f"币种：{'、'.join(coins)}\n事件：{text}"}],
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
        raw = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        got = parse_ai_reply(raw, coins)
        if not got:
            _log(f"AI 回复无法解析（前 120 字）：{raw[:120]!r}")
        return got
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:200]
        _log(f"AI 调用失败 HTTP {e.code}: {detail}")
        return None
    except Exception as e:
        _log(f"AI 调用异常: {type(e).__name__}: {e}")
        return None


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
    tradable_set()          # 启动先拿一次可交易集合（认英文 ticker 用）
    n = tradable_set()
    _log(f"启动：频道={config.EVENT_CHANNEL} 轮询={config.EVENT_POLL_SECONDS}s "
         f"模型={config.EVENT_AI_MODEL} key={'已配置' if config.EVENT_AI_API_KEY else '未配置'}"
         f" 可交易币={len(n)}")
    if not config.EVENT_AI_API_KEY:
        _log("⚠ EVENT_AI_API_KEY 为空 → 只抓取入库、不做 AI 分析（页面会提示）")
    threading.Thread(target=_scrape_loop, daemon=True).start()
    threading.Thread(target=_loop, args=("分析", analyze_once, 5), daemon=True).start()
    threading.Thread(target=_loop, args=("交易", trade_once, 10), daemon=True).start()
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
