# 外部数据源清单：百度财经 + AICoin + Telegram 金十（2026-09-21/22 实测整理）

抓取目标：可能影响金价 / 美股 / 加密的**美国经济数据**、**美股财报**、**美股快讯**、
**美股个股资金流**、**加密快讯**、**实时快讯（金十）**。

百度 / AICoin 那些是**站内私有接口**（不是开放平台），无文档、无鉴权、随时可能变；
Telegram 那条走公开网页预览（`t.me/s/`），同样随时可能变。
下面每条都标注了**实测结论**和**坑**——坑是踩过的，别再踩。

---

# 一、百度财经（finance.baidu.com）

## 0. 通用

- 数据域名：`https://finance.pae.baidu.com`（页面域名是 `finance.baidu.com`，只是壳）
- **无鉴权**：裸 `GET` 即可，我连 UA / Referer / Accept 都没带也返回数据。
  生产抓取建议仍带 `User-Agent` + `Referer: https://finance.baidu.com/`
- 返回统一是 `{"QueryID":"...","Result":{...},"ResultCode":0}`；
  `ResultCode != 0`（常见 `-1`）时 `ResultMsg` 是 `"internal server error"`——**多半是缺必填参数**，不是服务挂了
- **时间字段一律北京时间（UTC+8）字符串**，无时区标注（见下面各条的"坑"）
- 页面是 SPA（HTML 是空壳），数据全靠 JS 拉接口

### 怎么找到这些接口的（备查）
1. 拉页面 HTML → 只有 `vendors.js` / `index.js` 两个 bundle
2. 下 index bundle → 搜 `"/sapi/v1/` 路径、搜 `cate:"` 参数值
3. 路由懒加载：`d.u=function(e){...}` 里有 chunk id → hash 映射表，按需下分包
4. 各 tab 的 `type` 值（如 `notify_attend`）来自分包里的 `xxxTabs=[{text:"股东大会",type:"notify_attend"}]`

---

## 1. 美国经济数据 ✅

```
GET https://finance.pae.baidu.com/sapi/v1/financecalendar
    ?start_date=2026-09-21&end_date=2026-09-27&pn=0&rn=50&cate=economic_data
```

- `start_date` / `end_date`：`YYYY-MM-DD`
- `pn` / `rn`：翻页 / 每页条数（`rn=50` 实测可用）
- `cate=economic_data`

### 返回结构
```json
{"Result":{"calendarInfo":[
  {"date":"2026-09-21","dateStr":"2026年09月21日 周一","hasMore":"","total":35,
   "list":[{"time":"09:00","region":"中国","star":"2",
            "title":"中国9月五年期贷款市场报价利率LPR(%)",
            "formerVal":"3.5","indicateVal":"3.5","pubVal":"未公布",
            "dbName":"calendar_economic_data","countryIcon":"https://...",
            "capitalization":"0","marketValue":"0"}]}]}}
```

| 字段 | 含义 |
|---|---|
| `title` | 指标全名（含国别，如"美国8月耐用品订单月率初值(%)"） |
| `region` | 国家/地区（中文，如"美国"） |
| `time` | **北京时间** HH:MM |
| `star` | 重要性（实测只有 1 / 2） |
| `formerVal` | 前值 |
| `indicateVal` | 预期值（常缺失 → `null`） |
| `pubVal` | 公布值；**未发布时是字符串 `"未公布"`**（不是 null！） |
| `countryIcon` | 国旗图 URL |

### 实测
- 一周（09-21~09-27）252 条，其中**美国 125 条**（star2 有 28 条）
- 过去日期带完整公布值：
  `2026-09-17 02:00 美国 联邦基金利率目标下限(%) 前值3.5 预期3.75 公布3.75` ✓

### 坑 ⚠️
1. **`market` 参数对经济数据无效**——`market=us` 与不带 `market` 返回**字节数完全相同（80482）**。
   要美国数据只能**客户端按 `region == "美国"` 过滤**
2. 噪声极大，**必须自己过滤**。一周美国 125 条里，一半是每日更新的持仓/库存类：
   `iShares` / `SPDR` / `COMEX` / `NYMEX` / `钻井` / `API…库存` —— 一周光这几类就 ~60 条
3. **`star` 标得很粗**：FOMC 利率决议也只给 **2 星**（实测 09-17 那条）。
   想筛"宏观大事件"得自己按标题关键字匹配，别指望 star
4. 单日超 `rn` 条时 `hasMore:"1"`，要带 `pn` 逐日翻页

### 建议的过滤口径（宏观白名单）
保留标题命中：`利率 / CPI / PCE / 非农 / 失业 / 初请 / GDP / ISM / PMI / 零售 / 耐用品 /
消费者信心 / 通胀 / PPI / 工业产出 / 房地产 / 成屋 / 新屋 / 标普 / 美联储 / 鲍威尔 / EIA / 原油`
排除：`iShares / SPDR / COMEX / NYMEX / 钻井`

---

## 2. 美股股东大会 ❌ 拿不到

**结论：百度这个源没有美股股东大会数据，只有 A 股。** 证据（三重：

| 查询 | 结果 |
|---|---|
| `cate=notify_attend&market=us`（09-21~09-27） | 0 条 |
| `cate=notify_attend&market=us`（04-20~05-20，美股 proxy season） | 0 条 |
| `cate=notify_attend`（不带 market，一个月） | 416 条，**全部 `market="ab"`**（SZ 262 / SH 130 / BJ 24） |
| `cate=notify_attend`（不带 market，4 月 proxy season） | 987 条，**仍然全部 A 股** |
| `cate=notify_attend&market=us`（2026 全年） | 85 字节空响应 |

页面上确实有"股东大会"这个 tab（`trading-notify` 主 tab 下），但**切到美股是空的**。

**替代方案**：SEC EDGAR 的 `DEF 14A`（股东大会委托书），官方结构化 API，免费。
同类的 `notify_divide`（分红派息）/ `notify_suspend`（停复牌）/ `notify_contract`（重大经营合同）配
`market=us` 也全是空 —— 这一族（`notify_*`）都是 A 股专用。

---

## 3. 美股财报发行 ✅

```
GET https://finance.pae.baidu.com/sapi/v1/financecalendar
    ?start_date=2026-09-21&end_date=2026-09-27&pn=0&rn=50&cate=report_time&market=us
```

**`market=us` 在这条上生效**（返回全是美股标的）。

### 返回字段
```json
{"date":"2026-09-22","name":"汽车地带","code":"AZO","exchange":"US",
 "reportType":"美东时间发布2026年四季报及年报",
 "marketTime":"A",
 "marketValue":"46613949385",                       // 市值（美元）
 "mountSection":{"code":"US3540","name":"专卖店","market":"us","type":"block"}}
```

### 实测
一周（9 月底淡季）24 条，字段最全的一个源。

### 坑 ⚠️
**`marketTime` 代码不可信！**
- **数据里**：`marketTime:"A"` 的 reportType 写"盘后"（5 条），`"B"` 写"盘前"（4 条）—— 自洽
- **前端 JS 里的映射表写的却是反的**：`marketTime={A:{text:"盘前"}, B:{text:"盘后"}}`

两边打架 → **盘前/盘后只认 `reportType` 的中文原文**，别用 `marketTime`。

---

## 4. 美股快讯 ✅

```
GET https://finance.pae.baidu.com/selfselect/expressnews?rn=50&pn=0&tag=美股
```

- `tag` 取值：`A股` / `港股` / `美股` / `异动`，**包含匹配**（一条可同时挂多标签）

### 返回字段
```json
{"title":"马斯克预言：AI将使明年美国GDP增速翻番达到4%",
 "content":{"items":[{"type":"text","data":"…正文…（财联社）"}]},
 "publish_time":"1789909649",                       // 秒级 unix
 "tag":"快讯$美股",
 "provider":"Finscope",
 "third_url":"http://live.financebe.com/stock/detail.html?d=2198889",
 "important":"0",
 "entity":[{"code":"TSLA","name":"特斯拉","market":"us",
            "price":"364.270","ratio":"-0.53%"}]}   // ★ 关联标的 + 实时价/涨跌幅
```

### 实测
- 量级：周末美股 tag ≈ **26 条/天**，交易日 ≈ **90 条/天**（pn=200 那页 50 条覆盖 13.5 小时）
- `rn=50` 可用

### 坑 ⚠️
1. **`pn` 不是干净的偏移分页**：pn=0/1/10/20 返回的时间窗**互相重叠**
   （pn=10 的最新一条比 pn=0 的还旧 7 小时），只有 pn=50/200 才稳定更早
   → **正确用法：轮询头部 + 按 `(title, publish_time)` 去重**，别靠翻页做历史回填
2. **`important` 字段规则不明**：50 条里 39 条是 `"1"`，与"有没有关联标的/原文链接"都不相关 → 别当重要性用

### 附：要闻（可选）
```
GET https://finance.pae.baidu.com/selfselect/news?rn=20&pn=0&tag=美股
```
返回 `tabs: [ {7x24 expressnews}, {要闻 important} ]`，要闻项带
`news_id` / `publish_time` / `title` / `imgs` / `locate_url`。结构不同，未细测。

---

## 5. 美股个股资金流 ✅

```
GET https://finance.pae.baidu.com/vapi/v1/fundflow
    ?market=us&code=SNDK&type=stock&finance_type=stock&fund_flow_type=
```

`fund_flow_type` 三档用法：

| 值 | 返回 |
|---|---|
| 留空 `""` | `content.fundFlowSpread`（今日分布）+ `fundFlowBlock`（行业）+ `fundFlowMinute`（分时） |
| `day` / `week` / `month` | 各 15 根历史序列（含 `closePx`） |
| `realtime` | 实时（休市时为空，未在盘中实测） |

### `fundFlowSpread.result`（一次请求拿全"资金"tab）
```json
{"superGrp":  {"netTurnover":"0.00",  "turnoverIn":"0.00", "turnoverOut":"0.00",
               "turnoverInRate":"0.00%","turnoverOutRate":"0.00%"},
 "largeGrp":  {"netTurnover":"+79.40","turnoverIn":"96.85","turnoverOut":"17.45",
               "turnoverInRate":"53.07%","turnoverOutRate":"9.56%"},
 "mediumGrp": {"netTurnover":"+5.39", ...},
 "littleGrp": {"netTurnover":"+2.51", ...},
 "todayMainFlow": {"mainIn":"96.85","mainOut":"17.45","mainNetIn":"+79.40"},
 "turnoverInTotal":"134.90","turnoverOutTotal":"47.60","turnoverNetTotal":"+87.30",
 "mainNetTotal":"+79.40",
 "unit":"亿","updateTime":"2026-09-18","stockStatus":"已收盘",
 "analysis":{"content":"今日净流入较近5日净流入的均值增加62.24亿",
             "list":[{"content":"3.03%","desc":"占流通市值比例"},
                     {"content":"7/5566","desc":"主力净流入额市场排名"}]}}
```

- `fundFlowBlock`：所属行业资金流向（同款 superGrp/largeGrp/… 结构）
- `fundFlowMinute`：分时，字段是 `asyncUrl`（二次请求）；内嵌 `data` 是 `;` 分隔的 CSV，
  `headers = [时间,整体,主力,散户,特大单,大单,中单,小单,股价,涨跌幅]`，`maxPoints:390`，
  x 轴 `cx=["09:31","16:00"]`（**美股 390 分钟**）

### 历史序列（day/week/month）
每条 `{netTurnover, turnoverIn, turnoverOut, date}` + `closePx[]`：
- `day`：15 根（08-28 ~ 09-18）
- `week`：15 根（06-12 ~ 09-18）
- `month`：15 根（2025-07-31 ~ 09-18）

### 时效 ⏱️
**盘中实时，不是 T+1**，证据：
- 资金 tab 组件 `setInterval(..., 5000)`（**5 秒**），外面套 `isLoop(stockStatus)` 门控
- 资金分布组件 `setInterval(..., 15000)`（**15 秒**），同门控
- 非交易时段直接 `clearInterval`，不再请求
- 分时 390 个点逐分钟累积

收盘后**冻结在最后一个交易日**（周日拉到的是周五 09-18 + `stockStatus:"已收盘"`）。
⚠️ 真实延迟（后端算得多快）**未验证**——需要在交易日盘中连续拉两次对比 `updateTime`。

### 坑 ⚠️
1. **`superGrp`（超大单）对美股恒为 0** —— SNDK、NVDA 都是 `0.00`，主力净流入≈大单。
   说明"超大/大/中/小单"是 A 股的资金分类模型，美股只有粗分类
2. **分时行里的日期戳不可信** —— 行内写 `2026-09-20`（当时是周日），而 `updateTime` 是 `2026-09-18`。
   **判新鲜度要用 `updateTime` / `stockStatus`，不要信分时行的日期**
3. **`unit:"亿"` 不带币种** —— NVDA 单日 287.92：按美元≈$288亿、按人民币≈$40亿，量级上两种都说得通。
   **接进去之前必须跟页面显示核对一次**

---

## 6. 附：个股公告（bonus，未细测）

```
GET https://finance.pae.baidu.com/sapi/v1/stock_notice
    ?code=AAPL&market=us&pn=1&rn=10&group=stock_us_notice
```

返回 `classifiName`（全部/定期报告/重大事件/注册声明/持股声明/并购/证券发行/…）+ `list`
（带 `docUrl` PDF 原文、`doc_resources[].url` 的 txt 文本）。
**是逐只股票查**，不是全市场日历；分类里**没有"股东大会"**。

---

# 二、AICoin（www.aicoin.com）

## 7. 加密快讯 ✅（所有源里质量最好）

```
POST https://www.aicoin.com/napi/data?module=v3.newsflash.list
Content-Type: application/json

{"lan":"cn","version":"v1","only_important":0,"tab":0,
 "pagesize":20,"open":8,"selectFilter":true}
```

**无鉴权**（不需要 cookie / XSRF）。返回 `{"success":true,"status":200,"data":{"isLive":0,"list":[…]}}`

### 字段
```json
{"id": 3091471,                                  // 数字唯一 ID
 "title": "Visa 采取行动关闭 meme coin 信用卡奖励漏洞",
 "content": "Visa 正在采取行动关闭与 meme coin 交易相关的信用卡奖励漏洞。",
 "timestamp": 1789920259,                        // 秒级 unix
 "is_important": 0,
 "up_count": 0, "down_count": 5,                 // 利好/利空投票
 "link": "https://jump.do/zh-CN/xlink?target=…", // 原文
 "shareLink": "https://aicoin.com/zh-CN/flash-details/axdejdoz",
 "picArray": [],
 "member_id": 750, "source": "AiCoin", "grade": 4,
 "news_type": "1", "flashTagType": "1", "language": "zh-cn",
 "relate_markets": [{                            // ★ 关联标的 + 交易所 + 价格
     "coin":"BTC","market":"binance","market_name":"币安",
     "key":"btcusdt:binance","price":"80951","degree":"0.04"}],
 "relatedData": [{"degree":"0.04","name":"BTC 0.04%","type":"market",
                  "link":"aicoin:///open?url=…"}]}
```

### 翻页 / 过滤（全部实测）

| 参数 | 结果 |
|---|---|
| `lastTime:<上一页最旧的时间戳>` | ✅ **有效**——返回更早的 20 条，这就是翻页游标 |
| `maxId` / `minId` / `page` | ❌ 无效（返回和首页一样） |
| `pagesize:50` | ❌ 服务端**锁死在 20**（传 50 也只给 20） |
| `only_important:1` | ✅ 有效——只返回重要快讯（跨 13 小时才凑够 20 条，确实滤过） |

### 时效 ⏱️
**纯实时流**：实测最新一条就是当时刚发布的（00:04 拉到当页）。
适合 **1–2 分钟轮询 + 按 `id` 去重**（有数字 ID，比百度那个稳）。

### 坑 ⚠️
- **URL 里的 `?date=2026-09-23` 是前端日历视图的参数，API 不吃**
  → 要历史只能靠 `lastTime` 一页页往前翻（每页 20 条）
- 站内私有 `napi`（`/napi/data?module=<方法名>`），不是公开 API

### 同族接口（未实测）
- `v3.newsflash.unread-list` —— 未读快讯
- `upgrade.calendar.list` / `.marks` / `.words` —— **加密事件日历**
  （参数形如 `{startTime, period:31, returnType:["event","flash"]}`，带重要度打分）

### 怎么找到的（备查）
HTML 是 Next.js App Router（`self.__next_f.push` 流式渲染，**页面 SSR 里就有正文**）→
下 `_next/static/chunks/*.js` → 搜 `newsflash` → 命中请求封装模块：

```js
async function d(e, t, n="POST", a={}) {
  const {url: f, ...} = await s(`/napi/data?module=${e}`, d);
  return r.Z.post(f, {lan: m, ...t}, ...)
}
```
即 `POST /napi/data?module=<方法名>`。

---

# 三、Telegram：金十数据 闪电资讯（t.me/jin10light）

## 8. 实时快讯 ✅（覆盖面最广：加密 + 宏观 + 时政）

走 **Telegram 公开网页预览**，**不需要登录 / bot token / 客户端**：

```
GET https://t.me/s/jin10light              # 最新 20 条
GET https://t.me/s/jin10light?before=<id>  # 往更早翻一页（也是 20 条）
```

### 返回结构（HTML，正则或 BeautifulSoup 解析）
每条消息一个 `tgme_widget_message` 块：

| 字段 | 位置 | 说明 |
|---|---|---|
| 消息 id | `data-post="jin10light/235858"` | **天然去重键**，同时也是翻页游标 |
| 时间 | `<time datetime="2026-09-22T11:24:48+00:00">` | **ISO 8601 / UTC**，无时区歧义（比百度那个北京时间字符串省事） |
| 正文 | `<div class="tgme_widget_message_text">…</div>` | HTML → 去标签 + `unescape` |

样例（2026-09-22 实抓）：
```
jin10light/235858  2026-09-22T11:24:48+00:00  昨日比特币空单大幅清算后，合约交易仍拥挤，多空比胶着
jin10light/235859  2026-09-22T11:25:36+00:00  伊朗消息人士否认海峡开放相关报道
jin10light/235860  2026-09-22T11:29:36+00:00  Bitget PoolX：锁仓BGBTC解锁2400枚UNI空投
```

### 实测
- 每页 **20 条**；`?before=<id>` 往前一页，**与上一页不重叠**（235838~235857 → 235818~235837 …）
- **回填很深**：从当前一路翻到 **8 天前**（09-14 那批）仍正常返回 → 历史回填没障碍
- 密度 **≈ 20 条 / 100 分钟**（约每 5 分钟一条，突发事件更密）→ **轮询 1 分钟足够**

### 坑 ⚠️
1. **只对公开频道有效**——私有频道/群组在 `t.me/s/` 下读不到
2. **有节流**：Telegram 对预览页限流，遇到 429 / 空响应要退避重试，别高频刷
3. **合规**：读公开预览属于公开信息，风险最低；但该频道简介写着"顶级付费文章"——
   **自己系统内部用没问题，别再对外分发 / 转播**
4. 预览页可能只保留滚动窗口（本次实测能翻 8 天，更早没测）

### 另外两条路（都不推荐）

| 方案 | 结论 |
|---|---|
| **Bot API** | ❌ 机器人**读不到别人的频道**（除非把 bot 设成该频道管理员）——jin10light 是别人的频道，走不通 |
| **MTProto 用户端**（Telethon / Pyrogram） | 能实时收推送、延迟更低，但要手机号登录 + 自申请 `api_id/api_hash`，且**用个人账号跑自动化有封号风险** |

---

# 四、汇总与落地建议

## 数据源能力对照

| 需求 | 源 | 接口 | 建议频率 |
|---|---|---|---|
| 美国经济数据 | 百度 | `sapi/v1/financecalendar?cate=economic_data` | 30 min |
| ~~美股股东大会~~ | — | **拿不到**（换 SEC EDGAR DEF 14A） | — |
| 美股财报 | 百度 | `sapi/v1/financecalendar?cate=report_time&market=us` | 1 h |
| 美股快讯 | 百度 | `selfselect/expressnews?tag=美股` | 5 min（轮询+去重） |
| 美股要闻（可选） | 百度 | `selfselect/news?tag=美股` | 30 min |
| 美股个股资金流 | 百度 | `vapi/v1/fundflow?market=us&code=X` | 盘中 5 min / 收盘后 1 次 |
| **加密快讯** | **AICoin** | `POST /napi/data?module=v3.newsflash.list` | **1–2 min**（轮询+按 id 去重） |
| **实时快讯**（加密+宏观+时政） | **Telegram 金十** | `GET t.me/s/jin10light`（`?before=<id>` 翻页） | **1 min**（轮询+按 id 去重） |

## 通用注意事项

1. **时区**：百度的时间字段是**北京时间（UTC+8）字符串**；AICoin 和百度的 `publish_time` / `timestamp`
   是**秒级 unix**（无歧义，优先用）
2. **"未公布"是字符串**（百度 `pubVal`），不是 null
3. **缺失值是 `null` 或字段直接不存在**（如百度 `indicateVal`、AICoin `entity`）
4. 两个源都**无唯一 ID 或 ID 稳定性不同**：AICoin 有数字 `id`（可直接去重）；
   百度只能靠 `(date, time, region, title)` / `(title, publish_time)` 组合去重
5. **币种/单位不明的地方不要自动换算**（尤其百度资金流的 `unit:"亿"`）
6. 都是私有接口 → 加**异常告警**（连续 N 次解析失败就报），别静默
7. **抓取频率别高**：快讯类 1–5 分钟已经足够，日历类半小时一次；
   别做无意义的高频轮询（和币安接口的限流要求一个道理）
8. **合规**：Telegram 金十那条是**别人频道的内容**（简介写着"付费文章"）——内部用可以，
   别对外分发；预览页有限流，遇到 429 要退避

## 待定

- 资金流要跟哪些美股代码（需要一个 `CODES` 列表）
- 落库位置（建议独立 `db/baidu_finance.db`，不碰现有库）
- 是否要把 AICoin 的 `upgrade.calendar.list`（加密事件日历）也挖进来
- 要不要把 Telegram 金十一起接进抓取（1 分钟轮询 + 按消息 id 去重；首次可用 `?before=` 回填）
