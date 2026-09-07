#!/usr/bin/env python3
"""
Apple India -- iPhone 17 (6.3" 128GB/256GB/512GB) same-day pickup monitor.

Checks Apple Saket (R756) and Apple Noida (R787) for today-only pickup.
Mirrors check_apple_iphone16.py — same API, same logic, updated SKUs.
"""

import os
import sys
import json
import time
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

# Same-day pickup stock at Saket/Noida trickles in and vanishes within minutes. The
# workflow only fires every 5 min, so instead of a single snapshot per run we poll the
# (free, direct) Apple pickup API in a tight loop for POLL_TOTAL_SECONDS at
# POLL_INTERVAL_SECONDS cadence -- cutting detection latency from ~5 min to ~1 min.
# POLL_TOTAL_SECONDS=0 (default) keeps the original single-shot behaviour for local runs.
POLL_TOTAL_SECONDS = float(os.environ.get("APPLE_POLL_SECONDS", 0))
POLL_INTERVAL_SECONDS = float(os.environ.get("APPLE_POLL_INTERVAL", 45))

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

SAME_DAY_ONLY = os.environ.get("APPLE_SAME_DAY_ONLY", "true").lower() in ("1", "true", "yes")

POSTAL_CODE = os.environ.get("APPLE_PINCODE") or os.environ.get("POSTAL_CODE") or "110017"

_DEFAULT_STORE_IDS: Tuple[str, ...] = ("R756", "R787")


def _allowed_store_ids() -> Tuple[str, ...]:
    raw = os.environ.get("APPLE_STORE_IDS", "").strip()
    if raw:
        return tuple(x.strip().upper() for x in raw.split(",") if x.strip())
    return _DEFAULT_STORE_IDS


ALLOWED_STORE_IDS: Tuple[str, ...] = _allowed_store_ids()

REQUIRE_ALLOWED_STORE = os.environ.get(
    "APPLE_REQUIRE_STORE_MATCH",
    os.environ.get("APPLE_REQUIRE_SAKET", "true"),
).lower() in ("1", "true", "yes")

PICKUP_MSG_URL = "https://www.apple.com/in/shop/retail/pickup-message"

PRODUCT_URL = "https://www.apple.com/in/shop/buy-iphone/iphone-17/6.3-inch-display-256gb-black"

# iPhone 17 6.3" 256GB — India SKUs
IPHONE17_COLORS = {
    "black":      "MG6J4HN/A",
    "white":      "MG6K4HN/A",
    "mist-blue":  "MG6L4HN/A",
    "lavender":   "MG6M4HN/A",
    "sage":       "MG6N4HN/A",
}


def _buy_link(sku: str) -> str:
    """Buy box for this exact SKU. Deliberately drops the old `&step=start`: that jumped
    straight to the DELIVERY review step, which shows 'Currently unavailable' for a hot
    launch -- so tapping the alert looked like the item was already gone, even when
    same-day PICKUP at Saket/Noida (what we actually alert on) was live. Landing on the
    product buy box instead surfaces the Pick Up tab with the real store availability."""
    return f"https://www.apple.com/in/shop/buy-iphone?product={sku}"


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
    params: Dict[str, str] = {"pl": "true", "location": postal_code}
    for i, sku in enumerate(IPHONE17_COLORS.values()):
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

        for color, sku in IPHONE17_COLORS.items():
            pa = parts_avail.get(sku) or {}
            pickup_display = pa.get("pickupDisplay") or ""
            pickup_search_quote = (pa.get("pickupSearchQuote") or "").strip()
            msg_regular = ((pa.get("messageTypes") or {}).get("regular") or {})
            store_pickup_quote = (msg_regular.get("storePickupQuote") or "").strip()

            api_available = pickup_display.lower() == "available"

            combined = f"{pickup_search_quote} {store_pickup_quote}".lower()
            same_day = "today" in combined or today in combined

            day_ok = same_day if SAME_DAY_ONLY else api_available
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
        lines = [f"PIN {pin} - iPhone 17 SAME-DAY PICKUP available!\n"]
        for r in matches:
            lines.append(
                f"- {r['color']}: {r['pickup_search_quote'] or 'Today'} "
                f"@ {r['store_name']} ({r['store_id']})"
            )
            lines.append(f"  Open: {_buy_link(r['sku'])}")
        # On the buy box, pick "Pick Up" at the store above -- the Delivery tab shows
        # unavailable for hot launches, which is what made past alerts look already-OOS.
        lines.append("\nOn the page choose PICK UP at the store above (Delivery shows unavailable).")
        click_url = _buy_link(matches[0]["sku"])
        lines.append(f"Quick open: {click_url}")
        message = "\n".join(lines)
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": f"iPhone 17 pickup today: {len(matches)} match(es) - order now",
                "Priority": "high",
                "Tags": "iphone,apple",
                "Click": click_url,
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
        print(f"[{get_ist_now()}] Email not configured")
        return False
    try:
        ist_time = get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST")
        lines = [
            f"iPhone 17 same-day pickup available for PIN {pin}.",
            f"Stores: {','.join(ALLOWED_STORE_IDS)}  same_day_only={SAME_DAY_ONLY}",
            "",
        ]
        for r in matches:
            lines.append(
                f"- {r['color']}: {r['pickup_search_quote'] or 'Today'} "
                f"@ {r['store_name']} ({r['store_id']})"
            )
            lines.append(f"    Open buy box: {_buy_link(r['sku'])}")
        lines.extend([
            "",
            "On the page choose PICK UP at the store above -- the Delivery tab shows "
            "'Currently unavailable' for hot launches, which is not the pickup stock we alert on.",
            f"Quick open: {_buy_link(matches[0]['sku'])}",
            "",
            f"Time: {ist_time}",
        ])
        text_body = "\n".join(lines)
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"iPhone 17: same-day pickup available -- {len(matches)} match(es)"
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


def _scan_once() -> List[Dict[str, Any]]:
    """One fetch + parse + log pass. Returns the alertable matches (same-day pickup at a
    target store), or [] on a request error / no matches."""
    try:
        raw = fetch_pickup_availability(POSTAL_CODE)
    except requests.RequestException as e:
        print(f"[{get_ist_now()}] Request failed: {e}")
        return []

    results = parse_store_results(raw)
    if not results:
        print(f"[{get_ist_now()}] No stores returned by API for PIN {POSTAL_CODE}.")
        return []

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
            store_display = f"{r['store_name']} ({r['store_id']}) -- not target"
        print(
            f"[{datetime.now()}] {r['color']:20} {r['sku']}  "
            f"today={sd}  alert={al}  {q!r}  {store_display}"
        )
        if r["alert_this"]:
            alerts.append(r)

    if not saw_target:
        print("!" * 60)
        print("NOTE: API returned no Saket (R756) or Noida (R787) for this PIN.")
        print("!" * 60)

    if not alerts:
        print(f"[{get_ist_now()}] No same-day pickup matches at target stores.")
    return alerts


def check_apple_iphone17():
    print("=" * 60)
    print(f"Apple iPhone 17 same-day pickup check -- {get_ist_now()}")
    print(f"PIN code: {POSTAL_CODE}")
    print(f"Target stores: {', '.join(ALLOWED_STORE_IDS)} (Saket + Noida)  require={REQUIRE_ALLOWED_STORE}")
    print(f"Same-day only (IST): {SAME_DAY_ONLY}")
    print(f"Poll window: {POLL_TOTAL_SECONDS:.0f}s @ {POLL_INTERVAL_SECONDS:.0f}s")
    print("=" * 60)

    deadline = time.monotonic() + POLL_TOTAL_SECONDS
    poll = 0
    while True:
        poll += 1
        if POLL_TOTAL_SECONDS > 0:
            print(f"--- poll #{poll} @ {get_ist_now()} ---")

        alerts = _scan_once()
        if alerts:
            summary = ", ".join(f"{r['color']} @ {r['store_name']}" for r in alerts)
            print(f"[{get_ist_now()}] Same-day pickup alert: {summary}")

            if not should_send_alert("apple_iphone17"):
                return False  # alerted <cooldown ago -> polling more this run is pointless

            # Re-verify right before sending: hot pickup stock vanishes in minutes, so a
            # match from a few seconds/polls ago may already be stale. Only send what is
            # still live on a fresh fetch.
            confirmed = _scan_once()
            if not confirmed:
                print(f"[{get_ist_now()}] Vanished on re-verify -- not sending; continue polling.")
            else:
                ntfy_ok = send_ntfy_alert(POSTAL_CODE, confirmed)
                email_ok = send_email_alert(POSTAL_CODE, confirmed)
                if ntfy_ok or email_ok:
                    record_alert("apple_iphone17")
                return ntfy_ok or email_ok

        if time.monotonic() + POLL_INTERVAL_SECONDS >= deadline:
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    return False


if __name__ == "__main__":
    try:
        check_apple_iphone17()
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
