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
from datetime import datetime
import re

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("Error: Required packages not installed. Run: pip install -r requirements.txt")
    sys.exit(1)


# Configuration
URL = "https://store.oneplay.in/view/flipkart-e-gift-voucher-inr-10000-0242469b-dc1c-11f0-a1d3-0636a7656735"
TARGET_DISCOUNT = 2.0


def fetch_page_content():
    """Fetch the webpage content"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    
    try:
        print(f"[{datetime.now()}] Fetching URL: {URL}")
        response = requests.get(URL, headers=headers, timeout=30)
        response.raise_for_status()
        print(f"[{datetime.now()}] Successfully fetched page (Status: {response.status_code})")
        return response.text
    except requests.RequestException as e:
        print(f"[{datetime.now()}] Error fetching page: {e}")
        return None


def extract_discount(html_content):
    """Extract discount percentage from HTML"""
    if not html_content:
        return None
    
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # Method 1: Look for percentage text in page content
    discount_patterns = [
        r'(\d+(?:\.\d+)?)\s*%\s*OFF',
        r'(\d+(?:\.\d+)?)\s*%\s*off',
        r'(\d+(?:\.\d+)?)\s*%\s*discount',
        r'(\d+(?:\.\d+)?)\s*%',
        r'discount[:\s]+(\d+(?:\.\d+)?)',
    ]
    
    # Search through all text content
    page_text = soup.get_text()
    print(f"[{datetime.now()}] Searching for discount patterns in page text...")
    
    for pattern in discount_patterns:
        result = re.search(pattern, page_text, re.IGNORECASE)
        if result:
            discount = float(result.group(1))
            print(f"[{datetime.now()}] Found discount using pattern '{pattern}': {discount}%")
            return discount
    
    # Method 2: Look in specific HTML elements (using 'string' instead of deprecated 'text')
    for pattern in discount_patterns:
        matches = soup.find_all(string=re.compile(pattern, re.IGNORECASE))
        for match in matches:
            result = re.search(pattern, str(match), re.IGNORECASE)
            if result:
                discount = float(result.group(1))
                print(f"[{datetime.now()}] Found discount in element: {discount}%")
                return discount
    
    # Method 3: Calculate from price
    try:
        # Look for price elements (using 'string' instead of deprecated 'text')
        price_elements = soup.find_all(string=re.compile(r'₹\s*[\d,]+'))
        print(f"[{datetime.now()}] Found {len(price_elements)} price elements")
        
        if len(price_elements) >= 2:
            # Extract numeric values, removing commas
            prices = []
            for elem in price_elements[:5]:  # Check first 5 price elements
                match = re.search(r'₹\s*([\d,]+)', str(elem))
                if match:
                    price_str = match.group(1).replace(',', '')
                    prices.append(float(price_str))
            
            print(f"[{datetime.now()}] Extracted prices: {prices}")
            
            if len(prices) >= 2:
                # Assume first is current price, find original price (should be higher)
                for i in range(1, len(prices)):
                    if prices[i] > prices[0]:
                        current_price = prices[0]
                        original_price = prices[i]
                        discount = ((original_price - current_price) / original_price) * 100
                        print(f"[{datetime.now()}] Calculated discount: {discount:.2f}% (₹{current_price} vs ₹{original_price})")
                        return round(discount, 2)
    except Exception as e:
        print(f"[{datetime.now()}] Could not calculate discount from prices: {e}")
    
    # Method 4: Look for discount in meta tags or JSON-LD
    try:
        meta_tags = soup.find_all('meta', attrs={'property': re.compile('price|discount', re.IGNORECASE)})
        for tag in meta_tags:
            content = tag.get('content', '')
            print(f"[{datetime.now()}] Meta tag content: {content}")
    except Exception as e:
        print(f"[{datetime.now()}] Could not check meta tags: {e}")
    
    print(f"[{datetime.now()}] No discount found on page")
    print(f"[{datetime.now()}] Page text sample (first 500 chars): {page_text[:500]}")
    return None


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
    
    # Create plain text and HTML versions
    text_body = f"""
Discount Alert!

The Flipkart E-Gift Voucher INR 10000 has reached {discount}% OFF!

{f'Current Price: ₹{current_price}' if current_price else 'Check the link for current price'}

🔗 Link: {URL}

⏰ Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}

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
          <a href="{URL}" 
             style="display: inline-block; padding: 12px 24px; background-color: #2874f0; 
                    color: white; text-decoration: none; border-radius: 5px; font-weight: bold;">
            Buy Now on OnePlay Store
          </a>
        </p>
      </div>
      
      <p style="font-size: 12px; color: #666; margin-top: 20px;">
        ⏰ Alert triggered at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}<br>
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
    
    html_content = fetch_page_content()
    if not html_content:
        print(f"[{datetime.now()}] Failed to fetch page content")
        return False
    
    discount = extract_discount(html_content)
    
    if discount is None:
        print(f"[{datetime.now()}] Could not extract discount information")
        return False
    
    print(f"[{datetime.now()}] Current discount: {discount}% | Target: {TARGET_DISCOUNT}%")
    
    if discount >= TARGET_DISCOUNT:
        print(f"[{datetime.now()}] 🎯 Target discount reached! Sending alert...")
        success = send_email_alert(discount)
        if success:
            # Save state to avoid duplicate alerts
            state_file = os.path.join(os.path.dirname(__file__), 'last_alert.json')
            try:
                with open(state_file, 'w') as f:
                    json.dump({
                        'timestamp': datetime.now().isoformat(),
                        'discount': discount,
                        'url': URL
                    }, f)
            except Exception as e:
                print(f"[{datetime.now()}] Warning: Could not save state: {e}")
        return success
    else:
        print(f"[{datetime.now()}] Target not reached yet. Waiting...")
        return False


if __name__ == "__main__":
    try:
        check_discount()
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

