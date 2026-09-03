#!/usr/bin/env python3
"""
Amazon.in -- iPhone 15 / 16 / 17 base-handset STOCK monitor.

Alerts when a base iPhone 15/16/17 (any storage/colour) is in stock on Amazon.in.
Pro / Pro Max / Plus / Air / mini / 16e and all accessories are excluded -- the same
"base handset only" rule the Blinkit/Croma/etc. monitors use (iphone_models.is_base_handset).

Optimised exactly like check_amazon.py (the gift-card monitor):
  * Discovery: pull base-handset ASINs from the search listing (one per model) each run,
    throttled (DISCOVERY_INTERVAL_MIN) and cached so cheap scans reuse the last set.
  * Stock: batch every discovered ASIN's product page into ONE Bright Data Scraping
    Browser session (residential IP, real Chrome -> no CAPTCHA, minimal credits) and read
    availability straight from the server-rendered `#availability` block.
  * Scan throttle (RUN_INTERVAL_MIN) + alert cooldown (COOLDOWN_HOURS) keep credits/noise low.
Amazon CAPTCHAs the GitHub Actions datacenter IP, so on CI the browser path is required;
a direct cookieless request is used as a local fallback when no browser is configured.

Amazon search cards carry the product title in an `aria-label` (the <h2> now holds only the
brand), so titles are read from there. Availability text ("In stock" / "Only N left in
stock" / "Currently unavailable") is the reliable signal -- a whole-page "currently
unavailable" substring is NOT, it appears even on in-stock pages.
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
    from curl_cffi import requests as cffi_requests
    def _get(url, **kw):
        return cffi_requests.Session(impersonate="chrome120").get(url, **kw)
except ImportError:
    try:
        import requests as _requests
        def _get(url, **kw):
            return _requests.get(url, **kw)
    except ImportError:
        print("Error: pip install -r requirements.txt")
        sys.exit(1)

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

import brightdata_browser
from iphone_models import is_base_handset, models_summary

IST = timezone(timedelta(hours=5, minutes=30))
COOLDOWN_HOURS = float(os.environ.get(
    "AMAZON_IPHONE_COOLDOWN_HOURS", os.environ.get("ALERT_COOLDOWN_HOURS", 12)))
STATE_DIR = Path(".alert_state")
STATE_FILE = STATE_DIR / "last_alert.json"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

MODELS = [m.strip() for m in os.environ.get("AMAZON_IPHONE_MODELS", "15,16,17").split(",") if m.strip()]

# Base-handset ASINs always checked (unioned with discovery) so a known model is covered even
# when the listing omits it that session -- the deals search is high-variance, often ranks
# Pro/Air/sponsored above the base handsets, and sometimes returns a JS shell with no results.
# Seeded with base iPhone 16 128GB + iPhone 17 256GB/512GB so 17 is checked on every scan
# instead of only when discovery happens to surface it. Refresh when Amazon rotates these;
# override via AMAZON_IPHONE_ASINS.
_DEFAULT_ASINS = "B0DGJ7TGDR,B0DGHZWBYB,B0FQFYXCC4,B0FQFJ87HN,B0FQFLYV1S"
ASINS = [a.strip() for a in os.environ.get("AMAZON_IPHONE_ASINS", _DEFAULT_ASINS).split(",") if a.strip()]

# Search listing per model -- exactly where base handsets surface. Override the whole list
# (semicolon-separated) via AMAZON_IPHONE_SEARCH_URLS.
_SEARCH_TMPL = os.environ.get("AMAZON_IPHONE_SEARCH_TMPL", "https://www.amazon.in/s?k=apple+iphone+{m}")
_SEARCH_URLS = [u.strip() for u in os.environ.get("AMAZON_IPHONE_SEARCH_URLS", "").split(";") if u.strip()] \
    or [_SEARCH_TMPL.format(m=m) for m in MODELS]

# Delivery pincode. Prime eligibility AND the delivery promise are location-dependent -- with
# no location set Amazon renders neither (every item shows a "no delivery promise" block), so
# a pincode is required to tell a genuinely buyable offer from a ghost "Only N left" listing.
PINCODE = os.environ.get("AMAZON_IPHONE_PINCODE", "560035")

# Only alert on a genuine Prime offer: Amazon-fulfilled (buybox "Ships from Amazon") AND a
# committed delivery promise to PINCODE (a real PRIMARY_DELIVERY_MESSAGE, not
# NO_PROMISE_UPSELL_MESSAGE). A bare delivery promise is NOT enough -- a third-party
# seller-fulfilled offer promises delivery too but isn't Prime, and those leaked through as
# non-Prime alerts. Set AMAZON_IPHONE_REQUIRE_PRIME=false to alert on any in-stock offer.
REQUIRE_PRIME = os.environ.get("AMAZON_IPHONE_REQUIRE_PRIME", "true").strip().lower() not in ("0", "false", "no")

# Per-model storage allow-list (GB): only alert on these storages for the given model; a model
# not listed alerts on any storage. Default: iPhone 16 & 17 -> 256 GB only (skip 128/512 GB / 1 TB).
# Override via AMAZON_IPHONE_STORAGE="17:256,17:128;16:128" (model:gb, comma/semicolon-separated).
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


STORAGE_ALLOWED = _parse_storage_cfg(os.environ.get("AMAZON_IPHONE_STORAGE", "17:256,16:256"))

# Cap discovered ASINs to keep Amazon request volume (and browser cost) sane.
AMAZON_MAX = int(os.environ.get("AMAZON_IPHONE_MAX", 25))
# Only run the (multi-fetch) scan at most once per this many minutes. 0 = every trigger.
RUN_INTERVAL_MIN = float(os.environ.get("AMAZON_IPHONE_RUN_INTERVAL_MIN", 0))
# Discovery (the extra listing session) is throttled separately; cheap scans in between
# reuse the cached ASIN set. 0 = discover every scan.
DISCOVERY_INTERVAL_MIN = float(os.environ.get("AMAZON_IPHONE_DISCOVERY_INTERVAL_MIN", 0))

# No cookies on purpose (plain desktop Chrome UA) for the direct fallback path.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

PRODUCT_SEARCH_URL = "https://www.amazon.in/s?k=apple+iphone"


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


def _elapsed_min(key: str) -> Optional[float]:
    """Minutes since the timestamp stored under `key`, or None if absent/unreadable."""
    try:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        last = state.get(key)
        if not last:
            return None
        last_time = datetime.fromisoformat(last)
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=IST)
        return (get_ist_now() - last_time).total_seconds() / 60
    except Exception:
        return None


def _stamp(key: str, value: Any = None):
    STATE_DIR.mkdir(exist_ok=True)
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    state[key] = value if value is not None else get_ist_now().isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _recently_ran() -> bool:
    """True if a scan ran within RUN_INTERVAL_MIN -- skip to conserve credits / avoid hammering."""
    if RUN_INTERVAL_MIN <= 0:
        return False
    m = _elapsed_min("amazon_iphone_lastrun")
    if m is not None and m < RUN_INTERVAL_MIN:
        print(f"[{get_ist_now()}] Throttled: last scan {m:.0f}m ago "
              f"(< {RUN_INTERVAL_MIN:.0f}m) -- skipping")
        return True
    return False


def _discovery_due() -> bool:
    """True if ASIN discovery should run this scan (throttled separately from the scan)."""
    if DISCOVERY_INTERVAL_MIN <= 0:
        return True
    m = _elapsed_min("amazon_iphone_lastdiscovery")
    if m is not None and m < DISCOVERY_INTERVAL_MIN:
        print(f"[{get_ist_now()}] Discovery throttled: last {m:.0f}m ago "
              f"(< {DISCOVERY_INTERVAL_MIN:.0f}m) -- reusing cached ASINs")
        return False
    return True


def _mark_discovery(discovered: List[str]):
    _stamp("amazon_iphone_lastdiscovery")
    _stamp("amazon_iphone_discovered", discovered)


def _cached_discovered() -> List[str]:
    try:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        return [a for a in (state.get("amazon_iphone_discovered") or []) if isinstance(a, str)]
    except Exception:
        return []


def _clean_title(t: str) -> str:
    """Reduce a verbose Amazon title to its product-name head. Amazon titles read
    'iPhone 16 128 GB: 5G Mobile Phone ... Big Boost in Battery Life; Ultramarine' -- the
    marketing tail after the first ':'/';'/'|' contains words (battery, power, band, ...)
    that would trip is_base_handset's accessory denylist. The head ('iPhone 16 128 GB')
    carries the model + storage and is what we match on. Also drops a 'Sponsored Ad -' prefix."""
    t = re.sub(r"^\s*Sponsored Ad\s*-\s*", "", t or "", flags=re.I)
    return re.split(r"[:;|]", t, 1)[0].strip()


_STORAGE_RE = re.compile(r"(\d+)\s*(GB|TB)\b", re.I)


def _title_storage_gb(title: str) -> Optional[int]:
    m = _STORAGE_RE.search(title or "")
    if not m:
        return None
    return int(m.group(1)) * (1024 if m.group(2).lower() == "tb" else 1)


def _storage_allowed(title: str) -> bool:
    """False only when the title's model has a storage allow-list and its storage isn't in it."""
    mm = re.search(r"i[pP]hone\s*(\d{1,2})", title or "")
    allowed = STORAGE_ALLOWED.get(mm.group(1)) if mm else None
    if not allowed:
        return True
    return _title_storage_gb(title) in allowed


def _card_title(div) -> str:
    """Amazon search cards carry the full product title in an aria-label (the <h2> holds
    only the brand). Return the (cleaned) longest aria-label mentioning iPhone."""
    best = ""
    for el in div.find_all(attrs={"aria-label": True}):
        v = el.get("aria-label", "")
        if "iphone" in v.lower() and len(v) > len(best):
            best = v
    return _clean_title(best)


def _extract_iphone_asins(html: str, max_n: int) -> List[str]:
    """Base iPhone 15/16/17 handset ASINs from a search-results page's HTML."""
    if BeautifulSoup is None or not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    found: List[str] = []
    for div in soup.find_all("div", {"data-component-type": "s-search-result"}):
        asin = div.get("data-asin", "")
        if not asin or asin in found:
            continue
        if is_base_handset(_card_title(div), MODELS):
            found.append(asin)
        if len(found) >= max_n:
            break
    return found


def discover_asins_via_browser(max_n: int, attempts: int = 3) -> List[str]:
    """Discover base-handset ASINs from the per-model listings through the Scraping Browser.
    Amazon serves a JS shell (no server-rendered results) to a subset of sessions, fixed for
    the whole session/IP -- so retry across fresh sessions and take the first with results."""
    for i in range(max(1, attempts)):
        calls = [{"url": u, "method": "GET"} for u in _SEARCH_URLS]
        resp = brightdata_browser.browser_fetch("https://www.amazon.in/", calls) or []
        found: List[str] = []
        for u, r in zip(_SEARCH_URLS, resp):
            if r and r.get("status") == 200:
                for a in _extract_iphone_asins(r.get("text") or "", max_n):
                    if a not in found:
                        found.append(a)
            else:
                print(f"[{get_ist_now()}] browser discovery {u}: "
                      f"HTTP {r.get('status') if r else 'n/a'}")
        if found:
            return found[:max_n]
        print(f"[{get_ist_now()}] browser discovery attempt {i + 1}/{attempts}: no base handsets")
    return []


def discover_asins(max_n: int) -> List[str]:
    """Discover base-handset ASINs via direct (cookieless) requests -- local fallback."""
    found: List[str] = []
    for u in _SEARCH_URLS:
        try:
            r = _get(u, headers=HEADERS, timeout=30)
            if r.status_code != 200 or "validateCaptcha" in r.text:
                print(f"[{get_ist_now()}] discovery {u}: HTTP {r.status_code} (or captcha)")
                continue
            for a in _extract_iphone_asins(r.text, max_n):
                if a not in found:
                    found.append(a)
        except Exception as e:
            print(f"[{get_ist_now()}] discovery {u} failed: {e}")
    return found[:max_n]


def _fetch_pages(asins: List[str], use_browser: bool) -> Dict[str, str]:
    """Fetch product-page HTML for each ASIN -> {asin: html}. The browser path batches every
    ASIN into ONE Scraping Browser session; the direct path uses curl_cffi (CI is CAPTCHA'd)."""
    pages: Dict[str, str] = {}
    if not asins:
        return pages
    if use_browser:
        # First set the delivery pincode in the session (Amazon accepts this without a CSRF
        # token) so the product pages that follow render the real, location-specific buybox --
        # Prime badge + delivery promise. Same-session fetches carry the location cookie it sets.
        loc_call = {
            "url": "https://www.amazon.in/gp/delivery/ajax/address-change.html", "method": "POST",
            "headers": {"content-type": "application/x-www-form-urlencoded;charset=UTF-8",
                        "x-requested-with": "XMLHttpRequest"},
            "body": (f"locationType=LOCATION_INPUT&zipCode={PINCODE}&storeContext=generic"
                     f"&deviceType=web&pageType=Detail&actionSource=glow"),
            "credentials": "include",
        }
        prod_calls = [{"url": f"https://www.amazon.in/dp/{a}", "method": "GET",
                       "credentials": "include"} for a in asins]
        resp = brightdata_browser.browser_fetch("https://www.amazon.in/", [loc_call] + prod_calls) or []
        for asin, r in zip(asins, resp[1:]):  # resp[0] is the address-change result
            if r and r.get("status") == 200:
                pages[asin] = r.get("text") or ""
            else:
                print(f"[{get_ist_now()}] {asin}: browser HTTP {r.get('status') if r else 'n/a'}")
    else:
        for asin in asins:
            try:
                r = _get(f"https://www.amazon.in/dp/{asin}", headers=HEADERS, timeout=30)
                if r.status_code == 200:
                    pages[asin] = r.text
                else:
                    print(f"[{get_ist_now()}] {asin}: HTTP {r.status_code}")
            except Exception as e:
                print(f"[{get_ist_now()}] {asin}: fetch failed: {e}")
    return pages


def _num(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.]", "", text)
    try:
        return float(cleaned)
    except ValueError:
        return None


def _offer_feature(html: str, feat: str) -> Optional[str]:
    """Value of a buybox offer-display feature (e.g. desktop-fulfiller-info = 'Ships from',
    desktop-merchant-info = 'Sold by'). The value sits in a message span (or a seller link)
    just after the feature's text widget; search a bounded window to keep the regex cheap."""
    for m in re.finditer(r'offer-display-feature-name="' + re.escape(feat) + r'"', html):
        w = html[m.end(): m.end() + 600]
        vm = (re.search(r'offer-display-feature-text-message[^>]*>\s*([^<]+?)\s*<', w)
              or re.search(r'<a[^>]*>\s*([^<]+?)\s*</a>', w))
        if vm:
            return vm.group(1).strip()
    return None


def _parse_stock(asin: str, html: str) -> Optional[Dict[str, Any]]:
    """Parse title + availability + fulfiller (+ best-effort price) from a product page.
    Returns a base iPhone 15/16/17 handset dict, or None (not a base handset / CAPTCHA)."""
    if "validateCaptcha" in html or "Enter the characters you see" in html:
        print(f"[{get_ist_now()}] {asin}: CAPTCHA -- request was bot-flagged")
        return None

    title_m = re.search(r'id="productTitle"[^>]*>([^<]+)', html)
    title = _clean_title(title_m.group(1)) if title_m else ""
    if not is_base_handset(title, MODELS):
        return None  # Pro/Plus/Max/Air/mini/e, accessory, or unparseable title -> ignore

    # Availability from the server-rendered #availability block (the reliable signal).
    avail_m = re.search(r'id="availability".*?<span[^>]*>\s*([^<]+?)\s*<', html, re.S)
    avail = (avail_m.group(1).strip() if avail_m else "")
    al = avail.lower()
    has_buybox = ('id="add-to-cart-button"' in html) or ('id="buy-now-button"' in html) \
        or ('submit.add-to-cart' in html)
    if "unavailable" in al or "out of stock" in al or "sold out" in al or "soon" in al:
        in_stock = False
    elif "in stock" in al or "left in stock" in al or re.search(r"only\s+\d+\s+left", al):
        in_stock = True
    else:
        in_stock = has_buybox  # no availability text -> fall back to the buybox

    pay_m = (re.search(r'apex-pricetopay-value.*?a-price-whole"[^>]*>([0-9,]+)', html, re.S)
             or re.search(r'a-price-whole"[^>]*>([0-9,]+)', html))
    price = _num(pay_m.group(1)) if pay_m else None

    ships_from = _offer_feature(html, "desktop-fulfiller-info")
    sold_by = _offer_feature(html, "desktop-merchant-info")
    prime = "a-icon-prime" in html  # a rendered Prime badge (location-dependent)
    # Amazon commits a delivery date via a DELIVERY_BLOCK slot; when it can't actually deliver
    # to PINCODE it emits NO_PROMISE_UPSELL_MESSAGE (or no slot). A real promise slot is the
    # reliable "buyable for real" signal -- unlike a bare "Only N left" that oversells.
    slots = re.findall(r"mir-layout-DELIVERY_BLOCK-slot-([A-Z_]+)", html)
    deliverable = bool(slots) and any(s != "NO_PROMISE_UPSELL_MESSAGE" for s in slots)
    dm = re.search(r'data-csa-c-delivery-time="([^"]+)"', html)
    delivery = dm.group(1).strip() if dm else None
    return {
        "asin": asin,
        "title": title[:80],
        "in_stock": in_stock,
        "availability": avail or "(no availability text)",
        "price": price,
        "ships_from": ships_from,
        "sold_by": sold_by,
        "prime": prime,
        "deliverable": deliverable,
        "delivery": delivery,
        "url": f"https://www.amazon.in/dp/{asin}",
    }


def _prime_ok(p: Dict[str, Any]) -> bool:
    """Genuine Prime delivery: an Amazon-fulfilled offer (buybox 'Ships from Amazon') that
    also commits a real delivery promise to PINCODE. A bare delivery promise is NOT enough --
    a third-party seller-fulfilled offer promises delivery too but isn't Prime; those were
    leaking through the old deliverable-only gate as non-Prime alerts."""
    ships = (p.get("ships_from") or "").strip().lower()
    return bool(p.get("deliverable") and ships.startswith("amazon"))


def _fulfillment(p: Dict[str, Any]) -> str:
    bits = []
    if p.get("delivery"):
        bits.append(f"delivery {p['delivery']}")
    elif p.get("deliverable"):
        bits.append("delivery promised")
    if p.get("prime"):
        bits.append("Prime")
    if p.get("ships_from"):
        bits.append(f"ships from {p['ships_from']}")
    return " | ".join(bits) if bits else "no delivery promise"


def _lines(items: List[Dict[str, Any]]) -> List[str]:
    out = []
    for p in items:
        price = f"Rs.{int(p['price'])}" if p.get("price") else "price n/a"
        out.append(f"- {p['title']}: {price} | {p['availability']} | {_fulfillment(p)}\n  {p['url']}")
    return out


def send_alert(items: List[Dict[str, Any]]) -> bool:
    models = models_summary(p["title"] for p in items)
    subject = f"Amazon: {models} in stock -- {len(items)} item(s)"
    title = f"{models} in stock on Amazon ({len(items)})"
    body = "\n".join(["iPhone handset(s) in stock on Amazon.in:", ""] + _lines(items)
                     + ["", f"Search: {PRODUCT_SEARCH_URL}"])
    ntfy_ok = email_ok = False

    if NTFY_TOPIC:
        try:
            import requests as _rq
            _rq.post(
                f"https://ntfy.sh/{NTFY_TOPIC}",
                data=body.encode("utf-8"),
                headers={"Title": title, "Priority": "high", "Tags": "iphone,amazon",
                         "Click": items[0]["url"]},
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


def check_amazon_iphone():
    use_browser = brightdata_browser.is_configured()
    print("=" * 60)
    print(f"Amazon iPhone stock monitor -- {get_ist_now()}")
    print(f"Models: {', '.join(MODELS)}   Prime-only: {REQUIRE_PRIME} (pin {PINCODE})   "
          f"Source: {'Bright Data Scraping Browser' if use_browser else 'direct'}")
    print("=" * 60)

    if _recently_ran():
        return False

    if _discovery_due():
        discovered = discover_asins_via_browser(AMAZON_MAX) if use_browser else discover_asins(AMAZON_MAX)
        src = "Bright Data Scraping Browser" if use_browser else "direct"
        if discovered:
            _mark_discovery(discovered)
        else:
            discovered = _cached_discovered()
            src += " (empty, reusing cache)"
    else:
        discovered = _cached_discovered()
        src = "cached discovery"

    asins = list(dict.fromkeys(ASINS + discovered))[:AMAZON_MAX]
    print(f"[{get_ist_now()}] Source: {src}; discovered {len(discovered)}; "
          f"{len(asins)} ASIN(s) ({len(ASINS)} known + discovery)")
    _stamp("amazon_iphone_lastrun")

    if not asins:
        print(f"[{get_ist_now()}] No base iPhone {'/'.join(MODELS)} ASINs to check.")
        return False

    pages = _fetch_pages(asins, use_browser)
    infos = [i for i in (_parse_stock(a, h) for a, h in pages.items()) if i]

    if not infos:
        print(f"[{get_ist_now()}] No base iPhone handsets parsed from {len(pages)} page(s).")
        return False

    in_stock = []
    for info in infos:
        info["storage_ok"] = _storage_allowed(info["title"])
        status = "IN STOCK" if info["in_stock"] else "out of stock"
        price = f"Rs.{int(info['price'])}" if info.get("price") else "price n/a"
        deliv = (info.get("delivery") or ("promised" if info["deliverable"] else "no-promise"))
        eligible = info["in_stock"] and (_prime_ok(info) or not REQUIRE_PRIME)
        note = "  [skip: storage]" if eligible and not info["storage_ok"] else ""
        print(f"[{get_ist_now()}] {info['asin']}  {status:12s}  {price:12s}  "
              f"{info['availability'][:18]:18.18}  prime={'Y' if _prime_ok(info) else 'N'}  "
              f"ships:{(info.get('ships_from') or '-')[:10]:10.10}  "
              f"deliver:{deliv[:16]:16.16}  {info['title'][:30]}{note}")
        # Only alert when Amazon commits a delivery promise (Prime/deliverable) unless
        # REQUIRE_PRIME is off -- a bare 'Only N left' with no promise oversells and is gone by
        # checkout -- and the storage passes the per-model allow-list (AMAZON_IPHONE_STORAGE).
        if eligible and info["storage_ok"]:
            in_stock.append(info)

    if not in_stock:
        gated = [i for i in infos if i["in_stock"] and not _prime_ok(i)]
        filtered = [i for i in infos
                    if i["in_stock"] and (_prime_ok(i) or not REQUIRE_PRIME) and not i["storage_ok"]]
        if filtered:
            print(f"[{get_ist_now()}] {len(filtered)} in stock but excluded by "
                  f"AMAZON_IPHONE_STORAGE -- not alerting.")
        if REQUIRE_PRIME and gated:
            print(f"[{get_ist_now()}] {len(gated)} in stock but not Prime (Amazon-fulfilled + "
                  f"delivery) to {PINCODE} -- not alerting. Set AMAZON_IPHONE_REQUIRE_PRIME=false to include.")
        elif not filtered:
            print(f"[{get_ist_now()}] No base iPhone handsets in stock.")
        return False

    print(f"[{get_ist_now()}] IN STOCK: {[p['asin'] for p in in_stock]}")
    # Per-ASIN cooldown so a freshly-in-stock model/variant alerts on its own. A single global
    # key let one iPhone's alert silence every other model for the whole cooldown window (e.g.
    # an in-stock iPhone 17 starved while a 16 sat inside the 12h window).
    new = [p for p in in_stock if should_send_alert(f"amazon_iphone::{p['asin']}")]
    if not new:
        print(f"[{get_ist_now()}] All in-stock handsets already alerted within cooldown.")
        return False

    ok = send_alert(new)
    if ok:
        for p in new:
            record_alert(f"amazon_iphone::{p['asin']}")
    return ok


if __name__ == "__main__":
    try:
        check_amazon_iphone()
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
