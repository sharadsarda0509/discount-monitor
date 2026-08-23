#!/usr/bin/env python3
"""
Reliance Digital -- iPhone 15 / 16 / 17 stock monitor (pincode-serviceable).

Reliance Digital runs on the Fynd storefront platform. Its catalog/serviceability
APIs are clean, no-auth JSON (only a static, public application token in the
Authorization header -- reverse-engineered via DevTools). No signature, no cookies,
no JS execution needed -- works from a plain server request.

Flow (static Bearer token):
  1. Search   : /api/service/application/catalog/v1.0/products/?q=apple+iphone+<model>
  2. Sizes    : /api/service/application/catalog/v1.0/products/{slug}/sizes/
                -> national availability (quantity, is_available)
  3. Price    : /api/service/application/catalog/v1.0/products/{slug}/sizes/{size}/
                pincode/{pincode}/price/
                -> article_id + price. NOTE: this endpoint IGNORES the pincode and always
                returns a national "optimal seller" quantity/store, so its quantity is NOT a
                serviceability signal -- it is used here only to obtain article_id + price.
  4. Inventory: POST /ext/raven-api/inventory/multi/articles-v2
                body {"articles":[{"article_id":<id>,"custom_json":{},"quantity":0}],
                      "phone_number":"0","pincode":<pincode>,"request_page":"pdp"}
                -> data.success == true means genuinely in stock AND deliverable to <pincode>
                (this is the same call the website makes when you set a delivery pincode).

Handsets are discovered dynamically (not hardcoded), so a new model -- e.g. iPhone 17
-- is picked up the day it lands in the catalog, without any code change.

Alert condition: a watched iPhone (15/16/17 base, no Pro/Pro Max) must be BOTH in
stock at the pincode AND carry a qualifying offer -- either a non-EMI instant/bank
offer, or a Kotak (RELIANCE_EMI_BANK) No-Cost EMI offer. Stock alone does not alert.
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
    "RELIANCE_COOLDOWN_HOURS", os.environ.get("ALERT_COOLDOWN_HOURS", 12)))
STATE_DIR = Path(".alert_state")
STATE_FILE = STATE_DIR / "last_alert.json"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

PINCODE = os.environ.get("RELIANCE_PINCODE", "560035").strip()
# Which iPhone number-series to watch (comma-separated). Sub-variants such as
# "16 Plus", "16e", "17 Pro Max" are matched automatically by the number.
MODELS = [m.strip() for m in os.environ.get("RELIANCE_MODELS", "15,16,17").split(",") if m.strip()]

BASE = "https://www.reliancedigital.in/api/service/application/catalog/v1.0"
PROMO_URL = "https://www.reliancedigital.in/ext/raven-api/promotions"
# Authoritative per-pincode stock + delivery check (the call the site makes on pincode set).
INVENTORY_URL = "https://www.reliancedigital.in/ext/raven-api/inventory/multi/articles-v2"
PRODUCT_BASE_URL = "https://www.reliancedigital.in/product/"
SEARCH_URL = f"https://www.reliancedigital.in/search?q="

# Static, public Fynd application token embedded in the reliancedigital.in frontend.
# (decodes to <application_id>:<application_token>) -- no per-request signature needed.
AUTH_TOKEN = os.environ.get(
    "RELIANCE_AUTH_TOKEN",
    "Bearer NjQ1YTA1Nzg3NWQ4YzQ4ODJiMDk2ZjdlOl9fLU80NC00aQ==")

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "authorization": AUTH_TOKEN,
    "x-currency-code": "INR",
    "Origin": "https://www.reliancedigital.in",
    "Referer": "https://www.reliancedigital.in/",
}

# Accessory keywords -- exclude so only real handsets remain.
_ACCESSORY = re.compile(
    r"\b(case|cover|strap|glass|protector|charger|cable|adapter|screen|guard|"
    r"skin|holder|mount|stand|airpod|watch|band|tempered|wallet|magsafe|finewoven|"
    r"battery|power|pouch|sleeve|lens|film|dock|grip)\b", re.I)


def _is_handset(name: str) -> bool:
    """True only for the exact base iPhone models in MODELS.

    Excludes variant suffixes -- Plus, Pro, Pro Max, Air, mini, and the 'e' models
    (e.g. 16e). "iPhone 16" matches; "iPhone 16 Plus" / "16e" / "16 Pro" do not.
    """
    if not re.search(r"\bi[pP]hone\b", name, re.I):
        return False
    if _ACCESSORY.search(name):
        return False
    # storage present (e.g. "128 GB", "256GB", "1 TB")
    if not re.search(r"\d+\s*(GB|TB)\b", name, re.I):
        return False
    # exact base model: number followed by a word boundary (kills "16e") and NOT by a
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


def search_handsets() -> List[Dict[str, Any]]:
    """Search each watched model and return de-duplicated handset products."""
    seen: Dict[str, Dict[str, Any]] = {}
    for model in MODELS:
        q = f"apple iphone {model}"
        try:
            r = _get(f"{BASE}/products/", params={"q": q, "page_size": "50"},
                     headers=HEADERS, timeout=30)
            r.raise_for_status()
            items = (r.json() or {}).get("items") or []
        except (requests.RequestException, ValueError) as e:
            print(f"[{get_ist_now()}] search failed for '{q}': {e}")
            continue
        for p in items:
            name = (p.get("name") or "").strip()
            slug = p.get("slug")
            if not slug or slug in seen or not _is_handset(name):
                continue
            seen[slug] = {
                "name": name,
                "slug": slug,
                "sellable": bool(p.get("sellable")),
            }
    return list(seen.values())


def pincode_serviceable(article_id: str) -> Optional[Dict[str, Any]]:
    """Authoritative per-pincode stock + deliverability check for an article.

    The catalog /price/ endpoint ignores the pincode (it returns a national "optimal
    seller" store/quantity regardless of the pincode in the URL), so it cannot tell us
    whether an item is actually orderable to PINCODE. This endpoint -- the same one the
    website calls when you set a delivery pincode -- does respect the pincode.

    Returns {qty, display} when deliverable (data.success == true), else None.
    The `custom_json` key must be present (even as {}) or the API returns a spurious
    out-of-stock response.
    """
    try:
        r = _post(
            INVENTORY_URL,
            headers=HEADERS,
            json={
                "articles": [{"article_id": article_id, "custom_json": {}, "quantity": 0}],
                "phone_number": "0",
                "pincode": PINCODE,
                "request_page": "pdp",
            },
            timeout=30,
        )
        r.raise_for_status()
        data = (r.json() or {}).get("data") or {}
    except (requests.RequestException, ValueError) as e:
        print(f"[{get_ist_now()}] inventory check failed for {article_id}: {e}")
        return None

    if not data.get("success"):
        return None
    art = (data.get("articles") or [{}])[0]
    return {"qty": art.get("quantity") or 1, "display": art.get("display_message") or ""}


def check_pincode_stock(handset: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return stock info if the handset is in stock AND deliverable to PINCODE, else None."""
    slug = handset["slug"]
    # 1) sizes -> national availability + size value
    try:
        r = _get(f"{BASE}/products/{slug}/sizes/", headers=HEADERS, timeout=30)
        r.raise_for_status()
        sizes_data = r.json() or {}
    except (requests.RequestException, ValueError) as e:
        print(f"[{get_ist_now()}] sizes failed for {slug}: {e}")
        return None

    if not sizes_data.get("sellable"):
        return None
    avail = [s for s in (sizes_data.get("sizes") or []) if s.get("is_available")]
    if not avail:
        return None
    size = avail[0].get("value") or "OS"

    # 2) price -> article_id + price (quantity/store here are national, NOT pincode-accurate)
    try:
        r = _get(f"{BASE}/products/{slug}/sizes/{size}/pincode/{PINCODE}/price/",
                 headers=HEADERS, timeout=30)
        if r.status_code != 200:
            return None
        price = r.json() or {}
    except (requests.RequestException, ValueError) as e:
        print(f"[{get_ist_now()}] pincode price failed for {slug}: {e}")
        return None

    eff = (price.get("price_per_piece") or price.get("price") or {})
    # article_id looks like "6780_494423013"; the promotions/inventory APIs want the trailing id
    article_raw = str(price.get("article_id") or "")
    article_id = article_raw.split("_")[-1] if article_raw else ""
    if not article_id:
        return None

    # 3) authoritative per-pincode serviceability -- this is the real stock gate
    serv = pincode_serviceable(article_id)
    if not serv:
        return None

    return {
        "name": handset["name"],
        "slug": slug,
        "qty": serv["qty"],
        "effective": eff.get("effective"),
        "marked": eff.get("marked"),
        "article_id": article_id,
    }


# Bank whose No-Cost EMI offer also qualifies for an alert (in addition to non-EMI offers).
EMI_BANK = os.environ.get("RELIANCE_EMI_BANK", "KOTAK").upper()


def _bank_str(o: Dict[str, Any]) -> str:
    b = o.get("bank_codes") or o.get("bank_code") or o.get("bank_name") or ""
    if isinstance(b, list):
        b = ",".join(map(str, b))
    return str(b).upper()


def _is_emi_offer(o: Dict[str, Any]) -> bool:
    """An offer is EMI-based if its payment_method is EMI (or the desc says so)."""
    if str(o.get("payment_method") or "").upper() == "EMI":
        return True
    return "EMI" in str(o.get("offer_desc") or o.get("description") or "").upper()


def _is_nocost(o: Dict[str, Any]) -> bool:
    etype = str(o.get("emi_type") or "").upper()
    desc = str(o.get("offer_desc") or o.get("description") or "").upper()
    return "NO_COST" in etype or "NO COST" in desc or "NO-COST" in desc


def _offer_label(o: Dict[str, Any]) -> str:
    """Human label for an instant/bank offer."""
    desc = str(o.get("offer_desc") or o.get("description") or o.get("offer_code") or "Offer").strip()
    banks = o.get("bank_codes") or o.get("bank_code")
    if isinstance(banks, list):
        banks = ", ".join(banks)
    parts = [desc]
    if banks and str(banks) not in desc:
        parts.append(f"[{banks}]")
    code = o.get("offer_code")
    if code:
        parts.append(f"({code})")
    return " ".join(parts)


def fetch_offers(article_id: str, slug: str) -> Dict[str, List[str]]:
    """Return qualifying offers for a product, categorised.

    Fynd's promotions API segregates offers:
      - bank_offers / top_bank_offers / product_offers : instant offers (some EMI-linked)
      - emi_data / top_emi_offers                       : per-bank EMI tenure plans

    We return two buckets of human-readable labels:
      - "non_emi"      : instant offers that are NOT EMI-based
      - "kotak_nocost" : No-Cost EMI offers from EMI_BANK (default Kotak)
    """
    empty = {"non_emi": [], "kotak_nocost": []}
    if not article_id:
        return empty
    try:
        r = _get(PROMO_URL, params={"article-id": article_id, "slug": slug},
                 headers=HEADERS, timeout=30)
        r.raise_for_status()
        data = (r.json() or {}).get("data") or {}
    except (requests.RequestException, ValueError) as e:
        print(f"[{get_ist_now()}] offers fetch failed for {slug}: {e}")
        return empty

    non_emi: Dict[str, str] = {}
    kotak_nocost: Dict[str, str] = {}

    # 1) instant / bank offers
    for key in ("bank_offers", "top_bank_offers", "product_offers"):
        for o in (data.get(key) or []):
            if not isinstance(o, dict):
                continue
            code = str(o.get("offer_code") or o.get("offer_desc") or id(o))
            if _is_emi_offer(o):
                if EMI_BANK in _bank_str(o) and _is_nocost(o):
                    kotak_nocost.setdefault(code, _offer_label(o))
            else:
                non_emi.setdefault(code, _offer_label(o))

    # 2) EMI tenure plans (emi_data: card_name -> [plans]; top_emi_offers: dict or list)
    def _scan_plans(card: Any, plans: Any):
        card_u = str(card or "").upper()
        for p in (plans if isinstance(plans, list) else [plans]):
            if not isinstance(p, dict):
                continue
            if (EMI_BANK in card_u or EMI_BANK in _bank_str(p)) and _is_nocost(p):
                label = _offer_label(p) if p.get("offer_desc") else \
                    f"No Cost EMI on {card or _bank_str(p) or EMI_BANK}"
                kotak_nocost.setdefault(str(card or p.get("offer_code") or label), label)

    for card, plans in (data.get("emi_data") or {}).items():
        _scan_plans(card, plans)
    teo = data.get("top_emi_offers") or {}
    for card, plans in (teo.items() if isinstance(teo, dict) else [(None, teo)]):
        _scan_plans(card, plans)

    return {"non_emi": list(non_emi.values()), "kotak_nocost": list(kotak_nocost.values())}


def _stock_lines(matches: List[Dict[str, Any]]) -> List[str]:
    lines = []
    for m in matches:
        price = ""
        if m.get("effective"):
            price = f" -- Rs.{float(m['effective']):.0f}"
            if m.get("marked") and m["marked"] != m["effective"]:
                price += f" (MRP Rs.{float(m['marked']):.0f})"
        lines.append(f"- {m['name']}{price}  [qty {m['qty']}]")
        offers = m.get("offers") or {}
        for lbl in offers.get("non_emi", []):
            lines.append(f"    * non-EMI offer: {lbl}")
        for lbl in offers.get("kotak_nocost", []):
            lines.append(f"    * {EMI_BANK} No-Cost EMI: {lbl}")
    return lines


def send_alert(matches: List[Dict[str, Any]]) -> bool:
    subject = f"Reliance Digital: iPhone in stock + offer @ {PINCODE} -- {len(matches)} variant(s)"
    title = f"iPhone in stock + offer at Reliance Digital ({len(matches)})"
    body = "\n".join(
        [f"iPhone in stock at {PINCODE} WITH a non-EMI / {EMI_BANK} No-Cost EMI offer on Reliance Digital:", ""]
        + _stock_lines(matches)
        + ["", f"Shop: {SEARCH_URL}apple%20iphone"]
    )
    ntfy_ok = email_ok = False

    if NTFY_TOPIC:
        try:
            _SESSION.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=body.encode("utf-8"),
                headers={"Title": title, "Priority": "high", "Tags": "iphone,shopping",
                         "Click": f"{SEARCH_URL}apple%20iphone"},
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


def check_reliancedigital_iphone():
    print("=" * 60)
    print(f"Reliance Digital iPhone monitor -- {get_ist_now()}")
    print(f"Pincode: {PINCODE}   Models: {', '.join(MODELS)}")
    print("=" * 60)

    handsets = search_handsets()
    print(f"[{get_ist_now()}] {len(handsets)} handset(s) found in catalog")

    # Alert only when a phone is BOTH in stock at the pincode AND has a qualifying offer:
    # a non-EMI (instant) offer, OR a Kotak No-Cost EMI offer.
    matches: List[Dict[str, Any]] = []
    for h in handsets:
        info = check_pincode_stock(h)
        if not info:
            print(f"[{get_ist_now()}] {h['name']:45.45}  qty=0    out of stock")
            continue
        offers = fetch_offers(info.get("article_id", ""), info["slug"])
        info["offers"] = offers
        n_ne, n_kn = len(offers["non_emi"]), len(offers["kotak_nocost"])
        if n_ne or n_kn:
            flag = f"IN STOCK + {n_ne} non-EMI, {n_kn} {EMI_BANK} no-cost EMI"
        else:
            flag = "IN STOCK (no qualifying offer)"
        print(f"[{get_ist_now()}] {h['name']:45.45}  qty={info['qty']:<4} {flag}")
        if n_ne or n_kn:
            matches.append(info)

    if not matches:
        print(f"[{get_ist_now()}] No in-stock iPhone with a qualifying offer at {PINCODE}.")
        return False

    print(f"[{get_ist_now()}] STOCK + QUALIFYING OFFER: {[m['name'] for m in matches]}")
    if not should_send_alert("reliance_iphone"):
        return False

    ok = send_alert(matches)
    if ok:
        record_alert("reliance_iphone")
    return ok


if __name__ == "__main__":
    try:
        check_reliancedigital_iphone()
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
