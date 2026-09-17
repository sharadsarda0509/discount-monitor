#!/usr/bin/env python3
"""Fetch bot-gated pages via the Scrapfly Scrape API (https://scrapfly.io).

Unlike Bright Data's Scraping Browser (a WSS/CDP browser you drive), Scrapfly is a
plain HTTPS API: you GET https://api.scrapfly.io/scrape?key=..&url=..&asp=true and it
returns the fetched page in a JSON envelope (result.content). `asp=true` is the
anti-bot unblocker -- it brings a residential IP and solves the JS challenge, which is
what clears BigBasket's Akamai (verified: bare/BD/Jina requests all 403, Scrapfly 200).

COST: an asp call routes through the residential network at ~25 credits each. The free
plan is ~1,000 credits/month (~40 calls), so callers MUST throttle hard -- this is why
the BigBasket monitor scrapes only a few times a day, not every workflow tick.

fetch(url, ...) returns {status, text, cost}: `status` is the UPSTREAM HTTP status
(200 = the target answered), or 0 when Scrapfly itself errored (bad key, out of credits).
"""

import os
import re
import random
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

try:
    import requests
except ImportError:  # requirements guarantees this on CI; guard for safety
    requests = None  # type: ignore

API = "https://api.scrapfly.io/scrape"
KEY_ENV = "SCRAPFLY_KEY"
_TIMEOUT = int(os.environ.get("SCRAPFLY_TIMEOUT", 160))
# ISO country for the proxy egress. BigBasket serviceability needs an India IP.
_COUNTRY = os.environ.get("SCRAPFLY_COUNTRY", "in")

# Scrapfly HTTP statuses / error codes that mean "this account/key is spent or throttled"
# -- fail over to the next key. (Other statuses mean the target was reached, so we return.)
_ACCOUNT_STATUSES = {401, 402, 429}
_ACCOUNT_ERR_RE = re.compile(r"quota|credit|budget|throttl|too many|max.*concurren|subscription", re.I)


def _keys() -> List[str]:
    """The base key plus any numbered siblings (SCRAPFLY_KEY and _2, _3, ... _N), so
    adding a free account only means setting another secret -- no code change. Each free
    account is its own ~1,000-credit/month pool; we cycle across them (like the BD WSS pool)."""
    pat = re.compile(rf"^{re.escape(KEY_ENV)}(?:_(\d+))?$")
    found: Dict[int, str] = {}
    for k, v in os.environ.items():
        m = pat.match(k)
        v = (v or "").strip()
        if m and v:
            found[int(m.group(1)) if m.group(1) else 1] = v
    return [found[i] for i in sorted(found)]


def is_enabled() -> bool:
    return bool(_keys())


def _one(key: str, url: str, render_js: bool, cookies, method, body, headers, country):
    params: List[tuple] = [
        ("key", key), ("url", url), ("asp", "true"),
        ("render_js", "true" if render_js else "false"),
        ("country", country or _COUNTRY),
    ]
    if method and method.upper() != "GET":
        params.append(("method", method.upper()))
        if body is not None:
            params.append(("body", body))
    # Cookies + extra headers ride as headers[Name]=value (Scrapfly's forwarding syntax).
    hdrs = dict(headers or {})
    if cookies:
        hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    for name, val in hdrs.items():
        params.append((f"headers[{name}]", val))
    r = requests.get(f"{API}?{urlencode(params)}", timeout=_TIMEOUT)
    return r, r.json()


def fetch(url: str, render_js: bool = False,
          cookies: Optional[Dict[str, str]] = None,
          method: str = "GET", body: Optional[str] = None,
          headers: Optional[Dict[str, str]] = None,
          country: Optional[str] = None) -> Dict[str, Any]:
    """One Scrapfly scrape, cycling across configured keys. Returns {status, text, cost}:
    `status` is the TARGET's HTTP status (0 => every key errored). `asp` is always on.
    A key that is out of credits / throttled is skipped; any key that reaches the target
    returns immediately (we don't burn a second account on a target-side response)."""
    keys = _keys()
    if requests is None or not keys or not url:
        return {"status": 0, "text": "", "cost": 0}
    random.shuffle(keys)  # spread the monthly credit load across accounts
    last = {"status": 0, "text": "", "cost": 0}
    for i, key in enumerate(keys):
        try:
            r, j = _one(key, url, render_js, cookies, method, body, headers, country)
        except Exception as e:
            last = {"status": 0, "text": str(e), "cost": 0}
            continue
        res = j.get("result") or {}
        cost = ((j.get("context") or {}).get("cost") or {}).get("total") or res.get("cost") or 0
        if r.status_code == 200:
            return {"status": res.get("status_code") or 0, "text": res.get("content") or "", "cost": cost}
        err = res.get("error") or j.get("message") or f"scrapfly HTTP {r.status_code}"
        account_spent = r.status_code in _ACCOUNT_STATUSES or bool(_ACCOUNT_ERR_RE.search(str(err)))
        print(f"[scrapfly] key #{i + 1} {url[:50]} -> {err}"
              f"{' (spent -> next key)' if account_spent and i + 1 < len(keys) else ''}")
        last = {"status": 0, "text": "", "cost": cost}
        if account_spent:
            continue   # this account is out/throttled -> try the next key
        break          # a non-account error (bad target/config) -- more keys won't help
    return last
