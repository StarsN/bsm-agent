# 币安实盘镜像桥接 — 全流程详解

> 生成时间: 2026-06-20  
> 涉及文件: `exchange_bridge.py`, `exchange.py`, `onchain_scanner.py`, `auto_trader.py`, `web.py`, `storage.py`, `config.py`

---

## 一、架构总览

```
┌──────────────────────────────────────────────────────────────┐
│                    binance_square.db (纸盘)                   │
│                                                              │
│  trade_positions (strategy='onchain', mode='paper')          │
│  trading_settings (onchain_live_mode)                        │
│                                                              │
│  纸盘逻辑完全不动 ← 开仓风控/止盈止损/仓位计算                  │
└────────────────────────┬─────────────────────────────────────┘
                         │
                         │  读取纸盘状态，镜像同步
                         ▼
┌──────────────────────────────────────────────────────────────┐
│                 exchange_bridge.py                           │
│                                                              │
│  mirror_open()         纸盘开仓 → 币安开仓+挂单               │
│  sync_orders()         每2秒 → 检测纸盘变化 → 同步币安        │
│  close_live_position() 手动平仓 → 币安平仓                    │
└────────────────────────┬─────────────────────────────────────┘
                         │
                         │  exchange.py (签名 API)
                         ▼
┌──────────────────────────────────────────────────────────────┐
│                    Binance Futures                            │
│                                                              │
│  合约持仓 · SL/TP/追迹 挂单                                   │
└──────────────────────────────────────────────────────────────┘
                         │
                         │  记录轨迹
                         ▼
┌──────────────────────────────────────────────────────────────┐
│                 db/onchain_live.db                           │
│                                                              │
│  live_positions        镜像持仓 (1:1 映射 paper_position_id)  │
│  live_trade_log        交易日志 (open/close)                  │
│  live_daily_snapshots  每日账户快照                           │
└──────────────────────────────────────────────────────────────┘
```

---

## 二、mirror_open — 开仓镜像

```
_onchain_open_position(token, pool_entry)
  │
  ├─ get_market_snapshot(token)  → mark_price
  ├─ open_paper_position(main_conn, ...)  → paper OPEN
  │    纸盘写入: entry_price(with slippage), quantity, sl, tp1, tp2, leverage
  │
  ├─ SELECT * FROM trade_positions WHERE ... ORDER BY id DESC LIMIT 1
  │    → 取回完整 paper_position dict (含 id, quantity, entry_price, sl, tp1, tp2...)
  │
  └─ if settings.onchain_live_mode == "live":
        exchange_bridge.mirror_open(paper_position)
          │
          ├─ ① set_leverage("PEPEUSDT", 1)
          │     └─ POST /fapi/v1/leverage
          │     失败 → return False (无仓位、无DB记录)
          │
          ├─ ② market_open_long("PEPE", qty)
          │     └─ POST /fapi/v1/order type=MARKET side=BUY
          │     └─ 返回: {order_id, fill_price, actual_qty, status}
          │     失败 → return False (无仓位、无DB记录)
          │
          ├─ ③ take_profit_order("SELL", qty×80%, tp1)     ← best-effort
          │     └─ POST /fapi/v1/order type=TAKE_PROFIT_MARKET reduceOnly
          │     失败 → orders["tp1"]=None, 不影响
          │
          ├─ ④ take_profit_order("SELL", qty×10%, tp2)     ← best-effort
          │     └─ POST /fapi/v1/order type=TAKE_PROFIT_MARKET reduceOnly
          │     失败 → orders["tp2"]=None, 不影响
          │
          ├─ ⑤ stop_loss_order("SELL", qty, sl)            ← best-effort
          │     └─ POST /fapi/v1/order type=STOP_MARKET reduceOnly
          │     失败 → orders["sl"]=None, 不影响
          │
          └─ ⑥ 写 live DB
                ├─ live_position_insert → live_positions (status=OPEN)
                ├─ live_trade_log_insert → action="open"
                └─ _snapshot_daily → live_daily_snapshots
                DB失败 → print 日志, return True (仓位在币安, 下次sync恢复)
```

### 关键设计点

- **set_leverage + market_open 失败 → 立即中止**，不存在部分仓位
- **SL/TP best-effort**：失败不阻塞，安全网下轮补挂
- **fill_price 用交易所实际成交价**，非纸盘滑点价
- **actual_qty 用交易所实际成交数量**，非纸盘计算数量

---

## 三、sync_orders — 状态机 (每2秒)

```
auto_trader.one_scan()
  │
  ├─ update_paper_positions(conn)    ← 纸盘 TP/SL 检测 (不动)
  │
  └─ exchange_bridge.sync_orders(conn)
       │
       ├─ 检查 onchain_live_mode != "live" → return
       │
       ├─ 加载纸盘: SELECT * FROM trade_positions
       │    WHERE strategy='onchain' AND status IN ('OPEN','PARTIAL','PENDING')
       │
       ├─ 加载交易所持仓: exchange.get_positions()  ← 一次性
       │
       └─ for each paper_position:
            │
            ├─ live = get_live_by_paper(pid)
            │   │
            │   ├─ 无 live 记录 ─→ _recover_live_record()
            │   │   └─ 交易所若有持仓 → 补建 live 记录
            │   │      交易所若无持仓 → 跳过 (尚未镜像)
            │   │   └─ continue
            │   │
            │   └─ 有 live 记录 ─→ 进入状态检测 ↓
            │
            ├─ [检查1] pp.status == "CLOSED" AND live.status != "CLOSED"
            │   └─ _close_exchange_and_mark()
            │       ├─ cancel_all_orders(token)
            │       ├─ 如果交易所仍有持仓 → close_position_market
            │       ├─ live.status = "CLOSED"
            │       └─ live_trade_log: action="close"
            │   └─ continue
            │
            ├─ [检查2] closed_qty >= qty × 0.99 AND live.status != "CLOSED"
            │   └─ _close_exchange_and_mark()  ← 同检查1
            │   └─ continue
            │
            ├─ [检查3] closed_qty >= qty × 0.8 AND NOT live.tp1_done
            │   └─ _recreate_sl_breakeven()
            │       ├─ cancel_order(old_sl) → orders["sl"] = None
            │       ├─ stop_loss_order(remaining, entry_price)  ← SL@保本
            │       ├─ live.tp1_done = 1, live.status = "PARTIAL"
            │       └─ 如果 remaining=0 → 仅设 tp1_done
            │   └─ continue
            │
            ├─ [检查4] closed_qty >= qty × 0.9 AND tp1_done AND NOT tp2_done AND trailing>0
            │   └─ _recreate_trailing_stop()
            │       ├─ cancel_order(old_sl) → orders["sl"] = None
            │       ├─ trailing_stop_order(remaining, callback_rate=2.0%)
            │       ├─ 成功 → live.tp2_done=1, live.status="PARTIAL"
            │       └─ 失败 → 不设 tp2_done, 下轮重试
            │   └─ continue
            │
            ├─ [检查5] 交易所持仓是否还在
            │   └─ exchange.get_positions() 未找到此 token
            │       └─ _close_exchange_and_mark()  ← 交易所强平/已平
            │
            └─ [检查6] 安全网: 缺少 SL 保护?
                └─ live.status != CLOSED AND 无 sl AND 无 trailing
                    └─ stop_loss_order(remaining, paper.stop_loss_price)
                    └─ 更新 live.orders["sl"]
```

### 状态转换图

```
                    mirror_open
                        │
                        ▼
                 ┌─────────────┐
                 │    OPEN     │ ← 正常持有，有 SL+TP1+TP2
                 └──────┬──────┘
                        │
              纸盘 TP1 触发 (closed_qty ≥ 80%)
                        │
                        ▼
                 ┌─────────────┐
                 │  PARTIAL    │ ← TP1已平80%，SL@保本，TP2还在
                 │ tp1_done=1 │
                 └──────┬──────┘
                        │
              纸盘 TP2 触发 (closed_qty ≥ 90% + trailing>0)
                        │
                        ▼
                 ┌─────────────┐
                 │  PARTIAL    │ ← TP2已平10%，追迹止盈激活
                 │ tp2_done=1 │
                 └──────┬──────┘
                        │
              纸盘追迹/SL触发 → CLOSED
                        │
                        ▼
                 ┌─────────────┐
                 │   CLOSED    │ ← 全平
                 └─────────────┘
```

### 6个检查的顺序设计理由

1. CLOSED 优先 — 已平仓的无需再管 SL/TP
2. 0.99 全平次之 — 接近全平的直接清理
3. TP1 再之 — 需要先移SL保本，TP2依赖 tp1_done
4. TP2 最后 — 需要 tp1_done 前置条件
5. 交易所检查 — 异常检测，防止纸盘/交易所不一致
6. 安全网 — 兜底保护，修复任何遗漏的 SL

---

## 四、close_live_position — 手动平仓

```
策略页点 "平仓" → POST /api/trade/market-close
  │
  ├─ manual_close_on_unwatch(conn, token)     ← 纸盘 CLOSED (不动)
  │
  └─ if onchain_active:
        exchange_bridge.close_live_position(token)
          │
          ├─ cancel_all_orders(token)
          │   └─ DELETE /fapi/v1/allOpenOrders
          │
          ├─ exchange.get_positions()
          │   └─ GET /fapi/v2/positionRisk
          │
          ├─ if 有持仓:
          │     close_position_market(token, qty)
          │     └─ POST /fapi/v1/order type=MARKET side=SELL reduceOnly
          │
          └─ _record_close_in_db(token, ex_pos, result)
              ├─ live_positions → status=CLOSED
              └─ live_trade_log → action="close"
```

---

## 五、数据流 (纸盘 ↔ 币安)

```
                    纸盘                          币安
                 (binance_square)            (Binance Futures)
                ─────────────────          ─────────────────
开仓:
  entry_price   raw × 1.0005 (滑点)   →   实际成交价 (fill_price)
  quantity      risk.compute()        →   实际成交 quantity
  sl            ATR自适应             →   同纸盘值 (STOP_MARKET)
  tp1           entry + 1R            →   同纸盘值 (TAKE_PROFIT_MARKET)
  tp2           entry + 2R            →   同纸盘值 (TAKE_PROFIT_MARKET)

TP1后:
  stop_loss     entry (保本)          →   新 SL@entry (cancel+recreate)
  quantity      剩余 20%              →   剩余 ~20% (币安自动减持)

TP2后:
  trailing      最高×0.98             →   TRAILING_STOP_MARKET callbackRate=2.0
  quantity      剩余 10%              →   剩余 ~10%

全平:
  status        CLOSED                →   cancel_all_orders + (如有剩余) close_market
```

### PENDING 处理

纸盘 PENDING (挂单) 不会触发 `mirror_open`。`sync_orders` 读取 PENDING 状态的纸盘但无 live 记录 → 跳过。等纸盘 PENDING 触发成为 OPEN 时，`_open_onchain_position` 已执行完毕，所以不会镜像。这是正确的 — 挂单触发时的价格已经是新价格，应重新评估开仓条件。

实际上，纸盘 PENDING 触发的代码在 `update_paper_positions` 中，它直接在 DB 中更新状态为 OPEN，不会重新调用 `_open_onchain_position`。所以 PENDING→OPEN 的转换不会被桥接到交易所。

**这个场景需要一个补丁**：`sync_orders` 中，对状态为 OPEN/PARTIAL 但无 live 记录的纸盘持仓（且交易所无持仓），应调用 `mirror_open` 补开。

当前设计下，PENDING→OPEN 的纸盘持仓，sync_orders 会检测到：
- paper status=OPEN, live=None, 交易所无持仓
- `_recover_live_record` 尝试恢复 → 交易所无持仓 → 跳过
- 下轮继续跳过 → 纸盘永远缺实盘

**这是遗漏的 gap**。但概率极低 — PENDING 在每2秒检查中，一旦触发立即成为 OPEN，而 mirror_open 只在 `_open_onchain_position` 中被调用（scanner 每1小时才跑一次）。PENDING 是 `manual_open_on_watch` 创建的，onchain 策略不创建 PENDING。

结论：onchain 策略不创建挂单，所以这个 gap 不影响当前功能。后续如果有挂单需求再补。

---

## 六、DB 表结构

### db/onchain_live.db

```sql
live_positions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_position_id     INTEGER NOT NULL UNIQUE,  -- 1:1 映射
    token, symbol, side, strategy,
    entry_price, quantity, leverage, margin_usd,
    exchange_orders_json  TEXT,   -- {"open":id, "sl":id, "tp1":id, "tp2":id, "trailing":bool}
    tp1_done, tp2_done    INTEGER DEFAULT 0,
    status                TEXT DEFAULT 'OPEN',  -- OPEN/PARTIAL/CLOSED
    realized_pnl          REAL DEFAULT 0,
    created_at, closed_at
);

live_trade_log (
    id, token, strategy, action,   -- action: open/close
    price, quantity, paper_position_id,
    exchange_order_id, details_json,
    created_at
);

live_daily_snapshots (
    date, strategy,               -- PRIMARY KEY (date, strategy)
    equity, available_balance, margin_used,
    unrealized_pnl, realized_pnl, daily_pnl, fees_paid,
    snapshot_at
);
```

---

## 七、配置项

| 配置 | 位置 | 默认值 | 说明 |
|------|------|--------|------|
| `ONCHAIN_LIVE_MODE` | config.py / DB trading_settings | "paper" | paper/live |
| `LIVE_DB_PATH` | config.py | "db/onchain_live.db" | 实盘独立 DB |
| `BINANCE_API_KEY` | config.py | "" | 币安合约 API Key |
| `BINANCE_API_SECRET` | config.py | "" | 币安合约 API Secret |

---

## 八、异常恢复

| 异常场景 | 恢复机制 |
|---------|---------|
| mirror_open DB 写入失败 | sync_orders → _recover_live_record → 从交易所持仓补建 |
| SL/TP 挂单全部失败 | sync_orders → 安全网检测无 SL → 补挂 |
| 交易所强平 (纸盘还在) | sync_orders → 检查5 发现持仓消失 → _close_exchange_and_mark |
| trailing_stop_order 失败 | tp2_done 不设 → 下轮重试 → 安全网补 SL 过渡 |
| 手动平仓时 API 失败 | close_live_position try-catch → 打印日志 → 不影响纸盘 |
| 切换 paper→live 时有在途持仓 | 纸盘已有, 交易所无 → _recover_live_record 跳过 → 需手动平纸盘后重开 |
| 2秒间隔内的极端价格波动 | 纸盘先检测 → 下轮 sync 跟上 → 最大延迟 2 秒 |
