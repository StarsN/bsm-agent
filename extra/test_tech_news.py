"""科技股动态管线测试：拆条入库（三元组）/ 首轮水位 / 节奏预算 / 退化 / 重试 / 开关。

真样本夹具（与 test_tech_stocks.py 同一批服务器原文，夹具铁律）。
搜索与 AI 全 mock，不碰外网、不真等待。

运行：python extra/test_tech_news.py
"""
import io
import json
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config

_TMP = tempfile.mkdtemp(prefix="technews_")
config.DB_PATH = os.path.join(_TMP, "main.db")
config.TECH_NEWS_DB_PATH = os.path.join(_TMP, "tech_news.db")
config.EVENT_AI_API_KEY = "test-key"
config.TECH_NEWS_BATCH_PER_ROUND = 5
config.TECH_NEWS_DAILY_BUDGET = 200
config.TECH_NEWS_SEARCH_LIMIT = 6

import storage  # noqa: E402

with storage.get_conn() as _c:
    _c.executescript(storage.SCHEMA)
storage.init_tech_news_db()        # 生产由 event_agent.main() 初始化

import event_agent as ea  # noqa: E402

ea.time.sleep = lambda *a, **k: None       # 不等真实间隔

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def tn_rows(sql, args=()):
    with storage.get_conn(config.TECH_NEWS_DB_PATH) as c:
        return [dict(r) for r in c.execute(sql, args)]


def tn_meta(key, dflt=""):
    with storage.get_conn(config.TECH_NEWS_DB_PATH) as c:
        return storage.tech_news_meta_get(c, key, dflt)


# ---- 真样本夹具（服务器 stock_event_raw 原文）----
FIX_ROUNDUP = '1. DRAM迎超级成长周期，传南亚科计划涨价，涨幅最高达20%。 2. 三星电机签订2900亿韩元AI服务器MLCC供应合同。 3. 三星斥资近50亿美元在韩越扩产半导体基板。 4. 希捷和东芝据悉竞购TDK硬盘磁头业务，交易金额或达数十亿美元。 5. AMD苏姿丰：存储芯片供应仍然紧张，2027年将大幅提升供应量。 6. AMD苏姿丰密会台积电，证实在台百亿美元投资将加码。 7. CFM发布最新季度报告：存储价格还将继续上涨。 8. 华为余承东谈内存压力：每部手机成本大增200美元，为保公司生存不得不涨价。 9. 三大存储原厂产能已被提前锁定，大摩估算英伟达已锁定2027年全球约37.3%的HBM供应。 10. 据日经新闻：日本半导体设备制造商TOWA计划建设芯片制造设备工厂，以强化日本半导体供应链。'
FIX_MIXED = '国内新闻ï 1.商务部新闻发言人就法德要求欧盟强化贸易防御等保护主义工具答记者问2. 自10月10日起，国内航线燃油附加费将上调。 3. 中方回应英考虑对中国电动汽车提高关税。 国际新闻ï 1.美国8月贸易逆差1056亿美元。 2. 弗朗西斯·哈尔岑获2026年诺贝尔物理学奖。 3. 洲际交易所于伦敦推出黄金期货交易。 4. 消息人士：日本央行或将暗示基础通胀已达2%目标。 5. 特朗普：（被问及暂停征收联邦汽油税）正在考虑此事。 6. IEA预计将在10月14日至15日敲定G7一亿桶油品储备的具体释放方案。 7. 美国就俄罗斯疑似肺鼠疫病例发布卫生警报。 特朗普表示，很快将与普京就“疑似鼠疫”问题通话。 8. 日本央行行长植田和男：将继续根据经济、物价及金融状况的变化上调政策利率。 9. 中东局势— ①卡塔尔说结束地区冲突的谈判和信息交换仍在继续。  ②机构：霍尔木兹原油流量正在恢复，柴油仍严重短缺。  ③美方否认伊朗官媒有关美军直升机在红海坠毁的报道。'
FIX_MISC = '某厂商发布新款芯片，性能提升40%，功耗下降20%'


def mk_msg(mid, text):
    return {"msg_id": mid, "ts": "2026-10-07T03:09:28+00:00", "text": text}


print("== 1. 首轮只记水位（不回填历史）==")
msgs0 = [mk_msg(1001, FIX_ROUNDUP), mk_msg(1002, FIX_MIXED)]
check("首轮入库返回 0", ea._ingest_tech(msgs0) == 0)
check("首轮 raw 为空", tn_rows("SELECT COUNT(*) n FROM tech_news_raw")[0]["n"] == 0)
check("水位已建立=1002", tn_meta("watermark_msg_id") == "1002", tn_meta("watermark_msg_id"))
check("同批再跑也不回填", ea._ingest_tech(msgs0) == 0
      and tn_rows("SELECT COUNT(*) n FROM tech_news_raw")[0]["n"] == 0)

print("\n== 2. 拆条入库：三元组落库（真样本总结 10 条 → 13 个三元组）==")
n = ea._ingest_tech([mk_msg(1003, FIX_ROUNDUP)])
check("返回三元组数 = 13", n == 13, str(n))
raws = tn_rows("SELECT * FROM tech_news_raw WHERE msg_id=1003")
check("raw 1 条且 items=10", len(raws) == 1
      and len(json.loads(raws[0]["items_json"])) == 10,
      str(len(raws) and len(json.loads(raws[0]["items_json"]))))
ai_rows = tn_rows("SELECT * FROM tech_news_ai WHERE msg_id=1003")
check("ai 行 = 13 且全为待处理", len(ai_rows) == 13
      and all(r["status"] == 0 for r in ai_rows), str(len(ai_rows)))
pair_set = {(r["item_no"], r["stock_code"]) for r in ai_rows}
check("①→NANYA / ②→SEMCO（长词优先）/ ⑦→@存储（行业层）",
      (1, "NANYA") in pair_set and (2, "SEMCO") in pair_set and (7, "@存储") in pair_set,
      str(sorted(pair_set)))
check("④一条出三只（STX/TOSHIBA/TDK 全收，无上限）",
      {(4, "STX"), (4, "TOSHIBA"), (4, "TDK")} <= pair_set)
check("⑤AMD 与苏姿丰去重（item5 只有一行）；⑥AMD+台积电=两行",
      sum(1 for r in ai_rows if r["item_no"] == 5) == 1
      and sum(1 for r in ai_rows if r["item_no"] == 6) == 2,
      str({r["item_no"]: r["stock_code"] for r in ai_rows if r["item_no"] in (5, 6)}))
check("分类落库正确（NANYA=存储/内存）",
      [r["cat"] for r in ai_rows if r["stock_code"] == "NANYA"][0] == "存储/内存")

print("\n== 3. 负样本不产生三元组（不进图，但水位推进）==")
n2 = ea._ingest_tech([mk_msg(1004, FIX_MIXED)])
check("政治/宏观总结 → 0 个三元组", n2 == 0, str(n2))
check("水位推进到 1004", tn_meta("watermark_msg_id") == "1004")
check("未命中样本已记录（供补词）", "商务部" in (tn_meta("unmatched_samples") or ""))

print("\n== 4. 综合兜底：泛科技词也收（@科技综合）==")
n3 = ea._ingest_tech([mk_msg(1005, FIX_MISC)])
check("返回 1 个三元组", n3 == 1)
check("落 @科技综合", tn_rows(
    "SELECT stock_code FROM tech_news_ai WHERE msg_id=1005")[0]["stock_code"] == "@科技综合")

print("\n== 5. 生成管线：搜索 + AI + 防编造（sources 必须来自搜索结果）==")
FAKE_LINK = "https://example.com/news/1"
ea._google_news_search = lambda q, limit: [
    {"title": "存储涨价带动闪迪走高", "source": "路透", "date": "Wed, 07 Oct 2026 00:00:00 GMT",
     "link": FAKE_LINK}]


def _ai_good(*a, **k):
    return json.dumps({
        "headline": "HBM 合约价大涨 15%", "tldr": "存储原厂集体调价，AI 需求推高 HBM。",
        "points": ["三星与海力士上调报价", "NAND 供应持续紧张"],
        "numbers": ["HBM +15%"],
        "impact": {"direction": "偏多", "reason": "涨价传导存储厂", "beneficiaries": ["闪迪", "美光"]},
        "sources": [
            {"title": "存储涨价带动闪迪走高", "source": "路透", "url": FAKE_LINK},
            {"title": "编造来源", "source": "假媒体", "url": "https://evil.example/x"},
        ]}, ensure_ascii=False)


ea._ai_chat = _ai_good
done = ea.tech_process_once()
check("本轮生成 5 条（BATCH=5）", done == 5, str(done))
rows5 = tn_rows("SELECT * FROM tech_news_ai WHERE status=1")
check("5 条完成且含图解 JSON",
      len(rows5) == 5 and all(json.loads(r["summary_json"]).get("headline") for r in rows5))
sm = json.loads(rows5[0]["summary_json"])
check("编造的来源被剔除（只剩搜索里的那条）",
      len(sm["sources"]) == 1 and sm["sources"][0]["url"] == FAKE_LINK,
      str(sm["sources"]))
check("search_json 已留档", json.loads(rows5[0]["search_json"] or "[]"))
used = None
with storage.get_conn(config.TECH_NEWS_DB_PATH) as c:
    for r in c.execute("SELECT key, value FROM tech_news_meta"):
        if r["key"].startswith("daily_used:"):
            used = int(r["value"])
check("每日计数已累加 =5", used == 5, str(used))

print("\n== 6. 每日预算：超出只积压、不丢 ==")
config.TECH_NEWS_DAILY_BUDGET = 6
done = ea.tech_process_once()
check("预算只剩 1 → 本轮只生成 1 条", done == 1, str(done))
rest = tn_rows("SELECT COUNT(*) n FROM tech_news_ai WHERE status=0")[0]["n"]
check("其余仍留在待处理队列（8 条）", rest == 8, str(rest))
check("预算用尽后不再消费", ea.tech_process_once() == 0)
config.TECH_NEWS_DAILY_BUDGET = 200

print("\n== 7. 搜索失败 → 退化（仍出图，degrade=1，来源为空）==")
ea._google_news_search = lambda q, limit: None
done = ea.tech_process_once()
check("搜索不可用仍生成 5 条", done == 5, str(done))
degs = tn_rows("SELECT * FROM tech_news_ai WHERE degrade=1")
check("5 条标记 degrade=1 且 sources 为空",
      len(degs) == 5 and all(json.loads(r["summary_json"])["sources"] == [] for r in degs),
      str(len(degs)))

print("\n== 8. AI 连续失败：重试 3 次进 failed（不再自动消费）==")
ea._ai_chat = lambda *a, **k: "这不是 JSON"
config.TECH_NEWS_BATCH_PER_ROUND = 100
check("第 1 次失败 → 留在待处理", ea.tech_process_once() == 0
      and tn_rows("SELECT COUNT(*) n FROM tech_news_ai WHERE status=0")[0]["n"] == 3)
ea.tech_process_once()
ea.tech_process_once()
failed = tn_rows("SELECT * FROM tech_news_ai WHERE status=2")
check("3 次后转 failed", len(failed) == 3, str(len(failed)))
check("failed 不再被自动消费", ea.tech_process_once() == 0)

print("\n== 9. 失败重试（页面按钮的动作：status 2 → 0、attempts 归零）==")
f = failed[0]
with storage.get_conn(config.TECH_NEWS_DB_PATH) as c:
    c.execute("UPDATE tech_news_ai SET status=0, attempts=0, updated_at=CURRENT_TIMESTAMP "
              "WHERE msg_id=? AND item_no=? AND stock_code=? AND status=2",
              (f["msg_id"], f["item_no"], f["stock_code"]))
f2 = tn_rows("SELECT * FROM tech_news_ai WHERE msg_id=? AND item_no=? AND stock_code=?",
             (f["msg_id"], f["item_no"], f["stock_code"]))[0]
check("重试后回到待处理且 attempts=0", f2["status"] == 0 and f2["attempts"] == 0)
ea._ai_chat = _ai_good
check("重试后能重新生成", ea.tech_process_once() == 1)

print("\n== 10. 总开关：关=不入库、不推进水位（重开自动补）==")
with storage.get_conn() as c:
    storage.trading_settings_update(c, {"tech_news_enabled": False})
wm_before = tn_meta("watermark_msg_id")
check("关闭时入库返回 0", ea._ingest_tech([mk_msg(1006, FIX_ROUNDUP)]) == 0)
check("关闭时不推进水位（重开可补上页内消息）", tn_meta("watermark_msg_id") == wm_before)
with storage.get_conn() as c:
    storage.trading_settings_update(c, {"tech_news_enabled": True})
check("重新开启后恢复入库", ea._ingest_tech([mk_msg(1007, FIX_MISC)]) == 1)

print("\n== 11. 图解 JSON 结构整形（模型答成字符串/缺字段也不崩）==")
ea._google_news_search = lambda q, limit: []
ea._ai_chat = lambda *a, **k: json.dumps({
    "headline": "整形测试", "tldr": None, "points": "不是数组", "numbers": None,
    "impact": "偏多", "sources": "x"})
ea._ingest_tech([mk_msg(1008, "英伟达发布新一代芯片，算力翻倍")])
ea.tech_process_once()
row = tn_rows("SELECT * FROM tech_news_ai WHERE msg_id=1008")[0]
sm = json.loads(row["summary_json"])
check("points/numbers 强转为数组（字符串不落库）",
      isinstance(sm["points"], list) and sm["points"] == []
      and isinstance(sm["numbers"], list) and sm["numbers"] == [], str(sm))
check("impact 强转为对象（direction/beneficiaries 补全）",
      isinstance(sm["impact"], dict) and sm["impact"]["beneficiaries"] == []
      and sm["impact"]["direction"] == "", str(sm.get("impact")))
check("sources 给成字符串也不崩且为空", sm["sources"] == [])
check("headline 保留（行已生成）", row["status"] == 1 and sm["headline"] == "整形测试")

print("\n== 12. scrape_once 接线：抓取循环真的调了科技管线（漏挂=静默不工作）==")
_orig_tech = ea._ingest_tech
_calls = []
ea._ingest_tech = lambda msgs: _calls.append(len(msgs)) or 0
ea._ingest_crypto = lambda msgs: 0                      # 隔离另两路，不真跑
ea._ingest_stock = lambda msgs: 0
ea.event_scrape.fetch_page = lambda channel, before=None: "<html>x</html>"
ea.event_scrape.parse_messages = lambda html: [mk_msg(2001, "英伟达发布新一代芯片")]
ea.scrape_once()
check("scrape_once 里调了科技管线", _calls == [1], str(_calls))
ea._ingest_tech = _orig_tech

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
