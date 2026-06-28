"""
Namco Parks - 压测 Harness
摸清目标网站脾气：503率 / IP封禁 / Session稳定性 / 延迟分布

用法:  py -3.12 namco_bench.py [--phase 1] [--accounts 10] [--concurrency 5]
结果:  bench_results.jsonl  +  bench_YYYYMMDD_HHMMSS.log
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── 复用生产模块 ──────────────────────────────────────────────────────────────
from namco_prod import (
    AppConfig,
    ManagedSession,
    ProxyPool,
    METRICS,
    step_login,
    step_find_ticket,
    check_error,
)
from bs4 import BeautifulSoup

log = logging.getLogger("bench")

# ─────────────────────────────────────────────────────────────────────────────
# BenchResult
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BenchResult:
    phase: str = ""
    total: int = 0
    success: int = 0
    failed: int = 0
    # 异常细分
    rate_limited_503: int = 0   # 降并发或加代理
    ip_blocked_403: int = 0     # 必须挂代理
    session_expired: int = 0    # 延迟太短 / session 过短
    captcha_hit: int = 0        # 触发反爬，需住宅 IP
    login_failed: int = 0       # 账号问题
    net_error: int = 0          # 连接超时 / 拒绝
    # 延迟
    latencies: List[float] = field(default_factory=list, repr=False)
    avg_ms: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    # 汇总
    total_s: float = 0.0
    error_detail: Dict[str, int] = field(default_factory=dict)

    def finalize(self):
        if not self.latencies:
            return
        s = sorted(self.latencies)
        n = len(s)
        self.avg_ms = round(sum(s) / n, 1)
        self.p50_ms = round(s[max(0, int(n * 0.50) - 1)], 1)
        self.p95_ms = round(s[max(0, int(n * 0.95) - 1)], 1)
        self.p99_ms = round(s[max(0, int(n * 0.99) - 1)], 1)

    def as_dict(self) -> dict:
        d = {k: v for k, v in vars(self).items() if k != "latencies"}
        d["ts"] = datetime.now(timezone.utc).isoformat()
        return d

    def print_report(self):
        r = self
        success_rate = r.success / r.total * 100 if r.total else 0
        lines = [
            f"\n{'─'*50}",
            f"  压测阶段: {r.phase}",
            f"{'─'*50}",
            f"  总账号数 : {r.total}",
            f"  成  功   : {r.success:<5d}  ({success_rate:.1f}%)",
            f"  失  败   : {r.failed}",
            f"{'─'*50}",
            f"  503 限流 : {r.rate_limited_503:<5d}  {'⚠ 降并发或加代理' if r.rate_limited_503 > r.total * 0.1 else '✓'}",
            f"  IP 封禁  : {r.ip_blocked_403:<5d}  {'🔴 必须挂代理' if r.ip_blocked_403 > 0 else '✓'}",
            f"  Session过期: {r.session_expired:<3d}  {'⚠ 加延迟' if r.session_expired > 0 else '✓'}",
            f"  验证码   : {r.captcha_hit:<5d}  {'🔴 触发反爬' if r.captcha_hit > 0 else '✓'}",
            f"  登录失败 : {r.login_failed:<5d}  {'⚠ 检查账号' if r.login_failed > 0 else '✓'}",
            f"  网络错误 : {r.net_error:<5d}",
            f"{'─'*50}",
            f"  avg={r.avg_ms:.0f}ms  p50={r.p50_ms:.0f}ms  p95={r.p95_ms:.0f}ms  p99={r.p99_ms:.0f}ms",
            f"  总耗时   : {r.total_s:.1f}s",
        ]
        if r.error_detail:
            lines.append(f"  异常分布 : {json.dumps(r.error_detail, ensure_ascii=False)}")
        lines.append(f"{'─'*50}")
        print("\n".join(lines))

# ─────────────────────────────────────────────────────────────────────────────
# 核心：单账号压测任务
# ─────────────────────────────────────────────────────────────────────────────

async def _run_one(
    account_index: int,
    email: str,
    password: str,
    cfg: AppConfig,
    proxy: Optional[str],
    depth: str,               # "login" | "find" | "cart"
    result: BenchResult,
    sem: asyncio.Semaphore,
    error_counts: Dict[str, int],
):
    async with sem:
        t0 = time.monotonic()
        outcome = "failed"        # success | login_failed | no_ticket | cart_err | net_error | exception
        async with ManagedSession(proxy, cfg, f"bench-{account_index}") as s:
            try:
                # ── 1. 登录 ──────────────────────────────────────
                if not await step_login(s, email, password):
                    outcome = "login_failed"
                elif depth == "login":
                    outcome = "success"
                else:
                    await asyncio.sleep(random.uniform(cfg.delay_min, cfg.delay_max))

                    # ── 2. 找票 ──────────────────────────────────
                    ticket = await step_find_ticket(s, cfg, "")
                    if not ticket:
                        outcome = "no_ticket"
                    elif depth == "find":
                        outcome = "success"
                    else:
                        await asyncio.sleep(random.uniform(cfg.delay_min, cfg.delay_max))

                        # ── 3. 加购物车页（dry：只 GET，不提交） ──
                        resp = await s.get(ticket["url"])
                        err = check_error(BeautifulSoup(resp.text, "html.parser"))
                        if err:
                            outcome = "cart_err"
                            error_counts[f"cart_err:{err[:40]}"] = \
                                error_counts.get(f"cart_err:{err[:40]}", 0) + 1
                        else:
                            outcome = "success"

            except (Exception,) as e:
                name = type(e).__name__
                error_counts[name] = error_counts.get(name, 0) + 1
                if name in ("ConnectError", "ConnectTimeout", "ReadTimeout",
                            "TimeoutException", "RemoteProtocolError", "ProxyError"):
                    outcome = "net_error"
                else:
                    outcome = "exception"
                log.debug(f"bench-{account_index} error: {name}: {str(e)[:80]}")

            # ── 滚动 HTTP 信号（真相来源，与 outcome 独立）──────
            sg = s.signals
            result.rate_limited_503 += sg.http_503
            result.ip_blocked_403   += sg.http_403
            result.session_expired  += sg.redirect_login
            result.captcha_hit      += sg.captcha

        # ── 结算 outcome（每个账号只计一次成功/失败）──────────
        if outcome == "success":
            result.success += 1
            result.latencies.append((time.monotonic() - t0) * 1000)
        else:
            result.failed += 1
            if outcome == "login_failed":
                result.login_failed += 1
            elif outcome == "net_error":
                result.net_error += 1
            elif outcome == "no_ticket":
                error_counts["no_ticket"] = error_counts.get("no_ticket", 0) + 1

# ─────────────────────────────────────────────────────────────────────────────
# BenchHarness
# ─────────────────────────────────────────────────────────────────────────────

class BenchHarness:
    def __init__(self, cfg: AppConfig, proxy_pool: ProxyPool):
        self.cfg = cfg
        self.proxy_pool = proxy_pool

    async def run_phase(
        self,
        phase_name: str,
        accounts: List[tuple],        # [(email, password), ...]
        concurrency: int,
        depth: str = "find",          # "login" | "find" | "cart"
        proxies: Optional[List[str]] = None,
        delay_override: Optional[Tuple[float, float]] = None,
    ) -> BenchResult:

        # 临时覆盖延迟
        orig_min, orig_max = self.cfg.delay_min, self.cfg.delay_max
        if delay_override:
            self.cfg.delay_min, self.cfg.delay_max = delay_override

        pool = ProxyPool(proxies, self.cfg.proxy_max_fails) if proxies else self.proxy_pool
        result = BenchResult(phase=phase_name, total=len(accounts))
        error_counts: Dict[str, int] = {}
        sem = asyncio.Semaphore(concurrency)

        log.info(
            f"\n{'='*55}\n"
            f"  阶段: {phase_name}\n"
            f"  账号: {len(accounts)} | 并发: {concurrency} | 深度: {depth}\n"
            f"  代理: {len(proxies) if proxies else 0} 个\n"
            f"  延迟: {self.cfg.delay_min:.1f}-{self.cfg.delay_max:.1f}s\n"
            f"{'='*55}"
        )

        t_start = time.monotonic()
        tasks = [
            asyncio.create_task(_run_one(
                account_index=i,
                email=email,
                password=pw,
                cfg=self.cfg,
                proxy=await pool.acquire() if not pool.is_empty() else None,
                depth=depth,
                result=result,
                sem=sem,
                error_counts=error_counts,
            ))
            for i, (email, pw) in enumerate(accounts)
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        result.total_s = round(time.monotonic() - t_start, 1)
        result.error_detail = error_counts
        result.finalize()

        # 还原延迟
        self.cfg.delay_min, self.cfg.delay_max = orig_min, orig_max

        result.print_report()
        return result

# ─────────────────────────────────────────────────────────────────────────────
# 五阶段压测流程
# ─────────────────────────────────────────────────────────────────────────────

async def run_benchmark(
    cfg: AppConfig,
    all_accounts: List[tuple],
    proxies: List[str],
    start_phase: int = 1,
    results_path: str = "bench_results.jsonl",
):
    harness = BenchHarness(cfg, ProxyPool(proxies, cfg.proxy_max_fails))
    all_results = []

    def save(r: BenchResult):
        all_results.append(r)
        with open(results_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(r.as_dict(), ensure_ascii=False) + "\n")

    # ── Phase 1: 裸IP 5并发 10账号 ────────────────────────────────────────
    if start_phase <= 1:
        r1 = await harness.run_phase(
            phase_name="Phase1 裸IP×5并发×10账号",
            accounts=all_accounts[:10],
            concurrency=5,
            depth="find",
            proxies=None,
            delay_override=(3.0, 6.0),
        )
        save(r1)

        if r1.ip_blocked_403 > 0:
            print("\n🔴 裸IP直连即被403封禁 — 必须先挂代理再继续")
            return _conclude(all_results)
        if r1.captcha_hit > 0:
            print("\n🔴 触发验证码 — 需要住宅IP + 降速")
            return _conclude(all_results)

    # ── Phase 2: 裸IP 10并发 20账号 ──────────────────────────────────────
    if start_phase <= 2:
        r2 = await harness.run_phase(
            phase_name="Phase2 裸IP×10并发×20账号",
            accounts=all_accounts[:20],
            concurrency=10,
            depth="find",
            proxies=None,
            delay_override=(2.0, 5.0),
        )
        save(r2)

        if r2.ip_blocked_403 > 0:
            print("\n🔴 10并发已被IP封禁 — 后续阶段强制挂代理")
            if not proxies:
                return _conclude(all_results)

    # ── Phase 3: 裸IP 20并发 50账号 ──────────────────────────────────────
    if start_phase <= 3:
        r3 = await harness.run_phase(
            phase_name="Phase3 裸IP×20并发×50账号",
            accounts=all_accounts[:50],
            concurrency=20,
            depth="find",
            proxies=None,
            delay_override=(2.0, 4.0),
        )
        save(r3)

    # ── Phase 4: 挂代理 20并发 50账号 ────────────────────────────────────
    if start_phase <= 4:
        if proxies:
            r4 = await harness.run_phase(
                phase_name="Phase4 代理×20并发×50账号",
                accounts=all_accounts[:50],
                concurrency=20,
                depth="find",
                proxies=proxies,
                delay_override=(2.0, 4.0),
            )
            save(r4)
        else:
            print("\nPhase4 跳过（无代理配置）")

    # ── Phase 5: 全量100账号 ──────────────────────────────────────────────
    if start_phase <= 5:
        final_proxies = proxies if proxies else None
        r5 = await harness.run_phase(
            phase_name="Phase5 全量×100账号",
            accounts=all_accounts[:100],
            concurrency=20,
            depth="cart",               # 走到加购物车页但不提交
            proxies=final_proxies,
            delay_override=(2.0, 4.0),
        )
        save(r5)

    return _conclude(all_results)


def _conclude(results: List[BenchResult]) -> dict:
    if not results:
        return {}

    last = results[-1]
    phase3 = next((r for r in results if "Phase3" in r.phase), None)
    phase2 = next((r for r in results if "Phase2" in r.phase), None)

    rec_concurrency = 20
    need_proxy = False
    rec_delay_min = 2.0
    rec_delay_max = 4.0

    if phase3 and phase3.rate_limited_503 > phase3.total * 0.10:
        rec_concurrency = 10
        rec_delay_min, rec_delay_max = 3.0, 6.0
    if any(r.ip_blocked_403 > 0 for r in results):
        need_proxy = True
    if any(r.session_expired > r.total * 0.05 for r in results):
        rec_delay_min = max(rec_delay_min, 3.0)

    est_100 = last.total_s if last.total >= 100 else (last.total_s / last.total * 100 if last.total else 0)

    conclusion = {
        "recommended_concurrency": rec_concurrency,
        "need_proxy": need_proxy,
        "recommended_delay": f"{rec_delay_min:.1f}-{rec_delay_max:.1f}s",
        "estimated_100_accounts_s": round(est_100, 0),
        "phases_run": len(results),
    }

    print("\n" + "=" * 55)
    print("  压测结论")
    print("=" * 55)
    print(f"  推荐并发数    : {conclusion['recommended_concurrency']}")
    print(f"  需要代理      : {'是 🔴' if need_proxy else '可先不用 ✓'}")
    print(f"  推荐延迟      : {conclusion['recommended_delay']}")
    print(f"  预计100账号耗时: ~{conclusion['estimated_100_accounts_s']:.0f}s")
    print("=" * 55)

    return conclusion


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(description="Namco Parks 压测 Harness")
    p.add_argument("config", nargs="?", default="config.toml")
    p.add_argument("--phase", type=int, default=1, help="从第几阶段开始 (1-5)")
    p.add_argument("--accounts", type=int, default=0,
                   help="指定账号数（0=用 config 里全部）")
    p.add_argument("--concurrency", type=int, default=0,
                   help="覆盖并发数（0=按阶段默认值）")
    p.add_argument("--depth", choices=["login", "find", "cart"], default="find",
                   help="压测深度: login/find/cart")
    p.add_argument("--proxies", nargs="*", default=[],
                   help="代理 URL 列表（覆盖 config.toml 里的 pool）")
    return p.parse_args()


async def main():
    args = parse_args()
    script_dir = Path(__file__).parent
    cfg = AppConfig.load(str(script_dir / args.config))

    # 设置日志
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = str(script_dir / f"bench_{ts}.log")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter("%(asctime)s [%(levelname)-5s] %(message)s", "%H:%M:%S"))
    root.addHandler(ch)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s"))
    root.addHandler(fh)

    log.info(f"压测日志: {log_path}")

    # 组装账号列表 [(email, password), ...]
    accounts = [(a.email, a.password) for a in cfg.accounts]
    if not accounts:
        print("config.toml 里没有账号，退出")
        return

    if args.accounts > 0:
        accounts = accounts[:args.accounts]

    # 代理列表（命令行优先，其次 config）
    proxies = args.proxies or cfg.proxy_pool

    log.info(
        f"账号: {len(accounts)} | 代理: {len(proxies)} | "
        f"模式: {cfg.mode} | 起始阶段: Phase{args.phase}"
    )

    # 如果只想跑单一阶段（--phase + --concurrency + --depth）
    if args.concurrency > 0:
        harness = BenchHarness(cfg, ProxyPool(proxies, cfg.proxy_max_fails))
        r = await harness.run_phase(
            phase_name=f"自定义 {args.concurrency}并发 depth={args.depth}",
            accounts=accounts,
            concurrency=args.concurrency,
            depth=args.depth,
            proxies=proxies or None,
        )
        with open(script_dir / "bench_results.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(r.as_dict(), ensure_ascii=False) + "\n")
    else:
        # 跑完整五阶段
        await run_benchmark(
            cfg=cfg,
            all_accounts=accounts,
            proxies=proxies,
            start_phase=args.phase,
            results_path=str(script_dir / "bench_results.jsonl"),
        )

    METRICS.report(log)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[中断]")
