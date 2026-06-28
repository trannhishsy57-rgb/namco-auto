"""
Namco Parks 抽選結果チェッカー（登录式，不依赖邮件）

思路：登录每个账号 → 打开「購入履歴」(member_history.html) →
      读取每个注文番号(EC-xxxx)的状态标签 → 判定 当選/落選/抽選前。

为什么不用邮件：
  - 落選通常不发邮件，只能登录看
  - 登录态本来就有，比配 IMAP 密码更省事、更可靠

状态标签来源：
  <span class="block-mypage-history-block-status-icon ...">抽選前</span>
  开奖前 = 抽選前 / 抽選中
  中签   = 当選
  落选   = 落選 / 抽選対象外

使用：
  python namco_result.py                          # 用 config.toml 全部账号
  python namco_result.py --output winners.json
  python namco_result.py --concurrent 10

依赖 namco_prod.py（复用登录 + 会话逻辑）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from bs4 import BeautifulSoup

from namco_prod import (
    AppConfig,
    ManagedSession,
    ProxyPool,
    step_login,
    setup_logging,
    BASE_URL,
    _HTML_PARSER,
)

HISTORY_URL = "/member_history.html"

# 状态关键词分类
_WIN_KW     = ["当選", "当せん", "ご当選"]
_LOSE_KW    = ["落選", "落せん", "抽選対象外", "ご当選されませんでした"]
_PENDING_KW = ["抽選前", "抽選中", "抽選申込", "受付中"]


@dataclass
class OrderResult:
    account: str
    order_number: str
    status_label: str          # 原始状态文字
    result: str                # win | lose | pending | unknown
    item: str = ""


@dataclass
class AccountReport:
    account: str
    login_ok: bool
    orders: List[OrderResult] = field(default_factory=list)
    error: str = ""


def _classify(label: str) -> str:
    # LOSE MUST be checked before WIN. The polite lose phrase "ご当選されませんでした"
    # ("you were not selected") contains the substring "当選" — so a substring WIN
    # check run first would mark a losing account as a winner and we'd hand a loser's
    # credentials to the customer. Lose phrases are the more specific match, so they win.
    if any(k in label for k in _LOSE_KW):
        return "lose"
    if any(k in label for k in _WIN_KW):
        return "win"
    if any(k in label for k in _PENDING_KW):
        return "pending"
    return "unknown"


def parse_history(html: str) -> List[OrderResult]:
    """从購入履歴 HTML 提取每个订单的 (注文番号, 状态, 商品名)。"""
    soup = BeautifulSoup(html, _HTML_PARSER)
    results: List[OrderResult] = []

    # 每个订单块包含一个 status-icon span 和一个 EC-xxxx 注文番号
    status_spans = soup.select("span.block-mypage-history-block-status-icon")

    for span in status_spans:
        label = span.get_text(strip=True)
        # 向上找包含注文番号的容器
        order_no = ""
        item = ""
        container = span
        for _ in range(8):
            container = container.parent
            if container is None:
                break
            text = container.get_text(" ", strip=True)
            m = re.search(r"EC-\d+", text)
            if m:
                order_no = m.group(0)
                # 商品名：找含「チケット」或活动关键词的文字
                im = re.search(r"(【[^】]*】[^\n]{0,80}(?:チケット|OP-\d+)[^\n]{0,40})", text)
                if im:
                    item = im.group(1)[:120]
                break
        if order_no:
            results.append(OrderResult(
                account="",
                order_number=order_no,
                status_label=label,
                result=_classify(label),
                item=item,
            ))

    # 去重（同一订单可能匹配多次）
    seen = set()
    uniq = []
    for r in results:
        if r.order_number not in seen:
            seen.add(r.order_number)
            uniq.append(r)
    return uniq


async def check_account(acc, cfg: AppConfig, sem: asyncio.Semaphore) -> AccountReport:
    import logging
    log = logging.getLogger(f"namco.result.{acc.email[:20]}")
    async with sem:
        report = AccountReport(account=acc.email, login_ok=False)
        try:
            async with ManagedSession(acc.proxy or "", cfg, acc.email) as s:
                if not await step_login(s, acc.email, acc.password):
                    report.error = "login failed"
                    log.error(f"{acc.email}: login failed")
                    return report
                report.login_ok = True

                resp = await s.get(HISTORY_URL)
                orders = parse_history(resp.text)
                for o in orders:
                    o.account = acc.email
                report.orders = orders

                summary = ", ".join(f"{o.order_number}={o.status_label}" for o in orders) or "no orders"
                log.info(f"{acc.email}: {summary}")
        except Exception as e:
            report.error = str(e)
            log.exception(f"{acc.email}: {e}")
        return report


async def main_async(args):
    script_dir = Path(__file__).parent
    cfg = AppConfig.load(str(script_dir / "config.toml"))
    log = setup_logging(cfg.log_level)

    concurrent = args.concurrent or cfg.max_concurrent
    log.info(f"结果查询 | accounts={len(cfg.accounts)} concurrent={concurrent}")

    sem = asyncio.Semaphore(concurrent)
    reports: List[AccountReport] = await asyncio.gather(
        *[check_account(acc, cfg, sem) for acc in cfg.accounts]
    )

    # 汇总
    all_orders: List[OrderResult] = [o for r in reports for o in r.orders]
    wins    = [o for o in all_orders if o.result == "win"]
    loses   = [o for o in all_orders if o.result == "lose"]
    pending = [o for o in all_orders if o.result == "pending"]
    unknown = [o for o in all_orders if o.result == "unknown"]
    login_fail = [r for r in reports if not r.login_ok]

    print("\n" + "=" * 60)
    print("  NAMCO PARKS 抽選結果")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    print(f"  账号总数:   {len(reports)}")
    print(f"  登录失败:   {len(login_fail)}")
    print(f"  订单总数:   {len(all_orders)}")
    print(f"  当選 WIN:   {len(wins)}")
    print(f"  落選 LOSE:  {len(loses)}")
    print(f"  抽選前:     {len(pending)}")
    print(f"  未知状态:   {len(unknown)}")
    print("=" * 60)

    if wins:
        print("\n🎉 当選アカウント:")
        acc_map = {a.email: a.password for a in cfg.accounts}
        for o in wins:
            print(f"  {o.account:<35} {o.order_number}  pwd={acc_map.get(o.account,'?')}")
    if pending:
        print(f"\n⏳ 仍在抽選前（结果未公布）: {len(pending)} 单")
    if unknown:
        print("\n⚠️  未知状态（需人工确认）:")
        for o in unknown:
            print(f"  {o.account:<35} {o.order_number}  状态='{o.status_label}'")

    # 写文件
    acc_map = {a.email: a.password for a in cfg.accounts}
    out = {
        "checked_at": datetime.now().isoformat(),
        "total_accounts": len(reports),
        "total_orders": len(all_orders),
        "counts": {"win": len(wins), "lose": len(loses), "pending": len(pending), "unknown": len(unknown)},
        "winners": [{**asdict(o), "password": acc_map.get(o.account, "")} for o in wins],
        "pending": [asdict(o) for o in pending],
        "losers":  [asdict(o) for o in loses],
        "unknown": [asdict(o) for o in unknown],
        "login_failed": [r.account for r in login_fail],
    }
    out_path = Path(args.output)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n→ 结果已保存: {out_path.resolve()}")


def main():
    ap = argparse.ArgumentParser(description="Namco Parks 登录式抽选结果查询")
    ap.add_argument("--output", default="winners.json", help="输出文件")
    ap.add_argument("--concurrent", type=int, default=0, help="并发数（默认用config的max_concurrent）")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
