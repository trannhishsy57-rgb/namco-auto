# 联网实测清单（评审里需要"看真实 HTML / 真实响应"才能改的项）

> 这些问题改错会比不改更糟，所以**先不动代码**，等下次开 warmup / racetest 实测时，
> 按本清单抓取证据 → 确认 → 再改。每项给出：怎么复现、抓什么、判断标准、改法。
> 抓页面用 `racetest` 模式（只读不提交，不消耗额度）最安全。

---

## 3.2 token 提取可能选错（P1，直接影响成功率）

**现状**：`namco_prod.py` 在 confirm/complete 阶段用「第一个非空 `input[name="token"]`」。
Ebisu 同一文档里可能有多个同名 token（CSRF / cart / session），选错会导致
`cart_complete` 返回「画面操作の誤り」或 token 失效。

**抓什么**（开奖前用真实账号走一遍 checkout，或 racetest 时 dump confirm 页）：
- [ ] 保存 confirm 页 HTML（`step_confirm` 返回的那份）
- [ ] 数一下里面有几个 `<input name="token">`，各自的 `value` 和所在 `<form action=...>`
- [ ] 记录最终成功下单时实际提交的 token 值（对比 cart_pre 响应里的 token）

**判断标准**：
- 若全文只有 1 个 `token` → 当前代码没问题，标记此项关闭。
- 若有多个，且正确的那个在某个 `form[action*="complete"]` 里 → 需要改。

**改法**（确认后）：
```python
# 从 confirm 表单内定位，而不是全文第一个
el = soup.select_one('form[action*="complete"] input[name="token"]') \
     or soup.select_one('form[action*="confirm"] input[name="token"]')
token = el.get("value", "") if el else ""
```

---

## 3.5 find_confirm_form 启发式可能选错表单（P1）

**现状**：`find_confirm_form` 先找 action 含 `confirm`/`seisan` 的表单，找不到就回退到
「input 最多的表单」。若页面有搜索框/会员登录框/newsletter 框 input 很多，会误选，
把搜索词当订单确认提交。

**抓什么**：
- [ ] 保存 seisan/confirm 页 HTML
- [ ] 列出所有 `<form>` 的 `action` 和各自 input 数量
- [ ] 确认真正的确认表单 action 长什么样（是否含 confirm/seisan，还是 query 形式如 `?step=confirm`）

**判断标准**：若真正的确认表单 action 不含 confirm/seisan 关键词 → 当前启发式有风险。

**改法**（确认后，收紧启发式）：
```python
# 回退条件改为：必须含 token 或 request=confirm 的隐藏域才算确认表单
for form in forms:
    if form.select_one('input[name="token"]') or \
       form.select_one('input[name="request"][value="confirm"]'):
        return form
```

---

## 4.9 namco_result.py 订单分页（P2，仅影响历史订单多的账号）

**现状**：`parse_history` 只解析 `member_history.html` 第一页。若账号历史订单 > 20 条
（OP-15、其他活动…），最新的 OP-16 可能在第 2 页被漏掉，结果查询会把它当「无订单」。

**抓什么**（开奖后跑 result 时，挑一个老账号）：
- [ ] 保存 `member_history.html`，确认是否真有分页器（pager / 「次へ」/ `?page=2`）
- [ ] 确认单页最多显示几条、超过会不会分页（可能只是渲染上限，不一定真分页）

**判断标准**：
- 若不分页（或测试账号订单都 < 20）→ 标记关闭。
- 若真分页 → 需要翻页。

**改法**（确认后）：在 `check_account` 里循环 `?page=N` 直到没有新订单为止，
合并所有页的 `parse_history` 结果。

---

## 5.4 启用 HTTP/2（P3 性能，需确认站点支持）

**现状**：`ManagedSession.__aenter__` 用默认 HTTP/1.1。`requirements.txt` 已带
`httpx[http2]`（h2 包），改一行即可开 HTTP/2，多请求复用一个 TLS 连接省握手。

**抓什么**：
- [ ] 确认 `parks2.bandainamco-am.co.jp` 是否支持 h2（curl -I --http2 或浏览器 Network 面板看 Protocol 列）

**判断标准**：站点支持 h2 → 开；不支持会回落 h1.1，开了也无害但没收益。

**改法**（确认后）：
```python
kw["http2"] = True   # ManagedSession.__aenter__ 里
```
开后建议跑一次 `racetest` 对比 `就绪总耗时` 是否下降，再决定保留。

---

## 实测时的通用抓页面方法

最省事：临时在 `prepare_cart_form` / `step_confirm` 等函数里加一行落盘（实测完删掉）：
```python
open(f"dump_{step}_{int(time.time()*1000)}.html","w",encoding="utf-8").write(resp.text)
```
或直接用 `racetest` 跑，它已经会走到 login→find→prepare，配合临时 dump 就能拿到票面/表单页。
**注意**：confirm/complete 页只有真正 checkout 才会出现，racetest（只读）到不了，
需要用一个可消耗额度的测试账号实跑一次 checkout 模式。
