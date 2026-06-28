# 目标站点情报 · ナムコパークス オンラインストア

> 把散落在 CHECKOUT_FLOW.md / STATUS.md / 抓包 / 实测里的**站点知识**汇到一处的单一入口。
> 这是「关于网站」的情报，不是脚本说明。脚本见 README.md / SOP.md。
> 最后更新：2026-06-28

---

## 1. 基本信息

| 项 | 值 |
|---|---|
| 站点 | `https://parks2.bandainamco-am.co.jp` |
| 运营 | 万代南梦宫娱乐（Bandai Namco Amusement） |
| 框架 | **Ebisu EC**（Java Web），厂商 `jp.co.interfactory`（interfactory framework） |
| 基础设施 | AWS ALB 负载均衡（cookie 里有 `AWSALB` / `AWSALBCORS`） |
| 业务对象 | ONE PIECE OP-16 購入権チケット **抽選申込**（免费抽签报名） |
| 商品规模 | OP-16 共 **21 个店铺**可选 |

---

## 2. 业务规则（最关键 — 决定能不能重测 / 扩量）

| 规则 | 原文 / 说明 |
|---|---|
| **免费抽选** | 报名不付钱，**中签后当天到店才付**（0 円商品，无支付步骤） |
| **一人一店一次** | 「お一人様1回、1店舗までお申込み可能」——一个账号只能报 1 店 |
| **多店不可** | 「複数店舗のご応募はできない」 |
| **申込后不可变更** | 「ご予約完了後の変更は承っておりません」 |
| **无自助取消** | 页面无取消按钮，取消需走客服窗口 |
| **结果公布** | 见票页「抽選結果」时刻；未中自动作废 |

> 直接后果：**测全流程必须用干净账号**——一旦某账号报名成功，它就占满名额，无法再测。

---

## 3. 抽选时段结构（来自下拉框截图，单场示例）

两天 × 各 4 个时段，共 8 个 slot（对应 `PRIORITY_ITEMPROPERTY_CD_MATRIX_0` 下拉，排除占位项后从 0 开始）：

| slot | 日期 / 时间 | slot | 日期 / 时间 |
|---|---|---|---|
| 0 | 7/4(土) 11:00 | 4 | 7/5(日) 11:00 |
| 1 | 7/4(土) 13:30 | 5 | 7/5(日) 13:30 |
| 2 | 7/4(土) 16:00 | 6 | 7/5(日) 16:00 |
| 3 | 7/4(土) 18:30 | 7 | 7/5(日) 18:30 |

截图里名额为 **300 / 100 / 100 / 100**（第一档 11:00 通常 300，其余 100）。
**注意：名额与时段是单场单店的数字，每场会变，不要沿用上次。**

---

## 4. 完整请求链路（6 步 API）

```
GET  /login.html                       ← 先拿 session cookie（否则报"需开启cookie"）
POST /top_login.html                   ← 登录（LOGINID / PASSWORD / request=logon）
GET  /category/EL/{item_cd}.html       ← 票详情页：拿 token + 时段属性下拉
POST /cart_index.html?request=insert   ← 加购物车（命门，抢名额就在这步）
GET  /cart_index.html                  ← 看购物车
POST /cart_seisan.html                 ← 进结算（预填已存收件信息）
POST /cart_confirm.html                ← 确认（request=confirm + token）
POST /cart_pre.html                    ← 预处理（request=cart_order_pre，0円无支付）
POST /cart_complete.html               ← 下单完成 → 拿注文番号 EC-xxxx
```

完整逐参数说明见 [CHECKOUT_FLOW.md](../CHECKOUT_FLOW.md)。

---

## 5. 关键字段

| 字段 | 示例 | 含义 |
|---|---|---|
| `item_cd` | `ECCL00000043_20260704_05_020` | 商品编码（含日期），详情页拿 |
| `PRIORITY_ITEMPROPERTY_CD_MATRIX_0` | `113020_0705_1300_1` | **店铺+日期+时段属性码**（下拉 value），抢哪个时段靠它 |
| `CART_AMOUNT_0` | `1` | 数量 |
| `token` | `d9489d0b2513…` | session 级别、同 session 共享；**疑似有独立 TTL（约 30~60s）**，空等久了会失效 |
| `request` | `insert` / `confirm` / `cart_order_pre` | 各步动作标识 |

---

## 6. Session & Cookie

| Cookie | 用途 |
|---|---|
| `JSESSIONID` | 核心 session |
| `framework.security_id` | CSRF 保护 |
| `sc_2844_UW` | 登录态标记（登录后新增） |
| `esi_2844_UW` | Ebisu 框架 session |
| `AWSALB` / `AWSALBCORS` | ALB 负载均衡粘性 |

- Session 过期 → 302 跳 `/login.html`
- token 生命周期 = session 生命周期（重登会变）

---

## 7. 状态判断信号（逆向出来的）

| 判断 | 信号 |
|---|---|
| 登录成功 | HTML 含 `ログアウト` **且** 最终 URL 不是 `/login.html` |
| 加购成功 | `カートに追加されました` |
| 下单成功 | `ご注文完了` / `ご注文番号：EC-\d+` |
| 抽选结果 | `member_history.html` 里 `span.block-mypage-history-block-status-icon` 文字：`抽選前`/`抽選中`=未开奖，`当選`=中，`落選`/`抽選対象外`=落选 |

---

## 8. ⚠️ 已知坑（踩过的）

1. **`#error` 容器双用**：Ebisu 把**成功消息**（カートに追加されました）也渲染在 `#error` div 里——按"有 #error 就是失败"会把成功当失败。
2. **结果文案子串陷阱**：落选整句「ご当選されませんでした」里**包含「当選」**，粗暴子串匹配会把落选判成中签。必须先判更具体的落选串。
3. **token 可能过期**：cookie 还活着但 token 失效 → POST 报「画面操作の誤り」。开抢前要刷新 token。
4. **`request=insert` 位置**：AJAX 模式放 query（`?request=insert` + `response_type=json`）；表单模式放 body。

---

## 9. 限流 & 反爬行为（实测，Tokyo VPS 裸 IP）

| 项 | 实测结论 |
|---|---|
| **503** | 纯限流非封禁；3~6s 延迟 + 重试即过 |
| **裸 IP** | Vultr Tokyo 裸 IP，3 账号零 503 / 零 403 |
| **验证码** | 未观察到 captcha |
| **冷启动就绪** | 登录+找票+解析表单 p50≈2.9s / max≈5.1s（超过 1~2s 抢票窗口 → 必须预热） |
| **5xx/403/429** | 持续 403 = IP 被限，应停手（脚本侧靠熔断器拦） |
| **SSL** | 本地调试需 `verify=False` 绕本地代理 MITM；VPS 直连应开回验证 |

---

## 10. 结果查询

- 登录后访问 `/member_history.html`（購入履歴）即可看每个注文番号的状态，**不用邮件**（落選通常不发邮件）。
- 订单超过约 20 条可能分页（待实测确认，见 [ONLINE_TEST_CHECKLIST.md](ONLINE_TEST_CHECKLIST.md)）。
- 注文番号格式：`EC-` + 数字。
