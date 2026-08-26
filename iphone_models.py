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
