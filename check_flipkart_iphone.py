#!/usr/bin/env python3
"""
Flipkart -- iPhone 15 / 16 / 17 base handset (+ iPhone 18 Pro) STOCK monitor (per pincode).

Flipkart's SEARCH page carries a per-card `displayState` (IN_STOCK / COMING_SOON), but that
is NOT a reliable buyable signal -- a search "IN_STOCK" iPhone 17 is actually "Notify Me" /
out of stock and can't be bought. So, like the Amazon monitor: discover candidates from
search, then read the REAL, pincode-specific availability from Flipkart's own page API.

No Bright Data, no browser, no Jina -- plain curl_cffi:
  1. Discovery: GET /search?q=apple+iphone+<m> -> product paths (/<slug>/p/itm...?pid=...).
     The slug ("apple-iphone-17-mist-blue-256-gb") carries model+storage, so is_base_handset
     / is_pro_handset filter it to watched base handsets + the 6.3" iPhone 18 Pro (no Max).
  2. Stock: POST https://rome.api.flipkart.com/api/4/page/fetch with
     {"pageUri": <path>, "locationContext": {"pincode": <pin>}} for each watched pincode.
     The response's pageData.pageContext.fdpEventTracking.events.psi.pls.isAvailable is the
     authoritative, pincode-specific buyable flag (title from seoData.schema, price from
     psi.ppd.finalPrice). A watched iPhone alerts when isAvailable is true at ANY pincode.
     (API shape borrowed from the chetankaul/flipkart-stock-alert project.)
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

from iphone_models import is_base_handset, is_pro_handset, models_summary

# Two isolated sessions: the search GET sets Flipkart's www SN/session cookies, and those
# cookies make the rome page API reject the POST ("invalid session"). So the API POSTs use a
# separate session that never touches www.flipkart.com and therefore stays cookie-clean.
try:
    from curl_cffi import requests as cffi_requests
    _SEARCH_SESSION = cffi_requests.Session(impersonate="chrome120")
    _API_SESSION = cffi_requests.Session(impersonate="chrome120")
    def _get(url, **kw): return _SEARCH_SESSION.get(url, **kw)
    def _post(url, **kw): return _API_SESSION.post(url, **kw)
except ImportError:
    import requests as _requests
    _SEARCH_SESSION = _requests.Session()
    _API_SESSION = _requests.Session()
    def _get(url, **kw): return _SEARCH_SESSION.get(url, **kw)
    def _post(url, **kw): return _API_SESSION.post(url, **kw)

try:
    import requests  # for RequestException type only
except ImportError:
    print("Error: pip install -r requirements.txt")
    sys.exit(1)

IST = timezone(timedelta(hours=5, minutes=30))
COOLDOWN_HOURS = float(os.environ.get(
    "FLIPKART_IPHONE_COOLDOWN_HOURS", os.environ.get("ALERT_COOLDOWN_HOURS", 12)))
STATE_DIR = Path(".alert_state")
STATE_FILE = STATE_DIR / "last_alert.json"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# Base iPhone number-series to watch; sub-variants (17 Pro / 16 Plus / 17e / Air) are
# excluded by is_base_handset. Pro models to ALSO watch (the 6.3" Pro, not Pro Max).
MODELS = [m.strip() for m in os.environ.get("FLIPKART_MODELS", "15,16,17").split(",") if m.strip()]
PRO_MODELS = [m.strip() for m in os.environ.get("FLIPKART_PRO_MODELS", "18").split(",") if m.strip()]

# Delivery pincodes to check -- a product alerts if it is available at ANY of them, and the
# alert names which. Availability + price are pincode-specific in the Flipkart page API.
PINCODES = [p.strip() for p in os.environ.get(
    "FLIPKART_PINCODES", "560035,560048,560103,201019,201010").split(",") if p.strip()]

# Cap products checked per run (each product is one API call per pincode).
MAX_PRODUCTS = int(os.environ.get("FLIPKART_MAX", 15))


# Per-model storage allow-list (GB): only alert on these storages for the given model; a
# model not listed alerts on any storage. Empty default = any storage. Same format as the
# Amazon monitor: "17:256,16:128,18:256" (model:gb, comma/semicolon-separated).
def _parse_storage_cfg(raw: str) -> Dict[str, set]:
    cfg: Dict[str, set] = {}
    for part in re.split(r"[;,]", raw or ""):
        part = part.strip()
        if ":" not in part:
            continue
        model, gb = (p.strip() for p in part.split(":", 1))
        if model and gb.isdigit():
            cfg.setdefault(model, set()).add(int(gb))
    return cfg


STORAGE_ALLOWED = _parse_storage_cfg(os.environ.get("FLIPKART_STORAGE", ""))

BASE = "https://www.flipkart.com"
SEARCH_URL = BASE + "/search"
ROME_API = "https://rome.api.flipkart.com/api/4/page/fetch?cacheFirst=false"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
SEARCH_HEADERS = {"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9",
                  "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
# The FKUA-suffixed X-User-Agent is what the rome page API validates the request by.
API_HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": UA,
    "X-User-Agent": UA + " FKUA/website/42/website/Desktop",
    "Accept": "*/*",
    "Origin": BASE,
    "Referer": BASE + "/",
    "Accept-Language": "en-US,en;q=0.9",
}

# Product links in a search page: /<slug>/p/<itm...>?pid=<PID>
_PRODUCT_LINK_RE = re.compile(r'"(?:baseUrl|url)":"(/[a-z0-9-]+/p/itm[0-9a-f]+\?pid=[A-Z0-9]+)"')


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


def _name_from_slug(slug: str) -> str:
    """'apple-iphone-17-mist-blue-256-gb' -> 'apple iphone 17 mist blue 256 gb' -- enough for
    is_base_handset / is_pro_handset to gate discovery before any API call."""
    return slug.replace("-", " ")


def _is_watched(name: str) -> bool:
    return is_base_handset(name, MODELS) or (bool(PRO_MODELS) and is_pro_handset(name, PRO_MODELS))


def _storage_gb(name: str) -> Optional[int]:
    m = re.search(r"(\d+)\s*(GB|TB)\b", name or "", re.I)
    if not m:
        return None
    return int(m.group(1)) * (1024 if m.group(2).lower() == "tb" else 1)


def _storage_ok(name: str) -> bool:
    """False only when the title's model has a storage allow-list and its storage isn't in it."""
    mm = re.search(r"i[pP]hone\s*(\d{1,2})", name or "")
    allowed = STORAGE_ALLOWED.get(mm.group(1)) if mm else None
    if not allowed:
        return True
    return _storage_gb(name) in allowed


def _search_queries() -> List[str]:
    return [f"apple iphone {m}" for m in MODELS] + [f"apple iphone {m} pro" for m in PRO_MODELS]


def discover_products() -> List[Dict[str, str]]:
    """Discover watched iPhone products from Flipkart search. Returns [{pid, page_uri, name}]."""
    found: Dict[str, Dict[str, str]] = {}  # pid -> record
    for q in _search_queries():
        try:
            r = _get(SEARCH_URL, params={"q": q}, headers=SEARCH_HEADERS, timeout=30)
            if r.status_code != 200:
                print(f"[{get_ist_now()}] search {q!r} HTTP {r.status_code}")
                continue
            html = r.text
        except (requests.RequestException, ValueError) as e:
            print(f"[{get_ist_now()}] search {q!r} failed: {e}")
            continue
        for raw in _PRODUCT_LINK_RE.findall(html):
            path = raw.replace("\\u0026", "&").replace("\\/", "/")
            mslug = re.match(r"/([a-z0-9-]+)/p/", path)
            mpid = re.search(r"pid=([A-Z0-9]+)", path)
            if not (mslug and mpid) or mpid.group(1) in found:
                continue
            name = _name_from_slug(mslug.group(1))
            if not _is_watched(name):
                continue
            found[mpid.group(1)] = {"pid": mpid.group(1), "page_uri": path, "name": name}
    return list(found.values())


def _availability(page_uri: str, pincode: str) -> Optional[Dict[str, Any]]:
    """Pincode-specific availability via the Flipkart page API. Returns {available, title,
    price, mrp} or None on error."""
    payload = {"pageUri": page_uri,
               "pageContext": {"pageNumber": 1, "fetchSeoData": True},
               "locationContext": {"pincode": pincode}}
    try:
        r = _post(ROME_API, data=json.dumps(payload), headers=API_HEADERS, timeout=20)
        if r.status_code != 200:
            return None
        data = r.json()
    except (requests.RequestException, ValueError):
        return None
    pd = (data.get("RESPONSE") or {}).get("pageData") or {}
    psi = ((((pd.get("pageContext") or {}).get("fdpEventTracking") or {})
            .get("events") or {}).get("psi") or {})
    schema = (pd.get("seoData") or {}).get("schema") or []
    title = (schema[0].get("name") if schema else "") or ""
    ppd = psi.get("ppd") or {}
    return {
        "available": bool((psi.get("pls") or {}).get("isAvailable")),
        "title": title.strip(),
        "price": ppd.get("finalPrice"),
        "mrp": ppd.get("mrp"),
    }


def check_stock(product: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """Check the product across all watched pincodes. Returns it with in_stock /
    available_pincodes / title / price set."""
    avail_pins: List[str] = []
    title = product["name"]
    price = mrp = None
    for pin in PINCODES:
        info = _availability(product["page_uri"], pin)
        if not info:
            continue
        title = info["title"] or title
        price = info["price"] if info["price"] is not None else price
        mrp = info["mrp"] if info["mrp"] is not None else mrp
        if info["available"]:
            avail_pins.append(pin)
    return {
        **product,
        "title": title,
        "price": price,
        "mrp": mrp,
        "available_pincodes": avail_pins,
        "in_stock": bool(avail_pins),
    }


def _stock_lines(items: List[Dict[str, Any]]) -> List[str]:
    lines = []
    for p in items:
        price = f"Rs.{int(p['price'])}" if p.get("price") else ""
        if p.get("mrp") and p.get("price") and int(p["mrp"]) != int(p["price"]):
            price += f" (MRP Rs.{int(p['mrp'])})"
        pins = "/".join(p["available_pincodes"])
        lines.append(f"- {p['title']}: {price}  @ {pins}".rstrip())
        lines.append(f"    {BASE}{p['page_uri']}")
    return lines


def send_alert(items: List[Dict[str, Any]]) -> bool:
    models = models_summary(p["title"] for p in items)
    subject = f"Flipkart: {models} in stock -- {len(items)} item(s)"
    title = f"{models} in stock on Flipkart ({len(items)})"
    order_url = BASE + items[0]["page_uri"]
    body = "\n".join(["iPhone handset(s) in stock on Flipkart:", ""]
                     + _stock_lines(items)
                     + ["", f"Search: {SEARCH_URL}?q=apple+iphone"])
    ntfy_ok = email_ok = False

    if NTFY_TOPIC:
        try:
            import requests as _rq
            _rq.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode("utf-8"),
                     headers={"Title": title, "Priority": "high", "Tags": "iphone,flipkart",
                              "Click": order_url}, timeout=15).raise_for_status()
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


def check_flipkart_iphone():
    print("=" * 60)
    print(f"Flipkart iPhone monitor -- {get_ist_now()}")
    print(f"Models: {', '.join(MODELS)}   Pro: {', '.join(PRO_MODELS) or '-'}   "
          f"Pincodes: {', '.join(PINCODES)}")
    print("=" * 60)

    products = discover_products()
    print(f"[{get_ist_now()}] discovered {len(products)} watched product(s); "
          f"checking up to {MAX_PRODUCTS}")
    if not products:
        print(f"[{get_ist_now()}] No watched iPhone products found in search.")
        return False

    in_stock: List[Dict[str, Any]] = []
    for p in products[:MAX_PRODUCTS]:
        info = check_stock(p)
        if not info:
            continue
        # Re-check against the real API title (accurate model/storage), then storage gate.
        if not _is_watched(info["title"]):
            continue
        if info["in_stock"] and not _storage_ok(info["title"]):
            print(f"[{get_ist_now()}] {info['title']:42.42}  IN STOCK -- skip (storage)")
            continue
        price = f"Rs.{int(info['price'])}" if info.get("price") else "price n/a"
        where = ("@ " + "/".join(info["available_pincodes"])) if info["in_stock"] else "-"
        status = "IN STOCK" if info["in_stock"] else "out of stock"
        print(f"[{get_ist_now()}] {info['title']:42.42}  {status:12.12}  {price:12.12}  {where}")
        if info["in_stock"]:
            in_stock.append(info)

    if not in_stock:
        print(f"[{get_ist_now()}] No watched iPhone in stock at {', '.join(PINCODES)} on Flipkart.")
        return False

    print(f"[{get_ist_now()}] IN STOCK: {[p['title'] for p in in_stock]}")
    # Per-pid cooldown so each variant alerts on its own.
    new = [p for p in in_stock if should_send_alert(f"flipkart_iphone::{p['pid']}")]
    if not new:
        print(f"[{get_ist_now()}] All in-stock items already alerted within cooldown.")
        return False

    ok = send_alert(new)
    if ok:
        for p in new:
            record_alert(f"flipkart_iphone::{p['pid']}")
    return ok


if __name__ == "__main__":
    try:
        check_flipkart_iphone()
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
