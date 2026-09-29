"""
输出 KOL LLM 构建完的 system prompt + user prompt
用法: python3 extra/test_kol_prompt.py
输出: extra/test_prompt_output.txt
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

# 临时覆盖路径
config.KOL_KNOWLEDGE_DIR = r"C:\Users\Administrator\Desktop\小龙虾学习手册\MyAi\x"

from kol_agent import (
    load_kol_knowledge, build_system_prompt, build_user_prompt,
    _build_panorama_context, get_kol_candidates,
)

out_lines = []

# ====== 1. System Prompt ======
out_lines.append("=" * 80)
out_lines.append("SYSTEM PROMPT")
out_lines.append("=" * 80)

kol_dir = config.KOL_KNOWLEDGE_DIR
kol_data = load_kol_knowledge(kol_dir) if kol_dir else []
if kol_data:
    system = build_system_prompt(kol_data)
    out_lines.append(f"\nKOL 数量: {len(kol_data)}")
    out_lines.append(f"System prompt 长度: {len(system)} 字符")
    out_lines.append(f"\n{system}")

# ====== 2. Panorama ======
out_lines.append("\n\n" + "=" * 80)
out_lines.append("PANORAMA CONTEXT")
out_lines.append("=" * 80)
panorama = _build_panorama_context()
out_lines.append(f"\n{panorama}")

# ====== 3. User Prompt ======
out_lines.append("=" * 80)
out_lines.append("USER PROMPT (候选币数据)")
out_lines.append("=" * 80)

# 构建 mock 候选币数据（含新字段）
mock_candidate = {
    "token": "EXAMPLE",
    "price": 123.4567,
    "15m": -0.35,
    "1h": 1.28,
    "4h": 5.67,
    "24h": -3.21,
    "oi_15m": 2.15,
    "oi_1h": 4.83,
    "oi_4h": 12.50,
    "funding": 0.015,
    "taker": 1.23,
    "taker_pct": 55.2,
    "taker_trend": 18.5,
    "spread": 0.023,
    "depth_bid": 450000,
    "depth_ask": 380000,
    "imbalance": 8.5,
    "lsr": 1.35,
    "top_lsr": 2.10,
    "vol_24h": 85000000,
    "oi_usd": 125000000,
    "age": ">1d",
    "vol_oi": 0.68,
    "listing_days": 120,
    "sector": "AI",
    "chain": "Solana",
    "basis": -0.15,
    "okx_funding": 0.012,
    "liq_long_usd": 250000,
    "liq_short_usd": 180000,
    "liq_long_cnt": 45,
    "liq_short_cnt": 32,
    "tags": ["funding:正常", "depth:足", "聪明钱:多/散户:空"],
    # 新字段
    "ma20_deviation": 3.25,
    "market_cap_usd": 2500000000,
    "market_cap_rank": 45,
    "sm_long_ratio": 62.5,
    "sm_net_notional_usdt": 1500000,
    "sm_avg_long_win_rate": 78.3,
    "sm_traders_with_position": 28,
    "oi_marketcap": 5.0,
}

user = build_user_prompt([mock_candidate])
out_lines.append(f"\n候选币示例 (1 个):")
out_lines.append(user)

# ====== 4. 完整 Prompt ======
out_lines.append("\n\n" + "=" * 80)
out_lines.append("完整 PROMPT = PANORAMA + USER")
out_lines.append("=" * 80)
full = panorama + "\n" + user
out_lines.append(f"\n总长度: {len(full)} 字符 (panorama={len(panorama)}, user={len(user)})")
out_lines.append(f"\n{full}")

# ====== 5. 统计 ======
out_lines.append("\n\n" + "=" * 80)
out_lines.append("统计")
out_lines.append("=" * 80)
out_lines.append(f"System prompt:  {len(system)} 字符")
out_lines.append(f"Panorama:       {len(panorama)} 字符")
out_lines.append(f"User prompt:    {len(user)} 字符 (1 候选币)")
out_lines.append(f"完整 user:      {len(full)} 字符")
out_lines.append(f"总计 (sys+user): {len(system) + len(full)} 字符")

# 写入文件
output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_prompt_output.txt")
with open(output_path, "w", encoding="utf-8") as f:
    f.write("\n".join(out_lines))

print(f"Done! Output: {output_path}")
print(f"System prompt: {len(system)} chars")
print(f"User prompt (panorama+data): {len(full)} chars")
print(f"Total: {len(system) + len(full)} chars")
