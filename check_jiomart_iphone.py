#!/usr/bin/env python3
"""
JioMart iPhone discount monitor — iPhone 15, 16, 17 series.

Alerts when ANY in-stock iPhone 15/16/17 has:
  (a) base price discount >= MIN_DISCOUNT_RS (effective < marked), OR
  (b) an active non-EMI Electronics bank card offer >= MIN_DISCOUNT_RS

Stock check is pincode-accurate:
  1. Logistics API → pincode coordinates
  2. Delivery-promise → correct store IDs for that pincode
  3. Sizes API per slug → authoritative sellable/price for those stores

Bank offers from /api/service/application/content/v2.0/pages?tags=bankoffers,bankoffer
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
from typing import Any, Dict, List, Optional, Tuple

from iphone_models import models_summary

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

_raw = os.environ.get("JIOMART_PINCODES", "560035")
TARGET_PINCODES = [p.strip() for p in _raw.split(",") if p.strip()]

# Optional: override lat/lon for a single pincode when the centroid falls in the wrong QC zone.
# Get your exact coords from Google Maps (long-press your location).
# Only used when monitoring a single pincode.
JIOMART_LAT = os.environ.get("JIOMART_LAT", "")
JIOMART_LON = os.environ.get("JIOMART_LON", "")

BEARER_TOKEN = "Bearer Njg1OTQ1ZjQ2YzhjN2FlZTNmM2FmNjA1OlRwS3c3d0Q5aA=="

SEARCH_URL = "https://www.jiomart.com/ext/vertex/application/api/v1.0/products"
SIZES_PRICE_URL = "https://www.jiomart.com/api/service/application/catalog/v1.0/products/sizes/price"
LOGISTICS_URL = "https://www.jiomart.com/api/service/application/logistics/v1.0/pincode/{pincode}"
DELIVERY_PROMISE_URL = "https://www.jiomart.com/api/service/application/logistics/v1.0/delivery-promise"
BANKOFFERS_URL = "https://www.jiomart.com/api/service/application/content/v2.0/pages"

SEARCH_QUERIES = [
    ("iPhone 15", "apple iphone 15"),
    ("iPhone 16", "apple iphone 16"),
    ("iPhone 17", "apple iphone 17"),
]

JIOMART_BASE = "https://www.jiomart.com/product"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"

# At Bangalore (~13°N): 1° lat ≈ 111 km, 1° lon ≈ 108.2 km
_LAT_KM = 111.0
_LON_KM = 108.2

# Grid at ~110m steps, max 500m — sorted by distance so we always try nearest first
_NEARBY_OFFSETS = sorted(
    [(dl, dn)
     for dl in [-0.004, -0.003, -0.002, -0.001, 0.0, 0.001, 0.002, 0.003, 0.004]
     for dn in [-0.004, -0.003, -0.002, -0.001, 0.0, 0.001, 0.002, 0.003, 0.004]
     if dl != 0.0 or dn != 0.0],
    key=lambda x: (x[0] * _LAT_KM) ** 2 + (x[1] * _LON_KM) ** 2,
)

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


def _read_state() -> Dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def _write_state(state: Dict):
    STATE_DIR.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def get_nearby_state(pincode: str) -> Optional[str]:
    return _read_state().get(f"nearby_addr_{pincode}")


def set_nearby_state(pincode: str, address: Optional[str]):
    state = _read_state()
    key = f"nearby_addr_{pincode}"
    if address is None:
        state.pop(key, None)
    else:
        state[key] = address
    _write_state(state)


def _base_headers() -> Dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "authorization": BEARER_TOKEN,
        "x-currency-code": "INR",
        "x-fp-sdk-version": "1.10.3-60",
        "Origin": "https://www.jiomart.com",
        "Referer": "https://www.jiomart.com/",
    }


def make_headers(pincode: str, lat: str, lon: str, polygon_ids: List[str]) -> Dict[str, str]:
    h = _base_headers()
    h["x-location-detail"] = json.dumps({
        "country": "INDIA",
        "country_iso_code": "IN",
        "city": "BENGALURU",
        "pincode": pincode,
        "state": "KARNATAKA",
    })
    geo: Dict[str, Any] = {"latitude": lat, "longitude": lon}
    if polygon_ids:
        geo["polygon_ids"] = polygon_ids
    h["x-geolocation"] = json.dumps(geo)
    return h


def resolve_pincode(pincode: str) -> Optional[Dict[str, Any]]:
    """
    Returns {lat, lon, store_ids, polygon_ids} for the given pincode.
    If JIOMART_LAT/JIOMART_LON are set (and only one pincode is monitored),
    uses those exact coords instead of the pincode centroid — important because
    a single pincode can span multiple QC zones with different stock levels.
    """
    # Use exact coords if provided (avoids centroid landing in wrong QC zone)
    if JIOMART_LAT and JIOMART_LON and len(TARGET_PINCODES) == 1:
        lat, lon = JIOMART_LAT, JIOMART_LON
        print(f"  [resolve] using provided coords: {lat}, {lon}")
    else:
        try:
            h = _base_headers()
            h["x-location-detail"] = json.dumps({"country": "INDIA", "country_iso_code": "IN",
                                                   "city": "BENGALURU", "pincode": pincode, "state": "KARNATAKA"})
            r = requests.get(LOGISTICS_URL.format(pincode=pincode), headers=h, timeout=15)
            r.raise_for_status()
            coords = r.json()["data"][0]["lat_long"]["coordinates"]  # GeoJSON: [lon, lat]
            lat, lon = str(coords[1]), str(coords[0])
        except Exception as e:
            print(f"  [resolve] logistics API failed for {pincode}: {e}")
            return None

    try:
        h2 = make_headers(pincode, lat, lon, [])
        r2 = requests.get(DELIVERY_PROMISE_URL, headers=h2, timeout=15)
        r2.raise_for_status()
        items = r2.json().get("items", [])
        store_ids = [str(s["uid"]) for s in items]
        polygon_ids = []
        for s in items:
            for j in s.get("journey_wise_promise", []):
                pid = (j.get("meta") or {}).get("polygon_id")
                if pid:
                    polygon_ids.append(pid)
        return {"lat": lat, "lon": lon, "store_ids": store_ids, "polygon_ids": polygon_ids}
    except Exception as e:
        print(f"  [resolve] delivery-promise failed for {pincode}: {e}")
        return None


def discover_iphone_slugs(label: str, query: str, pincode_info: Dict) -> List[Dict[str, Any]]:
    """
    Vertex search to discover iPhone model slugs with price data.
    Stock is verified separately via the sizes API.
    """
    store_filter = "journey:quickcommerce:::store_ids:" + "||".join(pincode_info["store_ids"])
    try:
        r = requests.get(
            SEARCH_URL,
            params={"f": store_filter, "page_size": "30", "q": query},
            headers=make_headers(
                pincode_info["pincode"], pincode_info["lat"],
                pincode_info["lon"], pincode_info["polygon_ids"]
            ),
            timeout=20,
        )
        r.raise_for_status()
        items = r.json().get("items") or []
    except Exception as e:
        print(f"  [{label}] Search failed: {e}")
        return []

    results = []
    for item in items:
        name = item.get("name", "")
        if not any(gen in name for gen in ["iPhone 15", "iPhone 16", "iPhone 17"]):
            continue
        if any(w in name.lower() for w in ["adapter", "cable", "case", "cover", "charger", "airpod", "watch",
                                            "screen", "protector", "panzerglass"]):
            continue
        slug = item.get("slug", "")
        if not slug:
            continue
        price = item.get("price") or {}
        effective = (price.get("effective") or {}).get("min", 0)
        marked = (price.get("marked") or {}).get("min", 0)
        results.append({"name": name, "slug": slug, "effective": effective, "marked": marked})
    return results


def check_slug_stock(slug: str, name: str, effective: int, marked: int, pincode_info: Dict) -> Optional[Dict[str, Any]]:
    """
    sizes/price POST — same API the product page uses for per-pincode availability.
    Returns item dict with qty if serviceable, else None.
    """
    try:
        r = requests.post(
            SIZES_PRICE_URL,
            json={"items": [{"slug": slug, "size": "OS"}]},
            headers=make_headers(
                pincode_info["pincode"], pincode_info["lat"],
                pincode_info["lon"], pincode_info["polygon_ids"]
            ),
            timeout=15,
        )
        r.raise_for_status()
        items = r.json().get("items") or []
    except Exception as e:
        print(f"  [{name}] sizes/price failed: {e}")
        return None

    if not items:
        print(f"  [{name}]: OOS (no serviceable article)")
        return None

    item = items[0]
    serviceable = item.get("is_serviceable", False)
    qty = item.get("quantity", 0)
    price_data = item.get("price") or {}
    api_effective = int(price_data.get("effective") or effective or 0)
    api_marked = int(price_data.get("marked") or marked or 0)
    store_name = (item.get("store") or {}).get("name", "")

    eff = api_effective or effective
    mkd = api_marked or marked
    base_discount = max(0, mkd - eff) if mkd > 0 and eff > 0 else 0
    url = f"{JIOMART_BASE}/{slug}"
    stock_str = f"IN STOCK (qty={qty})" if serviceable else "OOS"

    if eff:
        price_str = f"₹{eff:,} (was ₹{mkd:,}, -₹{base_discount:,})" if base_discount > 0 else f"₹{eff:,}"
    else:
        price_str = ""

    print(f"  [{name}]: {price_str}  [{stock_str}]  store={store_name}")

    if not serviceable:
        return None
    return {
        "name": name,
        "effective": eff,
        "marked": mkd,
        "base_discount": base_discount,
        "qty": qty,
        "store": store_name,
        "url": url,
    }


def _parse_rs_amount(text: str) -> int:
    amounts = [int(m.replace(",", "")) for m in _RS_RE.findall(text)]
    return max(amounts) if amounts else 0


def _is_emi_offer(title: str, description: str) -> bool:
    combined = (title + " " + description).lower()
    if "non-emi" in combined or "non emi" in combined:
        return False
    return "emi" in combined


def _is_electronics_offer(tags: List[str]) -> bool:
    lower_tags = [t.lower() for t in tags]
    category_tags = [t for t in lower_tags if t not in ("bankoffer", "bankoffers")]
    if not category_tags:
        return True
    return "electronics" in lower_tags


def check_bank_offers() -> Optional[Dict[str, Any]]:
    """Returns the best active non-EMI Electronics bank offer >= MIN_DISCOUNT_RS, or None."""
    try:
        r = requests.get(
            BANKOFFERS_URL,
            params={"page_size": "50", "tags": "bankoffers,bankoffer"},
            headers=_base_headers(),
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
            try:
                start_dt = datetime.fromisoformat(s.get("start", "").replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(s.get("end", "").replace("Z", "+00:00"))
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


def _reverse_geocode(lat: float, lon: float) -> str:
    try:
        r = requests.get(
            NOMINATIM_URL,
            params={"lat": lat, "lon": lon, "format": "json"},
            headers={"User-Agent": "jiomart-iphone-monitor/1.0"},
            timeout=10,
        )
        r.raise_for_status()
        addr = r.json().get("address", {})
        parts = [p for p in [
            addr.get("road") or addr.get("neighbourhood") or addr.get("hamlet"),
            addr.get("suburb") or addr.get("locality") or addr.get("village"),
            addr.get("postcode"),
        ] if p]
        return ", ".join(parts) if parts else f"{lat:.5f},{lon:.5f}"
    except Exception:
        return f"{lat:.5f},{lon:.5f}"


def find_nearest_stock(
    user_lat: str,
    user_lon: str,
    user_polygon_ids: List[str],
    pincode: str,
) -> Optional[Dict]:
    """
    Scan a 500m grid around user_lat/user_lon to find the nearest zone where
    any iPhone is in stock. Discovers slugs fresh at each new zone (vertex search
    is zone-sensitive — it only returns items stocked at that store).
    Returns hit info or None.
    """
    base_lat, base_lon = float(user_lat), float(user_lon)
    MAX_KM = 0.5
    seen_polygons: set = set(user_polygon_ids)

    print(f"  [nearby] Scanning ≤{int(MAX_KM * 1000)}m grid for nearest in-stock zone...")

    for d_lat, d_lon in _NEARBY_OFFSETS:
        dist_km = ((d_lat * _LAT_KM) ** 2 + (d_lon * _LON_KM) ** 2) ** 0.5
        if dist_km > MAX_KM:
            break  # sorted — everything after is farther

        n_lat = str(round(base_lat + d_lat, 7))
        n_lon = str(round(base_lon + d_lon, 7))

        try:
            r = requests.get(
                DELIVERY_PROMISE_URL,
                headers=make_headers(pincode, n_lat, n_lon, []),
                timeout=10,
            )
            r.raise_for_status()
            dp_items = r.json().get("items", [])
        except Exception:
            continue

        polygon_ids: List[str] = []
        for s in dp_items:
            for j in s.get("journey_wise_promise", []):
                pid = (j.get("meta") or {}).get("polygon_id")
                if pid:
                    polygon_ids.append(pid)

        if polygon_ids and set(polygon_ids).issubset(seen_polygons):
            continue  # same zone as user's — skip
        seen_polygons.update(polygon_ids)

        store_ids = [str(s["uid"]) for s in dp_items]
        store_name = dp_items[0].get("code", "") if dp_items else ""

        nearby_info = {
            "lat": n_lat, "lon": n_lon,
            "store_ids": store_ids, "polygon_ids": polygon_ids,
            "pincode": pincode,
        }

        # Discover iPhone slugs at this new zone (vertex search is store-specific)
        zone_slugs: Dict[str, Dict] = {}
        for label, query in SEARCH_QUERIES:
            for item in discover_iphone_slugs(label, query, nearby_info):
                if item["slug"] not in zone_slugs:
                    zone_slugs[item["slug"]] = item

        if not zone_slugs:
            continue

        # Check each slug for availability at this zone
        for slug, item in zone_slugs.items():
            result = check_slug_stock(slug, item["name"], item["effective"], item["marked"], nearby_info)
            if result:
                address = _reverse_geocode(float(n_lat), float(n_lon))
                print(f"  [nearby] IN STOCK at {dist_km:.2f}km: {result['name']} qty={result['qty']}  {address}")
                return {
                    "dist_km": round(dist_km, 2),
                    "lat": n_lat,
                    "lon": n_lon,
                    "store": store_name,
                    "qty": result["qty"],
                    "address": address,
                    "name": result["name"],
                    "url": result["url"],
                }

    print("  [nearby] No stock found within 500m.")
    return None


def send_ntfy_alert(pincode: str, qualifying: List[Dict], bank_offer: Optional[Dict], nearest: Optional[Dict] = None) -> bool:
    if not NTFY_TOPIC:
        print(f"[{get_ist_now()}] ntfy not configured")
        return False
    try:
        if qualifying:
            lines = [f"JioMart iPhone deal — pincode {pincode}!\n"]
        else:
            lines = [f"JioMart bank offer active — your zone OOS but nearby stock found!\n"]
        if bank_offer:
            lines.append(f"Bank offer: {bank_offer['title']} (₹{bank_offer['amount']:,} off, non-EMI)")
            lines.append("")
        for m in qualifying:
            qty_str = f"  qty={m['qty']}" if m.get("qty") else ""
            if m["base_discount"] > 0:
                lines.append(f"• {m['name']}: ₹{m['effective']:,} (was ₹{m['marked']:,}, -₹{m['base_discount']:,}){qty_str}")
            else:
                lines.append(f"• {m['name']}: ₹{m['effective']:,} (in stock{qty_str})")
            lines.append(f"  {m['url']}")
        if nearest:
            lines.append(f"\n⚡ Nearest stock: ~{nearest['dist_km']:.2f}km away")
            lines.append(f"  {nearest['name']}  qty={nearest['qty']}")
            lines.append(f"  Address: {nearest['address']}")
            lines.append(f"  Set this as your JioMart delivery address, then order:")
            lines.append(f"  {nearest['url']}")
        message = "\n".join(lines)
        best_base = max((m["base_discount"] for m in qualifying), default=0)
        best_amount = max(best_base, bank_offer["amount"] if bank_offer else 0)
        best_item = max(qualifying, key=lambda x: x["base_discount"]) if qualifying else None
        click_url = (best_item["url"] if best_item else nearest["url"]) if nearest or best_item else ""
        if qualifying:
            title = f"JioMart iPhone deal: up to Rs.{best_amount:,} off"
        else:
            title = f"JioMart iPhone: nearby stock {nearest['dist_km']:.2f}km away - bank offer active"
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "urgent",
                "Tags": "iphone,shopping,rotating_light",
                "Click": click_url,
            },
            timeout=15,
        ).raise_for_status()
        print(f"[{get_ist_now()}] ntfy sent for {pincode}")
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] ntfy failed: {e}")
        return False


def send_email_alert(pincode: str, qualifying: List[Dict], bank_offer: Optional[Dict], nearest: Optional[Dict] = None) -> bool:
    sender = os.environ.get("SENDER_EMAIL")
    receiver = os.environ.get("RECEIVER_EMAIL")
    password = os.environ.get("EMAIL_PASSWORD")
    if not all([sender, receiver, password]):
        print(f"[{get_ist_now()}] Email not configured")
        return False
    try:
        ist_time = get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST")
        if qualifying:
            lines = [f"JioMart iPhone deal — pincode {pincode}\n"]
        else:
            lines = [f"JioMart bank offer active — your zone OOS, nearby stock found!\n"]
        if bank_offer:
            lines.append(f"Bank offer: {bank_offer['title']}")
            lines.append(f"  {bank_offer['description']}\n")
        for m in qualifying:
            qty_str = f"  qty={m['qty']}" if m.get("qty") else ""
            if m["base_discount"] > 0:
                lines.append(f"  • {m['name']}: ₹{m['effective']:,} (was ₹{m['marked']:,}, -₹{m['base_discount']:,}){qty_str}")
            else:
                lines.append(f"  • {m['name']}: ₹{m['effective']:,} (in stock{qty_str})")
            lines.append(f"    {m['url']}")
        if nearest:
            lines.append(f"\nNearest in-stock zone: ~{nearest['dist_km']:.2f}km away")
            lines.append(f"  {nearest['name']}  qty={nearest['qty']}")
            lines.append(f"  Address: {nearest['address']}")
            lines.append(f"  Set this as your JioMart delivery address, then order:")
            lines.append(f"  {nearest['url']}")
        lines.append(f"\nChecked at: {ist_time}")
        text_body = "\n".join(lines)
        best_base = max((m["base_discount"] for m in qualifying), default=0)
        best_amount = max(best_base, bank_offer["amount"] if bank_offer else 0)
        msg = MIMEMultipart("alternative")
        if qualifying:
            models = models_summary(m["name"] for m in qualifying)
            subject = f"JioMart {models} deal: up to ₹{best_amount:,} off — {pincode}"
        else:
            models = models_summary([nearest["name"]]) if nearest else "iPhone"
            subject = f"JioMart {models}: nearby stock {nearest['dist_km']:.2f}km — bank offer ₹{best_amount:,} off"
        msg["Subject"] = subject
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


def _maybe_send_nearby_alert(
    pincode: str,
    bank_offer: Dict,
    nearest: Optional[Dict],
    any_alerted: bool,
) -> bool:
    """
    Edge-trigger: only alert when the nearby state changes.
    Silently update state when nearby goes OOS (so the next appearance triggers again).
    """
    last_addr = get_nearby_state(pincode)
    new_addr = nearest["address"] if nearest else None

    if new_addr == last_addr:
        if new_addr:
            print(f"  [nearby] Same location as before ({new_addr}), skipping.")
        # No change — don't alert, don't update state
        return any_alerted

    # State changed — update regardless of whether we alert
    set_nearby_state(pincode, new_addr)

    if not nearest:
        print(f"  [nearby] Nearby stock gone (was {last_addr}).")
        return any_alerted  # stock disappeared — no alert needed

    # New nearby location found — alert once
    print(f"  [nearby] New nearby stock at {nearest['address']} (was {last_addr or 'none'}) — alerting")
    ntfy_ok = send_ntfy_alert(pincode, [], bank_offer, nearest=nearest)
    email_ok = send_email_alert(pincode, [], bank_offer, nearest=nearest)
    if ntfy_ok or email_ok:
        any_alerted = True
    return any_alerted


def check_jiomart_iphone():
    print("=" * 60)
    print(f"JioMart iPhone monitor — {get_ist_now()}")
    print(f"Models: iPhone 15, 16, 17  |  Pincodes: {', '.join(TARGET_PINCODES)}")
    print(f"Min discount: ₹{MIN_DISCOUNT_RS:,}")
    print("=" * 60)

    print("\n[Bank Offers]")
    bank_offer = check_bank_offers()
    if bank_offer:
        print(f"  -> Qualifying offer: ₹{bank_offer['amount']:,} off (non-EMI, Electronics)")
    else:
        print(f"  -> No active non-EMI Electronics offer >= ₹{MIN_DISCOUNT_RS:,}")

    any_alerted = False

    for pincode in TARGET_PINCODES:
        print(f"\n{'─' * 40}")
        print(f"Pincode: {pincode}")
        print("─" * 40)

        pincode_info = resolve_pincode(pincode)
        if not pincode_info:
            print(f"  Could not resolve stores for {pincode}, skipping.")
            continue
        pincode_info["pincode"] = pincode
        print(f"  Stores: {pincode_info['store_ids']}")

        # Discover slugs via vertex search (includes price data)
        all_slugs: Dict[str, Dict] = {}  # slug → {name, effective, marked}
        for label, query in SEARCH_QUERIES:
            for item in discover_iphone_slugs(label, query, pincode_info):
                if item["slug"] not in all_slugs:
                    all_slugs[item["slug"]] = item

        if not all_slugs:
            print(f"  No iPhone slugs found for {pincode}.")
            if bank_offer and JIOMART_LAT and JIOMART_LON:
                nearest = find_nearest_stock(JIOMART_LAT, JIOMART_LON, pincode_info["polygon_ids"], pincode)
                any_alerted = _maybe_send_nearby_alert(pincode, bank_offer, nearest, any_alerted)
            else:
                set_nearby_state(pincode, None)
            continue

        # Verify each slug with sizes API (pincode-accurate)
        in_stock: List[Dict[str, Any]] = []
        for slug, item in all_slugs.items():
            result = check_slug_stock(slug, item["name"], item["effective"], item["marked"], pincode_info)
            if result:
                in_stock.append(result)

        if not in_stock:
            print(f"  No iPhones in stock for {pincode}.")
            if bank_offer and JIOMART_LAT and JIOMART_LON:
                nearest = find_nearest_stock(JIOMART_LAT, JIOMART_LON, pincode_info["polygon_ids"], pincode)
                any_alerted = _maybe_send_nearby_alert(pincode, bank_offer, nearest, any_alerted)
            else:
                set_nearby_state(pincode, None)
            continue

        # Determine which qualify for alerting
        base_qualifying = [m for m in in_stock if m["base_discount"] >= MIN_DISCOUNT_RS]
        bank_qualifying = in_stock if bank_offer else []
        qualifying_urls = {m["url"] for m in base_qualifying} | {m["url"] for m in bank_qualifying}
        qualifying = [m for m in in_stock if m["url"] in qualifying_urls]

        if not qualifying:
            print(f"  {len(in_stock)} in stock but no qualifying discount for {pincode}.")
            continue

        print(f"\n  {len(qualifying)} qualifying iPhone(s) for {pincode}!")
        if base_qualifying:
            print(f"  Base discount >={MIN_DISCOUNT_RS:,}: {[m['name'] for m in base_qualifying]}")
        if bank_offer and bank_qualifying:
            print(f"  Bank offer: ₹{bank_offer['amount']:,} off on {len(bank_qualifying)} in-stock item(s)")

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
