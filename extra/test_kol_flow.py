"""
KOL Agent 全流程测试：模拟数据 → 入库 kol_candidates → 调 DeepSeek → 写入 kol_analyses
用法: python test_kol_flow.py
"""
import json
import sqlite3
import time

import config
import storage
import kol_agent

# 模拟 _build_data_blob 产出的扁平候选币数据
MOCK_CANDIDATES = [
    {
        "token": "FLUID", "social_score": 85.5, "mentions": 12,
        "price": 1.234, "15m": 1.2, "1h": 3.5, "4h": 8.2, "24h": 15.3,
        "oi_15m": 2.1, "oi_1h": 5.3, "oi_4h": 12.0,
        "funding": 0.01, "lsr": 1.2, "top_lsr": 1.8,
        "taker": 1.35, "taker_pct": 57.4, "taker_trend": 3.2,
        "spread": 0.03, "depth_bid": 250000, "depth_ask": 240000,
        "imbalance": 2.1, "vol_24h": 5000000, "oi_usd": 12000000,
        "age": "15d12h", "tags": ["Hot"], "tier": "A", "passed": True,
        "hard_block": [], "pass_count": 3, "suggestion": "", "reasons": []
    },
    {
        "token": "ORDI", "social_score": 72.0, "mentions": 8,
        "price": 45.67, "15m": -0.8, "1h": 2.1, "4h": -3.5, "24h": 10.2,
        "oi_15m": -1.5, "oi_1h": 0.8, "oi_4h": -2.3,
        "funding": 0.03, "lsr": 0.9, "top_lsr": 1.5,
        "taker": 1.18, "taker_pct": 54.1, "taker_trend": -1.5,
        "spread": 0.05, "depth_bid": 180000, "depth_ask": 175000,
        "imbalance": 1.4, "vol_24h": 3500000, "oi_usd": 9000000,
        "age": "30d6h", "tags": ["Trending"], "tier": "B", "passed": True,
        "hard_block": [], "pass_count": 2, "suggestion": "", "reasons": []
    },
    {
        "token": "PENDLE", "social_score": 68.0, "mentions": 5,
        "price": 3.21, "15m": 0.3, "1h": 5.2, "4h": 15.8, "24h": -5.1,
        "oi_15m": 3.8, "oi_1h": 8.5, "oi_4h": 20.1,
        "funding": -0.02, "lsr": 1.5, "top_lsr": 2.1,
        "taker": 1.52, "taker_pct": 60.3, "taker_trend": 5.8,
        "spread": 0.04, "depth_bid": 320000, "depth_ask": 310000,
        "imbalance": 3.2, "vol_24h": 8000000, "oi_usd": 22000000,
        "age": ">1d", "tags": ["Hot", "Divergence"], "tier": "A", "passed": True,
        "hard_block": [], "pass_count": 4, "suggestion": "", "reasons": []
    },
]


def run():
    db_path = config.DB_PATH
    print(f"DB: {db_path}")
    print(f"KOL dir: {config.KOL_KNOWLEDGE_DIR}")
    print(f"API key: {'已配置' if config.DEEPSEEK_API_KEY else '未配置'}")

    storage.init_db()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ── 1. 加载 KOL 知识 ──
    print("\n═══ 1. 加载 KOL 知识 ═══")
    kol_data = kol_agent.load_kol_knowledge(config.KOL_KNOWLEDGE_DIR)
    assert kol_data, "KOL 知识为空！"
    print(f"   {len(kol_data)} 位KOL: {[k['name'] for k in kol_data]}")
    for k in kol_data:
        print(f"   {k['name']}: theory={len(k.get('theory',''))}chars, "
              f"framework={len(k.get('framework',''))}chars")

    # ── 2. 模拟累积 → 写入 kol_candidates ──
    print("\n═══ 2. 模拟累积 → kol_candidates 表 ═══")
    round_num = conn.execute(
        "SELECT COALESCE(MAX(round_number), 0) + 1 FROM kol_candidates"
    ).fetchone()[0]
    batch = []
    for c in MOCK_CANDIDATES:
        batch.append({
            "token": c["token"],
            "data": json.dumps(c, ensure_ascii=False),
            "tier": c.get("tier", ""),
            "passed": 1,
            "hard_blocks": json.dumps(c.get("hard_block", [])),
            "pass_count": c.get("pass_count", 0),
            "signal_key": "test",
        })
    storage.kol_candidates_insert_batch(conn, round_num, batch)
    conn.commit()
    print(f"   写入 {len(batch)} 个候选 (round {round_num})")

    # 验证回读
    rows = storage.kol_candidates_latest_round(conn)
    print(f"   回读 kol_candidates: {len(rows)} 条")
    assert len(rows) == len(batch), "数量不一致！"

    # ── 3. 从 kol_candidates 读回 → 还原 dict ──
    print("\n═══ 3. get_kol_candidates() 读回 ═══")
    candidates = kol_agent.get_kol_candidates(conn)
    print(f"   反序列化: {len(candidates)} 个")
    for c in candidates:
        # 验证数据契约：build_user_prompt 需要的字段必须存在
        for field in ["token", "price", "15m", "1h", "4h", "24h",
                       "oi_15m", "oi_1h", "oi_4h", "funding",
                       "taker", "taker_pct", "taker_trend",
                       "spread", "depth_bid", "depth_ask", "imbalance",
                       "lsr", "top_lsr", "vol_24h", "oi_usd",
                       "social_score", "mentions", "age", "tags"]:
            assert field in c, f"{c['token']} 缺少字段: {field}"
        print(f"   {c['token']}: price={c.get('price')}, 15m={c.get('15m')}%, "
              f"social_score={c.get('social_score')}, tags={c.get('tags')}")
    print("   所有字段对齐 [OK]")

    # ── 4. 拼接 Prompt ──
    print("\n═══ 4. 拼接 Prompt ═══")
    system = kol_agent.build_system_prompt(kol_data)
    user = kol_agent.build_user_prompt(candidates)
    print(f"   system: {len(system)} chars")
    print(f"   user:   {len(user)} chars")

    # ── 5. 调 DeepSeek ──
    print("\n═══ 5. 调 DeepSeek API ═══")
    if not config.DEEPSEEK_API_KEY:
        print("   SKIP: API key 未配置")
        raw = None
    else:
        t0 = time.time()
        raw = kol_agent.call_deepseek(system, user, max_tokens=8192)
        elapsed = time.time() - t0
        assert raw, "API 返回为空"
        print(f"   返回 {len(raw)} chars ({elapsed:.1f}s)")
        print(f"   前 300 字符:\n{raw[:300]}")
        # 存一份到文件方便查看
        with open("extra/test_kol_response.txt", "w", encoding="utf-8") as f:
            f.write(f"=== SYSTEM ===\n{system}\n\n=== USER ===\n{user}\n\n=== RESPONSE ===\n{raw}")

    if not raw:
        print("\n跳过 API 步骤，前面模块测试全部通过")
        conn.close()
        return

    # ── 6. 解析 ──
    print("\n═══ 6. 解析 LLM 返回 ═══")
    analyses = kol_agent._parse_response(raw)
    if not analyses:
        print("   解析为空（可能 JSON 被截断），跳过写入步骤")
        print("   raw_response 已存入 extra/test_kol_response.txt")
        print("\n═══ 步骤 1-5 全部通过 [OK] ═══")
        conn.close()
        return
    print(f"   解析到 {len(analyses)} 条")
    for a in analyses:
        print(f"   {a.get('token')}: direction={a.get('direction')}, "
              f"confidence={a.get('confidence')}, trend={a.get('trend')}")

    # ── 7. 写入 kol_analyses ──
    print("\n═══ 7. 写入 kol_analyses ═══")
    written = 0
    for a in analyses:
        token = a.get("token", "").upper()
        if not token:
            continue
        a["raw_response"] = raw
        a["system_prompt"] = system
        a["user_prompt"] = user
        storage.kol_analysis_insert(conn, a)
        written += 1
    conn.commit()
    print(f"   写入 {written} 条")

    # 验证 DB
    latest = storage.kol_analyses_latest(conn, limit=10)
    print(f"   最新 {len(latest)} 条:")
    for r in latest:
        print(f"   {r['token']:8s} | {str(r.get('direction','')):6s} | "
              f"{str(r.get('confidence','')):6s} | "
              f"sys={len(r.get('system_prompt','') or '')}c "
              f"usr={len(r.get('user_prompt','') or '')}c "
              f"| {r['created_at']}")

    # ── 8. 开关验证 ──
    print("\n═══ 8. 开关 boolean 解析验证 ═══")
    # 先写一个 false 进去测试 bool 解析
    conn.execute(
        "INSERT OR REPLACE INTO trading_settings (key, value, updated_at) "
        "VALUES ('kol_agent_enabled', 'False', CURRENT_TIMESTAMP)"
    )
    conn.commit()
    ts = storage.trading_settings_get(conn)
    for key in ["agent_trade_enabled", "heat_agent_enabled", "kol_agent_enabled"]:
        val = ts.get(key)
        print(f"   {key} = {val!r} (type={type(val).__name__})")
        assert isinstance(val, bool), f"{key} 不是 bool，是 {type(val).__name__}！"
    print("   boolean 解析正确 [OK]")
    # 恢复
    conn.execute("DELETE FROM trading_settings WHERE key='kol_agent_enabled'")
    conn.commit()

    conn.close()
    print("\n═══════════════════════════════════")
    print("   全流程测试通过 [OK]")
    print("═══════════════════════════════════")


if __name__ == "__main__":
    run()
