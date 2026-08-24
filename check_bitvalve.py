#!/usr/bin/env python3
"""
BitValve P2P Offer Monitor
Checks for Bitcoin sell offers with cash deposit to bank from Indian (INR) traders
Sends email alert when margin is >= 5%

Replaces the retired NoOnes monitor. Parameters mirror the sell page URL:
https://www.bitvalve.com/sell?cryptocurrency=bitcoin&currency=INR&country=IN&sort=cheapest&paymentMethod=Cash-deposit
"""

import os
import sys
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from pathlib import Path

# IST timezone (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))

# Alert cooldown configuration
COOLDOWN_HOURS = int(os.environ.get('ALERT_COOLDOWN_HOURS', 24))
STATE_DIR = Path('.alert_state')
STATE_FILE = STATE_DIR / 'last_alert.json'


def get_ist_now():
    """Get current time in IST"""
    return datetime.now(IST)


def should_send_alert(alert_type: str) -> bool:
    """Check if enough time has passed since last alert of this type"""
    STATE_DIR.mkdir(exist_ok=True)

    if not STATE_FILE.exists():
        return True

    try:
        state = json.loads(STATE_FILE.read_text())
        last_alert = state.get(alert_type)
        if not last_alert:
            return True

        # Parse the ISO format timestamp
        last_time = datetime.fromisoformat(last_alert)
        # Make sure we're comparing timezone-aware datetimes
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=IST)

        elapsed = get_ist_now() - last_time
        elapsed_hours = elapsed.total_seconds() / 3600

        if elapsed_hours < COOLDOWN_HOURS:
            print(f"[{get_ist_now()}] ⏳ Cooldown active for {alert_type}: {elapsed_hours:.1f}h elapsed, need {COOLDOWN_HOURS}h")
            return False
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] Warning: Could not read state file: {e}")
        return True


def record_alert(alert_type: str):
    """Record that an alert was sent"""
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
    print("Error: Required packages not installed. Run: pip install -r requirements.txt")
    sys.exit(1)


# Configuration
BITVALVE_URL = "https://www.bitvalve.com/sell?cryptocurrency=bitcoin&currency=INR&country=IN&sort=cheapest&paymentMethod=Cash-deposit"
API_URL = "https://api.bitvalve.com/listings/listings"
TARGET_MARGIN = 5.0

# Blocked traders (unresponsive or problematic)
BLOCKED_TRADERS = [
]

# ntfy.sh configuration
NTFY_TOPIC = os.environ.get('NTFY_TOPIC', '')


def fetch_offers():
    """Fetch P2P offers from BitValve API (mirrors the sell page's listings call)"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': '*/*',
        'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
        'Origin': 'https://www.bitvalve.com',
        'Referer': 'https://www.bitvalve.com/',
    }

    # Form-encoded body; keys map directly to the sell page URL parameters
    payload = {
        'ctoken': '',
        'type': 'sell',
        'curr_one': 'bitcoin',            # cryptocurrency=bitcoin
        'payment_method': 'Cash-deposit',  # paymentMethod=Cash-deposit
        'payment_method_curr': 'INR',      # currency=INR
        'country': 'IN',                   # country=IN
        'amount': '',
        'page': '1',
        'sort': 'cheapest',                # sort=cheapest
        'only_online': '0',
        'only_with_feedback': '0',
        'only_trusted': '0',
        'hide_inactive': '1',
    }

    try:
        print(f"[{datetime.now()}] Fetching BitValve offers from API...")
        response = requests.post(API_URL, headers=headers, data=payload, timeout=30)
        response.raise_for_status()
        print(f"[{datetime.now()}] Successfully fetched data (Status: {response.status_code})")
        return response.json()
    except requests.RequestException as e:
        print(f"[{datetime.now()}] Error fetching data: {e}")
        return None


def filter_offers(offers_data):
    """Filter offers with margin >= TARGET_MARGIN.

    Country/currency are already scoped server-side via the request params
    (country=IN, payment_method_curr=INR), so we don't re-filter on the
    response's `country` field (it comes back as "-").
    """
    if not offers_data:
        return []

    try:
        # Offers live in the 'listings' array
        offers = offers_data.get('listings', [])

        print(f"[{datetime.now()}] Total offers found: {len(offers)}")

        matching_offers = []

        for offer in offers:
            # Extract offer details
            trader_name = offer.get('trader_name', 'Unknown')
            margin = offer.get('margin', 0)
            price = offer.get('rate', 0)
            min_amount = offer.get('min', 0)
            max_amount = offer.get('max', 0)
            rel_url = offer.get('url', '')

            # Convert numeric fields (API returns them as strings)
            try:
                margin_float = float(margin)
            except (ValueError, TypeError):
                margin_float = 0
            try:
                price_float = float(price)
            except (ValueError, TypeError):
                price_float = 0
            try:
                min_float = float(min_amount)
            except (ValueError, TypeError):
                min_float = 0
            try:
                max_float = float(max_amount)
            except (ValueError, TypeError):
                max_float = 0

            # Check margin >= target and trader not blocked
            if margin_float >= TARGET_MARGIN and trader_name not in BLOCKED_TRADERS:
                matching_offers.append({
                    'trader': trader_name,
                    'margin': margin_float,
                    'price': price_float,
                    'min_amount': min_float,
                    'max_amount': max_float,
                    'offer_url': f"https://www.bitvalve.com{rel_url}"
                })
                print(f"[{datetime.now()}] ✅ Match: {trader_name} - Margin: {margin_float}%")

        print(f"[{datetime.now()}] Matching offers (margin >= {TARGET_MARGIN}%): {len(matching_offers)}")
        return matching_offers

    except Exception as e:
        print(f"[{datetime.now()}] Error parsing offers: {e}")
        return []


def send_ntfy_alert(matching_offers):
    """Send push notification via ntfy.sh"""
    if not NTFY_TOPIC:
        print(f"[{get_ist_now()}] ntfy.sh not configured (NTFY_TOPIC not set)")
        return False

    try:
        best_offer = max(matching_offers, key=lambda x: x['margin'])

        title = f"BitValve: {best_offer['margin']}% Margin!"
        message = f"Found {len(matching_offers)} offer(s) with >={TARGET_MARGIN}% margin\n\n"

        for offer in sorted(matching_offers, key=lambda x: x['margin'], reverse=True)[:3]:
            message += f"- {offer['trader']}: {offer['margin']}%\n"

        message += f"\n{BITVALVE_URL}"

        response = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode('utf-8'),
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": "rocket,moneybag",
                "Click": BITVALVE_URL,
            },
            timeout=10
        )
        response.raise_for_status()
        print(f"[{get_ist_now()}] ✅ ntfy.sh notification sent!")
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] ❌ Failed to send ntfy notification: {e}")
        return False


def send_email_alert(matching_offers):
    """Send email alert when matching offers are found"""
    sender_email = os.environ.get('SENDER_EMAIL')
    receiver_email = os.environ.get('RECEIVER_EMAIL')
    email_password = os.environ.get('EMAIL_PASSWORD')

    if not all([sender_email, receiver_email, email_password]):
        print(f"[{datetime.now()}] Error: Email credentials not configured in environment variables")
        return False

    message = MIMEMultipart("alternative")
    message["From"] = sender_email
    message["To"] = receiver_email

    # Get best offer (highest margin)
    best_offer = max(matching_offers, key=lambda x: x['margin'])

    message["Subject"] = f"🚀 BitValve Alert: {best_offer['margin']}% Margin Offer!"

    # Create offer list text
    offers_text = ""
    offers_html = ""

    for offer in sorted(matching_offers, key=lambda x: x['margin'], reverse=True):
        offers_text += f"""
• Trader: {offer['trader']}
  Margin: {offer['margin']}%
  Price: ₹{offer['price']:,.2f}/BTC
  Amount Range: ₹{offer['min_amount']:,.0f} - ₹{offer['max_amount']:,.0f}
  Link: {offer['offer_url']}
"""
        offers_html += f"""
<div style="background-color: #f5f5f5; padding: 15px; border-radius: 8px; margin: 10px 0;">
  <p style="margin: 0 0 5px 0;"><strong>Trader:</strong> {offer['trader']}</p>
  <p style="margin: 0 0 5px 0;"><strong>Margin:</strong> <span style="color: #4caf50; font-weight: bold;">{offer['margin']}%</span></p>
  <p style="margin: 0 0 5px 0;"><strong>Price:</strong> ₹{offer['price']:,.2f}/BTC</p>
  <p style="margin: 0 0 5px 0;"><strong>Amount Range:</strong> ₹{offer['min_amount']:,.0f} - ₹{offer['max_amount']:,.0f}</p>
  <p style="margin: 0;"><a href="{offer['offer_url']}" style="color: #2196f3;">View Offer →</a></p>
</div>
"""

    # Get current IST time for email
    ist_time = get_ist_now().strftime('%Y-%m-%d %H:%M:%S IST')

    text_body = f"""
BitValve P2P Alert - High Margin Offers Available!

Found {len(matching_offers)} offer(s) with margin >= {TARGET_MARGIN}%:
{offers_text}

🔗 View all offers: {BITVALVE_URL}

⏰ Time: {ist_time}

---
This is an automated alert from your BitValve P2P Monitor.
"""

    html_body = f"""
<html>
  <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px; background-color: #f9f9f9; border-radius: 10px;">
      <h2 style="color: #ff9800;">🚀 BitValve P2P Alert - High Margin Offers!</h2>

      <p style="font-size: 16px;">
        Found <strong>{len(matching_offers)}</strong> offer(s) with margin ≥
        <span style="color: #4caf50; font-weight: bold;">{TARGET_MARGIN}%</span>
      </p>

      <div style="margin: 20px 0;">
        {offers_html}
      </div>

      <p style="margin: 20px 0;">
        <a href="{BITVALVE_URL}"
           style="display: inline-block; padding: 12px 24px; background-color: #ff9800;
                  color: white; text-decoration: none; border-radius: 5px; font-weight: bold;">
          View All Offers on BitValve
        </a>
      </p>

      <p style="font-size: 12px; color: #666; margin-top: 20px;">
        ⏰ Alert triggered at: {ist_time}<br>
        <em>This is an automated alert from your BitValve P2P Monitor.</em>
      </p>
    </div>
  </body>
</html>
"""

    part1 = MIMEText(text_body, "plain")
    part2 = MIMEText(html_body, "html")
    message.attach(part1)
    message.attach(part2)

    try:
        print(f"[{datetime.now()}] Sending email alert to {receiver_email}...")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, email_password)
            server.send_message(message)
        print(f"[{datetime.now()}] ✅ Email alert sent successfully!")
        return True
    except Exception as e:
        print(f"[{datetime.now()}] ❌ Failed to send email: {e}")
        return False


def check_bitvalve():
    """Main function to check BitValve offers and send alert if needed"""
    print("=" * 60)
    print(f"BitValve P2P Monitor - Run at {datetime.now()}")
    print("=" * 60)

    offers_data = fetch_offers()
    if not offers_data:
        print(f"[{datetime.now()}] Failed to fetch offers data")
        return False

    matching_offers = filter_offers(offers_data)

    if not matching_offers:
        print(f"[{get_ist_now()}] No offers with margin >= {TARGET_MARGIN}%. Waiting...")
        return False

    print(f"[{get_ist_now()}] 🎯 Found {len(matching_offers)} matching offer(s)!")

    # Check cooldown before sending
    if not should_send_alert('bitvalve'):
        return False

    print(f"[{get_ist_now()}] Sending alerts...")

    # Send both ntfy and email notifications
    ntfy_success = send_ntfy_alert(matching_offers)
    email_success = send_email_alert(matching_offers)

    # Record alert if at least one notification succeeded
    if ntfy_success or email_success:
        record_alert('bitvalve')

    return ntfy_success or email_success


if __name__ == "__main__":
    try:
        check_bitvalve()
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
