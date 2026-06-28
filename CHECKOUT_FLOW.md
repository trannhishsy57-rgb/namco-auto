# Namco Parks 下单流程 API 文档

> 基于 HAR 抓包分析，完整还原从登录到下单的全链路请求。  
> Base URL: `https://parks2.bandainamco-am.co.jp`  
> 框架: Ebisu EC (Java Web)，Session 基于 `JSESSIONID` cookie

---

## 0. 登录

```
GET /login.html
```
获取 session cookies (`JSESSIONID`, `framework.security_id` 等) + 表单隐藏字段。

```
POST /top_login.html
Content-Type: application/x-www-form-urlencoded
```

| 参数 | 值 | 说明 |
|------|---|------|
| `request` | `logon` | 固定值 |
| `redirectTo` | *(空)* | 登录后跳转，留空回首页 |
| `LOGINID` | 邮箱 | |
| `PASSWORD` | 密码 | |
| `jp.co.interfactory.framework.trim.LOGINID` | *(空)* | 隐藏字段，照传 |
| `jp.co.interfactory.framework.lower.LOGINID` | *(空)* | 隐藏字段，照传 |

**成功判断:** 响应 HTML 包含 `ログアウト`，不包含 `ログイン`

**Session Cookies（登录后）:**

| Cookie | 用途 |
|--------|------|
| `JSESSIONID` | 核心 session ID |
| `framework.security_id` | CSRF 保护 |
| `sc_2844_UW` | 登录态标记（登录后新增） |
| `AWSALB` / `AWSALBCORS` | AWS ALB 负载均衡 |
| `esi_2844_UW` | Ebisu 框架 session |

---

## 1. 加入购物车（AJAX）

```
POST /cart_index.html?request=insert
Content-Type: application/x-www-form-urlencoded
```

| 参数 | 值 | 说明 |
|------|---|------|
| `response_type` | `json` | AJAX 模式，返回 JSON |
| `item_cd` | `ECCL00000043_20260704_05_020` | 商品编码，从票详情页获取 |
| `PRIORITY_ITEMPROPERTY_CD_MATRIX_0` | `113020_0705_1300_1` | 抽選属性（店铺/时间段），从详情页 select 获取 |
| `CART_AMOUNT_0` | `1` | 数量 |
| `ITEM_SERIAL_CODE_HIDDEN` | *(空)* | |

**响应:** JSON，成功时返回购物车状态

> **注意:** URL 中 `?request=insert` 是必须的 query param，不是 POST body。  
> 如果用表单 POST（非 AJAX），则把 `request=insert` 放在 POST body，不带 `response_type=json`。

---

## 2. 查看购物车

```
GET /cart_index.html
```

**用途:** 渲染购物车页面，确认商品已加入。页面包含"購入手続きに進む"按钮。

**页面内表单隐藏字段（下一步需要）:**

| 字段 | 示例 |
|------|------|
| `request` | *(空，JS 设为 delete/seisan)* |
| `item_cd` | *(空)* |
| `seisan_page_id` | *(空)* |
| `key` | `ECCL00000043_20260704_05_020-113020_0705_1300_1` |
| `index` | `0` |
| `CART_INDEX_REFERER` | 来源页 URL (URL encoded) |
| `CART_AMOUNT_0` | `1` |

---

## 3. 进入结算页

```
POST /cart_seisan.html
Content-Type: application/x-www-form-urlencoded
```

| 参数 | 值 | 说明 |
|------|---|------|
| `request` | *(空)* | |
| `item_cd` | *(空)* | |
| `seisan_page_id` | *(空)* | |
| `key` | *(空)* | |
| `index` | *(空)* | |
| `CART_INDEX_REFERER` | 商品详情页 URL (URL encoded) | |
| `CART_AMOUNT_0` | `1` | |

**响应:** 结算页（ご注文情報入力），自动填充已保存的收件人信息。

**页面加载时触发的 AJAX:**

```
POST /ajax_send_hope_date.html?t={timestamp}
```

| 参数 | 值 | 说明 |
|------|---|------|
| `CURRENT_SEND_HOPE_DATE` | `undefined` | |
| `PREFIX` | `SEND_ITEM_AMOUNT_` | |
| `ITEM_AMOUNT` | *(空)* | |
| `ACTION_NAME` | `SEND_HOPE_CALENDAR` | |
| `SEND_ADDR1` | 都道府県 (URL encoded) | |
| `SEND_TO_ANOTHER_ADDRESS_CHECK` | *(空)* | |
| `ZIP` | 邮编（无横线） | |

> 这个 AJAX 用于获取配送日期选项，抽選票场景下可能非必须。

---

## 4. 确认订单信息

```
POST /cart_confirm.html
Content-Type: application/x-www-form-urlencoded
```

| 参数 | 值 | 说明 |
|------|---|------|
| `request` | `confirm` | 固定值 |
| `token` | `d9489d0b2513854b9596af853a98` | 从结算页隐藏字段获取，session 级别 |
| `mode` | `0` | |
| `key` | *(空)* | |
| `PC_MAIL_OLD` | 邮箱 | |
| `MOBILE_MAIL_OLD` | *(空)* | |
| `L_NAME` | 姓 (URL encoded) | |
| `F_NAME` | 名 (URL encoded) | |
| `L_KANA` | 姓カナ (URL encoded) | |
| `F_KANA` | 名カナ (URL encoded) | |
| `PC_MAIL` | 邮箱 | |
| `ZIP` | 邮编 | |
| `ADDR1` | 都道府県 (URL encoded) | |
| `ADDR2` | 市区町村+番地 (URL encoded) | |
| `ADDR3` | 建物名等 (URL encoded) | |
| `TEL` | 电话号码 | |
| `ORDER_H.FREE_ITEM12` | 番地部分 (URL encoded) | |
| `ORDER_H.FREE_ITEM22` | `1` | 规约同意 checkbox |
| `TOUROKU_CHECK` | `1` | |
| `BIRTH_YEAR` | *(空)* | |
| `BIRTH_MONTH` | *(空)* | |
| `BIRTH_DAY` | *(空)* | |
| `IS_REGIST` | *(空)* | |
| `QUICK_ORDER` | *(空)* | |
| `jp.co.interfactory.framework.trim.*` | *(空)* | 每个文本字段对应的 trim 隐藏字段 |
| `jp.co.interfactory.framework.unchecked.*` | *(空)* | checkbox 对应的 unchecked 隐藏字段 |

**响应:** 订单确认页（ご注文情報確認），展示订单详情。

> 如果账户已保存地址，Step 3 返回的结算页会预填所有字段。  
> 脚本可以先 GET 结算页，解析所有 input 的 value，直接回传。

---

## 5. 预处理订单

```
POST /cart_pre.html
Content-Type: application/x-www-form-urlencoded
```

| 参数 | 值 | 说明 |
|------|---|------|
| `request` | `cart_order_pre` | 固定值 |
| `token` | *(同上)* | |
| `mode` | `0` | |

**响应:** 服务端预处理（库存检查/支付准备等），0 円商品无支付。

---

## 6. 最终下单

```
POST /cart_complete.html
Content-Type: application/x-www-form-urlencoded
```

| 参数 | 值 | 说明 |
|------|---|------|
| `token` | *(同上)* | |

**响应:** 注文完了页面，包含注文番号。

```html
<h1 class="page-title">ご注文完了</h1>
【ご注文番号：EC-2105077634】
```

**成功判断:** 响应包含 `ご注文完了` 或 `ご注文番号`

---

## 完整请求链路图

```
GET  /login.html                    ← 拿 session cookies
POST /top_login.html                ← 登录
  │
GET  /category/EL/{item_cd}.html    ← 获取商品详情 + token + 属性选项
  │
POST /cart_index.html?request=insert ← 加购物车 (AJAX)
GET  /cart_index.html               ← 查看购物车
  │
POST /cart_seisan.html              ← 进入结算页（填写信息）
  │
POST /cart_confirm.html             ← 确认订单（request=confirm）
  │
POST /cart_pre.html                 ← 预处理（request=cart_order_pre）
  │
POST /cart_complete.html            ← 下单完成 → 拿到注文番号
```

---

## Token 机制

- `token` 从商品详情页或购物车页面的隐藏字段获取
- Session 级别，同一 session 内所有商品共享同一 token
- Session 过期或重新登录后 token 会变
- 不是每次请求生成新 token，生命周期 = session 生命周期

---

## 限流 & 注意事项

| 项目 | 说明 |
|------|------|
| **503** | 访问集中时返回，纯限流，非 ban |
| **建议延迟** | 请求间 3-6s 随机延迟 |
| **503 重试** | 等待 10-30s 后重试，最多 3 次 |
| **Session 过期** | 返回 302 → login.html |
| **一人一店** | お一人様1回、1店舗までお申込み可能 |
| **多店申込** | 复数店舗のご応募はできない（同一账号不能多店） |
| **SSL/代理** | Python requests 需 `verify=False` + `trust_env=False` 绕过本地代理 |

---

## 文件说明

| 文件 | 用途 |
|------|------|
| `namco_lottery.py` | 自动化脚本（登录 + 加购） |
| `inspect_form.py` | 表单结构分析工具 |
| `parse_har.py` | HAR 文件解析工具 |
| `namco_har_log.json` | 脚本生成的请求日志 |
| `results_summary.json` | 批量申込结果 |
| `debug_*.html` | 各步骤响应页面 |
| `submit_response_*.html` | 提交后响应页面 |
