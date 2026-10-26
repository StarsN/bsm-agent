"""科技股新闻识别：拆条 + 三层识别（公司 / 行业 / 科技综合）。

与 stock_coins.py 的关系：那是"可交易标的"（币安 TradFi 白名单）识别，服务于纸盘交易；
这里是"新闻雷达"识别，**不做可交易过滤**——华为/OpenAI/长江存储等非上市一样收。

三层识别（逐层兜底，越往下越宽）：
  1. 公司层：中文名/英文名/代码/高管名（黄仁勋→英伟达）；长词优先（三星电机 ⊄ 三星）
  2. 行业层：无公司命中但命中行业词（DRAM/HBM/光刻…）→ @存储/@设备 等行业条目
  3. 综合层：只命中泛科技词（芯片/半导体/AI…）→ @科技综合 条目
护栏只控制处理节奏，三层都兜不住的消息才跳过（样本记入 meta 供补词）。

拆条（金十"编号总结"消息，真样本实测格式）：
  - 主编号 (?<!\\d)\\d{1,2}[.、．](?!\\d)：防 37.3% / 2026. / 版本号拆坏；编号紧贴前文也能拆（"答记者问2."）
  - 编号在小节标题后重启也照拆（"… 个股新闻 1. …2. …"）
  - 备用圈号 ①②③（无阿拉伯编号时）
  - 条目数不设内容上限（防漏）；仅 >30 条打 WARN 日志
"""
import re

# ---- 分类（页面分组顺序也用它）----
CAT_STORAGE = "存储/内存"
CAT_FOUNDRY = "晶圆代工"
CAT_EQUIP = "半导体设备/材料"
CAT_CHIP = "芯片设计"
CAT_DEVICE = "消费电子/终端"
CAT_SOFT = "互联网/软件"
CAT_AI = "AI/前沿科技"
CAT_MISC = "科技综合"

CATEGORIES = [CAT_STORAGE, CAT_FOUNDRY, CAT_EQUIP, CAT_CHIP,
              CAT_DEVICE, CAT_SOFT, CAT_AI, CAT_MISC]

# ---- 公司词典：alias -> (code, 显示名, 分类) ----
# code 是稳定 ASCII 标识（上市用代码、未上市用公司名），页面按 code 分组、做三元组主键。
NAME_MAP = {
    # 存储/内存
    "闪迪": ("SNDK", "闪迪", CAT_STORAGE),
    "SanDisk": ("SNDK", "闪迪", CAT_STORAGE),
    "美光": ("MU", "美光", CAT_STORAGE),
    "Micron": ("MU", "美光", CAT_STORAGE),
    "SK海力士": ("HYNIX", "SK海力士", CAT_STORAGE),
    "海力士": ("HYNIX", "SK海力士", CAT_STORAGE),
    "Hynix": ("HYNIX", "SK海力士", CAT_STORAGE),
    "三星电子": ("SAMSUNG", "三星电子", CAT_STORAGE),
    "三星": ("SAMSUNG", "三星电子", CAT_STORAGE),
    "Samsung": ("SAMSUNG", "三星电子", CAT_STORAGE),
    "铠侠": ("KIOXIA", "铠侠", CAT_STORAGE),
    "Kioxia": ("KIOXIA", "铠侠", CAT_STORAGE),
    "西部数据": ("WDC", "西部数据", CAT_STORAGE),
    "西数": ("WDC", "西部数据", CAT_STORAGE),
    "Western Digital": ("WDC", "西部数据", CAT_STORAGE),
    "希捷": ("STX", "希捷", CAT_STORAGE),
    "Seagate": ("STX", "希捷", CAT_STORAGE),
    "长江存储": ("YMTC", "长江存储", CAT_STORAGE),
    "长存": ("YMTC", "长江存储", CAT_STORAGE),
    "YMTC": ("YMTC", "长江存储", CAT_STORAGE),
    "兆易创新": ("GIGADEVICE", "兆易创新", CAT_STORAGE),
    "南亚科": ("NANYA", "南亚科", CAT_STORAGE),
    "南亚科技": ("NANYA", "南亚科", CAT_STORAGE),
    "Nanya": ("NANYA", "南亚科", CAT_STORAGE),
    "华邦电子": ("WINBOND", "华邦电子", CAT_STORAGE),
    "旺宏": ("MACRONIX", "旺宏", CAT_STORAGE),
    "Macronix": ("MACRONIX", "旺宏", CAT_STORAGE),
    # 晶圆代工
    "台积电": ("TSM", "台积电", CAT_FOUNDRY),
    "台積電": ("TSM", "台积电", CAT_FOUNDRY),
    "TSMC": ("TSM", "台积电", CAT_FOUNDRY),
    "中芯国际": ("SMIC", "中芯国际", CAT_FOUNDRY),
    "中芯": ("SMIC", "中芯国际", CAT_FOUNDRY),
    "SMIC": ("SMIC", "中芯国际", CAT_FOUNDRY),
    "格芯": ("GFS", "格芯", CAT_FOUNDRY),
    "GlobalFoundries": ("GFS", "格芯", CAT_FOUNDRY),
    "联电": ("UMC", "联电", CAT_FOUNDRY),
    "联华电子": ("UMC", "联电", CAT_FOUNDRY),
    "力积电": ("PSMC", "力积电", CAT_FOUNDRY),
    # 半导体设备/材料
    "阿斯麦": ("ASML", "阿斯麦", CAT_EQUIP),
    "ASML": ("ASML", "阿斯麦", CAT_EQUIP),
    "应用材料": ("AMAT", "应用材料", CAT_EQUIP),
    "Applied Materials": ("AMAT", "应用材料", CAT_EQUIP),
    "泛林": ("LRCX", "泛林", CAT_EQUIP),
    "Lam Research": ("LRCX", "泛林", CAT_EQUIP),
    "科天": ("KLAC", "科天", CAT_EQUIP),
    "KLA": ("KLAC", "科天", CAT_EQUIP),
    "东京电子": ("TEL", "东京电子", CAT_EQUIP),
    "爱德万": ("ADVANTEST", "爱德万", CAT_EQUIP),
    "Advantest": ("ADVANTEST", "爱德万", CAT_EQUIP),
    "TOWA": ("TOWA", "TOWA", CAT_EQUIP),
    "三星电机": ("SEMCO", "三星电机", CAT_EQUIP),
    "东芝": ("TOSHIBA", "东芝", CAT_EQUIP),
    "Toshiba": ("TOSHIBA", "东芝", CAT_EQUIP),
    "TDK": ("TDK", "TDK", CAT_EQUIP),
    "中微公司": ("AMEC", "中微公司", CAT_EQUIP),
    "北方华创": ("NAURA", "北方华创", CAT_EQUIP),
    # 芯片设计
    "英伟达": ("NVDA", "英伟达", CAT_CHIP),
    "辉达": ("NVDA", "英伟达", CAT_CHIP),
    "NVIDIA": ("NVDA", "英伟达", CAT_CHIP),
    "NVDA": ("NVDA", "英伟达", CAT_CHIP),
    "黄仁勋": ("NVDA", "英伟达", CAT_CHIP),
    "AMD": ("AMD", "AMD", CAT_CHIP),
    "苏姿丰": ("AMD", "AMD", CAT_CHIP),
    "超威半导体": ("AMD", "AMD", CAT_CHIP),
    "超微电脑": ("SMCI", "超微电脑", CAT_CHIP),
    "Supermicro": ("SMCI", "超微电脑", CAT_CHIP),
    "英特尔": ("INTC", "英特尔", CAT_CHIP),
    "Intel": ("INTC", "英特尔", CAT_CHIP),
    "高通": ("QCOM", "高通", CAT_CHIP),
    "Qualcomm": ("QCOM", "高通", CAT_CHIP),
    "博通": ("AVGO", "博通", CAT_CHIP),
    "Broadcom": ("AVGO", "博通", CAT_CHIP),
    "联发科": ("MTK", "联发科", CAT_CHIP),
    "MediaTek": ("MTK", "联发科", CAT_CHIP),
    "Arm": ("ARM", "Arm", CAT_CHIP),
    "安谋": ("ARM", "Arm", CAT_CHIP),
    "Marvell": ("MRVL", "Marvell", CAT_CHIP),
    "迈威尔": ("MRVL", "Marvell", CAT_CHIP),
    "寒武纪": ("CAMBRICON", "寒武纪", CAT_CHIP),
    "英飞凌": ("INFINEON", "英飞凌", CAT_CHIP),
    "Infineon": ("INFINEON", "英飞凌", CAT_CHIP),
    "华为海思": ("HISILICON", "华为海思", CAT_CHIP),
    "海思": ("HISILICON", "华为海思", CAT_CHIP),
    "HiSilicon": ("HISILICON", "华为海思", CAT_CHIP),
    # 消费电子/终端
    "苹果": ("AAPL", "苹果", CAT_DEVICE),
    "Apple": ("AAPL", "苹果", CAT_DEVICE),
    "AAPL": ("AAPL", "苹果", CAT_DEVICE),
    "库克": ("AAPL", "苹果", CAT_DEVICE),
    "华为": ("HUAWEI", "华为", CAT_DEVICE),
    "Huawei": ("HUAWEI", "华为", CAT_DEVICE),
    "余承东": ("HUAWEI", "华为", CAT_DEVICE),
    "小米": ("XIAOMI", "小米", CAT_DEVICE),
    "Xiaomi": ("XIAOMI", "小米", CAT_DEVICE),
    "OPPO": ("OPPO", "OPPO", CAT_DEVICE),
    "vivo": ("VIVO", "vivo", CAT_DEVICE),
    "荣耀": ("HONOR", "荣耀", CAT_DEVICE),
    "索尼": ("SONY", "索尼", CAT_DEVICE),
    "Sony": ("SONY", "索尼", CAT_DEVICE),
    "LG": ("LG", "LG", CAT_DEVICE),
    "联想": ("LENOVO", "联想", CAT_DEVICE),
    "Lenovo": ("LENOVO", "联想", CAT_DEVICE),
    "戴尔": ("DELL", "戴尔", CAT_DEVICE),
    "Dell": ("DELL", "戴尔", CAT_DEVICE),
    "惠普": ("HPQ", "惠普", CAT_DEVICE),
    "传音": ("TRANSSION", "传音控股", CAT_DEVICE),
    "传音控股": ("TRANSSION", "传音控股", CAT_DEVICE),
    # 互联网/软件
    "微软": ("MSFT", "微软", CAT_SOFT),
    "Microsoft": ("MSFT", "微软", CAT_SOFT),
    "MSFT": ("MSFT", "微软", CAT_SOFT),
    "谷歌": ("GOOGL", "谷歌", CAT_SOFT),
    "Google": ("GOOGL", "谷歌", CAT_SOFT),
    "Alphabet": ("GOOGL", "谷歌", CAT_SOFT),
    "亚马逊": ("AMZN", "亚马逊", CAT_SOFT),
    "Amazon": ("AMZN", "亚马逊", CAT_SOFT),
    "Meta": ("META", "Meta", CAT_SOFT),
    "脸书": ("META", "Meta", CAT_SOFT),
    "Facebook": ("META", "Meta", CAT_SOFT),
    "甲骨文": ("ORCL", "甲骨文", CAT_SOFT),
    "Oracle": ("ORCL", "甲骨文", CAT_SOFT),
    "阿里巴巴": ("BABA", "阿里巴巴", CAT_SOFT),
    "阿里": ("BABA", "阿里巴巴", CAT_SOFT),
    "Alibaba": ("BABA", "阿里巴巴", CAT_SOFT),
    "腾讯": ("TENCENT", "腾讯", CAT_SOFT),
    "Tencent": ("TENCENT", "腾讯", CAT_SOFT),
    "百度": ("BIDU", "百度", CAT_SOFT),
    "Baidu": ("BIDU", "百度", CAT_SOFT),
    "奈飞": ("NFLX", "奈飞", CAT_SOFT),
    "Netflix": ("NFLX", "奈飞", CAT_SOFT),
    "思科": ("CSCO", "思科", CAT_SOFT),
    "Cisco": ("CSCO", "思科", CAT_SOFT),
    # AI/前沿科技（含未上市）
    "OpenAI": ("OPENAI", "OpenAI", CAT_AI),
    "DeepSeek": ("DEEPSEEK", "DeepSeek", CAT_AI),
    "深度求索": ("DEEPSEEK", "DeepSeek", CAT_AI),
    "Anthropic": ("ANTHROPIC", "Anthropic", CAT_AI),
    "xAI": ("XAI", "xAI", CAT_AI),
    "字节跳动": ("BYTEDANCE", "字节跳动", CAT_AI),
    "字节": ("BYTEDANCE", "字节跳动", CAT_AI),
    "ByteDance": ("BYTEDANCE", "字节跳动", CAT_AI),
    "月之暗面": ("MOONSHOT", "月之暗面", CAT_AI),
    "Moonshot": ("MOONSHOT", "月之暗面", CAT_AI),
    "Kimi": ("MOONSHOT", "月之暗面", CAT_AI),
    "Mistral": ("MISTRAL", "Mistral AI", CAT_AI),
    "智谱": ("ZHIPU", "智谱", CAT_AI),
    "MiniMax": ("MINIMAX", "MiniMax", CAT_AI),
    "科大讯飞": ("IFLYTEK", "科大讯飞", CAT_AI),
    "讯飞": ("IFLYTEK", "科大讯飞", CAT_AI),
    "商汤": ("SENSETIME", "商汤", CAT_AI),
    "大疆": ("DJI", "大疆", CAT_AI),
    "宇树": ("UNITREE", "宇树", CAT_AI),
    "特斯拉": ("TSLA", "特斯拉", CAT_AI),
    "Tesla": ("TSLA", "特斯拉", CAT_AI),
}

# 歧义排除：匹配后紧跟这些字/词 → 不算（真实误伤：三星堆考古、苹果是水果、库克群岛）
_EXCL_NEXT = {
    "三星": ["堆"],
    "苹果": ["园", "汁", "树", "丰收", "种植", "果农"],
    "小米": ["粥", "种植", "产区", "丰收", "粮食"],
    "库克": ["群岛"],
    "高通": ["胀", "膨"],
}
# 上下文限定：命中的别名前后 ±12 字内必须出现其中之一（常用词误伤防护）
_CTX_REQUIRED = {
    "荣耀": ["手机", "CEO", "发布", "赵明", "终端", "新机", "IPO", "上市", "品牌",
             "Magic", "折叠屏", "股价"],
    # 实测误伤：金十"交易应用 Moonshot 新增支持 Robinhood 链"（加密交易 app）≠ 月之暗面；
    # Kimi 也可能是人名 → 两者都要求 AI 语境
    "Moonshot": ["月之暗面", "AI", "模型", "大模型", "智能", "Kimi"],
    "Kimi": ["月之暗面", "AI", "模型", "大模型", "Moonshot", "智能"],
    # "联想"是常用词（"让人联想到/引发联想"）→ 要求科技语境
    "联想": ["手机", "PC", "电脑", "集团", "CEO", "杨元庆", "股价", "发布", "新品",
             "平板", "笔记本", "摩托罗拉", "财报", "业绩", "服务器"],
    # "月之暗面"也是月球/科幻常用语（探测语境）→ 要求公司语境
    "月之暗面": ["公司", "融资", "估值", "模型", "大模型", "AI", "Kimi", "Moonshot",
                 "IPO", "发布", "智能"],
}

# 行业层：code -> (显示名, 分类, 关键词)
INDUSTRY_MAP = {
    "@存储": ("存储行业", CAT_STORAGE, ["DRAM", "NAND", "HBM", "存储", "闪存", "内存"]),
    "@设备": ("半导体设备行业", CAT_EQUIP, ["光刻", "刻蚀", "半导体设备", "晶圆设备", "沉积"]),
    "@封装": ("先进封装行业", CAT_EQUIP, ["CoWoS", "先进封装"]),
    "@AI算力": ("AI 算力", CAT_AI, ["AI芯片", "AI服务器", "GPU", "算力"]),
}

# 综合层（兜底，防漏）：只命中这些泛科技词 → @科技综合
MISC_WORDS = ["芯片", "半导体", "晶圆", "集成电路", "光刻", "显卡", "服务器",
              "科技", "算力", "AI", "GPU", "存储"]

MISC_ENTRY = {"code": "@科技综合", "name": "科技综合动态", "cat": CAT_MISC}

_ALIASES = sorted(NAME_MAP.keys(), key=len, reverse=True)


def _is_ascii(s: str) -> bool:
    return s.isascii()


def _ascii_re(alias: str):
    # 与 stock_coins 同款：ASCII 字母 lookaround（\b 对 CJK 失效；顺带保住 MetaMask ≠ Meta）
    return re.compile(r"(?<![A-Za-z])" + re.escape(alias) + r"(?![A-Za-z])", re.I)


_ASCII_RES = {a: _ascii_re(a) for a in _ALIASES if _is_ascii(a)}


def _find_all(text: str, alias: str) -> list:
    if _is_ascii(alias):
        return [(m.start(), m.end()) for m in _ASCII_RES[alias].finditer(text)]
    out, start = [], 0
    while True:
        i = text.find(alias, start)
        if i < 0:
            return out
        out.append((i, i + len(alias)))
        start = i + 1


def _passes_guards(text: str, alias: str, s: int, e: int) -> bool:
    for excl in _EXCL_NEXT.get(alias, ()):     # 排除词：紧跟其后出现则不算
        if text.startswith(excl, e):
            return False
    ctx = _CTX_REQUIRED.get(alias)
    if ctx and not any(w in text[max(0, s - 12):e + 12] for w in ctx):
        return False
    return True


def identify(text: str) -> list:
    """文本 → 标的列表 [{"code","name","cat"}]（三层兜底；全部不中返回 []）。"""
    text = text or ""
    hits = []
    for alias in _ALIASES:
        code, name, cat = NAME_MAP[alias]
        for s, e in _find_all(text, alias):
            if _passes_guards(text, alias, s, e):
                hits.append((s, e, code, name, cat))
    # 重叠消解：位置靠前优先、同位置长者优先（三星电机 ⊄ 三星）；同标的去重
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    chosen, last_end, codes = [], -1, set()
    for h in hits:
        if h[0] < last_end:
            continue
        if h[2] in codes:          # 同一标的（如 AMD + 苏姿丰）只记一次，但推进边界
            last_end = max(last_end, h[1])
            continue
        chosen.append(h)
        codes.add(h[2])
        last_end = h[1]
    if chosen:
        if len(chosen) > 10:
            print(f"[tech] [WARN] 单条识别 {len(chosen)} 个标的（>10），疑似词典误伤: "
                  f"{[c[2] for c in chosen]}")
        return [{"code": c, "name": n, "cat": t} for _, _, c, n, t in chosen]
    # 行业层
    for code, (name, cat, words) in INDUSTRY_MAP.items():
        if any(_kw_hit(text, w) for w in words):
            return [{"code": code, "name": name, "cat": cat}]
    # 综合层（兜底）
    if any(_kw_hit(text, w) for w in MISC_WORDS):
        return [dict(MISC_ENTRY)]
    return []


def _kw_hit(text: str, word: str) -> bool:
    if _is_ascii(word):
        return re.search(r"(?<![A-Za-z])" + re.escape(word) + r"(?![A-Za-z])",
                         text, re.I) is not None
    return word in text


# ---- 拆条 ----

# 主编号：前面不是数字（防 37.3 被从 "3." 处拆）、后面不是数字（防 1.5亿/版本号）
_MARK_RE = re.compile(r"(?<!\d)\d{1,2}[.、．](?!\d)")
_CIRC_RE = re.compile(r"[①②③④⑤⑥⑦⑧⑨⑩⑪⑫]")
# 全角括号编号"（1）（2）"（两侧括号都全角才认——不会误伤数字里的半角括号）
_PAREN_RE = re.compile(r"（\d{1,2}）")
# 小节标题碎屑（真样本实测：抓取管道会把标题尾的 emoji 损坏成 é/ï 等，[^。！？，,；;] 宽容 0~3 字）
_TAIL_JUNK = re.compile(
    r"(重要新闻?|个股新闻?|国内新闻?|国际新闻?|财经新闻?|市场快讯|今日要闻|热点新闻|要闻|快讯)"
    r"[^。！？!?，,；;]{0,3}$")


def split_items(text: str) -> list:
    """金十消息 → 条目列表。≥2 个编号标记才拆；否则整条当 1 条。
    三种编号排版依序尝试：阿拉伯数字 → ①②③ → 全角括号（N）。
    条目全部保留（不设内容上限，防漏消息）；>30 条打 WARN。"""
    text = (text or "").strip()
    if not text:
        return []
    spans = None
    for rx in (_MARK_RE, _CIRC_RE, _PAREN_RE):
        found = list(rx.finditer(text))
        if len(found) >= 2:
            spans = [(m.start(), m.end()) for m in found]
            break
    if spans is None:
        return [text]
    items = []
    for i, (_s, e) in enumerate(spans):
        end = spans[i + 1][0] if i + 1 < len(spans) else len(text)
        seg = text[e:end].strip()
        for _ in range(3):          # 尾部小节标题碎屑（如 "…低配状态。 个股新é"）
            new = _TAIL_JUNK.sub("", seg).strip()
            if new == seg:
                break
            seg = new
        if len(seg) >= 2:
            items.append(seg)
    if not items:
        return [text]
    if len(items) > 30:
        print(f"[tech] [WARN] 单条消息拆出 {len(items)} 条（>30），疑似超长总结或拆分器误伤，"
              f"仍全部保留: {text[:60]}…")
    return items
