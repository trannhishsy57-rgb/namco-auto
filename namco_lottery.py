"""
Namco Parks Online Store - OP-16 Full Checkout Automation v3
Login -> Add to Cart -> Checkout -> Confirm -> Complete Order
"""

import requests
import json
import time
import os
import sys
import random
import re
import urllib3
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ============ CONFIG ============
CONFIG = {
    "email": "rekv68w80t@livee.email",
    "password": "Qwe123456",
}

BASE_URL = "https://parks2.bandainamco-am.co.jp"
LOGIN_URL = f"{BASE_URL}/top_login.html"
TICKET_CATEGORY_URL = f"{BASE_URL}/category/EL/"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HAR_LOG_FILE = os.path.join(SCRIPT_DIR, "namco_har_log.json")

# --- Mode ---
# "dry"      = parse only, no submit
# "cart"     = add to cart only, stop before checkout
# "checkout" = full flow: cart -> seisan -> confirm -> complete
MODE = "checkout"

TARGET_STORES = ["\u5bcc\u5c71\u9ad8\u5ca1"]  # 富山高岡

DELAY_MIN = 3.0
DELAY_MAX = 6.0
MAX_RETRIES = 3
RETRY_DELAY = 10.0


# ============ HAR Logger ============
class HarLogger:
    def __init__(self):
        self.entries = []

    def log(self, method, url, req_headers, req_body, resp):
        self.entries.append({
            "startedDateTime": datetime.now(timezone.utc).isoformat(),
            "request": {
                "method": method, "url": url,
                "headers": dict(req_headers) if req_headers else {},
                "body": req_body or "",
            },
            "response": {
                "status": resp.status_code, "statusText": resp.reason,
                "headers": dict(resp.headers),
                "cookies": dict(resp.cookies),
                "bodySize": len(resp.content),
                "bodyPreview": resp.text[:2000] if resp.text else "",
            },
        })
        print(f"  [{resp.status_code}] {method} {url}")

    def save(self, path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"log": {"version": "1.2", "creator": {"name": "namco-auto", "version": "3.0"}, "entries": self.entries}}, f, ensure_ascii=False, indent=2)
        print(f"\n[HAR] Saved {len(self.entries)} entries -> {path}")


# ============ Logged Session ============
class LoggedSession:
    def __init__(self, har: HarLogger):
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en;q=0.9",
        })
        self.har = har

    def get(self, url, **kwargs):
        resp = self.session.get(url, **kwargs)
        self.har.log("GET", url, self.session.headers, None, resp)
        return resp

    def post(self, url, data=None, **kwargs):
        resp = self.session.post(url, data=data, **kwargs)
        body = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)
        self.har.log("POST", url, self.session.headers, body, resp)
        return resp

    def post_retry(self, url, data=None, **kwargs):
        for attempt in range(MAX_RETRIES):
            resp = self.post(url, data=data, **kwargs)
            if resp.status_code != 503:
                return resp
            wait = RETRY_DELAY * (attempt + 1)
            print(f"  [RETRY] 503, waiting {wait:.0f}s ({attempt+1}/{MAX_RETRIES})")
            time.sleep(wait)
        return resp

    def get_retry(self, url, **kwargs):
        for attempt in range(MAX_RETRIES):
            resp = self.get(url, **kwargs)
            if resp.status_code != 503:
                return resp
            wait = RETRY_DELAY * (attempt + 1)
            print(f"  [RETRY] 503, waiting {wait:.0f}s ({attempt+1}/{MAX_RETRIES})")
            time.sleep(wait)
        return resp

    @property
    def cookies(self):
        return self.session.cookies


def rand_delay():
    d = random.uniform(DELAY_MIN, DELAY_MAX)
    print(f"  [WAIT] {d:.1f}s")
    time.sleep(d)


def save_debug(name, text):
    path = os.path.join(SCRIPT_DIR, f"debug_{name}_{int(time.time())}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def check_error(soup):
    err = soup.select_one("#error .form-error-message, #error ul li, .errMsg")
    if err:
        t = err.get_text(strip=True)
        if t:
            return t[:300]
    return None


# ============ Step 1: Login ============
def login(s: LoggedSession):
    print("\n=== [Step 1] Login ===")

    login_page = s.get(f"{BASE_URL}/login.html")
    soup = BeautifulSoup(login_page.text, "html.parser")
    hidden = {}
    form = soup.select_one("form")
    if form:
        for inp in form.select('input[type="hidden"]'):
            name = inp.get("name")
            if name:
                hidden[name] = inp.get("value", "")

    payload = {"request": "logon", "redirectTo": "", "LOGINID": CONFIG["email"], "PASSWORD": CONFIG["password"]}
    payload.update(hidden)

    resp = s.post(LOGIN_URL, data=payload, allow_redirects=True)

    if "\u30ed\u30b0\u30a2\u30a6\u30c8" in resp.text or "\u30de\u30a4\u30da\u30fc\u30b8" in resp.text:
        print("  [OK] Login successful")
        return True

    path = save_debug("login", resp.text)
    print(f"  [FAIL] Login failed, saved -> {path}")
    return False


# ============ Step 2: List & Filter Tickets ============
def find_target_ticket(s: LoggedSession):
    print("\n=== [Step 2] Find target ticket ===")
    resp = s.get(TICKET_CATEGORY_URL)
    soup = BeautifulSoup(resp.text, "html.parser")

    tickets = []
    for a in soup.select("a"):
        text = a.get_text(strip=True)
        href = a.get("href", "")
        if "OP-16" in text and "\u62bd\u9078" in text and href:
            full_url = urljoin(BASE_URL, href)
            if full_url not in [t["url"] for t in tickets]:
                tickets.append({"name": text, "url": full_url})

    print(f"  Found {len(tickets)} OP-16 tickets")

    if TARGET_STORES:
        tickets = [t for t in tickets if any(kw in t["name"] for kw in TARGET_STORES)]
        print(f"  Filtered to {len(tickets)} matching {TARGET_STORES}")

    if tickets:
        target = tickets[0]
        print(f"  Target: {target['name']}")
        return target
    return None


# ============ Step 3: Add to Cart (AJAX) ============
def add_to_cart(s: LoggedSession, ticket):
    print(f"\n=== [Step 3] Add to cart ===")
    print(f"  {ticket['name']}")

    resp = s.get_retry(ticket["url"])
    if resp.status_code != 200:
        print(f"  [ERR] Page returned {resp.status_code}")
        return False

    soup = BeautifulSoup(resp.text, "html.parser")

    cart_form = None
    for form in soup.select("form"):
        if "cart" in form.get("action", "").lower():
            cart_form = form
            break

    if not cart_form:
        print("  [ERR] No cart form found")
        save_debug("no_cart_form", resp.text)
        return False

    form_data = {}
    for inp in cart_form.select("input"):
        name = inp.get("name")
        if name:
            form_data[name] = inp.get("value", "")

    for sel in cart_form.select("select"):
        name = sel.get("name")
        if name:
            opt = sel.select_one("option[selected]") or sel.select_one("option")
            form_data[name] = opt.get("value", "") if opt else ""

    form_data["request"] = "insert"
    print(f"  Form data: {json.dumps(form_data, ensure_ascii=False)}")

    rand_delay()

    resp = s.post_retry(f"{BASE_URL}/cart_index.html", data=form_data, allow_redirects=True)
    soup = BeautifulSoup(resp.text, "html.parser")

    err = check_error(soup)
    if err:
        print(f"  [ERR] {err}")
        save_debug("cart_error", resp.text)
        return False

    # AJAX mode returns JSON, form mode returns cart page
    if "\u30ab\u30fc\u30c8\u306b\u8ffd\u52a0" in resp.text or "\u30ab\u30fc\u30c8" in resp.text:
        print("  [OK] Added to cart")
        return True

    try:
        j = resp.json()
        print(f"  [OK] Cart JSON response: {json.dumps(j, ensure_ascii=False)[:200]}")
        return True
    except Exception:
        pass

    path = save_debug("cart_response", resp.text)
    print(f"  [WARN] Uncertain cart result, saved -> {path}")
    return True


# ============ Step 4: Checkout (cart_seisan) ============
def checkout(s: LoggedSession, ticket):
    print(f"\n=== [Step 4] Checkout (cart_seisan) ===")

    # First GET cart page to collect form fields
    resp = s.get_retry(f"{BASE_URL}/cart_index.html")
    soup = BeautifulSoup(resp.text, "html.parser")

    # Find the cart form (cartFrm or similar)
    cart_form = soup.select_one('form[name="cartFrm"]') or soup.select_one("form")
    form_data = {}
    if cart_form:
        for inp in cart_form.select("input"):
            name = inp.get("name")
            if name:
                form_data[name] = inp.get("value", "")
        for sel in cart_form.select("select"):
            name = sel.get("name")
            if name:
                opt = sel.select_one("option[selected]") or sel.select_one("option")
                form_data[name] = opt.get("value", "") if opt else ""

    if not form_data.get("CART_AMOUNT_0"):
        form_data["CART_AMOUNT_0"] = "1"
    form_data["CART_INDEX_REFERER"] = ticket["url"]

    print(f"  Cart form data: {json.dumps(form_data, ensure_ascii=False)}")

    rand_delay()

    resp = s.post_retry(f"{BASE_URL}/cart_seisan.html", data=form_data, allow_redirects=True)
    soup = BeautifulSoup(resp.text, "html.parser")

    err = check_error(soup)
    if err:
        print(f"  [ERR] {err}")
        save_debug("seisan_error", resp.text)
        return None

    path = save_debug("seisan", resp.text)
    print(f"  [OK] Checkout page loaded, saved -> {path}")
    return resp.text


# ============ Step 5: Confirm Order (cart_confirm) ============
def confirm_order(s: LoggedSession, seisan_html):
    print(f"\n=== [Step 5] Confirm order (cart_confirm) ===")

    soup = BeautifulSoup(seisan_html, "html.parser")

    # Collect ALL form fields from the seisan page
    # The seisan page has a big form with all address/personal info pre-filled
    forms = soup.select("form")
    target_form = None
    for form in forms:
        action = form.get("action", "")
        if "confirm" in action.lower() or "seisan" in action.lower():
            target_form = form
            break
    if not target_form and forms:
        # Use the largest form
        target_form = max(forms, key=lambda f: len(f.select("input")))

    if not target_form:
        print("  [ERR] No confirm form found")
        return None

    form_data = {}
    for inp in target_form.select("input"):
        name = inp.get("name")
        if not name:
            continue
        inp_type = inp.get("type", "").lower()
        if inp_type == "checkbox":
            if inp.get("checked") is not None:
                form_data[name] = inp.get("value", "1")
        elif inp_type == "radio":
            if inp.get("checked") is not None:
                form_data[name] = inp.get("value", "")
        else:
            form_data[name] = inp.get("value", "")

    for sel in target_form.select("select"):
        name = sel.get("name")
        if name:
            opt = sel.select_one("option[selected]") or sel.select_one("option")
            form_data[name] = opt.get("value", "") if opt else ""

    for ta in target_form.select("textarea"):
        name = ta.get("name")
        if name:
            form_data[name] = ta.string or ""

    if "request" in form_data:
        form_data["request"] = "confirm"

    # Log key fields (mask sensitive ones)
    safe_keys = {k: v for k, v in form_data.items() if "password" not in k.lower()}
    print(f"  Confirm form fields ({len(form_data)} total): {list(form_data.keys())}")

    rand_delay()

    resp = s.post_retry(f"{BASE_URL}/cart_confirm.html", data=form_data, allow_redirects=True)
    soup = BeautifulSoup(resp.text, "html.parser")

    err = check_error(soup)
    if err:
        print(f"  [ERR] {err}")
        save_debug("confirm_error", resp.text)
        return None

    path = save_debug("confirm", resp.text)

    if "\u78ba\u8a8d" in resp.text or "\u6ce8\u6587" in resp.text:
        print(f"  [OK] Order confirmation page loaded, saved -> {path}")
        return resp.text
    else:
        print(f"  [WARN] Unexpected response, saved -> {path}")
        return resp.text


# ============ Step 6: Pre-process & Complete ============
def complete_order(s: LoggedSession, confirm_html):
    print(f"\n=== [Step 6] Complete order ===")

    soup = BeautifulSoup(confirm_html, "html.parser")

    # Extract token from confirm page
    token_input = soup.select_one('input[name="token"]')
    token = token_input.get("value", "") if token_input else ""

    if not token:
        # Try to find token in any form
        for inp in soup.select("input"):
            if inp.get("name") == "token":
                token = inp.get("value", "")
                break

    if not token:
        print("  [ERR] No token found on confirm page")
        return None

    print(f"  Token: {token}")

    # Step 6a: cart_pre.html
    print("  [6a] POST cart_pre.html...")
    rand_delay()

    resp = s.post_retry(f"{BASE_URL}/cart_pre.html", data={
        "request": "cart_order_pre",
        "token": token,
        "mode": "0",
    }, allow_redirects=True)

    if resp.status_code != 200:
        print(f"  [ERR] cart_pre returned {resp.status_code}")
        save_debug("pre_error", resp.text)
        return None

    err_soup = BeautifulSoup(resp.text, "html.parser")
    err = check_error(err_soup)
    if err:
        print(f"  [ERR] {err}")
        save_debug("pre_error", resp.text)
        return None

    print("  [OK] Pre-processing done")

    # Step 6b: cart_complete.html
    print("  [6b] POST cart_complete.html...")
    rand_delay()

    resp = s.post_retry(f"{BASE_URL}/cart_complete.html", data={
        "token": token,
    }, allow_redirects=True)

    path = save_debug("complete", resp.text)

    # Check for order number
    order_match = re.search(r'EC-\d+', resp.text)
    order_num = order_match.group(0) if order_match else None

    if "\u6ce8\u6587\u5b8c\u4e86" in resp.text or order_num:
        print(f"  ========================================")
        print(f"  [OK] ORDER COMPLETE!")
        print(f"  Order Number: {order_num or 'N/A'}")
        print(f"  ========================================")
        return {"order_number": order_num, "debug_file": path}
    else:
        err_soup = BeautifulSoup(resp.text, "html.parser")
        err = check_error(err_soup)
        if err:
            print(f"  [FAIL] {err}")
        else:
            print(f"  [FAIL] Order completion uncertain, saved -> {path}")
        return None


# ============ Main ============
def main():
    print("=" * 60)
    print(f"  Namco Parks OP-16 Full Checkout v3")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Mode: {MODE} | Target: {TARGET_STORES or 'ALL'}")
    print("=" * 60)

    har = HarLogger()
    s = LoggedSession(har)

    # Step 1: Login
    if not login(s):
        har.save(HAR_LOG_FILE)
        return

    rand_delay()

    # Step 2: Find ticket
    ticket = find_target_ticket(s)
    if not ticket:
        print("\nNo matching ticket found.")
        har.save(HAR_LOG_FILE)
        return

    rand_delay()

    # Step 3: Add to cart
    if not add_to_cart(s, ticket):
        print("\nFailed to add to cart.")
        har.save(HAR_LOG_FILE)
        return

    if MODE == "cart":
        print("\n[MODE=cart] Stopped after adding to cart.")
        har.save(HAR_LOG_FILE)
        return

    if MODE == "dry":
        print("\n[MODE=dry] Dry run complete.")
        har.save(HAR_LOG_FILE)
        return

    rand_delay()

    # Step 4: Checkout
    seisan_html = checkout(s, ticket)
    if not seisan_html:
        print("\nCheckout failed.")
        har.save(HAR_LOG_FILE)
        return

    rand_delay()

    # Step 5: Confirm
    confirm_html = confirm_order(s, seisan_html)
    if not confirm_html:
        print("\nConfirm failed.")
        har.save(HAR_LOG_FILE)
        return

    rand_delay()

    # Step 6: Complete
    result = complete_order(s, confirm_html)

    har.save(HAR_LOG_FILE)

    # Final report
    print("\n" + "=" * 60)
    if result:
        print(f"  SUCCESS - Order: {result['order_number']}")
    else:
        print(f"  FAILED - Check debug files in {SCRIPT_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    main()
