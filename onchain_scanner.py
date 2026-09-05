"""
链上监控扫描器：Stage 1 吸筹检测 + Stage 2 启动准备
后台 daemon 线程，每 N 小时全量扫描 BSC 代币
"""
import time
import json
from datetime import datetime, timezone
import config
import storage
from okx_source import (
    fetch_hot_tokens,
    fetch_cluster_overview,
    fetch_cluster_list,
    fetch_liquidity,
    fetch_wallet_analysis,
    fetch_token_trades,
)
from market import has_perpetual, has_spot

_round = 0
_running = False
_scanner_started = False


def _utcnow():
    return datetime.now(timezone.utc)


def _load_setting(key: str, default):
    """从 DB trading_settings 读取覆盖值，不存在则用 config 默认值"""
    try:
        with storage.get_conn() as conn:
            s = storage.trading_settings_get(conn)
            val = s.get(key)
            if val is not None:
                return val
    except Exception:
        pass
    return default


def run_round():
    """执行一轮完整扫描"""
    global _round
    _round += 1
    round_number = _round

    # 从 DB 读取阈值（允许设置页动态调整）
    fund_source_min = _load_setting("onchain_divergence_fund_source_min",
                                    getattr(config, "ONCHAIN_DIVERGENCE_FUND_SOURCE_MIN", 30.0))
    surface_top10_max = _load_setting("onchain_surface_top10_max",
                                      getattr(config, "ONCHAIN_SURFACE_TOP10_MAX", 40.0))
    max_days = _load_setting("onchain_observation_max_days",
                             getattr(config, "ONCHAIN_OBSERVATION_MAX_DAYS", 7))

    # 读取扫描链配置
    _chain = getattr(config, "ONCHAIN_SCAN_CHAINS", "all")
    _chain = None if _chain == "all" else _chain  # None → 全链

    _chain_label = _chain or "全链"
    _write_status(stage="scanning", detail=f"Step A: 拉取 {_chain_label} 热门代币...",
                  round_number=round_number, last_scan_start=_utcnow())

    # ---- Step A: 候选筛选 ----
    candidates = fetch_hot_tokens(
        chain=_chain,
        mc_min=500000,
        limit=getattr(config, "ONCHAIN_MAX_CANDIDATES", 50),
        price_min=getattr(config, "ONCHAIN_PRICE_CHANGE_MIN", -5),
        price_max=getattr(config, "ONCHAIN_PRICE_CHANGE_MAX", 5),
    )

    # 排除已晋级的 token + 黑名单 + 全局排除列表
    if candidates:
        try:
            with storage.get_conn(config.ONCHAIN_DB) as conn:
                actionable = {r["token"] for r in storage.onchain_pool_get_all(conn, "actionable")}
                blacklist = {r["token"] for r in storage.onchain_blacklist_get_all(conn)}
            excluded = getattr(config, "EXCLUDED_TOKENS", set()) or set()
            before = len(candidates)
            candidates = [c for c in candidates if c["token"].upper() not in actionable
                          and c["token"].upper() not in blacklist
                          and c["token"].upper() not in excluded]
            skipped = before - len(candidates)
            if skipped:
                print(f"[onchain] Step A: 跳过 {skipped} 个已在建仓池/黑名单的 token")
        except Exception:
            pass

    if not candidates:
        print("[onchain] Step A: 无新候选代币，仅刷新观察池")

    if candidates:
        print(f"[onchain] Step A: {len(candidates)} 个候选代币（横盘）")

    _write_status(stage="analyzing",
                  detail=f"Step B: 聚类分析 {len(candidates)} 个代币...+ 观察池刷新",
                  tokens_scanned=0, candidates_found=len(candidates))

    # ---- Step B: 逐币聚类分析 ----
    snapshots = []
    divergence_tokens = []
    cluster_records = []

    for i, c in enumerate(candidates):
        token = c["token"]
        addr = c["token_address"]
        if not addr:
            continue

        # 只保留有币安合约的代币
        if not has_perpetual(token):
            continue
        # 排除已有现货的代币（仅保留合约专属）
        if has_spot(token):
            continue

        token_chain = c.get("chain", "bsc")
        print(f"  [{i+1}/{len(candidates)}] {token} ({token_chain})...", flush=True)

        # 聚合查询
        overview = fetch_cluster_overview(addr, chain=token_chain)
        time.sleep(0.3)

        cluster_list, cluster_raw = fetch_cluster_list(addr, chain=token_chain)
        time.sleep(0.3)

        # 计算有效集中度：过滤交易所+合约后的集群持有量
        effective_conc = 0.0
        if cluster_list:
            for cl in cluster_list:
                # 检查集群中是否有交易所/合约地址
                has_exchange = any(a["is_exchange"] for a in cl["addresses"])
                has_contract = any(a["is_contract"] for a in cl["addresses"])
                if not has_exchange and not has_contract:
                    effective_conc += cl["holding_percent"] or 0.0

        # 存储集群详情（供集群监控页面使用）
        if _load_setting("onchain_cluster_enabled", True) and cluster_list:
            for ci, cl in enumerate(cluster_list):
                has_ex = any(a["is_exchange"] for a in cl["addresses"])
                has_ct = any(a["is_contract"] for a in cl["addresses"])
                cluster_records.append({
                    "token": token, "token_address": addr,
                    "chain": token_chain, "cluster_index": ci,
                    "holding_percent": round((cl["holding_percent"] or 0) * 100, 2),
                    "trend_type": json.dumps(cl.get("trend_type", [])),
                    "pnl_percent": cl.get("pnl_percent"),
                    "address_count": len(cl["addresses"]),
                    "is_real_cluster": 0 if (has_ex or has_ct) else 1,
                    "addresses_json": json.dumps(cl["addresses"], ensure_ascii=False),
                })

        fund_pct = (overview or {}).get("holder_same_fund_pct") or 0.0
        top10_pct = c.get("top10_hold_pct") or 0.0

        # 背离检测
        divergence_score = 0.0
        divergence_detected = False
        if fund_pct and top10_pct:
            if fund_pct > fund_source_min and top10_pct < surface_top10_max:
                divergence_detected = True
                divergence_score = round((fund_pct - fund_source_min) * (surface_top10_max - top10_pct) / 100, 2)

        snapshot = {
            "token": token,
            "token_address": addr,
            "chain": c.get("chain", "bsc"),
            "hot_rank": c.get("hot_rank"),
            "top10_hold_pct": top10_pct,
            "holders_count": c.get("holders_count"),
            "market_cap_usd": c.get("market_cap_usd"),
            "cluster_concentration": (overview or {}).get("cluster_concentration", ""),
            "top100_holdings_pct": (overview or {}).get("top100_holdings_pct"),
            "holder_same_fund_pct": fund_pct,
            "holder_new_addr_pct": (overview or {}).get("holder_new_addr_pct"),
            "holder_same_creation_pct": (overview or {}).get("holder_same_creation_pct"),
            "rug_pull_pct": (overview or {}).get("rug_pull_pct"),
            "effective_concentration": round(effective_conc * 100, 2) if effective_conc else None,
            "divergence_score": divergence_score,
            "cluster_raw_json": cluster_raw,
            "cluster_list_json": json.dumps(cluster_list, ensure_ascii=False) if cluster_list else None,
            "overview_ok": overview is not None,
        }
        snapshots.append(snapshot)

        if divergence_detected:
            divergence_tokens.append(snapshot)
            print(f"    ⚡ 背离: fundSrc={fund_pct}% top10={top10_pct}% effConc={snapshot['effective_concentration']}%")

    # ---- Step B2: 观察池 token 补充扫描 ----
    scanned = {c["token"] for c in candidates}
    pool_tokens = []
    black_set = set()
    try:
        with storage.get_conn(config.ONCHAIN_DB) as conn:
            pool_tokens = storage.onchain_pool_get_all(conn, "observation")
            black_set = {r["token"] for r in storage.onchain_blacklist_get_all(conn)}
    except Exception:
        pool_tokens = []

    for pt in pool_tokens:
        token = pt["token"]
        if token in scanned or token in black_set:
            continue
        addr = pt.get("token_address")
        if not addr:
            continue
        token_chain = pt.get("chain", "bsc")
        print(f"  [pool] {token} ({token_chain}) 刷新快照...", flush=True)

        overview = fetch_cluster_overview(addr, chain=token_chain)
        time.sleep(0.3)
        cluster_list, cluster_raw = fetch_cluster_list(addr, chain=token_chain)
        time.sleep(0.3)

        effective_conc = 0.0
        if cluster_list:
            for cl in cluster_list:
                has_exchange = any(a["is_exchange"] for a in cl["addresses"])
                has_contract = any(a["is_contract"] for a in cl["addresses"])
                if not has_exchange and not has_contract:
                    effective_conc += cl["holding_percent"] or 0.0

        if _load_setting("onchain_cluster_enabled", True) and cluster_list:
            for ci, cl in enumerate(cluster_list):
                has_ex = any(a["is_exchange"] for a in cl["addresses"])
                has_ct = any(a["is_contract"] for a in cl["addresses"])
                cluster_records.append({
                    "token": token, "token_address": addr,
                    "chain": token_chain, "cluster_index": ci,
                    "holding_percent": round((cl["holding_percent"] or 0) * 100, 2),
                    "trend_type": json.dumps(cl.get("trend_type", [])),
                    "pnl_percent": cl.get("pnl_percent"),
                    "address_count": len(cl["addresses"]),
                    "is_real_cluster": 0 if (has_ex or has_ct) else 1,
                    "addresses_json": json.dumps(cl["addresses"], ensure_ascii=False),
                })

        fund_pct = (overview or {}).get("holder_same_fund_pct") or 0.0

        snapshot = {
            "token": token,
            "token_address": addr,
            "chain": token_chain,
            "hot_rank": None,
            "top10_hold_pct": pt.get("surface_top10_pct") or 0.0,
            "holders_count": None,
            "market_cap_usd": None,
            "cluster_concentration": (overview or {}).get("cluster_concentration", ""),
            "top100_holdings_pct": (overview or {}).get("top100_holdings_pct"),
            "holder_same_fund_pct": fund_pct,
            "holder_new_addr_pct": (overview or {}).get("holder_new_addr_pct"),
            "holder_same_creation_pct": (overview or {}).get("holder_same_creation_pct"),
            "rug_pull_pct": (overview or {}).get("rug_pull_pct"),
            "effective_concentration": round(effective_conc * 100, 2) if effective_conc else None,
            "divergence_score": 0.0,
            "cluster_raw_json": cluster_raw,
            "cluster_list_json": json.dumps(cluster_list, ensure_ascii=False) if cluster_list else None,
            "overview_ok": overview is not None,
        }
        snapshots.append(snapshot)

    # 写快照
    with storage.get_conn(config.ONCHAIN_DB) as conn:
        storage.onchain_snapshot_insert_batch(conn, round_number, snapshots)
        storage.onchain_snapshots_purge_old(conn, keep_last_rounds=50)
        if cluster_records:
            storage.onchain_cluster_insert_batch(conn, round_number, cluster_records)
        keep = _load_setting("onchain_cluster_keep_rounds", 1500)
        storage._purge_by_round(conn, "onchain_clusters", keep)
        storage._purge_by_round(conn, "onchain_address_portfolio", keep)
        storage._purge_by_round(conn, "onchain_wallet_analysis", keep)
        storage._purge_by_round(conn, "onchain_cluster_trades", keep)
        storage._purge_by_round(conn, "onchain_cluster_tags", keep)

    # ---- Step C: 观察池更新 ----
    pool_updates = 0
    obs_count = act_count = 0
    with storage.get_conn(config.ONCHAIN_DB) as conn:
        existing_pool = {r["token"]: r for r in storage.onchain_pool_get_all(conn)}
        blacklist = {r["token"] for r in storage.onchain_blacklist_get_all(conn)}
        scan_interval = float(_load_setting("onchain_scan_interval_hours",
                              getattr(config, "ONCHAIN_SCAN_INTERVAL_HOURS", 4)))

        for s in divergence_tokens:
            token = s["token"]
            if token in blacklist:
                continue
            existing = existing_pool.get(token)
            rounds_in_pool = (existing.get("rounds_in_pool") or 0) + 1 if existing else 1
            days_in_pool = round(rounds_in_pool * scan_interval / 24, 1)
            # 保留已有的 stage（已晋级的不会被降级）
            existing_stage = (existing or {}).get("pool_stage") or "observation"

            entry = {
                "token": token,
                "token_address": s["token_address"],
                "chain": s.get("chain", "bsc"),
                "pool_stage": existing_stage,
                "surface_top10_pct": s["top10_hold_pct"],
                "effective_concentration": s["effective_concentration"],
                "fund_source_same_pct": s["holder_same_fund_pct"],
                "divergence_detected": 1,
                "days_in_pool": days_in_pool,
                "rounds_in_pool": rounds_in_pool,
                "first_seen_at": (existing or {}).get("first_seen_at") or _utcnow().isoformat(),
                "last_updated_at": _utcnow().isoformat(),
            }
            storage.onchain_pool_upsert(conn, entry)
            pool_updates += 1

        # ---- Step C2: 刷新已在池中但本轮未背离的 token 快照数据 ----
        div_set = {s["token"] for s in divergence_tokens}
        for s in snapshots:
            token = s["token"]
            if token not in existing_pool:
                continue
            if token in div_set:
                continue

            existing = existing_pool[token]
            rounds_in_pool = (existing.get("rounds_in_pool") or 0) + 1
            days_in_pool = round(rounds_in_pool * scan_interval / 24, 1)

            fresh_top10 = s["top10_hold_pct"] or existing.get("surface_top10_pct") or 0.0

            storage.onchain_pool_upsert(conn, {
                "token": token,
                "token_address": s["token_address"],
                "chain": s.get("chain", "bsc"),
                "pool_stage": existing.get("pool_stage") or "observation",
                "surface_top10_pct": fresh_top10,
                "effective_concentration": s["effective_concentration"],
                "fund_source_same_pct": s["holder_same_fund_pct"],
                "divergence_detected": existing.get("divergence_detected", 1),
                "days_in_pool": days_in_pool,
                "rounds_in_pool": rounds_in_pool,
                "first_seen_at": existing.get("first_seen_at"),
                "last_updated_at": _utcnow().isoformat(),
            })
            pool_updates += 1

        # ---- Step D: 超期清理（基于首次入池时间，而非轮次计数）----
        from datetime import timedelta
        cutoff = (_utcnow() - timedelta(days=max_days)).isoformat()
        for token, p in existing_pool.items():
            if p.get("pool_stage") == "observation":
                first_seen = p.get("first_seen_at") or ""
                if first_seen and first_seen < cutoff:
                    storage.onchain_pool_remove(conn, token)
                    print(f"  [超期清理] {token} 入池超过{max_days}天未晋级，移出")

        # ---- Step E: Stage 2 检查（每 N 轮一次）----
        stage2_interval = getattr(config, "ONCHAIN_STAGE2_CHECK_INTERVAL", 2)
        promoted = 0
        _promoted_names = []
        if round_number % stage2_interval == 0:
            print(f"[onchain] Stage 2 检查（第 {round_number} 轮）...", flush=True)
            obs_tokens = storage.onchain_pool_get_all(conn, "observation")
            snap_lookup = {s["token"]: s for s in snapshots}
            for pt in obs_tokens:
                token = pt["token"]
                addr = pt["token_address"]
                snap = snap_lookup.get(token, {})
                print(f"  Stage2: {token}...", flush=True)

                # 1. LP 储备检查（独有 API）
                token_chain = pt.get("chain", "bsc")
                lp_trend, lp_score = _check_lp_reserve(conn, token, addr, round_number, token_chain)
                time.sleep(0.3)

                # 2. Gas seeding 检查（复用本轮快照，overview 失败时回退 API）
                if snap and snap.get("overview_ok"):
                    gas_overview = {
                        "holder_new_addr_pct": snap.get("holder_new_addr_pct"),
                        "holder_same_fund_pct": snap.get("holder_same_fund_pct"),
                        "holder_same_creation_pct": snap.get("holder_same_creation_pct"),
                    }
                else:
                    gas_overview = None
                gas_signal, new_addr_pct = _check_gas_seeding(addr, pt, token_chain, overview=gas_overview)
                prev_new_saved = pt.get("prev_new_addr_pct") or 0

                # 3. CEX staging 检查（复用本轮快照，无数据时回退 API）
                cex_clusters = None
                if snap.get("cluster_list_json"):
                    try:
                        cex_clusters = json.loads(snap["cluster_list_json"])
                    except Exception:
                        pass
                cex_signal = _check_cex_staging(addr, pt, token_chain, clusters=cex_clusters)

                # 4. 综合置信度
                confidence = _compute_confidence(lp_score, gas_signal, cex_signal)
                print(f"    LP={lp_trend or '--'} ({lp_score}) gas={gas_signal} cex={cex_signal} conf={confidence}%", flush=True)

                if confidence >= 50:
                    storage.onchain_pool_upsert(conn, {
                        "token": token,
                        "token_address": addr,
                        "chain": pt.get("chain", "bsc"),
                        "pool_stage": "actionable",
                        "surface_top10_pct": pt.get("surface_top10_pct"),
                        "effective_concentration": pt.get("effective_concentration"),
                        "fund_source_same_pct": pt.get("fund_source_same_pct"),
                        "divergence_detected": pt.get("divergence_detected", 1),
                        "days_in_pool": pt.get("days_in_pool"),
                        "rounds_in_pool": pt.get("rounds_in_pool"),
                        "lp_reserve_trend": lp_trend,
                        "lp_trend_score": lp_score,
                        "gas_seeding_signal": gas_signal,
                        "cex_staging_signal": cex_signal,
                        "confidence_score": confidence,
                        "first_seen_at": pt.get("first_seen_at"),
                        "last_updated_at": _utcnow().isoformat(),
                    })
                    promoted += 1
                    _promoted_names.append(token)
                    print(f"    ⚡ {token} 晋级 actionable! (置信度 {confidence}%)", flush=True)

                    # 自动开仓（链上监控策略，从 DB 读开关允许动态调整）
                    if _load_setting("onchain_strategy_enabled",
                                    getattr(config, "ONCHAIN_STRATEGY_ENABLED", True)):
                        _open_onchain_position(conn, token, pt)
                else:
                    # 更新 Stage 2 信号但不晋级
                    storage.onchain_pool_upsert(conn, {
                        "token": token,
                        "token_address": addr,
                        "chain": pt.get("chain", "bsc"),
                        "pool_stage": "observation",
                        "lp_reserve_trend": lp_trend,
                        "lp_trend_score": lp_score,
                        "gas_seeding_signal": gas_signal,
                        "cex_staging_signal": cex_signal,
                        "confidence_score": confidence,
                        "prev_new_addr_pct": new_addr_pct if gas_signal >= 1 else prev_new_saved,
                        "last_updated_at": _utcnow().isoformat(),
                    })

        if promoted:
            print(f"[onchain] Stage 2: {promoted} 个晋级 actionable", flush=True)

        # 更新状态
        obs_count = len(storage.onchain_pool_get_all(conn, "observation"))
        act_count = len(storage.onchain_pool_get_all(conn, "actionable"))
        storage.onchain_status_update(conn, key="scanner", stage="idle",
            detail=f"轮 #{round_number} 完成: {len(snapshots)} 快照, {pool_updates} 入池, {len(divergence_tokens)} 背离",
            round_number=round_number, tokens_scanned=len(snapshots),
            candidates_found=len(divergence_tokens), pool_size=obs_count, actionable_size=act_count,
            last_scan_end=_utcnow(), last_heartbeat=_utcnow())

    print(f"[onchain] 轮 #{round_number} 完成: {len(snapshots)} 快照, "
          f"{len(divergence_tokens)} 背离, 观察池 {obs_count}, 可建仓 {act_count}")

    # TG 通知（静默失败，不影响主流程）
    if getattr(config, "TG_ONCHAIN_ENABLED", False):
        try:
            from tg_notify import send_onchain_round
            # 刚晋级的 token 名（在 Stage 2 循环里收集）
            div_tokens = [{"token": s["token"],
                           "chain": s.get("chain", ""),
                           "market_cap_usd": s.get("market_cap_usd"),
                           "holder_same_fund_pct": s.get("holder_same_fund_pct"),
                           "top10_hold_pct": s.get("top10_hold_pct"),
                           "effective_concentration": s.get("effective_concentration")}
                          for s in divergence_tokens]
            send_onchain_round(round_number, len(snapshots), div_tokens,
                              obs_count, act_count, _promoted_names)
        except Exception:
            pass

    # ---- Step F: Stage 3 集群标签分析（每 N 轮一次）----
    if (_load_setting("onchain_cluster_enabled", True) and
            _load_setting("onchain_cluster_analysis_enabled", True)):
        interval = int(_load_setting("onchain_cluster_analysis_interval", 1))
        if round_number % interval == 0:
            print(f"[onchain] Stage 3: 集群标签分析（第 {round_number} 轮）...", flush=True)
            _analyze_clusters(round_number)


# ============================================================
# Stage 2 检测函数
# ============================================================

def _check_lp_reserve(conn, token: str, addr: str, round_number: int, chain: str = "bsc") -> tuple[str | None, float]:
    """检测 LP 储备加速减少。返回 (trend_label, score_0_to_100)"""
    liq = fetch_liquidity(addr, chain, token_symbol=token)
    if not liq:
        return None, 0

    token_amount = liq.get("total_token_amount", 0)
    if token_amount <= 0:
        return None, 0

    # 写入 LP 快照
    storage.onchain_lp_snapshot_insert_batch(conn, [{
        "token": token,
        "token_address": addr,
        "chain": chain,
        "round_number": round_number,
        "token_amount": token_amount,
    }])

    # 读取最近 3 轮 LP 快照
    lp_history = storage.onchain_lp_snapshots_for_token(conn, token, limit=4)
    if len(lp_history) < 3:
        return "数据积累中", 10

    amounts = [r["token_amount"] for r in lp_history[:3]]
    if not all(a and a > 0 for a in amounts):
        return "数据不完整", 5

    # 计算变化率（取负号：减少为正，增加为负）
    changes = []
    for i in range(len(amounts) - 1):
        if amounts[i + 1] > 0:
            # (older - newer) / older * 100 → LP减少时为正
            changes.append((amounts[i + 1] - amounts[i]) / amounts[i + 1] * 100)

    if len(changes) < 2:
        return "数据积累中", 10

    latest_change = changes[0]   # 最新一轮的变化
    prev_change = changes[1]     # 前一轮的变化

    # 加速减少检测：两轮都在减少，且最新减少速度 > 前一轮的 1.5x
    if latest_change > 0 and prev_change > 0:
        if latest_change > prev_change * 1.5 and latest_change > 3:
            return "↓↓ 加速减少", 80
        elif latest_change > prev_change * 1.2 and latest_change > 2:
            return "↓ 加速减少(弱)", 50
        elif latest_change > 0:
            return "↓ 轻微减少", 25
    elif latest_change > 0 and prev_change <= 0:
        return "↓ 开始减少", 20
    elif latest_change <= 0 and prev_change > 0:
        return "增加（反向）", 0
    else:
        return "稳定", 10


def _check_gas_seeding(addr: str, pool_entry: dict, chain: str = "bsc", overview: dict = None) -> tuple[int, float]:
    """检测新鲜钱包批量 seed gas。返回 (signal_0_1_2, new_addr_pct)"""
    if overview is None:
        overview = fetch_cluster_overview(addr, chain)
    if not overview:
        return 0, 0

    new_addr_pct = overview.get("holder_new_addr_pct") or 0
    fund_same_pct = overview.get("holder_same_fund_pct") or 0
    same_creation_pct = overview.get("holder_same_creation_pct") or 0

    # 与入池时的基准对比（pool 里存了 prev_new_addr_pct）
    prev_new = pool_entry.get("prev_new_addr_pct") or 0

    score = 0
    if new_addr_pct and new_addr_pct > 2:              # >2% 为新地址
        score += 1
    if fund_same_pct and fund_same_pct > 35:           # >35% 同源
        score += 1
    if same_creation_pct and same_creation_pct > 5:    # >5% 同时创建
        score += 1
    if new_addr_pct > prev_new + 1:                    # 新地址占比在上升
        score += 1

    if score >= 3:
        return 2, new_addr_pct  # 强
    elif score >= 2:
        return 1, new_addr_pct  # 弱
    return 0, new_addr_pct


def _check_cex_staging(addr: str, pool_entry: dict, chain: str = "bsc", clusters: list = None) -> int:
    """检测 CEX 充值地址 staging。返回 0=无, 1=弱, 2=强（权重低）"""
    if clusters is None:
        clusters, _ = fetch_cluster_list(addr, chain)
    if not clusters:
        return 0

    exchange_clusters = []
    for cl in clusters:
        has_ex = any(a["is_exchange"] for a in cl["addresses"])
        if has_ex:
            exchange_clusters.append(cl)

    if not exchange_clusters:
        return 0

    total_ex_pct = sum(c["holding_percent"] or 0 for c in exchange_clusters) * 100
    ex_count = len(exchange_clusters)

    if ex_count >= 3 and total_ex_pct > 5:
        return 2
    elif ex_count >= 1 and total_ex_pct > 2:
        return 1
    return 0


def _compute_confidence(lp_score: float, gas_signal: int, cex_signal: int) -> float:
    """综合置信度评分（0-100）"""
    # 权重：LP 50%, Gas 35%, CEX 15%（CEX 方向不可判，降权）
    lp_weighted = lp_score * 0.50
    gas_weighted = gas_signal * 50 * 0.35     # gas 信号 0→0, 1→17.5, 2→35
    cex_weighted = cex_signal * 50 * 0.15     # cex 信号 0→0, 1→7.5, 2→15
    return round(lp_weighted + gas_weighted + cex_weighted, 1)


def _open_onchain_position(conn, token: str, pool_entry: dict):
    """链上监控策略自动市价开仓"""
    try:
        import trade_logic
        from market import get_market_snapshot
        snap = get_market_snapshot(token, heavy=False)
        if not snap or not snap.get("mark_price"):
            print(f"    [onchain trade] {token} 快照无价格，跳过开仓")
            return
        candidate = {
            "token": token, "passed": True,
            "tier": "half", "has_active_position": False,
            "price": snap["mark_price"],
            "market": {"snapshot": snap, "analysis": {}},
            "realtime": {},
            "direction": "short",
        }
        with storage.get_conn() as main_conn:
            settings = storage.trading_settings_get(main_conn)
            result = trade_logic.open_paper_position(
                main_conn, candidate, settings, strategy="onchain", side="SHORT")
            if result:
                print(f"    [onchain trade] {token} 开仓成功 @ ${snap['mark_price']}")
                # 实盘镜像
                if settings.get("onchain_live_mode") == "live":
                    try:
                        pos = main_conn.execute(
                            "SELECT * FROM trade_positions WHERE token=? AND strategy='onchain' ORDER BY id DESC LIMIT 1",
                            (token,),
                        ).fetchone()
                        if pos:
                            import exchange_bridge
                            exchange_bridge.mirror_open(dict(pos))
                    except Exception as _e:
                        print(f"    [onchain trade] {token} 实盘镜像失败: {_e}")
            else:
                print(f"    [onchain trade] {token} 开仓被拒（去重/风控）")
    except Exception as e:
        print(f"    [onchain trade] {token} 开仓异常: {e}")


def _write_status(**fields):
    try:
        with storage.get_conn(config.ONCHAIN_DB) as conn:
            fields.setdefault("key", "scanner")
            storage.onchain_status_update(conn, **fields)
    except Exception:
        pass


def scanner_loop():
    """后台扫描主循环"""
    global _running, _scanner_started
    _running = True

    interval_hours = float(_load_setting("onchain_scan_interval_hours",
                          getattr(config, "ONCHAIN_SCAN_INTERVAL_HOURS", 4)))
    print(f"[onchain scanner] 已启动，扫描间隔 {interval_hours}h", flush=True)

    # 恢复轮次
    try:
        with storage.get_conn(config.ONCHAIN_DB) as conn:
            status = storage.onchain_status_get(conn)
            if status and status.get("round_number"):
                global _round
                _round = status["round_number"]
                print(f"[onchain scanner] 续上次: round #{_round}", flush=True)
    except Exception:
        pass

    while True:
        try:
            if not _load_setting("onchain_scan_enabled", True):
                time.sleep(60)
                continue

            # 首次启动延迟 2 分钟，错开 worker 首轮 API 调用高峰
            if not _scanner_started:
                print("[onchain scanner] 首次启动，等待 120s 错开 worker 首轮...", flush=True)
                time.sleep(120)
                _scanner_started = True

            run_round()

            # 每轮从 DB 重读扫描间隔（允许设置页动态调整）
            interval_hours = float(_load_setting("onchain_scan_interval_hours",
                                   getattr(config, "ONCHAIN_SCAN_INTERVAL_HOURS", 4)))
            sleep_sec = interval_hours * 3600
            print(f"[onchain scanner] 下次扫描: {sleep_sec/3600:.1f}h 后", flush=True)
            time.sleep(sleep_sec)

        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[onchain scanner] 错误: {e}，5 分钟后重试", flush=True)
            _write_status(stage="error", detail=str(e)[:200])
            time.sleep(300)


# ============================================================
# Stage 3 集群标签分析
# ============================================================

def _insert_tag(round_number, token, cluster_index, tag_type, tag_label, tag_detail=None):
    try:
        with storage.get_conn(config.ONCHAIN_DB) as conn:
            storage.onchain_cluster_tag_insert(conn, {
                "round_number": round_number, "token": token,
                "cluster_index": cluster_index, "tag_type": tag_type,
                "tag_label": tag_label, "tag_detail": tag_detail,
            })
    except Exception:
        pass


def _analyze_clusters(round_number):
    """Stage 3：对观察池/建仓池的每个 token，加载最新 cluster 数据，调 wallet-analysis+trades，计算标签"""
    pool = []
    try:
        with storage.get_conn(config.ONCHAIN_DB) as conn:
            pool = storage.onchain_pool_get_all(conn, "observation")
    except Exception:
        return
    if not pool:
        return

    top_n = int(_load_setting("onchain_cluster_top_addresses", 5))

    for pt in pool:
        token = pt["token"]
        addr = pt["token_address"]
        chain = pt.get("chain", "bsc")
        print(f"  [Stage3] {token}...", flush=True)

        # 1. 读取最新 + 上一轮 cluster 数据
        clusters_now = []
        try:
            with storage.get_conn(config.ONCHAIN_DB) as conn:
                clusters_now = storage.onchain_clusters_for_token(conn, token, limit_rounds=1)
                clusters_prev = storage.onchain_clusters_for_token(conn, token, limit_rounds=2)
        except Exception:
            continue
        if not clusters_now:
            continue

        prev_map = {}
        if len(clusters_prev) > len(clusters_now):
            for c in clusters_prev:
                if c["round_number"] != clusters_now[0]["round_number"]:
                    prev_map[c["cluster_index"]] = c

        for cl in clusters_now:
            ci = cl["cluster_index"]
            if not cl["is_real_cluster"]:
                continue
            if (cl["holding_percent"] or 0) < _load_setting("onchain_cluster_min_holding_pct", 1.0):
                continue

            # ---- 对比上轮变化 ----
            prev = prev_map.get(ci, {})
            if prev:
                hp_now = cl["holding_percent"] or 0
                hp_prev = prev.get("holding_percent") or 0
                hp_change = round(hp_now - hp_prev, 2)
                if hp_change > 0.5:
                    _insert_tag(round_number, token, ci, "holding_change",
                               f"增持 +{hp_change}%")
                elif hp_change < -0.5:
                    _insert_tag(round_number, token, ci, "holding_change",
                               f"减持 {hp_change}%")
                elif abs(hp_change) <= 0.5:
                    _insert_tag(round_number, token, ci, "holding_change",
                               "持仓稳定")

            # ---- API 标签（trend_type / pnl）----
            trend_raw = cl.get("trend_type") or ""
            if "accumulating" in trend_raw:
                _insert_tag(round_number, token, ci, "api", "吸筹中")
            elif "distributing" in trend_raw:
                _insert_tag(round_number, token, ci, "api", "派发中")
            if cl.get("pnl_percent") is not None:
                pnl = cl["pnl_percent"]
                label = f"集群盈利 +{pnl:.1f}%" if pnl >= 0 else f"集群亏损 {abs(pnl):.1f}%"
                _insert_tag(round_number, token, ci, "api", label)

            # ---- 2. wallet-analysis（每个地址：存储 + 交叉持仓提取）----
            addresses_json = cl.get("addresses_json") or "[]"
            try:
                addresses = json.loads(addresses_json)
            except Exception:
                continue
            real_addrs = [a for a in addresses
                         if not a.get("is_exchange") and not a.get("is_contract")]
            real_addrs = real_addrs[:top_n]

            # ---- 2. wallet-analysis（每个地址：存储 + 交叉持仓提取）----
            wa_records = []
            addr_holdings = {}
            for a in real_addrs:
                wa = fetch_wallet_analysis(a["address"], chain)
                time.sleep(0.3)
                if wa:
                    trades = wa.get("activities", {}).get("trades", [])
                    perf_30d = wa.get("performance", {}).get("30d", {})
                    last_trade = trades[0].get("tradeTime") if trades else None
                    if last_trade:
                        try:
                            last_trade = datetime.fromtimestamp(int(last_trade) / 1000, timezone.utc).isoformat()
                        except Exception:
                            last_trade = None
                    buy_count = int(perf_30d.get("buyTxCount", 0))
                    sell_count = int(perf_30d.get("sellTxCount", 0))
                    tv_list = wa.get("totalValue") or []
                    total_val = float(tv_list[0].get("totalValue", 0)) if tv_list else None
                    wa_records.append({
                        "round_number": round_number, "token": token,
                        "cluster_index": ci, "address": a["address"],
                        "total_value_usd": total_val,
                        "buy_count_30d": buy_count,
                        "sell_count_30d": sell_count,
                        "buy_volume_30d": float(perf_30d.get("buyTxVolume", 0)),
                        "sell_volume_30d": float(perf_30d.get("sellTxVolume", 0)),
                        "avg_buy_value": float(perf_30d.get("avgBuyValueUsd", 0)),
                        "avg_sell_value": round(float(perf_30d.get("sellTxVolume", 0)) / sell_count, 2) if sell_count > 0 else None,
                        "realized_pnl": float(perf_30d.get("realizedPnlUsd", 0)),
                        "win_rate": float(perf_30d.get("winRate", 0)),
                        "last_trade_time": last_trade,
                        "analysis_json": json.dumps(wa, ensure_ascii=False),
                    })
                    # 提取持仓列表（用于交叉对比）
                    balances = wa.get("balances") or []
                    if balances:
                        tokens_held = set()
                        for ta in balances[0].get("tokenAssets") or []:
                            sym = (ta.get("symbol") or "").upper()
                            if sym:
                                tokens_held.add(sym)
                        if tokens_held:
                            addr_holdings[a["address"]] = tokens_held
            if wa_records:
                try:
                    with storage.get_conn(config.ONCHAIN_DB) as conn:
                        storage.onchain_wallet_analysis_insert_batch(conn, wa_records)
                except Exception:
                    pass

            # ---- 3. 交叉持仓计算 ----
            if len(addr_holdings) >= 2:
                common = set.intersection(*addr_holdings.values())
                common.discard(token.upper())
                if len(common) >= 3:
                    _insert_tag(round_number, token, ci, "common_holdings",
                               f"共持 {len(common)} 个币",
                               ", ".join(sorted(common)[:8]))

            # ---- 4. trades（批量查集群地址）----
            addr_list = [a["address"] for a in real_addrs]
            if addr_list and addr:
                trades_data = fetch_token_trades(addr, chain, wallet_filter=addr_list)
                time.sleep(0.3)
                if trades_data:
                    try:
                        with storage.get_conn(config.ONCHAIN_DB) as conn:
                            storage.onchain_cluster_trades_insert(conn, {
                                "round_number": round_number, "token": token,
                                "cluster_index": ci,
                                "addresses_batch": ",".join(addr_list),
                                "trades_json": json.dumps(trades_data, ensure_ascii=False),
                            })
                    except Exception:
                        pass

                    sells = [t for t in trades_data
                            if str(t.get("tradeType", "")) == "2"]
                    buys = [t for t in trades_data
                           if str(t.get("tradeType", "")) == "1"]
                    if not sells:
                        _insert_tag(round_number, token, ci, "trade_behavior",
                                   "近期无卖出")
                    elif len(sells) <= 2:
                        _insert_tag(round_number, token, ci, "trade_behavior",
                                   f"少量卖出 ({len(sells)}笔)")
                    else:
                        _insert_tag(round_number, token, ci, "trade_behavior",
                                   f"活跃卖出 ({len(sells)}笔)")
                    if buys:
                        _insert_tag(round_number, token, ci, "trade_behavior",
                                   f"近期买入 ({len(buys)}笔)")

            time.sleep(0.2)
