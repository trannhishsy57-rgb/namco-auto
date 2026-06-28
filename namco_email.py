"""
Namco Parks 抽选结果邮件解析器

功能：
  1. 通过 IMAP 连接邮箱，扫描 Namco Parks 抽选结果邮件
  2. 识别 当选（中签）/ 落选（未中）
  3. 输出中签账号列表 → winners.json

使用：
  python namco_email.py --config config.toml [--since 2026-07-04] [--output winners.json]

邮件关键词参考（Namco Parks）：
  Subject: 抽選結果のご通知
  当选: 当選 / 当せん / ご当選 / 当選おめでとう
  落选: 落選 / 落せん / 今回は当選されませんでした

支持的邮件服务商:
  Gmail:    imap.gmail.com:993
  Outlook:  outlook.office365.com:993
  livee:    imap.livee.email:993 (或查看服务商文档)
"""

from __future__ import annotations

import argparse
import email
import email.header
import imaplib
import json
import re
import sys
import tomllib
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional


# ── 邮件关键词 ──────────────────────────────────────────────────────────────
_SUBJECT_KEYWORDS = ["抽選結果", "抽選", "当選", "落選", "ナムコパークス", "parks"]
_WIN_KEYWORDS     = ["当選", "当せん", "ご当選", "当選おめでとう", "当選されました"]
_LOSE_KEYWORDS    = ["落選", "落せん", "当選されませんでした", "当選できませんでした"]
_SENDER_HINTS     = ["namco", "bandainamco", "parks"]


# ── 数据结构 ────────────────────────────────────────────────────────────────
@dataclass
class LotteryResult:
    email: str
    result: str          # "win" | "lose" | "unknown"
    subject: str
    received_at: str
    order_number: str = ""
    raw_snippet: str = ""


# ── IMAP 连接 ───────────────────────────────────────────────────────────────
def _decode_header(h: str) -> str:
    parts = email.header.decode_header(h)
    decoded = []
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return "".join(decoded)


def _get_body(msg: email.message.Message) -> str:
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body += payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return body


def _classify(subject: str, body: str) -> str:
    text = subject + " " + body
    if any(kw in text for kw in _WIN_KEYWORDS):
        return "win"
    if any(kw in text for kw in _LOSE_KEYWORDS):
        return "lose"
    return "unknown"


def _extract_order(text: str) -> str:
    m = re.search(r"EC-\d{10,}", text)
    return m.group(0) if m else ""


def scan_mailbox(
    host: str,
    port: int,
    email_addr: str,
    password: str,
    since: Optional[date] = None,
    mailbox: str = "INBOX",
) -> List[LotteryResult]:
    results: List[LotteryResult] = []
    try:
        conn = imaplib.IMAP4_SSL(host, port)
        conn.login(email_addr, password)
        conn.select(mailbox)
    except Exception as e:
        print(f"  [IMAP ERROR] {email_addr}: {e}", file=sys.stderr)
        return results

    try:
        since_str = since.strftime("%d-%b-%Y") if since else "01-Jan-2020"
        _, msg_ids = conn.search(None, f'(SINCE "{since_str}")')
        ids = msg_ids[0].split() if msg_ids[0] else []

        for mid in ids:
            _, data = conn.fetch(mid, "(RFC822)")
            raw = data[0][1] if data and data[0] else None
            if not raw:
                continue
            msg = email.message_from_bytes(raw)

            subject = _decode_header(msg.get("Subject", ""))
            sender  = msg.get("From", "")
            date_hdr = msg.get("Date", "")

            # Filter: must be from Namco or contain lottery keywords
            if not any(kw.lower() in sender.lower() for kw in _SENDER_HINTS):
                if not any(kw in subject for kw in _SUBJECT_KEYWORDS):
                    continue

            body = _get_body(msg)
            result = _classify(subject, body)
            order = _extract_order(subject + body)

            results.append(LotteryResult(
                email=email_addr,
                result=result,
                subject=subject[:120],
                received_at=date_hdr,
                order_number=order,
                raw_snippet=(body[:300].replace("\n", " ")),
            ))
    finally:
        try:
            conn.logout()
        except Exception:
            pass

    return results


# ── 配置加载 ────────────────────────────────────────────────────────────────
@dataclass
class EmailAccount:
    email: str
    password: str
    imap_host: str = ""
    imap_port: int = 993

    def auto_host(self) -> str:
        if self.imap_host:
            return self.imap_host
        domain = self.email.split("@")[-1].lower()
        _hosts = {
            "gmail.com":        "imap.gmail.com",
            "googlemail.com":   "imap.gmail.com",
            "outlook.com":      "outlook.office365.com",
            "hotmail.com":      "outlook.office365.com",
            "yahoo.co.jp":      "imap.mail.yahoo.co.jp",
            "livee.email":      "imap.livee.email",
        }
        return _hosts.get(domain, f"imap.{domain}")


def load_accounts(cfg_path: str) -> List[EmailAccount]:
    with open(cfg_path, "rb") as f:
        raw = tomllib.load(f)
    email_cfg = raw.get("email_check", {})
    accs = []
    for a in raw.get("accounts", []):
        host = email_cfg.get("imap_host", "")
        port = email_cfg.get("imap_port", 993)
        accs.append(EmailAccount(
            email=a["email"],
            password=a["password"],
            imap_host=host,
            imap_port=port,
        ))
    return accs


# ── 主逻辑 ──────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Namco Parks 抽选结果邮件扫描")
    ap.add_argument("--config",  default="config.toml", help="配置文件路径")
    ap.add_argument("--since",   default="",   help="扫描此日期之后的邮件 YYYY-MM-DD")
    ap.add_argument("--output",  default="winners.json", help="输出文件")
    ap.add_argument("--mailbox", default="INBOX")
    args = ap.parse_args()

    since = None
    if args.since:
        try:
            since = date.fromisoformat(args.since)
        except ValueError:
            print(f"Invalid --since date: {args.since}", file=sys.stderr)
            sys.exit(1)

    cfg_path = str(Path(args.config).resolve())
    accounts = load_accounts(cfg_path)
    if not accounts:
        print("No accounts found in config.toml", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning {len(accounts)} mailboxes …")
    all_results: List[LotteryResult] = []

    for acc in accounts:
        host = acc.auto_host()
        print(f"  {acc.email}  →  {host}:{acc.imap_port}")
        results = scan_mailbox(host, acc.imap_port, acc.email, acc.password, since, args.mailbox)
        all_results.extend(results)
        for r in results:
            tag = "✓ WIN " if r.result == "win" else ("✗ LOSE" if r.result == "lose" else "? UNK ")
            print(f"    [{tag}] {r.subject[:60]}  order={r.order_number or '-'}")

    winners = [r for r in all_results if r.result == "win"]
    losers  = [r for r in all_results if r.result == "lose"]

    print(f"\n{'='*55}")
    print(f"  Total emails: {len(all_results)}")
    print(f"  当選 (WIN):   {len(winners)}")
    print(f"  落選 (LOSE):  {len(losers)}")
    print(f"  Unknown:      {len(all_results) - len(winners) - len(losers)}")
    print(f"{'='*55}")

    if winners:
        print("\n当選アカウント一覧:")
        for w in winners:
            print(f"  {w.email}  order={w.order_number or 'N/A'}")

    out = {
        "scanned_at": datetime.now().isoformat(),
        "since": str(since) if since else "all",
        "total": len(all_results),
        "winners": [asdict(w) for w in winners],
        "losers":  [asdict(l) for l in losers],
        "unknown": [asdict(r) for r in all_results if r.result == "unknown"],
    }
    output_path = Path(args.output)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n→ 结果已保存: {output_path.resolve()}")


if __name__ == "__main__":
    main()
