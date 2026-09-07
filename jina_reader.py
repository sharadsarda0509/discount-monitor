#!/usr/bin/env python3
"""Free Amazon fetch via Jina AI Reader (https://r.jina.ai/<url>).

Jina fetches the target from ITS OWN IPs, so it works from a datacenter/CI IP
without any residential proxy or Bright Data credits. Verified working for
Amazon.in product/search pages; Blinkit and Zepto still 403 through Jina (their
AWS-WAF/CloudFront blocks Jina's IPs too), so this is deliberately Amazon-only --
brightdata_browser only routes amazon.in origins here.

Switch: AMAZON_USE_JINA=1 turns it on. Optional JINA_API_KEY (free from jina.ai)
raises the keyless ~20 req/min rate limit so frequency/AMAZON_*_MAX can go higher.

fetch(calls) mirrors brightdata_browser.browser_fetch's return contract --
[{status, text}] aligned 1:1 with `calls` -- so callers stay unchanged.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

try:
    import requests
except ImportError:  # requirements guarantees this on CI; guard for safety
    requests = None  # type: ignore

READER = "https://r.jina.ai/"
_CONCURRENCY = int(os.environ.get("JINA_CONCURRENCY", 3))
_TIMEOUT = int(os.environ.get("JINA_TIMEOUT", 40))


def is_enabled() -> bool:
    return os.environ.get("AMAZON_USE_JINA", "").lower() in ("1", "true", "yes")


def _headers() -> Dict[str, str]:
    # X-Return-Format: html -> Jina returns the raw rendered HTML the existing
    # Amazon parsers expect (default is markdown). Bearer key lifts rate limits.
    h = {"X-Return-Format": "html", "X-Timeout": str(_TIMEOUT)}
    key = os.environ.get("JINA_API_KEY", "").strip()
    if key:
        h["Authorization"] = f"Bearer {key}"
    return h


def _one(call: Dict[str, Any]) -> Dict[str, Any]:
    # Only GET page fetches go through Jina. A non-GET call (Amazon's location
    # address-change POST) is a no-op 200: Jina can't persist a session cookie,
    # so pages come back default-location (see caveat in the workflow comment).
    if (call.get("method") or "GET").upper() != "GET":
        return {"status": 200, "text": ""}
    url = call.get("url") or ""
    if requests is None or not url:
        return {"status": 0, "text": ""}
    try:
        r = requests.get(READER + url, headers=_headers(), timeout=_TIMEOUT + 5)
        return {"status": r.status_code, "text": r.text if r.status_code == 200 else ""}
    except Exception as e:  # timeout / connection error -> failed page, caller skips it
        return {"status": 0, "text": str(e)}


def fetch(calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fetch each call's URL through Jina Reader, preserving order."""
    if not calls:
        return []
    workers = max(1, min(_CONCURRENCY, len(calls)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(_one, calls))
