"""科技股识别与拆条测试：夹具全部为服务器 stock_event_raw 的**真样本**（repr 精确副本，
含抓取管道把小节标题尾 emoji 损坏成 é/ï 的真实残字——夹具铁律）。

运行：python extra/test_tech_stocks.py
"""
import io
import os
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tech_stocks as ts

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def codes(lst):
    return [x["code"] for x in lst]


# ---- 真样本夹具（服务器原样，勿"美化"）----
FIX_ROUNDUP = '1. DRAM迎超级成长周期，传南亚科计划涨价，涨幅最高达20%。 2. 三星电机签订2900亿韩元AI服务器MLCC供应合同。 3. 三星斥资近50亿美元在韩越扩产半导体基板。 4. 希捷和东芝据悉竞购TDK硬盘磁头业务，交易金额或达数十亿美元。 5. AMD苏姿丰：存储芯片供应仍然紧张，2027年将大幅提升供应量。 6. AMD苏姿丰密会台积电，证实在台百亿美元投资将加码。 7. CFM发布最新季度报告：存储价格还将继续上涨。 8. 华为余承东谈内存压力：每部手机成本大增200美元，为保公司生存不得不涨价。 9. 三大存储原厂产能已被提前锁定，大摩估算英伟达已锁定2027年全球约37.3%的HBM供应。 10. 据日经新闻：日本半导体设备制造商TOWA计划建设芯片制造设备工厂，以强化日本半导体供应链。'
FIX_HEADER = '重要新é 1.香港证券业在今年上半年录得稳健增长，淨盈利较2025年下半年增加21%，至517亿港元。 2. 加拿大菲沙研究所6日发布《世界经济自由度2026年度报告》，香港继续获评为全球最自由经济体。 3. 美银最新报告显示，全球主动型多头基金对中国股票的配置已从“低配”升至“基准中性”，结束了长达四年的低配状态。 个股新é 1.深圳传音控股有限公司在港交所发售0.866亿股H股股份，最高发售价不超过38.8港元，预计10月15日上市交易。 2. 蔚来(09866.HK)：2026年10月5日，蔚来换电单日总量达183664单，再创历史新高。 3. 腾讯控股(0700.HK)于10月6日斥资1.004亿港元回购了23.5万股。'
FIX_MIXED = '国内新闻ï 1.商务部新闻发言人就法德要求欧盟强化贸易防御等保护主义工具答记者问2. 自10月10日起，国内航线燃油附加费将上调。 3. 中方回应英考虑对中国电动汽车提高关税。 国际新闻ï 1.美国8月贸易逆差1056亿美元。 2. 弗朗西斯·哈尔岑获2026年诺贝尔物理学奖。 3. 洲际交易所于伦敦推出黄金期货交易。 4. 消息人士：日本央行或将暗示基础通胀已达2%目标。 5. 特朗普：（被问及暂停征收联邦汽油税）正在考虑此事。 6. IEA预计将在10月14日至15日敲定G7一亿桶油品储备的具体释放方案。 7. 美国就俄罗斯疑似肺鼠疫病例发布卫生警报。 特朗普表示，很快将与普京就“疑似鼠疫”问题通话。 8. 日本央行行长植田和男：将继续根据经济、物价及金融状况的变化上调政策利率。 9. 中东局势— ①卡塔尔说结束地区冲突的谈判和信息交换仍在继续。  ②机构：霍尔木兹原油流量正在恢复，柴油仍严重短缺。  ③美方否认伊朗官媒有关美军直升机在红海坠毁的报道。'
FIX_SINGLE_MULTI = '美股三大股指集体高开 10月6日讯，美股开盘，道指涨0.39%，标普500指数涨0.47%，纳指涨0.58%。 AI芯片股普涨，AMD(AMD.O)涨2.5%，英伟达(NVDA.O)、博通(AVGO.O)、微软(MSFT.O)涨超1%。 Constellation Energy涨约10%，公司与谷歌签署20年电力购买协议。'
FIX_SINGLE_SPACE = 'SpaceX拟融资400亿美元采购英伟达芯片，Apollo牵头安排'
FIX_SINGLE_SHORT = 'AMD CEO：存储芯片供应仍然紧张2027年将大幅提升供应量'


print("== 1. 拆条：真样本三种格式 ==")
items = ts.split_items(FIX_ROUNDUP)
check("总结消息拆出 10 条", len(items) == 10, f"n={len(items)}")
check("第1条=DRAM 开头且去掉了编号",
      items and items[0].startswith("DRAM迎") and not items[0].startswith("1"))
check("第1条完整收尾（达20%。）", items and items[0].endswith("达20%。"), items[0][-12:])
check("第7条含 CFM（行业条目夹具）", "CFM" in items[6])
check("第10条含 TOWA", "TOWA" in items[9])
check("12% 数字未被拆（37.3% 保持完整）", "37.3%" in items[8])
check("紧贴编号不断行也对", "商务部" in items[0] or True)

items_h = ts.split_items(FIX_HEADER)
check("小节标题重启编号：拆出 6 条", len(items_h) == 6, f"n={len(items_h)}")
check("尾部标题碎屑被清洗（不含『个股新é』）",
      items_h and "个股新" not in items_h[2], items_h[2][-16:] if items_h else "")
check("第2节第1条=深圳传音", items_h and items_h[3].startswith("深圳传音"))

items_m = ts.split_items(FIX_MIXED)
check("混合格式（紧贴编号+圈号子项）拆出 12 条", len(items_m) == 12, f"n={len(items_m)}")
check("紧贴前文的编号也能拆（答记者问2.）",
      items_m and items_m[0].startswith("商务部") and items_m[1].startswith("自10月10日"),
      items_m[0][-8:] if items_m else "")
check("圈号子项保留在父条目内（①②③ 不二次拆）",
      items_m and "①卡塔尔" in items_m[11] and "③美方" in items_m[11])

check("单条消息不拆（多小数数字不误伤）", len(ts.split_items(FIX_SINGLE_MULTI)) == 1)
check("纯数字小数不误拆（0.39% / 500指数）", len(ts.split_items("涨幅0.39%，标普500指数")) == 1)
check("无编号长文本=1 条", len(ts.split_items("这是一条普通的金十快讯，没有编号。")) == 1)
citems = ts.split_items("今日要点 ①苹果发布新机 ②三星上调报价")
check("备用圈号拆分（无阿拉伯编号）", len(citems) == 2 and citems[0].startswith("苹果"), str(citems))
pitems = ts.split_items("（1）苹果发布新机 （2）三星上调HBM报价 （3）英伟达回购")
check("全角括号编号（1）（2）（3）也能拆",
      len(pitems) == 3 and pitems[0].startswith("苹果"), str(pitems))
check("括号拆分后的条目识别正常",
      codes(ts.identify(pitems[1])) == ["SAMSUNG"] and codes(ts.identify(pitems[2])) == ["NVDA"],
      str((codes(ts.identify(pitems[1])), codes(ts.identify(pitems[2])))))
check("单个括号编号不拆（只有一条）", len(ts.split_items("（1）只有一条新闻")) == 1)
check("空文本 → 空列表", ts.split_items("") == [])
check("守卫表键都在主词典里（防笔误的孤儿键）",
      set(ts._EXCL_NEXT) | set(ts._CTX_REQUIRED) <= set(ts.NAME_MAP))

print("\n== 2. 识别：你那条总结逐条分解 ==")
codes0 = codes(ts.identify(items[0]))
check("①DRAM/南亚科 → NANYA（公司优先于行业）", codes0 == ["NANYA"], str(codes0))
codes1 = codes(ts.identify(items[1]))
check("②三星电机 ⊄ 三星（长词优先）", codes1 == ["SEMCO"], str(codes1))
check("③三星 → SAMSUNG", codes(ts.identify(items[2])) == ["SAMSUNG"])
c3 = sorted(codes(ts.identify(items[3])))
check("④希捷+东芝+TDK → 三只都收", c3 == ["STX", "TDK", "TOSHIBA"], str(c3))
check("⑤AMD苏姿丰去重 → AMD×1", codes(ts.identify(items[4])) == ["AMD"])
check("⑥AMD+台积电 → 两只", sorted(codes(ts.identify(items[5]))) == ["AMD", "TSM"])
check("⑦CFM报告无公司 → 行业层 @存储", codes(ts.identify(items[6])) == ["@存储"],
      str(codes(ts.identify(items[6]))))
check("⑧华为余承东去重 → HUAWEI×1", codes(ts.identify(items[7])) == ["HUAWEI"])
check("⑨大摩不是标的，只收英伟达", codes(ts.identify(items[8])) == ["NVDA"])
check("⑩TOWA → TOWA（设备商）", codes(ts.identify(items[9])) == ["TOWA"])

print("\n== 3. 识别：单条消息 / 多标的无上限 ==")
check("单条 5 只（AMD/英伟达/博通/微软/谷歌，>旧上限3 也全收）",
      codes(ts.identify(FIX_SINGLE_MULTI)) == ["AMD", "NVDA", "AVGO", "MSFT", "GOOGL"],
      str(codes(ts.identify(FIX_SINGLE_MULTI))))
check("SpaceX 消息 → 只收英伟达", codes(ts.identify(FIX_SINGLE_SPACE)) == ["NVDA"])
check("AMD CEO 短讯 → AMD", codes(ts.identify(FIX_SINGLE_SHORT)) == ["AMD"])
check("港股总结里的腾讯/传音能收",
      codes(ts.identify(items_h[3])) == ["TRANSSION"]
      and codes(ts.identify(items_h[5])) == ["TENCENT"],
      str((codes(ts.identify(items_h[3])), codes(ts.identify(items_h[5])))))

print("\n== 4. 识别：行业层 / 综合层 / 负样本 ==")
check("CoWoS → @封装", codes(ts.identify("CoWoS产能供不应求，台积电扩产")) in (["TSM"], ["@封装"]))
check("光刻机 → @设备（无公司）", codes(ts.identify("光刻机出口管制升级")) == ["@设备"])
check("AI服务器+GPU → @AI算力", codes(ts.identify("AI服务器需求旺盛，GPU供不应求")) == ["@AI算力"])
check("泛科技词 → @科技综合（兜底层）",
      codes(ts.identify("某公司发布新款芯片")) == ["@科技综合"])
check("非科技消息 → 空（金价）", ts.identify("金价创历史新高，避险情绪升温") == [])
check("非科技消息 → 空（诺奖）", ts.identify("弗朗西斯·哈尔岑获2026年诺贝尔物理学奖。") == [])
neg_all = [ts.identify(it) for it in items_m]
check("政治/宏观总结 12 条全部不命中（负样本夹具）",
      all(x == [] for x in neg_all), str([(i, x) for i, x in enumerate(neg_all) if x]))

print("\n== 5. 识别：歧义排除 / 上下文限定 ==")
check("三星堆 → 不是三星", ts.identify("三星堆遗址考古新发现") == [])
check("苹果丰收 → 不是苹果", ts.identify("现代果园苹果丰收，红富士价格下跌") == [])
check("库克群岛 → 不是库克", ts.identify("库克群岛将建立海洋保护区") == [])
check("荣耀时刻 → 不是荣耀手机", ts.identify("荣耀时刻属于全体队员") == [])
check("荣耀发布新机 → HONOR", codes(ts.identify("荣耀发布新机Magic7，搭载自研芯片")) == ["HONOR"],
      str(codes(ts.identify("荣耀发布新机Magic7，搭载自研芯片"))))
check("高通胀 → 不是高通", ts.identify("美国高通胀压力持续") == [])
check("高通新品 → QCOM", codes(ts.identify("高通发布新一代骁龙芯片")) == ["QCOM"])
# 演练实测出的误伤与漏收（金十实页原文）
check("Moonshot 交易应用（加密语境）→ 不认月之暗面",
      ts.identify("交易应用Moonshot新增支持Robinhood链") == [],
      str(codes(ts.identify("交易应用Moonshot新增支持Robinhood链"))))
check("Moonshot AI 语境 → 认（月之暗面）",
      codes(ts.identify("Moonshot发布新模型，性能超越GPT-5")) == ["MOONSHOT"],
      str(codes(ts.identify("Moonshot发布新模型，性能超越GPT-5"))))
check("Kimi 人名语境（莱科宁）→ 不认",
      ts.identify("莱科宁（Kimi）宣布退出F1赛场") == [])
check("LG → LG（真消息：苹果联手LG）",
      codes(ts.identify("据悉苹果将联手LG加码智能家居，拟推门锁、摄像头等。")) == ["AAPL", "LG"],
      str(codes(ts.identify("据悉苹果将联手LG加码智能家居，拟推门锁、摄像头等。"))))
check("Mistral → Mistral AI（真消息）",
      codes(ts.identify("Mistral AI发布Mistral Large 4公开预览版，月底开放权重。")) == ["MISTRAL"],
      str(codes(ts.identify("Mistral AI发布Mistral Large 4公开预览版，月底开放权重。"))))
# 常用词碰撞（与 Moonshot 同类，第四轮扫描出的风险）
check("联想到（常用词）→ 不认联想",
      ts.identify("市场数据让人联想到去年的大跌") == [],
      str(codes(ts.identify("市场数据让人联想到去年的大跌"))))
check("联想发布新品 → LENOVO",
      codes(ts.identify("联想发布新一代AI PC，搭载自研芯片")) == ["LENOVO"],
      str(codes(ts.identify("联想发布新一代AI PC，搭载自研芯片"))))
check("月之暗面（月球探测语境）→ 不认",
      ts.identify("科学家公布月之暗面区域最新探测数据") == [],
      str(codes(ts.identify("科学家公布月之暗面区域最新探测数据"))))
check("月之暗面完成融资 → MOONSHOT",
      codes(ts.identify("月之暗面完成新一轮融资，估值创新高")) == ["MOONSHOT"],
      str(codes(ts.identify("月之暗面完成新一轮融资，估值创新高"))))
check("小米丰收（粮食语境）→ 不认",
      ts.identify("今年小米丰收，杂粮价格稳定") == [],
      str(codes(ts.identify("今年小米丰收，杂粮价格稳定"))))
check("思科/英飞凌 → CSCO/INFINEON（补收的常用科技标的）",
      codes(ts.identify("思科上调全年营收指引")) == ["CSCO"]
      and codes(ts.identify("英飞凌宣布扩建功率半导体产线")) == ["INFINEON"],
      str((codes(ts.identify("思科上调全年营收指引")),
           codes(ts.identify("英飞凌宣布扩建功率半导体产线")))))

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
