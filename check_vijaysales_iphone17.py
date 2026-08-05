#!/usr/bin/env python3
"""
Vijay Sales -- iPhone 17 (256GB, all colours) stock + non-EMI card offer monitor.

Two independent alerts (each with its own cooldown):
  1. STOCK      -- any colour becomes serviceable at any watched pincode
  2. NON-EMI    -- a card offer with a non-EMI transaction type appears

Both signals come from clean, no-auth JSON APIs (reverse-engineered via DevTools):
  - Stock : GET  oms.vijaysales.systems/v1/servicability?pincode=<PIN>&vanNo=<csv skus>&storeList=true
  - Offers: POST vsprod.vijaysales.com/graphql  getOffers(sku:"...") -> jusPayOffer[]
Server-side, per-colour, per-pincode -- no HTML scraping, no JS execution needed.
"""

import os
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
except ImportError:
    print("Error: pip install -r requirements.txt")
    sys.exit(1)

IST = timezone(timedelta(hours=5, minutes=30))

COOLDOWN_HOURS = {
    "vijaysales_stock": float(os.environ.get(
        "VIJAYSALES_STOCK_COOLDOWN_HOURS",
        os.environ.get("VIJAYSALES_COOLDOWN_HOURS", os.environ.get("ALERT_COOLDOWN_HOURS", 12)))),
    "vijaysales_nonemi_offer": float(os.environ.get(
        "VIJAYSALES_NONEMI_COOLDOWN_HOURS",
        os.environ.get("VIJAYSALES_COOLDOWN_HOURS", os.environ.get("ALERT_COOLDOWN_HOURS", 24)))),
}
STATE_DIR = Path(".alert_state")
STATE_FILE = STATE_DIR / "last_alert.json"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

PRODUCT_URL = "https://www.vijaysales.com/p/P245179/245179/apple-iphone-17-256gb-storage-black"

SERVICE_URL = "https://oms.vijaysales.systems/v1/servicability"
OFFERS_URL = "https://vsprod.vijaysales.com/graphql"

# iPhone 17 256GB -- Vijay Sales SKUs (vanNo) per colour
SKU_COLORS = {
    "245179": "Black",
    "245180": "White",
    "245181": "Mist Blue",
    "245182": "Lavender",
    "245183": "Sage",
}

# Watched pincodes: 3 target areas + geographic neighbours.
_DEFAULT_PINCODES = (
    "560035,560103,560048,560087,"   # Bangalore SE
    "201019,201301,201009,110096,"   # Ghaziabad / Noida
    "636007,636006,636004,636009"    # Salem
)


def _pincodes() -> List[str]:
    raw = os.environ.get("VIJAYSALES_PINCODES", "").strip() or _DEFAULT_PINCODES
    return [p.strip() for p in raw.split(",") if p.strip()]


PINCODES = _pincodes()

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

GET_OFFERS_QUERY = (
    'query GetOffers { getOffers(sku: "245179") { '
    'jusPayOffer { title description txn_types card_types min_order_amount '
    'benefits { calculation_rule value maxAmount } } } }'
)


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
        cooldown = COOLDOWN_HOURS[alert_type]
        if elapsed_h < cooldown:
            print(f"[{get_ist_now()}] Cooldown active for {alert_type}: {elapsed_h:.2f}h / {cooldown:.2f}h")
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


def fetch_stock(session: requests.Session, pincode: str) -> Dict[str, Any]:
    """Return the API's per-SKU serviceability map for one pincode (all colours in one call)."""
    r = session.get(
        SERVICE_URL,
        params={"pincode": pincode, "vanNo": ",".join(SKU_COLORS), "storeList": "true"},
        headers={"User-Agent": UA, "Accept": "*/*", "Origin": "https://www.vijaysales.com",
                 "Referer": "https://www.vijaysales.com/"},
        timeout=30,
    )
    r.raise_for_status()
    return (r.json() or {}).get("data") or {}


def check_stock(session: requests.Session) -> List[Dict[str, Any]]:
    """Probe every watched pincode; return in-stock rows."""
    matches: List[Dict[str, Any]] = []
    for pin in PINCODES:
        try:
            data = fetch_stock(session, pin)
        except requests.RequestException as e:
            print(f"[{get_ist_now()}] stock fetch failed for {pin}: {e}")
            continue
        for sku, color in SKU_COLORS.items():
            d = data.get(sku) or {}
            serviceable = bool(d.get("isServiceable"))
            qty = d.get("maxQuantity") or 0
            modes = [m.get("mode") or m.get("deliveryType") or m.get("type")
                     for m in (d.get("shippingModes") or [])]
            stores = len(d.get("storePickupList") or [])
            flag = "IN STOCK" if serviceable else "-"
            print(f"[{get_ist_now()}] {pin}  {color:10} qty={qty:<3} modes={modes} stores={stores}  {flag}")
            if serviceable:
                matches.append({"pincode": pin, "color": color, "sku": sku,
                                "qty": qty, "modes": modes, "stores": stores})
    return matches


def fetch_nonemi_offers(session: requests.Session) -> List[Dict[str, Any]]:
    """Return card offers whose transaction type is NOT EMI (full-swipe / instant)."""
    try:
        r = session.post(
            OFFERS_URL,
            json={"query": GET_OFFERS_QUERY, "variables": {}},
            headers={"User-Agent": UA, "Accept": "*/*", "Content-Type": "application/json",
                     "Store": "vijay_sales", "Origin": "https://www.vijaysales.com",
                     "Referer": "https://www.vijaysales.com/"},
            timeout=30,
        )
        r.raise_for_status()
        offers = (((r.json() or {}).get("data") or {}).get("getOffers") or {}).get("jusPayOffer") or []
    except (requests.RequestException, ValueError) as e:
        print(f"[{get_ist_now()}] offers fetch failed: {e}")
        return []

    non_emi = []
    for o in offers:
        txn = str(o.get("txn_types") or "").upper()
        emi = "EMI" in txn
        print(f"[{get_ist_now()}] offer txn={txn or '(none)'}  emi={emi}  {o.get('title')}")
        if not emi:
            non_emi.append(o)
    return non_emi


def _stock_lines(matches: List[Dict[str, Any]]) -> List[str]:
    lines = []
    for m in matches:
        mode = ", ".join([x for x in m["modes"] if x]) or "delivery"
        store = f" +{m['stores']} store pickup" if m["stores"] else ""
        lines.append(f"- {m['color']} @ {m['pincode']}: qty {m['qty']} ({mode}){store}")
    return lines


def _offer_lines(offers: List[Dict[str, Any]]) -> List[str]:
    lines = []
    for o in offers:
        b = o.get("benefits") or {}
        val = f"{b.get('calculation_rule','')} {b.get('value','')}".strip()
        mn = o.get("min_order_amount")
        min_txt = f", min order Rs.{float(mn):.0f}" if mn else ""
        lines.append(f"- {o.get('title')} [{o.get('card_types')}, {val}{min_txt}]")
    return lines


def send_alert(subject: str, title: str, body_lines: List[str], tags: str) -> bool:
    message = "\n".join(body_lines + ["", f"Order: {PRODUCT_URL}"])
    ntfy_ok = email_ok = False

    if NTFY_TOPIC:
        try:
            requests.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=message.encode("utf-8"),
                headers={"Title": title, "Priority": "high", "Tags": tags, "Click": PRODUCT_URL},
                timeout=15,
            ).raise_for_status()
            print(f"[{get_ist_now()}] ntfy sent")
            ntfy_ok = True
        except Exception as e:
            print(f"[{get_ist_now()}] ntfy failed: {e}")
    else:
        print(f"[{get_ist_now()}] ntfy not configured (NTFY_TOPIC empty)")

    sender = os.environ.get("SENDER_EMAIL")
    receiver = os.environ.get("RECEIVER_EMAIL")
    password = os.environ.get("EMAIL_PASSWORD")
    if all([sender, receiver, password]):
        try:
            ist_time = get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST")
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = sender
            msg["To"] = receiver
            msg.attach(MIMEText(message + f"\n\nTime: {ist_time}", "plain"))
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(sender, password)
                server.send_message(msg)
            print(f"[{get_ist_now()}] email sent to {receiver}")
            email_ok = True
        except Exception as e:
            print(f"[{get_ist_now()}] email failed: {e}")
    else:
        print(f"[{get_ist_now()}] email not configured")

    return ntfy_ok or email_ok


def check_vijaysales_iphone17():
    print("=" * 60)
    print(f"Vijay Sales iPhone 17 (256GB) monitor -- {get_ist_now()}")
    print(f"Pincodes: {', '.join(PINCODES)}")
    print(f"Colours: {', '.join(SKU_COLORS.values())}")
    print("=" * 60)

    session = requests.Session()

    matches = check_stock(session)
    offers = fetch_nonemi_offers(session)

    fired = False

    if matches:
        summary = ", ".join(f"{m['color']}@{m['pincode']}" for m in matches)
        print(f"[{get_ist_now()}] STOCK match: {summary}")
        if should_send_alert("vijaysales_stock"):
            ok = send_alert(
                subject=f"Vijay Sales iPhone 17 in stock -- {len(matches)} match(es)",
                title=f"iPhone 17 in stock: {len(matches)} match(es)",
                body_lines=["iPhone 17 (256GB) is in stock at Vijay Sales:", ""] + _stock_lines(matches),
                tags="iphone,shopping",
            )
            if ok:
                record_alert("vijaysales_stock")
                fired = True
    else:
        print(f"[{get_ist_now()}] No colour in stock at any watched pincode.")

    if offers:
        print(f"[{get_ist_now()}] NON-EMI offer(s): {len(offers)}")
        if should_send_alert("vijaysales_nonemi_offer"):
            ok = send_alert(
                subject=f"Vijay Sales iPhone 17 non-EMI card offer -- {len(offers)}",
                title=f"iPhone 17 non-EMI card offer: {len(offers)}",
                body_lines=["Non-EMI card offer(s) on iPhone 17 at Vijay Sales:", ""] + _offer_lines(offers),
                tags="iphone,creditcard",
            )
            if ok:
                record_alert("vijaysales_nonemi_offer")
                fired = True
    else:
        print(f"[{get_ist_now()}] No non-EMI card offers (EMI-only or none).")

    return fired


if __name__ == "__main__":
    try:
        check_vijaysales_iphone17()
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
