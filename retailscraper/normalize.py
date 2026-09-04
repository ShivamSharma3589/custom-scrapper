"""Retailer-agnostic helpers for cleaning raw scraped strings.

Nothing here knows anything about a specific retailer -- these are the
generic "turn messy page text into typed data" utilities.
"""

from __future__ import annotations

import html
import re
from typing import Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

# Matches a currency amount such as "£42.00", "42.00", "£1,299.99".
_PRICE_RE = re.compile(r"(?P<symbol>[£$€])?\s*(?P<amount>\d[\d,]*(?:\.\d{1,2})?)")

_CURRENCY_BY_SYMBOL = {"£": "GBP", "$": "USD", "€": "EUR"}

# Tracking / session parameters that never change what a page shows. Stripping
# them is what makes deduplication work: the same product reached from a
# category, a brand page and an email link must collapse to one record.
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "affil", "sv1", "sv_campaign_id", "awc", "cjevent",
    "shareToken", "switchcurrency", "shippingcountry",
}


def parse_price(raw: Optional[str]) -> Tuple[Optional[float], Optional[str]]:
    """Turn a price string into (amount, currency_code).

    Returns (None, None) when the string holds no recognisable amount, rather
    than raising -- a missing price is a data condition to be validated, not
    an exception.

    >>> parse_price("£42.00")
    (42.0, 'GBP')
    """
    if not raw:
        return None, None

    match = _PRICE_RE.search(raw.replace("\xa0", " "))
    if not match:
        return None, None

    amount = float(match.group("amount").replace(",", ""))
    currency = _CURRENCY_BY_SYMBOL.get(match.group("symbol") or "")
    return amount, currency


def clean_text(raw: Optional[str]) -> Optional[str]:
    """Collapse whitespace, decode HTML entities, return None if empty.

    Page text arrives full of newlines and runs of spaces from the HTML
    source; promotional copy in particular is unusable without this. Entities
    are decoded because some retailers double-escape values inside data
    attributes, which otherwise reaches the output as
    "Wood Sage &amp; Sea Salt".
    """
    if raw is None:
        return None
    collapsed = " ".join(html.unescape(raw).split())
    return collapsed or None


def canonical_url(url: str) -> str:
    """Normalise a URL so equivalent addresses compare equal.

    Drops the fragment, removes tracking query parameters, and lowercases the
    host. The path is left alone -- on many retailers the path carries the
    product id we depend on for identity.
    """
    parts = urlsplit(url)

    kept = []
    for pair in parts.query.split("&"):
        if not pair:
            continue
        name = pair.split("=", 1)[0]
        if name not in _TRACKING_PARAMS:
            kept.append(pair)

    return urlunsplit((
        parts.scheme,
        parts.netloc.lower(),
        parts.path,
        "&".join(kept),
        "",  # always drop the fragment
    ))


def percent_off(original: Optional[float], current: Optional[float]) -> Optional[float]:
    """Compute the discount percentage, rounded to 2dp.

    Returns None when the inputs cannot express a discount. We deliberately do
    NOT clamp or absolute-value a negative result -- an "original" price below
    the current price is a data problem for the validator to catch, not
    something to quietly paper over.
    """
    if original is None or current is None or original <= 0:
        return None
    return round((original - current) / original * 100, 2)


def extract_promo_code(text: Optional[str]) -> Optional[str]:
    """Pull a discount code out of promotional copy, if one is present.

    Handles the common retail phrasings: "Use Code: EXTRA10",
    "with code SAVE20", "Code - WELCOME".
    """
    if not text:
        return None
    match = re.search(
        r"\bcode\b\s*[:\-]?\s*([A-Z0-9][A-Z0-9_-]{2,24})",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    code = match.group(1)
    # A real code is written in caps; this rejects prose like "code to".
    return code if code.isupper() else None
