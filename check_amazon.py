#!/usr/bin/env python3
"""
Amazon Pay Physical Gift Card discount monitor.

Uses Amazon search page (less bot-protected than product pages) to check
if any Amazon Pay physical gift card has an upfront discount ≥ MIN_DISCOUNT_PCT.

Session cookies are long-lived anonymous guest cookies (session-id-time ~2036).
"""

import os
import sys
import json
import smtplib
import re
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

IST = timezone(timedelta(hours=5, minutes=30))
COOLDOWN_HOURS = int(os.environ.get('ALERT_COOLDOWN_HOURS', 12))
STATE_DIR = Path('.alert_state')
STATE_FILE = STATE_DIR / 'last_alert.json'
NTFY_TOPIC = os.environ.get('NTFY_TOPIC', '')

SEARCH_URL = "https://www.amazon.in/s?k=amazon+pay+physical+gift+card"
MIN_DISCOUNT_PCT = float(os.environ.get("AMAZON_MIN_DISCOUNT", 2.0))

# Long-lived anonymous guest session cookies (session-id-time expires ~2036).
_SESSION_COOKIES = {
    "session-id": "524-8245774-6919306",
    "session-id-time": "2082787201l",
    "i18n-prefs": "INR",
    "lc-acbin": "en_IN",
    "ubid-acbin": "259-8092123-3440844",
}

try:
    from curl_cffi import requests as cffi_requests
    from bs4 import BeautifulSoup
    _SESSION = cffi_requests.Session(impersonate="chrome120")
    for k, v in _SESSION_COOKIES.items():
        _SESSION.cookies.set(k, v, domain=".amazon.in")
except ImportError:
    try:
        import requests as _fallback
        from bs4 import BeautifulSoup
        _SESSION = _fallback.Session()
        _SESSION.cookies.update(_SESSION_COOKIES)
    except ImportError:
        print("Error: pip install -r requirements.txt")
        sys.exit(1)


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
            print(f"[{get_ist_now()}] Cooldown active for amazon: {elapsed_h:.1f}h / {COOLDOWN_HOURS}h")
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


def _parse_price(text: str) -> Optional[float]:
    cleaned = re.sub(r"[₹,\s]", "", text.strip())
    try:
        return float(cleaned)
    except ValueError:
        return None


def fetch_search_results() -> List[Dict[str, Any]]:
    r = _SESSION.get(SEARCH_URL, timeout=30)
    r.raise_for_status()

    if "validateCaptcha" in r.text or "Enter the characters" in r.text:
        print(f"[{get_ist_now()}] Amazon CAPTCHA detected -- session cookies may have expired")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    items = []
    for div in soup.find_all("div", {"data-component-type": "s-search-result"}):
        asin = div.get("data-asin", "")
        title_el = div.find("h2")
        price_el = div.find("span", class_="a-price-whole")
        strike_el = div.find("span", class_="a-text-price")

        if not title_el or not price_el:
            continue

        title = title_el.get_text(strip=True)
        if "amazon pay" not in title.lower():
            continue

        current_price = _parse_price(price_el.get_text())
        if current_price is None:
            continue

        original_price = None
        if strike_el:
            strike_price_el = strike_el.find("span")
            if strike_price_el:
                original_price = _parse_price(strike_price_el.get_text())

        discount_pct = 0.0
        discount_label = ""
        if original_price and original_price > current_price:
            discount_pct = (original_price - current_price) / original_price * 100

        # Also check badge text: "5% off", "Save ₹200", coupon clips etc.
        badge_el = div.find("span", class_=re.compile(r"savingsPercentage|a-badge-text|s-coupon"))
        if badge_el:
            badge_text = badge_el.get_text(strip=True)
            discount_label = badge_text
            if discount_pct == 0:
                m = re.search(r"(\d+)%", badge_text)
                if m:
                    discount_pct = float(m.group(1))

        # Check any element with "% off" text inside the item
        if discount_pct == 0:
            pct_el = div.find(string=re.compile(r"\d+%\s*off", re.I))
            if pct_el:
                m = re.search(r"(\d+)%", pct_el)
                if m:
                    discount_pct = float(m.group(1))
                    discount_label = pct_el.strip()

        items.append({
            "asin": asin,
            "title": title[:80],
            "current_price": current_price,
            "original_price": original_price,
            "discount_pct": discount_pct,
            "discount_label": discount_label,
            "url": f"https://www.amazon.in/dp/{asin}",
        })

    return items


def send_ntfy_alert(matches: List[Dict[str, Any]]) -> bool:
    if not NTFY_TOPIC:
        print(f"[{get_ist_now()}] ntfy not configured")
        return False
    try:
        lines = ["Amazon Pay Gift Card discount found!\n"]
        for m in matches:
            orig = f", was Rs.{int(m['original_price'])}" if m.get('original_price') else ""
            lines.append(
                f"- {m['title']}: Rs.{int(m['current_price'])}{orig} — {m['discount_pct']:.1f}% off"
            )
            lines.append(f"  Buy: {m['url']}")
        message = "\n".join(lines)
        _SESSION.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": f"Amazon GC discount: {len(matches)} item(s)",
                "Priority": "high",
                "Tags": "amazon,gift,discount",
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
        lines = ["Amazon Pay Gift Card discount!\n"]
        for m in matches:
            orig = f", was Rs.{int(m['original_price'])}" if m.get('original_price') else ""
            lines.append(
                f"- {m['title']}: Rs.{int(m['current_price'])}{orig} — {m['discount_pct']:.1f}% off\n  {m['url']}"
            )
        lines.extend(["", f"Search: {SEARCH_URL}", "", f"Time: {ist_time}"])
        text_body = "\n".join(lines)
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Amazon Pay GC discount: {len(matches)} item(s)"
        msg["From"] = sender
        msg["To"] = receiver
        msg.attach(MIMEText(text_body, "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.send_message(msg)
        print(f"[{get_ist_now()}] Email sent")
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] Email failed: {e}")
        return False


def check_amazon():
    print("=" * 60)
    print(f"Amazon Gift Card Monitor -- {get_ist_now()}")
    print(f"Min discount: {MIN_DISCOUNT_PCT}%")
    print("=" * 60)

    try:
        items = fetch_search_results()
    except Exception as e:
        print(f"[{get_ist_now()}] Failed to fetch: {e}")
        return False

    if not items:
        print(f"[{get_ist_now()}] No results (captcha hit or page changed)")
        return False

    matches = []
    for item in items:
        disc_str = f"{item['discount_pct']:.1f}% off" if item['discount_pct'] > 0 else "no discount"
        print(
            f"[{get_ist_now()}] {item['asin']}  Rs.{int(item['current_price']):6d}  "
            f"{disc_str:12s}  {item['title'][:60]}"
        )
        if item["discount_pct"] >= MIN_DISCOUNT_PCT:
            matches.append(item)

    if not matches:
        print(f"[{get_ist_now()}] No gift cards with >={MIN_DISCOUNT_PCT}% discount.")
        return False

    print(f"[{get_ist_now()}] MATCH: {len(matches)} discounted item(s)")

    if not should_send_alert("amazon"):
        return False

    ntfy_ok = send_ntfy_alert(matches)
    email_ok = send_email_alert(matches)
    if ntfy_ok or email_ok:
        record_alert("amazon")
    return ntfy_ok or email_ok


if __name__ == "__main__":
    try:
        check_amazon()
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
