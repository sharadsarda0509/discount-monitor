#!/usr/bin/env python3
"""
OnePlay Store -- PhonePe Gift Card Rs.7500 discount monitor.

Alerts when any discount is available on the PhonePe gift card.
"""

import os
import sys
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
COOLDOWN_HOURS = int(os.environ.get('ALERT_COOLDOWN_HOURS', 12))
STATE_DIR = Path('.alert_state')
STATE_FILE = STATE_DIR / 'last_alert.json'

NTFY_TOPIC = os.environ.get('NTFY_TOPIC', '')

PRODUCT_ID = "6cf5b942-e260-11f0-a1d3-0636a7656735"
PRODUCT_URL = f"https://store.oneplay.in/view/phonepe-gift-card-7500-{PRODUCT_ID}"
API_URL = f"https://commerce-services.oneplay.in/v1/content/details/info/{PRODUCT_ID}"
TARGET_DISCOUNT = 2.0

try:
    import requests
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


def fetch_product_data():
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'Origin': 'https://store.oneplay.in',
        'Referer': PRODUCT_URL,
    }
    try:
        print(f"[{get_ist_now()}] Fetching PhonePe gift card data...")
        response = requests.post(API_URL, headers=headers, json={}, timeout=30)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        print(f"[{get_ist_now()}] Error fetching data: {e}")
        return None


def extract_discount(product_data):
    if not product_data:
        return None, None
    try:
        info = (product_data.get('response') or {}).get('info') or {}
        product_name = info.get('title', 'Unknown')
        discount = info.get('discount_percentage', 0)
        current_price = info.get('best_buy_price', 0)
        original_price = info.get('best_display_price', 0)

        print(f"[{get_ist_now()}] Product: {product_name}")
        print(f"[{get_ist_now()}] Current Price: Rs.{current_price}")
        print(f"[{get_ist_now()}] Original Price: Rs.{original_price}")
        print(f"[{get_ist_now()}] Discount: {discount}%")

        if discount > 0 and current_price > 0:
            return float(discount), int(current_price)
        print(f"[{get_ist_now()}] No discount available")
        return None, None
    except Exception as e:
        print(f"[{get_ist_now()}] Error parsing data: {e}")
        return None, None


def send_ntfy_alert(discount, current_price=None):
    if not NTFY_TOPIC:
        print(f"[{get_ist_now()}] ntfy not configured (NTFY_TOPIC empty)")
        return False
    try:
        title = f"PhonePe GC Rs.7500: {discount}% OFF!"
        message = f"PhonePe Gift Card Rs.7500 is now {discount}% OFF"
        if current_price:
            message += f"\nPrice: Rs.{current_price}"
        message += f"\n\n{PRODUCT_URL}"
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode('utf-8'),
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": "phonepe,shopping",
                "Click": PRODUCT_URL,
            },
            timeout=10,
        ).raise_for_status()
        print(f"[{get_ist_now()}] ntfy notification sent")
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] ntfy failed: {e}")
        return False


def send_email_alert(discount, current_price=None):
    sender = os.environ.get('SENDER_EMAIL')
    receiver = os.environ.get('RECEIVER_EMAIL')
    password = os.environ.get('EMAIL_PASSWORD')
    if not all([sender, receiver, password]):
        print(f"[{get_ist_now()}] Email not configured")
        return False

    ist_time = get_ist_now().strftime('%Y-%m-%d %H:%M:%S IST')
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"PhonePe GC Rs.7500: {discount}% OFF!"
    msg["From"] = sender
    msg["To"] = receiver

    text_body = f"""PhonePe Gift Card Rs.7500 discount alert!

Discount: {discount}% OFF
{f'Price: Rs.{current_price}' if current_price else ''}

Link: {PRODUCT_URL}

Time: {ist_time}
"""
    html_body = f"""<html><body style="font-family:Arial,sans-serif;">
<h2>PhonePe Gift Card Rs.7500 - {discount}% OFF!</h2>
{'<p>Price: <strong>Rs.' + str(current_price) + '</strong></p>' if current_price else ''}
<p><a href="{PRODUCT_URL}">Buy Now on OnePlay Store</a></p>
<p style="font-size:12px;color:#666;">Alert at: {ist_time}</p>
</body></html>"""

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.send_message(msg)
        print(f"[{get_ist_now()}] Email sent to {receiver}")
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] Email failed: {e}")
        return False


def check_oneplay_phonepe():
    print("=" * 60)
    print(f"OnePlay PhonePe GC Rs.7500 Monitor - {get_ist_now()}")
    print("=" * 60)

    product_data = fetch_product_data()
    if not product_data:
        print(f"[{get_ist_now()}] Failed to fetch product data")
        return False

    discount, current_price = extract_discount(product_data)
    if discount is None:
        print(f"[{get_ist_now()}] No discount info")
        return False

    print(f"[{get_ist_now()}] Discount: {discount}% | Target: {TARGET_DISCOUNT}%")

    if discount >= TARGET_DISCOUNT:
        print(f"[{get_ist_now()}] Target discount reached!")
        if not should_send_alert('oneplay_phonepe'):
            return False
        ntfy_ok = send_ntfy_alert(discount, current_price)
        email_ok = send_email_alert(discount, current_price)
        if ntfy_ok or email_ok:
            record_alert('oneplay_phonepe')
        return ntfy_ok or email_ok
    else:
        print(f"[{get_ist_now()}] Target not reached. Waiting...")
        return False


if __name__ == "__main__":
    try:
        check_oneplay_phonepe()
    except Exception as e:
        print(f"[{get_ist_now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
