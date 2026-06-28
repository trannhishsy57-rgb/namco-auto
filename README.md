# Namco Parks 抽選自動化 - 项目总文档

> 万代南梦宫「ナムコパークス オンラインストア」OP-16 入场抽选自动化项目  
> 最后更新：2026-06-27

---

## 目录

1. [项目概况](#一项目概况)
2. [当前状态](#二当前状态)
3. [网站与账号信息](#三网站与账号信息)
4. [完整下单流程 (6 步 API)](#四完整下单流程-6-步-api)
5. [业务规则（重要）](#五业务规则重要)
6. [技术要点与坑](#六技术要点与坑)
7. [脚本使用方法](#七脚本使用方法)
8. [文件清单](#八文件清单)
9. [后续升级方向](#九后续升级方向)

---

## 一、项目概况

**目标：** 自动化「ONE PIECE OP-16 购入権チケット」的**抽选申込**（免费报名抽签）。

**已实现：** 全链路自动化跑通并实际报名成功一单。

```
登录 → 找票 → 加购物车 → 结算 → 确认订单 → 完成下单（拿注文番号）
```

**技术栈：** Python 3.12 + requests + BeautifulSoup4

---

## 二、当前状态

| 项目 | 状态 |
|------|------|
| 脚本开发 | ✅ v3 全自动版完成 |
| 登录自动化 | ✅ 成功 |
| 加购物车 | ✅ 成功（关键修复：`request=insert`） |
| 全链路下单 | ✅ 手动验证成功 |
| 已报名订单 | **EC-2105077634**（富山高岡店 OP-16） |
| 抽选结果公布 | 2026/7/1（三）17:00 |

**当前卡点：** 已报名的账号因"一人一店一次"规则无法重新报名 OP-16；该免费抽选无自助取消入口。要重测需换新账号或用其他免费票。

---

## 三、网站与账号信息

**网站：**
- Base URL: `https://parks2.bandainamco-am.co.jp`
- 框架: Ebisu EC（Java Web）
- 入场票分类页: `/category/EL/`
- 登录页: `/login.html`
- 客服咨询窗口: `https://bnam-customer.my.site.com/NamcoParks/s/createrecord/InquirySpot`

**账号：**
- 邮箱: `rekv68w80t@livee.email`
- 密码: `Qwe123456`
- 会员号: `B001866278593`
- 目标店铺: 富山高岡店

**Session Cookies（登录后获得）：**

| Cookie | 用途 |
|--------|------|
| `JSESSIONID` | 核心 session ID |
| `framework.security_id` | CSRF 保护 |
| `sc_2844_UW` | 登录态标记（登录后新增） |
| `AWSALB` / `AWSALBCORS` | AWS ALB 负载均衡 |
| `esi_2844_UW` | Ebisu 框架 session |

---

## 四、完整下单流程 (6 步 API)

> 详细参数见 `CHECKOUT_FLOW.md`，这里是精简版。

```
GET  /login.html                     ← 拿 session cookies + 隐藏字段
POST /top_login.html                 ← 登录 (request=logon)
  │
GET  /category/EL/{item_cd}.html     ← 商品详情 + token + 属性选项
  │
POST /cart_index.html?request=insert ← 加购物车 (response_type=json)
GET  /cart_index.html                ← 查看购物车
  │
POST /cart_seisan.html               ← 进入结算页（自动填充地址）
  │
POST /cart_confirm.html              ← 确认订单 (request=confirm + 个人信息)
  │
POST /cart_pre.html                  ← 预处理 (request=cart_order_pre)
  │
POST /cart_complete.html             ← 下单完成 → 注文番号 (EC-xxxx)
```

**各步骤关键参数：**

| 步骤 | Endpoint | 关键参数 |
|------|----------|----------|
| 登录 | `POST /top_login.html` | `request=logon`, `LOGINID`, `PASSWORD` |
| 加购 | `POST /cart_index.html?request=insert` | `item_cd`, `CART_AMOUNT_0=1`, `request=insert` |
| 结算 | `POST /cart_seisan.html` | `CART_AMOUNT_0=1`, `CART_INDEX_REFERER` |
| 确认 | `POST /cart_confirm.html` | `request=confirm`, `token`, 姓名/地址/电话 |
| 预处理 | `POST /cart_pre.html` | `request=cart_order_pre`, `token`, `mode=0` |
| 完成 | `POST /cart_complete.html` | `token` |

**Token 机制：** session 级别，同一 session 内所有请求共享同一个 token，重新登录后会变。

---

## 五、业务规则（重要）

> 来自票详情页和申込方法说明。

| 规则 | 原文 | 含义 |
|------|------|------|
| **免费报名** | 申込時にお支払いはありません | 报名不付钱，中签后当天到店付 |
| **一人一店一次** | お一人様1回、1店舗まで（複数店舗のご応募はできません） | 一个账号只能报名 1 个店铺 1 次 |
| **不可变更** | ご予約完了後の変更は承っておりません | 申込后不接受日期/时间变更 |
| **无自助取消** | （页面无取消按钮） | 取消需走客服咨询窗口 |
| **结果公布** | 2026/7/1(水) 17:00頃 | 中签邮件通知，落选无通知 |
| **结果查询** | マイページ「購入履歴」的注文番号状态 | 通过订单状态看是否中签 |
| **本人确认** | 抽選にご当選されたご本人様のみ購入可能 | 中签需本人带证件到店 |
| **禁止转卖** | 譲渡・転売・複製・偽造は禁止 | 购入権票不可转让 |
| **不补发** | 予約チケットの再発行は一切しかねます | 票不补发 |

---

## 六、技术要点与坑

> 实战中踩过的坑，给以后省时间。

| 问题 | 原因 | 解决 |
|------|------|------|
| **登录报"需开启cookie"** | 直接 POST 登录没有 session | 必须先 `GET /login.html` 拿 cookie 再 POST |
| **加购物车成功但购物车空** | `request` 字段为空 | JS `putItemToCart()` 设 `request=insert`，必须手动加 |
| **SSL 连接被重置 (10054)** | 本地代理 `127.0.0.1:19828` 拦截 + Python OpenSSL 不支持代理 SSL 重协商 | `requests` 加 `verify=False` |
| **503 错误** | 网站访问集中限流（非被封） | 加 3-6 秒随机延迟 + 重试（等 10-30s） |
| **Session 过期** | — | 表现为 302 跳转到 `login.html` |
| **PowerShell 内联 Python 转义** | 引号冲突 | 写成 `.py` 文件再运行，别内联 |

**判断成功的标志：**
- 登录成功：响应含 `ログアウト`，不含 `ログイン`
- 加购成功：响应含 `カートに追加されました`
- 下单成功：响应含 `ご注文完了` 或匹配 `EC-\d+`

---

## 七、脚本使用方法

**运行：**

```bash
cd c:\Users\da983\Downloads\namco-auto
python namco_lottery.py
```

**配置（改 `namco_lottery.py` 顶部）：**

```python
CONFIG = {
    "email": "你的邮箱",
    "password": "你的密码",
}

MODE = "checkout"              # 运行模式
TARGET_STORES = ["富山高岡"]    # 目标店铺关键字，留空 [] = 全部
```

**MODE 三种模式：**

| MODE | 说明 |
|------|------|
| `"dry"` | 只解析表单，不提交（安全测试） |
| `"cart"` | 只加购物车，不结算 |
| `"checkout"` | 全自动到下单完成 |

**依赖安装：**

```bash
pip install requests beautifulsoup4
```

---

## 八、文件清单

| 文件 | 作用 |
|------|------|
| `README.md` | **本文件**（项目总索引） |
| `namco_lottery.py` | 主脚本（v3 全自动下单） |
| `CHECKOUT_FLOW.md` | 完整 API 流程文档（6 步请求详解） |
| `ARCHITECTURE.md` | 通用自动化工程架构（7 模块 + 升级路径） |
| `STATUS.md` | 进度与情况记录 |
| `parse_har.py` | HAR 抓包分析工具 |
| `inspect_form.py` | 表单结构分析工具 |
| `namco_har_log.json` | 脚本生成的请求日志 |
| `results_summary.json` | 批量申込结果 |
| `debug_*.html` | 各步骤响应页面（存档） |
| `submit_response_*.html` | 提交后响应页面（存档） |

---

## 九、后续升级方向

> 详见 `ARCHITECTURE.md`。当前 v3 是同步单任务版，升级路径：

| 优先级 | 改动 | 收益 |
|--------|------|------|
| 1 | `FormParser` 统一表单解析类 | 减少重复代码 |
| 2 | `tenacity` 重试装饰器 | 指数退避 + 抖动 |
| 3 | `WebFlowFSM` 状态机 | session 过期自动重登录 |
| 4 | `httpx.AsyncClient` + `Scheduler` | 多账号并发 |
| 5 | `ProxyPool` + `TaskDB` | 多出口 IP + 断点恢复 |

**多账号场景：** 因"一人一店一次"，要覆盖多店铺需多账号。架构升级到 v4（异步并发 + 代理池 + SQLite 持久化）后，可实现「N 个账号 × N 个店铺」的批量报名调度。

---

## 免责声明

本项目仅用于个人合法用途的自动化学习与技术研究。使用者需自行遵守网站服务条款及相关法律法规。
