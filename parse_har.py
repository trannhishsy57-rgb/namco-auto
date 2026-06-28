"""Parse HAR file to extract checkout flow (POST requests + page navigations)"""
import json, sys, os

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

HAR_FILE = r"c:\Users\da983\Downloads\parks2.b2andainamco-am.co.jp.har"

with open(HAR_FILE, "r", encoding="utf-8") as f:
    har = json.load(f)

entries = har["log"]["entries"]
print(f"Total entries: {len(entries)}\n")

# Filter: only POST requests + HTML page GETs (skip static assets)
skip_ext = ('.png', '.jpg', '.jpeg', '.gif', '.css', '.js', '.woff', '.woff2', '.svg', '.ico', '.webp')

print("=" * 80)
print("  CHECKOUT FLOW - POST requests + HTML navigations")
print("=" * 80)

for i, entry in enumerate(entries):
    req = entry["request"]
    resp = entry["response"]
    url = req["url"]
    method = req["method"]
    status = resp["status"]
    mime = resp.get("content", {}).get("mimeType", "")

    # Skip static assets
    if any(url.lower().endswith(ext) for ext in skip_ext):
        continue
    if any(x in url for x in ['googletagmanager', 'google-analytics', 'gtm.js',
                                'rcmd.jp', 'onetrust', 'cookielaw', 'socialplus',
                                'fonts.googleapis', 'gstatic']):
        continue

    # Only show POST or HTML pages
    is_post = method == "POST"
    is_html = 'html' in mime or 'html' in url.split('?')[0]
    is_json_api = 'json' in mime

    if not (is_post or is_html or is_json_api):
        continue

    # Focus on checkout-related URLs
    checkout_keywords = ['cart', 'login', 'seisan', 'order', 'confirm', 'complete',
                         'payment', 'address', 'delivery', 'checkout', 'item',
                         'top_login', 'category']
    url_lower = url.lower()
    is_relevant = any(kw in url_lower for kw in checkout_keywords) or is_post

    if not is_relevant:
        continue

    print(f"\n[{i:4d}] {method} {status} {url}")

    if is_post:
        # Print POST body
        post_data = req.get("postData", {})
        if post_data:
            params = post_data.get("params", [])
            text = post_data.get("text", "")
            if params:
                print(f"       POST params:")
                for p in params:
                    name = p.get("name", "")
                    value = p.get("value", "")
                    if name.upper() == "PASSWORD":
                        value = "***"
                    print(f"         {name}={value}")
            elif text:
                print(f"       POST body: {text[:300]}")

    # Print redirect
    if status in (301, 302, 303):
        for h in resp.get("headers", []):
            if h["name"].lower() == "location":
                print(f"       -> Redirect: {h['value']}")

    # Print response title/snippet
    content = resp.get("content", {})
    body_text = content.get("text", "")
    if body_text and 'html' in mime:
        # Extract title
        import re
        title_match = re.search(r'<title>(.*?)</title>', body_text, re.DOTALL)
        if title_match:
            print(f"       Title: {title_match.group(1).strip()[:100]}")

print("\n" + "=" * 80)
print("  DONE")
print("=" * 80)
