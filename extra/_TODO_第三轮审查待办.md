# 主流趋势（supertrend）审查与修复记录（第 3~4 轮，2026-09-14）

> 状态：`supertrend` + `web` 两组 **553 断言全绿**；变异 **155/155 锚点有效**。
> 默认只跑这两组；**全量回归 + 全量变异等部署时再跑**（用户要求，跑前先问）。

---

## 0. 背景与不变式（接手必读）

- **纸盘层** `supertrend_strategy.py`：60 秒一轮拉 OKX 15m Supertrend，写 `trade_positions`（strategy='supertrend'）
- **实盘镜像层** `supertrend_live.py`：对账式镜像到币安 `live_mode=1` 的币
- **1 秒循环** `trade_logic.update_paper_positions`：挂单成交、扫损
- **币种级互斥**：`supertrend_live._alphafox_occupied` ↔ `alphafox_live._supertrend_occupied`
- **反转并存**：`flip_market_open=1` 的币反转时，纸盘同时有「市价腿 OPEN」+「挂单腿 PENDING」，实盘两条记录两张 SL；1s 循环转 OPEN 后由 `_merge_positions` 并成一笔
- **三块一致性的铁律**：SL 总覆盖 == 交易所持仓(+预挂的挂单腿量)；live 记录数 == 纸盘活跃行数；记录 quantity == 纸盘数量
- **SL 覆盖量存在 `orders["sl_qty"]`**（第 4 轮加）：对账据此发现"覆盖不足"，别只靠价格变化

## 1. 第三轮修的 6 条

| # | 问题 | 修法 | 测试段 | 变异 |
|---|---|---|---|---|
| 1 | 「补建记录」路径不可达（`_mirror_pending(reuse_only=True)` 后无条件 return） | 返回 bool（True=真转 OPEN）；没成交就继续往下补建 | 50 | M148 |
| 2 | `live_position_insert` 返值被忽略 → 撞 CLOSED 残留行时记录静默丢失 → 补建每轮重复挂 SL | `_write_live_record()`：冲突复用旧行，返回 id；三处调用点接返值；补建改"先落记录再挂 SL" | 49 | M149 |
| 3 | `_handover_sl` 无返回值 → 交接失败照样撤 dup 的单，止损覆盖归零 | 返回 bool；失败则本轮不收敛 dups | 51 | M150 |
| 4 | 平仓量 `max(result, minQty)` 把残仓放大成超量单 | `_round_qty_down`（向下、不兜 minQty、0→None）；残仓不足最小下单量 → 告警 + 记录照常收敛 | 54 / `_check_exchange_orders` | M151/M152 |
| 5 | `_align_open` 无跟单闸门 | 开头加 `_alphafox_occupied`（**仅超势侧**） | 52 | M153 |
| 6 | `close_live`/`switch_live_mode` 传 `paper_close_row=None` → realized 少记 | 抽 `_latest_closed_paper_row(token)` | 53 | M154 |

## 2. 第四轮：流程走查抓到的 1 条（已修）

**新增走查脚本 `extra/_scenario_supertrend_cycle.py`**（跑真 `_run_round` + 真 1s 循环 +
真镜像，每步验四项一致性）。前几轮套件都是"手工摆 DB 状态"，走查补上了
**"成交与合并在同一轮"**这个真实时序。

### 🔴 孤儿挂单记录的预挂 SL 变无主单（M155）

- **触发**：共存窗口里交易所限价单真成交 → 1s 循环把纸盘挂单行转 OPEN → **同轮**
  `_run_round` 开头的合并把两条纸盘行并成一笔 → 轮末对账时那条 live 挂单记录
  按纸盘 id 成了"孤儿"
- **根因**：孤儿收敛分支只在"限价单没成交"时才撤单（`if not _limit_fill_state(...)`），
  成交时直接把记录标 CLOSED → 它**预挂的 STOP_MARKET 再没人认识**
- **后果**：与代表作记录的 SL 叠加成**超覆盖**（1.5 倍）→ 两张同时触发 = 超量平仓/
  反向开仓；即使当下金额凑巧对得上，下一根 K 线 SL 跟随后就变成超覆盖
- **修法**：① 孤儿分支**无条件撤单**（撤已成交的单是幂等的）；② `_align_open` 在
  "SL 覆盖量 ≠ 持仓量"时重挂（`sl_qty` 校验，旧记录无该字段 → 当不符处理，重挂一次自愈）；
  ③ `_align_open` 读的记录要**从 DB 取最新 orders**（本轮前面的 SL 交接可能已改过它，
  用旧快照会撤作废单号再挂一张 → 两张 SL 并存）

### 附带发现的坑（同轮修掉）

- **局部变量遮蔽**：在 `_align_open` 里用 `_fresh` 当变量名会把模块级 `_fresh()` 函数
  遮蔽掉（函数作用域）→ 后半段 `TypeError`，被上层 `except` 吞成一句"同步异常"，
  **SL 更新/数量回写全停**。教训：动这类函数时别用与模块级函数同名的局部变量。

### 🟡 已定性为设计（用户确认）

**BOTH 币的陈旧挂单在反转时必成交**：挂单价（下轨×1.01 / 上轨×0.99）就贴在
"反转触发线"（轨道）内侧，而触发反转必须先穿过这条轨道 → **价格必然先穿过挂单价**，
限价单（maker）必成交。所以：

- 不存在"整个反向趋势段不持仓"（我最初报错了）——反转那一下会先成交一个反向仓，
  随即被 SL 扫掉（≈ 1R = 2×容忍），然后才挂新方向的单
- 这是"买在轨道上、止损在轨道外"的必然后果（成交那一刻分不出"回调守住"还是"反转"），
  不是 bug。要压 whipsaw 就调大挂单容忍（设置页），或改成反转确认后平仓/突破进场

### 第五轮：对账重复动作（已修，`_align_open`）

SL 校验块原来在补/减仓块**之前** → 同一轮既"覆盖量不符"又"需要补/减仓"时会
cancel+place **两遍**。已把 SL 块挪到补/减仓**之后**，并把减仓路径里的两个
提前 `return` 改成"跳过减仓但继续"（否则"疑似手动仓"守卫一触发，连 SL 维护也被跳过）。
走查新增 D1/D2 覆盖（断言挂 SL 的调用次数），变异 M159 锚定 D2。

## 3. 已有且**用户已决定不改**的（别提议）

- 跳空那一下（超容忍分支里 `_place_order` 仍可能转市价）—— 用户明确不加守卫
- 超势挂单永不超时 —— 与用户"停策略≠撤已下的单"语义一致，属设计
- `_limit_fill_state` 把"已撤"等同于"已成交"
- 跟单侧的共享全局竞态、`alphafox_occupied` 的 `except: return False`

## 4. 跑什么

```bash
python extra/run_tests.py supertrend web        # 改动覆盖这两组（含走查脚本）
python extra/_mutation_check.py --check-anchors
python extra/_mutation_check.py M148 M149 M150 M151 M152 M153 M154 M155 M156 M157 M158
# 全量回归 + 全量变异：等部署时再跑（跑前先问）
```

## 5. 部署清单（未部署）

上传：`exchange.py`、`supertrend_live.py`、`supertrend_strategy.py`、`web.py`、`storage.py`（+ 可选 `config.py`）
部署后：删死文件 `alphofox_live.py` → `python manage_processes.py restart` → 页面强刷。
**注意事项**：`exchange.close_position_market` 语义变了（向下取整、不兜 minQty）——
影响所有策略的平仓（跟单/web 手动平仓/exchange_bridge）；跟单组测试用自家 FakeExchange
测不到，部署后留意跟单侧平仓日志。首次对账会给"没有 sl_qty 字段"的旧记录重挂一次 SL（自愈）。
