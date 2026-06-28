"""
Namco Parks OP-16 - Production Multi-Account Automation
Architecture: Async + ProxyPool + SQLite TaskQueue + FSM + CircuitBreaker + Metrics

Run with: py -3.12 namco_prod.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

import tomllib

import aiosqlite
import httpx
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ─────────────────────────────────────────────────────────────────────────────
# Module 12: AppConfig
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class AccountConfig:
    email: str
    password: str
    target_store: str = ""
    proxy: str = ""

@dataclass
class AppConfig:
    accounts: List[AccountConfig]
    max_concurrent: int = 3
    delay_min: float = 3.0
    delay_max: float = 7.0
    retry_max_attempts: int = 3
    retry_initial_wait: float = 5.0
    retry_max_wait: float = 40.0
    retry_jitter: float = 5.0
    proxy_pool: List[str] = field(default_factory=list)
    proxy_max_fails: int = 3
    requests_per_second: float = 0.5
    rate_burst: int = 2
    circuit_failure_threshold: int = 5
    circuit_recovery_timeout: float = 120.0
    event_keyword: str = "OP-16"
    lottery_keyword: str = "抽選"
    db_path: str = "namco_tasks.db"
    log_level: str = "INFO"
    mode: str = "checkout"
    results_file: str = "results.jsonl"

    @classmethod
    def load(cls, path: str = "config.toml") -> "AppConfig":
        with open(path, "rb") as f:
            raw = tomllib.load(f)

        accounts = [AccountConfig(**a) for a in raw.get("accounts", [])]
        sched = raw.get("scheduler", {})
        retry_cfg = raw.get("retry", {})
        proxy_cfg = raw.get("proxy", {})
        rl = raw.get("rate_limit", {})
        cb = raw.get("circuit_breaker", {})
        tgt = raw.get("target", {})

        return cls(
            accounts=accounts,
            max_concurrent=sched.get("max_concurrent", 3),
            delay_min=sched.get("delay_min", 3.0),
            delay_max=sched.get("delay_max", 7.0),
            retry_max_attempts=retry_cfg.get("max_attempts", 3),
            retry_initial_wait=retry_cfg.get("initial_wait", 5.0),
            retry_max_wait=retry_cfg.get("max_wait", 40.0),
            retry_jitter=retry_cfg.get("jitter", 5.0),
            proxy_pool=proxy_cfg.get("pool", []) or [],
            proxy_max_fails=proxy_cfg.get("max_fails", 3),
            requests_per_second=rl.get("requests_per_second", 0.5),
            rate_burst=rl.get("burst", 2),
            circuit_failure_threshold=cb.get("failure_threshold", 5),
            circuit_recovery_timeout=cb.get("recovery_timeout", 120.0),
            event_keyword=tgt.get("event_keyword", "OP-16"),
            lottery_keyword=tgt.get("lottery_keyword", "抽選"),
            db_path=raw.get("db_path", "namco_tasks.db"),
            log_level=raw.get("log_level", "INFO"),
            mode=raw.get("mode", "checkout"),
            results_file=raw.get("results_file", "results.jsonl"),
        )

# ─────────────────────────────────────────────────────────────────────────────
# Module 13: Structured Logging
# ─────────────────────────────────────────────────────────────────────────────

class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, ensure_ascii=False)

def setup_logging(level: str = "INFO") -> logging.Logger:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(name)-30s %(message)s",
        datefmt="%H:%M:%S",
    ))
    root.addHandler(ch)

    fh = logging.FileHandler("namco_prod.log", encoding="utf-8")
    fh.setFormatter(JsonFormatter())
    root.addHandler(fh)

    return logging.getLogger("namco")

# ─────────────────────────────────────────────────────────────────────────────
# Module 14: Metrics
# ─────────────────────────────────────────────────────────────────────────────

class Metrics:
    def __init__(self):
        self._counters: Dict[str, int] = {}
        self._timings: Dict[str, List[float]] = {}

    def inc(self, name: str, n: int = 1):
        self._counters[name] = self._counters.get(name, 0) + n

    def record(self, name: str, duration_ms: float):
        self._timings.setdefault(name, []).append(duration_ms)

    def _pct(self, name: str, p: int) -> Optional[float]:
        vals = sorted(self._timings.get(name, []))
        if not vals:
            return None
        return vals[max(0, int(len(vals) * p / 100) - 1)]

    def report(self, log: logging.Logger):
        for name, count in sorted(self._counters.items()):
            log.info(f"metric.counter {name}={count}")
        for name in sorted(self._timings):
            p50, p95, p99 = self._pct(name, 50), self._pct(name, 95), self._pct(name, 99)
            log.info(f"metric.timer {name} p50={p50:.0f}ms p95={p95:.0f}ms p99={p99:.0f}ms")

METRICS = Metrics()

# ─────────────────────────────────────────────────────────────────────────────
# Module 6: ProxyPool (LRU + health)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _ProxyEntry:
    url: str
    fail_count: int = 0
    banned: bool = False
    last_used: float = 0.0

class ProxyPool:
    def __init__(self, proxies: List[str], max_fails: int = 3):
        self._entries: deque[_ProxyEntry] = deque(
            _ProxyEntry(url=p) for p in proxies
        )
        self._lock = asyncio.Lock()
        self.max_fails = max_fails
        self._log = logging.getLogger("namco.proxy")

    def is_empty(self) -> bool:
        return len(self._entries) == 0

    async def acquire(self) -> Optional[str]:
        if self.is_empty():
            return None
        async with self._lock:
            for _ in range(len(self._entries)):
                entry = self._entries[0]
                self._entries.rotate(-1)
                if not entry.banned:
                    entry.last_used = time.time()
                    return entry.url
            # All banned: reset and hand out anyway
            for e in self._entries:
                e.banned = False
                e.fail_count = 0
            entry = self._entries[0]
            self._entries.rotate(-1)
            entry.last_used = time.time()
            self._log.warning("All proxies were banned; resetting pool")
            return entry.url

    async def report_failure(self, url: str):
        async with self._lock:
            for e in self._entries:
                if e.url == url:
                    e.fail_count += 1
                    if e.fail_count >= self.max_fails:
                        e.banned = True
                        self._log.warning(f"Proxy banned ({e.fail_count} fails): {url}")
                    break

    async def report_success(self, url: str):
        async with self._lock:
            for e in self._entries:
                if e.url == url:
                    e.fail_count = 0
                    break

# ─────────────────────────────────────────────────────────────────────────────
# Module 11: RateLimiter (Token Bucket, per session)
# ─────────────────────────────────────────────────────────────────────────────

class TokenBucket:
    def __init__(self, rate: float, burst: int):
        self._rate = rate
        self._burst = float(burst)
        self._tokens = float(burst)
        self._last = time.monotonic()

    async def acquire(self):
        now = time.monotonic()
        elapsed = now - self._last
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
        else:
            wait = (1.0 - self._tokens) / self._rate
            await asyncio.sleep(wait)
            self._tokens = 0.0

# ─────────────────────────────────────────────────────────────────────────────
# Module 10: CircuitBreaker (CLOSED / OPEN / HALF_OPEN)
# ─────────────────────────────────────────────────────────────────────────────

class _CBState(Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"

class CircuitBreaker:
    def __init__(self, failure_threshold: int, recovery_timeout: float):
        self._threshold = failure_threshold
        self._recovery = recovery_timeout
        self._state = _CBState.CLOSED
        self._failures = 0
        self._opened_at: float = 0.0
        self._lock = asyncio.Lock()
        self._log = logging.getLogger("namco.cb")

    async def call(self, coro):
        async with self._lock:
            if self._state == _CBState.OPEN:
                if time.time() - self._opened_at >= self._recovery:
                    self._state = _CBState.HALF_OPEN
                    self._log.info("Circuit HALF_OPEN")
                else:
                    raise RuntimeError(
                        f"Circuit OPEN — retry in {self._recovery - (time.time() - self._opened_at):.0f}s"
                    )
        try:
            result = await coro
        except Exception:
            async with self._lock:
                self._failures += 1
                if self._failures >= self._threshold:
                    self._state = _CBState.OPEN
                    self._opened_at = time.time()
                    self._log.warning(f"Circuit OPEN after {self._failures} failures")
            raise
        else:
            async with self._lock:
                if self._state == _CBState.HALF_OPEN:
                    self._state = _CBState.CLOSED
                    self._failures = 0
                    self._log.info("Circuit CLOSED")
            return result

# ─────────────────────────────────────────────────────────────────────────────
# Module 2: FormParser
# ─────────────────────────────────────────────────────────────────────────────

def _parse_form_element(form) -> Dict[str, str]:
    data: Dict[str, str] = {}
    for inp in form.select("input"):
        name = inp.get("name")
        if not name:
            continue
        t = inp.get("type", "text").lower()
        if t == "checkbox":
            if inp.get("checked") is not None:
                data[name] = inp.get("value", "1")
        elif t == "radio":
            if inp.get("checked") is not None:
                data[name] = inp.get("value", "")
        elif t != "submit":
            data[name] = inp.get("value", "")
    for sel in form.select("select"):
        name = sel.get("name")
        if name:
            opt = sel.select_one("option[selected]") or sel.select_one("option")
            data[name] = opt.get("value", "") if opt else ""
    for ta in form.select("textarea"):
        name = ta.get("name")
        if name:
            data[name] = ta.string or ""
    return data

def find_cart_form(soup: BeautifulSoup) -> Optional[Any]:
    for form in soup.select("form"):
        action = form.get("action", "")
        if "cart" in action.lower():
            return form
    return None

def find_confirm_form(soup: BeautifulSoup) -> Optional[Any]:
    forms = soup.select("form")
    for form in forms:
        action = form.get("action", "")
        if "confirm" in action.lower() or "seisan" in action.lower():
            return form
    if forms:
        return max(forms, key=lambda f: len(f.select("input")))
    return None

def check_error(soup: BeautifulSoup) -> Optional[str]:
    for sel in [
        "#error .form-error-message",
        "#error ul li",
        ".errMsg",
        ".error-message",
        ".form-error",
    ]:
        el = soup.select_one(sel)
        if el:
            t = el.get_text(strip=True)
            if t:
                return t[:400]
    return None

def find_order_number(html: str) -> Optional[str]:
    m = re.search(r"EC-\d+", html)
    return m.group(0) if m else None

# ─────────────────────────────────────────────────────────────────────────────
# Module 3: ManagedSession (httpx AsyncClient)
# ─────────────────────────────────────────────────────────────────────────────

BASE_URL = "https://parks2.bandainamco-am.co.jp"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ja,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}

@dataclass
class SessionSignals:
    """Per-session HTTP health signals — the source of truth for the benchmark.
    Counts raw responses (every retry counted), so 503/redirect/captcha rates
    are measured directly instead of being inferred from downstream booleans."""
    requests: int = 0
    http_503: int = 0          # rate limited
    http_403: int = 0          # IP blocked
    redirect_login: int = 0    # session expired (bounced to login.html)
    captcha: int = 0           # anti-bot triggered


class ManagedSession:
    def __init__(self, proxy: Optional[str], cfg: AppConfig, account_id: str):
        self._proxy = proxy
        self._cfg = cfg
        self._account_id = account_id
        self._log = logging.getLogger(f"namco.sess.{account_id[:20]}")
        self._tb = TokenBucket(cfg.requests_per_second, cfg.rate_burst)
        self.client: Optional[httpx.AsyncClient] = None
        self.signals = SessionSignals()

    async def __aenter__(self) -> "ManagedSession":
        kw: Dict[str, Any] = dict(
            headers=_HEADERS,
            follow_redirects=True,
            verify=False,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        if self._proxy:
            kw["proxy"] = self._proxy
        self.client = httpx.AsyncClient(**kw)
        return self

    async def __aexit__(self, *_):
        if self.client:
            await self.client.aclose()

    async def _delay_polite(self):
        d = random.uniform(self._cfg.delay_min, self._cfg.delay_max)
        self._log.debug(f"polite {d:.1f}s")
        await asyncio.sleep(d)

    def _record_signals(self, path: str, resp: httpx.Response):
        """Inspect a raw response for rate-limit / block / session / captcha signals.
        Called on every attempt (incl. retries) so counts reflect true site behaviour."""
        sg = self.signals
        sg.requests += 1

        if resp.status_code == 503:
            sg.http_503 += 1
        elif resp.status_code == 403:
            sg.http_403 += 1

        # Session expired: we ended up at login.html without having asked for it.
        # follow_redirects=True is kept (login flow depends on it), so a 302→login
        # shows up as the *final* URL being login.html.
        final_url = str(resp.url)
        if "/login.html" in final_url and not path.startswith("/login.html"):
            sg.redirect_login += 1

        # Anti-bot: only scan HTML bodies, cheaply.
        ctype = resp.headers.get("content-type", "")
        if "html" in ctype:
            low = resp.text.lower()
            if "captcha" in low or "recaptcha" in low or "hcaptcha" in low:
                sg.captcha += 1

    async def _do_get(self, path: str, **kw) -> httpx.Response:
        await self._tb.acquire()
        url = f"{BASE_URL}{path}" if path.startswith("/") else path
        t0 = time.time()
        resp = await self.client.get(url, **kw)
        ms = (time.time() - t0) * 1000
        METRICS.record("http.ms", ms)
        self._record_signals(path, resp)
        self._log.info(f"GET {path[:60]} → {resp.status_code} ({ms:.0f}ms)")
        return resp

    async def _do_post(self, path: str, data: Optional[Dict] = None, **kw) -> httpx.Response:
        await self._tb.acquire()
        url = f"{BASE_URL}{path}" if path.startswith("/") else path
        t0 = time.time()
        resp = await self.client.post(url, data=data, **kw)
        ms = (time.time() - t0) * 1000
        METRICS.record("http.ms", ms)
        self._record_signals(path, resp)
        self._log.info(f"POST {path[:60]} → {resp.status_code} ({ms:.0f}ms)")
        return resp

    async def get(self, path: str, **kw) -> httpx.Response:
        cfg = self._cfg
        for attempt in range(cfg.retry_max_attempts):
            try:
                resp = await self._do_get(path, **kw)
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                wait = cfg.retry_initial_wait * (2 ** attempt) + random.uniform(0, cfg.retry_jitter)
                self._log.warning(f"Network error on GET {path}: {e}, retry in {wait:.1f}s")
                METRICS.inc("http.retry.net")
                await asyncio.sleep(min(wait, cfg.retry_max_wait))
                continue
            if resp.status_code != 503:
                return resp
            wait = cfg.retry_initial_wait * (2 ** attempt) + random.uniform(0, cfg.retry_jitter)
            wait = min(wait, cfg.retry_max_wait)
            self._log.warning(f"503 GET {path}, retry in {wait:.1f}s ({attempt+1}/{cfg.retry_max_attempts})")
            METRICS.inc("http.retry.503")
            await asyncio.sleep(wait)
        return resp

    async def post(self, path: str, data: Optional[Dict] = None, **kw) -> httpx.Response:
        cfg = self._cfg
        for attempt in range(cfg.retry_max_attempts):
            try:
                resp = await self._do_post(path, data=data, **kw)
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                wait = cfg.retry_initial_wait * (2 ** attempt) + random.uniform(0, cfg.retry_jitter)
                self._log.warning(f"Network error on POST {path}: {e}, retry in {wait:.1f}s")
                METRICS.inc("http.retry.net")
                await asyncio.sleep(min(wait, cfg.retry_max_wait))
                continue
            if resp.status_code != 503:
                return resp
            wait = cfg.retry_initial_wait * (2 ** attempt) + random.uniform(0, cfg.retry_jitter)
            wait = min(wait, cfg.retry_max_wait)
            self._log.warning(f"503 POST {path}, retry in {wait:.1f}s ({attempt+1}/{cfg.retry_max_attempts})")
            METRICS.inc("http.retry.503")
            await asyncio.sleep(wait)
        return resp

# ─────────────────────────────────────────────────────────────────────────────
# Module 1: FSM
# ─────────────────────────────────────────────────────────────────────────────

class FlowState(Enum):
    INIT       = "INIT"
    LOGIN      = "LOGIN"
    FIND       = "FIND"
    CART       = "CART"
    GET_CART   = "GET_CART"
    CHECKOUT   = "CHECKOUT"
    CONFIRM    = "CONFIRM"
    PRE        = "PRE"
    COMPLETE   = "COMPLETE"
    DONE       = "DONE"
    FAILED     = "FAILED"

_ALLOWED: Dict[FlowState, List[FlowState]] = {
    FlowState.INIT:     [FlowState.LOGIN,    FlowState.FAILED],
    FlowState.LOGIN:    [FlowState.FIND,     FlowState.FAILED],
    FlowState.FIND:     [FlowState.CART,     FlowState.DONE, FlowState.FAILED],
    FlowState.CART:     [FlowState.GET_CART, FlowState.DONE, FlowState.FAILED],
    FlowState.GET_CART: [FlowState.CHECKOUT, FlowState.FAILED],
    FlowState.CHECKOUT: [FlowState.CONFIRM,  FlowState.FAILED],
    FlowState.CONFIRM:  [FlowState.PRE,      FlowState.FAILED],
    FlowState.PRE:      [FlowState.COMPLETE, FlowState.FAILED],
    FlowState.COMPLETE: [FlowState.DONE,     FlowState.FAILED],
    FlowState.DONE:     [],
    FlowState.FAILED:   [],
}

class BookingFSM:
    def __init__(self, label: str):
        self.state = FlowState.INIT
        self._log = logging.getLogger(f"namco.fsm.{label[:20]}")

    def go(self, target: FlowState):
        if target not in _ALLOWED.get(self.state, []):
            raise ValueError(f"Invalid FSM transition {self.state.value}→{target.value}")
        self._log.debug(f"{self.state.value} → {target.value}")
        self.state = target

    def fail(self, reason: str):
        self._log.error(f"FAILED at {self.state.value}: {reason}")
        self.state = FlowState.FAILED

# ─────────────────────────────────────────────────────────────────────────────
# Module 7: TaskQueue (SQLite WAL)
# ─────────────────────────────────────────────────────────────────────────────

class TaskQueue:
    DDL = """
    CREATE TABLE IF NOT EXISTS tasks (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id   TEXT    NOT NULL,
        status       TEXT    NOT NULL DEFAULT 'PENDING',
        proxy        TEXT    DEFAULT '',
        target_store TEXT    DEFAULT '',
        mode         TEXT    DEFAULT 'checkout',
        retry_count  INTEGER DEFAULT 0,
        result       TEXT    DEFAULT '',
        error        TEXT    DEFAULT '',
        created_at   TEXT    NOT NULL,
        updated_at   TEXT    NOT NULL
    );
    CREATE UNIQUE INDEX IF NOT EXISTS idx_account ON tasks(account_id);
    CREATE INDEX IF NOT EXISTS idx_status  ON tasks(status);
    """

    def __init__(self, db_path: str):
        self._db = db_path
        self._log = logging.getLogger("namco.queue")

    async def init(self):
        async with aiosqlite.connect(self._db) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            for stmt in self.DDL.split(";"):
                stmt = stmt.strip()
                if stmt:
                    await db.execute(stmt)
            await db.commit()
        self._log.info(f"Queue ready: {self._db}")

    async def enqueue_accounts(self, accounts: List[AccountConfig], mode: str):
        now = _now()
        async with aiosqlite.connect(self._db) as db:
            for acc in accounts:
                await db.execute(
                    "INSERT OR IGNORE INTO tasks "
                    "(account_id,status,proxy,target_store,mode,created_at,updated_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (acc.email, "PENDING", acc.proxy, acc.target_store, mode, now, now),
                )
            await db.commit()
        self._log.info(f"Enqueued {len(accounts)} accounts")

    async def claim(self) -> Optional[Dict]:
        now = _now()
        async with aiosqlite.connect(self._db) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            async with db.execute(
                "SELECT id,account_id,proxy,target_store,mode,retry_count "
                "FROM tasks WHERE status='PENDING' LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            tid, acct, proxy, store, mode, retries = row
            await db.execute(
                "UPDATE tasks SET status='RUNNING',updated_at=? WHERE id=?",
                (now, tid),
            )
            await db.commit()
        return {"id": tid, "account_id": acct, "proxy": proxy,
                "target_store": store, "mode": mode, "retry_count": retries}

    async def complete(self, tid: int, result: Dict):
        async with aiosqlite.connect(self._db) as db:
            await db.execute(
                "UPDATE tasks SET status='COMPLETED',result=?,updated_at=? WHERE id=?",
                (json.dumps(result, ensure_ascii=False), _now(), tid),
            )
            await db.commit()

    async def fail(self, tid: int, error: str, retries: int, max_retries: int):
        new_status = "PENDING" if retries < max_retries else "DEAD_LETTER"
        async with aiosqlite.connect(self._db) as db:
            await db.execute(
                "UPDATE tasks SET status=?,error=?,retry_count=?,updated_at=? WHERE id=?",
                (new_status, error, retries + 1, _now(), tid),
            )
            await db.commit()

    async def pending_count(self) -> int:
        async with aiosqlite.connect(self._db) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM tasks WHERE status IN ('PENDING','RUNNING')"
            ) as cur:
                row = await cur.fetchone()
                return row[0] if row else 0

    async def summary(self) -> Dict[str, int]:
        async with aiosqlite.connect(self._db) as db:
            async with db.execute(
                "SELECT status, COUNT(*) FROM tasks GROUP BY status"
            ) as cur:
                return {r[0]: r[1] for r in await cur.fetchall()}

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

# ─────────────────────────────────────────────────────────────────────────────
# Core Booking Flow Functions
# ─────────────────────────────────────────────────────────────────────────────

async def step_login(s: ManagedSession, email: str, password: str) -> bool:
    resp = await s.get("/login.html")
    soup = BeautifulSoup(resp.text, "html.parser")
    hidden: Dict[str, str] = {}
    form = soup.select_one("form")
    if form:
        for inp in form.select('input[type="hidden"]'):
            n = inp.get("name")
            if n:
                hidden[n] = inp.get("value", "")

    payload = {"request": "logon", "redirectTo": "", "LOGINID": email, "PASSWORD": password}
    payload.update(hidden)

    resp = await s.post("/top_login.html", data=payload)

    if "ログアウト" in resp.text or "マイページ" in resp.text:
        METRICS.inc("login.ok")
        return True
    METRICS.inc("login.fail")
    s._log.error("Login failed — no logout link in response")
    return False


async def step_find_ticket(s: ManagedSession, cfg: AppConfig, store: str) -> Optional[Dict]:
    from urllib.parse import urljoin
    resp = await s.get("/category/EL/")
    soup = BeautifulSoup(resp.text, "html.parser")

    tickets: List[Dict] = []
    seen: set = set()
    for a in soup.select("a"):
        text = a.get_text(strip=True)
        href = a.get("href", "")
        if cfg.event_keyword in text and cfg.lottery_keyword in text and href:
            url = urljoin(BASE_URL, href)
            if url not in seen:
                seen.add(url)
                tickets.append({"name": text, "url": url})

    s._log.info(f"Found {len(tickets)} {cfg.event_keyword} lottery tickets total")

    if store:
        filtered = [t for t in tickets if store in t["name"]]
        if filtered:
            s._log.info(f"Filtered {len(filtered)} matching '{store}'")
            return filtered[0]
        s._log.warning(f"No ticket matching '{store}', trying first available")

    return tickets[0] if tickets else None


async def step_add_cart(s: ManagedSession, ticket: Dict) -> bool:
    resp = await s.get(ticket["url"])
    if resp.status_code != 200:
        s._log.error(f"Ticket page {resp.status_code}")
        return False

    soup = BeautifulSoup(resp.text, "html.parser")
    cart_form = find_cart_form(soup)
    if not cart_form:
        s._log.error("No cart form on ticket page")
        return False

    form_data = _parse_form_element(cart_form)
    form_data["request"] = "insert"
    s._log.debug(f"Cart form keys: {list(form_data.keys())}")

    await s._delay_polite()
    resp = await s.post("/cart_index.html", data=form_data)

    soup = BeautifulSoup(resp.text, "html.parser")
    err = check_error(soup)
    if err:
        s._log.error(f"Cart error: {err}")
        METRICS.inc("cart.fail")
        return False

    # Accept JSON response (AJAX mode) or HTML cart page (form mode)
    if "カート" in resp.text:
        METRICS.inc("cart.ok")
        return True
    try:
        j = resp.json()
        s._log.debug(f"Cart JSON: {str(j)[:200]}")
        METRICS.inc("cart.ok")
        return True
    except Exception:
        pass

    s._log.warning("Cart result uncertain — assuming success")
    METRICS.inc("cart.ok")
    return True


async def step_get_cart(s: ManagedSession, ticket_url: str) -> Optional[Dict]:
    resp = await s.get("/cart_index.html")
    soup = BeautifulSoup(resp.text, "html.parser")

    form = soup.select_one('form[name="cartFrm"]') or soup.select_one("form")
    form_data: Dict[str, str] = _parse_form_element(form) if form else {}

    if not form_data.get("CART_AMOUNT_0"):
        form_data["CART_AMOUNT_0"] = "1"
    form_data["CART_INDEX_REFERER"] = ticket_url
    return form_data


async def step_checkout(s: ManagedSession, cart_data: Dict) -> Optional[str]:
    await s._delay_polite()
    resp = await s.post("/cart_seisan.html", data=cart_data)
    soup = BeautifulSoup(resp.text, "html.parser")
    err = check_error(soup)
    if err:
        s._log.error(f"Seisan error: {err}")
        return None
    s._log.info("Seisan page loaded")
    return resp.text


async def step_confirm(s: ManagedSession, seisan_html: str) -> Optional[str]:
    soup = BeautifulSoup(seisan_html, "html.parser")
    form = find_confirm_form(soup)
    if not form:
        s._log.error("No confirm form")
        return None

    form_data = _parse_form_element(form)
    if "request" in form_data:
        form_data["request"] = "confirm"

    await s._delay_polite()
    resp = await s.post("/cart_confirm.html", data=form_data)
    soup = BeautifulSoup(resp.text, "html.parser")
    err = check_error(soup)
    if err:
        s._log.error(f"Confirm error: {err}")
        return None
    s._log.info("Confirm page loaded")
    return resp.text


async def step_complete(s: ManagedSession, confirm_html: str) -> Optional[Dict]:
    soup = BeautifulSoup(confirm_html, "html.parser")
    token = ""
    for inp in soup.select('input[name="token"]'):
        token = inp.get("value", "")
        if token:
            break

    if not token:
        s._log.error("Token not found on confirm page")
        return None
    s._log.info(f"Token: {token[:12]}…")

    # cart_pre
    await s._delay_polite()
    resp = await s.post("/cart_pre.html", data={"request": "cart_order_pre", "token": token, "mode": "0"})
    if resp.status_code != 200:
        s._log.error(f"cart_pre → {resp.status_code}")
        return None
    err = check_error(BeautifulSoup(resp.text, "html.parser"))
    if err:
        s._log.error(f"Pre error: {err}")
        return None
    s._log.info("Pre-order done")

    # cart_complete
    await s._delay_polite()
    resp = await s.post("/cart_complete.html", data={"token": token})
    order = find_order_number(resp.text)
    if "ご注文完了" in resp.text or order:
        METRICS.inc("order.ok")
        s._log.info(f"ORDER COMPLETE: {order}")
        return {"order_number": order, "token": token}

    err = check_error(BeautifulSoup(resp.text, "html.parser"))
    if err:
        s._log.error(f"Complete error: {err}")
    else:
        s._log.warning("Order result uncertain")
    METRICS.inc("order.fail")
    return None

# ─────────────────────────────────────────────────────────────────────────────
# Module 8: Worker
# ─────────────────────────────────────────────────────────────────────────────

async def run_account(
    account: AccountConfig,
    task: Dict,
    cfg: AppConfig,
    proxy_pool: ProxyPool,
    results_path: str,
) -> Dict:
    log = logging.getLogger(f"namco.worker.{account.email[:20]}")

    # Resolve proxy: account-level takes priority, then pool
    proxy = account.proxy or await proxy_pool.acquire()
    log.info(
        f"account={account.email} store={account.target_store or 'any'} "
        f"proxy={'yes' if proxy else 'none'} mode={cfg.mode}"
    )

    fsm = BookingFSM(account.email)
    rec: Dict[str, Any] = {
        "account": account.email,
        "target_store": account.target_store,
        "proxy": proxy or "none",
        "mode": cfg.mode,
        "started_at": _now(),
        "success": False,
        "order_number": None,
        "ticket": None,
        "error": None,
        "states": [],
    }

    try:
        async with ManagedSession(proxy, cfg, account.email) as s:

            # LOGIN
            fsm.go(FlowState.LOGIN); rec["states"].append("LOGIN")
            if not await step_login(s, account.email, account.password):
                fsm.fail("login failed"); rec["error"] = "Login failed"
                return rec

            await s._delay_polite()

            # FIND TICKET
            fsm.go(FlowState.FIND); rec["states"].append("FIND")
            ticket = await step_find_ticket(s, cfg, account.target_store)
            if not ticket:
                fsm.fail("no ticket"); rec["error"] = "No matching ticket found"
                return rec

            rec["ticket"] = ticket["name"]
            log.info(f"Ticket: {ticket['name']}")

            if cfg.mode == "dry":
                fsm.go(FlowState.DONE); rec["states"].append("DONE")
                rec["success"] = True; rec["note"] = "dry run"
                return rec

            await s._delay_polite()

            # ADD TO CART
            fsm.go(FlowState.CART); rec["states"].append("CART")
            if not await step_add_cart(s, ticket):
                fsm.fail("cart failed"); rec["error"] = "Add to cart failed"
                return rec

            if cfg.mode == "cart":
                fsm.go(FlowState.DONE); rec["states"].append("DONE")
                rec["success"] = True; rec["note"] = "cart only"
                return rec

            # GET CART PAGE
            fsm.go(FlowState.GET_CART); rec["states"].append("GET_CART")
            cart_data = await step_get_cart(s, ticket["url"])
            if not cart_data:
                fsm.fail("cart read failed"); rec["error"] = "Could not read cart"
                return rec

            await s._delay_polite()

            # CHECKOUT (seisan)
            fsm.go(FlowState.CHECKOUT); rec["states"].append("CHECKOUT")
            seisan_html = await step_checkout(s, cart_data)
            if not seisan_html:
                fsm.fail("checkout failed"); rec["error"] = "Checkout failed"
                return rec

            await s._delay_polite()

            # CONFIRM
            fsm.go(FlowState.CONFIRM); rec["states"].append("CONFIRM")
            confirm_html = await step_confirm(s, seisan_html)
            if not confirm_html:
                fsm.fail("confirm failed"); rec["error"] = "Confirm failed"
                return rec

            await s._delay_polite()

            # PRE + COMPLETE
            fsm.go(FlowState.PRE); rec["states"].append("PRE")
            order = await step_complete(s, confirm_html)
            if order:
                fsm.go(FlowState.COMPLETE); fsm.go(FlowState.DONE)
                rec["states"].extend(["COMPLETE", "DONE"])
                rec["success"] = True
                rec["order_number"] = order["order_number"]
            else:
                fsm.fail("complete failed"); rec["error"] = "Order completion failed"

    except Exception as e:
        log.exception(f"Worker crash: {e}")
        rec["error"] = str(e)
        if proxy:
            await proxy_pool.report_failure(proxy)
    else:
        if rec["success"] and proxy:
            await proxy_pool.report_success(proxy)

    rec["finished_at"] = _now()
    rec["final_state"] = fsm.state.value

    # Persist result
    with open(results_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    if rec["success"]:
        print(f"\n{'='*60}")
        print(f"  SUCCESS  {account.email}")
        print(f"  Order:   {rec['order_number']}")
        print(f"  Ticket:  {rec['ticket']}")
        print(f"{'='*60}")
    else:
        log.warning(f"FAILED {account.email}: {rec['error']}")

    return rec

# ─────────────────────────────────────────────────────────────────────────────
# Module 9: GracefulShutdown
# ─────────────────────────────────────────────────────────────────────────────

class GracefulShutdown:
    def __init__(self):
        self._stop = asyncio.Event()

    def setup(self):
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT,):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, AttributeError):
                pass  # Windows: use KeyboardInterrupt instead

    @property
    def requested(self) -> bool:
        return self._stop.is_set()

# ─────────────────────────────────────────────────────────────────────────────
# Module 5: Scheduler (asyncio.Semaphore)
# ─────────────────────────────────────────────────────────────────────────────

async def run_scheduler(
    cfg: AppConfig,
    queue: TaskQueue,
    proxy_pool: ProxyPool,
) -> List[Dict]:
    log = logging.getLogger("namco.sched")
    shutdown = GracefulShutdown()
    shutdown.setup()

    account_map = {a.email: a for a in cfg.accounts}
    sem = asyncio.Semaphore(cfg.max_concurrent)
    results: List[Dict] = []
    results_path = cfg.results_file

    log.info(
        f"Scheduler: {cfg.max_concurrent} concurrent | "
        f"{await queue.pending_count()} tasks pending"
    )

    async def _worker(task: Dict):
        async with sem:
            account = account_map.get(task["account_id"])
            if not account:
                log.error(f"Unknown account in queue: {task['account_id']}")
                return
            result = await run_account(account, task, cfg, proxy_pool, results_path)
            results.append(result)
            if result["success"]:
                await queue.complete(task["id"], result)
            else:
                await queue.fail(
                    task["id"], result.get("error", ""),
                    task["retry_count"], cfg.retry_max_attempts,
                )

    worker_tasks: List[asyncio.Task] = []
    while not shutdown.requested:
        task = await queue.claim()
        if task is None:
            break
        t = asyncio.create_task(_worker(task))
        worker_tasks.append(t)

    if worker_tasks:
        log.info(f"Waiting for {len(worker_tasks)} workers…")
        await asyncio.gather(*worker_tasks, return_exceptions=True)

    return results

# ─────────────────────────────────────────────────────────────────────────────
# Module 16: ExportPipeline / Summary
# ─────────────────────────────────────────────────────────────────────────────

def print_summary(results: List[Dict], log: logging.Logger):
    ok = [r for r in results if r.get("success")]
    fail = [r for r in results if not r.get("success")]

    print()
    print("=" * 60)
    print("  NAMCO PARKS OP-16 — BATCH COMPLETE")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    print(f"  Total:   {len(results)}")
    print(f"  Success: {len(ok)}")
    print(f"  Failed:  {len(fail)}")

    if ok:
        print("\n  Successful orders:")
        for r in ok:
            print(f"    {r['account']:<35} → {r['order_number']}")

    if fail:
        print("\n  Failed accounts:")
        for r in fail:
            print(f"    {r['account']:<35} : {r.get('error', '—')}")

    print("=" * 60)
    METRICS.report(log)

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    script_dir = Path(__file__).parent
    cfg_path = script_dir / "config.toml"

    cfg = AppConfig.load(str(cfg_path))
    log = setup_logging(cfg.log_level)

    log.info(
        f"Namco Parks Prod | accounts={len(cfg.accounts)} "
        f"mode={cfg.mode} concurrent={cfg.max_concurrent}"
    )

    # Task queue
    db_path = str(script_dir / cfg.db_path)
    queue = TaskQueue(db_path)
    await queue.init()
    await queue.enqueue_accounts(cfg.accounts, cfg.mode)
    log.info(f"Queue summary: {await queue.summary()}")

    # Proxy pool
    proxy_pool = ProxyPool(cfg.proxy_pool, cfg.proxy_max_fails)
    if cfg.proxy_pool:
        log.info(f"Proxy pool: {len(cfg.proxy_pool)} proxies")
    else:
        log.info("No proxies configured — running direct")

    # Results file
    cfg.results_file = str(script_dir / cfg.results_file)

    # Run
    results = await run_scheduler(cfg, queue, proxy_pool)

    # Report
    print_summary(results, log)

    return results


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Interrupted by user]")
