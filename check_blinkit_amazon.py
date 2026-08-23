#!/usr/bin/env python3
"""
Blinkit -- Amazon Pay Physical Gift Card DISCOUNT monitor.

Alerts only when the Amazon Pay Physical Gift Card is in stock AND actually
discounted (selling price < MRP by >= BLINKIT_AMAZON_MIN_DISCOUNT %) at the Blinkit
store serving the given lat/lon (default: pincode 560035, South Bengaluru).
In-stock-at-face-value is ignored -- only a real discount is worth an alert.

API flow:
  1. GET /v2/accounts/auth_key/      -> auth_key
  2. POST /v1/layout/search          -> product inventory data
"""

import os
import sys
import re
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

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

import brightdata_browser

IST = timezone(timedelta(hours=5, minutes=30))
COOLDOWN_HOURS = float(os.environ.get("BLINKIT_COOLDOWN_HOURS", os.environ.get("ALERT_COOLDOWN_HOURS", 1)))
# Only hit the Scraping Browser at most once per this many minutes (credit conservation).
RUN_INTERVAL_MIN = float(os.environ.get("BLINKIT_AMAZON_RUN_INTERVAL_MIN", 0))
# Only alert when the card is actually DISCOUNTED (selling price < MRP) by at least this %.
# In-stock-at-face-value is not interesting. 0.5 = any real discount.
MIN_DISCOUNT_PCT = float(os.environ.get("BLINKIT_AMAZON_MIN_DISCOUNT", 0.5))
STATE_DIR = Path(".alert_state")
STATE_FILE = STATE_DIR / "last_alert.json"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# Default coords for Bengaluru 560035 (Vijayanagar area)
LAT = float(os.environ.get("BLINKIT_LAT") or "12.9822")
LON = float(os.environ.get("BLINKIT_LON") or "77.5392")

# Public guest auth key — same for all unauthenticated sessions.
# Blinkit's /v2/accounts/auth_key/ endpoint requires existing browser state to refresh;
# this key is stable and works for all anonymous searches.
_AUTH_KEY = "c761ec3633c22afad934fb17a66385c1c06c5472b4898b866b7306186d0bb477"

SEARCH_QUERY = "amazon pay gift card"
WATCH_PRODUCT_NAMES = ["amazon pay physical gift card", "amazon pay gift card"]
PRODUCT_URL = "https://blinkit.com/s/?q=amazon+pay+gift+card"

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-IN,en;q=0.9",
    "app_client": "consumer_web",
    "app_version": "1010101010",
    "rn_bundle_version": "1009003012",
    "web_app_version": "1008010016",
    "content-type": "application/json",
    "Origin": "https://blinkit.com",
    "Referer": PRODUCT_URL,
}


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
            print(f"[{get_ist_now()}] Cooldown active for {alert_type}: {elapsed_h:.1f}h / {COOLDOWN_HOURS}h")
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


def _recently_scraped() -> bool:
    """True if a browser scrape ran within RUN_INTERVAL_MIN -- skip to conserve credits."""
    if RUN_INTERVAL_MIN <= 0:
        return False
    try:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        last = state.get("blinkit_amazon_lastrun")
        if not last:
            return False
        last_time = datetime.fromisoformat(last)
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=IST)
        elapsed_min = (get_ist_now() - last_time).total_seconds() / 60
        if elapsed_min < RUN_INTERVAL_MIN:
            print(f"[{get_ist_now()}] Throttled: last scrape {elapsed_min:.0f}m ago "
                  f"(< {RUN_INTERVAL_MIN:.0f}m) -- skipping to save Scraping Browser credits")
            return True
        return False
    except Exception:
        return False


def _mark_scraped():
    STATE_DIR.mkdir(exist_ok=True)
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    state["blinkit_amazon_lastrun"] = get_ist_now().isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2))


_SEARCH_URL = (f"https://blinkit.com/v1/layout/search?q={SEARCH_QUERY.replace(' ', '+')}"
               f"&search_type=type_to_search")
_SEARCH_BODY = {
    "applied_filters": None,
    "monet_assets": [],
    "postback_meta": {},
    "previous_search_query": "",
    "processed_rails": {},
    "similar_entities": None,
    "sort": "",
    "vertical_cards_processed": 0,
}


def search_products(auth_key: str) -> List[Dict[str, Any]]:
    headers = {**BASE_HEADERS, "auth_key": auth_key, "lat": str(LAT), "lon": str(LON)}
    # Blinkit 403s datacenter IPs; route through the Scraping Browser (residential) when
    # configured, in one session. Falls back to a direct request otherwise.
    if brightdata_browser.is_configured():
        call = {"url": _SEARCH_URL, "method": "POST", "headers": headers,
                "body": json.dumps(_SEARCH_BODY)}
        resp = brightdata_browser.browser_fetch("https://blinkit.com/", [call]) or []
        if not resp or resp[0].get("status") != 200:
            print(f"[{get_ist_now()}] browser search: HTTP {resp[0].get('status') if resp else 'n/a'}")
            return []
        try:
            return (json.loads(resp[0].get("text") or "{}").get("response", {}) or {}).get("snippets", [])
        except ValueError:
            return []
    r = _post(_SEARCH_URL, headers=headers, json=_SEARCH_BODY, timeout=30)
    r.raise_for_status()
    return r.json().get("response", {}).get("snippets", [])


def _price_num(text: str) -> Optional[float]:
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.]", "", text)
    try:
        return float(cleaned)
    except ValueError:
        return None


def parse_products(snippets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results = []
    for snippet in snippets:
        data = snippet.get("data", {})
        name_obj = data.get("name") or data.get("display_name") or {}
        name = (name_obj.get("text") or "").strip()
        if not any(w in name.lower() for w in WATCH_PRODUCT_NAMES):
            continue

        is_sold_out = data.get("is_sold_out", True)
        inventory = data.get("inventory", 0)
        product_state = data.get("product_state", "")

        mrp_text = (data.get("mrp") or {}).get("text", "")
        price_text = (data.get("normal_price") or {}).get("text", "")

        mrp = _price_num(mrp_text)
        price = _price_num(price_text)
        discount_pct = (round((mrp - price) / mrp * 100, 1)
                        if (mrp and price and mrp > price) else 0.0)

        results.append({
            "name": name,
            "inventory": inventory,
            "is_sold_out": is_sold_out,
            "product_state": product_state,
            "mrp_text": mrp_text,
            "price_text": price_text,
            "discount_pct": discount_pct,
            "in_stock": not is_sold_out and inventory > 0,
        })
    return results


def send_ntfy_alert(products: List[Dict[str, Any]]) -> bool:
    if not NTFY_TOPIC:
        print(f"[{get_ist_now()}] ntfy not configured")
        return False
    try:
        lines = ["Amazon Pay Gift Card DISCOUNTED on Blinkit!\n"]
        for p in products:
            lines.append(f"- {p['name']}: {p['price_text']} (MRP {p['mrp_text']}) "
                         f"-- {p['discount_pct']:.1f}% off | inventory={p['inventory']}")
        lines.append(f"\nOrder now: {PRODUCT_URL}")
        message = "\n".join(lines)
        _post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": f"Blinkit: Amazon Pay GC {products[0]['discount_pct']:.0f}% off ({len(products)} item(s))",
                "Priority": "high",
                "Tags": "amazon,blinkit,gift",
                "Click": PRODUCT_URL,
            },
            timeout=15,
        ).raise_for_status()
        print(f"[{get_ist_now()}] ntfy notification sent")
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] ntfy failed: {e}")
        return False


def send_email_alert(products: List[Dict[str, Any]]) -> bool:
    sender = os.environ.get("SENDER_EMAIL")
    receiver = os.environ.get("RECEIVER_EMAIL")
    password = os.environ.get("EMAIL_PASSWORD")
    if not all([sender, receiver, password]):
        return False
    try:
        ist_time = get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST")
        lines = ["Amazon Pay Gift Card DISCOUNTED on Blinkit!\n"]
        for p in products:
            lines.append(f"- {p['name']}: {p['price_text']} (MRP {p['mrp_text']}) "
                         f"-- {p['discount_pct']:.1f}% off, inventory={p['inventory']}")
        lines.extend(["", f"Order now: {PRODUCT_URL}", "", f"Time: {ist_time}"])
        text_body = "\n".join(lines)
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Blinkit: Amazon Pay GC {products[0]['discount_pct']:.0f}% off!"
        msg["From"] = sender
        msg["To"] = receiver
        msg.attach(MIMEText(text_body, "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.send_message(msg)
        print(f"[{get_ist_now()}] Email sent to {receiver}")
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] Email failed: {e}")
        return False


def check_blinkit_amazon():
    use_browser = brightdata_browser.is_configured()
    print("=" * 60)
    print(f"Blinkit Amazon Pay Gift Card monitor -- {get_ist_now()}")
    print(f"Location: lat={LAT}, lon={LON}   "
          f"Source: {'Bright Data Scraping Browser' if use_browser else 'direct'}")
    print("=" * 60)

    if use_browser and _recently_scraped():
        return False

    auth_key = _AUTH_KEY
    print(f"[{get_ist_now()}] Using auth_key: {auth_key[:16]}...")

    try:
        snippets = search_products(auth_key)
    except requests.RequestException as e:
        print(f"[{get_ist_now()}] Search request failed: {e}")
        return False
    if use_browser:
        _mark_scraped()

    products = parse_products(snippets)

    if not products:
        print(f"[{get_ist_now()}] No Amazon Pay gift card products found in results")
        return False

    discounted = []
    for p in products:
        status = "IN STOCK" if p["in_stock"] else "out of stock"
        disc = f"{p['discount_pct']:.1f}% off" if p["discount_pct"] > 0 else "no discount"
        print(
            f"[{get_ist_now()}] {p['name']:40s}  {status:12s}  "
            f"{p['price_text']} (MRP {p['mrp_text']})  {disc}  inv={p['inventory']}"
        )
        if p["in_stock"] and p["discount_pct"] >= MIN_DISCOUNT_PCT:
            discounted.append(p)

    if not discounted:
        print(f"[{get_ist_now()}] No in-stock Amazon Pay gift card with >={MIN_DISCOUNT_PCT}% discount.")
        return False

    print(f"[{get_ist_now()}] DISCOUNTED: {[(p['name'], p['discount_pct']) for p in discounted]}")

    if not should_send_alert("blinkit_amazon"):
        return False

    ntfy_ok = send_ntfy_alert(discounted)
    email_ok = send_email_alert(discounted)
    if ntfy_ok or email_ok:
        record_alert("blinkit_amazon")
    return ntfy_ok or email_ok


if __name__ == "__main__":
    try:
        check_blinkit_amazon()
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
