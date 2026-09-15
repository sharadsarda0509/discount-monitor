#!/usr/bin/env python3
"""Flipkart per-pincode buyability via the internal `rome` page-fetch API -- browserless.

WHY THIS EXISTS: check_flipkart_iphone.py's default path launches headless Chromium and,
per pincode, sets the delivery location via geolocation then reads the rendered buybox
(~2-3 min/run, one Chromium context per product). Flipkart's SPA actually gets the same
per-pincode serviceability from a JSON API, so we can skip the browser entirely.

THE RECIPE (reverse-engineered + verified against the live site, Sep 2026):
  1. Seed a session: GET https://www.flipkart.com/  (curl_cffi impersonate=chrome120 to
     pass the PerimeterX/Akamai TLS fingerprint check -- a plain requests call gets HTTP 406).
  2. Bind the pincode to that SESSION:
        POST https://1.rome.api.flipkart.com/api/4/location/update
        body: {"pincode": "<PIN>", "marketplace": "FLIPKART"}
     This sets the location-bearing `ud` cookie. NOTE: passing the pincode only in the
     page/fetch body WITHOUT this call is ignored -> the response comes back with
     unserviceabilityReason "NO_PINCODE" (i.e. national/no-location data). The bind is required.
  3. Fetch the product page bound to that location (reuse the same session's cookies):
        POST https://1.rome.api.flipkart.com/api/4/page/fetch?cacheFirst=false
        body: {"pageUri": "<path?pid=...>", "locationContext": {"pincode": <PIN int>, "changed": false}, ...}
     Read RESPONSE.pageData.pageContext.fdpEventTracking.events.psi:
        .pls  -> serviceable / isAvailable / availabilityStatus / unserviceabilityReason
        .ppd  -> finalPrice
     BUYABLE == serviceable AND isAvailable AND not unserviceabilityReason.

VERIFIED bidirectional vs the browser buybox (the gold standard) AND real stock, Sep 2026:
  iPhone 15 (in stock)  @ 560035 & 201019 -> serviceable=True  (browser: Buy Now + delivery promise)
  iPhone 17 (sold out)  @ 560035 & 201019 -> serviceable=False (browser: Notify Me), NoServiceableVendor

RELIABILITY: both rome endpoints 406 intermittently under rapid fire (PerimeterX rate limit),
so we bind ONCE per pincode and reuse the session for every product (cuts request volume ~Nx),
with bounded retries on each call. Caveat: verified from a residential Indian IP; a GitHub
Actions datacenter IP may see heavier gating -- watch the run logs after enabling.
"""

import os
import time
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional

from curl_cffi import requests as cffi

BASE = "https://www.flipkart.com"
LOC_UPDATE = "https://1.rome.api.flipkart.com/api/4/location/update"
PAGE_FETCH = "https://1.rome.api.flipkart.com/api/4/page/fetch?cacheFirst=false"

# The FKUA-suffixed X-User-Agent is the load-bearing header: the rome API only serves the
# JSON page payload to Flipkart's own web client, identified by this token.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_FKUA = _UA + " FKUA/website/42/website/Desktop"

LOC_TRIES = 10      # location/update 406s often (PerimeterX); retry until it binds
FETCH_TRIES = 6
RETRY_SLEEP_S = 0.8
RETRY_SLEEP_MAX_S = 8.0
# Rotate the impersonated TLS fingerprint across retries: a fresh fingerprint + fresh
# cookies is far more likely to slip past a PerimeterX rate-limit window than hammering
# the same flagged session.
_IMPERSONATE = ["chrome120", "chrome124", "chrome116", "chrome123", "safari17_0"]

# Optional residential proxy for the rome calls. A GitHub Actions datacenter IP may get
# gated by PerimeterX harder than a residential IP; set FLIPKART_PROXY (e.g. a Bright Data
# residential endpoint) to route through it. Empty = direct.
_PROXY = os.environ.get("FLIPKART_PROXY", "").strip()


def _new_session(impersonate: str):
    kw: Dict[str, Any] = {"impersonate": impersonate}
    if _PROXY:
        kw["proxies"] = {"http": _PROXY, "https": _PROXY}
    return cffi.Session(**kw)


def _api_headers(referer: str) -> Dict[str, str]:
    return {
        "content-type": "application/json",
        "origin": BASE,
        "referer": referer,
        "x-user-agent": _FKUA,
        "accept": "*/*",
        "accept-language": "en-IN,en;q=0.9",
    }


def _page_uri(product_url: str) -> str:
    p = urlparse(product_url)
    return p.path + (("?" + p.query) if p.query else "")


def bind_session(pincode: str, log=print) -> Optional[Any]:
    """Bind `pincode` to a fresh session. Returns a curl_cffi Session or None.

    Each attempt uses a brand-new session with a rotated TLS fingerprint (new cookies too),
    seeds cookies with a home GET, then POSTs location/update. Backoff grows on repeated 406
    to ride out a PerimeterX rate-limit window rather than pounding a flagged session."""
    body = {"pincode": str(pincode), "marketplace": "FLIPKART"}
    last = "?"
    for attempt in range(LOC_TRIES):
        imp = _IMPERSONATE[attempt % len(_IMPERSONATE)]
        s = _new_session(imp)
        try:
            s.get(BASE + "/", headers={"user-agent": _FKUA, "accept-language": "en-IN,en;q=0.9"},
                  timeout=30)
            r = s.post(LOC_UPDATE, json=body, headers=_api_headers(BASE + "/"), timeout=30)
            last = r.status_code
            if r.status_code == 200:
                return s
        except Exception as e:
            last = f"err:{str(e)[:40]}"
        time.sleep(min(RETRY_SLEEP_S * (2 ** attempt), RETRY_SLEEP_MAX_S))
    log(f"  rome: could not bind pincode {pincode} after {LOC_TRIES} tries (last {last})")
    return None


def fetch_buyability(session, product_url: str, pincode: str, log=print) -> Optional[Dict[str, Any]]:
    """Fetch one product bound to the session's pincode. Returns {buyable, price, status, ...} or None."""
    page_uri = _page_uri(product_url)
    body = {
        "pageUri": page_uri,
        "pageContext": {"trackingContext": {}, "networkSpeed": 9400},
        "locationContext": {"pincode": int(pincode), "changed": False},
    }
    resp = None
    for _ in range(FETCH_TRIES):
        try:
            resp = session.post(PAGE_FETCH, json=body, headers=_api_headers(BASE + page_uri), timeout=30)
            if resp.status_code == 200:
                break
        except Exception as e:
            log(f"  rome: page/fetch error {pincode}: {str(e)[:50]}")
        time.sleep(RETRY_SLEEP_S)
    if resp is None or resp.status_code != 200:
        return None
    try:
        psi = resp.json()["RESPONSE"]["pageData"]["pageContext"]["fdpEventTracking"]["events"]["psi"]
    except Exception as e:
        log(f"  rome: parse failed {pincode}: {str(e)[:50]}")
        return None
    pls = psi.get("pls", {}) or {}
    ppd = psi.get("ppd", {}) or {}
    serviceable = bool(pls.get("serviceable"))
    is_available = bool(pls.get("isAvailable"))
    reason = pls.get("unserviceabilityReason")
    price = ppd.get("finalPrice") or ppd.get("fsp")
    return {
        # Buyable only when the listing is serviceable at this pincode AND available AND no
        # unserviceability reason -- the API equivalent of the browser's "Buy Now + delivery promise".
        "buyable": serviceable and is_available and not reason,
        "price": int(price) if isinstance(price, (int, float)) and price else None,
        "availabilityStatus": pls.get("availabilityStatus"),
        "serviceable": serviceable,
        "isAvailable": is_available,
        "reason": reason,
    }


def check_all_rome(products: List[Dict[str, str]], pincodes: List[str],
                   max_products: int = 12, log=print) -> List[Dict[str, Any]]:
    """Browserless equivalent of check_flipkart_iphone.check_all: for each pincode bind once,
    then fetch every product's buyability. Returns records shaped like check_all's output."""
    watch = products[:max_products]
    by_pid: Dict[str, Dict[str, Any]] = {
        p["pid"]: {**p, "title": p["name"], "price": None, "available_pincodes": []} for p in watch}
    for pincode in pincodes:
        session = bind_session(pincode, log=log)
        if not session:
            continue
        for p in watch:
            info = fetch_buyability(session, p["url"], pincode, log=log)
            if not info:
                continue
            if info["price"]:
                by_pid[p["pid"]]["price"] = info["price"]
            if info["buyable"]:
                by_pid[p["pid"]]["available_pincodes"].append(pincode)
    out = []
    for rec in by_pid.values():
        rec["in_stock"] = bool(rec["available_pincodes"])
        out.append(rec)
    return out
