"""Inspect the ticket detail page cart form to find correct request value"""
import requests, urllib3, sys
urllib3.disable_warnings()
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from bs4 import BeautifulSoup

s = requests.Session()
s.verify = False

s.get('https://parks2.bandainamco-am.co.jp/login.html')
s.post('https://parks2.bandainamco-am.co.jp/top_login.html', data={
    'request': 'logon', 'redirectTo': '',
    'LOGINID': 'rekv68w80t@livee.email', 'PASSWORD': 'Qwe123456'
}, allow_redirects=True)

r = s.get('https://parks2.bandainamco-am.co.jp/category/EL/ECCL00000043_20260704_05_023.html')
soup = BeautifulSoup(r.text, 'html.parser')

print("=== ALL FORMS ===")
for i, form in enumerate(soup.select('form')):
    action = form.get('action', '')
    method = form.get('method', '')
    print(f"\nForm[{i}] action={action} method={method}")
    for el in form.select('input,select,button'):
        tag = el.name
        n = el.get('name', '')
        t = el.get('type', '')
        v = el.get('value', '')
        oc = el.get('onclick', '')
        print(f"  <{tag}> name={n} type={t} value={v}" + (f" onclick={oc}" if oc else ""))
    for sc in form.select('script'):
        txt = (sc.string or '').strip()
        if txt:
            print(f"  <script>: {txt[:500]}")

# Also check for JS that sets request field
print("\n=== JS CART FUNCTIONS ===")
for sc in soup.select('script'):
    txt = sc.string or ''
    if 'cart' in txt.lower() or 'request' in txt.lower():
        lines = [l.strip() for l in txt.split('\n') if 'cart' in l.lower() or 'request' in l.lower()]
        for l in lines[:20]:
            print(f"  {l}")
