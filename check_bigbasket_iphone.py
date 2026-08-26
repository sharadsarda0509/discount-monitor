#!/usr/bin/env python3
"""
BigBasket -- iPhone 15 / 16 / 17 base handset stock monitor (hyperlocal, fully autonomous).

BigBasket's search/listing API is locked behind an Akamai-validated, geolocated browser
session that cannot be replicated headlessly. BUT its Next.js product-detail SSR endpoint
IS reachable from a plain server request and -- crucially -- honours the `_bb_lat_long`
cookie regardless of the caller's IP, so it returns per-location availability from GitHub
Actions without any browser, cookie upload, or manual step:

  GET https://www.bigbasket.com/_next/data/<buildId>/pd/<id>/<slug>.json

Flow per run:
  1. GET homepage  -> scrape the current Next.js buildId (changes each deploy)
  2. For each watched product (id + slug) -> GET the pd SSR json -> read availability
     (avail_status "001" = in stock / "Add"; "000" = out of stock / "Notify Me")

Discovery: base iPhone 15/16/17 SKUs are discovered dynamically each run from BigBasket's
listing service (search "iphone"). The listing service is Akamai-gated but returns JSON
reliably once the homepage has been fetched in the same session (fetch_build_id does that),
so new colours or a new model (e.g. iPhone 17) are picked up automatically -- no code change.
BIGBASKET_PRODUCTS still works as a manual seed / fallback if discovery ever returns nothing.
Location is fully config-driven via BIGBASKET_LAT / BIGBASKET_LON -- no hardcoded store.
"""

import os
import re
import sys
import json
import base64
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from iphone_models import models_summary

try:
    from curl_cffi import requests as cffi_requests
    _SESSION = cffi_requests.Session(impersonate="chrome120")
    def _get(url, **kw): return _SESSION.get(url, **kw)
    def _post(url, **kw): return _SESSION.post(url, **kw)
except ImportError:
    import requests as _requests
    _SESSION = _requests.Session()
    def _get(url, **kw): return _SESSION.get(url, **kw)
    def _post(url, **kw): return _SESSION.post(url, **kw)

try:
    import requests  # for RequestException type only
except ImportError:
    print("Error: pip install -r requirements.txt")
    sys.exit(1)

IST = timezone(timedelta(hours=5, minutes=30))
COOLDOWN_HOURS = float(os.environ.get(
    "BIGBASKET_COOLDOWN_HOURS", os.environ.get("ALERT_COOLDOWN_HOURS", 12)))
STATE_DIR = Path(".alert_state")
STATE_FILE = STATE_DIR / "last_alert.json"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# 560035 area (Bellandur, SE Bengaluru). Availability follows these coords, not the IP.
LAT = str(os.environ.get("BIGBASKET_LAT") or "12.9048022")
LON = str(os.environ.get("BIGBASKET_LON") or "77.6821069")
PINCODE = os.environ.get("BIGBASKET_PINCODE", "560035").strip()

MODELS = [m.strip() for m in os.environ.get("BIGBASKET_MODELS", "15,16,17").split(",") if m.strip()]

# Manual seed / fallback watch list as "id:slug" (comma-separated), used when dynamic
# discovery returns nothing. Kept current with the base iPhone SKUs BigBasket lists today;
# override via the BIGBASKET_PRODUCTS env var without code changes.
_DEFAULT_PRODUCTS = (
    "40332363:apple-iphone-15-128gb-blue-1-unit,"
    "40331223:apple-iphone-15-128gb-black,"
    "40330603:apple-iphone-16-128gb-white-1-n,"
    "40356301:apple-iphone-17-256gb-white-1-unit"
)

HOME_URL = "https://www.bigbasket.com/"
PRODUCT_PAGE = "https://www.bigbasket.com/ps/?q=iphone"
# Search/listing service used for dynamic SKU discovery (Akamai-gated; works once the
# homepage is fetched in the same session -- see fetch_build_id).
LISTING_URL = "https://www.bigbasket.com/listing-svc/v2/products"
SEARCH_TERM = os.environ.get("BIGBASKET_SEARCH_TERM", "iphone")

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _cookies() -> Dict[str, str]:
    lat_long_b64 = base64.b64encode(f"{LAT}|{LON}".encode()).decode()
    return {
        "_bb_lat_long": lat_long_b64,
        "_bb_pin_code": PINCODE,
        "bb2_enabled": "true",
        "_bb_bb2.0": "1",
    }


def _products() -> List[Tuple[str, str]]:
    raw = os.environ.get("BIGBASKET_PRODUCTS", "").strip() or _DEFAULT_PRODUCTS
    out = []
    for item in raw.split(","):
        item = item.strip()
        if ":" in item:
            pid, slug = item.split(":", 1)
            out.append((pid.strip(), slug.strip()))
    return out


_ACCESSORY = re.compile(
    r"\b(case|cover|strap|glass|protector|charger|cable|adapter|screen|guard|"
    r"skin|holder|mount|stand|airpod|band|tempered|wallet|magsafe|battery|"
    r"power|pouch|sleeve|lens|film|dock|grip)\b", re.I)


def _is_handset(name: str) -> bool:
    """Exact base iPhone models only -- excludes Plus, Pro, Pro Max, Air, mini and 16e."""
    if not re.search(r"\bi[pP]hone\b", name, re.I):
        return False
    if _ACCESSORY.search(name):
        return False
    if not re.search(r"\d+\s*(GB|TB)\b", name, re.I):
        return False
    pattern = (r"\bi[pP]hone\s*(" + "|".join(map(re.escape, MODELS)) +
               r")\b(?!\s*(?:plus|pro|max|air|mini))")
    return bool(re.search(pattern, name, re.I))


def get_ist_now():
    return datetime.now(IST)


def should_send_alert(alert_type: str) -> bool:
    STATE_DIR.mkdir(exist_ok=True)
    if not STATE_FILE.exists():
        return True
    try:
        state = json.loads(STATE_FILE.read_text())
        last = state.get(alert_type)
        if not last:
            return True
        last_time = datetime.fromisoformat(last)
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=IST)
        elapsed_h = (get_ist_now() - last_time).total_seconds() / 3600
        if elapsed_h < COOLDOWN_HOURS:
            print(f"[{get_ist_now()}] Cooldown active for {alert_type}: {elapsed_h:.2f}h / {COOLDOWN_HOURS}h")
            return False
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] Warning reading state: {e}")
        return True


def record_alert(alert_type: str):
    STATE_DIR.mkdir(exist_ok=True)
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    state[alert_type] = get_ist_now().isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2))


def fetch_build_id() -> Optional[str]:
    try:
        r = _get(HOME_URL, headers={"User-Agent": UA}, cookies=_cookies(), timeout=30)
        r.raise_for_status()
        m = re.search(r'"buildId":"([^"]+)"', r.text)
        return m.group(1) if m else None
    except (requests.RequestException, ValueError) as e:
        print(f"[{get_ist_now()}] homepage/buildId fetch failed: {e}")
        return None


def discover_products(max_pages: int = 5) -> List[Tuple[str, str]]:
    """Discover base iPhone 15/16/17 SKUs from BigBasket search (listing-svc).

    Mirrors the Reliance monitor's dynamic discovery: a new colour or a new model
    (e.g. iPhone 17) is picked up the day BigBasket lists it, with no code change.
    Requires the homepage to have been fetched first in the same session (fetch_build_id
    does that) so the Akamai cookies are seeded. Returns a list of (id, slug) tuples;
    empty on failure so the caller can fall back to the seeded BIGBASKET_PRODUCTS list.
    """
    found: Dict[str, str] = {}
    for page in range(1, max_pages + 1):
        try:
            r = _get(LISTING_URL,
                     params={"type": "ps", "slug": SEARCH_TERM, "page": str(page)},
                     headers={"User-Agent": UA, "x-channel": "BB-WEB", "accept": "*/*",
                              "referer": PRODUCT_PAGE},
                     cookies=_cookies(), timeout=30)
            if r.status_code != 200:
                break
            tabs = (r.json() or {}).get("tabs") or []
        except (requests.RequestException, ValueError) as e:
            print(f"[{get_ist_now()}] discovery page {page} failed: {e}")
            break
        got = 0
        for t in tabs:
            for p in ((t.get("product_info") or {}).get("products") or []):
                got += 1
                if not _is_handset((p.get("desc") or "").strip()):
                    continue
                m = re.search(r"/pd/(\d+)/([^/?]+)", str(p.get("absolute_url") or ""))
                if m:
                    found.setdefault(m.group(1), m.group(2))
        if got == 0:
            break
    return list(found.items())


def _find_product(node: Any, pid: str) -> Optional[Dict[str, Any]]:
    """Walk the SSR json and return the product dict whose id == pid (has availability)."""
    if isinstance(node, dict):
        if str(node.get("id")) == pid and "availability" in node and node.get("desc"):
            return node
        for v in node.values():
            found = _find_product(v, pid)
            if found:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find_product(v, pid)
            if found:
                return found
    return None


def check_product(build_id: str, pid: str, slug: str) -> Optional[Dict[str, Any]]:
    url = (f"https://www.bigbasket.com/_next/data/{build_id}/pd/{pid}/{slug}.json"
           f"?params={pid}&params={slug}")
    try:
        r = _get(url, headers={"User-Agent": UA, "x-channel": "BB-WEB", "accept": "*/*",
                               "referer": HOME_URL}, cookies=_cookies(), timeout=30)
        if r.status_code != 200:
            print(f"[{get_ist_now()}] pd {pid} HTTP {r.status_code}")
            return None
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        print(f"[{get_ist_now()}] pd {pid} failed: {e}")
        return None

    p = _find_product(data, pid)
    if not p:
        print(f"[{get_ist_now()}] pd {pid} ({slug}): product node not found")
        return None

    name = (p.get("desc") or "").strip()
    if not _is_handset(name):
        print(f"[{get_ist_now()}] {name!r} is not a watched base handset -- skipping")
        return None

    av = p.get("availability") or {}
    pricing = p.get("pricing") or {}
    in_stock = str(av.get("avail_status")) == "001" and av.get("button") != "Notify Me"
    return {
        "id": pid,
        "name": name,
        "avail_status": av.get("avail_status"),
        "button": av.get("button"),
        "label": av.get("label"),
        "sp": pricing.get("sp"),
        "mrp": pricing.get("mrp"),
        "url": "https://www.bigbasket.com" + (p.get("absolute_url") or f"/pd/{pid}/{slug}/"),
        "in_stock": in_stock,
    }


def _stock_lines(items: List[Dict[str, Any]]) -> List[str]:
    lines = []
    for p in items:
        price = f"Rs.{p['sp']}" if p.get("sp") else ""
        if p.get("mrp") and p.get("sp") and str(p["mrp"]) != str(p["sp"]):
            price += f" (MRP Rs.{p['mrp']})"
        lines.append(f"- {p['name']}: {price}".rstrip())
    return lines


def send_alert(items: List[Dict[str, Any]]) -> bool:
    models = models_summary(p["name"] for p in items)
    subject = f"BigBasket: {models} in stock -- {len(items)} item(s)"
    title = f"{models} in stock on BigBasket ({len(items)})"
    order_url = items[0]["url"] if items else PRODUCT_PAGE
    body = "\n".join(
        ["iPhone handset(s) in stock on BigBasket:", ""]
        + _stock_lines(items) + ["", f"Order now: {order_url}"]
    )
    ntfy_ok = email_ok = False

    if NTFY_TOPIC:
        try:
            _post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=body.encode("utf-8"),
                headers={"Title": title, "Priority": "high", "Tags": "iphone,bigbasket",
                         "Click": order_url},
                timeout=15,
            ).raise_for_status()
            print(f"[{get_ist_now()}] ntfy sent")
            ntfy_ok = True
        except Exception as e:
            print(f"[{get_ist_now()}] ntfy failed: {e}")
    else:
        print(f"[{get_ist_now()}] ntfy not configured (NTFY_TOPIC empty)")

    sender = os.environ.get("SENDER_EMAIL")
    receiver = os.environ.get("RECEIVER_EMAIL")
    password = os.environ.get("EMAIL_PASSWORD")
    if all([sender, receiver, password]):
        try:
            ist_time = get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST")
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = sender
            msg["To"] = receiver
            msg.attach(MIMEText(body + f"\n\nTime: {ist_time}", "plain"))
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(sender, password)
                server.send_message(msg)
            print(f"[{get_ist_now()}] email sent to {receiver}")
            email_ok = True
        except Exception as e:
            print(f"[{get_ist_now()}] email failed: {e}")
    else:
        print(f"[{get_ist_now()}] email not configured")

    return ntfy_ok or email_ok


def check_bigbasket_iphone():
    print("=" * 60)
    print(f"BigBasket iPhone monitor -- {get_ist_now()}")
    print(f"Location: lat={LAT}, lon={LON} (pin {PINCODE})   Models: {', '.join(MODELS)}")
    print("=" * 60)

    build_id = fetch_build_id()
    if not build_id:
        print(f"[{get_ist_now()}] Could not resolve buildId -- aborting this run.")
        return False
    print(f"[{get_ist_now()}] buildId={build_id}")

    # Dynamic discovery (like Reliance) merged with the seeded fallback, de-duped by id.
    # Discovered slugs (from the live listing) win over the seed.
    products: Dict[str, str] = {pid: slug for pid, slug in _products()}
    discovered = discover_products()
    for pid, slug in discovered:
        products[pid] = slug
    print(f"[{get_ist_now()}] discovered {len(discovered)} SKU(s) via search; "
          f"watching {len(products)} total")

    in_stock = []
    for pid, slug in products.items():
        info = check_product(build_id, pid, slug)
        if not info:
            continue
        status = "IN STOCK" if info["in_stock"] else f"out of stock ({info.get('label') or info.get('button')})"
        print(f"[{get_ist_now()}] {info['name']:40.40}  {status}")
        if info["in_stock"]:
            in_stock.append(info)

    if not in_stock:
        print(f"[{get_ist_now()}] No watched iPhone in stock on BigBasket.")
        return False

    print(f"[{get_ist_now()}] IN STOCK: {[p['name'] for p in in_stock]}")
    if not should_send_alert("bigbasket_iphone"):
        return False

    ok = send_alert(in_stock)
    if ok:
        record_alert("bigbasket_iphone")
    return ok


if __name__ == "__main__":
    try:
        check_bigbasket_iphone()
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
