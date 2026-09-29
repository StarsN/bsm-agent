"""
从 DB 读取最近 10 个快照 token，用 CLI 直拉新字段值
用法: python3 extra/check_new_fields.py
"""
import subprocess, json, sys, os, sqlite3

def _okx(args, live=False):
    cmd = ["okx"]
    if live: cmd += ["--profile", "live"]
    cmd += args + ["--json"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if r.returncode != 0 or not r.stdout.strip(): return None
        out = r.stdout.strip()
        for i, ch in enumerate(out):
            if ch in ('{', '['): return json.loads(out[i:])
        return None
    except: return None

# ====== 从 DB 取最近 10 个 token ======
db = "binance_square.db"
root = os.environ.get("AGENT_DB_ROOT", "/root/binance-monitor/bsm-agent")
if os.path.exists(os.path.join(root, db)): db = os.path.join(root, db)
conn = sqlite3.connect(db)
cur = conn.cursor()
cur.execute("SELECT token, snapshot FROM market_snapshots ORDER BY rowid DESC LIMIT 10")
rows = cur.fetchall()
conn.close()

tokens = list(dict.fromkeys([r[0] for r in rows]))
print(f"最近 10 条快照 token: {tokens}\n")

# ====== 1. 市值（TOP100 批量查，不在的就显示 -）======
print(f"{'Token':<10} {'市值':>14} {'排名':>6}")
print("-" * 34)
mc_result = _okx(["market", "filter", "--instType", "SPOT",
    "--sortBy", "marketCapUsd", "--sortOrder", "desc",
    "--limit", "100", "--quoteCcy", "USDT"])
mc_map = {}
if mc_result and isinstance(mc_result, list):
    for row in mc_result[0].get("rows", []):
        t = row.get("baseCcy", "").upper()
        if t in tokens:
            mc_map[t] = (float(row.get("marketCapUsd", 0)), int(row.get("rank", 0)))
# 补拉 TOP100 未覆盖的
missing = [t for t in tokens if t not in mc_map]
if missing:
    print(f"  (TOP100 未覆盖，补拉: {missing})")
    for t in missing:
        d = _okx(["market", "filter", "--instType", "SPOT", "--baseCcy", t, "--limit", "1"])
        if d and isinstance(d, list) and d[0].get("rows"):
            r = d[0]["rows"][0]
            b = r.get("baseCcy", "").upper()
            # 只接受精确匹配
            if b == t:
                mc_map[t] = (float(r.get("marketCapUsd", 0)), int(r.get("rank", 0)))
            else:
                print(f"    {t}: baseCcy 不匹配 (got '{b}'), 跳过")
        else:
            print(f"    {t}: 无数据")

for t in tokens:
    mc, rank = mc_map.get(t, (None, None))
    if mc and mc > 0:
        print(f"{t:<10} {mc/1e9:>11.1f}B {rank:>6}")
    else:
        print(f"{t:<10} {'-':>14} {'-':>6}")

# ====== 2. 聪明钱 ======
print(f"\n{'Token':<10} {'SM多%':>8} {'SM净头寸':>14} {'SM人数':>6} {'多胜率':>8} {'空胜率':>8}")
print("-" * 62)
sm_data = _okx(["smartmoney", "signal-overview-by-filter",
    "--instCcyList", ",".join(tokens), "--period", "7"], live=True)
if sm_data and isinstance(sm_data, dict):
    found = set()
    for item in sm_data.get("data", []):
        t = item.get("ccy", "").upper()
        if t not in tokens: continue
        found.add(t)
        lsr = item.get("longShortRatio", {}) or {}
        nt = item.get("notional", {}) or {}
        wr = item.get("winRate", {}) or {}
        print(f"{t:<10} {float(lsr.get('longRatio',0) or 0)*100:>7.1f}% "
              f"{float(nt.get('netNotionalUsdt',0) or 0)/1e6:>+12.1f}M "
              f"{int(item.get('tradersWithPosition',0) or 0):>6} "
              f"{float(wr.get('avgLongWinRate',0) or 0)*100:>7.1f}% "
              f"{float(wr.get('avgShortWinRate',0) or 0)*100:>7.1f}%")
    not_found = [t for t in tokens if t not in found]
    if not_found:
        print(f"  (无聪明钱数据: {not_found})")
else:
    print("  聪明钱查询失败")

# ====== 3. MA20 乖离率 ======
print(f"\n{'Token':<10} {'现价':>12} {'MA20(5h)':>12} {'乖离率':>8}")
print("-" * 46)
for t in tokens:
    klines = _okx(["market", "candles", f"{t}-USDT-SWAP", "--bar", "15m", "--limit", "25"])
    if klines and isinstance(klines, list) and len(klines) >= 20:
        closes = [float(k[4]) for k in klines]
        now = closes[-1]; ma20 = sum(closes[-20:]) / 20
        print(f"{t:<10} {now:>12.4f} {ma20:>12.4f} {(now-ma20)/ma20*100:>+7.2f}%")
    else:
        print(f"{t:<10} {'无K线':>30}")

print()
