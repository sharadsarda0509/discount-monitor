#!/usr/bin/env python3
"""Flipkart -- iPhone 15/16/17 base handset (+ iPhone 18 Pro) STOCK monitor (per pincode).

BROWSERLESS via Flipkart's internal `rome` API (see flipkart_rome.py). Earlier versions drove
headless Playwright to set a delivery location by geolocation and read the rendered buybox
(~2-3 min/run, one Chromium context per product). Flipkart's SPA gets the same per-pincode
serviceability from a JSON API, so we skip the browser entirely:

FLOW:
  1. Discovery (curl): GET /search?q=apple+iphone+<m> -> product paths; the slug
     ("apple-iphone-17-mist-blue-256-gb") carries model+storage, filtered to watched base
     handsets (is_base_handset) + the 6.3" iPhone 18 Pro (is_pro_handset) and to STORAGE_ALLOWED.
  2. Stock (rome, flipkart_rome.check_all_rome): for each pincode, bind it to a session via
     POST /api/4/location/update, then POST /api/4/page/fetch per product and read
     pageContext...psi.pls -> BUYABLE == serviceable AND isAvailable AND no unserviceabilityReason.
     A product alerts if buyable at ANY pincode.

KNOWN LIMITATION: the rome serviceability (like the guest buybox before it) reflects the
ANONYMOUS view. A listing can read serviceable=True here yet fail at the logged-in Buy Now
step with "not available for purchase" (that state appears to be account/checkout-specific).
Eliminating that fully needs a logged-in add-to-cart/checkout probe; not wired in yet.
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
from flipkart_rome import check_all_rome

try:
    from curl_cffi import requests as cffi_requests
    def _search_get(url, **kw):
        return cffi_requests.Session(impersonate="chrome120").get(url, **kw)
except ImportError:
    import requests as _requests
    def _search_get(url, **kw):
        return _requests.get(url, **kw)

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

MODELS = [m.strip() for m in os.environ.get("FLIPKART_MODELS", "15,16,17").split(",") if m.strip()]
PRO_MODELS = [m.strip() for m in os.environ.get("FLIPKART_PRO_MODELS", "18").split(",") if m.strip()]

PINCODES = [p.strip() for p in os.environ.get(
    "FLIPKART_PINCODES", "560035,560048,560103,201019,201010").split(",") if p.strip()]

MAX_PRODUCTS = int(os.environ.get("FLIPKART_MAX", 12))
# This is a network check (a few JSON calls per pincode), so it can run every dispatch. Set
# FLIPKART_RUN_INTERVAL_MIN > 0 to throttle if you widen it to many models/pincodes. 0 = every run.
RUN_INTERVAL_MIN = float(os.environ.get("FLIPKART_RUN_INTERVAL_MIN", 0))


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
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
SEARCH_HEADERS = {"User-Agent": UA, "Accept-Language": "en-IN,en;q=0.9",
                  "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
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


def _should_run() -> bool:
    if RUN_INTERVAL_MIN <= 0:
        return True
    try:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        last = state.get("flipkart_iphone_lastrun")
        if last:
            lt = datetime.fromisoformat(last)
            if lt.tzinfo is None:
                lt = lt.replace(tzinfo=IST)
            el = (get_ist_now() - lt).total_seconds() / 60
            if el < RUN_INTERVAL_MIN:
                print(f"[{get_ist_now()}] Throttled: last run {el:.0f}m ago (< {RUN_INTERVAL_MIN:.0f}m) -- skipping")
                return False
    except Exception:
        pass
    return True


def _stamp_run():
    record_alert("flipkart_iphone_lastrun")


def _name_from_slug(slug: str) -> str:
    """'apple-iphone-17-mist-blue-256-gb' -> 'Apple iPhone 17 Mist Blue 256 GB'."""
    out = []
    for w in slug.split("-"):
        if re.fullmatch(r"\d+gb|\d+tb", w, re.I):
            out.append(re.sub(r"(gb|tb)$", lambda m: " " + m.group(1).upper(), w, flags=re.I))
        elif w.lower() == "iphone":
            out.append("iPhone")
        else:
            out.append(w.capitalize())
    return " ".join(out)


def _is_watched(name: str) -> bool:
    return is_base_handset(name, MODELS) or (bool(PRO_MODELS) and is_pro_handset(name, PRO_MODELS))


def _storage_gb(name: str) -> Optional[int]:
    m = re.search(r"(\d+)\s*(GB|TB)\b", name or "", re.I)
    if not m:
        return None
    return int(m.group(1)) * (1024 if m.group(2).lower() == "tb" else 1)


def _storage_ok(name: str) -> bool:
    mm = re.search(r"i[pP]hone\s*(\d{1,2})", name or "")
    allowed = STORAGE_ALLOWED.get(mm.group(1)) if mm else None
    if not allowed:
        return True
    return _storage_gb(name) in allowed


def _search_queries() -> List[str]:
    return [f"apple iphone {m}" for m in MODELS] + [f"apple iphone {m} pro" for m in PRO_MODELS]


def discover_products() -> List[Dict[str, str]]:
    """Discover watched iPhone products from Flipkart search (fast, curl). [{pid, url, name}]."""
    found: Dict[str, Dict[str, str]] = {}
    for q in _search_queries():
        try:
            r = _search_get(SEARCH_URL, params={"q": q}, headers=SEARCH_HEADERS, timeout=30)
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
            if not _is_watched(name) or not _storage_ok(name):
                continue
            found[mpid.group(1)] = {"pid": mpid.group(1), "url": BASE + path, "name": name}
    return list(found.values())


def _stock_lines(items: List[Dict[str, Any]]) -> List[str]:
    lines = []
    for p in items:
        price = f"Rs.{p['price']}" if p.get("price") else ""
        pins = "/".join(p["available_pincodes"])
        lines.append(f"- {p['title']}: {price}  @ {pins}".rstrip())
        lines.append(f"    {p['url']}")
    return lines


def send_alert(items: List[Dict[str, Any]]) -> bool:
    models = models_summary(p["title"] for p in items)
    subject = f"Flipkart: {models} in stock -- {len(items)} item(s)"
    title = f"{models} buyable on Flipkart ({len(items)})"
    order_url = items[0]["url"]
    body = "\n".join(["iPhone handset(s) BUYABLE (deliverable) on Flipkart:", ""]
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
    print(f"Flipkart iPhone monitor (rome API) -- {get_ist_now()}")
    print(f"Models: {', '.join(MODELS)}   Pro: {', '.join(PRO_MODELS) or '-'}   "
          f"Pincodes: {', '.join(PINCODES)}")
    print("=" * 60)

    if not _should_run():
        return False
    _stamp_run()

    products = discover_products()
    print(f"[{get_ist_now()}] discovered {len(products)} watched product(s); checking up to {MAX_PRODUCTS}")

    results = check_all_rome(products, PINCODES, MAX_PRODUCTS,
                             log=lambda m: print(f"[{get_ist_now()}]{m}"))
    if not results:
        print(f"[{get_ist_now()}] No watched iPhone products found.")
        return False
    for r in sorted(results, key=lambda r: r["title"]):
        if r["in_stock"]:
            price = f"Rs.{r['price']}" if r.get("price") else "price n/a"
            print(f"[{get_ist_now()}] {r['title']:40.40}  BUYABLE  {price:12.12}  @ {'/'.join(r['available_pincodes'])}")
        else:
            price = f"Rs.{r['price']}" if r.get("price") else "price n/a"
            print(f"[{get_ist_now()}] {r['title']:40.40}  not deliverable / OOS  {price:12.12}")

    in_stock = [r for r in results if r["in_stock"]]
    if not in_stock:
        print(f"[{get_ist_now()}] No watched iPhone buyable at {', '.join(PINCODES)} on Flipkart.")
        return False

    print(f"[{get_ist_now()}] BUYABLE: {[r['title'] for r in in_stock]}")
    new = [r for r in in_stock if should_send_alert(f"flipkart_iphone::{r['pid']}")]
    if not new:
        print(f"[{get_ist_now()}] All buyable items already alerted within cooldown.")
        return False

    ok = send_alert(new)
    if ok:
        for r in new:
            record_alert(f"flipkart_iphone::{r['pid']}")
    return ok


if __name__ == "__main__":
    try:
        check_flipkart_iphone()
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
