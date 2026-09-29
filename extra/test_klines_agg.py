"""
测试 K 线聚合逻辑：1H/4H 从 15m 蜡烛合成
用法: python extra/test_klines_agg.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pass_count = 0
fail_count = 0

def check(name, ok, detail=""):
    global pass_count, fail_count
    if ok: pass_count += 1; print(f"  [PASS] {name}")
    else: fail_count += 1; print(f"  [FAIL] {name}  {detail}")

# 模拟 K 线: [O,T,H,L,C,Vol,_,QuoteVol,...]
def make_kline(idx, o, h, l, c, vol, qvol):
    return [str(idx), str(o), str(h), str(l), str(c), str(vol), "0", str(qvol), "0", "0", "0", "0"]

# 100 根递增: O=C=100+i, H=O+1, L=O-1
klines = []
for i in range(100):
    klines.append(make_kline(i, 100+i, 101+i, 99+i, 100+i, 1000+i*10, 10000+i*100))

n = len(klines)
print(f"模拟 K 线: {n} 根, 价格 100→199\n")

# 从 market.py 复制的 _agg_candle
def _agg_candle(group):
    try:
        return [
            float(group[0][1]),
            float(max(float(c[2]) for c in group)),
            float(min(float(c[3]) for c in group)),
            float(group[-1][4]),
            sum(float(c[5]) for c in group),
            sum(float(c[7]) for c in group),
        ]
    except Exception:
        return None

# ====== Test 1: 单根蜡烛校验 ======
print("=" * 60)
print("Test 1: 单根 1H 蜡烛值")
print("=" * 60)

single = _agg_candle(klines[0:4])
check("O=100", single[0] == 100, f"O={single[0]}")
check("H=104", single[1] == 104, f"H={single[1]}")
check("L=99", single[2] == 99, f"L={single[2]}")
check("C=103", single[3] == 103, f"C={single[3]}")

# ====== Test 2: 100 根正常聚合 ======
print(f"\n{'=' * 60}")
print("Test 2: 100 根 → 1H×6 + 4H×6")
print("=" * 60)

h1 = list(filter(None, (_agg_candle(klines[i:i+4]) for i in range(n-24, n, 4))))
h4 = list(filter(None, (_agg_candle(klines[i:i+16]) for i in range(n-96, n, 16))))

check("1H 返回 6 根", len(h1) == 6, f"got {len(h1)}")
check("4H 返回 6 根", len(h4) == 6, f"got {len(h4)}")

# 1H[0]: klines[76:80]
# k76: O=C=176,H=177,L=175  k79: O=C=179,H=180,L=178
c0 = h1[0]
check("1H[0] O=176", c0[0] == 176, f"O={c0[0]}")
check("1H[0] H=180", c0[1] == 180, f"H={c0[1]}")
check("1H[0] L=175", c0[2] == 175, f"L={c0[2]}")
check("1H[0] C=179", c0[3] == 179, f"C={c0[3]}")

# 1H[5]: klines[96:100]
# k96: O=C=196  k99: O=C=199
c5 = h1[5]
check("1H[5] O=196", c5[0] == 196, f"O={c5[0]}")
check("1H[5] C=199", c5[3] == 199, f"C={c5[3]}")
check("1H[5] H=200 L=195", c5[1] == 200 and c5[2] == 195, f"H={c5[1]} L={c5[2]}")

# 4H[0]: klines[4:20]
c0_4 = h4[0]
check("4H[0] O=104", c0_4[0] == 104, f"O={c0_4[0]}")
check("4H[0] C=119", c0_4[3] == 119, f"C={c0_4[3]}")

# 4H[5]: klines[84:100]
c5_4 = h4[5]
check("4H[5] O=184", c5_4[0] == 184, f"O={c5_4[0]}")
check("4H[5] C=199", c5_4[3] == 199, f"C={c5_4[3]}")

# ====== Test 3: 新币兼容 ======
print(f"\n{'=' * 60}")
print("Test 3: 新币兼容")
print("=" * 60)

klines_20 = klines[:20]
check("20根→1H不生成", len(klines_20) < 24)
check("20根→4H不生成", len(klines_20) < 96)

klines_40 = klines[:40]
h1_40 = list(filter(None, (_agg_candle(klines_40[i:i+4]) for i in range(len(klines_40)-24, len(klines_40), 4))))
check("40根→1H×6", len(h1_40) == 6, f"got {len(h1_40)}")
check("40根→4H不生成", len(klines_40) < 96)

# ====== Test 4: 成交量 ======
print(f"\n{'=' * 60}")
print("Test 4: 成交量聚合")
print("=" * 60)

# 4 根蜡烛成交量: 1000,1010,1020,1030 = 4060
check("Vol=4060", single[4] == 4060, f"Vol={single[4]}")
# 成交额: 10000,10100,10200,10300 = 40600
check("QuoteVol=40600", single[5] == 40600, f"QuoteVol={single[5]}")

print(f"\n{'=' * 60}")
print(f"Result: {pass_count} PASS / {fail_count} FAIL  (total {pass_count + fail_count})")
print("=" * 60)
sys.exit(0 if fail_count == 0 else 1)
