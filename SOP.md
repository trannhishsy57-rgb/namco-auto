# Namco Parks 抽選自動化 — 实战操作手册 (SOP)

> 「开抢当天照着这份做」的标准流程。任何一场新抽选，按本文从头跑到尾即可。
> 配套脚本：`namco_prod.py`（抢票）、`namco_result.py`（登录式结果识别）
> 最后更新：2026-06-28

---

## 0. 一句话流程

```
T-1天  准备账号 + 配服务器 + 改config + 跑dry测试
T-15分 启动 warmup 模式（自动登录全部账号 + 保活）
T=0    脚本在开抢时刻自动同步触发，全部账号下单
T+结果 跑 namco_result.py 登录查中签账号 → 交付客户
```

---

## 1. 角色与资产清单

开跑前确认这些都到位：

| 资产 | 说明 | 检查命令 / 位置 |
|------|------|----------------|
| VPS | Vultr Tokyo, Python 3.12 venv | `ssh root@45.76.195.59` |
| 代码 | GitHub `trannhishsy57-rgb/namco-auto` | `git log --oneline -1` |
| 账号池 | email + password 列表 | `config.toml` 的 `[[accounts]]` |
| 抢票目标 | event_keyword / 店铺 / 时间段 | `config.toml` 的 `[target]` |
| 开抢时刻 | 日本时间，精确到分 | 票详情页「申込期間」 |

---

## 2. T-1天：准备阶段

### 2.1 同步最新代码到 VPS

```bash
ssh root@45.76.195.59
cd /root/namco-auto
git pull
source venv/bin/activate
python -c "import httpx, aiosqlite, bs4; print('deps OK')"
```

> ⚠️ 如果是新机器，先装 Python 3.12（系统自带 3.14 会让 bs4 段错误崩溃）：
> ```bash
> add-apt-repository -y ppa:deadsnakes/ppa && apt update
> apt install -y python3.12 python3.12-venv python3.12-dev
> cd /root/namco-auto && python3.12 -m venv venv && source venv/bin/activate
> pip install httpx aiosqlite beautifulsoup4 lxml urllib3
> ```

### 2.2 录入账号池

编辑 `config.toml`，每个账号一段：

```toml
[[accounts]]
email        = "xxx@livee.email"
password     = "Qwe123456"
target_store = ""        # 留空=自动选第一个；填"札幌"=只选札幌场
proxy        = ""        # 留空=裸IP直连
```

> **规则提醒：一人一店一次。** 同一账号不能在同一活动报名两次。要覆盖多店铺/多时段必须用多账号。

### 2.3 ⚠️ 配置时间段分配（必须人工确认，禁止硬编码）

这一步**每场都要当面问清楚**，不能用上次的数字。
时间段索引对应票页面下拉框（排除占位项后从0开始）。OP-16 实测=每小时一档、两天共 **18 档**
（2026-06-28 实测；旧版"4档/天"是错的）：

```
7/4(土): 0=11:00 1=12:00 2=13:00 3=14:00 4=15:00 5=16:00 6=17:00 7=18:00 8=19:00
7/5(日): 9=11:00 10=12:00 11=13:00 12=14:00 13=15:00 14=16:00 15=17:00 16=18:00 17=19:00
```

> 每场开抢前用控制台 JS 抽查一次时段数（见 docs/SITE_NOTES.md），别假设永远 18 档。

在 `[target]` 下填权重（数字 = 分到该时段的账号数，按 `账号序号 % 总和` 循环分配）：

```toml
[target]
# 18 档按顺序填；下例偏好午后高峰
slot_weights = [50,50,80,80,80,80,80,50,30, 50,50,80,80,80,80,80,50,30]
```

- 留空 `[]` = 所有账号都选第一个可用时段（仅测试用）。
- ⚠️ **抽選(摇号)下，时段只决定中签后的入场时刻，不影响中签率**——所以权重是"按客户想去的时间分配名额"，不是抢票策略。

### 2.4 设置开抢时刻

```toml
mode               = "warmup"
lottery_open       = "2026-07-04T10:00:00+09:00"   # 日本时间，必须带 +09:00
pre_login_minutes  = 10     # 提前10分钟完成登录
keepalive_interval = 60     # 每60秒保活一次
```

### 2.5 dry 测试（不下单，只验证登录+找票+时段分配）

把 `mode` 临时改成 `"dry"` 跑一次：

```bash
rm -f namco_tasks.db && python namco_prod.py 2>&1 | tee dry_test.log
```

**检查清单：**
- [ ] 每个账号 `login.ok`，无 403/captcha
- [ ] `Found N OP-16 lottery tickets`
- [ ] 每账号日志里 `slot=X` 和实际选中的时段一致
- [ ] 无 `Worker crash`

确认无误后把 `mode` 改回 `"warmup"`。

---

## 3. T-0：开抢日执行

### 3.1 用 tmux/screen 启动（防 SSH 断线）

```bash
ssh root@45.76.195.59
cd /root/namco-auto && source venv/bin/activate
tmux new -s namco          # 新建会话（断线后 tmux attach -t namco 恢复）
rm -f namco_tasks.db results.jsonl
python namco_prod.py 2>&1 | tee run_$(date +%Y%m%d).log
```

脚本行为（全自动，无需干预）：
1. 算出 `warmup_at = lottery_open - pre_login_minutes`，**自动 sleep 到该时刻**
2. 并发登录全部账号 → 打印 `Warmup done: N/M logged in`
3. **预暂存**：每个会话提前 GET 票详情页 + 解析加购表单 → `Staged N/M sessions ready for T=0`
4. 每 60 秒保活 ping。**掉线自动恢复**：ping 时若某会话被弹回 `login.html`（session 掉了），自动重登 + 重新预暂存 → `⚠ N session(s) dropped — re-logging in` / `Recovered ✓`。这保证 10 分钟空等期间不会有账号悄悄失效。
5. **开抢前刷新 token**：临近开抢（约 90s 前）对所有会话再抓一次票面/表单 → `Re-staged N/M sessions with fresh tokens`，防止 staged token 在空等期间过期（cookie 还活着但 token 失效会导致 T=0 报「画面操作の誤り」）。
6. 到 `lottery_open` 瞬间 → `🔥 FIRING N sessions simultaneously!`，**第一个动作就是抢名额的 cart POST**（登录/找票/表单都已预热完）
7. 全部账号同步下单，逐个打印 `⚡ CART secured at T+XXXms` / `SUCCESS / Order: EC-xxxx`

> 离开 tmux：`Ctrl+B` 然后按 `D`。重新进入：`tmux attach -t namco`。

### 3.2 实时监控要点

- **时段分配核对**：启动时打印 `时段分配 (SLOT DISTRIBUTION)` 表格，开抢前肉眼核对每个时段分到的账号数对不对。
- **掉线恢复**：看到 `Recovered ✓` 是正常的（保活期间偶有掉线被自动救回）；若看到 `Recovery FAILED ... absent at fire` 说明该账号开抢时会缺席，需人工查（密码/封号/IP）。
- **抢名额速度**：每个成功账号打印 `⚡ T=0→cart: XXXms`，这是抢热门场的命门——越接近 0 越好。
- **限流**：日志出现 `503` 重试 → 当前并发偏高或需要代理；非 race 阶段连续失败会触发 `Circuit OPEN` 自动刹车。
- **批次完成**：最后打印 `⚡ 速度报告` 表格 + `BATCH COMPLETE` 统计。

### 3.3 结果落盘

- `results.jsonl` — 每账号一行，含 `order_number`、`step_ms`、`fire_offset_ms`、`success`
- `speed_report.json` — 聚合速度统计（p50/p95/p99）
- `run_YYYYMMDD.log` — 完整日志

快速统计成功数：
```bash
grep -c '"success": true' results.jsonl
```

---

## 3X. 速度优化（核心：热门场1~2秒填满）

> 同行多、竞争大，速度决定成败。本系统已做三层提速 + 全程计时，支持逐步优化。

### 为什么必须预热（实测数据，Tokyo VPS 裸IP）

`racetest` 测出**冷启动**到「准备好发起抢购」需要：

| 步骤 | p50 | p95 | max |
|------|-----|-----|-----|
| 登录 | 1497ms | 1970ms | 2104ms |
| 找票 | 1167ms | 2110ms | 2480ms |
| 解析加购表单 | 394ms | 507ms | 548ms |
| **就绪总耗时** | **2888ms** | **4589ms** | **5133ms** |

**结论：冷启动光"准备好"就要 2.9~5.1 秒，已经超过 1~2 秒窗口。**
所以登录+找票+表单**必须在开抢前完成**（warmup 预暂存做的就是这个），
T=0 时第一个网络请求直接是抢名额的 cart POST。

### 三层提速（已实现）

1. **预暂存 (staging)**：warmup 阶段把登录、找票、表单解析全做完，开抢瞬间零准备。
2. **race_mode**：砍掉所有自加的礼貌延迟（原来 30~70s）和限速器（原来 0.5 req/s）。`config.toml` 里 `race_mode = true`（warmup 默认开）。
3. **统一 T=0 + 并发触发**：所有账号共享同一开抢时刻引用，`asyncio.gather` 同时发射。

### 测速命令（不消耗账号额度）

```bash
# 改 config.toml: mode = "racetest"，然后：
python namco_prod.py
```
只做登录→找票→解析表单（只读，**不提交**），重复3轮，输出 `racetest_report.json`。
**用途**：换服务器/换代理/改并发后，先跑 racetest 对比就绪耗时，再决定是否上正式。

### 逐步优化方向（按收益排序）

| 优先级 | 手段 | 预期收益 |
|--------|------|---------|
| 1 | 预暂存（已做） | 把 2.9s 准备时间挪到开抢前 |
| 2 | 服务器选址靠近东京 | 降 RTT，cart POST 更快 |
| 3 | 调 `max_concurrent` | 太高触发503，跑 racetest 找平衡点 |
| 4 | 加代理池分散出口IP | 防单IP限流（裸IP够用就先不加） |
| 5 | HTTP/2 连接复用 / keepalive 预连 | 省 TLS 握手 |

每次改动后看 `speed_report.json` 的 `fire_offset_ms.cart` 的 p50/p95 是否下降。

---

## 4. T+结果公布：中签识别（登录式，不用邮件）

> **为什么不用邮件**：落選通常不发邮件，只能登录看；登录态本来就有，比配 IMAP 省事可靠。
> 结果公布时刻见票页「抽選結果」。

### 4.1 一条命令查全部账号

```bash
python namco_result.py --output winners.json
```

脚本对每个账号：登录 → 打开「購入履歴」(`member_history.html`) → 读注文番号状态标签 → 判定。

**状态判定（来自 `<span class="...status-icon">` 标签文字）：**

| 标签 | 判定 |
|------|------|
| `抽選前` / `抽選中` | pending（结果未公布） |
| `当選` | **win 中签** |
| `落選` / `抽選対象外` | lose 落选 |
| 其他 | unknown（人工确认） |

### 4.2 输出

控制台直接打印汇总 + 中签账号（含密码），并写入 `winners.json`：
```
当選 WIN:   N
落選 LOSE:  M
抽選前:     K     ← 开奖前跑会全是这个

🎉 当選アカウント:
  xxx@livee.email   EC-2105xxxx  pwd=Qwe123456
```

> 开奖前跑会显示全部「抽選前」（已实测验证）；开奖后同一命令自动识别 当選/落選。

### 4.3 交付客户

`winners.json` 的 `winners[]` 里每条含 `account + password + order_number`，直接交给买票客户（免支付票，凭账号到店）。

```bash
# 只看中签账号密码
python -c "import json; [print(w['account'], w['password'], w['order_number']) for w in json.load(open('winners.json'))['winners']]"
```

---

## 5. 故障速查

| 现象 | 原因 | 处理 |
|------|------|------|
| `Queue summary: {'DEAD_LETTER': N}` | 上次失败任务残留 | `rm namco_tasks.db` 重跑 |
| `cart_seisan.html → 403` | 缺 Referer 头 | 已修复；确认代码是最新 `git pull` |
| 登录全失败 | 密码错/账号被封/IP被限 | 先单账号 dry 测试定位 |
| `503` 频繁 | 并发过高 | 调低 `max_concurrent` 或加 `[proxy].pool` |
| bs4 段错误退出 | 跑在 Python 3.14 | 必须用 `venv`（3.12） |
| Python 进程被杀 | 1GB 内存不够大批量 | 分批跑 或 升级 VPS 规格 |

---

## 6. 开跑前最终 Checklist

```
□ git pull 已同步最新代码（含 Referer 修复 + 速度提速 + 4功能）
□ config.toml 账号池已录入，数量正确
□ slot_weights 已【当面确认】，非沿用上次
□ lottery_open 时刻正确，带 +09:00 时区
□ mode = "warmup"  且  race_mode = true
□ dry 测试通过：登录OK / 找到票 / slot分配正确
□ racetest 测速跑过，就绪耗时在预期内（换服务器/代理后必跑）
□ 在 tmux 里启动，已 rm 旧 namco_tasks.db
□ 服务器时间与日本同步（date -u 核对）
```

---

## 附：常用命令速查

```bash
# 进 VPS
ssh root@45.76.195.59

# 进项目 + 激活环境
cd /root/namco-auto && source venv/bin/activate

# 同步代码
git pull

# 清空状态重跑
rm -f namco_tasks.db results.jsonl

# 后台跑（防断线）
tmux new -s namco          # 启动
tmux attach -t namco       # 恢复
# Ctrl+B 再 D = 离开但保持运行

# 测速（mode=racetest，只读不提交，换服务器/代理后必跑）
python namco_prod.py 2>&1 | tee racetest.log

# 抢票
python namco_prod.py 2>&1 | tee run.log

# 结果识别（登录式，开奖后跑）
python namco_result.py --output winners.json

# 成功数统计
grep -c '"success": true' results.jsonl

# 看抢名额速度分布（开抢后）
python -c "import json; d=json.load(open('speed_report.json')); print(d['fire_offset_ms'])"
```
