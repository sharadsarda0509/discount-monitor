#!/usr/bin/env python3
"""
JioMart iPhone discount monitor — iPhone 15, 16, 17 series.

Alerts when ANY in-stock iPhone 15/16/17 has:
  (a) base price discount >= MIN_DISCOUNT_RS (effective < marked), OR
  (b) an active non-EMI bank card offer >= MIN_DISCOUNT_RS tagged for Electronics

Bank offers come from the content pages API (bankoffers/bankoffer tag).
Stock + base price come from the Fynd vertex search API.

Bearer token is JioMart's public Fynd app credential (base64 of app_id:app_token).
If requests start failing with 401, recapture from any /api/service/* request header.
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

MIN_DISCOUNT_RS = int(os.environ.get("JIOMART_MIN_DISCOUNT", "2000"))
COOLDOWN_HOURS = float(os.environ.get("JIOMART_IPHONE_COOLDOWN_HOURS", os.environ.get("ALERT_COOLDOWN_HOURS", 1)))
STATE_DIR = Path(".alert_state")
STATE_FILE = STATE_DIR / "last_alert.json"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

_raw = os.environ.get("JIOMART_PINCODES", "560035,560048,560103")
TARGET_PINCODES = [p.strip() for p in _raw.split(",") if p.strip()]

BEARER_TOKEN = "Bearer Njg1OTQ1ZjQ2YzhjN2FlZTNmM2FmNjA1OlRwS3c3d0Q5aA=="

SEARCH_URL = "https://www.jiomart.com/ext/vertex/application/api/v1.0/products"
BANKOFFERS_URL = "https://www.jiomart.com/api/service/application/content/v2.0/pages"

SEARCH_QUERIES = [
    ("iPhone 15", "apple iphone 15"),
    ("iPhone 16", "apple iphone 16"),
    ("iPhone 17", "apple iphone 17"),
]

# Decoded form — requests will URL-encode this correctly (no double-encoding)
STORE_FILTER = "journey:quickcommerce:::store_ids:14050||3191||15460"
JIOMART_BASE = "https://www.jiomart.com/product"

# Regex to extract rupee amounts from offer titles/descriptions
_RS_RE = re.compile(r"[Rr]s\.?\s*([\d,]+)\s*(?:[Oo]ff|[Dd]iscount|[Ii]nstant|[Cc]ashback)")


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


def make_headers(pincode: str) -> Dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "authorization": BEARER_TOKEN,
        "x-currency-code": "INR",
        "x-location-detail": json.dumps({
            "country": "INDIA",
            "country_iso_code": "IN",
            "city": "BENGALURU",
            "pincode": pincode,
            "state": "KARNATAKA",
        }),
        "x-geolocation": json.dumps({
            "latitude": "12.9371619",
            "longitude": "77.6951926",
            "polygon_ids": ["TN1S_QC_aa4f6670", "8792_QC_efefecd4"],
        }),
        "x-fp-sdk-version": "1.10.3-60",
        "Origin": "https://www.jiomart.com",
        "Referer": "https://www.jiomart.com/",
    }


def _parse_rs_amount(text: str) -> int:
    """Extract the largest rupee amount mentioned in text (e.g. 'Rs. 2000 Off' → 2000)."""
    amounts = [int(m.replace(",", "")) for m in _RS_RE.findall(text)]
    return max(amounts) if amounts else 0


def _is_emi_offer(title: str, description: str) -> bool:
    """Return True if the offer is EMI-only (and not a direct/non-EMI bank offer)."""
    combined = (title + " " + description).lower()
    if "non-emi" in combined or "non emi" in combined:
        return False
    return "emi" in combined


def _is_electronics_offer(tags: List[str]) -> bool:
    """Return True if the offer applies to electronics (tagged or generic)."""
    lower_tags = [t.lower() for t in tags]
    category_tags = [t for t in lower_tags if t not in ("bankoffer", "bankoffers")]
    if not category_tags:
        return True  # generic — applies to all categories
    return "electronics" in lower_tags


def check_bank_offers() -> Optional[Dict[str, Any]]:
    """
    Fetch active non-EMI bank card offers for Electronics.
    Returns the best qualifying offer dict (amount, title) or None.
    """
    try:
        r = requests.get(
            BANKOFFERS_URL,
            params={"page_size": "50", "tags": "bankoffers,bankoffer"},
            headers=make_headers(TARGET_PINCODES[0]),
            timeout=20,
        )
        r.raise_for_status()
        items = r.json().get("items") or []
    except Exception as e:
        print(f"  [BankOffers] fetch failed: {e}")
        return None

    now = get_ist_now()
    best: Optional[Dict[str, Any]] = None

    for item in items:
        if not item.get("published"):
            continue

        schedule = (item.get("_schedule") or {}).get("next_schedule") or []
        active = False
        for s in schedule:
            start = s.get("start", "")
            end = s.get("end", "")
            try:
                start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
                if start_dt <= now <= end_dt:
                    active = True
                    break
            except Exception:
                pass
        if not active:
            continue

        tags = item.get("tags") or []
        if not _is_electronics_offer(tags):
            continue

        title = item.get("title", "")
        desc = item.get("description", "")
        if _is_emi_offer(title, desc):
            continue

        amount = _parse_rs_amount(title) or _parse_rs_amount(desc)
        if amount < MIN_DISCOUNT_RS:
            continue

        print(f"  [BankOffer] {title!r} — ₹{amount:,} off (non-EMI, Electronics)")
        if best is None or amount > best["amount"]:
            best = {"amount": amount, "title": title, "description": desc}

    return best


def search_in_stock_iphones(label: str, query: str, pincode: str) -> List[Dict[str, Any]]:
    """
    Search for a model and return all IN-STOCK items with their base price info.
    Includes both discounted and full-price items.
    """
    params = {
        "f": STORE_FILTER,
        "page_size": "30",
        "q": query,
    }
    try:
        r = requests.get(SEARCH_URL, params=params, headers=make_headers(pincode), timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  [{label}] Search failed: {e}")
        return []

    items = data.get("items") or []
    results = []
    for item in items:
        name = item.get("name", "")
        if not any(gen in name for gen in ["iPhone 15", "iPhone 16", "iPhone 17"]):
            continue
        if any(w in name.lower() for w in ["adapter", "cable", "case", "cover", "charger", "airpod", "watch"]):
            continue

        price = item.get("price") or {}
        effective = (price.get("effective") or {}).get("min", 0)
        marked = (price.get("marked") or {}).get("min", 0)
        in_stock = item.get("sellable", True)
        slug = item.get("slug", "")
        url = f"{JIOMART_BASE}/{slug}" if slug else ""
        base_discount = max(0, marked - effective) if marked > 0 else 0

        stock_str = "IN STOCK" if in_stock else "OOS"
        if effective < marked:
            price_str = f"₹{effective:,} (was ₹{marked:,}, -₹{base_discount:,})"
        else:
            price_str = f"₹{effective:,} (no base discount)"
        print(f"  [{label}] {name}: {price_str}  [{stock_str}]")

        if in_stock and effective > 0:
            results.append({
                "name": name,
                "effective": effective,
                "marked": marked,
                "base_discount": base_discount,
                "url": url,
            })

    return results


def send_ntfy_alert(
    pincode: str,
    in_stock: List[Dict[str, Any]],
    bank_offer: Optional[Dict[str, Any]],
) -> bool:
    if not NTFY_TOPIC:
        print(f"[{get_ist_now()}] ntfy not configured")
        return False
    try:
        lines = [f"JioMart iPhone deal — pincode {pincode}!\n"]
        if bank_offer:
            lines.append(f"Bank offer: {bank_offer['title']} (₹{bank_offer['amount']:,} off)")
            lines.append("")
        for m in in_stock:
            if m["base_discount"] > 0:
                lines.append(f"• {m['name']}: ₹{m['effective']:,} (was ₹{m['marked']:,}, -₹{m['base_discount']:,})")
            else:
                lines.append(f"• {m['name']}: ₹{m['effective']:,} (in stock)")
            lines.append(f"  {m['url']}")
        message = "\n".join(lines)

        best_base = max((m["base_discount"] for m in in_stock), default=0)
        best_amount = max(best_base, bank_offer["amount"] if bank_offer else 0)
        best_item = max(in_stock, key=lambda x: x["base_discount"]) if in_stock else None
        click = best_item["url"] if best_item else ""
        title = f"JioMart iPhone deal: up to ₹{best_amount:,} off"

        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "urgent",
                "Tags": "iphone,shopping,rotating_light",
                "Click": click,
            },
            timeout=15,
        ).raise_for_status()
        print(f"[{get_ist_now()}] ntfy sent for {pincode}")
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] ntfy failed: {e}")
        return False


def send_email_alert(
    pincode: str,
    in_stock: List[Dict[str, Any]],
    bank_offer: Optional[Dict[str, Any]],
) -> bool:
    sender = os.environ.get("SENDER_EMAIL")
    receiver = os.environ.get("RECEIVER_EMAIL")
    password = os.environ.get("EMAIL_PASSWORD")
    if not all([sender, receiver, password]):
        print(f"[{get_ist_now()}] Email not configured")
        return False
    try:
        ist_time = get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST")
        lines = [f"JioMart iPhone deal — pincode {pincode}\n"]
        if bank_offer:
            lines.append(f"Bank offer active: {bank_offer['title']}")
            lines.append(f"  {bank_offer['description']}\n")
        for m in in_stock:
            if m["base_discount"] > 0:
                lines.append(f"  • {m['name']}: ₹{m['effective']:,} (was ₹{m['marked']:,}, -₹{m['base_discount']:,})")
            else:
                lines.append(f"  • {m['name']}: ₹{m['effective']:,} (in stock)")
            lines.append(f"    {m['url']}")
        lines.append(f"\nChecked at: {ist_time}")
        text_body = "\n".join(lines)

        best_base = max((m["base_discount"] for m in in_stock), default=0)
        best_amount = max(best_base, bank_offer["amount"] if bank_offer else 0)

        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"JioMart iPhone deal: up to ₹{best_amount:,} off — {pincode}"
        msg["From"] = sender
        msg["To"] = receiver
        msg.attach(MIMEText(text_body, "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(sender, password)
            srv.send_message(msg)
        print(f"[{get_ist_now()}] Email sent for {pincode}")
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] Email failed: {e}")
        return False


def check_jiomart_iphone():
    print("=" * 60)
    print(f"JioMart iPhone monitor — {get_ist_now()}")
    print(f"Models: iPhone 15, 16, 17  |  Pincodes: {', '.join(TARGET_PINCODES)}")
    print(f"Min discount: ₹{MIN_DISCOUNT_RS:,}")
    print("=" * 60)

    # Bank offer check is global (not pincode-specific)
    print("\n[Bank Offers]")
    bank_offer = check_bank_offers()
    if bank_offer:
        print(f"  -> Qualifying offer found: ₹{bank_offer['amount']:,} off (non-EMI, Electronics)")
    else:
        print(f"  -> No active non-EMI Electronics bank offer >= ₹{MIN_DISCOUNT_RS:,}")

    any_alerted = False

    for pincode in TARGET_PINCODES:
        print(f"\n{'─' * 40}")
        print(f"Pincode: {pincode}")
        print("─" * 40)

        all_in_stock: List[Dict[str, Any]] = []
        for label, query in SEARCH_QUERIES:
            found = search_in_stock_iphones(label, query, pincode)
            all_in_stock.extend(found)

        # Deduplicate by URL
        seen: set = set()
        unique_in_stock: List[Dict[str, Any]] = []
        for m in all_in_stock:
            if m["url"] not in seen:
                seen.add(m["url"])
                unique_in_stock.append(m)

        if not unique_in_stock:
            print(f"  No iPhones in stock for {pincode}.")
            continue

        # Items that qualify on base price alone
        base_qualifying = [m for m in unique_in_stock if m["base_discount"] >= MIN_DISCOUNT_RS]

        # Items qualifying via bank offer (any in-stock iPhone + bank_offer present)
        bank_qualifying = unique_in_stock if bank_offer else []

        # Merge both qualifying sets (deduplicated)
        qualifying_urls: set = {m["url"] for m in base_qualifying} | {m["url"] for m in bank_qualifying}
        qualifying = [m for m in unique_in_stock if m["url"] in qualifying_urls]

        if not qualifying:
            print(f"  {len(unique_in_stock)} iPhone(s) in stock but no qualifying discount for {pincode}.")
            continue

        print(f"\n  {len(qualifying)} qualifying iPhone(s) found for {pincode}!")
        if base_qualifying:
            print(f"  Base discount >={MIN_DISCOUNT_RS:,}: {[m['name'] for m in base_qualifying]}")
        if bank_offer and bank_qualifying:
            print(f"  Bank offer applicable: ₹{bank_offer['amount']:,} off on {len(bank_qualifying)} item(s)")

        alert_key = f"jiomart_iphone_discount_{pincode}"
        if not should_send_alert(alert_key):
            continue

        ntfy_ok = send_ntfy_alert(pincode, qualifying, bank_offer)
        email_ok = send_email_alert(pincode, qualifying, bank_offer)
        if ntfy_ok or email_ok:
            record_alert(alert_key)
            any_alerted = True

    return any_alerted


if __name__ == "__main__":
    try:
        check_jiomart_iphone()
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
