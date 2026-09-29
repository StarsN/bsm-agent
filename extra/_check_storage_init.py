"""全新库上 storage.init_db() 必须可用（部署/换库/恢复备份的前置检查）。

覆盖：全新库首次 init_db 不炸（遗留表 round_candidates 可能不存在）、重复调用幂等、
跟单托管依赖的 trade_positions.managed 列由 _migrate 自动创建、live 库表齐全。
"""
import io
import os
import sys
import tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)

import config

_TMP = tempfile.mkdtemp(prefix="stinit_")
# 所有 .db 路径都指到临时目录（init_db 会初始化多个独立库）
_remapped = []
for _k in dir(config):
    if _k.endswith("_DB") or _k == "DB_PATH":
        _v = getattr(config, _k)
        if isinstance(_v, str) and _v.endswith(".db"):
            setattr(config, _k, os.path.join(_TMP, os.path.basename(_v)))
            _remapped.append(_k)

import storage  # noqa: E402

PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


err = ""
try:
    storage.init_db()
    ok1 = True
except Exception as e:  # noqa: BLE001
    ok1 = False
    err = f"{type(e).__name__}: {e}"
check(f"全新库首次 init_db 不报错（重定向 {len(_remapped)} 个 DB 路径）", ok1, err)

with storage.get_conn() as conn:
    cols = [r[1] for r in conn.execute(
        "PRAGMA table_info(trade_positions)").fetchall()]
check("trade_positions.managed 由 _migrate 自动创建（部署不需要手动 SQL）",
      "managed" in cols, str(cols))

with storage.get_conn(config.LIVE_DB_PATH) as lc:
    live_tables = [r[0] for r in lc.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
check("live 库表齐全（live_positions/live_trade_log）",
      "live_positions" in live_tables and "live_trade_log" in live_tables,
      str(live_tables))

try:
    storage.init_db()          # 再跑一次：迁移必须幂等
    ok2 = True
except Exception as e:  # noqa: BLE001
    ok2 = False
    err = f"{type(e).__name__}: {e}"
check("重复 init_db 幂等", ok2, err)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
