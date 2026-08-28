#!/usr/bin/env python3
"""Shared helper to name which iPhone model(s) an alert covers.

Used by the multi-model stock monitors (Croma, BigBasket, Blinkit, Reliance
Digital, JioMart) so the email subject / push title reads "iPhone 15, 17"
instead of a bare "iPhone -- N variant(s)". Reports the model number only
(15 / 16 / 17); colour and Pro/Plus variants are left out of the subject.
"""

import re
from typing import Iterable

_MODEL_RE = re.compile(r"i[pP]hone\s*(\d{1,2})")

# Non-handset listings that share the word "iPhone" (accessories/add-ons).
_ACCESSORY = re.compile(
    r"\b(case|cover|strap|glass|protector|charger|cable|adapter|screen|guard|"
    r"skin|holder|mount|stand|airpod|band|tempered|wallet|magsafe|battery|"
    r"power|pouch|sleeve|lens|film|dock|grip)\b", re.I)


def is_base_handset(name: str, models: Iterable[str]) -> bool:
    """True only for a base iPhone handset in one of `models` (e.g. ["15","16","17"]).

    Excludes Plus / Pro / Pro Max / Air / mini / e and every accessory (case, charger,
    ...). Requires a storage token (GB/TB) so title-less listing chaff is dropped.
    Shared by the multi-model stock monitors so the "base only" rule lives in one place.
    """
    name = name or ""
    if not re.search(r"\bi[pP]hone\b", name, re.I):
        return False
    if _ACCESSORY.search(name):
        return False
    if not re.search(r"\d+\s*(GB|TB)\b", name, re.I):
        return False
    mlist = [str(m).strip() for m in models if str(m).strip()]
    if not mlist:
        return False
    pattern = (r"\bi[pP]hone\s*(" + "|".join(map(re.escape, mlist)) +
               r")\b(?!\s*(?:plus|pro|max|air|mini))")
    return bool(re.search(pattern, name, re.I))


def models_summary(names: Iterable[str]) -> str:
    """Distinct iPhone model numbers in first-seen order.

    ["Apple iPhone 15 (128GB, Blue)", "Apple iPhone 17 (256GB)"] -> "iPhone 15, 17"
    Falls back to "iPhone" when no model number is found.
    """
    seen: set = set()
    nums = []
    for n in names:
        m = _MODEL_RE.search(n or "")
        if not m:
            continue
        num = m.group(1)
        if num not in seen:
            seen.add(num)
            nums.append(num)
    return "iPhone " + ", ".join(nums) if nums else "iPhone"
