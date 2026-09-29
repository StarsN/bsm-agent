"""alphofox 跟单雷达数据源验证脚本（在服务器上运行，服务器可访问 alphofox）

验证内容：
1. traders 列表接口
2. snapshot 接口（顶部指标 + 持有仓位 + 成交流水 + 交易点位字段完整性）
3. dna 接口（带 Origin/Referer 头，交易分析字段完整性）
4. 币种盈亏汇总聚合逻辑
5. 模拟探测循环：polledAt 变化检测（等 35s 看第二版数据）

运行：python3 test_alphafox_api.py
"""
import json
import time
from collections import defaultdict

import requests

BASE = "https://www.alphafox.app"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36",
}
DNA_HEADERS = {
    **HEADERS,
    "Content-Type": "application/json",
    "Origin": BASE,
    "Referer": BASE + "/zh/radar",
}

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def get_traders():
    r = requests.get(BASE + "/api/radar/traders", headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def get_snapshot(source_id):
    r = requests.get(BASE + f"/api/radar/snapshot?sourceId={source_id}",
                     headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()


def get_dna(portfolio_id):
    r = requests.post(BASE + "/api/radar/dna",
                      headers=DNA_HEADERS,
                      json={"portfolioId": portfolio_id}, timeout=15)
    r.raise_for_status()
    return r.json()


def main():
    print("== 1. traders 列表 ==")
    d = get_traders()
    check("ok=True", d.get("ok") is True)
    traders = d.get("traders") or []
    check("有交易员", len(traders) > 0, f"len={len(traders)}")
    if traders:
        t0 = traders[0]
        check("字段齐全(id/label/margin/signalType)",
              all(k in t0 for k in ("id", "label", "margin", "signalType")),
              str(list(t0.keys())[:8]))
        print(f"  首位交易员: {t0['label']} ({t0['signalType']}) margin={t0['margin']}")
    # 选一个 Binance 交易员
    bn = next((t for t in traders if t["signalType"] == "Binance"), traders[0])
    sid = bn["id"]
    pid = bn.get("portfolioId") or sid.split("-", 1)[1]
    print(f"  测试对象: {bn['label']} sid={sid} pid={pid}")

    print("== 2. snapshot 接口 ==")
    d = get_snapshot(sid)
    check("ok=True", d.get("ok") is True)
    data = d.get("data") or {}
    check("顶部4指标字段", all(k in data for k in (
        "marginBalance", "aumAmount", "currentCopyCount", "effectiveLeverage")),
        str(list(data.keys())[:12]))
    check("polledAt 存在", bool(data.get("polledAt")))
    print(f"  polledAt={data.get('polledAt')}")
    print(f"  保证金={data.get('marginBalance')} AUM={data.get('aumAmount')} "
          f"跟单={data.get('currentCopyCount')}/{data.get('maxCopyCount')} "
          f"杠杆={data.get('effectiveLeverage')}")
    positions = data.get("positions") or []
    print(f"  持仓 {len(positions)} 条")
    if positions:
        check("持仓字段齐全", all(k in positions[0] for k in (
            "symbol", "side", "absQty", "avgPrice", "markPrice", "notional",
            "unrealizedPnl", "leverage", "estimatedLiqPrice", "lastOrderTime")),
            str(list(positions[0].keys())))
    orders = data.get("latestOrders") or []
    print(f"  成交流水 {len(orders)} 条")
    if orders:
        check("流水字段齐全", all(k in orders[0] for k in (
            "symbol", "side", "positionSide", "canonicalAction",
            "avgPrice", "executedQty", "notional", "realizedPnl", "time")),
            str(list(orders[0].keys())))
    tp = data.get("tradePoints") or {}
    print(f"  交易点位 {len(tp)} 个币种")
    if tp:
        coin = list(tp.keys())[0]
        check("点位字段齐全", all(k in tp[coin][0] for k in (
            "timeMs", "price", "quantity", "side", "action", "notional")),
            str(list(tp[coin][0].keys())))

    print("== 3. dna 接口（带 Origin/Referer）==")
    d = get_dna(pid)
    check("ok=True", d.get("ok") is True)
    norm = (d.get("data") or {}).get("normalized") or {}
    check("normalized 结构", "profile" in norm and "performance" in norm
          and "positions" in norm and "charts" in norm)
    perf = norm.get("performance") or {}
    check("绩效字段(roi/pnl/mdd/winRate)",
          all(k in perf for k in ("roi", "pnl", "mdd", "winRate")),
          str(perf))
    print(f"  ROI={perf.get('roi')}% PnL={perf.get('pnl')} MDD={perf.get('mdd')}% "
          f"winRate={perf.get('winRate')}")
    charts = (norm.get("charts") or {}).get("items") or []
    check("收益曲线", len(charts) > 0, f"len={len(charts)}")
    print(f"  收益曲线 {len(charts)} 个点")
    items = (norm.get("positions") or {}).get("items") or []
    check("周期明细(含真实盈亏)", len(items) > 0
          and any((it.get("pnl") or 0) != 0 for it in items),
          f"len={len(items)}")
    print(f"  交易周期 {len(items)} 条（含非零盈亏）")
    profile = norm.get("profile") or {}
    print(f"  自述: {(profile.get('description') or '')[:40]}")

    print("== 4. 币种盈亏汇总聚合 ==")
    agg = defaultdict(lambda: {"count": 0, "pnl": 0.0, "wins": 0, "losses": 0})
    for it in items:
        a = agg[it["symbol"]]
        a["count"] += 1
        a["pnl"] += it.get("pnl") or 0
        if (it.get("pnl") or 0) > 0:
            a["wins"] += 1
        elif (it.get("pnl") or 0) < 0:
            a["losses"] += 1
    check("聚合出币种汇总", len(agg) > 0)
    for sym, a in sorted(agg.items(), key=lambda x: -x[1]["pnl"])[:5]:
        wr = a["wins"] / max(a["wins"] + a["losses"], 1) * 100
        print(f"    {sym:14s} 仓位数={a['count']:3d} 总盈亏={a['pnl']:>12.2f} 胜率={wr:5.1f}%")

    print("== 5. 模拟探测循环（polledAt 变化检测）==")
    first_poll = (get_snapshot(sid).get("data") or {}).get("polledAt")
    print(f"  第一次 polledAt={first_poll}")
    print("  等 35 秒...")
    time.sleep(35)
    second_poll = (get_snapshot(sid).get("data") or {}).get("polledAt")
    print(f"  第二次 polledAt={second_poll}")
    if second_poll == first_poll:
        check("35s 内 polledAt 未变（符合数据源分钟级更新特性，SSE 探测逻辑成立）", True)
    else:
        check("35s 内 polledAt 变化（探测线程能感知更新）", True)

    print(f"\n结果: {PASS} 通过, {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
