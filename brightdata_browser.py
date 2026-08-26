#!/usr/bin/env python3
"""Route API calls through a Bright Data Scraping Browser (a real cloud Chrome on a
residential IP) when BRIGHTDATA_BROWSER_WSS is set.

Used by the monitors whose target APIs block datacenter IPs and/or bare API clients
(Blinkit, Zepto): those requests return 403 from GitHub Actions' datacenter IP, and
some (e.g. Blinkit's iPhone search) 403 even from a residential IP when called as a
bare API -- but succeed from a real browser session. The Scraping Browser provides
exactly that: a genuine Chrome with a residential IP and Bright Data's unblocking.

Config (GitHub secrets):
  BRIGHTDATA_BROWSER_WSS    = wss://brd-customer-<id>-zone-<zone>:<password>@brd.superproxy.io:9222
  BRIGHTDATA_BROWSER_WSS_2  = (optional) a second Scraping Browser endpoint.
When both are set, browser_fetch picks one at random each run (spreads the credit/rate
load across zones/accounts) and fails over to the other on a connection error. If the
two endpoints are separate Bright Data accounts, this doubles the free credit budget;
if they are two zones of one account, it only spreads rate limits (shared credit pool).

browser_fetch(origin, calls) opens ONE browser session, navigates to `origin` (so the
fetches are same-origin with real cookies + a residential IP), runs every call in a
single page.evaluate, and returns [{status, text}, ...] in order. Batching every call
into one session keeps Scraping Browser time -- and therefore credit cost -- low.
Returns None when no endpoint is configured so callers can fall back to a direct request.
"""

import os
import random
import re
from typing import Any, Dict, List, Optional

WSS_ENV = "BRIGHTDATA_BROWSER_WSS"

# Runs inside the remote browser page: execute each call with fetch() and collect the
# status + raw body. c = {url, method, headers, body}.
_FETCH_JS = r"""
async (calls) => {
  const out = [];
  for (const c of calls) {
    try {
      const r = await fetch(c.url, {
        method: c.method || "GET",
        headers: c.headers || {},
        body: c.body || undefined,
      });
      out.push({ status: r.status, text: await r.text() });
    } catch (e) {
      out.push({ status: 0, text: String(e) });
    }
  }
  return out;
}
"""


def _wss_urls() -> List[str]:
    """Collect the base endpoint plus any numbered siblings (BRIGHTDATA_BROWSER_WSS and
    _2, _3, ... _N) so adding an endpoint only means setting a new secret -- no code change."""
    pat = re.compile(rf"^{re.escape(WSS_ENV)}(?:_(\d+))?$")
    found: Dict[int, str] = {}
    for key, val in os.environ.items():
        m = pat.match(key)
        val = (val or "").strip()
        if m and val:
            found[int(m.group(1)) if m.group(1) else 1] = val
    return [found[i] for i in sorted(found)]


def is_configured() -> bool:
    return bool(_wss_urls())


def _run_once(wss: str, origin: str, calls: List[Dict[str, Any]],
              timeout_ms: int) -> List[Dict[str, Any]]:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(wss, timeout=timeout_ms)
        try:
            page = browser.new_page()
            page.goto(origin, timeout=timeout_ms, wait_until="domcontentloaded")
            return page.evaluate(_FETCH_JS, calls)
        finally:
            browser.close()


def browser_fetch(origin: str, calls: List[Dict[str, Any]],
                  timeout_ms: int = 120000) -> Optional[List[Dict[str, Any]]]:
    """Run `calls` from `origin`'s page context in one Scraping Browser session.

    Rotates across configured endpoints (spreads credit/rate load) and fails over on
    connection error. Returns a list of {status, text} aligned with `calls`, or None if
    no endpoint is configured or every endpoint failed.
    """
    urls = _wss_urls()
    if not urls:
        return None
    random.shuffle(urls)  # spread load; also randomises failover order
    last_err = None
    for wss in urls:
        try:
            return _run_once(wss, origin, calls, timeout_ms)
        except Exception as e:  # connection/navigation failure -> try the next endpoint
            last_err = e
            zone = wss.split("zone-", 1)[-1].split(":", 1)[0] if "zone-" in wss else "?"
            print(f"[brightdata_browser] endpoint (zone {zone}) failed: {e}")
    if last_err:
        print(f"[brightdata_browser] all endpoints failed: {last_err}")
    return None
