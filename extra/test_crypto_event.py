"""加密事件地基测试：币种识别 / 预览页解析（真样本夹具）/ 事件 DB。

夹具是**从 https://t.me/s/jin10light 实抓裁剪**的（extra/fixtures/jin10_sample.html），
按"外部 payload 夹具必须照真样本造"的规矩来 —— 自己手搓的 HTML 会骗过解析器。
"""
import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)

import config

_TMP = tempfile.mkdtemp(prefix="cryptoev_")
config.EVENT_DB_PATH = os.path.join(_TMP, "event.db")
config.DB_PATH = os.path.join(_TMP, "main.db")

import event_coins  # noqa: E402
import event_scrape  # noqa: E402
import storage  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


TRADABLE = {"BTC", "ETH", "UNI", "ONDO", "BCH", "SOL", "DOGE", "NEAR", "PEPE"}

print("== 1. 币种识别：中文别名 ==")
check("中文「比特币」→ BTC",
      event_coins.recognize("昨日比特币空单大幅清算后，合约交易仍拥挤", TRADABLE) == ["BTC"])
check("中文长词优先：「比特现金」→ BCH，不是 BTC",
      event_coins.recognize("比特现金短线拉升", TRADABLE) == ["BCH"],
      str(event_coins.recognize("比特现金短线拉升", TRADABLE)))
check("中文「狗狗币」→ DOGE",
      event_coins.recognize("狗狗币涨超 10%", TRADABLE) == ["DOGE"])
check("不认通名：稳定币/山寨币不是币种",
      event_coins.recognize("稳定币市值创新高，山寨币普涨", TRADABLE) == [],
      str(event_coins.recognize("稳定币市值创新高，山寨币普涨", TRADABLE)))

print("\n== 2. 币种识别：英文 ticker（必须过可交易集合）==")
check("独立大写 ticker：UNI 认出来",
      event_coins.recognize("锁仓BGBTC解锁2400枚UNI空投", TRADABLE) == ["UNI"],
      str(event_coins.recognize("锁仓BGBTC解锁2400枚UNI空投", TRADABLE)))
check("假 token（BGBTC 不在可交易集合）被挡掉",
      "BGBTC" not in event_coins.recognize("锁仓BGBTC解锁2400枚UNI空投", TRADABLE))
check("大小写边界：UNITED 里的 UNI 不算",
      event_coins.recognize("UNITED 集团发布财报", TRADABLE) == [],
      str(event_coins.recognize("UNITED 集团发布财报", TRADABLE)))
check("小写不认（near 是普通英文词，误报比漏报糟）",
      event_coins.recognize("price is near the top", TRADABLE) == [])
check("可交易集合为空 → 只认中文别名（降级，不放行英文）",
      event_coins.recognize("比特币与 UNI 同时上涨", set()) == ["BTC"],
      str(event_coins.recognize("比特币与 UNI 同时上涨", set())))
check("中英混合、去重保序",
      event_coins.recognize("比特币(BTC)与 UNI 同步走强，随后 BTC 回落", TRADABLE)
      == ["BTC", "UNI"],
      str(event_coins.recognize("比特币(BTC)与 UNI 同步走强，随后 BTC 回落", TRADABLE)))

print("\n== 3. 预览页解析（真样本夹具）==")
fx = open(os.path.join(root, "extra", "fixtures", "jin10_sample.html"),
          encoding="utf-8").read()
msgs = event_scrape.parse_messages(fx)
check("解析出 3 条", len(msgs) == 3, str(len(msgs)))
check("字段齐全（msg_id / ISO 时间 / 正文）",
      all(m["msg_id"] and m["ts"].endswith("+00:00") and m["text"] for m in msgs),
      str(msgs[:1]))
check("按 msg_id 升序", [m["msg_id"] for m in msgs] == sorted(m["msg_id"] for m in msgs))
check("正文里去掉了 HTML 标签", all("<" not in m["text"] for m in msgs),
      str([m["text"] for m in msgs]))
_ids = {m["msg_id"]: m for m in msgs}
check("真样本第 1 条：中文币名认得出（比特币）",
      event_coins.recognize(_ids[235858]["text"], TRADABLE) == ["BTC"],
      _ids[235858]["text"][:40])
check("真样本第 2 条：无币种的不认",
      event_coins.recognize(_ids[235859]["text"], TRADABLE) == [],
      _ids[235859]["text"][:40])
check("真样本第 3 条：英文 ticker 认得出（UNI），BGBTC 不算",
      event_coins.recognize(_ids[235860]["text"], TRADABLE) == ["UNI"],
      _ids[235860]["text"][:40])

print("\n== 4. 事件 DB：建表 / 去重 / 水位 ==")
storage.init_event_db()
with storage.get_conn(config.EVENT_DB_PATH) as c:
    for m in msgs:
        coins = event_coins.recognize(m["text"], TRADABLE)
        c.execute("INSERT OR IGNORE INTO crypto_event_raw (msg_id, ts, text, coins) "
                  "VALUES (?,?,?,?)", (m["msg_id"], m["ts"], m["text"],
                                       json.dumps(coins, ensure_ascii=False)))
    n1 = c.execute("SELECT COUNT(*) n FROM crypto_event_raw").fetchone()["n"]
    # 再插一遍（模拟重复抓取）→ 靠 msg_id UNIQUE 去重
    for m in msgs:
        c.execute("INSERT OR IGNORE INTO crypto_event_raw (msg_id, ts, text, coins) "
                  "VALUES (?,?,?,?)", (m["msg_id"], m["ts"], m["text"], "[]"))
    n2 = c.execute("SELECT COUNT(*) n FROM crypto_event_raw").fetchone()["n"]
    storage.event_meta_set(c, "watermark_msg_id", max(m["msg_id"] for m in msgs))
    wm = storage.event_meta_get(c, "watermark_msg_id")
check("入库 3 条", n1 == 3, str(n1))
check("重复抓同一条不重复入库（msg_id UNIQUE）", n2 == 3, str(n2))
check("水位读写正常", wm == "235860", str(wm))
with storage.get_conn(config.EVENT_DB_PATH) as c:
    row = c.execute("SELECT coins FROM crypto_event_raw WHERE msg_id=235858").fetchone()
check("币种落库为 JSON（含中文识别结果）",
      json.loads(row["coins"]) == ["BTC"], str(row["coins"]))
with storage.get_conn(config.EVENT_DB_PATH) as c:
    c.execute("INSERT INTO crypto_event_ai (event_id, coin, impact, status) "
              "VALUES (1,'BTC','利多','ok')")
    dup = 0
    try:
        c.execute("INSERT INTO crypto_event_ai (event_id, coin, impact, status) "
                  "VALUES (1,'BTC','利空','ok')")
    except Exception:
        dup = 1
check("同一事件同一币种不会判定两次（UNIQUE(event_id, coin)）", dup == 1)

print("\n== 5. AI 回复解析（容错）==")
import event_agent as ea  # noqa: E402
check("正常 JSON 数组",
      ea.parse_ai_reply('[{"coin":"UNI","impact":"利多","reason":"机构入场","confidence":82}]',
                        ["UNI"]) == [{"coin": "UNI", "impact": "利多",
                                      "reason": "机构入场", "confidence": 82}])
check("带 ```json 围栏也能解析",
      [x["impact"] for x in ea.parse_ai_reply(
          '```json\n[{"coin":"BTC","impact":"利空","confidence":60}]\n```', ["BTC"])]
      == ["利空"])
check("模型多回没问的币 → 丢掉",
      ea.parse_ai_reply('[{"coin":"ETH","impact":"利多","confidence":50}]', ["UNI"]) == [])
check("impact 写了别的词 → 丢掉",
      ea.parse_ai_reply('[{"coin":"UNI","impact":"看涨","confidence":50}]', ["UNI"]) == [])
check("置信度越界被夹到 0~100",
      [x["confidence"] for x in ea.parse_ai_reply(
          '[{"coin":"UNI","impact":"利多","confidence":999}]', ["UNI"])] == [100])
check("不是 JSON → 返回空（不抛）", ea.parse_ai_reply("利多吧", ["UNI"]) == [])

print("\n== 6. 新鲜度 / 抓取水位（首轮不回填）==")
from datetime import datetime, timedelta, timezone  # noqa: E402
_iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
check("刚发的 → 新鲜", ea._fresh(_iso(datetime.now(timezone.utc)), 10) is True)
check("11 分钟前 → 不新鲜",
      ea._fresh(_iso(datetime.now(timezone.utc) - timedelta(minutes=11)), 10) is False)
check("时间格式坏掉 → 当不新鲜（宁可不开）", ea._fresh("", 10) is False)

storage.init_event_db()
with storage.get_conn(config.EVENT_DB_PATH) as c:
    c.execute("DELETE FROM crypto_event_raw")
    c.execute("DELETE FROM crypto_event_ai")
    c.execute("DELETE FROM crypto_event_meta")
ea.event_scrape.fetch_page = lambda ch, before=None: fx
# 可交易集合打桩：不然测试会真去调币安 exchangeInfo（网络一断就 flaky）
ea.tradable_set = lambda: TRADABLE
check("首轮：只记水位、不回填（入库 0 条）", ea.scrape_once() == 0)
with storage.get_conn(config.EVENT_DB_PATH) as c:
    n_raw = c.execute("SELECT COUNT(*) n FROM crypto_event_raw").fetchone()["n"]
    wm2 = storage.event_meta_get(c, "watermark_msg_id")
check("首轮后库里没有历史消息", n_raw == 0, str(n_raw))
check("水位记到了最新 id", wm2 == "235860", str(wm2))
check("第二轮：没有比水位更新的 → 不入库", ea.scrape_once() == 0)
# 追加一条更新的消息（模拟新快讯）
_new = (fx.replace("235860", "235861").replace("11:29:36", "11:35:36")
          .replace("解锁2400枚UNI空投", "解锁2400枚ONDO空投"))
ea.event_scrape.fetch_page = lambda ch, before=None: fx + _new
check("新消息入库 1 条", ea.scrape_once() == 1)
with storage.get_conn(config.EVENT_DB_PATH) as c:
    row = c.execute("SELECT coins FROM crypto_event_raw WHERE msg_id=235861").fetchone()
    pend = c.execute("SELECT coin FROM crypto_event_ai WHERE status='pending'").fetchall()
check("新消息里认出了币种（ONDO）", row and json.loads(row["coins"]) == ["ONDO"],
      str(row and row["coins"]))
check("只为含币种的事件排队做 AI（1 条）", [r["coin"] for r in pend] == ["ONDO"],
      str([r["coin"] for r in pend]))
# 无币种的消息**不入库**（用户要求："只收录明确指向某个币种的消息"）
_no_coin = (fx.replace("235860", "235862").replace("11:29:36", "11:40:00")
              .replace("解锁2400枚UNI空投", "某交易所宣布将上线新的理财活动"))
ea.event_scrape.fetch_page = lambda ch, before=None: fx + _new + _no_coin
check("无币种的新消息不入库", ea.scrape_once() == 0)
with storage.get_conn(config.EVENT_DB_PATH) as c:
    n_raw2 = c.execute("SELECT COUNT(*) n FROM crypto_event_raw").fetchone()["n"]
    wm3 = storage.event_meta_get(c, "watermark_msg_id")
check("库里只有含币种的那条", n_raw2 == 1, str(n_raw2))
check("水位照常推进（不会反复扫同一条）", wm3 == "235862", str(wm3))
# 跳过的要留一行日志：定期 review 才知道该补哪些中文别名（不然漏了也不知道）
import contextlib  # noqa: E402
_buf = io.StringIO()
ea.event_scrape.fetch_page = lambda ch, before=None: fx + _new + _no_coin.replace(
    "235862", "235863")
with contextlib.redirect_stdout(_buf):
    ea.scrape_once()
check("未识别到币种的消息会打日志", "未识别到币种" in _buf.getvalue(),
      _buf.getvalue()[:120])

print("\n== 7. 交易护栏（决定要不要下单）==")
import types  # noqa: E402
_calls = []


def _fake_open(conn, cand, settings, strategy=None, side=None):
    _calls.append((cand["token"], side))
    conn.execute(
        "INSERT INTO trade_positions (token, symbol, side, status, mode, "
        "margin_amount, leverage, notional, quantity, entry_price, current_price, "
        "strategy, closed_qty) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cand["token"], cand["token"] + "USDT", side, "OPEN", "paper", 50.0, 3.0,
         150.0, 1.0, 100.0, 100.0, "crypto_event", 0.0))
    return True


_fake_tl = types.ModuleType("trade_logic")
_fake_tl.open_paper_position = _fake_open
sys.modules["trade_logic"] = _fake_tl
_fake_mkt = types.ModuleType("market")
_fake_mkt.get_market_snapshot = lambda tok, heavy=False: {"mark_price": 100.0}
sys.modules["market"] = _fake_mkt
config.EVENT_MIN_CONFIDENCE = 70
config.EVENT_FRESH_MINUTES = 10
config.EVENT_DAILY_MAX = 12
config.EVENT_COOLDOWN_MINUTES = 30


def _add_ai(coin, impact, conf, *, ts_min_ago=0, status="ok"):
    with storage.get_conn(config.EVENT_DB_PATH) as c:
        cur = c.execute("INSERT INTO crypto_event_raw (msg_id, ts, text, coins) "
                        "VALUES (?,?,?,?)",
                        (abs(hash((coin, impact, conf, ts_min_ago))) % 10**9,
                         _iso(datetime.now(timezone.utc) - timedelta(minutes=ts_min_ago)),
                         f"{coin} 的测试事件", json.dumps([coin])))
        c.execute("INSERT INTO crypto_event_ai (event_id, coin, impact, confidence, "
                  "status) VALUES (?,?,?,?,?)", (cur.lastrowid, coin, impact, conf, status))


with storage.get_conn() as c:
    c.executescript(storage.SCHEMA)
    c.execute("ALTER TABLE trade_positions ADD COLUMN managed INTEGER DEFAULT 1")

_calls.clear()
_add_ai("UNI", "利多", 90)                    # 该开多
_add_ai("BTC", "中性", 95)                    # 中性 → 不开
_add_ai("DOGE", "利多", 50)                   # 置信度不够 → 不开
_add_ai("SOL", "利空", 95, ts_min_ago=30)     # 过期 → 不开
n = ea.trade_once()
check("只开了 1 笔（中性/低置信度/过期都被挡）", n == 1 and _calls == [("UNI", "LONG")],
      f"{n} {_calls}")
with storage.get_conn(config.EVENT_DB_PATH) as c:
    reasons = [r["reason"] for r in c.execute(
        "SELECT reason FROM crypto_event_trade ORDER BY id")]
    traded_impacts = {r["impact"] for r in c.execute(
        "SELECT impact FROM crypto_event_ai WHERE traded=1")}
check("被挡的每条都留了原因（页面能看到为什么不跟）",
      any("过期" in (x or "") for x in reasons), str(reasons))
# 只有"利多/利空"才会被策略选中（中性/无关、低置信度连查询都进不去）→ 它们不打 traded ✓
check("策略考虑过的判定（利多/利空）都打了 traded 标记",
      traded_impacts == {"利多", "利空"}, str(traded_impacts))
check("重复消费不重复开仓（第二条同币）", ea.trade_once() == 0)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
