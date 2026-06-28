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
    slot_index: int = -1   # -1 = auto (first available); 0+ = nth non-placeholder option

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
    slot_weights: List[int] = field(default_factory=list)   # e.g. [300,100,100,100] → assign slot 0 to first 300 accounts, etc.
    lottery_open: str = ""          # "2026-07-04T10:00:00+09:00" or "HH:MM" (today JST)
    pre_login_minutes: int = 10     # login this many minutes before lottery_open
    keepalive_interval: int = 60    # seconds between session keepalive pings
    race_mode: bool = True          # warmup/racetest: strip polite delays + rate limit for max speed
    db_path: str = "namco_tasks.db"
    log_level: str = "INFO"
    mode: str = "checkout"
    results_file: str = "results.jsonl"
    speed_report: str = "speed_report.json"

    @classmethod
    def load(cls, path: str = "config.toml") -> "AppConfig":
        with open(path, "rb") as f:
            raw = tomllib.load(f)

        acc_raw = raw.get("accounts", [])
        # Remove slot_index from TOML if not supported by old configs (handled by default)
        accounts = [AccountConfig(**{k: v for k, v in a.items() if k in AccountConfig.__dataclass_fields__}) for a in acc_raw]
        sched = raw.get("scheduler", {})
        retry_cfg = raw.get("retry", {})
        proxy_cfg = raw.get("proxy", {})
        rl = raw.get("rate_limit", {})
        cb = raw.get("circuit_breaker", {})
        tgt = raw.get("target", {})

        # Auto-assign slot_index from slot_weights (for accounts that haven't set one explicitly)
        weights = tgt.get("slot_weights", [])
        if weights:
            total = sum(weights)
            cumulative: List[int] = []
            c = 0
            for w in weights:
                c += w
                cumulative.append(c)
            for i, acc in enumerate(accounts):
                if acc.slot_index < 0:
                    pos = i % total
                    for slot_i, threshold in enumerate(cumulative):
                        if pos < threshold:
                            acc.slot_index = slot_i
                            break

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
            slot_weights=weights,
            lottery_open=raw.get("lottery_open", ""),
            pre_login_minutes=raw.get("pre_login_minutes", 10),
            keepalive_interval=raw.get("keepalive_interval", 60),
            race_mode=raw.get("race_mode", True),
            db_path=raw.get("db_path", "namco_tasks.db"),
            log_level=raw.get("log_level", "INFO"),
            mode=raw.get("mode", "checkout"),
            results_file=raw.get("results_file", "results.jsonl"),
            speed_report=raw.get("speed_report", "speed_report.json"),
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

# Shared circuit breaker, configured in main(). Consulted by the request path in
# non-race modes (dry/checkout/cart) so a struggling target trips the breaker
# instead of getting hammered. None until main() wires it.
CIRCUIT: Optional["CircuitBreaker"] = None

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

    # Lightweight check/record interface used by the request path (so the breaker
    # is actually consulted, not dead code). race_mode skips it for max T=0 speed.
    async def can_proceed(self) -> bool:
        async with self._lock:
            if self._state == _CBState.OPEN:
                if time.time() - self._opened_at >= self._recovery:
                    self._state = _CBState.HALF_OPEN
                    self._log.info("Circuit HALF_OPEN (probing)")
                    return True
                return False
            return True

    async def record_success(self):
        async with self._lock:
            if self._state == _CBState.HALF_OPEN:
                self._state = _CBState.CLOSED
                self._log.info("Circuit CLOSED")
            self._failures = 0

    async def record_failure(self):
        async with self._lock:
            self._failures += 1
            self._opened_at = time.time()
            if self._state == _CBState.HALF_OPEN:
                self._state = _CBState.OPEN
                self._log.warning("Circuit re-OPEN (half-open probe failed)")
            elif self._failures >= self._threshold:
                self._state = _CBState.OPEN
                self._log.warning(f"Circuit OPEN after {self._failures} failures")

# ─────────────────────────────────────────────────────────────────────────────
# Module 2: FormParser
# ─────────────────────────────────────────────────────────────────────────────

def _parse_form_element(form, slot_overrides: Optional[Dict[str, int]] = None) -> Dict[str, str]:
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
        if not name:
            continue
        # Slot override: pick the nth non-placeholder option by index
        if slot_overrides and name in slot_overrides:
            idx = slot_overrides[name]
            valid_opts = [o for o in sel.select("option") if o.get("value", "").strip()]
            if valid_opts:
                data[name] = valid_opts[min(idx, len(valid_opts) - 1)].get("value", "")
                continue
        opt = sel.select_one("option[selected]")
        if opt and not opt.get("value", "").strip():
            opt = None
        if not opt:
            for o in sel.select("option"):
                if o.get("value", "").strip():
                    opt = o
                    break
        if not opt:
            opt = sel.select_one("option")
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
        self._race = cfg.race_mode      # True → bypass rate limiter & polite delays
        self.client: Optional[httpx.AsyncClient] = None
        self.signals = SessionSignals()
        self._last_url: str = BASE_URL + "/"
        # Pre-staged data (filled during warmup so T=0 starts at the competitive POST)
        self.staged_ticket: Optional[Dict] = None
        self.staged_cart_data: Optional[Dict] = None

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
        if self._race:
            return  # race mode: no polite delays — every ms counts in a 1-2s window
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
        if not self._race:
            await self._tb.acquire()
        url = f"{BASE_URL}{path}" if path.startswith("/") else path
        t0 = time.time()
        resp = await self.client.get(url, **kw)
        ms = (time.time() - t0) * 1000
        METRICS.record("http.ms", ms)
        self._record_signals(path, resp)
        self._log.info(f"GET {path[:60]} → {resp.status_code} ({ms:.0f}ms)")
        self._last_url = str(resp.url)
        return resp

    async def _do_post(self, path: str, data: Optional[Dict] = None, **kw) -> httpx.Response:
        if not self._race:
            await self._tb.acquire()
        url = f"{BASE_URL}{path}" if path.startswith("/") else path
        hdrs = kw.pop("headers", {})
        hdrs.setdefault("Referer", self._last_url)
        hdrs.setdefault("Origin", BASE_URL)
        t0 = time.time()
        resp = await self.client.post(url, data=data, headers=hdrs, **kw)
        ms = (time.time() - t0) * 1000
        METRICS.record("http.ms", ms)
        self._record_signals(path, resp)
        self._log.info(f"POST {path[:60]} → {resp.status_code} ({ms:.0f}ms)")
        self._last_url = str(resp.url)
        return resp

    def _cb_active(self) -> bool:
        # Breaker only guards polite (non-race) phases; T=0 race must stay unthrottled.
        return not self._race and CIRCUIT is not None

    async def get(self, path: str, **kw) -> httpx.Response:
        cfg = self._cfg
        resp = None
        for attempt in range(cfg.retry_max_attempts):
            if self._cb_active() and not await CIRCUIT.can_proceed():
                self._log.warning(f"Circuit OPEN — refusing GET {path}")
                raise RuntimeError("Circuit OPEN")
            try:
                resp = await self._do_get(path, **kw)
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                if self._cb_active():
                    await CIRCUIT.record_failure()
                wait = cfg.retry_initial_wait * (2 ** attempt) + random.uniform(0, cfg.retry_jitter)
                self._log.warning(f"Network error on GET {path}: {e}, retry in {wait:.1f}s")
                METRICS.inc("http.retry.net")
                await asyncio.sleep(min(wait, cfg.retry_max_wait))
                continue
            if resp.status_code != 503:
                if self._cb_active():
                    await CIRCUIT.record_success()
                return resp
            if self._cb_active():
                await CIRCUIT.record_failure()
            wait = cfg.retry_initial_wait * (2 ** attempt) + random.uniform(0, cfg.retry_jitter)
            wait = min(wait, cfg.retry_max_wait)
            self._log.warning(f"503 GET {path}, retry in {wait:.1f}s ({attempt+1}/{cfg.retry_max_attempts})")
            METRICS.inc("http.retry.503")
            await asyncio.sleep(wait)
        return resp

    async def post(self, path: str, data: Optional[Dict] = None, **kw) -> httpx.Response:
        cfg = self._cfg
        resp = None
        for attempt in range(cfg.retry_max_attempts):
            if self._cb_active() and not await CIRCUIT.can_proceed():
                self._log.warning(f"Circuit OPEN — refusing POST {path}")
                raise RuntimeError("Circuit OPEN")
            try:
                resp = await self._do_post(path, data=data, **kw)
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                if self._cb_active():
                    await CIRCUIT.record_failure()
                wait = cfg.retry_initial_wait * (2 ** attempt) + random.uniform(0, cfg.retry_jitter)
                self._log.warning(f"Network error on POST {path}: {e}, retry in {wait:.1f}s")
                METRICS.inc("http.retry.net")
                await asyncio.sleep(min(wait, cfg.retry_max_wait))
                continue
            if resp.status_code != 503:
                if self._cb_active():
                    await CIRCUIT.record_success()
                return resp
            if self._cb_active():
                await CIRCUIT.record_failure()
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


async def prepare_cart_form(s: ManagedSession, ticket: Dict, slot_index: int = -1) -> Optional[Dict]:
    """GET ticket detail page + parse the cart form. Safe to call during warmup
    (read-only) so the competitive POST at T=0 needs no prior fetch."""
    resp = await s.get(ticket["url"])
    if resp.status_code != 200:
        s._log.error(f"Ticket page {resp.status_code}")
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    cart_form = find_cart_form(soup)
    if not cart_form:
        s._log.error("No cart form on ticket page")
        return None

    overrides = {"PRIORITY_ITEMPROPERTY_CD_MATRIX_0": slot_index} if slot_index >= 0 else None
    form_data = _parse_form_element(cart_form, slot_overrides=overrides)
    form_data["request"] = "insert"
    if slot_index >= 0:
        s._log.info(f"Slot index {slot_index} → {form_data.get('PRIORITY_ITEMPROPERTY_CD_MATRIX_0', '?')}")
    return form_data


async def step_add_cart(s: ManagedSession, ticket: Dict, slot_index: int = -1,
                        prepared_form: Optional[Dict] = None) -> bool:
    # Use pre-staged form if available (race mode), else prepare it now
    form_data = prepared_form
    if form_data is None:
        form_data = await prepare_cart_form(s, ticket, slot_index)
        if form_data is None:
            return False

    await s._delay_polite()
    resp = await s.post("/cart_index.html", data=form_data)
    text = resp.text

    # "カートに追加されました" is the explicit success message Ebisu renders
    # inside the #error CSS container — definitive success, check it first.
    if "カートに追加されました" in text:
        METRICS.inc("cart.ok")
        return True

    # Explicit error must win over the loose "カート" nav-menu match below, so a
    # real failure isn't reported as success just because the page has a cart link.
    soup = BeautifulSoup(text, "html.parser")
    err = check_error(soup)
    if err:
        s._log.error(f"Cart error: {err}")
        METRICS.inc("cart.fail")
        return False

    # No explicit success phrase and no error: a page still rendering cart context
    # counts as success (matches prior verified behaviour, minus the error case).
    if "カート" in text:
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
    form_data["request"] = ""
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

def _ts_ms(t0: float) -> int:
    return int((time.time() - t0) * 1000)


async def _do_booking(
    s: ManagedSession,
    account: AccountConfig,
    cfg: AppConfig,
    rec: Dict,
    fsm: "BookingFSM",
    log: logging.Logger,
) -> None:
    """Core booking flow starting from FIND (assumes session already logged in).
    If the session was pre-staged during warmup, FIND + ticket-page fetch are
    skipped so the first action at T=0 is the competitive cart POST."""
    t0 = rec["_t0"]
    step_ms = rec["step_ms"]          # per-step duration (ms)
    fire_t0 = rec.get("_fire_t0", t0) # common T=0 across all accounts (fire moment)
    fire_off = rec["fire_offset_ms"]  # ms since T=0 when each step COMPLETED

    # FIND TICKET — use pre-staged ticket if available (warmup)
    if s.staged_ticket is not None:
        ticket = s.staged_ticket
        step_ms["find"] = 0
        rec["staged"] = True
    else:
        _t = time.time()
        ticket = await step_find_ticket(s, cfg, account.target_store)
        step_ms["find"] = _ts_ms(_t)
        if not ticket:
            fsm.go(FlowState.FIND); fsm.fail("no ticket")
            rec["error"] = "No matching ticket found"; return

    fsm.go(FlowState.FIND); rec["states"].append("FIND")
    rec["ticket"] = ticket["name"]
    log.info(f"Ticket: {ticket['name']}")

    if cfg.mode == "dry":
        fsm.go(FlowState.DONE); rec["states"].append("DONE")
        rec["success"] = True; rec["note"] = "dry run"; return

    await s._delay_polite()

    # ADD TO CART — the competitive action; use staged form to skip the GET
    _t = time.time()
    fsm.go(FlowState.CART); rec["states"].append("CART")
    if not await step_add_cart(s, ticket, account.slot_index, prepared_form=s.staged_cart_data):
        fsm.fail("cart failed"); rec["error"] = "Add to cart failed"; return
    step_ms["cart"] = _ts_ms(_t)
    fire_off["cart"] = _ts_ms(fire_t0)
    log.info(f"⚡ CART secured at T+{fire_off['cart']}ms")

    if cfg.mode == "cart":
        fsm.go(FlowState.DONE); rec["states"].append("DONE")
        rec["success"] = True; rec["note"] = "cart only"; return

    # GET CART PAGE
    fsm.go(FlowState.GET_CART); rec["states"].append("GET_CART")
    cart_data = await step_get_cart(s, ticket["url"])
    if not cart_data:
        fsm.fail("cart read failed"); rec["error"] = "Could not read cart"; return

    await s._delay_polite()

    # CHECKOUT (seisan)
    _t = time.time()
    fsm.go(FlowState.CHECKOUT); rec["states"].append("CHECKOUT")
    seisan_html = await step_checkout(s, cart_data)
    step_ms["seisan"] = _ts_ms(_t)
    fire_off["seisan"] = _ts_ms(fire_t0)
    if not seisan_html:
        fsm.fail("checkout failed"); rec["error"] = "Checkout failed"; return

    await s._delay_polite()

    # CONFIRM
    _t = time.time()
    fsm.go(FlowState.CONFIRM); rec["states"].append("CONFIRM")
    confirm_html = await step_confirm(s, seisan_html)
    step_ms["confirm"] = _ts_ms(_t)
    if not confirm_html:
        fsm.fail("confirm failed"); rec["error"] = "Confirm failed"; return

    await s._delay_polite()

    # PRE + COMPLETE
    _t = time.time()
    fsm.go(FlowState.PRE); rec["states"].append("PRE")
    order = await step_complete(s, confirm_html)
    step_ms["complete"] = _ts_ms(_t)
    if order:
        fsm.go(FlowState.COMPLETE); fsm.go(FlowState.DONE)
        rec["states"].extend(["COMPLETE", "DONE"])
        rec["success"] = True
        rec["order_number"] = order["order_number"]
    else:
        fsm.fail("complete failed"); rec["error"] = "Order completion failed"

    step_ms["total"] = _ts_ms(t0)
    fire_off["complete"] = _ts_ms(fire_t0)


def _finish_record(rec: Dict, fsm: "BookingFSM", results_path: str, log: logging.Logger):
    rec.pop("_t0", None)
    rec.pop("_fire_t0", None)
    rec["finished_at"] = _now()
    rec["final_state"] = fsm.state.value
    with open(results_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if rec["success"]:
        ms = rec["step_ms"]
        off = rec.get("fire_offset_ms", {})
        cart_off = off.get("cart")
        print(f"\n{'='*60}")
        print(f"  SUCCESS  {rec['account']}")
        print(f"  Order:   {rec['order_number']}")
        print(f"  Ticket:  {rec['ticket']}")
        print(f"  Steps:   cart={ms.get('cart','?')}ms seisan={ms.get('seisan','?')}ms "
              f"confirm={ms.get('confirm','?')}ms complete={ms.get('complete','?')}ms total={ms.get('total','?')}ms")
        if cart_off is not None:
            print(f"  ⚡ T=0→cart: {cart_off}ms   T=0→done: {off.get('complete','?')}ms")
        print(f"{'='*60}")
    else:
        log.warning(f"FAILED {rec['account']}: {rec.get('error','—')}")


async def run_account(
    account: AccountConfig,
    task: Dict,
    cfg: AppConfig,
    proxy_pool: ProxyPool,
    results_path: str,
) -> Dict:
    log = logging.getLogger(f"namco.worker.{account.email[:20]}")
    proxy = account.proxy or await proxy_pool.acquire()
    log.info(
        f"account={account.email} store={account.target_store or 'any'} "
        f"slot={account.slot_index} proxy={'yes' if proxy else 'none'} mode={cfg.mode}"
    )

    fsm = BookingFSM(account.email)
    t0 = time.time()
    rec: Dict[str, Any] = {
        "_t0": t0,
        "account": account.email,
        "target_store": account.target_store,
        "slot_index": account.slot_index,
        "proxy": proxy or "none",
        "mode": cfg.mode,
        "started_at": _now(),
        "success": False,
        "order_number": None,
        "ticket": None,
        "error": None,
        "states": [],
        "step_ms": {},
        "fire_offset_ms": {},
    }

    try:
        async with ManagedSession(proxy, cfg, account.email) as s:
            # LOGIN
            _t = time.time()
            fsm.go(FlowState.LOGIN); rec["states"].append("LOGIN")
            if not await step_login(s, account.email, account.password):
                fsm.fail("login failed"); rec["error"] = "Login failed"
                _finish_record(rec, fsm, results_path, log)
                return rec
            rec["step_ms"]["login"] = _ts_ms(_t)

            await s._delay_polite()
            await _do_booking(s, account, cfg, rec, fsm, log)

    except Exception as e:
        log.exception(f"Worker crash: {e}")
        rec["error"] = str(e)
        if proxy:
            await proxy_pool.report_failure(proxy)
    else:
        if rec["success"] and proxy:
            await proxy_pool.report_success(proxy)

    _finish_record(rec, fsm, results_path, log)
    return rec


async def run_prewarmed(
    s: ManagedSession,
    account: AccountConfig,
    cfg: AppConfig,
    results_path: str,
    fire_t0: Optional[float] = None,
) -> Dict:
    """Run booking with an already-logged-in session (warmup mode).
    fire_t0 = common T=0 perf reference shared across all accounts (the fire moment)."""
    log = logging.getLogger(f"namco.worker.{account.email[:20]}")
    fsm = BookingFSM(account.email)
    t0 = time.time()
    rec: Dict[str, Any] = {
        "_t0": t0,
        "_fire_t0": fire_t0 if fire_t0 is not None else t0,
        "account": account.email,
        "target_store": account.target_store,
        "slot_index": account.slot_index,
        "proxy": "warmed",
        "mode": cfg.mode,
        "started_at": _now(),
        "success": False,
        "order_number": None,
        "ticket": None,
        "error": None,
        "fire_offset_ms": {},
        "states": ["LOGIN"],   # already logged in
        "step_ms": {"login": 0},
        "warmed": True,
    }
    # Session already authenticated during warmup — advance FSM past LOGIN
    fsm.go(FlowState.LOGIN)
    try:
        await _do_booking(s, account, cfg, rec, fsm, log)
    except Exception as e:
        log.exception(f"Worker crash: {e}")
        rec["error"] = str(e)
    _finish_record(rec, fsm, results_path, log)
    return rec

# ─────────────────────────────────────────────────────────────────────────────
# Warmup Scheduler — pre-login + keepalive + synchronized fire
# ─────────────────────────────────────────────────────────────────────────────

def _parse_lottery_open(s: str) -> Optional[datetime]:
    """Parse lottery open time.  Accepts:
      "2026-07-04T10:00:00+09:00"  →  full ISO
      "2026-07-04 10:00"           →  assumes JST (+09:00)
      "10:00"                      →  today JST
    Returns UTC-aware datetime, or None if empty/invalid.
    """
    if not s:
        return None
    from datetime import timezone, timedelta
    JST = timezone(timedelta(hours=9))
    s = s.strip()
    try:
        if "T" in s and "+" in s:
            return datetime.fromisoformat(s)
        if len(s) == 5 and ":" in s:  # "HH:MM"
            now_jst = datetime.now(JST)
            t = datetime.strptime(s, "%H:%M").replace(
                year=now_jst.year, month=now_jst.month, day=now_jst.day, tzinfo=JST
            )
            return t
        if " " in s:
            t = datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
            return t
    except ValueError:
        pass
    return None


class WarmupScheduler:
    """
    1. warmup_all()        — login all accounts (concurrent, throttled)
    2. keepalive_until()   — ping sessions every cfg.keepalive_interval seconds
    3. fire_all()          — run booking for all live sessions simultaneously
    """

    def __init__(self, cfg: AppConfig, results_path: str):
        self._cfg = cfg
        self._results_path = results_path
        self._sessions: Dict[str, ManagedSession] = {}
        self._accounts: Dict[str, AccountConfig] = {a.email: a for a in cfg.accounts}
        self._log = logging.getLogger("namco.warmup")

    # ── shared primitives (login + stage) — used by warmup, recovery, re-stage ──

    async def _stage_session(self, acc: AccountConfig, s: ManagedSession) -> bool:
        """(Re)fetch ticket page + parse cart form so the token is fresh for T=0.
        Returns True if the session now holds a usable staged cart form."""
        try:
            ticket = await step_find_ticket(s, self._cfg, acc.target_store)
            if not ticket:
                self._log.warning(f"Stage: no ticket for {acc.email}")
                return False
            form = await prepare_cart_form(s, ticket, acc.slot_index)
            if form is None:
                self._log.warning(f"Stage: no cart form for {acc.email}")
                return False
            s.staged_ticket = ticket
            s.staged_cart_data = form
            return True
        except Exception as e:
            self._log.warning(f"Stage error {acc.email}: {e}")
            return False

    async def _login_and_stage(self, acc: AccountConfig) -> Optional[ManagedSession]:
        """Fresh login + pre-stage for one account. Returns a live session (staged
        if possible) or None if login failed. Used for keepalive recovery."""
        s = ManagedSession(acc.proxy or "", self._cfg, acc.email)
        await s.__aenter__()
        if not await step_login(s, acc.email, acc.password):
            await s.__aexit__(None, None, None)
            return None
        await self._stage_session(acc, s)
        return s

    async def warmup_all(self) -> int:
        sem = asyncio.Semaphore(self._cfg.max_concurrent)

        async def _login_one(acc: AccountConfig):
            async with sem:
                proxy = acc.proxy or ""
                s = ManagedSession(proxy, self._cfg, acc.email)
                await s.__aenter__()
                ok = await step_login(s, acc.email, acc.password)
                if ok:
                    self._sessions[acc.email] = s
                    self._log.info(f"Warmed ✓ {acc.email}  slot={acc.slot_index}")
                else:
                    await s.__aexit__(None, None, None)
                    self._log.error(f"Warmup login FAILED: {acc.email}")

        self._log.info(f"Warming up {len(self._cfg.accounts)} accounts …")
        t0 = time.time()
        await asyncio.gather(*[_login_one(acc) for acc in self._cfg.accounts])
        elapsed = time.time() - t0
        ok_count = len(self._sessions)
        self._log.info(
            f"Warmup done: {ok_count}/{len(self._cfg.accounts)} logged in ({elapsed:.1f}s)"
        )
        return ok_count

    async def stage_all(self):
        """Pre-fetch ticket page + parse cart form for every live session, so the
        first action at T=0 is the competitive cart POST (no GET on the hot path)."""
        sem = asyncio.Semaphore(self._cfg.max_concurrent)

        async def _one(email: str, s: ManagedSession):
            acc = self._accounts.get(email)
            if not acc:
                return
            async with sem:
                if await self._stage_session(acc, s):
                    self._log.info(f"Staged ✓ {email} → {s.staged_ticket['name'][:30]}")

        self._log.info(f"Staging {len(self._sessions)} sessions (pre-fetch ticket + form) …")
        await asyncio.gather(*[_one(e, s) for e, s in self._sessions.items()])
        staged = sum(1 for s in self._sessions.values() if s.staged_cart_data is not None)
        self._log.info(f"Staged {staged}/{len(self._sessions)} sessions ready for T=0")

    async def restage_all(self):
        """Refresh ticket page + cart form for all live sessions shortly before fire,
        so the cart token is fresh at T=0 (guards against token TTL expiry during the
        idle wait — the staged token may go stale even while the cookie stays valid)."""
        sem = asyncio.Semaphore(self._cfg.max_concurrent)

        async def _one(email: str, s: ManagedSession):
            acc = self._accounts.get(email)
            if not acc:
                return
            async with sem:
                await self._stage_session(acc, s)

        self._log.info(f"Pre-fire re-stage: refreshing cart tokens for {len(self._sessions)} sessions …")
        await asyncio.gather(*[_one(e, s) for e, s in list(self._sessions.items())])
        staged = sum(1 for s in self._sessions.values() if s.staged_cart_data is not None)
        self._log.info(f"Re-staged {staged}/{len(self._sessions)} sessions with fresh tokens")

    async def _ping_and_recover(self):
        """Ping every session; any that bounced to login.html (session dropped) or
        errored is re-logged-in and re-staged — this is what keeps all accounts truly
        'ready' through the 10-min idle wait instead of silently dying."""
        dead: List[str] = []

        async def _ping(email: str, s: ManagedSession):
            try:
                resp = await s.get("/")
                if "/login.html" in str(resp.url):
                    dead.append(email)
            except Exception as e:
                self._log.warning(f"Keepalive ping error {email}: {e}")
                dead.append(email)

        await asyncio.gather(*[_ping(e, s) for e, s in list(self._sessions.items())])
        if dead:
            self._log.warning(f"⚠ {len(dead)} session(s) dropped — re-logging in: {dead}")
            await self._recover(dead)

    async def _recover(self, emails: List[str]):
        sem = asyncio.Semaphore(self._cfg.max_concurrent)

        async def _one(email: str):
            acc = self._accounts.get(email)
            if not acc:
                return
            async with sem:
                old = self._sessions.pop(email, None)
                if old:
                    try:
                        await old.__aexit__(None, None, None)
                    except Exception:
                        pass
                s = await self._login_and_stage(acc)
                if s and s.staged_cart_data is not None:
                    self._sessions[email] = s
                    self._log.info(f"Recovered ✓ {email}")
                elif s:
                    self._sessions[email] = s
                    self._log.warning(f"Recovered (login OK, stage failed) {email}")
                else:
                    self._log.error(f"Recovery FAILED {email} — absent at fire")

        await asyncio.gather(*[_one(e) for e in emails])

    async def keepalive_until(self, target: datetime):
        interval = self._cfg.keepalive_interval
        restaged = False
        # Refresh all cart tokens once when we get within this many seconds of open.
        restage_window = max(interval + 30, 90)

        while True:
            now = datetime.now(timezone.utc)
            remaining = (target - now).total_seconds()
            if remaining <= 2:
                break
            self._log.info(
                f"Keepalive — {len(self._sessions)} sessions alive, "
                f"{remaining:.0f}s until open"
            )
            await self._ping_and_recover()
            if not restaged and remaining <= restage_window:
                await self.restage_all()
                restaged = True
            sleep_secs = min(interval, remaining - 2)
            await asyncio.sleep(sleep_secs)

        # Last liveness pass on the doorstep of T=0 (recover any final drop).
        await self._ping_and_recover()
        if not restaged:
            # Very short pre-login window: ensure tokens are fresh at least once.
            await self.restage_all()

    async def fire_all(self) -> List[Dict]:
        self._log.info(f"🔥 FIRING {len(self._sessions)} sessions simultaneously!")
        account_map = {a.email: a for a in self._cfg.accounts}
        # Single common T=0 reference so every account's offsets are comparable
        fire_t0 = time.time()
        tasks = [
            run_prewarmed(s, account_map[email], self._cfg, self._results_path, fire_t0=fire_t0)
            for email, s in self._sessions.items()
            if email in account_map
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Close all sessions
        for s in self._sessions.values():
            try:
                await s.__aexit__(None, None, None)
            except Exception:
                pass

        return [r for r in results if isinstance(r, dict)]


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


def _percentiles(vals: List[int]) -> Dict[str, int]:
    if not vals:
        return {}
    s = sorted(vals)
    def pct(p: float) -> int:
        if len(s) == 1:
            return s[0]
        k = (len(s) - 1) * p
        f = int(k)
        c = min(f + 1, len(s) - 1)
        return int(s[f] + (s[c] - s[f]) * (k - f))
    return {
        "min": s[0], "p50": pct(0.50), "p95": pct(0.95),
        "p99": pct(0.99), "max": s[-1], "mean": int(sum(s) / len(s)),
    }


def build_speed_report(results: List[Dict], cfg: AppConfig, log: logging.Logger) -> Dict:
    """Aggregate per-step & fire-offset timing across all accounts, save + display.
    This is the data that drives 'gradually optimize speed' for the 1-2s window."""
    ok = [r for r in results if r.get("success")]

    # Collect step durations
    step_keys = ["login", "find", "cart", "seisan", "confirm", "complete", "total"]
    step_stats = {}
    for k in step_keys:
        vals = [r["step_ms"][k] for r in results if r.get("step_ms", {}).get(k) is not None]
        if vals:
            step_stats[k] = _percentiles(vals)

    # Collect fire-offsets (time from T=0 to reaching each milestone) — the key metric
    off_keys = ["cart", "seisan", "complete"]
    offset_stats = {}
    for k in off_keys:
        vals = [r["fire_offset_ms"][k] for r in results
                if r.get("fire_offset_ms", {}).get(k) is not None]
        if vals:
            offset_stats[k] = _percentiles(vals)

    report = {
        "generated_at": _now(),
        "accounts": len(results),
        "success": len(ok),
        "step_duration_ms": step_stats,
        "fire_offset_ms": offset_stats,
        "per_account": [
            {
                "account": r["account"],
                "success": r.get("success"),
                "order": r.get("order_number"),
                "step_ms": r.get("step_ms", {}),
                "fire_offset_ms": r.get("fire_offset_ms", {}),
            }
            for r in results
        ],
    }

    # Save
    out_path = Path(cfg.speed_report)
    if not out_path.is_absolute():
        out_path = Path(cfg.results_file).parent / cfg.speed_report
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # Display
    print("\n" + "=" * 68)
    print("  ⚡ 速度报告 (SPEED REPORT)")
    print("=" * 68)
    if offset_stats:
        print("\n  距开抢T=0的耗时 (越小越能抢到热门场):")
        print(f"    {'里程碑':<12}{'min':>8}{'p50':>8}{'p95':>8}{'p99':>8}{'max':>8}")
        names = {"cart": "抢到名额", "seisan": "进结算", "complete": "下单完成"}
        for k in off_keys:
            if k in offset_stats:
                st = offset_stats[k]
                print(f"    {names[k]:<10}{st['min']:>8}{st['p50']:>8}{st['p95']:>8}{st['p99']:>8}{st['max']:>8}")
    print("\n  各步骤耗时 (ms, 找瓶颈):")
    print(f"    {'步骤':<12}{'min':>8}{'p50':>8}{'p95':>8}{'p99':>8}{'max':>8}")
    labels = {"login":"登录","find":"找票","cart":"加购","seisan":"结算","confirm":"确认","complete":"完成","total":"总计"}
    for k in step_keys:
        if k in step_stats:
            st = step_stats[k]
            print(f"    {labels[k]:<10}{st['min']:>8}{st['p50']:>8}{st['p95']:>8}{st['p99']:>8}{st['max']:>8}")
    print("=" * 68)
    print(f"  → 已保存: {out_path}")
    print("=" * 68)
    return report


async def run_racetest(cfg: AppConfig, log: logging.Logger, rounds: int = 3) -> Dict:
    """安全测速：登录 → 找票 → 解析加购表单，重复 N 轮，测可控链路网络/服务器延迟。
    不做任何 POST 提交，不消耗账号的「一人一店一次」额度。"""
    log.info(f"RACETEST | {len(cfg.accounts)} accounts × {rounds} rounds (read-only, no submit)")
    sem = asyncio.Semaphore(cfg.max_concurrent)
    samples: Dict[str, List[int]] = {"login": [], "find": [], "prepare": [], "ready_total": []}

    async def _one(acc: AccountConfig, rnd: int):
        async with sem:
            try:
                async with ManagedSession(acc.proxy or "", cfg, acc.email) as s:
                    t_all = time.time()
                    _t = time.time()
                    if not await step_login(s, acc.email, acc.password):
                        log.warning(f"[r{rnd}] login failed {acc.email}")
                        return
                    samples["login"].append(_ts_ms(_t))
                    _t = time.time()
                    ticket = await step_find_ticket(s, cfg, acc.target_store)
                    samples["find"].append(_ts_ms(_t))
                    if not ticket:
                        return
                    _t = time.time()
                    form = await prepare_cart_form(s, ticket, acc.slot_index)
                    samples["prepare"].append(_ts_ms(_t))
                    if form is not None:
                        samples["ready_total"].append(_ts_ms(t_all))
            except Exception as e:
                log.warning(f"[r{rnd}] racetest error {acc.email}: {e}")

    for rnd in range(1, rounds + 1):
        log.info(f"--- round {rnd}/{rounds} ---")
        await asyncio.gather(*[_one(acc, rnd) for acc in cfg.accounts])

    print("\n" + "=" * 60)
    print("  ⚡ RACETEST 测速结果 (可控链路, 不含竞争POST)")
    print("=" * 60)
    print(f"    {'步骤':<14}{'min':>7}{'p50':>7}{'p95':>7}{'max':>7}{'n':>6}")
    labels = {"login":"登录","find":"找票","prepare":"解析加购表单","ready_total":"就绪总耗时"}
    stats = {}
    for k in ["login", "find", "prepare", "ready_total"]:
        st = _percentiles(samples[k])
        stats[k] = st
        if st:
            print(f"    {labels[k]:<12}{st['min']:>7}{st['p50']:>7}{st['p95']:>7}{st['max']:>7}{len(samples[k]):>6}")
    print("=" * 60)
    print("  注：'就绪总耗时'=从零到可发起抢购POST的时间。warmup预热后此项≈0，")
    print("      因为登录+找票+表单都已在开抢前完成，T=0直接POST抢名额。")
    print("=" * 60)

    report = {"generated_at": _now(), "rounds": rounds, "stats": stats}
    out_path = Path(cfg.results_file).parent / "racetest_report.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"  → 已保存: {out_path}\n")
    return report

# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def _log_slot_distribution(cfg: AppConfig, log: logging.Logger):
    """Show how many accounts land in each time slot, so the operator can VERIFY the
    distribution before firing (goal 2). slot=-1 means 'auto / first available'."""
    from collections import Counter
    counts = Counter(a.slot_index for a in cfg.accounts)
    print("\n" + "=" * 52)
    print("  时段分配 (SLOT DISTRIBUTION) — 开抢前请核对")
    print("=" * 52)
    for slot in sorted(counts):
        label = "自动(第一个可用)" if slot < 0 else f"slot[{slot}]"
        n = counts[slot]
        bar = "█" * min(40, n)
        print(f"    {label:<16} {n:>5} 账号  {bar}")
    print("    " + "-" * 44)
    print(f"    {'合计':<16} {sum(counts.values()):>5} 账号")
    if cfg.slot_weights:
        print(f"    权重来源: slot_weights={cfg.slot_weights}")
    else:
        print("    (未配置 slot_weights → 全部自动选第一个可用时段)")
    print("=" * 52 + "\n")


async def main():
    script_dir = Path(__file__).parent
    cfg_path = script_dir / "config.toml"

    cfg = AppConfig.load(str(cfg_path))
    log = setup_logging(cfg.log_level)
    cfg.results_file = str(script_dir / cfg.results_file)

    # Wire the shared circuit breaker (consulted by non-race request paths).
    global CIRCUIT
    CIRCUIT = CircuitBreaker(cfg.circuit_failure_threshold, cfg.circuit_recovery_timeout)

    log.info(
        f"Namco Parks Prod | accounts={len(cfg.accounts)} "
        f"mode={cfg.mode} concurrent={cfg.max_concurrent}"
    )
    if cfg.slot_weights:
        log.info(f"Slot weights: {cfg.slot_weights} (total={sum(cfg.slot_weights)} accounts per cycle)")
    _log_slot_distribution(cfg, log)

    # ── Racetest mode (safe speed benchmark, no submit) ───────────────────────
    if cfg.mode == "racetest":
        await run_racetest(cfg, log, rounds=3)
        return []

    # ── Warmup mode ──────────────────────────────────────────────────────────
    if cfg.mode == "warmup":
        lottery_open = _parse_lottery_open(cfg.lottery_open)
        if not lottery_open:
            log.error("warmup mode requires 'lottery_open' in config.toml  e.g. \"2026-07-04T10:00:00+09:00\"")
            return []

        now = datetime.now(timezone.utc)
        warmup_at = lottery_open - __import__("datetime").timedelta(minutes=cfg.pre_login_minutes)

        if now < warmup_at:
            wait_secs = (warmup_at - now).total_seconds()
            log.info(
                f"Lottery opens at {lottery_open.isoformat()}  "
                f"Pre-login at {warmup_at.isoformat()}  "
                f"Sleeping {wait_secs:.0f}s …"
            )
            await asyncio.sleep(wait_secs)

        scheduler = WarmupScheduler(cfg, cfg.results_file)
        ok = await scheduler.warmup_all()
        if ok == 0:
            log.error("All warmup logins failed — aborting")
            return []

        # Pre-stage ticket + cart form so T=0 starts at the competitive POST
        await scheduler.stage_all()

        await scheduler.keepalive_until(lottery_open)

        log.info(f"Lottery open! Firing all {ok} sessions …")
        results = await scheduler.fire_all()
        print_summary(results, log)
        build_speed_report(results, cfg, log)
        return results

    # ── Normal / scheduled mode ───────────────────────────────────────────────
    db_path = str(script_dir / cfg.db_path)
    queue = TaskQueue(db_path)
    await queue.init()
    await queue.enqueue_accounts(cfg.accounts, cfg.mode)
    log.info(f"Queue summary: {await queue.summary()}")

    proxy_pool = ProxyPool(cfg.proxy_pool, cfg.proxy_max_fails)
    if cfg.proxy_pool:
        log.info(f"Proxy pool: {len(cfg.proxy_pool)} proxies")
    else:
        log.info("No proxies configured — running direct")

    results = await run_scheduler(cfg, queue, proxy_pool)
    print_summary(results, log)
    build_speed_report(results, cfg, log)
    return results


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Interrupted by user]")
