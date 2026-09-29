"""页面 JS 语法检查：把 web.py 里各 HTML 常量**实际输出到浏览器**的 <script>
喂给 `node --check`。

为什么必须有：HTML 常量是 Python 字符串，`\\'` 在 Python 里被吃掉一层 ——
写成 `\'`（文件里一个反斜杠）看源码没问题，输出给 JS 就变成
`return '<div onclick="location.href='/alphafox'">'`（字符串提前闭合）→
整段 <script> 语法错误 → 页面所有面板永远"加载中"。
2026-09-14 实测踩到过一次（trd-card），所以做成常规检查。
"""
import ast
import io
import json
import os
import re
import subprocess
import sys
import tempfile

# 控制台是 GBK：断言名里的 ⚡ 之类字符会让 print 直接抛 UnicodeEncodeError
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def html_constants(src: str) -> dict:
    out = {}
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str) \
                and node.value.value.rstrip().endswith("</html>"):
            name = getattr(node.targets[0], "id", None)
            if name:
                out[name] = node.value.value
    return out


def scripts_of(html: str) -> list:
    blocks = []
    pos = 0
    while True:
        i = html.find("<script>", pos)
        if i < 0:
            break
        j = html.find("</script>", i)
        if j < 0:
            break
        blocks.append(html[i + len("<script>"):j])
        pos = j + len("</script>")
    return blocks


# 渲染冒烟：桩掉 DOM/fetch/setInterval，把页面 JS 真跑一遍（load() → 六个渲染函数），
# 检查产物 HTML 里该有的内容都在。语法检查抓不到"函数名打错 / 变量未定义 / 渲染时抛错"
# 这类问题 —— 它们在浏览器里表现为"面板永远加载中"。
RENDER_HARNESS = r"""
const fs = require('fs');
const [, , jsPath, payloadPath] = process.argv;
const store = {};
const listeners = [];
function el(id) {
  if (!store[id]) store[id] = {innerHTML: '', textContent: '', dataset: {},
                               addEventListener() {}, getAttribute() { return null; }};
  return store[id];
}
// 页面会挂 document 级委托监听（如"加载更多"按钮）→ 桩里必须有 addEventListener
global.document = {getElementById: el,
                   addEventListener(t, h) { listeners.push([t, String(h)]); }};
global.fetch = () => Promise.resolve({
  json: () => Promise.resolve(JSON.parse(fs.readFileSync(payloadPath, 'utf8')))});
global.setInterval = () => 0;
eval(fs.readFileSync(jsPath, 'utf8'));
const want = ['acct', 'stats', 'tk-wrap', 'pos-wrap', 'his-wrap', 'log-wrap',
              'trd-wrap', 'ts-wrap', 'ts-chips',
              'his-more', 'log-more', 'his-cnt', 'log-cnt'];
let tries = 0;
(function poll() {
  tries += 1;
  if ((store.acct && store.acct.innerHTML) || tries > 50) {
    const out = {};
    for (const k of want) {
      out[k] = store[k] ? store[k].innerHTML : '';
      // textContent 也导出：页面有些地方只写 textContent（如"已显示 N 条"）
      out[k + '_text'] = store[k] ? String(store[k].textContent || '') : '';
    }
    process.stdout.write(JSON.stringify(out) + '\n');
    return;
  }
  setTimeout(poll, 20);
})();
"""

def _gen_history(n):
    """造 n 条已平仓纸盘行（渲染侧字段齐全）"""
    return [{"id": 9000 + i, "token": "AAA", "side": "LONG",
             "closed_at": "2026-09-12 11:00:00", "entry_price": 100.0,
             "current_price": 90.0, "pnl_pct": -10.0, "realized_pnl": -12.5,
             "live_pnl": -11.8, "live_entry": 100.0, "advice": "超势反转平仓",
             "close_snap": {"trend": "DOWN"}} for i in range(n)]


def _gen_trades(n):
    """造 n 条实盘动作日志（两种动作交替，保证动作中文映射断言有料可查）"""
    out = []
    for i in range(n):
        if i % 2 == 0:
            out.append({"created_at": "2026-09-13 09:00:00", "token": "AAA",
                        "action": "sl_set", "price": 95.0, "quantity": 2.0,
                        "exchange_order_id": "222",
                        "details_json": json.dumps({"reason": "补开挂止损"})})
        else:
            out.append({"created_at": "2026-09-13 08:00:00", "token": "AAA",
                        "action": "rebalance_add", "price": 100.0, "quantity": 1.0,
                        "exchange_order_id": "333",
                        "details_json": json.dumps(
                            {"reason": "交易所少于纸盘，补仓对齐"})})
    return out


RENDER_PAYLOAD = {
    "account": {"equity": 1234.5, "available_balance": 1000.0, "margin_used": 60.0,
                "unrealized_pnl": 25.0, "open_count": 2, "ok": True},
    "params": {"notional_usd": 50.0, "leverage": 3.0},
    "stats": {
        "paper": {"closed": 3, "wins": 2, "pnl": 12.5},
        "account": {"equity": 1987.5, "available_balance": 1900.0,
                    "locked_margin": 50.0},
        "live": {"closed": 1, "wins": 0, "pnl": -11.8, "margin_used": 20.0,
                 "open_count": 1, "pending_count": 1,
                 "orphan_count": 1, "orphan_tokens": ["CCC"]},
        "open": {"paper_pnl": 20.0, "ex_pnl": 25.0, "open_paper": 1,
                 "open_pending": 1, "open_live": 2, "open_ex": 2}},
    "cards": [
        {"token": "AAA", "trend": "UP", "upper_band": 112.0, "lower_band": 100.0,
         "last_price": 110.0, "state": "HOLDING",
         "position": {"quantity": 2.0, "entry_price": 100.0}, "pending": None,
         "notional_usd": None, "leverage": None, "live_mode": True,
         "paused": False, "live_blocked_by": None, "fetch_error": None},
        {"token": "BBB", "trend": "DOWN", "upper_band": 52.0, "lower_band": 44.0,
         "last_price": 45.0, "state": "PAUSED", "position": None,
         "pending": {"quantity": 1.0, "limit_price": 50.0},
         "notional_usd": 80.0, "leverage": 5.0, "live_mode": False,
         "paused": True, "live_blocked_by": "alphafox", "fetch_error": None}],
    "positions": [
        {"id": 1, "token": "AAA", "side": "LONG", "status": "OPEN", "quantity": 2.0,
         "entry_price": 100.0, "current_price": 110.0, "unrealized_pnl": 20.0,
         "leverage": 5.0, "stop_loss_price": 95.0, "live_id": 1,
         "live_status": "OPEN", "live_sl": 95.0, "live_sl_qty": 2.0,
         "ex_qty": 2.0, "ex_entry": 100.0, "ex_pnl": 20.0, "ex_leverage": 5,
         "state": "HOLDING"},
        {"id": 2, "token": "BBB", "side": "SHORT", "status": "PENDING",
         "quantity": 1.0, "entry_price": None, "current_price": 50.0,
         "unrealized_pnl": 0.0, "leverage": 3.0, "stop_loss_price": 60.0,
         "limit_price": 50.0, "live_id": None, "live_status": None, "live_sl": None,
         "ex_qty": None, "ex_entry": None, "ex_pnl": None, "state": "PENDING"}],
    # 历史/日志故意给超一页的量（60/55 > 每页 50）→ 验证分页：只渲染 50 条 + 出现"加载更多"
    "history": _gen_history(60),
    "trades": _gen_trades(55),
}


# 跟单托管页载荷（/api/alphafox/board 的形态）：含"探测过"和"没探测过"两种交易员
ALPHAFOX_PAYLOAD = {
    "account": {"equity": 1500.0, "available_balance": 1200.0, "margin_used": 90.0,
                "unrealized_pnl": 20.0, "open_count": 1, "ok": True},
    "stats": {"paper": {"closed": 5, "wins": 3, "pnl": 42.5},
              "live": {"closed": 5, "wins": 3, "pnl": 39.0},
              "open": {"paper_pnl": 12.0, "ex_pnl": 3.0, "open_paper": 1,
                       "open_ex": 1}},
    "positions": [],
    "history": _gen_history(60), "trades": _gen_trades(55),
    "traders": [
        {"trader_id": "hl-1", "label": "熬鹰链上fomo", "stale": False,
         "polled_at": 1789405500000, "pos_count": 1,
         "notional_sum": 100917.71, "upnl_sum": -1549.36,
         "positions": [{"symbol": "XYZ:CL", "side": "SHORT", "qty": 1020.154,
                        "avg_price": 97.405, "mark_price": 98.924,
                        "notional": 100917.71, "leverage": 15,
                        "unrealized_pnl": -1549.36, "liq_price": 110.2,
                        "last_order_time": 1789405426522}]},
        {"trader_id": "bn-2", "label": "等待中的交易员", "stale": True,
         "polled_at": None, "pos_count": 0, "notional_sum": 0.0, "upnl_sum": 0.0,
         "positions": None}],
    # token_options 在**顶层**（照 /api/alphafox/board 真实形态造，
    # 早期夹具把它塞进 token_settings → 页面拿不到、一排币种按钮恒为空）
    "token_options": ["BTC", "CL", "ETH"],
    # 比例模型（2026-09-17）：globals 只剩 {杠杆, 单笔上限}，每币覆盖只剩杠杆
    "token_settings": {
        "globals": {"live_leverage": 5.0, "max_notional": 0},
        "items": [{"token": "CL", "live_leverage": 3.0, "eff_leverage": 3.0,
                   "has_position": True}]},
}


# 这两页的 HTML 全静态、脚本全部有效 → 缺 id 直接判失败（其余页面只告警）
HARD_PAGES = {"SUPERTREND_LIVE_HTML", "ALPHAFOX_LIVE_HTML"}


# 短列表变体（历史/日志都空）→ 验证"不到一页不显示加载更多"
ALPHAFOX_PAYLOAD_SHORT = {**ALPHAFOX_PAYLOAD, "history": [], "trades": []}


def render_smoke(html: str, payload: dict):
    """跑一遍页面 JS 的渲染管线，返回 {容器id: 渲染出的 HTML}"""
    js = "\n;\n".join(scripts_of(html))
    if not js:
        return None, "页面没有 <script> 段"
    paths = []
    try:
        for content, suffix in ((RENDER_HARNESS, ".js"), (js, ".js"),
                                (json.dumps(payload, ensure_ascii=False), ".json")):
            with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False,
                                             encoding="utf-8") as f:
                f.write(content)
                paths.append(f.name)
        r = subprocess.run(["node", paths[0], paths[1], paths[2]],
                           capture_output=True, text=True, encoding="utf-8",
                           timeout=60)
    except Exception as e:
        return None, f"node 执行失败: {e}"
    finally:
        for p in paths:
            try:
                os.unlink(p)
            except OSError:
                pass
    if r.returncode != 0 or not (r.stdout or "").strip():
        return None, (r.stderr or "").strip()[:400]
    try:
        return json.loads(r.stdout.strip().splitlines()[-1]), ""
    except Exception as e:
        return None, f"渲染结果解析失败: {e}"


def id_refs_ok(html: str):
    """JS 里 getElementById('X') 引用的 id 必须真的存在（拼错/漏建元素 = 真 bug）。

    判据用**出现次数**而不是"静态 HTML 里有 id=" ：
      · 静态元素 → 至少两处（`id="x"` 本身 + 引用）
      · JS 动态建的元素（`el.id='x'` / innerHTML 模板）→ 也至少两处
      · 真拼错 → 全常量里**只出现这一次**
    漏一个的后果是运行时 TypeError → **整段 <script> 直接死** → 页面全部面板卡在
    "加载中"；语法检查和渲染冒烟（桩 DOM 会自动造元素）都发现不了，所以单列一条。
    返回可疑 id 列表。"""
    js = "\n;\n".join(scripts_of(html))
    refs = set(re.findall(r"getElementById\(\s*['\"]([^'\"]+)['\"]\s*\)", js))
    return sorted(r for r in refs if html.count(r) <= 1)


def main() -> int:
    sys.path.insert(0, os.path.join(ROOT, "extra"))
    from _mut_src import read_module_source   # 变异注入的源码优先（否则读磁盘原文）
    src = read_module_source("web")
    consts = html_constants(src)
    fails = []
    for name, html in sorted(consts.items()):
        blocks = scripts_of(html)
        if not blocks:
            # **假绿灯防护**：页面里明显有 JS 却没有 <script> 标签 → 脚本会被当正文
            # 渲染出来（2026-09-25 事件页就是这么"一团乱码"的），不能拿"0 段 script"
            # 当通过。判据取几个只可能出现在 JS 里的特征串，避免误伤普通 HTML。
            if any(k in html for k in ("const ", "function ", "getElementById(")) \
                    and "<script" not in html:
                fails.append((name, "有 JS 却没有 <script> 标签（脚本会被当正文渲染）"))
                print(f"[FAIL] {name}: 有 JS 却没有 <script> 标签（脚本会被当正文渲染）")
            continue
        js = "\n;\n".join(blocks)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as f:
            f.write(js)
            path = f.name
        try:
            r = subprocess.run(["node", "--check", path], capture_output=True,
                               text=True, encoding="utf-8", timeout=60)
        finally:
            os.unlink(path)
        if r.returncode != 0:
            fails.append((name, (r.stderr or "").strip()))
            print(f"[FAIL] {name}: JS 语法错误")
            for line in (r.stderr or "").strip().splitlines()[:6]:
                print("    " + line)
        else:
            print(f"[OK  ] {name}  JS 语法通过（{len(blocks)} 段 script）")
        _miss = id_refs_ok(html)
        if _miss and name in HARD_PAGES:
            # 这两页的 HTML 是静态的、脚本全部有效 → 缺 id 必是真 bug
            fails.append((f"{name}(id)", str(_miss)))
            print(f"[FAIL] {name}: JS 引用了 HTML 里不存在的元素 id: {_miss}")
        elif _miss:
            # 其它页可能是"脚本里的死函数引用了别的页面的元素"（首页就有两个这样的
            # 死函数）→ 只告警不失败，免得掩盖真正的问题
            print(f"[warn] {name}: JS 引用但页面里没有的元素 id（可能是死代码）: {_miss}")
        else:
            print(f"[OK  ] {name}  JS 引用的元素 id 都存在")

    # ---- 渲染冒烟：只对趋势实盘页做（真 board 载荷 → 六个面板产物）----
    print("\n== 趋势实盘页渲染冒烟（桩 DOM + 真 board 载荷）==")
    out, err = render_smoke(consts.get("SUPERTREND_LIVE_HTML", ""), RENDER_PAYLOAD)
    if out is None:
        fails.append(("SUPERTREND_LIVE_HTML(render)", err))
        print(f"[FAIL] 渲染报错: {err}")
    else:
        def rc(name, cond, extra=""):
            if cond:
                print(f"[OK  ] {name}")
            else:
                fails.append((name, "渲染产物里没有期望内容"))
                print(f"[FAIL] {name} {extra}")
        rc("① 账户：权益/可用 + 趋势归属笔数",
           "账户权益" in out["acct"] and "$1,234.5" in out["acct"]
           and "趋势实盘·挂单腿" in out["acct"])
        rc("② 盈亏：纸盘胜率 67% / 实盘累计 -11.80",
           "67%" in out["stats"] and "-11.80" in out["stats"])
        rc("③ 币种：趋势/状态/实盘标记/暂停+占用备注",
           "AAA" in out["tk-wrap"] and "持仓中" in out["tk-wrap"]
           and "实盘" in out["tk-wrap"] and "暂停" in out["tk-wrap"]
           and "被跟单占用" in out["tk-wrap"])
        rc("④ 持仓：平仓按钮 + 止损两列 + 挂单腿行 + 孤儿告警",
           "data-close=\"AAA\"" in out["pos-wrap"] and "95" in out["pos-wrap"]
           and "挂单" in out["pos-wrap"]
           and "没有对应的活跃纸盘仓" in out["pos-wrap"])
        rc("⑤ 历史：平仓原因 + 平仓时趋势",
           "超势反转平仓" in out["his-wrap"] and "DOWN" in out["his-wrap"])
        # 断言用带标签的精确形式：仅查"挂止损"会被原因列里的"补开挂止损"误命中
        rc("⑥ 日志：动作中文映射 + 原因",
           "<td>挂止损</td>" in out["log-wrap"]
           and "<td>对账补仓</td>" in out["log-wrap"]
           and "补开挂止损" in out["log-wrap"])
        # 分页：载荷 60 条历史 / 55 条日志 → 只渲染首页 50 条 + 出现"加载更多"。
        # 数数据行要看 <tbody> 段（<thead> 里还有一行表头）
        _his_rows = out["his-wrap"].split("<tbody>")[-1].count("<tr>")
        _log_rows = out["log-wrap"].split("<tbody>")[-1].count("<tr>")
        rc("⑤ 历史持仓分页：只渲染 50 条（载荷 60）", _his_rows == 50, str(_his_rows))
        rc("⑥ 交易日志分页：只渲染 50 条（载荷 55）", _log_rows == 50, str(_log_rows))
        rc("两个面板都出现「加载更多」按钮 + 已显示条数",
           'data-more="his"' in out["his-more"] and 'data-more="log"' in out["log-more"]
           and "已显示 50 条" in out.get("his-cnt_text", "")
           and "已显示 50 条" in out.get("log-cnt_text", ""),
           out["his-more"][:100] + " | his-cnt=" + out.get("his-cnt_text", "")
           + " | log-cnt=" + out.get("log-cnt_text", ""))
    # ---- 渲染冒烟：跟单托管页（交易员卡片 + 币种设置按钮）----
    print("\n== 跟单托管页渲染冒烟（桩 DOM + 真 board 载荷）==")
    out2, err2 = render_smoke(consts.get("ALPHAFOX_LIVE_HTML", ""), ALPHAFOX_PAYLOAD)
    if out2 is None:
        fails.append(("ALPHAFOX_LIVE_HTML(render)", err2))
        print(f"[FAIL] 渲染报错: {err2}")
    else:
        def rc2(name, cond, extra=""):
            if cond:
                print(f"[OK  ] {name}")
            else:
                fails.append((name, "渲染产物里没有期望内容"))
                print(f"[FAIL] {name} {extra}")

        card = out2["trd-wrap"]
        rc2("③ 交易员卡片：名字 + 汇总（持仓数/名义/浮盈）",
            "熬鹰链上fomo" in card and "1 个持仓" in card
            and "$100,917.71" in card and "-1549.36" in card, card[:200])
        rc2("③ 持仓明细字段齐全（币种/方向/杠杆/数量/开仓/标记/名义/强平/浮盈/最后下单）",
            all(x in card for x in ("XYZ:CL", "空", "15x", "1020.154000",
                                    "97.405000", "98.924000", "强平",
                                    "110.2", "浮盈", "最后下单")), card[:400])
        # 只看有持仓那张卡：没探测那张的"等待探测…"是文案省略号，不是截断
        _card1 = card.split("trd-card")[1] if "trd-card" in card else ""
        rc2("③ 明细不截断（有持仓的卡片里没有省略号）",
            "…" not in _card1 and "..." not in _card1, _card1[:300])
        rc2("③ 没探测到的交易员显示等待（不谎报空仓）",
            "等待探测" in card, card[-200:])
        rc2("③ 卡片带「⚡ 快速跟随」按钮（data-qf = trader_id）",
            'data-qf="hl-1"' in card and 'data-qf="bn-2"' in card
            and "⚡ 快速跟随" in card, card[:300])
        rc2("⑦ 已交易过的币是可点选按钮（token_options 取自顶层）",
            'data-chip="CL"' in out2["ts-chips"] and 'data-chip="BTC"' in out2["ts-chips"],
            out2["ts-chips"][:200])
        rc2("⑦ 覆盖表带生效杠杆 + 持仓中标记、无「名义」列",
            "持仓中" in out2["ts-wrap"] and "CL" in out2["ts-wrap"]
            and "生效杠杆" in out2["ts-wrap"] and "3x" in out2["ts-wrap"]
            and "名义" not in out2["ts-wrap"], out2["ts-wrap"][:300])
        rc2("⑦ 全局区有杠杆与单笔上限输入",
            'id="g-lev"' in consts.get("ALPHAFOX_LIVE_HTML", "")
            and 'id="g-maxn"' in consts.get("ALPHAFOX_LIVE_HTML", ""))
        # 函数必须定义在**跟单页**里（曾经误放进趋势实盘页：那边 getElementById
        # ('trd-wrap') 直接 TypeError → 整段 script 死 → 全页卡"加载中"）
        rc2("⚡ 快速跟随的委托与函数都在跟单托管页",
            "function quickFollow" in consts.get("ALPHAFOX_LIVE_HTML", "")
            and "getElementById('trd-wrap').addEventListener" in consts.get("ALPHAFOX_LIVE_HTML", "")
            and "quickFollow" not in consts.get("SUPERTREND_LIVE_HTML", ""),
            "趋势实盘页混入了 quickFollow")
        # 分页（长列表载荷 60/55）→ 只渲染首页 50 条 + 出现"加载更多"
        _ah = out2["his-wrap"].split("<tbody>")[-1].count("<tr>")
        _al = out2["log-wrap"].split("<tbody>")[-1].count("<tr>")
        rc2("⑤ 历史持仓分页：只渲染 50 条（载荷 60）", _ah == 50, str(_ah))
        rc2("⑥ 交易日志分页：只渲染 50 条（载荷 55）", _al == 50, str(_al))
        rc2("都出现「加载更多」按钮 + 已显示条数",
            'data-more="his"' in out2["his-more"]
            and 'data-more="log"' in out2["log-more"]
            and "已显示 50 条" in out2.get("his-cnt_text", "")
            and "已显示 50 条" in out2.get("log-cnt_text", ""),
            out2.get("his-cnt_text", "") + " | " + out2.get("log-cnt_text", ""))

    # 同一页 + 短列表载荷 → 不到一页时不显示"加载更多"
    out3, err3 = render_smoke(consts.get("ALPHAFOX_LIVE_HTML", ""),
                              ALPHAFOX_PAYLOAD_SHORT)
    if out3 is None:
        fails.append(("ALPHAFOX_LIVE_HTML(render/short)", err3))
        print(f"[FAIL] 渲染报错(短列表): {err3}")
    else:
        rc2("⑤⑥ 短列表（0 条）时不显示「加载更多」",
            "加载更多" not in out3["his-more"] and "加载更多" not in out3["log-more"],
            out3["his-more"][:80] + " | " + out3["log-more"][:80])
    print(f"\n页面 JS 检查: {len(consts) - len([f for f in fails if f[0] in consts])}"
          f"/{len(consts)} 语法通过")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
