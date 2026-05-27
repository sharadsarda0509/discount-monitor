#!/usr/bin/env python3
"""
Amazon Gift Card Monitor
Checks price for Amazon Pay Gift Card and sends alert when target is reached
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
COOLDOWN_HOURS = int(os.environ.get('ALERT_COOLDOWN_HOURS', 12))
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
    from curl_cffi import requests as cffi_requests
    from bs4 import BeautifulSoup
    _SESSION = cffi_requests.Session(impersonate="chrome120")
except ImportError:
    try:
        import requests as _fallback_requests
        from bs4 import BeautifulSoup
        _SESSION = None
    except ImportError:
        print("Error: Required packages not installed. Run: pip install -r requirements.txt")
        sys.exit(1)


def _get(url, **kwargs):
    if _SESSION is not None:
        return _SESSION.get(url, **kwargs)
    return _fallback_requests.get(url, **kwargs)


# Configuration
PRODUCT_URL = "https://www.amazon.in/dp/B00PQ70336"
TARGET_PRICE = 9800
TARGET_DISCOUNT = 2.0
FACE_VALUE = 10000

# ntfy.sh configuration
NTFY_TOPIC = os.environ.get('NTFY_TOPIC', '')


def fetch_product_price():
    """Fetch product price from Amazon page"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1'
    }
    
    try:
        print(f"[{get_ist_now()}] Fetching Amazon product page...")
        response = _get(PRODUCT_URL, headers=headers, timeout=30)
        response.raise_for_status()
        print(f"[{get_ist_now()}] Successfully fetched page (Status: {response.status_code})")
        
        soup = BeautifulSoup(response.text, 'html.parser')

        # Detect CAPTCHA / bot-check page
        if soup.find('form', {'action': re.compile(r'validateCaptcha', re.I)}) or \
                'Enter the characters you see below' in response.text:
            print(f"[{get_ist_now()}] Amazon served a CAPTCHA page -- skipping this run")
            return None

        price = None

        # Method 1: Try JSON-LD structured data (most reliable)
        json_ld_scripts = soup.find_all('script', {'type': 'application/ld+json'})
        for script in json_ld_scripts:
            try:
                data = json.loads(script.string)
                if isinstance(data, dict) and 'offers' in data:
                    price_text = data['offers'].get('price', '')
                    if price_text:
                        price = int(float(price_text))
                        if price > 0:
                            print(f"[{get_ist_now()}] Found price in JSON-LD: Rs.{price}")
                            break
            except (json.JSONDecodeError, ValueError, KeyError):
                continue

        # Method 2: Parse TWISTER_PLUS_INLINE_STATE JSON (Amazon's dynamic data)
        if not price:
            twister_script = soup.find('script', string=re.compile(r'TWISTER_PLUS_INLINE_STATE'))
            if twister_script:
                match = re.search(r'displayPrice["\']:\s*["\'][\s₹]*([0-9,]+)', twister_script.string)
                if match:
                    price_text = match.group(1).replace(',', '')
                    try:
                        price = int(float(price_text))
                        print(f"[{get_ist_now()}] Found price in TWISTER data: Rs.{price}")
                    except ValueError:
                        pass

        # Method 3: corePriceDisplay / apexPriceToPay (current Amazon structure)
        if not price:
            for sel in [
                {'id': 'corePriceDisplay_desktop_feature_div'},
                {'id': 'apex_desktop'},
            ]:
                container = soup.find('div', sel)
                if container:
                    whole = container.find('span', {'class': 'a-price-whole'})
                    if whole:
                        price_text = whole.text.strip().replace(',', '').rstrip('.')
                        try:
                            price = int(price_text)
                            print(f"[{get_ist_now()}] Found price in {sel}: Rs.{price}")
                            break
                        except ValueError:
                            pass

        # Method 4: any a-price-whole on page
        if not price:
            for el in soup.find_all('span', {'class': 'a-price-whole'}):
                price_text = el.text.strip().replace(',', '').rstrip('.')
                try:
                    candidate = int(price_text)
                    if 100 < candidate < 100000:
                        price = candidate
                        print(f"[{get_ist_now()}] Found price via a-price-whole: Rs.{price}")
                        break
                except ValueError:
                    continue

        # Method 5: regex over raw HTML as last resort
        if not price:
            match = re.search(r'"priceAmount"\s*:\s*([\d.]+)', response.text)
            if match:
                try:
                    price = int(float(match.group(1)))
                    print(f"[{get_ist_now()}] Found price via priceAmount JSON: Rs.{price}")
                except ValueError:
                    pass

        return price
        
    except requests.RequestException as e:
        print(f"[{get_ist_now()}] Error fetching page: {e}")
        return None
    except Exception as e:
        print(f"[{get_ist_now()}] Error parsing page: {e}")
        return None


def send_ntfy_alert(current_price):
    """Send push notification via ntfy.sh"""
    if not NTFY_TOPIC:
        print(f"[{get_ist_now()}] ntfy.sh not configured (NTFY_TOPIC not set)")
        return False
    
    try:
        discount = ((FACE_VALUE - current_price) / FACE_VALUE) * 100
        title = f"Amazon GV: Rs.{current_price} ({discount:.1f}% OFF)"
        message = f"Amazon Pay Gift Card Rs.10000 is now at Rs.{current_price}\n"
        message += f"Discount: {discount:.1f}%\n\n{PRODUCT_URL}"
        
        response = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode('utf-8'),
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": "gift,shopping",
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


def send_email_alert(current_price):
    """Send email alert when target price is reached"""
    sender_email = os.environ.get('SENDER_EMAIL')
    receiver_email = os.environ.get('RECEIVER_EMAIL')
    email_password = os.environ.get('EMAIL_PASSWORD')
    
    if not all([sender_email, receiver_email, email_password]):
        print(f"[{get_ist_now()}] Email credentials not configured")
        return False
    
    discount = ((FACE_VALUE - current_price) / FACE_VALUE) * 100
    
    message = MIMEMultipart("alternative")
    message["From"] = sender_email
    message["To"] = receiver_email
    message["Subject"] = f"🎁 Amazon Gift Card Alert: Rs.{current_price} ({discount:.1f}% OFF)!"
    
    ist_time = get_ist_now().strftime('%Y-%m-%d %H:%M:%S IST')
    
    text_body = f"""
Amazon Gift Card Price Alert!

Amazon Pay Black Gift Card Box Rs.10000 is now at Rs.{current_price}!

Discount: {discount:.1f}% OFF
Face Value: Rs.{FACE_VALUE}
Current Price: Rs.{current_price}

Link: {PRODUCT_URL}

Time: {ist_time}

---
This is an automated alert from your Amazon Monitor.
"""
    
    html_body = f"""
<html>
  <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
    <div style="max-width: 600px; margin: 0 auto; padding: 20px; background-color: #f9f9f9; border-radius: 10px;">
      <h2 style="color: #ff9900;">🎁 Amazon Gift Card Alert!</h2>
      
      <div style="background-color: white; padding: 20px; border-radius: 8px; margin: 20px 0;">
        <p style="font-size: 16px;">
          <strong>Amazon Pay Black Gift Card Box Rs.10000</strong> is now at 
          <span style="color: #ff6b00; font-size: 24px; font-weight: bold;">Rs.{current_price}</span>!
        </p>
        
        <p style="font-size: 18px;">
          <strong>Discount:</strong> <span style="color: #388e3c; font-weight: bold;">{discount:.1f}% OFF</span>
        </p>
        
        <p style="margin: 20px 0;">
          <a href="{PRODUCT_URL}" 
             style="display: inline-block; padding: 12px 24px; background-color: #ff9900; 
                    color: white; text-decoration: none; border-radius: 5px; font-weight: bold;">
            Buy Now on Amazon
          </a>
        </p>
      </div>
      
      <p style="font-size: 12px; color: #666; margin-top: 20px;">
        ⏰ Alert triggered at: {ist_time}<br>
        <em>This is an automated alert from your Amazon Monitor.</em>
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
        print(f"[{get_ist_now()}] Sending email alert...")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, email_password)
            server.send_message(message)
        print(f"[{get_ist_now()}] ✅ Email alert sent successfully!")
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] ❌ Failed to send email: {e}")
        return False


def check_amazon():
    """Main function to check Amazon price and send alert if needed"""
    print("=" * 60)
    print(f"Amazon Gift Card Monitor - Run at {get_ist_now()}")
    print("=" * 60)
    
    current_price = fetch_product_price()
    
    if current_price is None:
        print(f"[{get_ist_now()}] Could not fetch price")
        return False
    
    discount = ((FACE_VALUE - current_price) / FACE_VALUE) * 100
    print(f"[{get_ist_now()}] Current Price: Rs.{current_price}")
    print(f"[{get_ist_now()}] Discount: {discount:.1f}%")
    print(f"[{get_ist_now()}] Target: Rs.{TARGET_PRICE} ({TARGET_DISCOUNT}% discount)")
    
    # Check if price is at or below target AND discount is at or above target
    if current_price <= TARGET_PRICE and discount >= TARGET_DISCOUNT:
        print(f"[{get_ist_now()}] 🎯 Target reached!")
        
        # Check cooldown before sending
        if not should_send_alert('amazon'):
            return False
        
        print(f"[{get_ist_now()}] Sending alerts...")
        
        # Send both ntfy and email notifications
        ntfy_success = send_ntfy_alert(current_price)
        email_success = send_email_alert(current_price)
        
        # Record alert if at least one notification succeeded
        if ntfy_success or email_success:
            record_alert('amazon')
        
        return ntfy_success or email_success
    else:
        print(f"[{get_ist_now()}] Target not reached yet. Waiting...")
        return False


if __name__ == "__main__":
    try:
        check_amazon()
    except Exception as e:
        print(f"[{get_ist_now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
