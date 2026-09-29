"""加密事件：从快讯正文里识别**币种**（英文 ticker + 中文别名）。

两级识别，缺一不可：

  ① **候选**：英文 = 独立的大写 2~6 字母 token（前后不紧贴字母数字）；
     中文 = 别名表（比特币/以太坊/狗狗币…）
  ② **验证**：候选必须落在**可交易集合**里（= 币安合约能下单的币，由调用方传入）

为什么要第二级：正文里的 `BGBTC`、`ETF`、`API` 这类大写 token 长得跟 ticker 一样，
但不会是币 → 只有真在币安合约列表里的才算。这样**新上市的币能自动认出来**，
不用维护英文白名单。

已知边界：**英文只认大写**（`uni` 这种小写不认）——小写会撞上 near / link / cake
这类普通英文词，误报远比漏报糟。中文别名表在下面按需扩。
"""
import re

# 中文别名 → ticker。只放**有把握**的常见写法；宁可漏，不要放模棱两可的
# （放错一条会天天误触发开仓）。新币的中文名等出现了再加。
CN_ALIASES = {
    "比特币": "BTC", "大饼": "BTC",
    "以太坊": "ETH", "以太币": "ETH",
    "索拉纳": "SOL",
    "币安币": "BNB",
    "瑞波币": "XRP", "瑞波": "XRP",
    "狗狗币": "DOGE", "狗币": "DOGE",
    "艾达币": "ADA", "卡尔达诺": "ADA",
    "雪崩": "AVAX",
    "链环": "LINK",
    "莱特币": "LTC",
    "比特现金": "BCH", "比特币现金": "BCH",
    "波场": "TRX",
    "波卡": "DOT",
    "柚子币": "EOS", "柚子": "EOS",
    "门罗币": "XMR",
    "马蹄": "MATIC",
    "猿币": "APE",
    "寿司币": "SUSHI",
    "佩佩": "PEPE",
    "以太经典": "ETC", "以太坊经典": "ETC",
    "恒星币": "XLM",
    "小蚁": "NEO",
    "达世币": "DASH",
    "零币": "ZEC", "大零币": "ZEC",
    "唯链": "VET",
    "阿童木": "ATOM",
    "特朗普币": "TRUMP",
    "塞莱斯蒂亚": "TIA",
    "超流动": "HYPE",
    "木星": "JUP",
}

_TICKER_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,6})(?![A-Za-z0-9])")


def english_candidates(text: str) -> list[str]:
    """正文里所有**独立的大写 2~6 字母 token**（未验证，按出现顺序去重）。"""
    out, seen = [], set()
    for m in _TICKER_RE.finditer(text or ""):
        t = m.group(1)
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def chinese_candidates(text: str) -> list[str]:
    """中文别名命中的 ticker（**长词优先**：先匹配"比特币现金"再匹配"比特币"）。"""
    out, seen = [], set()
    t = text or ""
    for alias in sorted(CN_ALIASES, key=len, reverse=True):
        if alias in t:
            tk = CN_ALIASES[alias]
            if tk not in seen:
                seen.add(tk)
                out.append(tk)
    return out


def recognize(text: str, tradable: set) -> list[str]:
    """文本里出现、且**可交易**的币种（英文 ticker + 中文别名，合并去重保序）。

    tradable：可交易集合（大写的 ticker 集合，如币安合约的 symbol 去掉 USDT）。
    传入空集合 → 只认中文别名（调用方没拿到交易所列表时的降级，别反过来把英文全放行）。"""
    out, seen = [], set()
    for tk in chinese_candidates(text):
        if tk not in seen:
            seen.add(tk)
            out.append(tk)
    for tk in english_candidates(text):
        if tk in seen or tk not in tradable:
            continue
        seen.add(tk)
        out.append(tk)
    return out
