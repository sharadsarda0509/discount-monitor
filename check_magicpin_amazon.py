#!/usr/bin/env python3
"""
MagicPin -- Amazon Pay Gift Card Rs.5000 and Rs.10000 monitor.

Alerts when either denomination is in stock AND has ≥ MIN_DISCOUNT_PCT off.
Parses BeautifulSoup HTML directly — no JSON-LD (it lacks price data).
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
from typing import Any, Dict, List

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Error: pip install -r requirements.txt")
    sys.exit(1)

IST = timezone(timedelta(hours=5, minutes=30))
COOLDOWN_HOURS = float(os.environ.get("MAGICPIN_AMAZON_COOLDOWN_HOURS", os.environ.get("ALERT_COOLDOWN_HOURS", 1)))
STATE_DIR = Path(".alert_state")
STATE_FILE = STATE_DIR / "last_alert.json"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

PAGE_URL = "https://magicpin.in/Amazon-Pay-offers/292901/"
WATCH_AMOUNTS = [5000, 10000]
MIN_DISCOUNT_PCT = float(os.environ.get("MAGICPIN_AMAZON_MIN_DISCOUNT", 2.0))


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


def _parse_inr(text: str) -> float:
    """'₹4,900' or '₹4900' -> 4900.0"""
    cleaned = re.sub(r"[₹,\s]", "", text)
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_vouchers(html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    results = []
    for article in soup.find_all("article", class_="merchant-voucher-single"):
        name_el = article.find(class_="voucher-text")
        if not name_el:
            continue
        name = name_el.get_text(strip=True)

        orig_el = article.find(class_="voucher-original-price")
        sell_el = article.find(class_="voucher-sell-price")
        mrp = _parse_inr(orig_el.get_text()) if orig_el else 0.0
        price = _parse_inr(sell_el.get_text()) if sell_el else mrp

        sold_out = "voucher-sold-out-new" in article.get("class", [])
        sold_out = sold_out or bool(article.find(class_="voucher-sold-out-label"))

        discount_pct = ((mrp - price) / mrp * 100) if mrp > 0 else 0.0

        results.append({
            "name": name,
            "mrp": mrp,
            "price": price,
            "discount_pct": discount_pct,
            "in_stock": not sold_out,
        })
    return results


def send_ntfy_alert(matches: List[Dict[str, Any]]) -> bool:
    if not NTFY_TOPIC:
        print(f"[{get_ist_now()}] ntfy not configured")
        return False
    try:
        lines = ["MagicPin Amazon Pay voucher in stock with discount!\n"]
        for m in matches:
            lines.append(f"- {m['name']}: Rs.{int(m['price'])} ({m['discount_pct']:.1f}% off)")
        lines.append(f"\nBuy now: {PAGE_URL}")
        message = "\n".join(lines)
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": f"Amazon Pay GC in stock: {', '.join(m['name'] for m in matches)}",
                "Priority": "high",
                "Tags": "amazon,voucher,magicpin",
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
        return False
    try:
        ist_time = get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST")
        lines = ["MagicPin Amazon Pay voucher in stock!\n"]
        for m in matches:
            lines.append(f"- {m['name']}: Rs.{int(m['price'])} ({m['discount_pct']:.1f}% off) - {m['name']}")
        lines.extend(["", f"Buy now: {PAGE_URL}", "", f"Time: {ist_time}"])
        text_body = "\n".join(lines)
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Amazon Pay GC in stock: {', '.join(m['name'] for m in matches)}"
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


def check_magicpin_amazon():
    print("=" * 60)
    print(f"MagicPin Amazon Pay voucher check -- {get_ist_now()}")
    print(f"Watching: Rs. {WATCH_AMOUNTS}  min discount: {MIN_DISCOUNT_PCT}%")
    print("=" * 60)

    try:
        html = fetch_page_html()
    except requests.RequestException as e:
        print(f"[{get_ist_now()}] Request failed: {e}")
        sys.exit(1)

    vouchers = parse_vouchers(html)
    if not vouchers:
        print(f"[{get_ist_now()}] No vouchers parsed — page structure may have changed")
        return False

    matches = []
    for v in vouchers:
        amount_match = any(str(amt) in v["name"] for amt in WATCH_AMOUNTS)
        status = "IN STOCK" if v["in_stock"] else "sold out"
        print(
            f"[{get_ist_now()}] {v['name']:35s}  {status:9s}  "
            f"Rs.{int(v['mrp'])} -> Rs.{int(v['price'])}  ({v['discount_pct']:.1f}% off)"
        )
        if amount_match and v["in_stock"] and v["discount_pct"] >= MIN_DISCOUNT_PCT:
            matches.append(v)

    if not matches:
        print(f"[{get_ist_now()}] No watched vouchers in stock with >={MIN_DISCOUNT_PCT}% discount.")
        return False

    print(f"[{get_ist_now()}] MATCH: {[m['name'] for m in matches]}")

    if not should_send_alert("magicpin_amazon"):
        return False

    ntfy_ok = send_ntfy_alert(matches)
    email_ok = send_email_alert(matches)
    if ntfy_ok or email_ok:
        record_alert("magicpin_amazon")
    return ntfy_ok or email_ok


if __name__ == "__main__":
    try:
        check_magicpin_amazon()
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
