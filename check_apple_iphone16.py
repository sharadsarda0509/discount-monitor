#!/usr/bin/env python3
"""
Apple India — iPhone 16 (6.1\" 128GB) pickup / availability monitor

Uses the retail pickup-message endpoint (the same one used by Telegram/automated
monitors globally):

  GET /in/shop/retail/pickup-message?pl=true&parts.0=<SKU>&...&location=<PIN>

This endpoint:
  - Accepts `location=110017` and returns stores near that PIN code
  - Works from ANY IP — no cookies, no session warmup required
  - Returns both Apple Saket (R756) and Apple Noida (R787) for PIN 110017
  - Per-store per-SKU availability via body.stores[].partsAvailability

When stock IS available pickupDisplay == "available" and pickupSearchQuote
contains "Today" or a date. Currently unavailable shows pickupDisplay == "ineligible".

Pattern mirrors check_noones.py: fetch → interpret → optional ntfy + email + cooldown.
"""

import os
import sys
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    print("Error: pip install -r requirements.txt")
    sys.exit(1)

IST = timezone(timedelta(hours=5, minutes=30))

COOLDOWN_HOURS = float(os.environ.get("APPLE_COOLDOWN_HOURS", os.environ.get("ALERT_COOLDOWN_HOURS", 1)))
STATE_DIR = Path(".alert_state")
STATE_FILE = STATE_DIR / "last_alert.json"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

SAME_DAY_ONLY = os.environ.get("APPLE_SAME_DAY_ONLY", "true").lower() in ("1", "true", "yes")

POSTAL_CODE = os.environ.get("APPLE_PINCODE") or os.environ.get("POSTAL_CODE") or "110017"

_DEFAULT_STORE_IDS: Tuple[str, ...] = ("R756", "R787")


def _allowed_store_ids() -> Tuple[str, ...]:
    raw = os.environ.get("APPLE_STORE_IDS", "").strip()
    if raw:
        return tuple(x.strip().upper() for x in raw.split(",") if x.strip())
    legacy = os.environ.get("APPLE_STORE_ID", "").strip()
    if legacy:
        return (legacy.upper(),)
    return _DEFAULT_STORE_IDS


ALLOWED_STORE_IDS: Tuple[str, ...] = _allowed_store_ids()

REQUIRE_ALLOWED_STORE = os.environ.get(
    "APPLE_REQUIRE_STORE_MATCH",
    os.environ.get("APPLE_REQUIRE_SAKET", "true"),
).lower() in ("1", "true", "yes")

PICKUP_MSG_URL = "https://www.apple.com/in/shop/retail/pickup-message"

PRODUCT_URL = (
    "https://www.apple.com/in/shop/buy-iphone/iphone-16/"
    "6.1%22-display-128gb-pink"
)

# iPhone 16 6.1" 128GB — India SKUs
IPHONE16_128GB_COLORS = {
    "ultramarine": "MYEC3HN/A",
    "pink":        "MYEA3HN/A",
    "white":       "MYE93HN/A",
    "black":       "MYE73HN/A",
}


def get_ist_now():
    return datetime.now(IST)


def _ist_today_yyyymmdd() -> str:
    return get_ist_now().strftime("%Y%m%d")


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


def fetch_pickup_availability(postal_code: str) -> Dict[str, Any]:
    """
    Single call to pickup-message with all 4 SKUs.
    Returns the parsed JSON body (contains body.stores[]).
    """
    params: Dict[str, str] = {"pl": "true", "location": postal_code}
    for i, sku in enumerate(IPHONE16_128GB_COLORS.values()):
        params[f"parts.{i}"] = sku

    r = requests.get(
        PICKUP_MSG_URL,
        params=params,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Language": "en-IN,en;q=0.9",
            "Referer": PRODUCT_URL,
        },
        timeout=45,
    )
    r.raise_for_status()
    return r.json()


def parse_store_results(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Parse pickup-message response into a flat list of (store x color) dicts.
    Each dict has: color, sku, store_id, store_name, store_city, pickup_display,
    pickup_search_quote, store_pickup_quote, same_day_pickup, alert_this_color.
    """
    stores = (raw.get("body") or {}).get("stores") or []
    results = []
    today = _ist_today_yyyymmdd()

    for store in stores:
        store_id = (store.get("storeNumber") or "").upper()
        store_name = store.get("storeName") or store.get("address", {}).get("address") or ""
        store_city = store.get("city") or ""
        parts_avail = store.get("partsAvailability") or {}

        store_matches = (
            store_id in ALLOWED_STORE_IDS
            or any(t in store_name.lower() for t in ("saket", "noida"))
        )

        for color, sku in IPHONE16_128GB_COLORS.items():
            pa = parts_avail.get(sku) or {}
            pickup_display = pa.get("pickupDisplay") or ""
            pickup_search_quote = (pa.get("pickupSearchQuote") or "").strip()
            msg_regular = ((pa.get("messageTypes") or {}).get("regular") or {})
            store_pickup_quote = (msg_regular.get("storePickupQuote") or "").strip()

            # Available when pickupDisplay == "available" (not "ineligible" / "unavailable")
            api_available = pickup_display.lower() == "available"

            combined = f"{pickup_search_quote} {store_pickup_quote}".lower()
            same_day = "today" in combined or today in combined

            if SAME_DAY_ONLY:
                day_ok = same_day
            else:
                day_ok = api_available  # any future date counts too

            store_ok = store_matches or not REQUIRE_ALLOWED_STORE
            alert = api_available and store_ok and day_ok

            results.append({
                "color": color,
                "sku": sku,
                "store_id": store_id,
                "store_name": store_name,
                "store_city": store_city,
                "store_matches_target": store_matches,
                "pickup_display": pickup_display,
                "pickup_search_quote": pickup_search_quote,
                "store_pickup_quote": store_pickup_quote,
                "same_day_pickup": same_day,
                "api_available": api_available,
                "alert_this": alert,
            })

    return results


def send_ntfy_alert(pin: str, matches: List[Dict[str, Any]]):
    if not NTFY_TOPIC:
        print(f"[{get_ist_now()}] ntfy not configured (NTFY_TOPIC empty)")
        return False
    try:
        lines = [f"PIN {pin} - iPhone 16 128GB (6.1\") pickup:\n"]
        for r in matches:
            lines.append(
                f"- {r['color']}: {r['pickup_search_quote'] or 'Available'} "
                f"@ {r['store_name']} ({r['store_id']})"
            )
        lines.append(f"\nOrder: {PRODUCT_URL}")
        message = "\n".join(lines)
        title = f"iPhone 16: pickup available ({len(matches)} match(es)) - order now"
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": "iphone,apple",
                "Click": PRODUCT_URL,
            },
            timeout=15,
        ).raise_for_status()
        print(f"[{get_ist_now()}] ntfy.sh notification sent")
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] ntfy failed: {e}")
        return False


def send_email_alert(pin: str, matches: List[Dict[str, Any]]) -> bool:
    sender = os.environ.get("SENDER_EMAIL")
    receiver = os.environ.get("RECEIVER_EMAIL")
    password = os.environ.get("EMAIL_PASSWORD")
    if not all([sender, receiver, password]):
        print(f"[{get_ist_now()}] Email not configured (missing SENDER/RECEIVER/PASSWORD)")
        return False
    try:
        ist_time = get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST")
        lines = [
            f"iPhone 16 128GB — pickup available for PIN {pin}.",
            f"Stores: {','.join(ALLOWED_STORE_IDS)}  same_day_only={SAME_DAY_ONLY}",
            "",
        ]
        for r in matches:
            lines.append(
                f"- {r['color']}: {r['pickup_search_quote'] or 'Available'} "
                f"@ {r['store_name']} ({r['store_id']})"
            )
        lines.extend(["", PRODUCT_URL, "", f"Time: {ist_time}"])
        text_body = "\n".join(lines)
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"iPhone 16: pickup available — {len(matches)} match(es)"
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


def check_apple_iphone16():
    print("=" * 60)
    print(f"Apple iPhone 16 128GB pickup check — {get_ist_now()}")
    print(f"PIN code: {POSTAL_CODE}")
    print(f"Target stores: {', '.join(ALLOWED_STORE_IDS)} (Saket + Noida)  require={REQUIRE_ALLOWED_STORE}")
    print(f"Same-day only (IST): {SAME_DAY_ONLY}")
    print("=" * 60)

    try:
        raw = fetch_pickup_availability(POSTAL_CODE)
    except requests.RequestException as e:
        print(f"[{get_ist_now()}] Request failed: {e}")
        sys.exit(1)

    results = parse_store_results(raw)

    if not results:
        print(f"[{get_ist_now()}] No stores returned by API for PIN {POSTAL_CODE}.")
        return False

    saw_target = False
    alerts: List[Dict[str, Any]] = []

    for r in results:
        q = r["pickup_search_quote"] or r["pickup_display"] or "?"
        sd = "yes" if r["same_day_pickup"] else "no"
        al = "yes" if r["alert_this"] else "no"
        if r["store_matches_target"]:
            saw_target = True
            store_display = f"{r['store_name']} ({r['store_id']})"
        else:
            store_display = f"{r['store_name']} ({r['store_id']}) — not target"
        print(
            f"[{datetime.now()}] {r['color']:12} {r['sku']}  "
            f"today={sd}  alert={al}  {q!r}  {store_display}"
        )
        if r["alert_this"]:
            alerts.append(r)

    if not saw_target:
        print()
        print("!" * 60)
        print("NOTE: API returned no Saket (R756) or Noida (R787) for this PIN.")
        print("!" * 60)

    if not alerts:
        print(f"[{get_ist_now()}] No pickup matches (same-day + target store).")
        return False

    alert_summary = ", ".join(f"{r['color']} @ {r['store_name']}" for r in alerts)
    print(f"[{get_ist_now()}] Pickup alert: {alert_summary}")

    if not should_send_alert("apple_iphone16"):
        return False

    ntfy_ok = send_ntfy_alert(POSTAL_CODE, alerts)
    email_ok = send_email_alert(POSTAL_CODE, alerts)
    if ntfy_ok or email_ok:
        record_alert("apple_iphone16")
    return ntfy_ok or email_ok


if __name__ == "__main__":
    try:
        check_apple_iphone16()
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
