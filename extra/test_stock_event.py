"""美股/商品事件地基测试：标的识别 / 独立库 / 双管线（入库→AI→纸盘交易）/ web 接线。

识别用例的文本是**从服务器 crypto_event_raw 实抓的金十真消息**（跑覆盖率统计时拿到的），
按"外部 payload 夹具必须照真样本造"的规矩来 —— 手搓的样例会骗过识别器。
"""
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import types

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config

_TMP = tempfile.mkdtemp(prefix="stockev_")
config.DB_PATH = os.path.join(_TMP, "main.db")
config.EVENT_DB_PATH = os.path.join(_TMP, "crypto_event.db")
config.STOCK_EVENT_DB_PATH = os.path.join(_TMP, "stock_event.db")
config.EVENT_AI_API_KEY = "test-key"

import stock_coins  # noqa: E402
import storage  # noqa: E402

# 桩：币安 TradFi 永续白名单（照真名单收常用的；未上市的一律不在）
# SPY/QQQ 在真名单里（本次不做指数靠"不映射"实现）→ 桩里必须留着，
# 否则"有人把指数加进映射表"这个变异会静默通过
TRADIFI = {"NVDA", "AMD", "MU", "GOOGL", "SPCX", "SPY", "QQQ", "XAU", "XAG",
           "CL", "BZ", "NATGAS", "COPPER", "COIN", "HOOD", "TSLA", "TSM",
           "MSFT", "AAPL", "TENCENT", "MEITUAN", "KUAISHOU", "SMCI",
           "QCOM", "HYUNDAI"}
stock_coins._tradable = lambda t: t in TRADIFI

# 主库（设置/仓位都在它里面）：先建出来，后面各段直接用
with storage.get_conn() as _c:
    _c.executescript(storage.SCHEMA)
    _c.execute("ALTER TABLE trade_positions ADD COLUMN managed INTEGER DEFAULT 1")

import event_agent as ea  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


# ============================================================ 1. 标的识别（真样本）
print("== 1. 标的识别：金十真消息 ==")
_real_nvda = ("个股新é 1.英伟达(NVDA.O)宣布将股票回购授权规模增加1500亿美元，现总额达到2350亿美元。"
              " 2. 英伟达(NVDA.O)推出开放式智能体安全平台，从测试到部署全程保障智能体安全。"
              " 3. SpaceX(SPCX.O)将首次进行星舰入轨飞行测试。 4. 谷歌(GOOG.O)投资152亿美元")
check("后缀代码 NVDA.O/SPCX.O/GOOG.O 识别（≤3 个上限）",
      stock_coins.identify(_real_nvda) == ["NVDA", "SPCX", "GOOGL"],
      str(stock_coins.identify(_real_nvda)))
_real_amd = ("整理：每日科技要闻速递（9月29日） 人工智能 1.OpenAI因安全问题推迟发布GPT-6.1 Astra。"
             " 2. Anthropic IPO招股书曝光，2025财年营收达45.9亿美元。"
             " 3. AMD斥资82亿美元收购World Labs，李飞飞将任AMD首席")
check("中文句子里的英文名 AMD 能认（\\b 对中文失效的坑）",
      stock_coins.identify(_real_amd) == ["AMD"],
      str(stock_coins.identify(_real_amd)))
_real_mixed = ("个股新é 1.AMD(AMD.O)同意以82亿美元收购李飞飞的人工智能初创公司World Labs。"
               " 2. 英伟达(NVDA.O)、Anthropic等AI巨头高管将与特朗普讨论AI风险。"
               " 3. Netlist就涉及人工智能计算的HBM专利，向美国国际贸易委员会对美光科技(MU.O)提起新的诉讼。")
check("一条含 4 个标的 → 按优先级+顺序取 3 个",
      stock_coins.identify(_real_mixed) == ["AMD", "NVDA", "MU"],
      str(stock_coins.identify(_real_mixed)))
check("MetaMask ≠ Meta（词边界只挡 ASCII 字母）",
      stock_coins.identify("分析师：疑似19个MetaMask验证者遭攻击，攻击者仅窃取约0.36 ETH奖励") == [],
      str(stock_coins.identify("分析师：疑似19个MetaMask验证者遭攻击，攻击者仅窃取约0.36 ETH奖励")))
check("metadata 里的 meta 不算",
      stock_coins.identify("某个 metadata 字段缺失") == [])
check("标普/纳指/道琼斯不映射（不做指数：SPY/QQQ 永不出现）",
      stock_coins.identify("标普500指数期货涨0.3%，纳斯达克100期货涨0.5%") == [],
      str(stock_coins.identify("标普500指数期货涨0.3%，纳斯达克100期货涨0.5%")))
check("京东/百度/蔚来/波音/星巴克（白名单外）被挡",
      stock_coins.identify("京东、百度、蔚来、波音、星巴克均公布最新业绩") == [],
      str(stock_coins.identify("京东、百度、蔚来、波音、星巴克均公布最新业绩")))
check("黄金交叉是技术术语 → 不认；同一句里的「现货黄金」照认",
      stock_coins.identify("黄金交叉出现后，比特币短线拉升；现货黄金站上4300美元") == ["XAU"],
      str(stock_coins.identify("黄金交叉出现后，比特币短线拉升；现货黄金站上4300美元")))
check("商品：WTI原油+布伦特 → CL+BZ",
      stock_coins.identify("OPEC+讨论增产，WTI原油跌破60美元，布伦特原油同步走低") == ["CL", "BZ"],
      str(stock_coins.identify("OPEC+讨论增产，WTI原油跌破60美元，布伦特原油同步走低")))
check("「布伦特原油」只是一处引用 → 只算 BZ（区间去重，不许再被「原油」算成 CL）",
      stock_coins.identify("布伦特原油日内下跌2.00美元，现报99.43美元/桶") == ["BZ"],
      str(stock_coins.identify("布伦特原油日内下跌2.00美元，现报99.43美元/桶")))
check("「高通胀」不是高通（下一个字排除，实测踩过）",
      stock_coins.identify("AI建设需求正在推高通胀") == [],
      str(stock_coins.identify("AI建设需求正在推高通胀")))
check("「现代化」不是现代汽车；「现代汽车」照认",
      stock_coins.identify("加快现代化建设") == []
      and stock_coins.identify("现代汽车发布财报") == ["HYUNDAI"],
      f"{stock_coins.identify('加快现代化建设')} {stock_coins.identify('现代汽车发布财报')}")
check("真名为高通时照认（TradFi 有 QCOM）",
      stock_coins.identify("高通(QCOM.O)发布新一代芯片") == ["QCOM"],
      str(stock_coins.identify("高通(QCOM.O)发布新一代芯片")))
check("港股/中概：腾讯/美团/快手",
      stock_coins.identify("腾讯宣布回购，美团、快手跟涨") == ["TENCENT", "MEITUAN", "KUAISHOU"],
      str(stock_coins.identify("腾讯宣布回购，美团、快手跟涨")))
check("max_n 生效（要 1 个就只给 1 个）",
      len(stock_coins.identify(_real_mixed, max_n=1)) == 1)
check("max_n=0（脏设置）钳到 1，不许静默返回空",
      len(stock_coins.identify(_real_mixed, max_n=0)) == 1,
      str(stock_coins.identify(_real_mixed, max_n=0)))
check("加密消息里的 Coinbase 会命中 COIN（由 AI 判无关，属于预期）",
      stock_coins.identify("Coinbase比特币溢价指数已连续4日录得负值") == ["COIN"],
      str(stock_coins.identify("Coinbase比特币溢价指数已连续4日录得负值")))
check("超微歧义：超微电脑→SMCI、超威半导体→AMD，裸「超微」不认",
      stock_coins.identify("超微电脑发布财报") == ["SMCI"]
      and stock_coins.identify("超威半导体发布财报") == ["AMD"]
      and stock_coins.identify("超微发布财报") == [],
      f"{stock_coins.identify('超微电脑发布财报')} {stock_coins.identify('超微发布财报')}")
check("指数词表（EXCLUDED_INDEX）与映射表零冲突（不做指数的不变量）",
      not (set(stock_coins.EXCLUDED_INDEX) & set(stock_coins.NAME_MAP)),
      str(set(stock_coins.EXCLUDED_INDEX) & set(stock_coins.NAME_MAP)))

# ============================================================ 2. 独立库 + 设置
print("\n== 2. 独立库 / 设置项 ==")
storage.init_stock_event_db()
with sqlite3.connect(config.STOCK_EVENT_DB_PATH) as c:
    tabs = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
check("独立库建出 4 张表",
      {"stock_event_raw", "stock_event_ai", "stock_event_trade",
       "stock_event_meta"} <= tabs, str(sorted(tabs)))
check("没有污染加密事件的表（独立库）",
      not any(t.startswith("crypto_") for t in tabs))
with storage.get_conn(config.STOCK_EVENT_DB_PATH) as c:
    storage.stock_event_meta_set(c, "watermark_msg_id", 999)
with storage.get_conn(config.STOCK_EVENT_DB_PATH) as c:
    check("meta 读写", storage.stock_event_meta_get(c, "watermark_msg_id") == "999")
    check("meta 缺省值", storage.stock_event_meta_get(c, "nope", "dflt") == "dflt")
with storage.get_conn() as c:
    st = storage.trading_settings_get(c)
    ks = {k: st.get(k) for k in ("stock_event_enabled", "stock_event_trade_enabled",
                                 "stock_event_min_confidence", "stock_event_fresh_minutes",
                                 "stock_event_cooldown_minutes", "stock_event_daily_max",
                                 "stock_event_max_tickers", "strategy_initial_stock_event")}
check("设置默认值齐备（7 项 + 本金）",
      ks == {"stock_event_enabled": True, "stock_event_trade_enabled": True,
             "stock_event_min_confidence": 75, "stock_event_fresh_minutes": 15,
             "stock_event_cooldown_minutes": 120, "stock_event_daily_max": 6,
             "stock_event_max_tickers": 3, "strategy_initial_stock_event": 1000}, str(ks))
with storage.get_conn() as c:
    storage.trading_settings_update(c, {"stock_event_min_confidence": 88,
                                        "stock_event_enabled": False})
    st2 = storage.trading_settings_get(c)
check("设置写入 + 布尔/数值类型正确",
      st2["stock_event_min_confidence"] == 88.0 and st2["stock_event_enabled"] is False,
      f"{st2['stock_event_min_confidence']} {st2['stock_event_enabled']}")
check("st_settings() 读到热值",
      ea.st_settings()["min_conf"] == 88 and ea.st_settings()["enabled"] is False,
      str(ea.st_settings()))
with storage.get_conn() as c:
    storage.trading_settings_update(c, {"stock_event_min_confidence": 75,
                                        "stock_event_enabled": True})

# ============================================================ 3. 双管线
print("\n== 3. 双管线：入库 → AI → 纸盘交易 ==")
# 先把水位清空（第 2 节的 meta 读写测试刚把它设成了 999）
with storage.get_conn(config.STOCK_EVENT_DB_PATH) as c:
    storage.stock_event_meta_set(c, "watermark_msg_id", "")

_msgs = [
    {"msg_id": 1001, "ts": "2026-10-02T06:00:00+00:00",
     "text": "英伟达(NVDA.O)宣布回购1500亿美元；谷歌(GOOG.O)投资152亿"},
    {"msg_id": 1002, "ts": "2026-10-02T06:01:00+00:00",
     "text": "美联储理事发表讲话（没有标的的消息）"},
]
check("首轮只记水位、不回填", ea._ingest_stock(_msgs) == 0)
with storage.get_conn(config.STOCK_EVENT_DB_PATH) as c:
    check("首轮水位推到最新一条（含未识别的，防重复扫）",
          storage.stock_event_meta_get(c, "watermark_msg_id") == "1002")
_msgs2 = _msgs + [{"msg_id": 1003, "ts": ea._now_iso(),
                   "text": "英伟达(NVDA.O)、美光科技(MU.O)获上调评级"},
                  {"msg_id": 1004, "ts": ea._now_iso(),
                   "text": "美联储理事发表讲话（同样没有标的）"}]
check("第二轮增量入库 1 条（1004 无标的入样本、不入库）",
      ea._ingest_stock(_msgs2) == 1)
with storage.get_conn(config.STOCK_EVENT_DB_PATH) as c:
    c.row_factory = sqlite3.Row
    raws = [dict(r) for r in c.execute("SELECT msg_id, tickers FROM stock_event_raw")]
    ais = [dict(r) for r in c.execute(
        "SELECT ticker, status FROM stock_event_ai ORDER BY id")]
    un = storage.stock_event_meta_get(c, "unmatched_samples")
check("raw 落库（含 tickers JSON，历史消息不回填）",
      len(raws) == 1 and raws[0]["msg_id"] == 1003
      and json.loads(raws[0]["tickers"]) == ["NVDA", "MU"], str(raws))
check("每个标的建一条 pending 判定",
      [a["ticker"] for a in ais] == ["NVDA", "MU"] and
      all(a["status"] == "pending" for a in ais), str(ais))
check("未识别样本留档（给补别名用）", "美联储" in (un or ""), str(un[:60]))

# 识别异常不许卡住管线：水位照常推进（否则坏消息每轮重试 → 之后的快讯全堵死）
_orig_identify = stock_coins.identify


def _boom_identify(text, max_n=None):
    if "炸弹" in (text or ""):
        raise RuntimeError("boom")
    return _orig_identify(text, max_n)


stock_coins.identify = _boom_identify
_msgs3 = _msgs2 + [{"msg_id": 1005, "ts": ea._now_iso(), "text": "炸弹消息"}]
check("识别异常 → 跳过该条、管线不卡（返回 0 而不是抛出去）",
      ea._ingest_stock(_msgs3) == 0)
with storage.get_conn(config.STOCK_EVENT_DB_PATH) as c:
    check("水位已推到 1005（坏消息不会卡住水位）",
          storage.stock_event_meta_get(c, "watermark_msg_id") == "1005")
stock_coins.identify = _orig_identify

check("AI 解析：利好 收（美股管线档位）",
      ea.parse_ai_reply('[{"ticker":"NVDA","impact":"利好","confidence":90}]',
                        ["NVDA"], key_field="ticker")
      == [{"ticker": "NVDA", "impact": "利好", "reason": "", "confidence": 90}])
check("AI 解析：利多 也收（加密管线档位，两条管线共用解析）",
      ea.parse_ai_reply('[{"coin":"BTC","impact":"利多","confidence":80}]', ["BTC"])
      [0]["impact"] == "利多")
check("AI 解析：非法档位/未问的标的被挡",
      ea.parse_ai_reply('[{"ticker":"TSLA","impact":"看涨","confidence":90}]',
                        ["NVDA"], key_field="ticker") == [])

# AI 桩：NVDA 90（过门槛 75）、MU 80（过）→ 都应开仓
ea._ai_chat = lambda s, u: json.dumps(
    [{"ticker": "NVDA", "impact": "利好", "reason": "回购", "confidence": 90},
     {"ticker": "MU", "impact": "利好", "reason": "评级上调", "confidence": 80}],
    ensure_ascii=False)
check("分析：1 条消息成功", ea.stock_analyze_once() == 1)
with storage.get_conn(config.STOCK_EVENT_DB_PATH) as c:
    c.row_factory = sqlite3.Row
    got = {r["ticker"]: (r["impact"], r["confidence"], r["status"])
           for r in c.execute("SELECT * FROM stock_event_ai")}
check("判定回写 ok + 置信度", got["NVDA"] == ("利好", 90, "ok"), str(got))

# 再手工造两条"该被护栏挡下"的判定：一条过期、一条低置信度
with storage.get_conn(config.STOCK_EVENT_DB_PATH) as c:
    _old = "2026-10-02T06:00:00+00:00"
    c.execute("INSERT INTO stock_event_raw (msg_id, ts, text, tickers) "
              "VALUES (3001, ?, '特斯拉盘前涨3%', '[\"TSLA\"]')", (_old,))
    e1 = c.execute("SELECT id FROM stock_event_raw WHERE msg_id=3001").fetchone()[0]
    c.execute("INSERT INTO stock_event_ai (event_id, ticker, impact, confidence, "
              "status, traded) VALUES (?, 'TSLA', '利好', 90, 'ok', 0)", (e1,))
    c.execute("INSERT INTO stock_event_raw (msg_id, ts, text, tickers) "
              "VALUES (3002, ?, '英伟达盘前微涨', '[\"NVDA\"]')", (ea._now_iso(),))
    e2 = c.execute("SELECT id FROM stock_event_raw WHERE msg_id=3002").fetchone()[0]
    c.execute("INSERT INTO stock_event_ai (event_id, ticker, impact, confidence, "
              "status, traded) VALUES (?, 'NVDA', '利好', 10, 'ok', 0)", (e2,))

_calls = []
_fake_tl = types.ModuleType("trade_logic")
_fake_tl.open_paper_position = (
    lambda conn, cand, settings, strategy=None, side=None:
    (_calls.append((cand["token"], strategy, side)) or True))
sys.modules["trade_logic"] = _fake_tl
import market as market_mod  # noqa: E402
market_mod.get_market_snapshot = lambda t, heavy=False: {"mark_price": 100.0}
market_mod.get_mark_price = lambda t: 0.0
check("交易：开了 2 笔（strategy=stock_event，过期/低置信度被挡）",
      ea.stock_trade_once() == 2
      and [c[1] for c in _calls] == ["stock_event", "stock_event"], str(_calls))
check("交易：方向按利好=LONG",
      [c[2] for c in _calls] == ["LONG", "LONG"], str(_calls))
with storage.get_conn(config.STOCK_EVENT_DB_PATH) as c:
    c.row_factory = sqlite3.Row
    tr = [dict(r) for r in c.execute("SELECT ticker, ok, reason "
                                     "FROM stock_event_trade ORDER BY id")]
check("护栏落痕：过期 + 低置信度各留一条未下单原因",
      sum(r["ok"] for r in tr) == 2
      and any("过期" in (r["reason"] or "") and r["ok"] == 0 for r in tr),
      str(tr))
check("幂等：再跑不重复下单", ea.stock_trade_once() == 0 and len(_calls) == 2)

# 模型偶尔按加密档位答「利多」：解析层已收 → 下单也要认（不能静默丢信号）
with storage.get_conn(config.STOCK_EVENT_DB_PATH) as c:
    c.execute("INSERT INTO stock_event_raw (msg_id, ts, text, tickers) "
              "VALUES (3003, ?, '特斯拉获上调评级', '[\"TSLA\"]')", (ea._now_iso(),))
    e3 = c.execute("SELECT id FROM stock_event_raw WHERE msg_id=3003").fetchone()[0]
    c.execute("INSERT INTO stock_event_ai (event_id, ticker, impact, confidence, "
              "status, traded) VALUES (?, 'TSLA', '利多', 90, 'ok', 0)", (e3,))
check("模型答「利多」（加密档位）时照开 LONG",
      ea.stock_trade_once() == 1 and _calls[-1] == ("TSLA", "stock_event", "LONG"),
      str(_calls[-1:]))
with storage.get_conn() as c:
    storage.trading_settings_update(c, {"stock_event_trade_enabled": True})

# ============================================================ 4. web 接线（静态）
print("\n== 4. web.py 接线 ==")
_web = open(os.path.join(ROOT, "web.py"), encoding="utf-8").read()
check("页面路由 /stock-events 与 /stock-event-ai",
      '@app.get("/stock-events"' in _web and '@app.get("/stock-event-ai"' in _web)
check("API 路由 /api/stock-events 与 /api/stock-event-ai",
      '@app.get("/api/stock-events")' in _web
      and '@app.get("/api/stock-event-ai")' in _web)
check("导航（三大 TAG）里两个美股页都有入口",
      '("/stock-events", "美股/商品事件展览", "stock-events")' in _web
      and '("/stock-event-ai", "美股 AI 判定", "stock-event-ai")' in _web)
check("页面常量是模块级字面量（检查器能自动发现）",
      'STOCK_EVENTS_HTML = """' in _web and 'STOCK_EVENT_AI_HTML = """' in _web)
check("两页都有 {NAV}/{EV_CSS} 占位符 + <script>",
      _web.count("STOCK_EVENTS_HTML.replace") == 1
      and _web.count("STOCK_EVENT_AI_HTML.replace") == 1
      and "{EV_CSS}" in _web and "{NAV}" in _web)
for _id in ("trade-stock-event-switch", "trade-stock-event-trade-switch",
            "trade-stock-event-min-confidence", "trade-stock-event-fresh-minutes",
            "trade-stock-event-cooldown-minutes", "trade-stock-event-daily-max",
            "trade-stock-event-max-tickers"):
    check(f"设置页输入框 {_id}（HTML+回填+保存三处都在）",
          _web.count(_id) >= 3, str(_web.count(_id)))
check("数值项用 numOrUndef（清空输入框不许静默存 0）",
      "stock_event_min_confidence: numOrUndef(" in _web
      and "stock_event_max_tickers: numOrUndef(" in _web)
check("策略页开关徽章反映真实开关（enabled_map 里有 stock_event）",
      '"stock_event": ts.get("stock_event_enabled"' in _web)
check("AI 判定页有美股盘中/休市提示（设计里承诺的时段可见性）",
      "America/New_York" in _web and "usSession()" in _web)

# ============================================================ 5. API 逻辑（无 fastapi，抽源码 exec）
print("\n== 5. API 逻辑（exec 抽出的函数 + 桩 app）==")


_API_NS = {"storage": storage, "config": config,
           "HTTPException": type("HTTPException", (Exception,), {}),
           "_ev_local_ts": lambda s: str(s)[:19],
           "app": type("App", (), {"get": lambda *a, **k: (lambda f: f)})()}
# API 用到的模块级常量（_ST_META_KEYS 等）也从源码里 exec 进来
for _cn in ("_ST_META_KEYS", "_ST_IMPACTS"):
    _mm = re.search(rf"^{_cn} = \(.*?\)\n", _web, re.S | re.M)
    if _mm:
        exec(_mm.group(0), _API_NS)


def _load_api(name: str) -> dict:
    """本地没有 fastapi，不能 import web → 把 API 函数源码抽出来在桩命名空间里 exec。"""
    m = re.search(r'@app\.get\("/api/' + name +
                  r'"\)\n(def \w+\(.*?)(?=\n@app\.|\n[A-Z_]{3,} = )',
                  _web, re.S)
    assert m, f"未找到 API {name}"
    ns = dict(_API_NS)
    exec("from __future__ import annotations\n" + m.group(1), ns)
    return ns


_ns1 = _load_api("stock-events")
_out1 = _ns1["api_stock_events"](limit=10, offset=0)
check("api_stock_events：结构 + 总数 + tickers 解析",
      _out1["ok"] and _out1["total"] == 4 and _out1["list"]
      and isinstance(_out1["list"][0]["tickers"], list), str(_out1)[:120])
check("api_stock_events：带未识别样本 + meta",
      "unmatched" in _out1 and "ai_state" in _out1["meta"])
_ns2 = _load_api("stock-event-ai")
_out2 = _ns2["api_stock_event_ai"](limit=10, offset=0, impact="利好")
check("api_stock_event_ai：结构 + 按档位过滤", _out2["ok"] and _out2["total"] >= 2,
      str(_out2)[:120])
check("筛选「利好」时把同义的「利多」也算进来（不筛丢信号）",
      any(x["impact"] == "利多" for x in _out2["list"]),
      str([x["impact"] for x in _out2["list"]]))
check("筛选「利空」不混入其它档位",
      all(x["impact"] == "利空" for x in
          _ns2["api_stock_event_ai"](limit=10, offset=0, impact="利空")["list"]))
check("api_stock_event_ai：仓位信息回填（position_id 关联主库）",
      all("position" in x for x in _out2["list"]))

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
