#!/usr/bin/env python3
"""
Zepto -- Amazon Pay Gift Card discount monitor for pincode 560035.

Flow:
  1. GET https://www.zepto.com/ with the user's device_id pre-seeded to obtain
     fresh session cookies (session_id, csrfSecret, XSRF-TOKEN)
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
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

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

IST = timezone(timedelta(hours=5, minutes=30))
COOLDOWN_HOURS = float(os.environ.get("ZEPTO_COOLDOWN_HOURS", os.environ.get("ALERT_COOLDOWN_HOURS", 1)))
STATE_DIR = Path(".alert_state")
STATE_FILE = STATE_DIR / "last_alert.json"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
MIN_DISCOUNT_PCT = float(os.environ.get("ZEPTO_MIN_DISCOUNT", 1.0))

SEARCH_URL = "https://www.zepto.com/search?query=amazon+pay+gift+card"
API_BASE = "https://bff-gateway.zepto.com"
SEARCH_PATH = "/user-search-service/api/v3/search"

# Store IDs for pincode 560035 (South Bengaluru) — stable physical store identifiers.
_STORE_ID = "6f08827d-5bea-4c32-8bea-ba8a34ae7ed9"
_STORE_IDS = f"{_STORE_ID},774a725c-2c4b-4dc2-94d1-72b737f7e1f6"

# Stable device identifier from the user's browser session — keeps requests
# consistently attributed to the same device across runs.
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


def _get_session_cookies() -> Dict[str, str]:
    """Load the Zepto homepage with the user's device_id pre-seeded to get fresh XSRF tokens."""
    _SESSION.cookies.set("device_id", _DEVICE_ID, domain="www.zepto.com")
    _SESSION.get(
        "https://www.zepto.com/",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        },
        timeout=30,
    )
    cookies = dict(_SESSION.cookies.items())
    # Ensure our device_id is used even if the server overwrites it
    cookies["device_id"] = _DEVICE_ID
    return cookies


def search_products(cookies: Dict[str, str]) -> List[Dict[str, Any]]:
    session_id = cookies.get("session_id", str(uuid.uuid4()))
    device_id = cookies.get("device_id", str(uuid.uuid4()))
    csrf_secret = cookies.get("csrfSecret", "")
    xsrf = unquote(cookies.get("XSRF-TOKEN", ""))
    marketplace = cookies.get("marketplace", "SUPER_SAVER")
    request_id = str(uuid.uuid4())

    body_dict = {
        "query": "amazon pay gift card",
        "pageNumber": 0,
        "mode": "SHOW_ALL_RESULTS",
        "userSessionId": session_id,
    }
    body_str = json.dumps(body_dict, separators=(",", ":"))
    sig = _sign("post", SEARCH_PATH, request_id, device_id, xsrf, body_str)

    r = _SESSION.post(
        f"{API_BASE}{SEARCH_PATH}",
        headers={
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
        },
        data=body_str,
        timeout=30,
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
    print("=" * 60)
    print(f"Zepto Amazon Pay Gift Card Monitor -- {get_ist_now()}")
    print(f"Min discount: {MIN_DISCOUNT_PCT}%  |  Store: 560035")
    print("=" * 60)

    try:
        print(f"[{get_ist_now()}] Initialising anonymous session...")
        cookies = _get_session_cookies()
        sid = cookies.get("session_id", "")
        has_xsrf = bool(cookies.get("XSRF-TOKEN"))
        print(f"[{get_ist_now()}] session={sid[:8] if sid else 'N/A'}...  xsrf={'yes' if has_xsrf else 'no'}")
    except Exception as e:
        print(f"[{get_ist_now()}] Session init failed: {e}")
        return False

    try:
        products = search_products(cookies)
    except Exception as e:
        print(f"[{get_ist_now()}] Search failed: {e}")
        return False

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
        if p["in_stock"] and p["discount_pct"] >= MIN_DISCOUNT_PCT:
            matches.append(p)

    if not matches:
        print(f"[{get_ist_now()}] No Amazon Pay gift cards with >={MIN_DISCOUNT_PCT}% discount.")
        return False

    print(f"[{get_ist_now()}] MATCH: {[m['name'] for m in matches]}")

    if not should_send_alert("zepto_amazon"):
        return False

    ntfy_ok = send_ntfy_alert(matches)
    email_ok = send_email_alert(matches)
    if ntfy_ok or email_ok:
        record_alert("zepto_amazon")
    return ntfy_ok or email_ok


if __name__ == "__main__":
    try:
        check_zepto_amazon()
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
