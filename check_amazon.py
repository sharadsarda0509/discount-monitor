#!/usr/bin/env python3
"""
Amazon Pay Physical Gift Card upfront-discount monitor.

Amazon Pay physical gift cards come in two forms:
  * fixed-denomination "twister" cards (a child ASIN per denomination, e.g. the
    "Black Box" family: 1000/2000/3000/10000) -- their selling price AND any upfront
    discount are SERVER-rendered in the product-page HTML, so a plain request sees them.
  * free-amount cards (a "Gift Amount" box/slider) -- price is computed client-side by
    JavaScript and is NOT in any static HTML, so they can't be monitored without a browser.

This monitor watches the fixed-denomination ASINs (the ones that carry the recurring
"flat X% off Amazon Pay Gift Card" promo, e.g. a Rs.10,000 card selling for Rs.9,800).
For each ASIN it reads the buybox price (`apex-pricetopay-value`) and the struck MRP
(`apex-basisprice-feature`) straight from the product page and alerts when price < MRP.

IMPORTANT: send NO cookies. A guest `session-id`/`ubid` cookie makes Amazon render a
variant that strips the price feature-div; a cookieless request returns the full price.
"""

import os
import sys
import json
import re
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

IST = timezone(timedelta(hours=5, minutes=30))
COOLDOWN_HOURS = int(os.environ.get('ALERT_COOLDOWN_HOURS', 12))
STATE_DIR = Path('.alert_state')
STATE_FILE = STATE_DIR / 'last_alert.json'
NTFY_TOPIC = os.environ.get('NTFY_TOPIC', '')

MIN_DISCOUNT_PCT = float(os.environ.get("AMAZON_MIN_DISCOUNT", 2.0))

# Fallback fixed-denomination Amazon Pay gift-card ASINs, used when dynamic discovery
# returns nothing (Amazon served the JS-shell search variant / a captcha). These are the
# current physical + digital gifting cards surfaced by the deals listing; refresh them if
# Amazon rotates the festival ASINs again. Override via AMAZON_GC_ASINS.
_DEFAULT_ASINS = ("B0DTJ2Q579,B0DB61H2KX,B0B838CT1X,B0CCSJPV6T,"
                  "B0H5WGYP74,B0H5WRQ52J,B0H5WT5LDV")
ASINS = [a.strip() for a in os.environ.get("AMAZON_GC_ASINS", _DEFAULT_ASINS).split(",") if a.strip()]

# Dynamic discovery: pull gift-card ASINs from the deals listing each run, then verify each
# via its product page (free-amount cards render no static price and are skipped). Default
# source is the gift-cards "deals" listing filtered to the Amazon Pay brand -- exactly where
# upfront-discount cards surface (node 3704982031 = gift cards, p_123=414939 = Amazon Pay,
# p_n_deal_type=26921226031 = deals). Festival/gifting ASINs rotate constantly, so a static
# seed list goes stale within weeks; discovery keeps coverage current. Override via env.
SEARCH_URL = os.environ.get(
    "AMAZON_GC_SEARCH_URL",
    "https://www.amazon.in/s?i=gift-cards&rh=n%3A3704982031%2C"
    "p_n_deal_type%3A26921226031%2Cp_123%3A414939",
)
# Cap discovered ASINs to keep Amazon request volume (and browser cost) sane.
AMAZON_GC_MAX = int(os.environ.get("AMAZON_GC_MAX", 25))
# Only run the (multi-fetch) scan at most once per this many minutes. The workflow fires
# every 5 min; scanning ~25 product pages that often would get the IP bot-flagged.
# 0 = every trigger. Set e.g. 20 in the workflow.
RUN_INTERVAL_MIN = float(os.environ.get("AMAZON_RUN_INTERVAL_MIN", 0))

# No cookies on purpose (see module docstring). Plain desktop Chrome UA + language.
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


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
            print(f"[{get_ist_now()}] Cooldown active for amazon: {elapsed_h:.1f}h / {COOLDOWN_HOURS}h")
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


def _recently_ran() -> bool:
    """True if a scan ran within RUN_INTERVAL_MIN -- skip to avoid hammering Amazon."""
    if RUN_INTERVAL_MIN <= 0:
        return False
    try:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        last = state.get("amazon_lastrun")
        if not last:
            return False
        last_time = datetime.fromisoformat(last)
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=IST)
        elapsed_min = (get_ist_now() - last_time).total_seconds() / 60
        if elapsed_min < RUN_INTERVAL_MIN:
            print(f"[{get_ist_now()}] Throttled: last scan {elapsed_min:.0f}m ago "
                  f"(< {RUN_INTERVAL_MIN:.0f}m) -- skipping")
            return True
        return False
    except Exception:
        return False


def _mark_ran():
    STATE_DIR.mkdir(exist_ok=True)
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    state["amazon_lastrun"] = get_ist_now().isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _extract_gc_asins(html: str, max_n: int) -> List[str]:
    """Pull Amazon Pay gift-card ASINs out of a search-results page's HTML."""
    if BeautifulSoup is None or not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    found: List[str] = []
    for div in soup.find_all("div", {"data-component-type": "s-search-result"}):
        asin = div.get("data-asin", "")
        t = div.find("h2")
        if not asin or not t:
            continue
        title = t.get_text(strip=True).lower()
        if "amazon pay" in title and "gift card" in title and asin not in found:
            found.append(asin)
        if len(found) >= max_n:
            break
    return found


def discover_asins(max_n: int) -> List[str]:
    """Discover gift-card ASINs from the deals listing via a direct (cookieless) request."""
    try:
        r = _get(SEARCH_URL, headers=HEADERS, timeout=30)
        if r.status_code != 200 or "validateCaptcha" in r.text:
            print(f"[{get_ist_now()}] discovery: HTTP {r.status_code} (or captcha)")
            return []
    except Exception as e:
        print(f"[{get_ist_now()}] discovery failed: {e}")
        return []
    return _extract_gc_asins(r.text, max_n)


def discover_asins_via_browser(max_n: int, attempts: int = 4) -> List[str]:
    """Discover gift-card ASINs from the deals listing through the Scraping Browser.
    Amazon CAPTCHAs the datacenter IP, so on CI discovery must go through the residential
    browser too (a direct request there returns a CAPTCHA page and finds nothing).

    Amazon serves the search page as a client-rendered JS shell (no server-rendered results)
    for a subset of sessions, and this is fixed for the whole session/IP -- so retrying
    within one session is useless. Each browser_fetch() opens a fresh session (a new
    residential IP), so we retry across sessions and take the first that returns results."""
    for i in range(max(1, attempts)):
        resp = brightdata_browser.browser_fetch(
            "https://www.amazon.in/", [{"url": SEARCH_URL, "method": "GET"}]) or []
        first = resp[0] if resp else None
        if first and first.get("status") == 200:
            asins = _extract_gc_asins(first.get("text") or "", max_n)
            if asins:
                return asins
        print(f"[{get_ist_now()}] browser discovery attempt {i + 1}/{attempts}: "
              f"HTTP {first.get('status') if first else 'n/a'}, no results")
    return []


def _num(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    cleaned = re.sub(r"[^0-9.]", "", text)
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_gift_card(asin: str, html: str) -> Optional[Dict[str, Any]]:
    """Parse buybox price + struck MRP from a fixed-denomination gift-card product page."""
    if "validateCaptcha" in html or "Enter the characters you see" in html:
        print(f"[{get_ist_now()}] {asin}: CAPTCHA -- request was bot-flagged")
        return None

    # Buybox price (the amount you actually pay).
    pay_m = (re.search(r'apex-pricetopay-value.*?a-price-whole"[^>]*>([0-9,]+)', html, re.S)
             or re.search(r'apex-pricetopay-accessibility-label[^>]*>\s*₹?([0-9,.]+)', html))
    # Struck MRP / basis price -- present (equal to price) even without a discount.
    mrp_m = re.search(r'apex-basisprice-feature.*?a-offscreen"[^>]*>₹?([0-9,.]+)', html, re.S)
    title_m = re.search(r'id="productTitle"[^>]*>([^<]+)', html)

    price = _num(pay_m.group(1)) if pay_m else None
    mrp = _num(mrp_m.group(1)) if mrp_m else None
    if price is None:
        print(f"[{get_ist_now()}] {asin}: could not parse price (page layout changed / bot-stripped)")
        return None
    if not mrp or mrp < price:
        mrp = price  # no basis price -> treat as no discount

    discount_pct = round((mrp - price) / mrp * 100, 1) if mrp > price else 0.0
    return {
        "asin": asin,
        "title": (title_m.group(1).strip()[:80] if title_m else asin),
        "price": price,
        "mrp": mrp,
        "discount_pct": discount_pct,
        "url": f"https://www.amazon.in/dp/{asin}",
    }


def fetch_gift_card(asin: str) -> Optional[Dict[str, Any]]:
    """Direct (curl_cffi) product-page fetch -- works from a residential IP; the GitHub
    Actions datacenter IP is CAPTCHA'd by Amazon (use the Scraping Browser path there)."""
    try:
        r = _get(f"https://www.amazon.in/dp/{asin}", headers=HEADERS, timeout=30)
        if r.status_code != 200:
            print(f"[{get_ist_now()}] {asin}: HTTP {r.status_code}")
            return None
    except Exception as e:
        print(f"[{get_ist_now()}] {asin}: fetch failed: {e}")
        return None
    return _parse_gift_card(asin, r.text)


def fetch_gift_cards_via_browser(asins: List[str]) -> List[Dict[str, Any]]:
    """Fetch all ASINs' product pages in ONE Scraping Browser session (residential IP,
    real browser -> no CAPTCHA). Minimal credits: one session, same-origin GETs."""
    calls = [{"url": f"https://www.amazon.in/dp/{a}", "method": "GET"} for a in asins]
    resp = brightdata_browser.browser_fetch("https://www.amazon.in/", calls) or []
    out = []
    for asin, r in zip(asins, resp):
        if not r or r.get("status") != 200:
            print(f"[{get_ist_now()}] {asin}: browser HTTP {r.get('status') if r else 'n/a'}")
            continue
        info = _parse_gift_card(asin, r.get("text") or "")
        if info:
            out.append(info)
    return out


def _lines(matches: List[Dict[str, Any]]) -> List[str]:
    out = []
    for m in matches:
        out.append(f"- {m['title']}: Rs.{int(m['price'])} (was Rs.{int(m['mrp'])}) "
                   f"-- {m['discount_pct']:.1f}% off\n  {m['url']}")
    return out


def send_ntfy_alert(matches: List[Dict[str, Any]]) -> bool:
    if not NTFY_TOPIC:
        print(f"[{get_ist_now()}] ntfy not configured")
        return False
    try:
        message = "\n".join(["Amazon Pay Gift Card upfront discount found!\n"] + _lines(matches))
        import requests as _rq
        _rq.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={
                "Title": f"Amazon Pay GC: {matches[0]['discount_pct']:.0f}% off ({len(matches)} item(s))",
                "Priority": "high",
                "Tags": "amazon,gift,discount",
                "Click": matches[0]["url"],
            },
            timeout=15,
        ).raise_for_status()
        print(f"[{get_ist_now()}] ntfy notification sent")
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] ntfy failed: {e}")
        return False


def send_email_alert(matches: List[Dict[str, Any]]) -> bool:
    sender = os.environ.get("SENDER_EMAIL")
    receiver = os.environ.get("RECEIVER_EMAIL")
    password = os.environ.get("EMAIL_PASSWORD")
    if not all([sender, receiver, password]):
        return False
    try:
        ist_time = get_ist_now().strftime("%Y-%m-%d %H:%M:%S IST")
        text_body = "\n".join(["Amazon Pay Gift Card upfront discount!\n"] + _lines(matches)
                              + ["", f"Time: {ist_time}"])
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Amazon Pay GC: {matches[0]['discount_pct']:.0f}% off ({len(matches)} item(s))"
        msg["From"] = sender
        msg["To"] = receiver
        msg.attach(MIMEText(text_body, "plain"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.send_message(msg)
        print(f"[{get_ist_now()}] Email sent")
        return True
    except Exception as e:
        print(f"[{get_ist_now()}] Email failed: {e}")
        return False


def check_amazon():
    print("=" * 60)
    print(f"Amazon Pay Gift Card Monitor -- {get_ist_now()}")
    print(f"Min discount: {MIN_DISCOUNT_PCT}%")
    print("=" * 60)

    if _recently_ran():
        return False

    use_browser = brightdata_browser.is_configured()
    if use_browser:
        # Scraping Browser (residential) -- Amazon CAPTCHAs the datacenter IP. Discover the
        # current gift-card ASINs from the deals listing (one fetch), then verify each via
        # its product page in a second session. The festival/gifting ASINs that carry the
        # upfront-discount promo rotate constantly, so the old seed list went stale and the
        # monitor was watching 4 cards that never discount -- discovery fixes that. Seeds
        # are only a fallback for when discovery itself fails (captcha / layout change).
        discovered = discover_asins_via_browser(AMAZON_GC_MAX)
        asins = discovered or ASINS
        print(f"[{get_ist_now()}] Source: Bright Data Scraping Browser; discovered "
              f"{len(discovered)} via deals listing; checking {len(asins)} ASIN(s)")
        _mark_ran()
        infos = fetch_gift_cards_via_browser(asins)
    else:
        # Direct (residential/local): full dynamic discovery.
        discovered = discover_asins(AMAZON_GC_MAX)
        asins = list(dict.fromkeys(ASINS + discovered))
        print(f"[{get_ist_now()}] Source: direct; discovered {len(discovered)} via search; checking {len(asins)} ASIN(s)")
        _mark_ran()
        infos = [i for i in (fetch_gift_card(a) for a in asins) if i]

    matches = []
    for info in infos:
        disc = f"{info['discount_pct']:.1f}% off" if info["discount_pct"] > 0 else "no discount"
        print(f"[{get_ist_now()}] {info['asin']}  Rs.{int(info['price']):6d}  {disc:12s}  {info['title'][:50]}")
        if info["discount_pct"] >= MIN_DISCOUNT_PCT:
            matches.append(info)

    if not matches:
        print(f"[{get_ist_now()}] No gift cards with >={MIN_DISCOUNT_PCT}% upfront discount.")
        return False

    print(f"[{get_ist_now()}] MATCH: {len(matches)} discounted item(s)")
    if not should_send_alert("amazon"):
        return False

    ntfy_ok = send_ntfy_alert(matches)
    email_ok = send_email_alert(matches)
    if ntfy_ok or email_ok:
        record_alert("amazon")
    return ntfy_ok or email_ok


if __name__ == "__main__":
    try:
        check_amazon()
    except Exception as e:
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
