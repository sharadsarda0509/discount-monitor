#!/usr/bin/env python3
"""
Apple India — iPhone 16 (6.1\" 128GB) pickup / availability monitor

Uses the same Shop-by-Availability endpoint the Apple store web UI loads after
the buy-flow page (discovered via browser network capture):

  GET /in/shop/sba/availability-message?fae=true&parts.0=<SKU>&postalCode=<PIN>

Related endpoints seen on the product page (fulfillmentBootstrap / PRODUCT_AVAILABILITY_BOOTSTRAP):
  - /in/shop/fulfillment-messages?fae=true — delivery + pickup copy
  - /in/shop/sba/availability-message — structured pickup + delivery availability

The `postalCode` query alone does not reliably pin the response to that PIN: Apple
also uses geo/IP and session cookies, so the API may pick a store **outside** Saket/Noida
unless your session matches the Delhi area.

This monitor only **cares about Apple Saket and Apple Noida** (retail IDs **R756** and
**R787**). Other stores are **ignored** and are **not named** in logs so you only see
pickup data relevant to Saket/Noida. Override IDs with `APPLE_STORE_IDS` if needed.

To get Saket or Noida in the API response, use a Delhi/NCR IP or `APPLE_COOKIES` from
the browser after choosing PIN + pickup at Saket or Noida — see below.

**Pickup dates:** If `APPLE_PICKUP_DATES` is **not** set, the script does not target a
specific calendar day. With `APPLE_SAME_DAY_ONLY=true` (default), it only matches
**same-day pickup** (“Available Today” / today’s encoded date in IST) — i.e. it tracks
**daily** pickup availability per run. Set `APPLE_PICKUP_DATES` (e.g. `20260331`) when
you also want alerts for specific future pickup dates.

Pattern mirrors check_noones.py: fetch → interpret → optional ntfy + email + cooldown.

CI (no browser): add GitHub secret `APPLE_COOKIES` with a Cookie header copied from
DevTools after you set PIN and pick Saket or Noida on the buy page. Without it,
Apple may resolve a different city from the runner IP — set `APPLE_REQUIRE_SAKET=false`
only if you accept “today” alerts for whatever store the API returns (not recommended).
"""

import os
import re
import sys
import json
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

IST = timezone(timedelta(hours=5, minutes=30))

COOLDOWN_HOURS = int(os.environ.get("ALERT_COOLDOWN_HOURS", 24))
STATE_DIR = Path(".alert_state")
STATE_FILE = STATE_DIR / "last_alert.json"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# Only alert when pickup date is “today” in IST (matches “Available Today” or encoded date).
SAME_DAY_ONLY = os.environ.get("APPLE_SAME_DAY_ONLY", "true").lower() in (
    "1",
    "true",
    "yes",
)

# Optional: comma-separated YYYYMMDD (Apple pickupEncodedUpperDateString). Unset = no
# fixed dates; with SAME_DAY_ONLY=true (default), only “today” pickup counts (daily stock).
# If set, enc in this set OR (when SAME_DAY_ONLY) same-day pickup still matches.
def _allowed_pickup_date_set() -> Optional[Tuple[str, ...]]:
    raw = os.environ.get("APPLE_PICKUP_DATES", "").strip()
    if not raw:
        return None
    dates = tuple(
        x.strip()
        for x in raw.split(",")
        if len(x.strip()) == 8 and x.strip().isdigit()
    )
    return dates if dates else None


ALLOWED_PICKUP_DATES: Optional[Tuple[str, ...]] = _allowed_pickup_date_set()

# Store must be one of ALLOWED_STORE_IDS (Saket/Noida by default). Set false only if
# you accept any store the API returns (e.g. blind CI). Env name kept for compatibility.
REQUIRE_ALLOWED_STORE = os.environ.get(
    "APPLE_REQUIRE_STORE_MATCH",
    os.environ.get("APPLE_REQUIRE_SAKET", "true"),
).lower() in (
    "1",
    "true",
    "yes",
)

# Default: Delhi PIN (Saket area). Override with APPLE_PINCODE or POSTAL_CODE.
POSTAL_CODE = os.environ.get("APPLE_PINCODE") or os.environ.get("POSTAL_CODE") or "110017"

# Apple Saket (R756), Apple Noida (R787) — https://www.apple.com/in/retail/saket/ , .../noida/
# Override via APPLE_STORE_IDS="R756,R787" or legacy APPLE_STORE_ID for a single id.
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

# Optional: paste Cookie header value from DevTools after opening the buy page,
# entering PIN 110017, and selecting pickup at Apple Saket (so `as_gloc` / etc.
# match Delhi). Example: export APPLE_COOKIES='as_dc=...; as_gloc=...; ...'
APPLE_COOKIES = os.environ.get("APPLE_COOKIES", "").strip()

# Product page used to seed session cookies (same family as all 6.1" 128GB colors).
PRODUCT_URL = (
    "https://www.apple.com/in/shop/buy-iphone/iphone-16/"
    "6.1%22-display-128gb-pink"
)

AVAILABILITY_URL = "https://www.apple.com/in/shop/sba/availability-message"
SBA_INIT_PATTERN = r'v2UserInterestUrl":"([^"]+)"'

# iPhone 16 6.1" 128GB — India SKUs (from JSON-LD sku on each color URL). Teal omitted.
IPHONE16_128GB_COLORS = {
    "ultramarine": "MYEC3HN/A",
    "pink": "MYEA3HN/A",
    "white": "MYE93HN/A",
    "black": "MYE73HN/A",
}

try:
    import requests
except ImportError:
    print("Error: pip install -r requirements.txt")
    sys.exit(1)


def get_ist_now():
    return datetime.now(IST)


def _ist_today_yyyymmdd() -> str:
    return get_ist_now().strftime("%Y%m%d")


def _store_in_allowlist(store_id: str, store_name: str) -> bool:
    """True if retail id or display name matches Saket / Noida (or APPLE_STORE_IDS)."""
    sid = (store_id or "").strip().upper()
    if sid and sid in ALLOWED_STORE_IDS:
        return True
    nl = (store_name or "").lower()
    for token in ("saket", "noida"):
        if token in nl:
            return True
    return False


def _is_same_day_pickup(
    pickup_search_quote: str,
    store_pickup_quote: str,
    encoded_date: str,
) -> bool:
    """True if Apple indicates same-day pickup (IST ‘today’)."""
    combined = f"{pickup_search_quote} {store_pickup_quote}".lower()
    if "today" in combined:
        return True
    if encoded_date and len(encoded_date) == 8 and encoded_date.isdigit():
        return encoded_date == _ist_today_yyyymmdd()
    return False


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
            print(
                f"[{get_ist_now()}] Cooldown active for {alert_type}: "
                f"{elapsed_h:.1f}h / {COOLDOWN_HOURS}h"
            )
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


def _session_headers():
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-IN,en;q=0.9",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }


def _apply_cookie_string(session: requests.Session, cookie_header: str) -> None:
    """Apply a browser Cookie header (name=value; name2=value2) to the session."""
    for part in cookie_header.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        session.cookies.set(name.strip(), value.strip(), domain=".apple.com", path="/")


def _call_sba_init(session: requests.Session, product_html: str) -> None:
    """Warm session the same way the web UI does (sets location / SBA cookies)."""
    m = re.search(SBA_INIT_PATTERN, product_html)
    if not m:
        return
    path = m.group(1)
    if not path.startswith("http"):
        url = "https://www.apple.com" + path
    else:
        url = path
    session.get(
        url,
        headers={**_session_headers(), "Referer": PRODUCT_URL},
        timeout=45,
    )


def build_session():
    session = requests.Session()
    session.headers.update(_session_headers())
    if APPLE_COOKIES:
        _apply_cookie_string(session, APPLE_COOKIES)
    r = session.get(PRODUCT_URL, timeout=45)
    r.raise_for_status()
    _call_sba_init(session, r.text)
    return session


def fetch_availability(session: requests.Session, part_number: str, postal_code: str):
    """GET /in/shop/sba/availability-message for one SKU."""
    params = {"fae": "true", "parts.0": part_number, "postalCode": postal_code}
    headers = {
        "Referer": PRODUCT_URL,
    }
    r = session.get(AVAILABILITY_URL, params=params, headers=headers, timeout=45)
    r.raise_for_status()
    return r.json()


def parse_pickup_row(body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Extract pickup summary from availability-message body.
    Returns None if not present or malformed.
    """
    if not body or "body" not in body:
        return None
    content = body["body"].get("content") or []
    if not content:
        return None
    row = content[0]
    pm = row.get("pickupMessage") or {}
    addr = (pm.get("address") or {}) if isinstance(pm.get("address"), dict) else {}
    quote = (pm.get("pickupSearchQuote") or "").strip()
    store_name = addr.get("address") or ""
    pickup_pc = addr.get("postalCode") or ""
    city = pm.get("city") or ""
    store_id = (row.get("storeId") or pm.get("storeId") or "").strip().upper()
    available = bool(row.get("availableAtAnyStore"))
    n_stores = int(row.get("partAvailableStoresCount") or 0)
    # Heuristic: explicit "unavailable" phrasing
    lowered = quote.lower()
    looks_unavailable = "unavailable" in lowered or "not available" in lowered
    api_says_pickup = available and n_stores > 0 and not looks_unavailable

    store_matches_target = _store_in_allowlist(store_id, store_name)

    enc = (
        row.get("pickupEncodedUpperDateString")
        or row.get("encodedUpperDateString")
        or pm.get("pickupEncodedUpperDateString")
        or ""
    )
    if isinstance(enc, str):
        enc = enc.strip()
    else:
        enc = ""

    spq = pm.get("storePickupQuote") or ""
    same_day = _is_same_day_pickup(quote, spq, enc)

    if ALLOWED_PICKUP_DATES is not None:
        in_list = bool(enc and enc in ALLOWED_PICKUP_DATES)
        day_ok = in_list or (SAME_DAY_ONLY and same_day)
    elif SAME_DAY_ONLY:
        day_ok = same_day
    else:
        day_ok = True

    store_ok = store_matches_target or not REQUIRE_ALLOWED_STORE
    alert_this_color = bool(api_says_pickup and store_ok and day_ok)

    dm = row.get("deliveryMessage") or {}
    delivery_pc = (dm.get("address") or {}).get("postalCode") or ""
    buyable = bool((dm.get("buyability") or {}).get("isBuyable"))

    return {
        "part_number": row.get("partNumber"),
        "store_id": store_id,
        "store_pickup_quote": pm.get("storePickupQuote") or "",
        "pickup_search_quote": quote,
        "store_name": store_name,
        "store_city": city,
        "store_postal": pickup_pc,
        "part_available_stores_count": n_stores,
        "available_at_any_store": available,
        "api_pickup_ok": api_says_pickup,
        "store_matches_target": store_matches_target,
        "same_day_pickup": same_day,
        "pickup_date_encoded": enc,
        "pickup_date_ok": day_ok,
        "alert_this_color": alert_this_color,
        "delivery_postal_hint": delivery_pc,
        "delivery_buyable": buyable,
    }


def summarize_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Colors that pass store + same-day rules and should trigger notifications."""
    return [r for r in results if r.get("alert_this_color")]


def send_ntfy_alert(pin: str, matches: List[Dict[str, Any]]):
    if not NTFY_TOPIC:
        print(f"[{get_ist_now()}] ntfy not configured (NTFY_TOPIC empty)")
        return False
    try:
        lines = [
            f"PIN {pin} — iPhone 16 128GB (6.1\") pickup at Saket or Noida (IST):\n",
        ]
        for r in matches:
            lines.append(
                f"- {r['color']}: {r['pickup_search_quote'] or 'OK'} @ "
                f"{r['store_name'] or '?'} ({r.get('store_id', '')})"
            )
        lines.append(f"\nOrder: {PRODUCT_URL}")
        message = "\n".join(lines)
        title = f"iPhone 16: today pickup ({len(matches)} color(s)) — order now"
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
            f"iPhone 16 128GB — pickup at Saket or Noida (IST) for PIN {pin}.",
            f"Allowlist: {','.join(ALLOWED_STORE_IDS)}  require_allowlist={REQUIRE_ALLOWED_STORE}  "
            f"same_day_filter={SAME_DAY_ONLY}.",
            "",
        ]
        for r in matches:
            lines.append(
                f"- {r['color']}: {r.get('pickup_search_quote') or ''} @ "
                f"{r.get('store_name')} ({r.get('store_id')})"
            )
        lines.extend(["", PRODUCT_URL, "", f"Time: {ist_time}"])
        text_body = "\n".join(lines)
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"iPhone 16: today pickup — {len(matches)} color(s)"
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
    print(
        f"Allowed stores: {', '.join(ALLOWED_STORE_IDS)} (Saket + Noida)  "
        f"require_allowlist={REQUIRE_ALLOWED_STORE}"
    )
    print(f"Same-day only (IST): {SAME_DAY_ONLY}")
    if ALLOWED_PICKUP_DATES is not None:
        print(f"Also alert on pickup dates (YYYYMMDD): {', '.join(ALLOWED_PICKUP_DATES)}")
    if APPLE_COOKIES:
        print("Using APPLE_COOKIES from environment (browser session)")
    else:
        print(
            "Tip: for Saket/Noida in CI, set GitHub secret APPLE_COOKIES (Cookie header "
            "from browser after PIN + pickup at Saket or Noida)."
        )
    print("=" * 60)

    session = build_session()
    results: List[Dict[str, Any]] = []

    for color, sku in IPHONE16_128GB_COLORS.items():
        try:
            raw = fetch_availability(session, sku, POSTAL_CODE)
            info = parse_pickup_row(raw) or {}
            info["color"] = color
            info["sku"] = sku
            results.append(info)
            q = info.get("pickup_search_quote") or "?"
            sd = "yes" if info.get("same_day_pickup") else "no"
            pdo = "yes" if info.get("pickup_date_ok") else "no"
            al = "yes" if info.get("alert_this_color") else "no"
            store = info.get("store_name") or ""
            sid = info.get("store_id") or ""
            if info.get("store_matches_target"):
                store_display = f"{store} ({sid})" if store else f"({sid})"
            else:
                store_display = "not Saket/Noida (ignored)"
            print(
                f"[{datetime.now()}] {color:12} {sku}  today={sd}  date_ok={pdo}  "
                f"alert={al}  {q!r}  {store_display}"
            )
        except requests.RequestException as e:
            print(f"[{datetime.now()}] {color}: request error {e}")
            results.append(
                {
                    "color": color,
                    "sku": sku,
                    "alert_this_color": False,
                    "error": str(e),
                }
            )
        time.sleep(0.5)

    good = summarize_results(results)
    if not good:
        print(f"[{get_ist_now()}] No Saket/Noida pickup matches (date rules + allowlist).")
        return False

    print(
        f"[{get_ist_now()}] Saket/Noida pickup alert for: {', '.join(r['color'] for r in good)}"
    )

    if not should_send_alert("apple_iphone16"):
        return False

    ntfy_ok = send_ntfy_alert(POSTAL_CODE, good)
    email_ok = send_email_alert(POSTAL_CODE, good)
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
