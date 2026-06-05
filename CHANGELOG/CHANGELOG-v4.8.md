# v4.8 CHANGELOG — DB分库 + db/目录迁移 + 多重Bug修复

## 一、DB分库：1 个 system.db → 4 个 agent DB

system.db 写锁竞争严重（7 个进程并发），将 5 类高频读写表拆分到独立 DB：

| 数据库 | 表 | 策略 |
|--------|-----|------|
| `agent_main.db` | `agent_candidates` | agent |
| `kol.db` | `kol_candidates`、`kol_analyses`、`kol_llm_logs` | kol_agent |
| `snapshot.db` | `kol_candidates`、`kol_analyses`、`kol_llm_logs` | kol_snapshot |
| `nl.db` | `nl_candidates` | agent_no_lessons |

`lessons` 保留在 system.db（最初拆分到 heat.db/heat_lessons.db 后因跨库 journal UPDATE bug 回退）。

**受影响的文件：**
- `config.py` — 新增 `AGENT_MAIN_DB`、`KOL_DB`、`SNAPSHOT_DB`、`NL_DB`
- `storage.py` — `get_conn(db_path)` 路由、`init_agent_dbs()` 4 个 agent DDL、`kol_analysis_insert`/`kol_llm_log_insert` 新增 `agent_db` 参数、`trade_positions_with_kol_enrichment` 跨库查询、`trade_reset_strategy`/`trade_reset_all` agent DB DELETE
- `web.py` — collector 写入 + API 端点路由到正确的 agent DB、`api_agent_lessons` 统一用 system.db
- `kol_agent.py` — `get_kol_candidates()` 按策略选择 KOL_DB/SNAPSHOT_DB、`analyze_candidates` 传 `agent_db`
- `migrate_agent_dbs.py` — 纯 sqlite3 迁移脚本，从 system.db 复制到 4 个 agent DB
- 12 个 agent 脚本 — `extract_market_data.py`/`extract_review_data.py` 用正确 agent DB 连接

## 二、db/ 目录迁移

所有 5 个数据库文件从项目根目录移入 `db/` 子目录：

- `config.py` — 7 个路径常量统一加 `db/` 前缀
- `storage.py` — `get_conn()` 启动时自动 `os.makedirs("db", exist_ok=True)`
- 14 个 agent 脚本 — `DB_NAME` 从硬编码改为 `getattr(config, "DB_PATH", "db/binance_square.db")`
- `CLAUDE.md` — 示例命令更新为 `db/binance_square.db`

## 三、Bug 修复（14 项）

### auto_trader.py
- **C1**：`open_short` 无 `entry_price` 时明确拒绝（不再掉到"未知 action"）
- **D**：异常时 `pending_decisions` 状态必定更新为 `rejected`（try/except 保护）
- **E**：`execute_close` 做空 PnL 公式修正：`(entry - price)` 替代 `(price - entry)`
- **G**：`subprocess.run(timeout=10)` → `timeout=30`

### risk.py
- **硬否决 #8 缩进错误**：`SHORT_SELLING_ENABLED` 检查从 `vol_oi > 20` 缩进下独立出来
- **vol_oi 阈值硬编码**：`20` → `getattr(config, "KOL_MAX_VOL_OI", 20)`

### kol_agent.py
- **`kol_is_interesting` 返回值**：`bool` → `(bool, reasons_list)`，前端可展示触发原因
- **getattr 默认值对齐 config**：3/8/5 → 4/10/2.5

### web.py
- **`loadTradingPanel` 错误处理**：`getElementById('trade-summary')`（不存在）→ `'trade-candidates'`
- **KOL 收集器内存泄漏**：`_kol_collected` 只在 `kol_agent_enabled` 时累积
- **前端文字**："合约扫描与操作建议"→"做多建议"、"做空候选"→"做空建议"、"KOL 关注"→"综合建议"
- **`renderKolCandidates`**：JS 渲染 `reasons`（OI异动/费率异常/MA20乖离）

### storage.py
- **`trade_reset_all`**：候选池删除从 system.db → 正确的 agent DB
- **reset 不再处理 lessons**：`UPDATE lessons SET learned=1` 已移除

### worker.py
- **每轮后 `pkill -f chromium`**：防止 Playwright 僵尸进程内存累积导致轮次卡死

## 四、测试
- `test_db_split.py` — 36 项，验证 agent DB 路由 + DDL + INSERT/SELECT/JOIN
- `test_agent_dbs_full.py` — 99 项，基于迁移产物验证全部读/写 SQL
- `verify_migration.py` — 迁移后结构一致性 + 行数 + 抽样验证
