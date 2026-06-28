# Namco Parks 抽選自動化 — 实战操作手册 (SOP)

> 「开抢当天照着这份做」的标准流程。任何一场新抽选，按本文从头跑到尾即可。
> 配套脚本：`namco_prod.py`（抢票）、`namco_email.py`（结果识别）
> 最后更新：2026-06-28

---

## 0. 一句话流程

```
T-1天  准备账号 + 配服务器 + 改config + 跑dry测试
T-15分 启动 warmup 模式（自动登录全部账号 + 保活）
T=0    脚本在开抢时刻自动同步触发，全部账号下单
T+结果 跑 namco_email.py 提取中签账号 → 交付客户
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
时间段索引对应票页面下拉框（排除「選択してください」后从0开始）：

```
0 → 7/4(土) 11:00      4 → 7/5(日) 11:00
1 → 7/4(土) 13:30      5 → 7/5(日) 13:30
2 → 7/4(土) 16:00      6 → 7/5(日) 16:00
3 → 7/4(土) 18:30      7 → 7/5(日) 18:30
```

在 `[target]` 下填权重（数字 = 分到该时段的账号数，循环分配）：

```toml
[target]
slot_weights = [300, 100, 100, 100, 100, 100, 100, 100]
#               ↑0    ↑1   ↑2   ↑3   ↑4   ↑5   ↑6   ↑7
```

- 留空 `[]` = 所有账号都选第一个可用时段（仅测试用）。
- 总和不必等于账号数，按 `账号序号 % 总和` 循环套用。

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
3. 每 60 秒保活 ping，倒计时显示 `Xs until open`
4. 到 `lottery_open` 瞬间 → `🔥 FIRING N sessions simultaneously!`
5. 全部账号同步下单，逐个打印 `SUCCESS / Order: EC-xxxx`

> 离开 tmux：`Ctrl+B` 然后按 `D`。重新进入：`tmux attach -t namco`。

### 3.2 实时监控要点

- **速度**：每条 `SUCCESS` 行带 `Timing: login=.. cart=.. total=..ms`。热门场票1~2秒抢光，重点看 `cart` 和 `seisan` 耗时。
- **限流**：日志出现 `503` 重试 → 当前并发偏高或需要代理。
- **批次完成**：最后 `BATCH COMPLETE` 给出 Success/Failed 统计。

### 3.3 结果落盘

- `results.jsonl` — 每账号一行，含 `order_number`、`step_ms`、`success`
- `run_YYYYMMDD.log` — 完整日志

快速统计成功数：
```bash
grep -c '"success": true' results.jsonl
```

---

## 4. T+结果公布：中签识别

> 抽选结果以邮件通知（落选通常无邮件）。结果公布时刻见票页「抽選結果」。

### 4.1 配置邮箱 IMAP（开跑前补全）

在 `config.toml` 加一段（**密码是邮箱密码，不一定等于Namco密码**）：

```toml
[email_check]
imap_host = "imap.livee.email"   # livee.email 实测 143 端口非SSL可连
imap_port = 143
```

### 4.2 扫描全部邮箱

```bash
python namco_email.py --since 2026-07-01 --output winners.json
```

输出：
- 控制台逐账号打印 `✓ WIN / ✗ LOSE / ? UNK`
- `winners.json` — 中签账号列表 + 注文番号

### 4.3 交付客户

从 `winners.json` 提取中签的 `email + password`，给到买票客户即可（免支付票，客户凭账号到店）。

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
□ git pull 已同步最新代码（含 Referer 修复 + 4功能）
□ config.toml 账号池已录入，数量正确
□ slot_weights 已【当面确认】，非沿用上次
□ lottery_open 时刻正确，带 +09:00 时区
□ mode = "warmup"
□ dry 测试通过：登录OK / 找到票 / slot分配正确
□ [email_check] IMAP 已配（密码待客户提供则标注）
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

# 抢票
python namco_prod.py 2>&1 | tee run.log

# 结果识别
python namco_email.py --since YYYY-MM-DD --output winners.json

# 成功数统计
grep -c '"success": true' results.jsonl
```
