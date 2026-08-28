#!/usr/bin/env python3
"""
Zepto -- Amazon Pay Gift Card discount monitor for pincode 560035.

Flow:
  1. Generate random session_id, csrfSecret and XSRF-TOKEN — the Zepto BFF API
     validates only that the request-signature is internally consistent (it does
     NOT check XSRF against a server-side store), so random values work fine.
  2. Compute request-signature = SHA-256(sorted {body|deviceId|method|requestId|secret|url})
  3. POST /user-search-service/api/v3/search and parse organic results

The 1% discount is only visible to unauthenticated (anonymous) sessions --
this script intentionally calls the API without logging in (x-without-bearer: true).
Store IDs are hardcoded for pincode 560035 (South Bengaluru, Vijayanagar).
"""

import os
import sys
import json
import hashlib
import uuid
import base64
import secrets
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from curl_cffi import requests as cffi_requests
    _SESSION = cffi_requests.Session(impersonate="chrome120")
except ImportError:
    try:
        import requests as cffi_requests
        _SESSION = cffi_requests.Session()
    except ImportError:
        print("Error: pip install -r requirements.txt")
        sys.exit(1)

import brightdata_browser

IST = timezone(timedelta(hours=5, minutes=30))
COOLDOWN_HOURS = float(os.environ.get("ZEPTO_COOLDOWN_HOURS", os.environ.get("ALERT_COOLDOWN_HOURS", 24)))
# Only hit the Scraping Browser at most once per this many minutes (credit conservation).
RUN_INTERVAL_MIN = float(os.environ.get("ZEPTO_RUN_INTERVAL_MIN", 0))
STATE_DIR = Path(".alert_state")
STATE_FILE = STATE_DIR / "last_alert.json"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
MIN_DISCOUNT_PCT = float(os.environ.get("ZEPTO_MIN_DISCOUNT", 1.0))
# Only alert on cards whose denomination (₹ face value = MRP) is at least this.
# 0 = no floor. Set to e.g. 5000 to ignore small denominations (₹500/1100/2100/3100).
MIN_DENOMINATION = float(os.environ.get("ZEPTO_MIN_DENOMINATION", 0))

SEARCH_URL = "https://www.zepto.com/search?query=amazon+pay+gift+card"
API_BASE = "https://bff-gateway.zepto.com"
SEARCH_PATH = "/user-search-service/api/v3/search"

# Store IDs for home delivery address (BLR-BELLANDUR - 2, 80 Trees Apartment,
# lat=12.9176162 lon=77.6979). Amazon Pay Gift Card Black Box is available
# at 1% off for anonymous (guest) requests — not visible when logged in.
_STORE_ID = "0f5f31f2-f764-498a-9cc5-606cf82f4f2e"
_STORE_IDS = f"{_STORE_ID},38caa147-31a6-4fae-a5b6-1c21d1bbfa1d"

# Stable device identifier from the user's browser session.
_DEVICE_ID = "6fea3bb4-0ac3-446e-9df3-17d690e0f647"


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


def _card_key(match: Dict[str, Any]) -> str:
    """Per-card alert key so each denomination/design cools off independently.

    The always-in-stock low denominations (₹500/1100/2100/3100…) otherwise consume the
    single global cooldown on every scrape, starving the rare, small-stock ₹10,000 card
    of an alert. Keying by product name lets a freshly-in-stock ₹10,000 alert on its own
    even when a ₹2,100 already alerted within the cooldown window.
    """
    return f"zepto_amazon::{match['name']}"


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
        last = state.get("zepto_amazon_lastrun")
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
    state["zepto_amazon_lastrun"] = get_ist_now().isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _sign(method: str, url_path: str, request_id: str, device_id: str, xsrf: str, body: Optional[str]) -> str:
    """
    Zepto request-signature: SHA-256(sorted_values joined by |)
    Object keys: body, deviceId, method, requestId, secret, url
    GET requests use "undefined" as the body value (matches JS undefined→string coercion).
    """
    obj = {
        "body": body if method.lower() != "get" else "undefined",
        "deviceId": device_id,
        "method": method.lower(),
        "requestId": request_id,
        "secret": xsrf,
        "url": url_path,
    }
    joined = "|".join(obj[k] for k in sorted(obj.keys()))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _make_session() -> Dict[str, str]:
    """Generate a fresh anonymous session — no homepage GET needed.

    The Zepto BFF validates only that the request-signature matches the values
    in the request headers (internal consistency). XSRF-TOKEN is not checked
    against any server-side store, so random values are accepted.
    """
    return {
        "session_id": str(uuid.uuid4()),
        "device_id": _DEVICE_ID,
        "XSRF-TOKEN": base64.urlsafe_b64encode(secrets.token_bytes(20)).decode().rstrip("=")
                      + ":" + base64.b64encode(secrets.token_bytes(32)).decode().rstrip("="),
        "csrfSecret": base64.urlsafe_b64encode(secrets.token_bytes(8)).decode().rstrip("="),
        "marketplace": "SUPER_SAVER",
    }


def search_products(session: Dict[str, str]) -> List[Dict[str, Any]]:
    session_id = session["session_id"]
    device_id = session["device_id"]
    csrf_secret = session["csrfSecret"]
    xsrf = session["XSRF-TOKEN"]
    marketplace = session["marketplace"]
    request_id = str(uuid.uuid4())

    body_dict = {
        "query": "amazon pay gift card",
        "pageNumber": 0,
        "mode": "SHOW_ALL_RESULTS",
        "userSessionId": session_id,
    }
    body_str = json.dumps(body_dict, separators=(",", ":"))
    sig = _sign("post", SEARCH_PATH, request_id, device_id, xsrf, body_str)

    headers = {
        "content-type": "application/json",
        "x-csrf-secret": csrf_secret,
        "x-xsrf-token": xsrf,
        "x-without-bearer": "true",
        "request-signature": sig,
        "x-timezone": hashlib.sha256(sig.encode()).hexdigest(),
        "platform": "WEB",
        "tenant": "ZEPTO",
        "auth_revamp_flow": "v2",
        "auth_from_cookie": "true",
        "marketplace_type": marketplace,
        "session_id": session_id,
        "sessionid": session_id,
        "device_id": device_id,
        "deviceid": device_id,
        "request_id": request_id,
        "requestid": request_id,
        "store_id": _STORE_ID,
        "storeid": _STORE_ID,
        "store_ids": _STORE_IDS,
        "app_version": "15.23.2",
        "appversion": "15.23.2",
        "source": "DIRECT",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }

    # Zepto's BFF now sits behind an AWS-WAF JS challenge that a bare API client can't
    # solve (returns HTTP 202 + a challenge page instead of JSON). Route through the
    # Scraping Browser: navigating zepto.com in a real Chrome mints the aws-waf-token
    # cookie, then the same-session fetch to the bff-gateway host (cross-origin, so
    # credentials:"include" to carry the cookie) succeeds.
    if brightdata_browser.is_configured():
        call = {"url": f"{API_BASE}{SEARCH_PATH}", "method": "POST",
                "headers": headers, "body": body_str, "credentials": "include"}
        resp = brightdata_browser.browser_fetch(
            "https://www.zepto.com/", [call], wait_cookie="aws-waf-token") or []
        if not resp or resp[0].get("status") != 200:
            print(f"[{get_ist_now()}] browser search: HTTP {resp[0].get('status') if resp else 'n/a'}")
            return []
        try:
            data = json.loads(resp[0].get("text") or "{}")
        except ValueError:
            return []
    else:
        r = _SESSION.post(
            f"{API_BASE}{SEARCH_PATH}", headers=headers, data=body_str, timeout=30,
        )
        r.raise_for_status()
        data = r.json()

    products: List[Dict[str, Any]] = []
    seen: set = set()

    for widget in data.get("layout", []):
        if widget.get("widgetId") != "PRODUCT_GRID":
            continue
        items = (widget.get("data") or {}).get("resolver", {}).get("data", {}).get("items", [])
        for item in items:
            pr = item.get("productResponse", {})
            prod = pr.get("product", {})
            pv = pr.get("productVariant", {})
            name = (prod.get("name") or "").strip()
            if "amazon pay" not in name.lower() or name in seen:
                continue
            seen.add(name)
            products.append({
                "name": name,
                "discount_pct": int(pr.get("discountPercent") or 0),
                "mrp": (pv.get("mrp") or 0) // 100,
                "selling_price": (pr.get("sellingPrice") or 0) // 100,
                "in_stock": not pr.get("outOfStock", True),
            })
    return products


def send_ntfy_alert(matches: List[Dict[str, Any]]) -> bool:
    if not NTFY_TOPIC:
        print(f"[{get_ist_now()}] ntfy not configured")
        return False
    try:
        import requests as _req
        lines = ["Zepto Amazon Pay Gift Card discount found!\n"]
        for m in matches:
            lines.append(
                f"- {m['name']}: Rs.{m['selling_price']} (MRP Rs.{m['mrp']}) — {m['discount_pct']}% off"
            )
        lines.append(f"\nBuy (as guest): {SEARCH_URL}")
        _req.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data="\n".join(lines).encode("utf-8"),
            headers={
                "Title": f"Zepto Amazon Pay GC: {len(matches)} discounted",
                "Priority": "high",
                "Tags": "amazon,zepto,gift",
                "Click": SEARCH_URL,
            },
            timeout=15,
        ).raise_for_status()
        print(f"[{get_ist_now()}] ntfy notification sent")
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] ntfy failed: {e}")
        return False


def send_email_alert(matches: List[Dict[str, Any]]) -> bool:
    sender = os.environ.get("SENDER_EMAIL")
    receiver = os.environ.get("RECEIVER_EMAIL")
    password = os.environ.get("EMAIL_PASSWORD")
    if not all([sender, receiver, password]):
        return False
    try:
        ist_time = get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST")
        lines = ["Zepto Amazon Pay Gift Card discount!\n"]
        for m in matches:
            lines.append(
                f"- {m['name']}: Rs.{m['selling_price']} (MRP Rs.{m['mrp']}) — {m['discount_pct']}% off"
            )
        lines.extend(["", f"Buy (as guest): {SEARCH_URL}", "", f"Time: {ist_time}"])
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Zepto Amazon Pay GC: {len(matches)} discounted"
        msg["From"] = sender
        msg["To"] = receiver
        msg.attach(MIMEText("\n".join(lines), "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.send_message(msg)
        print(f"[{get_ist_now()}] Email sent")
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] Email failed: {e}")
        return False


def check_zepto_amazon():
    use_browser = brightdata_browser.is_configured()
    print("=" * 60)
    print(f"Zepto Amazon Pay Gift Card Monitor -- {get_ist_now()}")
    print(f"Min discount: {MIN_DISCOUNT_PCT}%  |  Min denom: >=Rs.{MIN_DENOMINATION:.0f}  |  Store: 560035   "
          f"Source: {'Bright Data Scraping Browser' if use_browser else 'direct'}")
    print("=" * 60)

    if use_browser and _recently_scraped():
        return False

    session = _make_session()
    print(f"[{get_ist_now()}] Session: id={session['session_id'][:8]}...  device={session['device_id'][:8]}...")

    try:
        products = search_products(session)
    except Exception as e:
        print(f"[{get_ist_now()}] Search failed: {e}")
        return False
    if use_browser:
        _mark_scraped()

    if not products:
        print(f"[{get_ist_now()}] No Amazon Pay products found in results")
        return False

    matches = []
    for p in products:
        status = "IN STOCK" if p["in_stock"] else "out of stock"
        disc = f"{p['discount_pct']}% off" if p["discount_pct"] > 0 else "no discount"
        print(
            f"[{get_ist_now()}] {p['name'][:50]:50s}  {status:12s}  "
            f"Rs.{p['selling_price']:6d}  {disc}"
        )
        if (p["in_stock"] and p["discount_pct"] >= MIN_DISCOUNT_PCT
                and p["mrp"] >= MIN_DENOMINATION):
            matches.append(p)

    if not matches:
        print(f"[{get_ist_now()}] No Amazon Pay gift cards with >={MIN_DISCOUNT_PCT}% discount "
              f"and denomination >=Rs.{MIN_DENOMINATION:.0f}.")
        return False

    print(f"[{get_ist_now()}] MATCH: {[m['name'] for m in matches]}")

    # Per-card cooldown: only alert on cards not already alerted within the cooldown
    # window. Keeps the ~daily digest cadence for the always-in-stock low denominations
    # while letting a freshly-in-stock ₹10,000 (or any newly-appearing card) alert
    # immediately instead of being swallowed by a single global cooldown.
    new_matches = [m for m in matches if should_send_alert(_card_key(m))]
    if not new_matches:
        print(f"[{get_ist_now()}] All matching cards already alerted within cooldown.")
        return False

    print(f"[{get_ist_now()}] NEW: {[m['name'] for m in new_matches]}")
    ntfy_ok = send_ntfy_alert(new_matches)
    email_ok = send_email_alert(new_matches)
    if ntfy_ok or email_ok:
        for m in new_matches:
            record_alert(_card_key(m))
    return ntfy_ok or email_ok


if __name__ == "__main__":
    try:
        check_zepto_amazon()
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
