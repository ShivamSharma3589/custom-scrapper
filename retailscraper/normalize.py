"""Retailer-agnostic helpers for cleaning raw scraped strings."""

import html
import re
from typing import Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

_PRICE_RE = re.compile(r"(?P<symbol>[£$€])?\s*(?P<amount>\d[\d,]*(?:\.\d{1,2})?)")

_CURRENCY_BY_SYMBOL = {"£": "GBP", "$": "USD", "€": "EUR"}

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "affil", "sv1", "sv_campaign_id", "awc", "cjevent",
    "shareToken", "switchcurrency", "shippingcountry",
}


def parse_price(raw: Optional[str]) -> Tuple[Optional[float], Optional[str]]:
    """Turn a price string into (amount, currency_code)."""
    if not raw:
        return None, None

    match = _PRICE_RE.search(raw.replace("\xa0", " "))
    if not match:
        return None, None

    amount = float(match.group("amount").replace(",", ""))
    currency = _CURRENCY_BY_SYMBOL.get(match.group("symbol") or "")
    return amount, currency


def clean_text(raw: Optional[str]) -> Optional[str]:
    """Collapse whitespace, decode HTML entities, return None if empty."""
    if raw is None:
        return None
    collapsed = " ".join(html.unescape(raw).split())
    return collapsed or None


_CALL_TO_ACTION_RE = re.compile(
    r"\s*\b(SHOP NOW|SHOP ALL|FIND OUT MORE|BUY NOW|LEARN MORE|DISCOVER MORE|SEE MORE|VIEW ALL)\b.*$",
    re.DOTALL,
)
_TRAILING_TABS_RE = re.compile(r"(?=(?:\s+[A-Z0-9&/'’.\-]+)*\s+[A-Z])(?:\s+[A-Z0-9&/'’.\-]+)+$")


def strip_call_to_action(text: Optional[str]) -> Optional[str]:
    """Offer copy without the button text scraped along with it."""
    if not text:
        return text
    match = _CALL_TO_ACTION_RE.search(text)
    if not match:
        return text
    kept = text[:match.start()]
    if match.group(1) in ("SHOP ALL", "VIEW ALL") and re.search(r"[a-z]", kept):
        kept = _TRAILING_TABS_RE.sub("", kept)
    return kept.rstrip(" |-–—:,") or text


def canonical_url(url: str) -> str:
    """Normalise a URL so equivalent addresses compare equal."""
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
        "",
    ))


def percent_off(original: Optional[float], current: Optional[float]) -> Optional[float]:
    """Compute the discount percentage, rounded to 2dp."""
    if original is None or current is None or original <= 0:
        return None
    return round((original - current) / original * 100, 2)


def extract_promo_code(text: Optional[str]) -> Optional[str]:
    """Pull a discount code out of promotional copy, if one is present."""
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
    return code if code.isupper() else None
