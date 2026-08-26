#!/usr/bin/env python3
"""
Croma -- iPhone 15 / 16 / 17 base handset stock monitor (pincode-serviceable).

Croma (Tata) runs on a SAP-Commerce backend fronted by api.croma.com. Its search
/ product-listing APIs (/product/, /searchservices/) sit behind Akamai Bot Manager
and return 403 to any headless client -- so handsets CANNOT be discovered by search
from GitHub Actions. BUT two per-product endpoints are NOT bot-gated and answer a
plain server request (curl_cffi Chrome impersonation, no auth token, no cookies):

  1. Price/name : GET  /sku/v1/essentialcombo?pinCode=<pin>&ProductSkus=<sku>
                  -> images.altText (name), price, mrp, url
  2. Stock      : POST /inventory/oms/v2/tms/details-pwa/
                  body asks 3 fulfillment types (HDEL home-delivery, STOR store
                  pickup, SDEL same-day) for <itemID> at <zipCode>. A fulfilled
                  line lands in promise.suggestedOption.option.promiseLines; an
                  unfulfilled one in unavailableLines. Any promiseLine => in stock.

Because search is gated, watched handsets are SEEDED as SKUs (CROMA_PRODUCTS),
BigBasket-style, with the base iPhone 15/16/17 colours Croma lists today. Add a
new colour/SKU via the env var without code changes. The _is_handset name filter
keeps this to base models only (no Pro / Plus / Air / mini / e).

Alert condition: a watched base iPhone is serviceable (in stock) at the pincode via
any fulfillment type. Pure stock alert -- no offer requirement (like the BigBasket
and Blinkit iPhone monitors).
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
from typing import Any, Dict, List, Optional

from iphone_models import models_summary

try:
    from curl_cffi import requests as cffi_requests
    _SESSION = cffi_requests.Session(impersonate="chrome120")
    def _get(url, **kw): return _SESSION.get(url, **kw)
    def _post(url, **kw): return _SESSION.post(url, **kw)
except ImportError:
    import requests as _requests
    _SESSION = _requests.Session()
    def _get(url, **kw): return _SESSION.get(url, **kw)
    def _post(url, **kw): return _SESSION.post(url, **kw)

try:
    import requests  # for RequestException type only
except ImportError:
    print("Error: pip install -r requirements.txt")
    sys.exit(1)

IST = timezone(timedelta(hours=5, minutes=30))
COOLDOWN_HOURS = float(os.environ.get(
    "CROMA_COOLDOWN_HOURS", os.environ.get("ALERT_COOLDOWN_HOURS", 0.5)))
STATE_DIR = Path(".alert_state")
STATE_FILE = STATE_DIR / "last_alert.json"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# Pincodes to watch (comma-separated). A phone alerts if it is serviceable at ANY
# of them; each in-stock line names the store and the serviceable pincode.
PINCODES = [p.strip() for p in os.environ.get("CROMA_PINCODE", "560035,560048").split(",") if p.strip()]
# Which iPhone number-series to watch (comma-separated). Sub-variants such as
# "17 Pro", "16 Plus", "17e" are excluded automatically by _is_handset.
MODELS = [m.strip() for m in os.environ.get("CROMA_MODELS", "15,16,17").split(",") if m.strip()]

# Watched product SKUs (comma-separated). Seeded with the base iPhone 15/16/17
# colours Croma lists today; search is Akamai-gated so SKUs can't be discovered
# headlessly. Add a new colour/SKU via CROMA_PRODUCTS without code changes.
_DEFAULT_PRODUCTS = (
    # iPhone 15 (128GB): Black, Blue, Pink, Yellow, Green
    "300652,300684,300679,300825,300665,"
    # iPhone 16 (128GB): Black, White, Teal, Pink, Ultramarine
    "309621,309692,309695,309693,309694,"
    # iPhone 17 (256GB): Black, White, Mist Blue, Lavender, Sage
    "317396,317398,317400,317401,317403"
)

BASE = "https://api.croma.com"
ESSENTIAL_URL = f"{BASE}/sku/v1/essentialcombo"
INVENTORY_URL = f"{BASE}/inventory/oms/v2/tms/details-pwa/"
STORE_URL = f"{BASE}/lookup/mobile-app/v1/storelocation"
PRODUCT_BASE_URL = "https://www.croma.com"
SEARCH_URL = "https://www.croma.com/search/?q=apple%20iphone"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.croma.com",
    "Referer": "https://www.croma.com/",
}

# Fulfillment types Croma checks, mapped to human labels for the alert.
FULFILLMENT = {"HDEL": "Home delivery", "STOR": "Store pickup", "SDEL": "Same-day delivery"}

# Accessory keywords -- exclude so only real handsets remain.
_ACCESSORY = re.compile(
    r"\b(case|cover|strap|glass|protector|charger|cable|adapter|screen|guard|"
    r"skin|holder|mount|stand|airpod|watch|band|tempered|wallet|magsafe|finewoven|"
    r"battery|power|pouch|sleeve|lens|film|dock|grip)\b", re.I)


def _is_handset(name: str) -> bool:
    """True only for the exact base iPhone models in MODELS.

    Excludes variant suffixes -- Plus, Pro, Pro Max, Air, mini, and the 'e' models
    (e.g. 17e). "iPhone 17" matches; "iPhone 17 Pro" / "17e" / "16 Plus" do not.
    """
    if not re.search(r"\bi[pP]hone\b", name, re.I):
        return False
    if _ACCESSORY.search(name):
        return False
    # storage present (e.g. "128GB", "256 GB", "1 TB")
    if not re.search(r"\d+\s*(GB|TB)\b", name, re.I):
        return False
    # exact base model: number followed by a word boundary (kills "17e") and NOT by a
    # variant word (kills "16 Plus", "17 Pro", "17 Pro Max", "Air", "mini").
    pattern = (r"\bi[pP]hone\s*(" + "|".join(map(re.escape, MODELS)) +
               r")\b(?!\s*(?:plus|pro|max|air|mini))")
    return bool(re.search(pattern, name, re.I))


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
            print(f"[{get_ist_now()}] Cooldown active for {alert_type}: {elapsed_h:.2f}h / {COOLDOWN_HOURS}h")
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


def _skus() -> List[str]:
    raw = os.environ.get("CROMA_PRODUCTS", "").strip() or _DEFAULT_PRODUCTS
    return [s.strip() for s in raw.split(",") if s.strip()]


# Ship-node -> "Store name, City" resolver. The inventory API returns the fulfilling
# node as a store code (e.g. "A736"); Croma's public store directory keys every store
# by that same code, so we can name the location. Fetched once per run, lazily (only
# when there IS an in-stock match), and cached for the process.
_STORE_DIR: Optional[Dict[str, str]] = None


def _store_dir() -> Dict[str, str]:
    global _STORE_DIR
    if _STORE_DIR is not None:
        return _STORE_DIR
    _STORE_DIR = {}
    try:
        r = _get(STORE_URL, params={"pageSize": "1000", "sort": "asc",
                                    "radius": "25000", "fields": "FULL"},
                 headers=HEADERS, timeout=30)
        r.raise_for_status()
        for st in (r.json() or {}).get("stores") or []:
            code = st.get("name")
            if not code:
                continue
            addr = st.get("address") or {}
            city = (addr.get("city") or {}).get("name") or addr.get("town") or ""
            label = st.get("displayName") or f"Store {code}"
            _STORE_DIR[code] = f"{label}, {city}".rstrip(", ") if city else label
    except (requests.RequestException, ValueError) as e:
        print(f"[{get_ist_now()}] store directory fetch failed: {e}")
    return _STORE_DIR


def _resolve_node(node: Optional[str]) -> str:
    if not node:
        return ""
    return _store_dir().get(node) or f"node {node}"


def fetch_product(sku: str) -> Optional[Dict[str, Any]]:
    """Name/price via essentialcombo. Returns None if not a watched base handset."""
    try:
        r = _get(ESSENTIAL_URL, params={"pinCode": PINCODES[0], "ProductSkus": sku},
                 headers=HEADERS, timeout=30)
        r.raise_for_status()
        data = r.json() or []
    except (requests.RequestException, ValueError) as e:
        print(f"[{get_ist_now()}] essentialcombo failed for {sku}: {e}")
        return None
    if not data:
        print(f"[{get_ist_now()}] {sku}: no product data")
        return None
    p = data[0]
    name = ((p.get("images") or {}).get("altText") or "").strip()
    if not _is_handset(name):
        print(f"[{get_ist_now()}] {name or sku!r} is not a watched base handset -- skipping")
        return None
    path = p.get("url") or f"/p/{sku}"
    return {
        "sku": sku,
        "name": name,
        "price": (p.get("price") or {}).get("value"),
        "mrp": (p.get("mrp") or {}).get("value"),
        "url": PRODUCT_BASE_URL + path if path.startswith("/") else path,
    }


def _inventory_body(sku: str, pincode: str) -> Dict[str, Any]:
    def line(ft: str, lid: str) -> Dict[str, Any]:
        return {
            "fulfillmentType": ft, "mch": "", "itemID": sku, "lineId": lid,
            "categoryType": "mobile", "reqEndDate": "2500-01-01", "reqStartDate": "",
            "requiredQty": "1",
            "shipToAddress": {"company": "", "country": "", "city": "", "mobilePhone": "",
                              "state": "", "zipCode": pincode,
                              "extn": {"irlAddressLine1": "", "irlAddressLine2": ""}},
            "extn": {"widerStoreFlag": "N"},
        }
    return {"promise": {"allocationRuleID": "SYSTEM", "checkInventory": "Y",
                        "organizationCode": "CROMA", "sourcingClassification": "EC",
                        "promiseLines": {"promiseLine": [
                            line("HDEL", "1"), line("STOR", "2"), line("SDEL", "3")]}}}


def _stock_at(sku: str, pincode: str) -> List[Dict[str, Any]]:
    """Available fulfillments for a SKU at one pincode (empty if out of stock)."""
    try:
        r = _post(INVENTORY_URL, data=json.dumps(_inventory_body(sku, pincode)),
                  headers={**HEADERS, "Content-Type": "application/json"}, timeout=30)
        r.raise_for_status()
        promise = (r.json() or {}).get("promise") or {}
    except (requests.RequestException, ValueError) as e:
        print(f"[{get_ist_now()}] inventory failed for {sku} @ {pincode}: {e}")
        return []

    lines = (((promise.get("suggestedOption") or {}).get("option") or {})
             .get("promiseLines") or {}).get("promiseLine") or []
    out = []
    for ln in lines:
        method = FULFILLMENT.get(ln.get("fulfillmentType"), ln.get("fulfillmentType"))
        for a in ((ln.get("assignments") or {}).get("assignment") or [{}]):
            out.append({"method": method, "node": a.get("shipNode"),
                        "delivery": a.get("deliveryDate"), "pincode": pincode})
    return out


def check_stock(product: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return stock info if the SKU is serviceable at ANY watched pincode, else None."""
    fulfillments: List[Dict[str, Any]] = []
    for pincode in PINCODES:
        fulfillments.extend(_stock_at(product["sku"], pincode))
    if not fulfillments:
        return None
    return {**product, "fulfillments": fulfillments}


def _fmt_date(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%d %b, %I:%M %p")
    except Exception:
        return iso.split("T")[0]


def _where_lines(fulfillments: List[Dict[str, Any]]) -> List[str]:
    """One readable line per available fulfillment: method, store/city, pincode, ETA."""
    seen, out = set(), []
    for f in fulfillments:
        store = _resolve_node(f.get("node"))
        key = (f.get("method"), store, f.get("pincode"))
        if key in seen:
            continue
        seen.add(key)
        parts = [f.get("method") or "Available"]
        if store:
            parts.append(f"from {store}")
        if f.get("pincode"):
            parts.append(f"to {f['pincode']}")
        eta = _fmt_date(f.get("delivery"))
        if eta:
            parts.append(f"by {eta}")
        out.append("    * " + " ".join(parts))
    return out


def _stock_lines(matches: List[Dict[str, Any]]) -> List[str]:
    lines = []
    for m in matches:
        price = ""
        if m.get("price"):
            price = f" -- Rs.{float(m['price']):.0f}"
            if m.get("mrp") and m["mrp"] != m["price"]:
                price += f" (MRP Rs.{float(m['mrp']):.0f})"
        lines.append(f"- {m['name']}{price}")
        lines.extend(_where_lines(m.get("fulfillments") or []))
        lines.append(f"    * {m['url']}")
    return lines


def send_alert(matches: List[Dict[str, Any]]) -> bool:
    pins = "/".join(PINCODES)
    models = models_summary(m["name"] for m in matches)
    subject = f"Croma: {models} in stock @ {pins} -- {len(matches)} variant(s)"
    title = f"{models} in stock at Croma ({len(matches)})"
    click_url = matches[0]["url"] if matches else SEARCH_URL
    body = "\n".join(
        [f"iPhone in stock / serviceable at {pins} on Croma:", ""]
        + _stock_lines(matches)
        + ["", f"Shop: {SEARCH_URL}"]
    )
    ntfy_ok = email_ok = False

    if NTFY_TOPIC:
        try:
            _post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=body.encode("utf-8"),
                headers={"Title": title, "Priority": "high", "Tags": "iphone,croma",
                         "Click": click_url},
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
            msg.attach(MIMEText(body + f"\n\nTime: {ist_time}", "plain"))
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


def check_croma_iphone():
    print("=" * 60)
    print(f"Croma iPhone monitor -- {get_ist_now()}")
    print(f"Pincodes: {', '.join(PINCODES)}   Models: {', '.join(MODELS)}   SKUs: {len(_skus())}")
    print("=" * 60)

    matches: List[Dict[str, Any]] = []
    for sku in _skus():
        product = fetch_product(sku)
        if not product:
            continue
        info = check_stock(product)
        if not info:
            print(f"[{get_ist_now()}] {product['name']:45.45}  out of stock")
            continue
        methods = sorted({f["method"] for f in info["fulfillments"] if f.get("method")})
        print(f"[{get_ist_now()}] {info['name']:45.45}  IN STOCK ({', '.join(methods)})")
        matches.append(info)

    if not matches:
        print(f"[{get_ist_now()}] No watched iPhone in stock at {', '.join(PINCODES)} on Croma.")
        return False

    print(f"[{get_ist_now()}] IN STOCK: {[m['name'] for m in matches]}")
    if not should_send_alert("croma_iphone"):
        return False

    ok = send_alert(matches)
    if ok:
        record_alert("croma_iphone")
    return ok


if __name__ == "__main__":
    try:
        check_croma_iphone()
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
