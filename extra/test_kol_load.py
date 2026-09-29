"""
测试 _load_kol_files 只读取 TradeFramework 目录
用法: python extra/test_kol_load.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

# 临时覆盖路径为本地
config.KOL_KNOWLEDGE_DIR = r"C:\Users\Administrator\Desktop\小龙虾学习手册\MyAi\x"

from kol_agent import _load_kol_files, _build_kol_section, build_system_prompt

pass_count = 0
fail_count = 0

def check(name, ok, detail=""):
    global pass_count, fail_count
    if ok:
        pass_count += 1
        print(f"  [PASS] {name}")
    else:
        fail_count += 1
        print(f"  [FAIL] {name}  {detail}")


# ============================================================
# Test 1: 文件加载
# ============================================================
print("=" * 60)
print("Test 1: 加载 TradeFramework 文件")
print("=" * 60)

kol_data = _load_kol_files(config.KOL_KNOWLEDGE_DIR)
check("返回 list", isinstance(kol_data, list))
check("返回 6 个 KOL", len(kol_data) == 6, f"got {len(kol_data)}")

# 验证每个 KOL 有 name + framework
expected_kols = {"Arya_web3", "BTC_Alert", "CryptoRounder", "derrrrrrrq", "lanaaielsa", "thecryptoskanda"}
loaded_names = set()
for k in kol_data:
    loaded_names.add(k["name"])
    check(f"  {k['name']} has framework", len(k.get("framework", "")) > 100,
          f"len={len(k.get('framework',''))}")
    check(f"  {k['name']} no theory key", "theory" not in k,
          f"unexpected theory key")
    check(f"  {k['name']} no datasource key", "datasource" not in k,
          f"unexpected datasource key")

check("全部 6 位 KOL 已加载", loaded_names == expected_kols,
      f"missing={expected_kols - loaded_names}, extra={loaded_names - expected_kols}")


# ============================================================
# Test 2: 排除非 KOL 文件
# ============================================================
print(f"\n{'=' * 60}")
print("Test 2: 排除非 KOL 文件")
print("=" * 60)

# "#交易框架.md" 没有 "__" 分隔符，split("__")[0].strip() 后为空
# 应在 _load_kol_files 中被 filter 排除
has_hash = any("#" in k["name"] for k in kol_data)
check("#交易框架.md 未被加载", not has_hash, "意外加载了 #交易框架.md")

# 确认只有 __TradeFramework.md 格式的文件
all_names = [k["name"] for k in kol_data]
check("所有 name 不含 #", all("#" not in n for n in all_names))
check("所有 name 不含 __", all("__" not in n for n in all_names))


# ============================================================
# Test 3: _build_kol_section 输出
# ============================================================
print(f"\n{'=' * 60}")
print("Test 3: _build_kol_section 输出校验")
print("=" * 60)

btc = [k for k in kol_data if k["name"] == "BTC_Alert"][0]
section = _build_kol_section(btc)
check("含 KOL 名称", "BTC_Alert" in section)
check("含 '交易框架' 标题", "交易框架" in section)
check("不含 '核心理论'", "核心理论" not in section,
      f"found 核心理论 in section")
check("不含 '数据源与工具'", "数据源与工具" not in section,
      f"found 数据源与工具 in section")
check("以换行结尾", section.endswith("\n"))
check("内容 > 200 字符", len(section) > 200, f"len={len(section)}")


# ============================================================
# Test 4: build_system_prompt 输出
# ============================================================
print(f"\n{'=' * 60}")
print("Test 4: build_system_prompt 输出校验")
print("=" * 60)

prompt = build_system_prompt(kol_data)
check("含 '交易框架'", "交易框架" in prompt)
check("不含 '交易理论体系'", "交易理论体系" not in prompt,
      "prompt 仍然包含过时的 '交易理论体系' 文案")
check("不含 '核心理论'", "核心理论" not in prompt)
check("不含 '数据源'", "数据源与工具" not in prompt)
check("含 KOL 名称 BTC_Alert", "BTC_Alert" in prompt)
check("prompt > 5000 字符", len(prompt) > 5000, f"len={len(prompt)}")

# 确认所有 6 位 KOL 都在 prompt 中
for name in expected_kols:
    check(f"prompt 含 {name}", name in prompt,
          f"{name} not found in system prompt")

print(f"\n{'=' * 60}")
print(f"Result: {pass_count} PASS / {fail_count} FAIL  (total {pass_count + fail_count})")
print("=" * 60)

sys.exit(0 if fail_count == 0 else 1)
