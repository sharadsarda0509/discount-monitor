#!/usr/bin/env python3
"""
Shopsy/Flipkart Physical Gift Card stock monitor.

Fetches the product page and parses the JSON-LD schema.org structured data
to detect when the card comes back in stock (availability == InStock).
Server-side rendered -- no JS execution needed.
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
    import requests
except ImportError:
    print("Error: pip install -r requirements.txt")
    sys.exit(1)

IST = timezone(timedelta(hours=5, minutes=30))

COOLDOWN_HOURS = float(os.environ.get("SHOPSY_GC_COOLDOWN_HOURS", os.environ.get("ALERT_COOLDOWN_HOURS", 24)))
STATE_DIR = Path(".alert_state")
STATE_FILE = STATE_DIR / "last_alert.json"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# All Flipkart physical gift card denominations to monitor
# Add more (pid, denomination) tuples here if needed
PRODUCTS = [
    {
        "url": "https://www.shopsy.in/flipkart-physical-gift-card/p/itm1c58f3d21f4fd",
        "pid": "PGVHGPHTYGY4WDUT",
        "denomination": "Rs.1000",
    },
]


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


def fetch_availability(product: Dict) -> Optional[bool]:
    """
    Returns True if in stock, False if OOS, None if parse failed.
    """
    try:
        r = requests.get(
            product["url"],
            params={"pid": product["pid"], "marketplace": "FLIPKART"},
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
    except requests.RequestException as e:
        print(f"  [{product['denomination']}] Request failed: {e}")
        return None

    # JSON-LD structured data (server-rendered, most reliable)
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        r.text, re.DOTALL,
    )
    for block in blocks:
        try:
            data = json.loads(block)
            # Handle both list and dict top-level
            items = data if isinstance(data, list) else [data]
            for item in items:
                avail = (item.get("offers") or {}).get("availability", "")
                if avail:
                    in_stock = avail.endswith("InStock")
                    return in_stock
        except json.JSONDecodeError:
            continue

    # Fallback: inline productStatus JSON
    m = re.search(r'"productStatus"\s*:\s*"([^"]+)"', r.text)
    if m:
        return "out of stock" not in m.group(1).lower()

    print(f"  [{product['denomination']}] Could not parse availability")
    return None


def send_ntfy_alert(in_stock_products: List[Dict]) -> bool:
    if not NTFY_TOPIC:
        print(f"[{get_ist_now()}] ntfy not configured (NTFY_TOPIC empty)")
        return False
    try:
        lines = ["Flipkart Physical Gift Card back in stock on Shopsy!\n"]
        for p in in_stock_products:
            lines.append(f"- {p['denomination']}: {p['url']}?pid={p['pid']}")
        message = "\n".join(lines)
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": f"Flipkart Gift Card in stock: {', '.join(p['denomination'] for p in in_stock_products)}",
                "Priority": "high",
                "Tags": "gift,shopping,flipkart",
                "Click": in_stock_products[0]["url"],
            },
            timeout=15,
        ).raise_for_status()
        print(f"[{get_ist_now()}] ntfy notification sent")
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] ntfy failed: {e}")
        return False


def send_email_alert(in_stock_products: List[Dict]) -> bool:
    sender = os.environ.get("SENDER_EMAIL")
    receiver = os.environ.get("RECEIVER_EMAIL")
    password = os.environ.get("EMAIL_PASSWORD")
    if not all([sender, receiver, password]):
        print(f"[{get_ist_now()}] Email not configured")
        return False
    try:
        ist_time = get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST")
        lines = ["Flipkart Physical Gift Card back in stock on Shopsy!\n"]
        for p in in_stock_products:
            lines.append(f"  - {p['denomination']}: {p['url']}?pid={p['pid']}")
        lines.append(f"\nChecked at: {ist_time}")
        text_body = "\n".join(lines)
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Flipkart Gift Card in stock: {', '.join(p['denomination'] for p in in_stock_products)}"
        msg["From"] = sender
        msg["To"] = receiver
        msg.attach(MIMEText(text_body, "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(sender, password)
            srv.send_message(msg)
        print(f"[{get_ist_now()}] Email sent")
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] Email failed: {e}")
        return False


def check_shopsy_giftcard():
    print("=" * 60)
    print(f"Shopsy Flipkart Gift Card monitor -- {get_ist_now()}")
    print(f"Watching {len(PRODUCTS)} denomination(s): {[p['denomination'] for p in PRODUCTS]}")
    print("=" * 60)

    in_stock: List[Dict] = []
    for product in PRODUCTS:
        avail = fetch_availability(product)
        status = "IN STOCK" if avail is True else ("OOS" if avail is False else "UNKNOWN")
        print(f"  [{product['denomination']}]: {status}")
        if avail is True:
            in_stock.append(product)

    if not in_stock:
        print(f"[{get_ist_now()}] No denominations in stock.")
        return False

    print(f"[{get_ist_now()}] IN STOCK: {[p['denomination'] for p in in_stock]}")

    if not should_send_alert("shopsy_giftcard"):
        return False

    ntfy_ok = send_ntfy_alert(in_stock)
    email_ok = send_email_alert(in_stock)
    if ntfy_ok or email_ok:
        record_alert("shopsy_giftcard")
    return ntfy_ok or email_ok


if __name__ == "__main__":
    try:
        check_shopsy_giftcard()
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
