#!/usr/bin/env python3
"""
JioMart iPhone 16 Pro Max 256GB Stock Monitor
Checks all 4 color variants for delivery availability across multiple Bangalore pincodes.
Uses JioMart's promise API — no browser required.
"""

import os
import sys
import json
import uuid
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from pathlib import Path

IST = timezone(timedelta(hours=5, minutes=30))
COOLDOWN_HOURS = int(os.environ.get('ALERT_COOLDOWN_HOURS', 4))
STATE_DIR = Path('.alert_state')
STATE_FILE = STATE_DIR / 'last_alert.json'

NTFY_TOPIC = os.environ.get('NTFY_TOPIC', '')
_raw_pincodes = os.environ.get('JIOMART_PINCODES', '560035,560048,560103')
TARGET_PINCODES = [p.strip() for p in _raw_pincodes.split(',') if p.strip()]

# All iPhone 16 Pro Max 256GB color variants on JioMart
PRODUCTS = [
    {'color': 'Black Titanium',   'product_id': 609946183, 'article_id': '494423059'},
    {'color': 'White Titanium',   'product_id': 609946184, 'article_id': '494423060'},
    {'color': 'Desert Titanium',  'product_id': 609946185, 'article_id': '494423061'},
    {'color': 'Natural Titanium', 'product_id': 609946197, 'article_id': '494423062'},
]

# Coordinates per pincode — must match the area for JioMart's geolocation validation
PINCODE_COORDS = {
    '560035': {'lat': 12.9048022, 'long': 77.6821069},  # Sarjapur Road area
    '560048': {'lat': 12.9197,    'long': 77.5087},     # Rajarajeshwari Nagar
    '560103': {'lat': 12.9090,    'long': 77.7113},     # Sarjapur
}

PACKAGE_DIM = {
    'length': 18.2, 'length_uom': 'cm',
    'width': 9.6,   'width_uom': 'cm',
    'height': 2.9,  'height_uom': 'cm',
    'weight': '414', 'weight_uom': 'GRM',
}


def get_ist_now():
    return datetime.now(IST)


def should_send_alert(alert_type: str) -> bool:
    STATE_DIR.mkdir(exist_ok=True)
    if not STATE_FILE.exists():
        return True
    try:
        state = json.loads(STATE_FILE.read_text())
        last_alert = state.get(alert_type)
        if not last_alert:
            return True
        last_time = datetime.fromisoformat(last_alert)
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=IST)
        elapsed_hours = (get_ist_now() - last_time).total_seconds() / 3600
        if elapsed_hours < COOLDOWN_HOURS:
            print(f"[{get_ist_now()}] Cooldown active for {alert_type}: {elapsed_hours:.1f}h / {COOLDOWN_HOURS}h")
            return False
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] Warning: Could not read state file: {e}")
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


try:
    import requests
except ImportError:
    print("Error: requests not installed. Run: pip install -r requirements.txt")
    sys.exit(1)


def make_session(pincode: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        'User-Agent': (
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/146.0.0.0 Safari/537.36'
        ),
        'Accept': 'application/json, text/javascript, */*; q=0.01',
        'Accept-Language': 'en-GB,en-US;q=0.9,en;q=0.8',
        'x-requested-with': 'XMLHttpRequest',
        'Origin': 'https://www.jiomart.com',
        'Referer': 'https://www.jiomart.com/',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
    })
    s.cookies.set('AKA_A2', 'A', domain='.jiomart.com')
    s.cookies.set('nms_mgo_pincode', pincode, domain='.jiomart.com')
    return s


def check_product_available(session: requests.Session, product_id: int, pincode: str) -> bool:
    """Quick check: is the product generally in stock (not pincode-specific)?"""
    try:
        r = session.get(
            f'https://www.jiomart.com/catalog/productdetails/get/{product_id}',
            headers={'pin': pincode},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"    productdetails HTTP {r.status_code}")
            return False
        data = r.json()
        if data.get('status') != 'success':
            print(f"    productdetails status={data.get('status')}")
            return False
        avail = data['data'].get('availability_status')
        price = data['data'].get('selling_price')
        print(f"    availability_status={avail}, price=₹{price}")
        return avail == 'A'
    except Exception as e:
        print(f"    productdetails error: {e}")
        return False


def check_delivery_promise(session: requests.Session, article_id: str, pincode: str):
    """
    Check if the product can be delivered to the given pincode.
    Returns seller info dict on success, None if unavailable.
    """
    coords = PINCODE_COORDS.get(pincode, {'lat': 0.0, 'long': 0.0})
    payload = {
        'identifier': str(uuid.uuid4()),
        'to_pincode': pincode,
        'customer_details': {
            'phone_number': '0',
            'pincode': pincode,
            'coordinates': coords,
        },
        'articles': [{
            'article_id': article_id,
            'vertical': 'ELECTRONICS',
            'lookup_inventory': True,
            'tenant_ids': ['1004'],
            'merchant_id': None,
            'channel_id': None,
            'available_at_3p_seller': False,
            'available_at_1p_kirana': False,
            'available_at_rrl_fc': False,
            'available_at_rrl_store': True,
            'available_at_3p_kirana': False,
            'fulfillment_channel': '',
            'delivery_type': 'grab_and_go',
            'locked_phone': False,
            'transport_mode': 'surface',
            'package_dimension': PACKAGE_DIM,
            'is_liquid': False,
            'is_hazmat': False,
            'is_fragile': False,
            'store_selection_filter': {'store_type': 'DC'},
            'pre_order': False,
            'procurement_date': None,
            'is_tradein': True,
            'exchange_details': None,
        }],
    }
    try:
        r = session.post(
            'https://www.jiomart.com/platform/logistics/api/v1/promise',
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=15,
        )
        if r.status_code != 200:
            print(f"    promise API HTTP {r.status_code}")
            return None
        data = r.json()
        if not data.get('success'):
            err = data.get('error', {})
            print(f"    promise success=False, error={err}")
            return None
        articles = data.get('articles', [])
        if not articles:
            return None
        art = articles[0]
        sellers = art.get('seller_data', [])
        promise = art.get('promise', {}).get('formatted', {})
        return {
            'sellers': sellers,
            'delivery': promise.get('min', ''),
            'display_message': art.get('display_message', ''),
        }
    except Exception as e:
        print(f"    promise API error: {e}")
        return None


def send_ntfy_alert(pincode, in_stock_items):
    if not NTFY_TOPIC:
        print(f"[{get_ist_now()}] ntfy.sh not configured")
        return False
    first = in_stock_items[0]
    title = f"JioMart iPhone 16 Pro Max 256GB IN STOCK — {pincode}"
    lines = [f"iPhone 16 Pro Max 256GB available for delivery to {pincode}!\n"]
    for item in in_stock_items:
        slug = item['color'].lower().replace(' ', '-')
        url = f"https://www.jiomart.com/p/electronics/apple-iphone-16-pro-max-256-gb-{slug}/{item['product_id']}"
        lines.append(f"• {item['color']}: {item['delivery']}")
        lines.append(f"  {url}")
    message = '\n'.join(lines)
    first_slug = first['color'].lower().replace(' ', '-')
    click_url = f"https://www.jiomart.com/p/electronics/apple-iphone-16-pro-max-256-gb-{first_slug}/{first['product_id']}"
    try:
        r = requests.post(
            f'https://ntfy.sh/{NTFY_TOPIC}',
            data=message.encode('utf-8'),
            headers={
                'Title': title,
                'Priority': 'urgent',
                'Tags': 'iphone,shopping,rotating_light',
                'Click': click_url,
            },
            timeout=10,
        )
        r.raise_for_status()
        print(f"[{get_ist_now()}] ntfy.sh notification sent for {pincode}!")
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] Failed to send ntfy notification: {e}")
        return False


def send_email_alert(pincode, in_stock_items):
    sender = os.environ.get('SENDER_EMAIL')
    receiver = os.environ.get('RECEIVER_EMAIL')
    password = os.environ.get('EMAIL_PASSWORD')
    if not all([sender, receiver, password]):
        print(f"[{get_ist_now()}] Email credentials not configured")
        return False

    subject = f"iPhone 16 Pro Max 256GB IN STOCK on JioMart — {pincode}!"

    rows_html = ''
    rows_text = ''
    for item in in_stock_items:
        slug = item['color'].lower().replace(' ', '-')
        url = f"https://www.jiomart.com/p/electronics/apple-iphone-16-pro-max-256-gb-{slug}/{item['product_id']}"
        rows_html += f"""
        <tr>
          <td style="padding:8px;border:1px solid #ddd;"><strong>{item['color']}</strong></td>
          <td style="padding:8px;border:1px solid #ddd;">{item['delivery']}</td>
          <td style="padding:8px;border:1px solid #ddd;"><a href="{url}">Buy Now</a></td>
        </tr>"""
        rows_text += f"  • {item['color']}: {item['delivery']}\n    {url}\n"

    ist_time = get_ist_now().strftime('%Y-%m-%d %H:%M:%S IST')
    html_body = f"""
<html><body style="font-family:Arial,sans-serif;line-height:1.6;color:#333;">
  <div style="max-width:600px;margin:0 auto;padding:20px;background:#f9f9f9;border-radius:10px;">
    <h2 style="color:#d62d20;">iPhone 16 Pro Max 256GB — IN STOCK!</h2>
    <p>Available for delivery to pincode <strong>{pincode}</strong>:</p>
    <table style="border-collapse:collapse;width:100%;margin:16px 0;">
      <thead><tr style="background:#eee;">
        <th style="padding:8px;border:1px solid #ddd;text-align:left;">Color</th>
        <th style="padding:8px;border:1px solid #ddd;text-align:left;">Delivery</th>
        <th style="padding:8px;border:1px solid #ddd;text-align:left;">Link</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
    <p style="font-size:12px;color:#666;">Alert at: {ist_time}</p>
  </div>
</body></html>"""

    text_body = f"iPhone 16 Pro Max 256GB IN STOCK at pincode {pincode}!\n\n{rows_text}\nAlert at: {ist_time}\n"

    msg = MIMEMultipart('alternative')
    msg['From'] = sender
    msg['To'] = receiver
    msg['Subject'] = subject
    msg.attach(MIMEText(text_body, 'plain'))
    msg.attach(MIMEText(html_body, 'html'))

    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as srv:
            srv.login(sender, password)
            srv.send_message(msg)
        print(f"[{get_ist_now()}] Email alert sent for {pincode}!")
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] Failed to send email: {e}")
        return False


def check_jiomart():
    print('=' * 60)
    print(f"JioMart iPhone 16 Pro Max Monitor — {get_ist_now()}")
    print(f"Target pincodes: {', '.join(TARGET_PINCODES)}")
    print('=' * 60)

    any_alerted = False

    for pincode in TARGET_PINCODES:
        print(f"\n{'─' * 40}")
        print(f"Pincode: {pincode}")
        print('─' * 40)

        session = make_session(pincode)
        in_stock = []

        for product in PRODUCTS:
            color = product['color']
            pid = product['product_id']
            aid = product['article_id']
            print(f"\n  [{color}]")

            if not check_product_available(session, pid, pincode):
                print(f"    -> Not generally available, skipping")
                continue

            result = check_delivery_promise(session, aid, pincode)
            if result:
                sellers_info = [f"{s.get('storeid')}/{s.get('store_type')}" for s in result['sellers']]
                print(f"    -> IN STOCK! delivery={result['delivery']}, stores={sellers_info}")
                in_stock.append({
                    'color': color,
                    'product_id': pid,
                    'delivery': result['delivery'] or result['display_message'],
                    'sellers': sellers_info,
                })
            else:
                print(f"    -> Not deliverable to {pincode}")

        print(f"\n  Summary: {len(in_stock)}/{len(PRODUCTS)} colors in stock for {pincode}")

        if not in_stock:
            print(f"  No stock for {pincode}. No alert.")
            continue

        alert_key = f'jiomart_iphone16promax_{pincode}'
        if not should_send_alert(alert_key):
            continue

        ntfy_ok = send_ntfy_alert(pincode, in_stock)
        email_ok = send_email_alert(pincode, in_stock)

        if ntfy_ok or email_ok:
            record_alert(alert_key)
            any_alerted = True

    return any_alerted


if __name__ == '__main__':
    try:
        check_jiomart()
    except Exception as e:
        print(f"[{get_ist_now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
