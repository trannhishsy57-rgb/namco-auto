# 通用多步骤 Web 自动化工程架构

> 纯技术知识梳理，适用于自动化测试、内部系统集成、数据同步等合法多步骤 Web 交互场景。

---

## 模块总览

```
┌─────────────┐     ┌─────────────┐     ┌──────────────┐
│  Scheduler  │────▶│ ManagedSession │──▶│  FormParser   │
│  (并发调度)  │     │  (HTTP会话)   │    │  (表单解析)    │
└──────┬──────┘     └──────┬──────┘     └──────────────┘
       │                   │
       │            ┌──────┴──────┐
       │            │ WebFlowFSM  │
       │            │ (状态机)     │
       │            └──────┬──────┘
       │                   │
┌──────┴──────┐     ┌──────┴──────┐     ┌──────────────┐
│  ProxyPool  │     │   Retry     │     │   TaskDB     │
│  (代理池)    │     │ (重试策略)   │     │  (持久化)     │
└─────────────┘     └─────────────┘     └──────────────┘
```

**模块间关系：**
- **ManagedSession** — 单个 HTTP 会话的生命周期
- **FormParser** — 页面解析，提取表单全部字段
- **WebFlowFSM** — 保证步骤流转正确性（非法跳转抛异常）
- **stamina/tenacity** — 瞬态故障重试
- **Scheduler** — 并发控制（信号量 + 随机延迟）
- **ProxyPool** — 出口 IP 管理（健康检查 + LRU 选取）
- **TaskDB** — 持久化和断点恢复

---

## 一、Session 状态机 (FSM)

把多步骤 Web 交互抽象成有限状态自动机，每个状态代表流程中的一个阶段，非法跳转直接抛异常。

```python
from statemachine import StateMachine, State

class WebFlowFSM(StateMachine):
    """通用多步骤 Web 交互状态机"""

    # 定义状态
    init = State(initial=True)
    authenticated = State()
    page_loaded = State()
    form_filled = State()
    submitted = State()
    completed = State(final=True)
    expired = State()
    failed = State(final=True)

    # 定义合法转换
    login = init.to(authenticated)
    load_page = authenticated.to(page_loaded)
    fill_form = page_loaded.to(form_filled)
    submit = form_filled.to(submitted)
    complete = submitted.to(completed)

    # 异常路径
    expire = (
        authenticated.to(expired)
        | page_loaded.to(expired)
        | form_filled.to(expired)
    )
    relogin = expired.to(authenticated)
    fail = (
        init.to(failed)
        | authenticated.to(failed)
        | submitted.to(failed)
    )

    # 钩子：每次转换时自动触发
    def on_enter_expired(self):
        print("Session 过期，需要重新认证")

    def on_enter_failed(self):
        print("流程失败，记录日志")
```

**为什么用状态机而不是 if-else：** 步骤多、异常路径多时，状态机强制保证流程正确性——非法跳转直接抛异常，避免在错误状态下发请求。

**与当前 v3 脚本的映射：**

| FSM 状态 | v3 脚本对应步骤 | 函数 |
|----------|----------------|------|
| `init` | 脚本启动 | `main()` |
| `authenticated` | Step 1 登录成功 | `login()` |
| `page_loaded` | Step 2 找到票 + Step 3 解析表单 | `find_target_ticket()` + `add_to_cart()` |
| `form_filled` | Step 4 结算页拿到预填字段 | `checkout()` |
| `submitted` | Step 5 确认订单 | `confirm_order()` |
| `completed` | Step 6 下单完成拿到注文番号 | `complete_order()` |
| `expired` | 302 跳转到 login.html | 目前未处理，升级时加 |
| `failed` | 任意步骤返回错误 | 各函数的 `[ERR]` 分支 |

---

## 二、通用表单解析器

传统 Java Web 框架（Struts、Spring MVC、Ebisu 等）在表单里塞大量隐藏字段做校验。通用处理方式：先提取全部字段，再覆盖需要修改的。

```python
from bs4 import BeautifulSoup
from urllib.parse import urljoin

class FormParser:
    """从 HTML 响应中自动提取表单的全部字段"""

    @staticmethod
    def extract_fields(html: str, form_selector: str = "form") -> dict:
        soup = BeautifulSoup(html, "html.parser")
        form = soup.select_one(form_selector)
        if not form:
            return {}

        fields = {}

        for inp in form.find_all("input"):
            name = inp.get("name")
            if not name:
                continue
            input_type = inp.get("type", "text").lower()

            if input_type == "checkbox":
                if inp.has_attr("checked"):
                    fields[name] = inp.get("value", "on")
            elif input_type == "radio":
                if inp.has_attr("checked"):
                    fields[name] = inp.get("value", "")
            else:
                fields[name] = inp.get("value", "")

        for select in form.find_all("select"):
            name = select.get("name")
            if not name:
                continue
            selected = select.find("option", selected=True)
            if not selected:
                options = select.find_all("option")
                selected = options[0] if options else None
            fields[name] = selected.get("value", "") if selected else ""

        for ta in form.find_all("textarea"):
            name = ta.get("name")
            if name:
                fields[name] = ta.get_text()

        return fields

    @staticmethod
    def extract_action(html: str, base_url: str, form_selector: str = "form") -> str:
        soup = BeautifulSoup(html, "html.parser")
        form = soup.select_one(form_selector)
        if not form:
            return ""
        return urljoin(base_url, form.get("action", ""))

    @staticmethod
    def extract_select_options(html: str, select_name: str) -> list[dict]:
        """提取下拉框的所有选项（用于枚举可选值）"""
        soup = BeautifulSoup(html, "html.parser")
        select = soup.find("select", {"name": select_name})
        if not select:
            return []
        return [
            {"value": opt.get("value", ""), "text": opt.get_text(strip=True)}
            for opt in select.find_all("option")
            if opt.get("value")
        ]

    @staticmethod
    def merge(base_fields: dict, overrides: dict) -> dict:
        """基础字段 + 业务覆盖 = 最终提交数据"""
        merged = {**base_fields}
        merged.update({k: v for k, v in overrides.items() if v is not None})
        return merged
```

**与当前 v3 的关系：** v3 脚本里的 `parse_ticket_form()`、`confirm_order()` 中手写的 input/select 遍历逻辑，就是 `FormParser.extract_fields()` 的内联版本。升级时替换为这个类可以大幅减少重复代码，同时 `merge()` 模式避免手动维护 `jp.co.interfactory.framework.trim.*` 等框架隐藏字段。

---

## 三、Session 封装 (httpx AsyncClient)

每个独立任务需要自己的 HTTP client 实例，互不共享 cookie。

```python
from dataclasses import dataclass, field
import httpx

@dataclass
class ManagedSession:
    session_id: str
    proxy: str = None
    client: httpx.AsyncClient = field(default=None, repr=False)
    metadata: dict = field(default_factory=dict)

    async def start(self):
        transport = None
        if self.proxy:
            transport = httpx.AsyncHTTPTransport(proxy=self.proxy)

        self.client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,  # 手动检测 302 → login
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=5.0),
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            },
            limits=httpx.Limits(
                max_connections=10,
                max_keepalive_connections=5,
            ),
        )

    async def get(self, url: str, **kwargs) -> httpx.Response:
        return await self.client.get(url, **kwargs)

    async def post(self, url: str, **kwargs) -> httpx.Response:
        return await self.client.post(url, **kwargs)

    def is_redirect_to_login(self, resp: httpx.Response) -> bool:
        """通用的 session 过期检测"""
        if resp.status_code in (301, 302, 303, 307):
            location = resp.headers.get("location", "")
            if "login" in location.lower():
                return True
        return False

    def get_cookies(self) -> dict:
        return dict(self.client.cookies)

    async def close(self):
        if self.client:
            await self.client.aclose()
```

**关键设计：** `follow_redirects=False` 让你可以手动检测 302 跳转到登录页（session 过期），而不是 httpx 自动跟随后你不知道 session 已失效。

**与当前 v3 的关系：** v3 的 `LoggedSession` 用的是同步 `requests.Session`，升级到多任务并发时替换为这个异步版本。

---

## 四、重试策略 (stamina / tenacity)

### stamina — 简洁，适合"加上去就行"

```python
import stamina
import httpx

@stamina.retry(
    on=httpx.HTTPStatusError,
    attempts=3,
    wait_initial=2.0,
    wait_max=30.0,
    wait_jitter=5.0,
)
async def fetch_page(client: httpx.AsyncClient, url: str) -> httpx.Response:
    resp = await client.get(url)
    if resp.status_code == 503:
        raise httpx.HTTPStatusError("Rate limited", request=resp.request, response=resp)
    return resp

# 上下文管理器模式（更灵活）
async def fetch_with_context(client, url):
    async for attempt in stamina.retry_context(on=httpx.HTTPError, attempts=3):
        with attempt:
            resp = await client.get(url)
            if resp.status_code == 503:
                raise httpx.HTTPStatusError("503", request=resp.request, response=resp)
            return resp
```

### tenacity — 更强大，按异常类型分策略

```python
from tenacity import (
    retry, stop_after_attempt, wait_exponential,
    wait_random, retry_if_exception_type, before_sleep_log
)
import logging

logger = logging.getLogger(__name__)

@retry(
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TimeoutException)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=3, max=40) + wait_random(0, 5),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
async def resilient_post(client: httpx.AsyncClient, url: str, data: dict):
    resp = await client.post(url, data=data)
    if resp.status_code == 503:
        raise httpx.HTTPStatusError("503", request=resp.request, response=resp)
    resp.raise_for_status()
    return resp
```

`wait_exponential + wait_random` = 指数退避 + 随机抖动，避免 thundering herd。

**与当前 v3 的关系：** v3 的 `post_retry()` / `get_retry()` 是手写的线性重试，升级时替换为 tenacity 装饰器更优雅且支持指数退避。

---

## 五、并发调度器

```python
import asyncio
import random
import logging
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

@dataclass
class TaskResult:
    task_id: str
    success: bool
    final_state: str
    duration_ms: int = 0
    error: str = ""

class Scheduler:
    def __init__(
        self,
        max_concurrent: int = 10,
        delay_range: tuple[float, float] = (2.0, 5.0),
    ):
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.delay_range = delay_range
        self.results: list[TaskResult] = []
        self._lock = asyncio.Lock()

    async def run_one(self, task_id: str, steps: list) -> TaskResult:
        """执行一个多步骤任务，steps: list of async callables"""
        async with self.semaphore:
            start = datetime.now()
            last_step = "INIT"
            try:
                for i, step_fn in enumerate(steps):
                    delay = random.uniform(*self.delay_range)
                    await asyncio.sleep(delay)

                    result = await step_fn()
                    last_step = f"STEP_{i}_{result}" if isinstance(result, str) else f"STEP_{i}"
                    logger.info(f"[{task_id}] 完成 {last_step}")

                elapsed = int((datetime.now() - start).total_seconds() * 1000)
                task_result = TaskResult(task_id, True, last_step, elapsed)

            except Exception as e:
                elapsed = int((datetime.now() - start).total_seconds() * 1000)
                task_result = TaskResult(task_id, False, last_step, elapsed, str(e))
                logger.error(f"[{task_id}] 失败于 {last_step}: {e}")

            async with self._lock:
                self.results.append(task_result)
            return task_result

    async def run_all(self, task_map: dict[str, list]) -> list[TaskResult]:
        """并发执行所有任务，task_map: {task_id: [step_fn, ...]}"""
        coros = [
            self.run_one(task_id, steps)
            for task_id, steps in task_map.items()
        ]
        await asyncio.gather(*coros, return_exceptions=True)
        return self.results

    def summary(self) -> dict:
        total = len(self.results)
        success = sum(1 for r in self.results if r.success)
        avg_time = (
            sum(r.duration_ms for r in self.results) / total
            if total else 0
        )
        return {
            "total": total,
            "success": success,
            "failed": total - success,
            "success_rate": f"{success / total * 100:.1f}%" if total else "N/A",
            "avg_duration_ms": int(avg_time),
        }
```

---

## 六、代理池管理

```python
from dataclasses import dataclass
from time import time

@dataclass
class Proxy:
    url: str
    fail_count: int = 0
    success_count: int = 0
    last_used: float = 0.0
    healthy: bool = True

class ProxyPool:
    def __init__(self, proxy_urls: list[str], max_fails: int = 3):
        self.proxies = [Proxy(url=u) for u in proxy_urls]
        self.max_fails = max_fails

    def acquire(self, exclude: str = None) -> Proxy | None:
        """获取一个健康代理（最久未使用优先 = LRU）"""
        candidates = [
            p for p in self.proxies
            if p.healthy and p.url != exclude
        ]
        if not candidates:
            self._reset_all()
            candidates = self.proxies

        candidates.sort(key=lambda p: p.last_used)
        chosen = candidates[0]
        chosen.last_used = time()
        return chosen

    def report_success(self, url: str):
        p = self._find(url)
        if p:
            p.fail_count = 0
            p.success_count += 1
            p.healthy = True

    def report_failure(self, url: str):
        p = self._find(url)
        if p:
            p.fail_count += 1
            if p.fail_count >= self.max_fails:
                p.healthy = False

    def stats(self) -> dict:
        healthy = sum(1 for p in self.proxies if p.healthy)
        return {
            "total": len(self.proxies),
            "healthy": healthy,
            "unhealthy": len(self.proxies) - healthy,
        }

    def _find(self, url: str) -> Proxy | None:
        return next((p for p in self.proxies if p.url == url), None)

    def _reset_all(self):
        for p in self.proxies:
            p.healthy = True
            p.fail_count = 0
```

---

## 七、持久化存储 (aiosqlite)

```python
import aiosqlite

class TaskDB:
    def __init__(self, db_path: str = "tasks.db"):
        self.db_path = db_path

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    state TEXT DEFAULT 'INIT',
                    proxy_url TEXT,
                    started_at TEXT,
                    completed_at TEXT,
                    error TEXT,
                    result_data TEXT
                )
            """)
            await db.commit()

    async def upsert(self, task_id: str, **kwargs):
        fields = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values())
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"INSERT INTO tasks (task_id, {', '.join(kwargs.keys())}) "
                f"VALUES (?, {', '.join('?' for _ in kwargs)}) "
                f"ON CONFLICT(task_id) DO UPDATE SET {fields}",
                [task_id] + values + values,
            )
            await db.commit()

    async def get_by_state(self, state: str) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM tasks WHERE state = ?", (state,)
            )
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def summary(self) -> dict:
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT state, COUNT(*) FROM tasks GROUP BY state"
            )
            rows = await cursor.fetchall()
            return {row[0]: row[1] for row in rows}
```

---

## 升级路径：v3 → v4

当前 v3 脚本 (`namco_lottery.py`) 是同步单任务版本。要升级到这套架构的完整形态：

| 改动 | 从 | 到 |
|------|----|----|
| HTTP 客户端 | `requests.Session` (同步) | `httpx.AsyncClient` (异步) |
| 表单解析 | 各函数内联遍历 | `FormParser` 统一类 |
| 流程控制 | `if not login(): return` 链式 | `WebFlowFSM` 状态机 |
| 重试逻辑 | 手写 `for attempt in range()` | `tenacity` 装饰器 |
| 并发 | 无（单线程顺序） | `Scheduler` + `asyncio.Semaphore` |
| 代理 | `verify=False` 绕过 | `ProxyPool` 多出口 |
| 持久化 | JSON 文件 | `TaskDB` (SQLite) |

**升级优先级建议：**
1. `FormParser` — 最低成本，立刻减少重复代码
2. `tenacity` 重试 — 替换手写重试，加指数退避
3. `WebFlowFSM` — 加 session 过期自动重登录
4. `httpx` + `Scheduler` — 需要并发时再改
5. `ProxyPool` + `TaskDB` — 多任务场景才需要
