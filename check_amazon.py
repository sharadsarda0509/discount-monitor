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
# Once a discount is found + alerted, skip scanning entirely for this many hours -- no repeat
# alerts for the same promo, and no Bright Data credits burned re-checking a known live deal.
FOUND_COOLDOWN_HOURS = float(os.environ.get('AMAZON_FOUND_COOLDOWN_HOURS', 24))
STATE_DIR = Path('.alert_state')
STATE_FILE = STATE_DIR / 'last_alert.json'
NTFY_TOPIC = os.environ.get('NTFY_TOPIC', '')

MIN_DISCOUNT_PCT = float(os.environ.get("AMAZON_MIN_DISCOUNT", 2.0))

# Known Rs.5000 Amazon Pay gift-card ASINs, always checked (unioned with discovery) so the
# recurring ~2% arbitrage (Rs.4900 for a Rs.5000 card) is caught even when the deals listing
# omits them or returns nothing. Only one ASIN per card is needed -- each is expanded to its
# Rs.5000/10000 twister siblings at scan time. Refresh when Amazon rotates these; override via
# AMAZON_GC_ASINS. (Festival/gifting cards top out at Rs.3000, so they're intentionally absent:
# the Rs.5000/10000-only filter would drop them anyway.)
_DEFAULT_ASINS = "B0GGRCP3ZF,B0GGQXZQ1H,B0BSFB9CHS"
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


def _found_recently() -> bool:
    """True if an Amazon discount was already found + alerted within FOUND_COOLDOWN_HOURS.
    Once a promo is found we skip scanning entirely for a day: no repeat alerts, and no
    Bright Data credits spent re-checking the same live discount."""
    if FOUND_COOLDOWN_HOURS <= 0:
        return False
    try:
        state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        last = state.get("amazon")
        if not last:
            return False
        last_time = datetime.fromisoformat(last)
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=IST)
        elapsed_h = (get_ist_now() - last_time).total_seconds() / 3600
        if elapsed_h < FOUND_COOLDOWN_HOURS:
            print(f"[{get_ist_now()}] Found-cooldown: discount alerted {elapsed_h:.1f}h ago "
                  f"(< {FOUND_COOLDOWN_HOURS:.0f}h) -- skipping scan")
            return True
        return False
    except Exception:
        return False


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


# We only care about Rs.5000 / Rs.10000 cards -- that's where the upfront arbitrage lands.
DENOMS_WANTED = {"5000", "10000"}


def _fetch_pages(asins: List[str], use_browser: bool) -> Dict[str, str]:
    """Fetch product-page HTML for each ASIN -> {asin: html}. The browser path batches every
    ASIN into ONE Scraping Browser session (residential IP, real browser -> no CAPTCHA, minimal
    credits); the direct path uses curl_cffi (works from a residential IP; the GitHub Actions
    datacenter IP is CAPTCHA'd by Amazon, so CI must use the browser path)."""
    pages: Dict[str, str] = {}
    if not asins:
        return pages
    if use_browser:
        calls = [{"url": f"https://www.amazon.in/dp/{a}", "method": "GET"} for a in asins]
        resp = brightdata_browser.browser_fetch("https://www.amazon.in/", calls) or []
        for asin, r in zip(asins, resp):
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


def _denomination_variant_asins(html: str) -> List[str]:
    """From a gift-card page's twister map, return the sibling ASINs whose denomination is in
    DENOMS_WANTED (Rs.5000 / Rs.10000). Each card is a twister family (theme x denomination)
    and the deals listing surfaces an arbitrary denomination, so we expand to the 5000/10000
    siblings. Amazon embeds the whole denomination->ASIN map in `dimensionValuesDisplayData`,
    e.g. {"B0..":["5000","Wedding Envelope"], "B0..":["1000","Wedding Envelope"], ...}."""
    m = re.search(r'"dimensionValuesDisplayData"\s*:\s*(\{.*?\})\s*,\s*"', html, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except Exception:
        return []
    out = []
    for asin, labels in data.items():
        vals = labels if isinstance(labels, list) else [labels]
        if {re.sub(r"[^0-9]", "", str(v)) for v in vals} & DENOMS_WANTED:
            out.append(asin)
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

    # Once a discount is found + alerted, don't scan again for FOUND_COOLDOWN_HOURS (24h).
    if _found_recently():
        return False

    if _recently_ran():
        return False

    use_browser = brightdata_browser.is_configured()
    if use_browser:
        # Amazon CAPTCHAs the datacenter IP -> discover via the residential Scraping Browser.
        discovered = discover_asins_via_browser(AMAZON_GC_MAX)
        src = "Bright Data Scraping Browser"
    else:
        discovered = discover_asins(AMAZON_GC_MAX)  # direct (residential/local)
        src = "direct"
    # The deals listing returns a different subset per session, so union discovery with the
    # known Rs.5000 ASINs (known first so they survive the cap) for stable coverage.
    parents = list(dict.fromkeys(ASINS + discovered))[:AMAZON_GC_MAX]
    print(f"[{get_ist_now()}] Source: {src}; discovered {len(discovered)}; "
          f"{len(parents)} parent ASIN(s) ({len(ASINS)} known + discovery)")
    _mark_ran()

    # Fetch each parent, then expand every family to its Rs.5000/10000 twister siblings (the
    # listing surfaces an arbitrary denomination, but the arbitrage lives on 5000/10000).
    pages = _fetch_pages(parents, use_browser)
    variants = []
    for html in pages.values():
        variants += _denomination_variant_asins(html)
    variants = [a for a in dict.fromkeys(variants) if a not in pages][:AMAZON_GC_MAX]
    if variants:
        print(f"[{get_ist_now()}] expanding {len(variants)} Rs.5000/10000 denomination variant(s)")
        pages.update(_fetch_pages(variants, use_browser))

    infos = [i for i in (_parse_gift_card(a, h) for a, h in pages.items()) if i]
    # Keep only Rs.5000 / Rs.10000 face-value cards (mrp == face value even at 0% off).
    infos = [i for i in infos if int(round(i["mrp"])) in (5000, 10000)]

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
