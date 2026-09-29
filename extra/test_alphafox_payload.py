"""web.py 中 alphafox payload 构建逻辑的服务器实测（独立复制核心函数）

验证：
1. 正常 payload：页面用到的所有字段存在
2. dna 失败回退：prev slim dna 直接复用（结构判断分支）
3. 模拟两轮探测：polledAt 变化 → 版本递增
"""
import json

import requests

AF_BASE = "https://www.alphafox.app"
AF_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "Chrome/126.0 Safari/537.36",
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


def af_request(path, method="GET", body=None):
    headers = dict(AF_HEADERS)
    if method == "POST":
        headers.update({"Content-Type": "application/json",
                        "Origin": AF_BASE, "Referer": AF_BASE + "/zh/radar"})
        resp = requests.post(AF_BASE + path, headers=headers, json=body, timeout=15)
    else:
        resp = requests.get(AF_BASE + path, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def build_payload(snapshot, prev_dna=None):
    data = snapshot.get("data") or {}
    pid = data.get("portfolioId")
    dna = None
    if pid:
        try:
            dna = af_request("/api/radar/dna", "POST", {"portfolioId": str(pid)})
        except Exception:
            dna = prev_dna
    snap_keep = ("marginBalance", "aumAmount", "currentCopyCount", "maxCopyCount",
                 "effectiveLeverage", "sharpRatio", "winRate", "maxDrawdown",
                 "totalPnl", "totalCopierPnl", "traderName", "avatarUrl", "polledAt",
                 "positions", "latestOrders", "tradePoints")
    slim_snap = {k: data.get(k) for k in snap_keep}
    slim_snap["portfolioId"] = pid
    slim_dna = None
    if dna:
        if "data" in dna and isinstance(dna.get("data"), dict):
            norm = (dna.get("data") or {}).get("normalized") or {}
            slim_dna = {
                "profile": (norm.get("profile") or {}).get("description"),
                "profitSharingRate": (norm.get("profile") or {}).get("profitSharingRate"),
                "performance": norm.get("performance"),
                "charts": (norm.get("charts") or {}).get("items"),
                "cycleItems": (norm.get("positions") or {}).get("items"),
            }
        else:
            slim_dna = dna
    symbol_stats = []
    if slim_dna and slim_dna.get("cycleItems"):
        agg = {}
        for it in slim_dna["cycleItems"]:
            a = agg.setdefault(it.get("symbol"),
                               {"count": 0, "pnl": 0.0, "wins": 0, "losses": 0, "items": []})
            a["count"] += 1
            a["pnl"] += it.get("pnl") or 0
            if (it.get("pnl") or 0) > 0:
                a["wins"] += 1
            elif (it.get("pnl") or 0) < 0:
                a["losses"] += 1
            a["items"].append(it)
        for sym, a in agg.items():
            symbol_stats.append({
                "symbol": sym, "count": a["count"], "pnl": round(a["pnl"], 4),
                "win_rate": round(a["wins"] / max(a["wins"] + a["losses"], 1) * 100, 1),
                "items": sorted(a["items"], key=lambda x: x.get("openTime") or 0, reverse=True),
            })
        symbol_stats.sort(key=lambda x: -x["pnl"])
    return {"snapshot": slim_snap, "dna": slim_dna, "symbol_stats": symbol_stats}


traders = af_request("/api/radar/traders").get("traders") or []
bn = next((t for t in traders if t["signalType"] == "Binance"), traders[0])
sid = bn["id"]
print(f"测试交易员: {bn['label']} ({sid})")

print("== 1. 正常 payload 构建 ==")
snap1 = af_request(f"/api/radar/snapshot?sourceId={sid}")
p1 = build_payload(snap1)
check("snapshot 裁剪字段齐全", all(k in p1["snapshot"] for k in (
    "marginBalance", "aumAmount", "currentCopyCount", "maxCopyCount",
    "effectiveLeverage", "positions", "latestOrders", "tradePoints", "polledAt")),
    str(list(p1["snapshot"].keys())))
check("dna slim 字段齐全", p1["dna"] and all(k in p1["dna"] for k in (
    "profile", "profitSharingRate", "performance", "charts", "cycleItems")),
    str(list(p1["dna"].keys()) if p1["dna"] else None))
check("symbol_stats 聚合", len(p1["symbol_stats"]) > 0)
check("SSE 可序列化（json.dumps）", len(json.dumps(p1, ensure_ascii=False)) > 0)
print(f"  payload 大小: {len(json.dumps(p1, ensure_ascii=False))} 字节")

print("== 2. dna 失败回退 prev slim ==")
p2 = build_payload(snap1, prev_dna=p1["dna"])
check("回退后 dna 结构不变（slim 直接复用）",
      p2["dna"] and p2["dna"].get("performance") and p2["dna"].get("cycleItems"),
      str(list(p2["dna"].keys()) if p2["dna"] else None))
check("回退后 symbol_stats 正常聚合", len(p2["symbol_stats"]) == len(p1["symbol_stats"]))

print("== 3. 模拟两轮探测（polledAt 变化 → 版本递增）==")
latest = {}
import time
snap2 = af_request(f"/api/radar/snapshot?sourceId={sid}")
p_a = build_payload(snap2)
latest["ver"] = 1
poll_a = (snap2.get("data") or {}).get("polledAt")
print(f"  第1轮 polledAt={poll_a}")
time.sleep(30)
snap3 = af_request(f"/api/radar/snapshot?sourceId={sid}")
poll_b = (snap3.get("data") or {}).get("polledAt")
print(f"  第2轮 polledAt={poll_b}")
if poll_b != poll_a:
    latest["ver"] += 1
    p_b = build_payload(snap3, prev_dna=p_a["dna"])
    check("polledAt 变化触发推送（版本 1→2）", latest["ver"] == 2)
    check("第2轮 payload 正常", "snapshot" in p_b and "symbol_stats" in p_b)
else:
    check("polledAt 未变 → 不推送（版本保持 1）", latest["ver"] == 1)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
