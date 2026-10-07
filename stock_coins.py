"""美股/商品 标的识别：金十消息文本 → 币安 TradFi 永续代码（ticker）。

范围（用户 2026-10-02 定）：**美股个股 + 商品**（含港/韩股等已上市的股票类永续）；
**不做指数/ETF**（标普/纳指/道琼斯/QQQ/SPY/SOXL… 一律不映射）。

两道闸门（缺一不可）：
  ① 归一后的代码**必须在币安 TradFi 永续白名单**（market.get_tradifi_symbols）里
     —— 不在就丢（京东/百度/蔚来/波音/星巴克等未上市，自动被挡）
  ② 一条消息最多取 max_n 个（"个股要闻整理"一条能含 5~7 个标的 → 防刷屏）

重名/假阳性防护（都是实测踩过的）：
  · MetaMask / Metaverse **不是** Meta → 英文名一律用 \\b 词边界匹配
  · "黄金交叉/黄金分割" 是技术分析术语，不是金价 → 匹配前先剔除这些短语
  · 纯英文单词（V/MU/AI 这类）不裸匹配，只认 "NVDA.O / AMD.N" 这种**带交易所后缀**的写法
    （金十个股快讯就是这个格式：`英伟达(NVDA.O)`）
  · "超微"有歧义（超微电脑=SMCI / 超威半导体=AMD）→ 只认全称
"""
from __future__ import annotations

import re

import config
import market

# 名称 → ticker（中文名 + 常见英文名；同一个 ticker 多个写法都列上）
NAME_MAP: dict[str, str] = {
    # —— 科技七雄 + 半导体 ——
    "英伟达": "NVDA", "英伟达公司": "NVDA", "NVIDIA": "NVDA",
    "特斯拉": "TSLA", "Tesla": "TSLA",
    "苹果公司": "AAPL", "苹果": "AAPL", "Apple": "AAPL",
    "微软": "MSFT", "Microsoft": "MSFT",
    "谷歌": "GOOGL", "Alphabet": "GOOGL", "Google": "GOOGL",
    "亚马逊": "AMZN", "Amazon": "AMZN",
    "Meta": "META", "脸书": "META", "Facebook": "META",
    "奈飞": "NFLX", "Netflix": "NFLX",
    "英特尔": "INTC", "Intel": "INTC",
    "美光科技": "MU", "美光": "MU", "Micron": "MU",
    "高通": "QCOM", "Qualcomm": "QCOM",
    "博通": "AVGO", "Broadcom": "AVGO",
    "AMD": "AMD", "超威半导体": "AMD",
    "台积电": "TSM", "TSMC": "TSM",
    "甲骨文": "ORCL", "Oracle": "ORCL",
    "戴尔": "DELL", "Dell": "DELL",
    "超微电脑": "SMCI", "Supermicro": "SMCI", "超微半导体": "AMD",
    "ARM": "ARM", "Arm Holdings": "ARM",
    "阿斯麦": "ASML", "ASML": "ASML",
    "应用材料": "AMAT", "Applied Materials": "AMAT",
    "泛林": "LRCX", "Lam Research": "LRCX",
    "科磊": "KLAC", "KLA": "KLAC",
    "德州仪器": "TXN", "Texas Instruments": "TXN",
    "Marvell": "MRVL", "迈威尔": "MRVL",
    "赛富时": "CRM", "Salesforce": "CRM",
    "Adobe": "ADBE",
    "思科": "CSCO", "Cisco": "CSCO",
    "IBM": "IBM",
    # —— 加密相关美股 ——
    "Coinbase": "COIN", "Robinhood": "HOOD", "Palantir": "PLTR",
    "微策略": "MSTR", "MicroStrategy": "MSTR",
    "优步": "UBER", "Uber": "UBER",
    "Snowflake": "SNOW",
    "游戏驿站": "GME", "GameStop": "GME", "AMC院线": "AMC",
    # —— 消费/医药/金融 ——
    "开市客": "COST", "好市多": "COST", "Costco": "COST",
    "沃尔玛": "WMT", "Walmart": "WMT",
    "家得宝": "HD", "Home Depot": "HD",
    "可口可乐": "KO", "Coca-Cola": "KO",
    "耐克": "NKE", "Nike": "NKE",
    "迪士尼": "DIS", "Disney": "DIS",
    "Visa": "V",
    "摩根大通": "JPM", "JPMorgan": "JPM",
    "高盛": "GS", "Goldman": "GS",
    "伯克希尔": "BRKB", "Berkshire": "BRKB",
    "礼来": "LLY", "Eli Lilly": "LLY",
    "诺和诺德": "NVO", "Novo Nordisk": "NVO",
    "默克": "MRK", "Merck": "MRK",
    "联合健康": "UNH", "UnitedHealth": "UNH",
    "埃克森": "XOM", "Exxon": "XOM",
    # —— 中概/港股/韩股（同为股票类永续；用户确认收）——
    "阿里巴巴": "BABA", "Alibaba": "BABA",
    "拼多多": "PDD", "PDD": "PDD",
    "比亚迪": "BYD", "BYD": "BYD",
    "腾讯": "TENCENT", "Tencent": "TENCENT",
    "美团": "MEITUAN", "Meituan": "MEITUAN",
    "快手": "KUAISHOU", "Kuaishou": "KUAISHOU",
    "泡泡玛特": "POPMART",
    "三星电子": "SAMSUNG", "三星": "SAMSUNG", "Samsung": "SAMSUNG",
    "SK海力士": "SKHYNIX", "海力士": "SKHYNIX", "SK Hynix": "SKHYNIX",
    "现代汽车": "HYUNDAI", "现代": "HYUNDAI", "Hyundai": "HYUNDAI",
    "索尼": "SONY", "Sony": "SONY",
}

# 商品词 → ticker（"黄金"这类要防技术分析术语，见 _FALSE_POSITIVE；
# 长词必须列全：与"原油"重叠的写法只有长词在前，区间去重才能只算一次）
COMM_MAP: dict[str, str] = {
    "黄金": "XAU", "金价": "XAU", "伦敦金": "XAU", "现货黄金": "XAU",
    "白银": "XAG", "银价": "XAG",
    "铂金": "XPT", "钯金": "XPD",
    "WTI原油": "CL", "美国原油": "CL", "轻质原油": "CL", "WTI": "CL",
    "美油": "CL", "原油": "CL", "油价": "CL",
    "布伦特原油": "BZ", "布伦特": "BZ", "布油": "BZ",
    "天然气": "NATGAS", "气价": "NATGAS",
    "伦铜": "COPPER", "铜价": "COPPER", "COMEX铜": "COPPER", "铜": "COPPER",
}

# 匹配前先剔除的短语（假阳性；只影响商品词匹配，不影响公司名）
_FALSE_POSITIVE = ["黄金交叉", "黄金分割", "黄金坑", "黄金比例", "黄金时段"]

# 中文短名的"下一个字"排除表：短名是常用词前缀时会误报。
# 实测：宏观摘要「AI建设需求正在推高**通胀**」里的"高通"被当成高通(QCOM) ✓ 已抓到
_NEG_NEXT = {"高通": "胀", "现代": "化"}


def _find_cn(body: str, name: str) -> int:
    """中文名匹配（带"下一个字"排除）：返回第一个可用位置，没有 → -1。"""
    bad = _NEG_NEXT.get(name, "")
    pos = body.find(name)
    while pos >= 0:
        nxt = body[pos + len(name):pos + len(name) + 1]
        if not (bad and nxt == bad):
            return pos
        pos = body.find(name, pos + 1)
    return -1

# 明令不做的指数/ETF（写出来是为了让测试和后来人有据可查；不在映射表里所以本来就不会中）
EXCLUDED_INDEX = ["标普", "标普500", "纳指", "纳斯达克", "道琼斯", "QQQ", "SPY", "SOXX",
                  "SOXL", "TQQQ", "SQQQ", "IWM", "SMH", "XLE"]

_RE_SUFFIX = re.compile(r"(?<![A-Za-z0-9])([A-Z]{1,5})\.(?:O|N)(?![A-Za-z0-9])")   # 金十格式：英伟达(NVDA.O)


def _ascii_word_re(name: str):
    """英文名的匹配：**只挡 ASCII 字母**的前后边界。

    不能用 \\b：中文也是 re 的 word 字符，"AMD斥资"/"Coinbase比特币" 里
    `\\bAMD\\b` 匹配不到（实测踩过）。而 MetaMask 里 "Meta" 后面跟的是 M（ASCII 字母）
    → 被 (?![A-Za-z]) 挡住 ✓。"""
    return re.compile(r"(?<![A-Za-z])" + re.escape(name) + r"(?![A-Za-z])",
                      re.IGNORECASE)


def _tradable(ticker: str) -> bool:
    """必须是币安 TradFi 永续（美股/商品/港韩股），纯加密符号不算"""
    return ticker.upper() in market.get_tradifi_symbols()


def identify(text: str, max_n: int | None = None) -> list[str]:
    """消息文本 → ticker 列表（去重、按优先级+出现顺序、最多 max_n 个）。

    优先级：① 带交易所后缀的代码（NVDA.O，最准）→ ② 商品词 → ③ 中文/英文公司名。
    同一档内按出现位置。白名单不过的直接丢。
    """
    if not text:
        return []
    # 下限钳到 1：设置项被手改成 0 时不许"静默返回空"（整条管线会停摆但不报错）
    n = max(1, int(max_n if max_n is not None
                   else getattr(config, "STOCK_EVENT_MAX_TICKERS", 3)))
    hits: list[tuple[int, int, str]] = []      # (优先级, 位置, ticker)
    seen: set[str] = set()

    def _add(tk: str, prio: int, pos: int) -> None:
        tk = (tk or "").upper()
        if not tk or tk in seen or not _tradable(tk):
            return
        seen.add(tk)
        hits.append((prio, pos, tk))

    # ① 带后缀代码（NVDA.O / AMD.N）
    body = text
    for m in _RE_SUFFIX.finditer(body):
        _add(m.group(1), 0, m.start())

    # 商品词：先剔除假阳性短语再匹配；长词优先 + 区间去重
    # （"布伦特原油" 是一处引用 → 只算 BZ；不加区间去重会被 "原油" 再算一次 CL，
    #   一条新闻开出两个高度相关的仓 → 实测抓到）
    clean = body
    for fp in _FALSE_POSITIVE:
        clean = clean.replace(fp, "")
    claimed: list = []
    for name in sorted(COMM_MAP, key=len, reverse=True):
        pos = _find_cn(clean, name)
        if pos < 0:
            continue
        span = (pos, pos + len(name))
        if any(span[0] < c[1] and c[0] < span[1] for c in claimed):
            continue
        claimed.append(span)
        _add(COMM_MAP[name], 1, pos)

    # ③ 公司名：中文直接子串（带"下一个字"排除）；英文用 ASCII 字母边界（MetaMask ≠ Meta）
    for name, tk in NAME_MAP.items():
        if name.isascii():
            m = _ascii_word_re(name).search(body)
            if m:
                _add(tk, 2, m.start())
        else:
            pos = _find_cn(body, name)
            if pos >= 0:
                _add(tk, 2, pos)

    hits.sort(key=lambda x: (x[0], x[1]))
    return [t for _, _, t in hits[:n]]
