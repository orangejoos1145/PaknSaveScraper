"""
PAK'nSAVE Price Explorer — scrape + site generator
---------------------------------------------------
Scrapes every department from paknsave.co.nz, writes a CSV, and builds a
single self-contained index.html (ready for GitHub Pages).

Local (Windows):   python paknsave_pipeline.py
Rebuild site only: python paknsave_pipeline.py --from-json
GitHub Actions:    runs automatically via .github/workflows/daily-scrape.yml

Notes
- PAK'nSAVE's API has no "was" price, so the site shows ON SPECIAL rather
  than a made-up discount %.
- Categories come from the real scraped department/subcategory data.
- Product images are opt-in on the site (toggle, off by default). The image
  URL pattern (IMAGE_URL_TMPL) is Foodstuffs' public image CDN and is not
  returned by the API; if images never appear, check one of those URLs in a
  browser and adjust the template.
"""

import csv
import json
import os
import sys
import time
import webbrowser
from datetime import datetime, timezone

# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------

SITE_URL = "https://www.paknsave.co.nz/shop/category/fruit-and-vegetables"
CATEGORIES_URL_TMPL = "https://api-prod.paknsave.co.nz/v1/edge/store/{store_id}/categories"
SEARCH_URL = "https://api-prod.paknsave.co.nz/v1/edge/search/paginated/products"
STORE_ID = "2a1b331a-fc4a-496a-b072-e97cc8f70cae"

SKIP_CATEGORIES = ["Featured", "Makeaways"]

# 200x200 keeps bandwidth low; {num} is the numeric part of the product ID.
IMAGE_URL_TMPL = "https://a.fsimg.co.nz/product/retail/fan/image/200x200/{num}.png"

RAW_JSON = "pns_all_products.json"
OUTPUT_CSV = "paknsave_products.csv"
OUTPUT_HTML = "index.html"

HITS_PER_PAGE = 50
MAX_PAGES_PER_CATEGORY = 25
REQUEST_PAUSE_S = 0.15

IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"

BRAVE_CANDIDATE_PATHS = [
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
]
BRAVE_PATH_OVERRIDE = None


# ----------------------------------------------------------------------
# Browser / session
# ----------------------------------------------------------------------

def find_brave():
    if BRAVE_PATH_OVERRIDE:
        return BRAVE_PATH_OVERRIDE
    for path in BRAVE_CANDIDATE_PATHS:
        if os.path.exists(path):
            return path
    return None


class Session:
    def __init__(self, page):
        self.page = page
        self.token = None

    def refresh_token(self):
        captured = {"value": None}
        self.api_calls = 0

        def on_request(request):
            if "api-prod.paknsave.co.nz" in request.url:
                self.api_calls += 1
                auth = request.headers.get("authorization")
                if auth and captured["value"] is None:
                    captured["value"] = auth

        self.page.on("request", on_request)
        try:
            resp = self.page.goto(SITE_URL, wait_until="domcontentloaded", timeout=60000)
            self.last_status = resp.status if resp else None
        except Exception as e:
            self.last_status = f"page load failed: {e}"
        # Wait up to 30s for an authenticated API call instead of a fixed 5s.
        for _ in range(60):
            if captured["value"]:
                break
            self.page.wait_for_timeout(500)
        self.page.remove_listener("request", on_request)
        self.token = captured["value"]
        return self.token

    def _headers(self):
        return {"accept": "application/json", "content-type": "application/json",
                "authorization": self.token}

    def post(self, url, payload):
        resp = self.page.context.request.post(url, data=payload, headers=self._headers())
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status} for {url}")
        return resp.json()

    def get(self, url):
        resp = self.page.context.request.get(url, headers=self._headers())
        if not resp.ok:
            raise RuntimeError(f"HTTP {resp.status} for {url}")
        return resp.json()


def fetch_categories(session):
    data = session.get(CATEGORIES_URL_TMPL.format(store_id=STORE_ID))
    names = []
    if isinstance(data, list):
        for node in data:
            name = node.get("name")
            if name and name not in SKIP_CATEGORIES:
                names.append(name)
    return names


def search_page(session, category_name, page_num):
    payload = {
        "algoliaQuery": {
            "facets": ["brand", "category1NI", "onPromotion"],
            "filters": f'stores:{STORE_ID} AND category0NI:"{category_name}"',
            "hitsPerPage": HITS_PER_PAGE,
            "page": page_num,
        },
        "storeId": STORE_ID,
        "hitsPerPage": HITS_PER_PAGE,
        "page": page_num,
        "sortOrder": "NI_POPULARITY_ASC",
    }
    return session.post(SEARCH_URL, payload)


def scrape_all(session, categories):
    all_products, seen_ids = [], set()
    for category_name in categories:
        print(f"\n{category_name}:")
        page_num, retried = 0, False
        while page_num < MAX_PAGES_PER_CATEGORY:
            try:
                data = search_page(session, category_name, page_num)
                retried = False
            except RuntimeError as e:
                if not retried:
                    print(f"  {e} — refreshing session and retrying once...")
                    session.refresh_token()
                    retried = True
                    continue
                print(f"  Still failing ({e}), skipping this department.")
                break
            except Exception as e:
                print(f"  Request failed: {e}")
                break

            products = data.get("products", [])
            if not products:
                break
            new = 0
            for p in products:
                pid = p.get("productId")
                if pid and pid not in seen_ids:
                    seen_ids.add(pid)
                    all_products.append(p)
                    new += 1
            print(f"  page {page_num}: {len(products)} items ({new} new)")

            page_num += 1
            if page_num >= data.get("totalPages", 1):
                break
            time.sleep(REQUEST_PAUSE_S)
    return all_products


def run_scrape():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        if IN_CI:
            # No Brave on GitHub runners; Playwright's Chromium runs headed
            # under xvfb (see the workflow), since headless tends to get blocked.
            browser = p.chromium.launch(headless=False)
        else:
            brave_path = find_brave()
            if not brave_path:
                print("Couldn't find brave.exe — set BRAVE_PATH_OVERRIDE near the top.")
                return None
            browser = p.chromium.launch(headless=False, executable_path=brave_path)

        context = browser.new_context(viewport={"width": 1366, "height": 900})
        page = context.new_page()
        session = Session(page)

        print("Getting a fresh session (token + cookies)...")
        if not session.refresh_token():
            print("Couldn't capture an auth token.")
            print(f"  Page HTTP status: {session.last_status}")
            print(f"  Final URL: {page.url}")
            try:
                print(f"  Page title: {page.title()!r}")
            except Exception:
                pass
            print(f"  Requests seen to api-prod: {session.api_calls}")
            try:
                page.screenshot(path="debug_screenshot.png", full_page=True)
                with open("debug_page.html", "w", encoding="utf-8") as f:
                    f.write(page.content())
                print("  Saved debug_screenshot.png and debug_page.html")
            except Exception as e:
                print(f"  Couldn't save debug files: {e}")
            browser.close()
            return None
        print("Got session.")

        categories = fetch_categories(session)
        if not categories:
            print("No departments found — check STORE_ID.")
            browser.close()
            return None
        print(f"Departments: {categories}")

        products = scrape_all(session, categories)
        browser.close()
    return products


# ----------------------------------------------------------------------
# Data shaping
# ----------------------------------------------------------------------

def product_link(product_id, name):
    slug = product_id.lower().replace("-", "_") + "pns"
    return f"https://www.paknsave.co.nz/shop/product/{slug}?name={name.replace(' ', '-').lower()}"


def shape_item(item):
    product_id = item.get("productId", "")
    name = item.get("name", "")
    price_cents = (item.get("singlePrice") or {}).get("price")
    promotions = item.get("promotions") or []
    trees = item.get("categoryTrees") or []
    num = product_id.split("-")[0] if product_id else ""

    return {
        "id": product_id,
        "name": name,
        "size": item.get("displayName", ""),
        "price": price_cents / 100 if isinstance(price_cents, (int, float)) else None,
        "special": bool(promotions),
        # Only a multibuy adds info beyond the ON SPECIAL badge.
        "promo": "Multibuy deal" if (promotions and promotions[0].get("multiProducts")) else "",
        "dept": trees[0].get("level0", "") if trees else "",
        "sub": trees[0].get("level1", "") if trees else "",
        "link": product_link(product_id, name) if product_id else "",
        "image": IMAGE_URL_TMPL.format(num=num) if num else "",
    }


def write_csv(products):
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Product ID", "Name", "Size", "Price", "On Special", "Promo Note",
                    "Department", "Subcategory", "Link"])
        for item in products:
            s = shape_item(item)
            if s["name"]:
                w.writerow([s["id"], s["name"], s["size"],
                            s["price"] if s["price"] is not None else "",
                            "Yes" if s["special"] else "No",
                            s["promo"], s["dept"], s["sub"], s["link"]])


def write_html(products, scraped_at_iso):
    items = []
    for item in products:
        s = shape_item(item)
        if s["name"]:
            s.pop("id")
            items.append(s)
    data = json.dumps(items, separators=(",", ":")).replace("</", "<\\/")
    html = (HTML_TEMPLATE
            .replace("__PRODUCTS_JSON__", data)
            .replace("__SCRAPED_AT__", scraped_at_iso))
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)


# ----------------------------------------------------------------------
# HTML template
# ----------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>PAK'nSAVE Price Explorer</title>
<meta name="description" content="Browse and search every PAK'nSAVE product and special, updated daily.">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🛒</text></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="preconnect" href="https://a.fsimg.co.nz" crossorigin>
<link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.1/css/all.min.css" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
<style>
  :root {
    --yellow: #ffd600;
    --yellow-deep: #f2c500;
    --yellow-glow: rgba(255, 214, 0, 0.22);
    --bg: #0b0b0c;
    --bg-2: #121214;
    --surface: #17171a;
    --surface-2: #1e1e22;
    --border: rgba(255, 255, 255, 0.08);
    --border-strong: rgba(255, 255, 255, 0.16);
    --text: #f5f5f4;
    --text-muted: #a3a3a0;
    --special: #3ddc84;
    --special-bg: rgba(61, 220, 132, 0.12);
    --header-bg: rgba(11, 11, 12, 0.78);
    --shadow: 0 10px 30px rgba(0, 0, 0, 0.45);
    --input-bg: rgba(255, 255, 255, 0.05);
    --radius: 14px;
    --ease: cubic-bezier(0.22, 1, 0.36, 1);
  }
  [data-theme="light"] {
    --yellow-glow: rgba(242, 197, 0, 0.25);
    --bg: #f6f5f1;
    --bg-2: #eeece6;
    --surface: #ffffff;
    --surface-2: #faf9f6;
    --border: rgba(0, 0, 0, 0.08);
    --border-strong: rgba(0, 0, 0, 0.16);
    --text: #141412;
    --text-muted: #62625c;
    --special: #13803f;
    --special-bg: rgba(19, 128, 63, 0.1);
    --header-bg: rgba(255, 255, 255, 0.82);
    --shadow: 0 10px 30px rgba(20, 20, 18, 0.08);
    --input-bg: rgba(0, 0, 0, 0.04);
  }

  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html { scroll-behavior: smooth; }
  body {
    margin: 0; min-height: 100vh; font-family: 'Poppins', system-ui, sans-serif;
    color: var(--text); background: var(--bg);
    background-image:
      radial-gradient(1000px 500px at 10% -10%, var(--yellow-glow), transparent 60%),
      radial-gradient(800px 400px at 110% 10%, rgba(255, 214, 0, 0.06), transparent 60%);
    background-attachment: fixed;
    transition: background-color .4s ease, color .4s ease;
    display: flex; flex-direction: column;
  }
  button, input, select { font-family: inherit; }
  ::selection { background: var(--yellow); color: #111; }
  ::-webkit-scrollbar { width: 10px; height: 10px; }
  ::-webkit-scrollbar-thumb { background: var(--border-strong); border-radius: 99px; border: 2px solid var(--bg); }

  /* ---------- Header ---------- */
  header {
    position: sticky; top: 0; z-index: 50;
    display: flex; align-items: center; gap: 1rem;
    padding: .85rem 1.75rem; padding-top: calc(.85rem + env(safe-area-inset-top));
    background: var(--header-bg);
    backdrop-filter: saturate(180%) blur(16px); -webkit-backdrop-filter: saturate(180%) blur(16px);
    border-bottom: 1px solid var(--border);
  }
  header::after {
    content: ""; position: absolute; left: 0; right: 0; bottom: -1px; height: 2px;
    background: linear-gradient(90deg, transparent, var(--yellow), transparent);
    opacity: .7;
  }
  .brand {
    display: flex; align-items: center; gap: .6rem; cursor: pointer; user-select: none;
    border: none; background: none; padding: 0; color: var(--text);
  }
  .brand-mark {
    width: 40px; height: 40px; border-radius: 11px; display: grid; place-items: center;
    background: var(--yellow); color: #111; font-size: 1.1rem;
    box-shadow: 0 6px 18px var(--yellow-glow); transition: transform .3s var(--ease);
  }
  .brand:hover .brand-mark { transform: rotate(-8deg) scale(1.06); }
  .brand-text { display: flex; flex-direction: column; line-height: 1.05; text-align: left; }
  .brand-text strong { font-size: 1.15rem; font-weight: 900; letter-spacing: .3px; }
  .brand-text strong span { color: var(--yellow); }
  .brand-text small { font-size: .68rem; color: var(--text-muted); font-weight: 500; letter-spacing: .6px; text-transform: uppercase; }

  .search { flex: 1; max-width: 560px; margin: 0 auto; position: relative; }
  .search i.fa-magnifying-glass { position: absolute; left: 1rem; top: 50%; transform: translateY(-50%); color: var(--text-muted); pointer-events: none; }
  .search input {
    width: 100%; padding: .75rem 5.2rem .75rem 2.7rem; border-radius: 99px;
    border: 1px solid var(--border-strong); background: var(--input-bg); color: var(--text);
    font-size: .95rem; outline: none; transition: border-color .25s, box-shadow .25s, background .25s;
  }
  .search input::placeholder { color: var(--text-muted); }
  .search input:focus { border-color: var(--yellow); box-shadow: 0 0 0 4px var(--yellow-glow); background: var(--surface); }
  .search-kbd {
    position: absolute; right: .6rem; top: 50%; transform: translateY(-50%);
    font-size: .7rem; font-weight: 600; color: var(--text-muted); border: 1px solid var(--border-strong);
    padding: 2px 8px; border-radius: 6px; pointer-events: none;
  }
  .search-clear {
    position: absolute; right: .5rem; top: 50%; transform: translateY(-50%);
    border: none; background: var(--surface-2); color: var(--text); width: 28px; height: 28px;
    border-radius: 50%; cursor: pointer; display: none;
  }
  .search.has-value .search-clear { display: grid; place-items: center; }
  .search.has-value .search-kbd { display: none; }

  .header-actions { display: flex; align-items: center; gap: .6rem; }
  .icon-btn {
    width: 40px; height: 40px; border-radius: 12px; border: 1px solid var(--border-strong);
    background: var(--surface); color: var(--text); cursor: pointer; display: grid; place-items: center;
    font-size: 1rem; transition: all .25s var(--ease);
  }
  .icon-btn:hover { background: var(--yellow); color: #111; border-color: var(--yellow); transform: translateY(-1px); }
  .scrape-badge {
    display: flex; align-items: center; gap: .45rem; height: 40px; padding: 0 .85rem;
    border-radius: 12px; border: 1px solid var(--border-strong); background: var(--surface);
    color: var(--text); font-size: .76rem; font-weight: 600; white-space: nowrap;
  }
  .scrape-badge i { color: var(--special); }
  .scrape-badge .short { display: none; }
  @media (max-width: 520px) {
    .scrape-badge { height: 36px; padding: 0 .55rem; font-size: .7rem; gap: .35rem; }
    .scrape-badge .full { display: none; }
    .scrape-badge .short { display: inline; }
    .header-actions { gap: .4rem; }
  }
  @media (max-width: 380px) {
    .brand-mark { width: 34px; height: 34px; font-size: .95rem; }
    .brand-text strong { font-size: 1rem; }
    .scrape-badge i { display: none; }
  }
  .menu-btn { display: none; }

  /* ---------- Hero stats ---------- */
  .hero { max-width: 1520px; width: 100%; margin: 1.5rem auto 0; padding: 0 1.75rem; }
  .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: .9rem; }
  .stat {
    position: relative; overflow: hidden;
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 1rem 1.1rem; display: flex; align-items: center; gap: .9rem;
    box-shadow: var(--shadow);
  }
  .stat::before {
    content: ""; position: absolute; inset: 0 auto 0 0; width: 3px; background: var(--yellow);
  }
  .stat-icon {
    width: 42px; height: 42px; border-radius: 12px; display: grid; place-items: center; flex-shrink: 0;
    background: var(--yellow-glow); color: var(--yellow-deep); font-size: 1.05rem;
  }
  [data-theme="dark"] .stat-icon { color: var(--yellow); }
  .stat-label { font-size: .7rem; color: var(--text-muted); font-weight: 600; text-transform: uppercase; letter-spacing: .7px; }
  .stat-value { font-size: 1.3rem; font-weight: 800; line-height: 1.15; }
  .stat-value small { font-size: .75rem; font-weight: 500; color: var(--text-muted); display: block; }
  .stat.special .stat-icon { background: var(--special-bg); color: var(--special); }
  .stat.special::before { background: var(--special); }

  /* ---------- Layout ---------- */
  .layout { display: flex; gap: 1.75rem; max-width: 1520px; width: 100%; margin: 1.5rem auto; padding: 0 1.75rem; flex: 1; }

  .sidebar {
    width: 290px; flex-shrink: 0; align-self: flex-start; position: sticky; top: 92px;
    max-height: calc(100vh - 110px); overflow-y: auto;
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
    padding: 1.25rem; box-shadow: var(--shadow);
  }
  .sidebar-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1rem; }
  .sidebar-head h2 { margin: 0; font-size: 1rem; font-weight: 700; display: flex; align-items: center; gap: .5rem; }
  .sidebar-head h2 i { color: var(--yellow-deep); }
  [data-theme="dark"] .sidebar-head h2 i { color: var(--yellow); }
  .close-btn { display: none; }

  .section-label { font-size: .68rem; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 1px; margin: 1.1rem 0 .55rem; }

  .toggle-row {
    display: flex; align-items: center; justify-content: space-between; gap: .75rem;
    padding: .7rem .85rem; border-radius: 11px; background: var(--input-bg); border: 1px solid var(--border);
    margin-bottom: .5rem; cursor: pointer;
  }
  .toggle-row span { font-size: .86rem; font-weight: 600; display: flex; align-items: center; gap: .5rem; }
  .toggle-row span i { width: 16px; text-align: center; color: var(--text-muted); }
  .toggle-row small { display: block; font-size: .68rem; font-weight: 500; color: var(--text-muted); margin-left: 1.5rem; }
  .switch { position: relative; width: 42px; height: 24px; flex-shrink: 0; }
  .switch input { opacity: 0; width: 0; height: 0; }
  .slider { position: absolute; inset: 0; background: var(--border-strong); border-radius: 99px; transition: .3s var(--ease); cursor: pointer; }
  .slider::before { content: ""; position: absolute; width: 18px; height: 18px; left: 3px; top: 3px; background: #fff; border-radius: 50%; transition: .3s var(--ease); box-shadow: 0 1px 3px rgba(0,0,0,.35); }
  .switch input:checked + .slider { background: var(--yellow); }
  .switch input:checked + .slider::before { transform: translateX(18px); }
  #specialsOnly:checked + .slider { background: var(--special); }

  .select {
    width: 100%; padding: .7rem .85rem; border-radius: 11px; border: 1px solid var(--border-strong);
    background: var(--input-bg); color: var(--text); font-size: .88rem; font-weight: 500; outline: none; cursor: pointer;
  }
  .select option { background: var(--surface); color: var(--text); }

  .cat-list { display: flex; flex-direction: column; gap: 2px; }
  .cat-btn {
    width: 100%; display: flex; align-items: center; gap: .65rem; padding: .6rem .7rem; border-radius: 10px;
    border: 1px solid transparent; background: none; color: var(--text); font-size: .85rem; font-weight: 600;
    text-align: left; cursor: pointer; transition: background .2s, border-color .2s;
  }
  .cat-btn:hover { background: var(--input-bg); }
  .cat-btn .cat-icon { width: 28px; height: 28px; border-radius: 8px; display: grid; place-items: center; background: var(--input-bg); color: var(--yellow-deep); font-size: .8rem; flex-shrink: 0; }
  [data-theme="dark"] .cat-btn .cat-icon { color: var(--yellow); }
  .cat-btn .cat-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .cat-btn .count { font-size: .7rem; font-weight: 600; color: var(--text-muted); background: var(--input-bg); padding: 1px 7px; border-radius: 99px; }
  .cat-btn .chev { font-size: .65rem; color: var(--text-muted); transition: transform .3s var(--ease); }
  .cat-group.open > .cat-btn .chev { transform: rotate(180deg); }
  .cat-btn.active { background: var(--yellow-glow); border-color: var(--yellow); }
  .cat-btn.active .cat-icon { background: var(--yellow); color: #111; }

  .sub-list {
    display: grid; grid-template-rows: 0fr; transition: grid-template-rows .35s var(--ease);
    margin-left: 1.55rem; border-left: 2px solid var(--border);
  }
  .sub-list > div { overflow: hidden; }
  .cat-group.open .sub-list { grid-template-rows: 1fr; }
  .sub-btn {
    width: 100%; display: flex; justify-content: space-between; gap: .5rem; padding: .45rem .75rem; margin: 1px 0;
    border: none; background: none; color: var(--text-muted); font-size: .8rem; font-weight: 500;
    text-align: left; cursor: pointer; border-radius: 0 8px 8px 0; transition: color .2s, background .2s;
  }
  .sub-btn:hover { color: var(--text); background: var(--input-bg); }
  .sub-btn.active { color: var(--text); background: var(--yellow-glow); box-shadow: inset 3px 0 0 var(--yellow); font-weight: 600; }
  .sub-btn .count { font-size: .68rem; opacity: .8; }

  .reset-btn {
    width: 100%; margin-top: 1rem; padding: .7rem; border-radius: 11px; border: 1px dashed var(--border-strong);
    background: none; color: var(--text-muted); font-weight: 600; font-size: .82rem; cursor: pointer; transition: all .2s;
  }
  .reset-btn:hover { color: var(--text); border-color: var(--yellow); }

  /* ---------- Results ---------- */
  main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
  .results-head { display: flex; flex-wrap: wrap; align-items: flex-end; justify-content: space-between; gap: 1rem; padding-bottom: .9rem; border-bottom: 1px solid var(--border); }
  .results-title h2 { margin: 0; font-size: 1.45rem; font-weight: 800; letter-spacing: -.3px; }
  .results-title p { margin: .15rem 0 0; font-size: .82rem; color: var(--text-muted); font-weight: 500; }
  .chips { display: flex; flex-wrap: wrap; gap: .4rem; margin-top: .55rem; }
  .chip { display: inline-flex; align-items: center; gap: .4rem; padding: .25rem .4rem .25rem .7rem; border-radius: 99px; background: var(--yellow-glow); border: 1px solid var(--yellow); font-size: .72rem; font-weight: 600; }
  .chip button { border: none; background: rgba(0,0,0,.15); color: inherit; width: 18px; height: 18px; border-radius: 50%; cursor: pointer; font-size: .6rem; display: grid; place-items: center; }
  .results-controls { display: flex; align-items: center; gap: .75rem; flex-wrap: wrap; }
  .per-page { display: flex; align-items: center; gap: .45rem; font-size: .8rem; color: var(--text-muted); font-weight: 600; }
  .per-page .select { width: auto; padding: .4rem .6rem; }

  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(232px, 1fr)); gap: 1rem; margin-top: 1.1rem; }

  /* ---------- Cards ---------- */
  .card {
    position: relative; display: flex; flex-direction: column; overflow: hidden;
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
    box-shadow: var(--shadow);
    opacity: 0; transform: translateY(12px);
    transition: opacity .45s var(--ease), transform .45s var(--ease), border-color .25s, box-shadow .25s;
    content-visibility: auto; contain-intrinsic-size: 330px;
  }
  .card.show { opacity: 1; transform: none; }
  .card:hover { border-color: rgba(255, 214, 0, .55); box-shadow: 0 14px 34px rgba(0,0,0,.35), 0 0 0 1px var(--yellow-glow); transform: translateY(-3px); }

  .ribbon {
    display: flex; align-items: center; gap: .45rem; height: 34px; padding: 0 .9rem;
    background: var(--yellow); color: #151515;
    font-size: .66rem; font-weight: 800; letter-spacing: .7px; text-transform: uppercase;
  }
  .ribbon i { font-size: .72rem; }
  .ribbon span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  .card-body { display: flex; flex-direction: column; gap: .7rem; padding: 1rem; flex: 1; }

  .media { position: relative; width: 100%; aspect-ratio: 1 / 1; max-height: 170px; border-radius: 11px; background: #fff; overflow: hidden; display: grid; place-items: center; }
  .media img { width: 100%; height: 100%; object-fit: contain; padding: 10px; opacity: 0; transition: opacity .35s ease; }
  .media img.loaded { opacity: 1; }
  .media .ph { position: absolute; inset: 0; display: grid; place-items: center; font-size: 2rem; color: #c9a800; background: linear-gradient(110deg, #f4f4f2 30%, #ffffff 50%, #f4f4f2 70%); background-size: 200% 100%; animation: shimmer 1.3s linear infinite; }
  .media .ph.done { animation: none; background: #f7f7f5; }
  @keyframes shimmer { to { background-position: -200% 0; } }

  .icon-tile {
    width: 100%; height: 76px; border-radius: 11px; display: grid; place-items: center; font-size: 1.7rem;
    background: linear-gradient(135deg, var(--surface-2), var(--input-bg)); border: 1px solid var(--border);
    color: var(--yellow-deep);
  }
  [data-theme="dark"] .icon-tile { color: var(--yellow); }

  .card h3 { margin: 0; font-size: .93rem; font-weight: 700; line-height: 1.32; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .size { font-size: .75rem; color: var(--text-muted); font-weight: 500; margin-top: -.45rem; }
  .sub-tag { font-size: .7rem; color: var(--text-muted); }

  .price-row { margin-top: auto; display: flex; align-items: center; justify-content: space-between; gap: .5rem; flex-wrap: wrap; padding-top: .7rem; border-top: 1px dashed var(--border-strong); }
  .price { font-size: 1.7rem; font-weight: 900; letter-spacing: -.8px; line-height: 1; display: flex; align-items: flex-start; }
  .price .dollar { font-size: .9rem; font-weight: 700; margin: .2rem .1rem 0 0; }
  .price .cents { font-size: .9rem; font-weight: 700; margin-top: .2rem; }
  .price.na { font-size: .9rem; color: var(--text-muted); font-weight: 600; letter-spacing: 0; }

  .badges { display: flex; flex-direction: column; align-items: flex-end; gap: .3rem; }
  .badge { display: inline-flex; align-items: center; gap: .35rem; padding: .28rem .6rem; border-radius: 7px; font-size: .64rem; font-weight: 800; letter-spacing: .5px; text-transform: uppercase; white-space: nowrap; }
  .badge.special { background: var(--special-bg); color: var(--special); border: 1px solid var(--special); }
  .badge.multi { background: var(--input-bg); color: var(--text); border: 1px solid var(--border-strong); }

  .view-btn {
    display: flex; align-items: center; justify-content: center; gap: .5rem; padding: .7rem;
    border-radius: 10px; border: 1px solid var(--border-strong); background: var(--surface-2); color: var(--text);
    font-size: .76rem; font-weight: 700; letter-spacing: .6px; text-transform: uppercase; text-decoration: none;
    transition: all .25s var(--ease);
  }
  .view-btn:hover { background: var(--yellow); color: #111; border-color: var(--yellow); }
  .view-btn i.fa-arrow-right { transition: transform .25s var(--ease); }
  .view-btn:hover i.fa-arrow-right { transform: translateX(3px); }

  /* ---------- Pagination / empty / misc ---------- */
  .pagination { display: flex; flex-wrap: wrap; justify-content: center; gap: .35rem; }
  .pagination.bottom { margin: 1.6rem 0 .5rem; }
  .page-btn { min-width: 38px; height: 38px; padding: 0 .7rem; border-radius: 10px; border: 1px solid var(--border-strong); background: var(--surface); color: var(--text); font-weight: 600; font-size: .85rem; cursor: pointer; transition: all .2s; }
  .page-btn:hover:not(:disabled):not(.active) { border-color: var(--yellow); }
  .page-btn.active { background: var(--yellow); color: #111; border-color: var(--yellow); }
  .page-btn:disabled { opacity: .35; cursor: not-allowed; }
  .page-gap { align-self: center; color: var(--text-muted); padding: 0 .2rem; }

  .empty { display: none; text-align: center; padding: 4rem 1rem; margin-top: 1rem; border-radius: var(--radius); border: 1px dashed var(--border-strong); background: var(--input-bg); color: var(--text-muted); }
  .empty i { font-size: 2.6rem; color: var(--yellow); margin-bottom: .8rem; }
  .empty h3 { margin: 0 0 .3rem; color: var(--text); }

  .to-top {
    position: fixed; right: 1.25rem; bottom: calc(1.25rem + env(safe-area-inset-bottom)); z-index: 60;
    width: 46px; height: 46px; border-radius: 14px; border: none; background: var(--yellow); color: #111;
    font-size: 1rem; cursor: pointer; box-shadow: 0 8px 24px var(--yellow-glow);
    opacity: 0; transform: translateY(12px); pointer-events: none; transition: all .3s var(--ease);
  }
  .to-top.show { opacity: 1; transform: none; pointer-events: auto; }

  footer { text-align: center; font-size: .75rem; color: var(--text-muted); padding: 1.5rem 1rem 2rem; }
  footer a { color: inherit; }

  .overlay { position: fixed; inset: 0; z-index: 90; background: rgba(0,0,0,.55); backdrop-filter: blur(3px); opacity: 0; pointer-events: none; transition: opacity .3s; }
  .overlay.show { opacity: 1; pointer-events: auto; }

  /* ---------- Responsive ---------- */
  @media (max-width: 1100px) {
    .stats { grid-template-columns: repeat(2, 1fr); }
  }
  @media (max-width: 900px) {
    header { flex-wrap: wrap; padding: .75rem 1rem; padding-top: calc(.75rem + env(safe-area-inset-top)); }
    .menu-btn { display: grid; }
    .header-actions { margin-left: auto; }
    .search { order: 3; flex-basis: 100%; max-width: none; }
    .search-kbd { display: none; }
    .hero, .layout { padding: 0 1rem; }
    .hero { margin-top: 1rem; }
    .layout { margin: 1rem auto; }
    .sidebar {
      position: fixed; top: 0; left: 0; bottom: 0; z-index: 100; width: 86%; max-width: 330px;
      max-height: none; height: 100%; border-radius: 0 var(--radius) var(--radius) 0;
      padding-top: calc(1.25rem + env(safe-area-inset-top));
      transform: translateX(-105%); transition: transform .35s var(--ease);
    }
    .sidebar.open { transform: none; }
    .close-btn { display: grid; }
    .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .75rem; }
    .results-title h2 { font-size: 1.2rem; }
  }
  @media (max-width: 520px) {
    .brand-text small { display: none; }
    .stats { gap: .6rem; }
    .stat { padding: .7rem; gap: .55rem; }
    .stat-icon { width: 32px; height: 32px; font-size: .85rem; border-radius: 10px; }
    .stat-label { font-size: .62rem; }
    .stat-value { font-size: 1.05rem; }
    .grid { gap: .6rem; }
    .ribbon { height: 28px; padding: 0 .65rem; font-size: .58rem; }
    .card-body { padding: .7rem; gap: .5rem; }
    .icon-tile { height: 54px; font-size: 1.3rem; }
    .media { max-height: 130px; }
    .card h3 { font-size: .82rem; }
    .size { font-size: .7rem; margin-top: -.3rem; }
    .sub-tag { display: none; }
    .price-row { flex-direction: column; align-items: flex-start; gap: .4rem; padding-top: .5rem; }
    .price { font-size: 1.35rem; }
    .badges { align-items: flex-start; }
    .badge { font-size: .58rem; padding: .22rem .45rem; }
    .view-btn { padding: .55rem; font-size: .66rem; letter-spacing: .3px; }
    .view-btn .long { display: none; }
    .results-controls { width: 100%; justify-content: space-between; }
  }
  @media (max-width: 340px) { .grid { grid-template-columns: 1fr; } }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation: none !important; transition: none !important; }
    .card { opacity: 1; transform: none; }
  }
</style>
</head>
<body>

<header>
  <button class="icon-btn menu-btn" id="menuBtn" aria-label="Open filters"><i class="fa-solid fa-bars-staggered"></i></button>
  <button class="brand" id="brandReset" title="Back to all products">
    <div class="brand-mark"><i class="fa-solid fa-cart-shopping"></i></div>
    <div class="brand-text">
      <strong>PAK<span>'n</span>SAVE</strong>
      <small>Price Explorer</small>
    </div>
  </button>
  <div class="search" id="searchWrap">
    <i class="fa-solid fa-magnifying-glass"></i>
    <input type="text" id="searchInput" placeholder="Search products..." autocomplete="off">
    <span class="search-kbd">/</span>
    <button class="search-clear" id="searchClear" aria-label="Clear search"><i class="fa-solid fa-xmark"></i></button>
  </div>
  <div class="header-actions">
    <div class="scrape-badge" title="When this data was scraped"><i class="fa-solid fa-clock-rotate-left"></i> <span class="full" id="scrapeBadge">–</span><span class="short" id="scrapeBadgeShort">–</span></div>
    <button class="icon-btn" id="themeToggle" aria-label="Toggle theme"><i class="fa-solid fa-sun"></i></button>
  </div>
</header>

<section class="hero">
  <div class="stats">
    <div class="stat"><div class="stat-icon"><i class="fa-solid fa-boxes-stacked"></i></div><div><div class="stat-label">Products</div><div class="stat-value" id="statProducts">–</div></div></div>
    <div class="stat special"><div class="stat-icon"><i class="fa-solid fa-tags"></i></div><div><div class="stat-label">On special</div><div class="stat-value" id="statSpecials">–</div></div></div>
    <div class="stat"><div class="stat-icon"><i class="fa-solid fa-layer-group"></i></div><div><div class="stat-label">Departments</div><div class="stat-value" id="statDepts">–</div></div></div>
    <div class="stat"><div class="stat-icon"><i class="fa-solid fa-clock-rotate-left"></i></div><div><div class="stat-label">Last scraped</div><div class="stat-value" id="statScraped">–</div></div></div>
  </div>
</section>

<div class="overlay" id="overlay"></div>

<div class="layout">
  <aside class="sidebar" id="sidebar">
    <div class="sidebar-head">
      <h2><i class="fa-solid fa-sliders"></i> Filters</h2>
      <button class="icon-btn close-btn" id="closeBtn" aria-label="Close filters"><i class="fa-solid fa-xmark"></i></button>
    </div>

    <label class="toggle-row">
      <span><i class="fa-solid fa-tag"></i> Specials only</span>
      <div class="switch"><input type="checkbox" id="specialsOnly"><span class="slider"></span></div>
    </label>
    <label class="toggle-row">
      <div>
        <span><i class="fa-regular fa-image"></i> Show images</span>
        <small>Uses more data</small>
      </div>
      <div class="switch"><input type="checkbox" id="showImages"><span class="slider"></span></div>
    </label>

    <div class="section-label">Sort by</div>
    <select class="select" id="sortSelect">
      <option value="popular">Most popular</option>
      <option value="name_asc">Name (A–Z)</option>
      <option value="price_asc">Price: low to high</option>
      <option value="price_desc">Price: high to low</option>
      <option value="special_first">Specials first</option>
    </select>

    <div class="section-label">Departments</div>
    <div class="cat-list" id="catList"></div>

    <button class="reset-btn" id="resetBtn"><i class="fa-solid fa-rotate-left"></i> Reset everything</button>
  </aside>

  <main>
    <div class="results-head">
      <div class="results-title">
        <h2 id="heading">All products</h2>
        <p id="resultCount"></p>
        <div class="chips" id="chips"></div>
      </div>
      <div class="results-controls">
        <div class="per-page">
          <label for="perPage">Show</label>
          <select class="select" id="perPage">
            <option>24</option><option selected>48</option><option>96</option><option>192</option>
          </select>
        </div>
        <div class="pagination" id="pagTop"></div>
      </div>
    </div>

    <div class="grid" id="grid"></div>
    <div class="empty" id="empty">
      <i class="fa-solid fa-basket-shopping"></i>
      <h3>Nothing matches that</h3>
      <p>Try a different search, or clear your filters.</p>
    </div>
    <div class="pagination bottom" id="pagBottom"></div>
  </main>
</div>

<footer>Prices from paknsave.co.nz for one store and may differ in-store. Not affiliated with PAK'nSAVE.</footer>
<button class="to-top" id="toTop" aria-label="Back to top"><i class="fa-solid fa-arrow-up"></i></button>

<script>
(() => {
  const SCRAPED_AT = "__SCRAPED_AT__";
  const RAW = __PRODUCTS_JSON__;

  // Whole-word matching so "crumpets", "ginger", "original" etc. aren't caught.
  const RESTRICTED_RE = /\b(wines?|beers?|vodka|whiske?y|rum|gin|ciders?|bourbon|liquor|tequila|cigarettes?|tobacco|vapes?|condoms?|lubricants?|tampons?)\b/i;
  const RESTRICTED_DEPTS = new Set(["Beer, Wine & Cider"]);
  const PRODUCTS = RAW.filter(p => !RESTRICTED_DEPTS.has(p.dept) && !RESTRICTED_RE.test(p.name));
  PRODUCTS.forEach((p, i) => { p._i = i; p._q = (p.name + ' ' + (p.size || '') + ' ' + (p.sub || '')).toLowerCase(); });

  const DEPT_ICONS = {
    'Fruit & Vegetables': 'fa-carrot', 'Meat, Poultry & Seafood': 'fa-drumstick-bite',
    'Fridge, Deli & Eggs': 'fa-cheese', 'Frozen': 'fa-snowflake', 'Bakery': 'fa-bread-slice',
    'Pantry': 'fa-jar', 'Snacks, Treats & Easy Meals': 'fa-cookie-bite', 'Hot & Cold Drinks': 'fa-mug-hot',
    'Health & Body': 'fa-pump-soap', 'Household & Cleaning': 'fa-spray-can-sparkles',
    'Baby & Toddler': 'fa-baby-carriage', 'Pets': 'fa-paw',
  };
  const deptIcon = d => DEPT_ICONS[d] || 'fa-store';

  const ICON_RULES = [
    [/\b(beef|steak|mince)\b/, 'fa-cow'], [/\b(chicken|poultry|drumsticks?)\b/, 'fa-drumstick-bite'],
    [/\b(lamb|pork|sausages?|bacon|ham)\b/, 'fa-bacon'], [/\b(fish|salmon|tuna|prawns?|seafood)\b/, 'fa-fish'],
    [/\b(milk|cheese|butter|yoghurt|cream)\b/, 'fa-cheese'], [/\beggs?\b/, 'fa-egg'],
    [/\b(chocolate|lollies|sweets?|candy)\b/, 'fa-candy-cane'], [/\b(bread|wraps?|rolls?|buns?|bagels?)\b/, 'fa-bread-slice'],
    [/\b(apples?|bananas?|fruit|oranges?|berries|grapes?)\b/, 'fa-apple-whole'],
    [/\b(carrots?|potato(es)?|onions?|broccoli|cabbage|lettuce|veg\w*)\b/, 'fa-carrot'],
    [/\b(ice cream|frozen)\b/, 'fa-ice-cream'], [/\b(coffee|tea)\b/, 'fa-mug-hot'],
    [/\b(water|juice|drink|cola|soda|lemonade)\b/, 'fa-bottle-water'], [/\bpizza\b/, 'fa-pizza-slice'],
    [/\b(pasta|sauce|spread|jam|honey|canned)\b/, 'fa-jar'], [/\b(nappies|nappy|baby)\b/, 'fa-baby-carriage'],
    [/\b(dog|cat|pet)\b/, 'fa-paw'], [/\b(toothbrush|toothpaste|shampoo|soap)\b/, 'fa-pump-soap'],
    [/\b(clean\w*|spray|laundry|detergent)\b/, 'fa-spray-can-sparkles'], [/\b(chips|crackers|biscuits?|cookies?)\b/, 'fa-cookie-bite'],
  ];
  const itemIcon = p => { const t = p.name.toLowerCase(); for (const [re, ic] of ICON_RULES) if (re.test(t)) return ic; return deptIcon(p.dept); };

  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmtInt = n => n.toLocaleString('en-NZ');

  // ---------- State ----------
  const state = { q: '', dept: null, sub: null, specials: false, images: false, sort: 'popular', page: 1, perPage: 48 };
  let filtered = [];

  // ---------- Stats ----------
  function renderStats() {
    const specials = PRODUCTS.filter(p => p.special).length;
    const depts = new Set(PRODUCTS.map(p => p.dept).filter(Boolean)).size;
    $('statProducts').textContent = fmtInt(PRODUCTS.length);
    $('statSpecials').textContent = fmtInt(specials);
    $('statDepts').textContent = depts;

    const d = new Date(SCRAPED_AT.replace(/\.\d+/, ''));
    if (isNaN(d)) { $('statScraped').textContent = SCRAPED_AT; $('scrapeBadge').textContent = SCRAPED_AT; $('scrapeBadgeShort').textContent = SCRAPED_AT; }
    if (!isNaN(d)) {
      const when = d.toLocaleString('en-NZ', { weekday: 'short', day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' });
      const mins = Math.round((Date.now() - d) / 60000);
      const ago = mins < 60 ? `${Math.max(mins, 0)} min ago` : mins < 1440 ? `${Math.round(mins / 60)} h ago` : `${Math.round(mins / 1440)} d ago`;
      $('statScraped').innerHTML = `${esc(ago)}<small>${esc(when)}</small>`;
      $('scrapeBadge').textContent = 'Scraped ' + d.toLocaleString('en-NZ', { day: 'numeric', month: 'short', hour: 'numeric', minute: '2-digit' });
      $('scrapeBadgeShort').textContent = d.toLocaleTimeString('en-NZ', { hour: 'numeric', minute: '2-digit' });
    }
  }

  // ---------- Categories ----------
  function buildTree() {
    const tree = new Map();
    PRODUCTS.forEach(p => {
      if (!p.dept) return;
      if (!tree.has(p.dept)) tree.set(p.dept, { count: 0, subs: new Map() });
      const node = tree.get(p.dept);
      node.count++;
      if (p.sub) node.subs.set(p.sub, (node.subs.get(p.sub) || 0) + 1);
    });
    return [...tree.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }

  function renderCategories() {
    const html = buildTree().map(([dept, node]) => {
      const subs = [...node.subs.entries()].sort((a, b) => a[0].localeCompare(b[0]));
      const subHtml = subs.map(([sub, n]) =>
        `<button class="sub-btn" data-dept="${esc(dept)}" data-sub="${esc(sub)}"><span>${esc(sub)}</span><span class="count">${n}</span></button>`
      ).join('');
      return `<div class="cat-group" data-dept="${esc(dept)}">
        <button class="cat-btn" data-dept="${esc(dept)}">
          <span class="cat-icon"><i class="fa-solid ${deptIcon(dept)}"></i></span>
          <span class="cat-name">${esc(dept)}</span>
          <span class="count">${node.count}</span>
          ${subs.length ? '<i class="fa-solid fa-chevron-down chev"></i>' : ''}
        </button>
        ${subs.length ? `<div class="sub-list"><div>${subHtml}</div></div>` : ''}
      </div>`;
    }).join('');
    $('catList').innerHTML = html;
  }

  function syncCategoryUI() {
    document.querySelectorAll('.cat-group').forEach(g => {
      const isDept = g.dataset.dept === state.dept;
      g.classList.toggle('open', isDept);
      g.querySelector('.cat-btn').classList.toggle('active', isDept && !state.sub);
    });
    document.querySelectorAll('.sub-btn').forEach(b =>
      b.classList.toggle('active', b.dataset.dept === state.dept && b.dataset.sub === state.sub));
  }

  $('catList').addEventListener('click', e => {
    const sub = e.target.closest('.sub-btn');
    const cat = e.target.closest('.cat-btn');
    if (sub) {
      const same = state.sub === sub.dataset.sub && state.dept === sub.dataset.dept;
      state.dept = sub.dataset.dept; state.sub = same ? null : sub.dataset.sub;
    } else if (cat) {
      const same = state.dept === cat.dataset.dept && !state.sub;
      state.dept = same ? null : cat.dataset.dept; state.sub = null;
    } else return;
    state.q = ''; $('searchInput').value = ''; $('searchWrap').classList.remove('has-value');
    if (window.innerWidth <= 900 && sub) closeSidebar();
    update();
  });

  // ---------- Filtering ----------
  function applyFilters() {
    const q = state.q.trim().toLowerCase();
    const terms = q ? q.split(/\s+/) : [];
    filtered = PRODUCTS.filter(p => {
      if (state.specials && !p.special) return false;
      if (state.dept && p.dept !== state.dept) return false;
      if (state.sub && p.sub !== state.sub) return false;
      if (terms.length && !terms.every(t => p._q.includes(t))) return false;
      return true;
    });
    const byName = (a, b) => a.name.localeCompare(b.name);
    const sorters = {
      popular: (a, b) => a._i - b._i,
      name_asc: byName,
      price_asc: (a, b) => (a.price ?? Infinity) - (b.price ?? Infinity) || byName(a, b),
      price_desc: (a, b) => (b.price ?? -Infinity) - (a.price ?? -Infinity) || byName(a, b),
      special_first: (a, b) => (b.special - a.special) || byName(a, b),
    };
    filtered.sort(sorters[state.sort]);
  }

  function renderHeader() {
    $('heading').textContent = state.q ? `Results for "${state.q}"` : state.sub || state.dept || 'All products';
    $('resultCount').textContent = `${fmtInt(filtered.length)} product${filtered.length === 1 ? '' : 's'}`;

    const chips = [];
    if (state.dept) chips.push(['dept', state.dept]);
    if (state.sub) chips.push(['sub', state.sub]);
    if (state.specials) chips.push(['specials', 'Specials only']);
    if (state.q) chips.push(['q', `"${state.q}"`]);
    $('chips').innerHTML = chips.map(([k, label]) =>
      `<span class="chip">${esc(label)}<button data-chip="${k}" aria-label="Remove">✕</button></span>`).join('');
  }

  $('chips').addEventListener('click', e => {
    const k = e.target.closest('button')?.dataset.chip;
    if (!k) return;
    if (k === 'dept') { state.dept = null; state.sub = null; }
    if (k === 'sub') state.sub = null;
    if (k === 'specials') { state.specials = false; $('specialsOnly').checked = false; }
    if (k === 'q') { state.q = ''; $('searchInput').value = ''; $('searchWrap').classList.remove('has-value'); }
    update();
  });

  // ---------- Cards ----------
  const revealer = new IntersectionObserver(entries => {
    entries.forEach(en => { if (en.isIntersecting) { en.target.classList.add('show'); revealer.unobserve(en.target); } });
  }, { rootMargin: '0px 0px 80px 0px' });

  function priceHtml(price) {
    if (price == null) return '<div class="price na">Price unavailable</div>';
    const [d, c] = price.toFixed(2).split('.');
    return `<div class="price"><span class="dollar">$</span>${d}<span class="cents">.${c}</span></div>`;
  }

  function cardHtml(p) {
    const icon = itemIcon(p);
    const visual = state.images && p.image
      ? `<div class="media"><div class="ph"><i class="fa-solid ${icon}"></i></div>
           <img src="${esc(p.image)}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" width="200" height="200"></div>`
      : `<div class="icon-tile"><i class="fa-solid ${icon}"></i></div>`;

    let badges = '';
    if (p.special) badges += '<span class="badge special"><i class="fa-solid fa-tag"></i> On special</span>';
    if (p.promo) badges += `<span class="badge multi"><i class="fa-solid fa-layer-group"></i> ${esc(p.promo)}</span>`;

    return `
      <div class="ribbon"><i class="fa-solid ${deptIcon(p.dept)}"></i><span>${esc(p.dept || 'Other')}</span></div>
      <div class="card-body">
        ${visual}
        <h3 title="${esc(p.name)}">${esc(p.name)}</h3>
        ${p.size ? `<div class="size">${esc(p.size)}</div>` : ''}
        ${p.sub ? `<div class="sub-tag">${esc(p.sub)}</div>` : ''}
        <div class="price-row">${priceHtml(p.price)}${badges ? `<div class="badges">${badges}</div>` : ''}</div>
        <a class="view-btn" href="${esc(p.link)}" target="_blank" rel="noopener">View<span class="long"> on PAK'nSAVE</span> <i class="fa-solid fa-arrow-right"></i></a>
      </div>`;
  }

  function renderGrid() {
    const grid = $('grid');
    grid.innerHTML = '';
    $('empty').style.display = filtered.length ? 'none' : 'block';

    const start = (state.page - 1) * state.perPage;
    const frag = document.createDocumentFragment();
    filtered.slice(start, start + state.perPage).forEach(p => {
      const el = document.createElement('article');
      el.className = 'card';
      el.innerHTML = cardHtml(p);
      const img = el.querySelector('img');
      if (img) {
        img.addEventListener('load', () => { img.classList.add('loaded'); img.previousElementSibling.style.display = 'none'; });
        img.addEventListener('error', () => { img.remove(); el.querySelector('.ph').classList.add('done'); });
      }
      frag.appendChild(el);
      revealer.observe(el);
    });
    grid.appendChild(frag);
    renderPagination();
  }

  // ---------- Pagination ----------
  function renderPagination() {
    const total = Math.ceil(filtered.length / state.perPage);
    if (total <= 1) { $('pagTop').innerHTML = $('pagBottom').innerHTML = ''; return; }
    const cur = state.page;
    const pages = new Set([1, total, cur - 1, cur, cur + 1].filter(n => n >= 1 && n <= total));
    const sorted = [...pages].sort((a, b) => a - b);
    let html = `<button class="page-btn" data-page="${cur - 1}" ${cur === 1 ? 'disabled' : ''} aria-label="Previous"><i class="fa-solid fa-chevron-left"></i></button>`;
    sorted.forEach((n, i) => {
      if (i && n - sorted[i - 1] > 1) html += '<span class="page-gap">…</span>';
      html += `<button class="page-btn ${n === cur ? 'active' : ''}" data-page="${n}">${n}</button>`;
    });
    html += `<button class="page-btn" data-page="${cur + 1}" ${cur === total ? 'disabled' : ''} aria-label="Next"><i class="fa-solid fa-chevron-right"></i></button>`;
    $('pagTop').innerHTML = $('pagBottom').innerHTML = html;
  }
  ['pagTop', 'pagBottom'].forEach(id => $(id).addEventListener('click', e => {
    const b = e.target.closest('.page-btn');
    if (!b || b.disabled) return;
    state.page = +b.dataset.page;
    renderGrid();
    document.querySelector('.results-head').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }));

  // ---------- Update cycle ----------
  function update({ keepPage = false } = {}) {
    if (!keepPage) state.page = 1;
    applyFilters();
    syncCategoryUI();
    renderHeader();
    renderGrid();
  }

  // ---------- Controls ----------
  let searchTimer;
  $('searchInput').addEventListener('input', e => {
    $('searchWrap').classList.toggle('has-value', !!e.target.value);
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => { state.q = e.target.value; state.dept = null; state.sub = null; update(); }, 140);
  });
  $('searchClear').addEventListener('click', () => {
    $('searchInput').value = ''; $('searchWrap').classList.remove('has-value'); state.q = ''; update(); $('searchInput').focus();
  });
  document.addEventListener('keydown', e => {
    if (e.key === '/' && document.activeElement !== $('searchInput')) { e.preventDefault(); $('searchInput').focus(); }
    if (e.key === 'Escape') closeSidebar();
  });

  $('specialsOnly').addEventListener('change', e => { state.specials = e.target.checked; update(); });
  $('showImages').addEventListener('change', e => { state.images = e.target.checked; renderGrid(); });
  $('sortSelect').addEventListener('change', e => { state.sort = e.target.value; update(); });
  $('perPage').addEventListener('change', e => { state.perPage = +e.target.value; update(); });

  function resetAll() {
    Object.assign(state, { q: '', dept: null, sub: null, specials: false, sort: 'popular', page: 1 });
    $('searchInput').value = ''; $('searchWrap').classList.remove('has-value');
    $('specialsOnly').checked = false; $('sortSelect').value = 'popular';
    closeSidebar();
    update();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }
  $('brandReset').addEventListener('click', resetAll);
  $('resetBtn').addEventListener('click', resetAll);

  // Theme (remembered per browser)
  const setTheme = t => {
    document.documentElement.dataset.theme = t;
    $('themeToggle').innerHTML = t === 'dark' ? '<i class="fa-solid fa-sun"></i>' : '<i class="fa-solid fa-moon"></i>';
    try { localStorage.setItem('pns-theme', t); } catch (_) {}
  };
  let saved = null; try { saved = localStorage.getItem('pns-theme'); } catch (_) {}
  setTheme(saved || 'dark');
  $('themeToggle').addEventListener('click', () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));

  // Mobile sidebar
  const openSidebar = () => { $('sidebar').classList.add('open'); $('overlay').classList.add('show'); document.body.style.overflow = 'hidden'; };
  const closeSidebar = () => { $('sidebar').classList.remove('open'); $('overlay').classList.remove('show'); document.body.style.overflow = ''; };
  $('menuBtn').addEventListener('click', openSidebar);
  $('closeBtn').addEventListener('click', closeSidebar);
  $('overlay').addEventListener('click', closeSidebar);

  // Back to top
  window.addEventListener('scroll', () => $('toTop').classList.toggle('show', window.scrollY > 600), { passive: true });
  $('toTop').addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));

  renderStats();
  renderCategories();
  update();
})();
</script>
</body>
</html>
"""


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main():
    from_json = "--from-json" in sys.argv

    if from_json:
        if not os.path.exists(RAW_JSON):
            print(f"No {RAW_JSON} found — run without --from-json first.")
            sys.exit(1)
        with open(RAW_JSON, encoding="utf-8") as f:
            products = json.load(f)
        scraped_at = datetime.fromtimestamp(os.path.getmtime(RAW_JSON), tz=timezone.utc)
    else:
        products = run_scrape()
        if not products:
            print("Scrape returned nothing — leaving the existing site untouched.")
            sys.exit(1)
        scraped_at = datetime.now(timezone.utc)
        with open(RAW_JSON, "w", encoding="utf-8") as f:
            json.dump(products, f)

    print(f"\n{len(products)} products.")
    write_csv(products)
    scraped_iso = scraped_at.isoformat(timespec="seconds")
    print(f"Scraped at: {scraped_iso}")
    write_html(products, scraped_iso)
    print(f"Wrote {OUTPUT_CSV} and {OUTPUT_HTML}")

    if not IN_CI:
        webbrowser.open("file://" + os.path.abspath(OUTPUT_HTML))


if __name__ == "__main__":
    main()
