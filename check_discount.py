#!/usr/bin/env python3
"""
Flipkart Discount Monitor
Checks discount percentage and sends email alert when target is reached
"""

import os
import sys
import json
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from pathlib import Path
import re

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
PRODUCT_ID = "0242469b-dc1c-11f0-a1d3-0636a7656735"
PRODUCT_URL = f"https://store.oneplay.in/view/flipkart-e-gift-voucher-inr-10000-{PRODUCT_ID}"
API_URL = f"https://commerce-services.oneplay.in/v1/content/details/info/{PRODUCT_ID}"
TARGET_DISCOUNT = 2.0

# ntfy.sh configuration
NTFY_TOPIC = os.environ.get('NTFY_TOPIC', '')


def fetch_product_data():
    """Fetch product data from OnePlay API"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'Origin': 'https://store.oneplay.in',
        'Referer': PRODUCT_URL,
    }
    
    try:
        print(f"[{datetime.now()}] Fetching product data from API...")
        response = requests.post(API_URL, headers=headers, json={}, timeout=30)
        response.raise_for_status()
        print(f"[{datetime.now()}] Successfully fetched data (Status: {response.status_code})")
        return response.json()
    except requests.RequestException as e:
        print(f"[{datetime.now()}] Error fetching data: {e}")
        return None


def extract_discount(product_data):
    """Extract discount percentage and price from API response"""
    if not product_data:
        return None, None
    
    try:
        # Navigate through the JSON structure
        response = product_data.get('response', {})
        info = response.get('info', {})
        
        # Extract data
        product_name = info.get('title', 'Unknown')
        discount = info.get('discount_percentage', 0)
        current_price = info.get('best_buy_price', 0)
        original_price = info.get('best_display_price', 0)
        
        print(f"[{datetime.now()}] Product: {product_name}")
        print(f"[{datetime.now()}] Current Price: ₹{current_price}")
        print(f"[{datetime.now()}] Original Price: ₹{original_price}")
        print(f"[{datetime.now()}] Discount: {discount}%")
        
        if discount > 0 and current_price > 0:
            return float(discount), int(current_price)
        else:
            print(f"[{datetime.now()}] No discount available")
            return None, None
            
    except Exception as e:
        print(f"[{datetime.now()}] Error parsing product data: {e}")
        print(f"[{datetime.now()}] Raw data sample: {str(product_data)[:500]}")
        return None, None


def send_ntfy_alert(discount, current_price=None):
    """Send push notification via ntfy.sh"""
    if not NTFY_TOPIC:
        print(f"[{get_ist_now()}] ntfy.sh not configured (NTFY_TOPIC not set)")
        return False
    
    try:
        title = f"Flipkart: {discount}% OFF!"
        message = f"Flipkart E-Gift Voucher Rs.10000 is now {discount}% OFF"
        if current_price:
            message += f"\nPrice: Rs.{current_price}"
        message += f"\n\n{PRODUCT_URL}"
        
        response = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode('utf-8'),
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": "tada,shopping",
                "Click": PRODUCT_URL,
            },
            timeout=10
        )
        response.raise_for_status()
        print(f"[{get_ist_now()}] ✅ ntfy.sh notification sent!")
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] ❌ Failed to send ntfy notification: {e}")
        return False


def send_email_alert(discount, current_price=None):
    """Send email alert when target discount is reached"""
    sender_email = os.environ.get('SENDER_EMAIL')
    receiver_email = os.environ.get('RECEIVER_EMAIL')
    email_password = os.environ.get('EMAIL_PASSWORD')
    
    if not all([sender_email, receiver_email, email_password]):
        print(f"[{datetime.now()}] Error: Email credentials not configured in environment variables")
        return False
    
    message = MIMEMultipart("alternative")
    message["From"] = sender_email
    message["To"] = receiver_email
    message["Subject"] = f"🎉 Flipkart Voucher Alert: {discount}% OFF Available!"
    
    # Get current IST time for email
    ist_time = get_ist_now().strftime('%Y-%m-%d %H:%M:%S IST')
    
    # Create plain text and HTML versions
    text_body = f"""
Discount Alert!

The Flipkart E-Gift Voucher INR 10000 has reached {discount}% OFF!

{f'Current Price: ₹{current_price}' if current_price else 'Check the link for current price'}

🔗 Link: {PRODUCT_URL}

⏰ Time: {ist_time}

---
This is an automated alert from your Flipkart Discount Monitor.
"""
    
    html_body = f"""
<html>
  <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px; background-color: #f9f9f9; border-radius: 10px;">
      <h2 style="color: #2874f0;">🎉 Discount Alert!</h2>
      
      <div style="background-color: white; padding: 20px; border-radius: 8px; margin: 20px 0;">
        <p style="font-size: 16px;">
          The <strong>Flipkart E-Gift Voucher INR 10000</strong> has reached 
          <span style="color: #ff6b00; font-size: 24px; font-weight: bold;">{discount}% OFF</span>!
        </p>
        
        {f'<p style="font-size: 18px;"><strong>Current Price:</strong> <span style="color: #388e3c;">₹{current_price}</span></p>' if current_price else ''}
        
        <p style="margin: 20px 0;">
          <a href="{PRODUCT_URL}" 
             style="display: inline-block; padding: 12px 24px; background-color: #2874f0; 
                    color: white; text-decoration: none; border-radius: 5px; font-weight: bold;">
            Buy Now on OnePlay Store
          </a>
        </p>
      </div>
      
      <p style="font-size: 12px; color: #666; margin-top: 20px;">
        ⏰ Alert triggered at: {ist_time}<br>
        <em>This is an automated alert from your Flipkart Discount Monitor.</em>
      </p>
    </div>
  </body>
</html>
"""
    
    # Attach both plain text and HTML versions
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


def check_discount():
    """Main function to check discount and send alert if needed"""
    print("=" * 60)
    print(f"Flipkart Discount Monitor - Run at {datetime.now()}")
    print("=" * 60)
    
    product_data = fetch_product_data()
    if not product_data:
        print(f"[{datetime.now()}] Failed to fetch product data")
        return False
    
    discount, current_price = extract_discount(product_data)
    
    if discount is None:
        print(f"[{datetime.now()}] Could not extract discount information")
        return False
    
    print(f"[{datetime.now()}] Current discount: {discount}% | Target: {TARGET_DISCOUNT}%")
    
    if discount >= TARGET_DISCOUNT:
        print(f"[{get_ist_now()}] 🎯 Target discount reached!")
        
        # Check cooldown before sending
        if not should_send_alert('flipkart'):
            return False
        
        print(f"[{get_ist_now()}] Sending alerts...")
        
        # Send both ntfy and email notifications
        ntfy_success = send_ntfy_alert(discount, current_price)
        email_success = send_email_alert(discount, current_price)
        
        # Record alert if at least one notification succeeded
        if ntfy_success or email_success:
            record_alert('flipkart')
        
        return ntfy_success or email_success
    else:
        print(f"[{get_ist_now()}] Target not reached yet. Waiting...")
        return False


if __name__ == "__main__":
    try:
        check_discount()
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

