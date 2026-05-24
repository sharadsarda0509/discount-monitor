#!/usr/bin/env python3
"""
MagicPin PhonePe voucher stock monitor.

Fetches https://magicpin.in/Phonepe-offers/302198/ and parses the embedded
JSON-LD schema.org structured data to check if the Rs. 10000 gift card is
back in stock (availability == schema.org/InStock).

No browser or JS execution needed — the availability data is server-side
rendered into the HTML.
"""

import os
import re
import sys
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import requests
except ImportError:
    print("Error: pip install -r requirements.txt")
    sys.exit(1)

IST = timezone(timedelta(hours=5, minutes=30))

COOLDOWN_HOURS = float(os.environ.get("MAGICPIN_COOLDOWN_HOURS", os.environ.get("ALERT_COOLDOWN_HOURS", 1)))
STATE_DIR = Path(".alert_state")
STATE_FILE = STATE_DIR / "last_alert.json"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

PAGE_URL = "https://magicpin.in/Phonepe-offers/302198/"

# Watch these denominations (case-insensitive substring match on offer name)
WATCH_AMOUNTS = [10000]


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


def fetch_page_html() -> str:
    r = requests.get(
        PAGE_URL,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-IN,en;q=0.9",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.text


def parse_offers(html: str) -> List[Dict[str, Any]]:
    """
    Extract offer availability from the JSON-LD block embedded in the page.
    Returns list of dicts with keys: name, availability, url, in_stock.
    """
    # Find all <script type="application/ld+json"> blocks
    blocks = re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL)
    for block in blocks:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        offers_raw = (data.get("offers") or {}).get("offers") or []
        if not offers_raw:
            continue
        results = []
        for o in offers_raw:
            avail = o.get("availability", "")
            results.append({
                "name": o.get("name", ""),
                "availability": avail,
                "url": o.get("url", PAGE_URL),
                "in_stock": avail.endswith("InStock"),
            })
        return results
    return []


def send_ntfy_alert(matches: List[Dict[str, Any]]) -> bool:
    if not NTFY_TOPIC:
        print(f"[{get_ist_now()}] ntfy not configured (NTFY_TOPIC empty)")
        return False
    try:
        lines = ["MagicPin PhonePe voucher back in stock!\n"]
        for m in matches:
            lines.append(f"- {m['name']}")
        lines.append(f"\nBuy now: {PAGE_URL}")
        message = "\n".join(lines)
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": f"PhonePe voucher in stock: {', '.join(m['name'] for m in matches)}",
                "Priority": "high",
                "Tags": "phonepe,voucher,magicpin",
                "Click": PAGE_URL,
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
        print(f"[{get_ist_now()}] Email not configured (missing SENDER/RECEIVER/PASSWORD)")
        return False
    try:
        ist_time = get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST")
        lines = ["MagicPin PhonePe voucher back in stock!\n"]
        for m in matches:
            lines.append(f"- {m['name']}: {m['url']}")
        lines.extend(["", f"Buy now: {PAGE_URL}", "", f"Time: {ist_time}"])
        text_body = "\n".join(lines)
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"PhonePe voucher in stock: {', '.join(m['name'] for m in matches)}"
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


def check_magicpin_phonepe():
    print("=" * 60)
    print(f"MagicPin PhonePe voucher check — {get_ist_now()}")
    print(f"Watching amounts: Rs. {WATCH_AMOUNTS}")
    print("=" * 60)

    try:
        html = fetch_page_html()
    except requests.RequestException as e:
        print(f"[{get_ist_now()}] Request failed: {e}")
        sys.exit(1)

    offers = parse_offers(html)
    if not offers:
        print(f"[{get_ist_now()}] Could not parse offers from page — structure may have changed")
        sys.exit(1)

    matches_in_stock = []
    for offer in offers:
        amount_match = any(str(amt) in offer["name"] for amt in WATCH_AMOUNTS)
        status = "IN STOCK" if offer["in_stock"] else "sold out"
        print(f"[{get_ist_now()}] {offer['name']:35s}  {status}")
        if amount_match and offer["in_stock"]:
            matches_in_stock.append(offer)

    if not matches_in_stock:
        print(f"[{get_ist_now()}] No watched vouchers in stock.")
        return False

    print(f"[{get_ist_now()}] IN STOCK: {[m['name'] for m in matches_in_stock]}")

    if not should_send_alert("magicpin_phonepe"):
        return False

    ntfy_ok = send_ntfy_alert(matches_in_stock)
    email_ok = send_email_alert(matches_in_stock)
    if ntfy_ok or email_ok:
        record_alert("magicpin_phonepe")
    return ntfy_ok or email_ok


if __name__ == "__main__":
    try:
        check_magicpin_phonepe()
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
