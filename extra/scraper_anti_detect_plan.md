# scraper 反检测优化方案

## 背景
6/4-6/10 日志显示：同 IP、同请求量，5/30-6/3 完美运行 857 轮零异常，6/4 突然 Timeout 爆发，6/9 升级为 ERR_CONNECTION_RESET。
market.py 和 worker.py 的 FAPI 调用量在 v3.0 到 v4.9 之间没有增加，CG/GMGN 打的是不同域名。结论：币安侧单方面收紧反爬策略。
优化方向：让 Playwright 浏览行为更像人，降低行为检测命中率。

---

## P0-1：随机点击 + 防跳转

### 原因
当前 click/scroll 比 = 0，5 分钟内从不点击任何东西，是人类/机器分类的最强信号。

### 改动位置
scraper.py 第 280-284 行（替换现有鼠标移动块）

### 改动内容
1. 触发概率 18%（原来 15%）
2. 鼠标移到随机坐标，范围收窄到 (300-900, 200-600)，避开左侧导航栏和右侧推荐栏
3. 35% 概率点击同一个坐标，点击前 0.1-0.4s 随机犹豫延迟（模拟人类反应）
4. 整个点击逻辑包在 try/except，任何异常不影响后续滚动
5. 点击后等 0.3-0.8s 等页面反应
6. URL 锚点判断：检查 page.url 是否是 SQUARE_URL 或以 ?、# 结尾。跳走了说明点到了链接
7. 跳转后 goto 回来，重置 burst_start 和 burst_sec，缓冲 1.5-3s 等 DOM 就绪
8. 检查 context.pages 是否有新标签页（target="_blank"），有则关闭

### 额外需要
需要在 scrape_continuous 函数开头声明 burst_min 变量（或跳转恢复处用 getattr 读取 config）

---

## P0-2：Burst/Idle 随机化

### 原因
固定 35s 滚动 + 70s 休息，节奏过于规律，ML 分类模型极易识别。

### 改动位置
scraper.py 两处：
- 第 209-210 行：删除固定 burst_seconds/idle_seconds 变量声明
- 第 253-262 行：替换 burst/idle 循环逻辑

### 改动内容
1. 每次 burst 时长在循环内随机：config.SCRAPE_BURST_SECONDS 的 0.5x 到 2.0x（17.5s-70s）
2. 每次 idle 时长在循环内随机：config.SCRAPE_IDLE_SECONDS 的 0.3x 到 1.8x（21s-126s）
3. 15% 概率跳过 idle，直接进入下一段 burst
4. 变量命名：burst_min 读 config 值作为基准，idle_ref 读 config 值作为基准
5. 每次 burst 结束后重新生成 burst_sec 和 idle 值

---

## P1：刷新随机化

### 原因
固定 18-25 次滚动刷新一次，page.goto 永远是同一个 URL，刷新间隔可预测。

### 改动位置
- scraper.py 第 286-297 行（替换刷新块）
- config.py SCROLL_RESET_EVERY_MIN / MAX 改为 30 / 100

### 改动内容
1. 刷新间隔扩宽：30-100 次滚动（config 值配合修改）
2. 10% 概率跳过本次刷新，继续滚动不刷新（下次再试）
3. 有时用 page.reload() 替代 page.goto()，模拟 F5 刷新
4. reload 前做安全检查：如果 page.url 不在广场，改用 goto
5. reload 超时 20s（比 goto 快，原地刷新）
6. 刷新后停留 2-8s（原来是 2-4s）
7. 刷新成功后重置 burst_start 和 burst_sec
8. 刷新后 next_reset_at 正常推进（保持原有逻辑）
9. 如果页面已死（ERR_CONNECTION_RESET），不重试，break 跳出（避免重复请求相同 URL）

---

## P2：超时 + 初始停留

### 原因
90s page.goto 超时 × 3 次重试 = 最多 270s 在请求一个不响应的 URL，完全不像人。初始停留 2.5-4.5s 太短，不像人打开页面后的行为。

### 改动位置
- config.py：PAGE_GOTO_TIMEOUT 从 90000 改为 30000
- scraper.py 第 251 行：初始停留从 2500-4500ms 改为 3000-8000ms

---

## 改动量汇总

| 优先级 | 改动 | 文件 | 约行数 |
|--------|------|------|--------|
| P0-1 | 随机点击 + 防跳转 | scraper.py | ~25 |
| P0-2 | Burst/Idle 随机化 | scraper.py | ~10 |
| P1 | 刷新随机化 | scraper.py + config.py | ~15 |
| P2 | 超时 + 初始停留 | scraper.py + config.py | ~3 |
| 合计 | | 2 文件 | ~53 |

---

## 不改的部分
- _handle_response / _scan_for_posts / _process_post — 帖子采集逻辑不动
- _extract_tokens_from_post / _extract_followers — 数据提取不动
- worker.py — 主循环不动
- market.py — API 调用不动

---

## 已考虑并排除的风险
- P0-1 点击触发导航跳转 → URL 锚点判断 + goto 恢复
- P0-1 点击打开新标签 → 检查 context.pages 并关闭
- P0-1 点击坐标命中导航区 → 坐标收窄到主内容区
- P0-2 极值导致空转轮次 → burst 最短 17.5s，idle 最长 126s
- P1 reload 到错误页面 → reload 前 URL 安全检查
- P1 ERR_CONNECTION_RESET 后重试 → 不重试，break 跳出
- P2 超时太短导致误判 → 30s 对页面加载足够，失败用重试机制兜底

---

## 未纳入的优化（做不全不如不做）
- P1-2 屏蔽 headless 指纹：navigator.webdriver 可以遮，但 plugins/WebGL/字体等指纹点太多，遮一个被另一个暴露，不做
