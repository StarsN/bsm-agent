"""按改动范围挑测试套件跑——避免每次全量（全量 ~53s，改一个文件往往只需几秒）。

用法：
    python extra/run_tests.py --changed          # 按 git 改动自动挑（推荐）
    python extra/run_tests.py alphafox_live      # 按组名
    python extra/run_tests.py alphafox_live tg   # 多组
    python extra/run_tests.py all                # 全量
    python extra/run_tests.py --list             # 看分组

改动到共享底层（storage/exchange 等）会自动升级为全量。
新增修复后**必须**再单独跑对应变异：
    python extra/_mutation_check.py M55          # 只跑名字含 M55 的变异
"""
import io
import os
import re
import subprocess
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- 分组：一个组 = 一组要一起跑的套件 ----
GROUPS = {
    "alphafox_live": ["extra/test_alphafox_live.py",
                      "extra/_scenario_alphafox_live.py",
                      "extra/_scenario_alphafox_cycle.py",
                      "extra/_scenario_alphafox_quickfollow.py"],
    "alphafox_trade": ["extra/test_alphafox_trade.py"],
    "e2e": ["extra/_e2e_alphafox_live.py"],
    "supertrend": ["extra/test_supertrend_live.py",
                   "extra/test_supertrend_logic.py",
                   "extra/_check_supertrend_add_sql.py",
                   "extra/_check_flip_wiring.py",
                   "extra/test_coexist_sync.py",
                   "extra/test_double_sl.py",
                   "extra/_scenario_supertrend_cycle.py"],
    "web": ["extra/_check_exempt_recorder.py", "extra/_check_af_payload.py",
            "extra/_check_exchange_orders.py", "extra/_check_af_board.py",
            "extra/_check_live_close.py", "extra/_check_st_board.py",
            "extra/_check_pages_js.py", "extra/_check_nav.py",
            # 该套件第 5/8 节大量断言 web.py 源码接线（翻转相关），改 web.py 必须带上
            "extra/_check_flip_wiring.py"],
    "tg": ["extra/_check_tg_signals.py"],
    "macro": ["extra/_check_macro_cn.py"],
    # 币安符号表：TRADIFI 永续（CL/NVDA/XAU）跟单可见、但不混进纯加密符号集
    "market": ["extra/test_market_symbols.py"],
    # 加密事件：币种识别（中英文）/ 金十预览页解析（真样本夹具）/ 事件独立 DB
    # + 美股/商品事件（独立库、同一抓取、另一条管线）
    # + 科技股动态（拆条+三层识别 / 三元组管线，真样本夹具）
    "event": ["extra/test_crypto_event.py", "extra/test_stock_event.py",
              "extra/test_tech_stocks.py", "extra/test_tech_news.py"],
    # 全新库/换库/恢复备份的前置检查（init_db 不炸 + managed 列自动创建 + 幂等）
    "storage": ["extra/_check_storage_init.py"],
}

# ---- 模块 → 组。None = 动到它就全量（共享底层，改一处影响所有策略）----
MODULE_GROUPS = {
    # 两层由托管状态串起来（process 追平会调 alphafox_live.on_reattach）
    "alphafox_live.py": ["alphafox_live", "alphafox_trade", "e2e"],
    "alphafox_trade.py": ["alphafox_trade", "alphafox_live", "e2e"],
    "supertrend_live.py": ["supertrend"],
    "supertrend_strategy.py": ["supertrend"],
    "trade_logic.py": ["alphafox_trade"],
    "tg_notify.py": ["tg"],
    "dashboard.py": ["macro"],
    "web.py": ["web"],
    # market 符号表：动它要连带跑"跟单认不认得出这个合约"的套件
    "market.py": ["market", "alphafox_trade", "alphafox_live", "e2e"],
    # 事件两条管线（加密 + 美股/商品）改动跑 event 组
    "event_agent.py": ["event"],
    "event_coins.py": ["event"],
    "event_scrape.py": ["event"],
    "stock_coins.py": ["event"],
    # 科技股动态识别（三条管线共用抓取，改它跑 event 组）
    "tech_stocks.py": ["event"],
    # storage 改动跑"全新库 init"检查 + 跟单相关组（用户要求：不要自动升级为全量；
    # 真要大范围回归，部署前单独跑 all）
    "storage.py": ["storage", "alphafox_live", "alphafox_trade", "e2e"],
    "exchange.py": None,
    "config.py": None,
}

ALL_SUITES = [s for g in GROUPS.values() for s in g]


def _git_changed():
    """git 看到的改动 + 未跟踪（只看仓库根目录的 .py，extra/ 里的测试自己管自己）"""
    try:
        out = subprocess.run(["git", "status", "--porcelain"],
                             cwd=ROOT, capture_output=True, text=True,
                             timeout=60).stdout
    except Exception as e:
        print(f"[warn] git status 失败: {e}")
        return []
    files = []
    for line in out.splitlines():
        p = line[3:].strip().strip('"')
        files.append(p.replace("/", os.sep))
    return files


def pick_suites(files):
    """按改动文件挑套件；返回 (suites, why)"""
    picked, why, full = set(), [], False
    for f in files:
        base = os.path.basename(f)
        norm = f.replace(os.sep, "/")
        if norm.startswith("extra/") and norm in ALL_SUITES:
            picked.add(norm)
            why.append(f"{norm}（测试自身）")
            continue
        if os.sep in f.strip(os.sep):     # 子目录里的非测试文件 → 忽略
            continue
        if base not in MODULE_GROUPS:
            continue
        gs = MODULE_GROUPS[base]
        if gs is None:
            full = True
            why.append(f"{base}（共享底层 → 全量）")
            continue
        for g in gs:
            picked.update(GROUPS[g])
        why.append(f"{base} → {'/'.join(gs)}")
    if full:
        return ALL_SUITES, why
    return sorted(picked), why


def run(suites):
    fails = 0
    for s in suites:
        t0 = time.time()
        r = subprocess.run([sys.executable, s], cwd=ROOT, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=1800)
        out = (r.stdout or "") + (r.stderr or "")
        m = re.findall(r"(\d+)\s*(?:passed|通过)", out)
        if m:
            p = int(m[-1])
            mf = re.findall(r"(\d+)\s*(?:failed|失败)", out)
            f = int(mf[-1]) if mf else 0
        else:
            p, f = out.count("[PASS]"), out.count("[FAIL]")
        dt = time.time() - t0
        flag = "OK " if (r.returncode == 0 and f == 0) else "FAIL"
        print(f"[{flag}] {s}  P={p} F={f}  {dt:.1f}s")
        if f or r.returncode:
            fails += 1
            for line in out.splitlines():
                if "[FAIL]" in line:
                    print("      " + line.strip())
    return fails


def main():
    args = [a for a in sys.argv[1:]]
    if not args or args == ["--changed"]:
        files = _git_changed()
        suites, why = pick_suites(files)
        print("改动识别:")
        for w in why:
            print("   " + w)
        print(f"\n选中 {len(suites)}/{len(ALL_SUITES)} 个套件（全量需 {len(ALL_SUITES)}）\n")
    elif args[0] == "--list":
        for g, ss in GROUPS.items():
            print(f"{g}: {' '.join(ss)}")
        return 0
    elif args[0] == "all":
        suites = ALL_SUITES
    else:
        suites = []
        for a in args:
            if a not in GROUPS:
                print(f"未知分组: {a}（可用: {', '.join(GROUPS)} / all）")
                return 2
            suites.extend(GROUPS[a])
        suites = sorted(set(suites))
    if not suites:
        print("没有需要跑的套件（改动未映射到任何测试）")
        return 0
    t0 = time.time()
    fails = run(suites)
    print(f"\n{'全部通过' if not fails else str(fails) + ' 个套件失败'}  "
          f"耗时 {time.time() - t0:.1f}s")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
