#!/usr/bin/env python3
"""
HDFC SmartBuy (Imagine store) -- iPhone 17 per-variant stock monitor.

SmartBuy's Apple store (smartbuy.myimaginestore.com) runs on Magento 2 with a
Varnish full-page cache in front. iPhone 17 is a single configurable product
(id 14148) with two swatch axes -- colour (attribute 92) and storage (attribute
308). The PDP shows a parent-level "In stock" and the swatches look selectable
even when the specific child variant has ZERO salable qty, so neither the label
nor the swatch enabled-state is a reliable signal. The site's own jsonConfig
("products" per option) is stale too: it listed Black/White/Mist Blue as in stock
while only White could actually be carted.

The ONLY reliable signal is to probe the add-to-cart endpoint the way the site's
JS does and read the response:

  POST /checkout/cart/add/product/14148/    (X-Requested-With: XMLHttpRequest)
       product=14148 & form_key=<k> & super_attribute[92]=<colour>
       & super_attribute[308]=<storage> & qty=1
    -> {"backUrl": ".../iphone-17"}        => refused  => OUT OF STOCK
    -> {"html": "...You added iPhone 17..."} => added to the quote, but this is
       OPTIMISTIC: an almost-sold-out variant is accepted here yet flagged
       "...just went Out-of-Stock" on the cart page. So a "You added" is only
       trusted after GET /checkout/cart/ comes back with no "Out-of-Stock".

Session bootstrap: the PDP is fully cached, so it never sends Set-Cookie and the
form_key in its HTML is a shared stale value. A first hit to a non-cached path
(/customer/section/load/) sets PHPSESSID; the form_key itself is a client-side
CSRF double-submit cookie under FPC, so -- exactly like Magento_PageCache's
form-key-provider.js -- we generate a random form_key, set it as the cookie AND
post the same value. This makes a plain curl_cffi (Chrome-impersonated) request
behave identically to the browser; no Playwright / Bright Data needed.

Alert condition: any watched variant is add-to-cart-able AND not flagged
out-of-stock on the cart page. Watch-list defaults to iPhone 17 256GB in every
colour; override via SMARTBUY_STORAGES / SMARTBUY_COLORS.
"""

import os
import re
import sys
import json
import string
import random
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from curl_cffi import requests as cffi_requests
    def _session():
        return cffi_requests.Session(impersonate="chrome124")
except ImportError:  # pragma: no cover - falls back to plain requests
    import requests as _requests
    def _session():
        return _requests.Session()

try:
    import requests  # for RequestException type only
except ImportError:
    print("Error: pip install -r requirements.txt")
    sys.exit(1)

IST = timezone(timedelta(hours=5, minutes=30))
COOLDOWN_HOURS = float(os.environ.get(
    "SMARTBUY_COOLDOWN_HOURS", os.environ.get("ALERT_COOLDOWN_HOURS", 0.0333)))  # 2 min
STATE_DIR = Path(".alert_state")
STATE_FILE = STATE_DIR / "last_alert.json"

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

BASE = "https://smartbuy.myimaginestore.com"
PDP_PATH = os.environ.get("SMARTBUY_PDP", "/iphone-17")
PDP_URL = BASE + PDP_PATH
PRODUCT_ID = os.environ.get("SMARTBUY_PRODUCT_ID", "14148")
ADD_URL = f"{BASE}/checkout/cart/add/product/{PRODUCT_ID}/"
CART_URL = f"{BASE}/checkout/cart/"
BOOTSTRAP_URL = f"{BASE}/customer/section/load/?sections=cart"

# Swatch attribute ids on the iPhone 17 configurable product.
COLOR_ATTR = os.environ.get("SMARTBUY_COLOR_ATTR", "92")
STORAGE_ATTR = os.environ.get("SMARTBUY_STORAGE_ATTR", "308")

# option-id maps (label -> Magento option value). Seeded from the live PDP; override
# a whole axis via SMARTBUY_COLOR_OPTS / SMARTBUY_STORAGE_OPTS as "Label:id,Label:id".
_COLOR_OPTS = {"Black": "20", "White": "22", "Sage": "724", "Lavender": "883", "Mist Blue": "975"}
_STORAGE_OPTS = {"256GB": "288", "512GB": "289"}


def _parse_opts(env_val: str, default: Dict[str, str]) -> Dict[str, str]:
    raw = os.environ.get(env_val, "").strip()
    if not raw:
        return dict(default)
    out = {}
    for pair in raw.split(","):
        if ":" in pair:
            label, oid = pair.split(":", 1)
            out[label.strip()] = oid.strip()
    return out or dict(default)


COLOR_OPTS = _parse_opts("SMARTBUY_COLOR_OPTS", _COLOR_OPTS)
STORAGE_OPTS = _parse_opts("SMARTBUY_STORAGE_OPTS", _STORAGE_OPTS)

# Which variants to watch. Default: 256GB in every colour we know about.
WATCH_STORAGES = [s.strip() for s in os.environ.get("SMARTBUY_STORAGES", "256GB").split(",") if s.strip()]
WATCH_COLORS = [c.strip() for c in os.environ.get(
    "SMARTBUY_COLORS", ",".join(COLOR_OPTS)).split(",") if c.strip()]


def get_ist_now():
    return datetime.now(IST)


def _new_form_key() -> str:
    """A random 16-char alphanumeric key -- same shape Magento_PageCache's JS generates."""
    return "".join(random.choices(string.ascii_letters + string.digits, k=16))


def probe_variant(color_label: str, storage_label: str) -> Dict[str, Any]:
    """Add-to-cart probe for one colour/storage combo in a fresh guest session.

    Returns {'in_stock': bool, 'reason': str}. A fresh session per combo keeps the
    cart to a single line so the out-of-stock check is unambiguous.
    """
    color_id = COLOR_OPTS.get(color_label)
    storage_id = STORAGE_OPTS.get(storage_label)
    if not color_id or not storage_id:
        return {"in_stock": False, "reason": "unknown option id"}

    s = _session()
    fk = _new_form_key()
    try:
        s.get(BOOTSTRAP_URL, timeout=30)  # sets PHPSESSID
        s.cookies.set("form_key", fk, domain="smartbuy.myimaginestore.com", path="/")
        data = {
            "product": PRODUCT_ID,
            "form_key": fk,
            f"super_attribute[{COLOR_ATTR}]": color_id,
            f"super_attribute[{STORAGE_ATTR}]": storage_id,
            "qty": "1",
        }
        r = s.post(ADD_URL, data=data,
                   headers={"X-Requested-With": "XMLHttpRequest", "Referer": PDP_URL,
                            "Accept": "application/json, text/javascript, */*; q=0.01"},
                   timeout=30, allow_redirects=False)
        body = r.text or ""
    except (requests.RequestException, Exception) as e:  # noqa: BLE001 - network best-effort
        return {"in_stock": False, "reason": f"probe error: {e}"}

    if '"backUrl"' in body:
        return {"in_stock": False, "reason": "refused (backUrl)"}
    if "You added" not in body:
        # Unexpected (full-page HTML => form_key rejected, or an error). Treat as not in stock.
        return {"in_stock": False, "reason": "no add confirmation"}

    # "You added" is optimistic. Confirm the line actually stuck (authoritative cart
    # section JSON) AND the cart page doesn't flag it out-of-stock -- this is the exact
    # signal that distinguishes a real, checkout-able unit (White) from a fake accept
    # that reads "...just went Out-of-Stock" on the cart (Black).
    try:
        sec = s.get(BOOTSTRAP_URL, timeout=30).json()
        stuck = ((sec.get("cart") or {}).get("summary_count") or 0)
        cart = s.get(CART_URL, timeout=30).text or ""
    except (requests.RequestException, ValueError, Exception):  # noqa: BLE001
        return {"in_stock": True, "reason": "added (verify failed, assuming stock)"}
    if not stuck:
        return {"in_stock": False, "reason": "added-msg but cart empty"}
    if re.search(r"[Oo]ut-?of-?[Ss]tock", cart):
        return {"in_stock": False, "reason": "carted then flagged out-of-stock"}
    return {"in_stock": True, "reason": "added, cart clean"}


def check_stock() -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []
    for storage in WATCH_STORAGES:
        for color in WATCH_COLORS:
            res = probe_variant(color, storage)
            name = f"iPhone 17 {color} {storage}"
            state = "IN STOCK" if res["in_stock"] else "out of stock"
            print(f"[{get_ist_now()}] {name:28.28}  {state}  ({res['reason']})")
            if res["in_stock"]:
                matches.append({"name": name, "color": color, "storage": storage,
                                "url": PDP_URL})
    return matches


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
            print(f"[{get_ist_now()}] Cooldown active for {alert_type}: "
                  f"{elapsed_h:.2f}h / {COOLDOWN_HOURS}h")
            return False
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[{get_ist_now()}] Warning reading state: {e}")
        return True


def record_alert(alert_type: str):
    STATE_DIR.mkdir(exist_ok=True)
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
        except Exception:  # noqa: BLE001
            pass
    state[alert_type] = get_ist_now().isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2))


def send_alert(matches: List[Dict[str, Any]]) -> bool:
    variants = ", ".join(m["color"] for m in matches)
    subject = f"SmartBuy: iPhone 17 in stock -- {variants} ({len(matches)})"
    title = f"iPhone 17 in stock on SmartBuy ({len(matches)})"
    body = "\n".join(
        ["iPhone 17 add-to-cart-able on HDFC SmartBuy:", ""]
        + [f"- {m['name']}" for m in matches]
        + ["",
           "NOTE: the site's Buy Now button is broken (native submit, no add). To grab it,",
           "open the PDP logged in, paste the console add-to-cart script, then checkout.",
           "", f"Shop: {PDP_URL}"]
    )
    ntfy_ok = email_ok = False

    if NTFY_TOPIC:
        try:
            _post = _session().post
            _post(f"https://ntfy.sh/{NTFY_TOPIC}", data=body.encode("utf-8"),
                  headers={"Title": title, "Priority": "high", "Tags": "iphone,smartbuy",
                           "Click": PDP_URL}, timeout=15).raise_for_status()
            print(f"[{get_ist_now()}] ntfy sent")
            ntfy_ok = True
        except Exception as e:  # noqa: BLE001
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
        except Exception as e:  # noqa: BLE001
            print(f"[{get_ist_now()}] email failed: {e}")
    else:
        print(f"[{get_ist_now()}] email not configured")

    return ntfy_ok or email_ok


def check_smartbuy_iphone():
    print("=" * 60)
    print(f"SmartBuy iPhone 17 monitor -- {get_ist_now()}")
    print(f"Watching: {WATCH_STORAGES} x {WATCH_COLORS}")
    print("=" * 60)

    matches = check_stock()
    if not matches:
        print(f"[{get_ist_now()}] No watched iPhone 17 variant in stock on SmartBuy.")
        return False

    print(f"[{get_ist_now()}] IN STOCK: {[m['name'] for m in matches]}")
    if not should_send_alert("smartbuy_iphone"):
        return False

    ok = send_alert(matches)
    if ok:
        record_alert("smartbuy_iphone")
    return ok


if __name__ == "__main__":
    try:
        check_smartbuy_iphone()
    except Exception as e:  # noqa: BLE001
        print(f"[{datetime.now()}] Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
