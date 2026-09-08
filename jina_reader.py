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
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

try:
    import requests
except ImportError:  # requirements guarantees this on CI; guard for safety
    requests = None  # type: ignore

READER = "https://r.jina.ai/"
# Keyless is the sustainable free path: rate-limited (~20 req/min) but NOT
# token-metered, so it never 402s. A JINA_API_KEY is faster (500 rpm) but its
# free token grant is small and html-format full pages burn it in hours -> once
# exhausted Jina returns 402, and _one() falls back to keyless automatically.
_CONCURRENCY = int(os.environ.get("JINA_CONCURRENCY", 2))
_TIMEOUT = int(os.environ.get("JINA_TIMEOUT", 40))
_RETRIES = int(os.environ.get("JINA_RETRIES", 2))


def is_enabled() -> bool:
    return os.environ.get("AMAZON_USE_JINA", "").lower() in ("1", "true", "yes")


def _fetch(url: str, use_key: bool):
    # X-Return-Format: html -> the raw HTML the existing Amazon parsers expect
    # (default is markdown). Bearer key only when use_key (dropped after a 402).
    h = {"X-Return-Format": "html", "X-Timeout": str(_TIMEOUT)}
    key = os.environ.get("JINA_API_KEY", "").strip()
    if use_key and key:
        h["Authorization"] = f"Bearer {key}"
    return requests.get(READER + url, headers=h, timeout=_TIMEOUT + 5)


def _one(call: Dict[str, Any]) -> Dict[str, Any]:
    # Only GET page fetches go through Jina. A non-GET call (Amazon's location
    # address-change POST) is a no-op 200: Jina can't persist a session cookie,
    # so pages come back default-location (see caveat in the workflow comment).
    if (call.get("method") or "GET").upper() != "GET":
        return {"status": 200, "text": ""}
    url = call.get("url") or ""
    if requests is None or not url:
        return {"status": 0, "text": ""}
    use_key = bool(os.environ.get("JINA_API_KEY", "").strip())
    last = 0
    for attempt in range(_RETRIES + 1):
        try:
            r = _fetch(url, use_key)
        except Exception as e:  # timeout / connection error
            last = 0
            if attempt < _RETRIES:
                time.sleep(2 * (attempt + 1))
                continue
            return {"status": 0, "text": str(e)}
        if r.status_code == 200:
            return {"status": 200, "text": r.text}
        last = r.status_code
        # 402 = key token balance exhausted -> retry the SAME url keyless (free).
        if r.status_code == 402 and use_key:
            use_key = False
            continue
        # 429 = keyless rate limit (~20 rpm) -> back off and retry.
        if r.status_code == 429 and attempt < _RETRIES:
            time.sleep(3 * (attempt + 1))
            continue
        break
    return {"status": last, "text": ""}  # non-200 -> caller skips this page


def fetch(calls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Fetch each call's URL through Jina Reader, preserving order."""
    if not calls:
        return []
    workers = max(1, min(_CONCURRENCY, len(calls)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(_one, calls))
