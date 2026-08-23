#!/usr/bin/env python3
"""
Blinkit -- iPhone 15 / 16 / 17 handset stock monitor (hyperlocal).

Reuses Blinkit's public search API (same flow as check_blinkit_amazon.py):
  POST /v1/layout/search?q=<query>   with headers auth_key + lat + lon
Stock is hyperlocal -- each dark store has its own inventory. 560035 (SE Bengaluru)
is served by several nearby dark stores, so we probe a spread of coords (Bellandur,
HSR, Marathahalli, Sarjapur Rd, Kadubeesanahalli, Koramangala) and alert if ANY of
them has stock, naming the store. Override with BLINKIT_COORDS="lat,lon,label;...".
BLINKIT_LAT / BLINKIT_LON (if set) are added as an extra "home" point.

Only real iPhone 15/16/17 base handsets are matched -- Pro / Pro Max variants and
cases, cables, chargers, covers etc. are filtered out.
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
from typing import Any, Dict, List

try:
    from curl_cffi import requests as cffi_requests
    _SESSION = cffi_requests.Session(impersonate="chrome120")
    def _post(url, **kw): return _SESSION.post(url, **kw)
except ImportError:
    import requests as _requests
    _SESSION = _requests.Session()
    def _post(url, **kw): return _SESSION.post(url, **kw)

try:
    import requests  # for RequestException type only
except ImportError:
    print("Error: pip install -r requirements.txt")
    sys.exit(1)

import brightdata_browser

IST = timezone(timedelta(hours=5, minutes=30))
COOLDOWN_HOURS = float(os.environ.get(
    "BLINKIT_IPHONE_COOLDOWN_HOURS",
    os.environ.get("BLINKIT_COOLDOWN_HOURS", os.environ.get("ALERT_COOLDOWN_HOURS", 12))))
STATE_DIR = Path(".alert_state")
STATE_FILE = STATE_DIR / "last_alert.json"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# Blinkit stock is per dark store. 560035 (Bellandur/HSR, SE Bengaluru) is served by
# several nearby dark stores, so we probe a spread of coords and alert if ANY has stock.
# Override with BLINKIT_COORDS="lat,lon;lat,lon;..." (semicolon-separated).
_DEFAULT_COORDS = [
    ("12.9260", "77.6762", "Bellandur"),
    ("12.9166", "77.6470", "HSR Layout"),
    ("12.9560", "77.7010", "Marathahalli"),
    ("12.9010", "77.6870", "Sarjapur Rd"),
    ("12.9350", "77.6970", "Kadubeesanahalli"),
    ("12.9280", "77.6260", "Koramangala"),
]


def _coords():
    raw = os.environ.get("BLINKIT_COORDS", "").strip()
    coords = []
    if raw:
        for i, pair in enumerate(p for p in raw.split(";") if p.strip()):
            parts = [x.strip() for x in pair.split(",")]
            if len(parts) >= 2:
                label = parts[2] if len(parts) > 2 else f"store{i+1}"
                coords.append((parts[0], parts[1], label))
    # single-point override (kept for backwards-compat with existing secrets)
    lat, lon = os.environ.get("BLINKIT_LAT"), os.environ.get("BLINKIT_LON")
    if lat and lon:
        coords.append((str(lat), str(lon), "home"))
    return coords or _DEFAULT_COORDS


COORDS = _coords()

MODELS = [m.strip() for m in os.environ.get("BLINKIT_MODELS", "15,16,17").split(",") if m.strip()]

# Minimum minutes between actual Scraping Browser scrapes. The workflow fires every 5 min,
# but each browser scrape spends Bright Data credits, so throttle to conserve them.
# 0 = scrape on every trigger (only sensible in direct mode). Ignored in direct mode.
RUN_INTERVAL_MIN = float(os.environ.get("BLINKIT_RUN_INTERVAL_MIN", "0"))

# Public guest auth key -- stable for anonymous searches (same as check_blinkit_amazon.py).
_AUTH_KEY = os.environ.get(
    "BLINKIT_AUTH_KEY",
    "c761ec3633c22afad934fb17a66385c1c06c5472b4898b866b7306186d0bb477")

SEARCH_QUERIES = [f"iphone {m}" for m in MODELS]
PRODUCT_URL = "https://blinkit.com/s/?q=iphone"

BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-IN,en;q=0.9",
    "app_client": "consumer_web",
    "app_version": "1010101010",
    "rn_bundle_version": "1009003012",
    "web_app_version": "1008010016",
    "content-type": "application/json",
    "Origin": "https://blinkit.com",
    "Referer": PRODUCT_URL,
}

_ACCESSORY = re.compile(
    r"\b(case|cover|strap|glass|protector|charger|cable|adapter|screen|guard|"
    r"skin|holder|mount|stand|airpod|band|tempered|wallet|magsafe|battery|"
    r"power|pouch|sleeve|lens|film|dock|grip)\b", re.I)


def _is_handset(name: str) -> bool:
    """Exact base iPhone models only -- excludes Plus, Pro, Pro Max, Air, mini and 16e."""
    if not re.search(r"\bi[pP]hone\b", name, re.I):
        return False
    if _ACCESSORY.search(name):
        return False
    if not re.search(r"\d+\s*(GB|TB)\b", name, re.I):
        return False
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


def _recently_scraped() -> bool:
    """True if a browser scrape ran within RUN_INTERVAL_MIN -- skip to conserve credits."""
    if RUN_INTERVAL_MIN <= 0:
        return False
    try:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        last = state.get("blinkit_iphone_lastrun")
        if not last:
            return False
        last_time = datetime.fromisoformat(last)
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=IST)
        elapsed_min = (get_ist_now() - last_time).total_seconds() / 60
        if elapsed_min < RUN_INTERVAL_MIN:
            print(f"[{get_ist_now()}] Throttled: last scrape {elapsed_min:.0f}m ago "
                  f"(< {RUN_INTERVAL_MIN:.0f}m) -- skipping to save Scraping Browser credits")
            return True
        return False
    except Exception:
        return False


def _mark_scraped():
    STATE_DIR.mkdir(exist_ok=True)
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    state["blinkit_iphone_lastrun"] = get_ist_now().isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2))


_SEARCH_BODY = {
    "applied_filters": None, "monet_assets": [], "postback_meta": {},
    "previous_search_query": "", "processed_rails": {}, "similar_entities": None,
    "sort": "", "vertical_cards_processed": 0,
}


def _search_url(query: str) -> str:
    return (f"https://blinkit.com/v1/layout/search?q={query.replace(' ', '+')}"
            f"&search_type=type_to_search")


def _search_headers(query: str, lat: str, lon: str) -> Dict[str, str]:
    return {**BASE_HEADERS, "auth_key": _AUTH_KEY, "lat": lat, "lon": lon,
            "Referer": f"https://blinkit.com/s/?q={query.replace(' ', '+')}"}


def _snippets_from_json(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    return (payload or {}).get("response", {}).get("snippets", [])


def search_snippets(query: str, lat: str, lon: str) -> List[Dict[str, Any]]:
    """Direct (curl_cffi) search -- used when the Scraping Browser is not configured."""
    r = _post(_search_url(query), headers=_search_headers(query, lat, lon),
              json=_SEARCH_BODY, timeout=30)
    r.raise_for_status()
    return _snippets_from_json(r.json())


def collect_snippets(coords, queries) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """Return {store: {query: snippets}} for every (coord, query).

    When BRIGHTDATA_BROWSER_WSS is set, all searches run in ONE Bright Data Scraping
    Browser session (real Chrome on a residential IP) -- required because Blinkit's
    iPhone search returns 403 to bare API clients and to datacenter IPs, but 200 from a
    genuine browser session. Otherwise falls back to direct curl_cffi requests.
    """
    out: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    if brightdata_browser.is_configured():
        specs, index = [], []
        for lat, lon, store in coords:
            for q in queries:
                specs.append({"url": _search_url(q), "method": "POST",
                              "headers": _search_headers(q, lat, lon),
                              "body": json.dumps(_SEARCH_BODY)})
                index.append((store, q))
        resp = brightdata_browser.browser_fetch("https://blinkit.com/", specs) or []
        for (store, q), r in zip(index, resp):
            snippets: List[Dict[str, Any]] = []
            if r and r.get("status") == 200:
                try:
                    snippets = _snippets_from_json(json.loads(r.get("text") or "{}"))
                except ValueError:
                    pass
            else:
                print(f"[{get_ist_now()}] browser search '{q}' @ {store}: "
                      f"HTTP {r.get('status') if r else 'n/a'}")
            out.setdefault(store, {})[q] = snippets
    else:
        for lat, lon, store in coords:
            for q in queries:
                try:
                    out.setdefault(store, {})[q] = search_snippets(q, lat, lon)
                except requests.RequestException as e:
                    print(f"[{get_ist_now()}] search failed for '{q}' @ {store}: {e}")
                    out.setdefault(store, {})[q] = []
    return out


def parse_handsets(snippets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    results = []
    for snippet in snippets:
        data = snippet.get("data", {})
        name_obj = data.get("name") or data.get("display_name") or {}
        name = (name_obj.get("text") or "").strip()
        if not _is_handset(name):
            continue
        is_sold_out = data.get("is_sold_out", True)
        inventory = data.get("inventory", 0)
        mrp_text = (data.get("mrp") or {}).get("text", "")
        price_text = (data.get("normal_price") or {}).get("text", "")
        results.append({
            "name": name,
            "inventory": inventory,
            "is_sold_out": is_sold_out,
            "mrp_text": mrp_text,
            "price_text": price_text,
            "in_stock": not is_sold_out and inventory > 0,
        })
    return results


def _stock_lines(items: List[Dict[str, Any]]) -> List[str]:
    return [f"- {p['name']} @ {p['store']}: {p['price_text']} (MRP {p['mrp_text']}) | inventory={p['inventory']}"
            for p in items]


def send_alert(items: List[Dict[str, Any]]) -> bool:
    subject = f"Blinkit: iPhone in stock -- {len(items)} item(s)"
    title = f"iPhone in stock on Blinkit ({len(items)})"
    body = "\n".join(
        ["iPhone handset(s) in stock on Blinkit (10-min delivery):", ""]
        + _stock_lines(items) + ["", f"Order now: {PRODUCT_URL}"]
    )
    ntfy_ok = email_ok = False

    if NTFY_TOPIC:
        try:
            _post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=body.encode("utf-8"),
                headers={"Title": title, "Priority": "high", "Tags": "iphone,blinkit",
                         "Click": PRODUCT_URL},
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


def check_blinkit_iphone():
    use_browser = brightdata_browser.is_configured()
    print("=" * 60)
    print(f"Blinkit iPhone monitor -- {get_ist_now()}")
    print(f"Stores: {', '.join(c[2] for c in COORDS)}   Models: {', '.join(MODELS)}")
    print(f"Source: {'Bright Data Scraping Browser' if use_browser else 'direct'}")
    print("=" * 60)

    if use_browser and _recently_scraped():
        return False

    snippets_by_store = collect_snippets(COORDS, SEARCH_QUERIES)
    if use_browser:
        _mark_scraped()

    in_stock: List[Dict[str, Any]] = []
    in_stock_keys = set()
    any_catalog = False

    for lat, lon, store in COORDS:
        seen: Dict[str, Dict[str, Any]] = {}
        for q in SEARCH_QUERIES:
            for p in parse_handsets(snippets_by_store.get(store, {}).get(q, [])):
                seen.setdefault(p["name"], p)

        if not seen:
            print(f"[{get_ist_now()}] {store:16.16} — no iPhone handsets in catalog")
            continue
        any_catalog = True
        for p in seen.values():
            p["store"] = store
            status = "IN STOCK" if p["in_stock"] else "out of stock"
            print(f"[{get_ist_now()}] {store:16.16} {p['name']:34.34}  {status:12s}  "
                  f"{p['price_text']}  inv={p['inventory']}")
            key = (p["name"], store)
            if p["in_stock"] and key not in in_stock_keys:
                in_stock_keys.add(key)
                in_stock.append(p)

    if not any_catalog:
        print(f"[{get_ist_now()}] No iPhone handsets in the catalog at any nearby store.")
        return False

    if not in_stock:
        print(f"[{get_ist_now()}] No iPhone handsets in stock at any nearby store.")
        return False

    print(f"[{get_ist_now()}] IN STOCK: {[p['name'] for p in in_stock]}")
    if not should_send_alert("blinkit_iphone"):
        return False

    ok = send_alert(in_stock)
    if ok:
        record_alert("blinkit_iphone")
    return ok


if __name__ == "__main__":
    try:
        check_blinkit_iphone()
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
