"""Classifying promotional copy."""

import re
from typing import Optional

from .models import (
    PROMO_AMOUNT,
    PROMO_BUNDLE,
    PROMO_CODE,
    PROMO_GIFT,
    PROMO_OTHER,
    PROMO_PERCENTAGE,
    PROMO_PRICE_MATCH,
    PROMO_SALE,
    PROMO_SPEND_THRESHOLD,
)

_PERCENT = r"(?:\d+\s*%|\d+\s*per\s?cent)"

_OFFER_SIGNAL_RE = re.compile(
    rf"{_PERCENT}|\boff\b|\bsave\b|\bfree\b|\bspend\b|\bgift\b|\bcode\b|"
    r"\bbuy\s*\d|\bdiscount\b|\bhalf price\b|\bbogof\b",
    re.IGNORECASE,
)

_SPEND_RE = re.compile(
    r"\bspend\b.*\b(get|save|receive|choose)\b|\b(get|save|receive|choose)\b.*\bspend\b",
    re.IGNORECASE | re.DOTALL,
)

_BUNDLE_RE = re.compile(r"\bbuy\s*\d+\b.*\bget\b|\b\d\s*for\s*\d\b", re.IGNORECASE)
_EXPLICIT_GIFT_RE = re.compile(
    r"\bfree gift\b|\bgift with purchase\b|\bcomplimentary\b", re.IGNORECASE
)
_FREE_SHIPPING_RE = re.compile(
    r"\bfree\b[\w\s]{0,15}\b(delivery|shipping|returns?|postage)\b", re.IGNORECASE
)
_PERCENT_RE = re.compile(_PERCENT, re.IGNORECASE)
_FRACTION_OFF_RE = re.compile(r"\bhalf price\b|\b\d\s*/\s*\d\s*(?:price|off)\b", re.IGNORECASE)
_CODE_RE = re.compile(r"\bcode\b", re.IGNORECASE)
_AMOUNT_RE = re.compile(
    r"\bsave\s+(?:up\s+to\s+)?[£$€]\s?\d|[£$€]\s?\d+(?:\.\d+)?\s*off\b"
    r"|\b(?:only|worth|for)\s+[£$€]\s?\d",
    re.IGNORECASE,
)
_PRICE_MATCH_RE = re.compile(r"\bprice\s+match", re.IGNORECASE)
_SALE_RE = re.compile(r"\bsale\b|\breduced\b|\bclearance\b|\boutlet\b", re.IGNORECASE)


_custom_keywords = None


def set_offer_keywords(keyword_set) -> None:
    """Replace the built-in offer vocabulary for this process."""
    global _custom_keywords
    _custom_keywords = keyword_set


def active_keywords():
    """The custom vocabulary in force, or None when using the built-in rules."""
    return _custom_keywords


def looks_like_offer(text: str) -> bool:
    """True when the copy plausibly describes a promotion."""
    if _custom_keywords is not None:
        return _custom_keywords.matches(text)
    return bool(_OFFER_SIGNAL_RE.search(text))


_CONFIDENT_OFFER_RE = re.compile(
    rf"{_PERCENT}\s*(?:off|discount)"
    rf"|(?:up\s*to|save|extra|flat)\s*{_PERCENT}"
    rf"|{_PERCENT}\s*(?:off|discount)?\s*(?:selected|when you)"
    r"|\bhalf price\b|\b\d\s*/\s*\d\s*(?:price|off)\b"
    r"|\bbogof\b|\bbuy\s*\d+\s*get\b|\b\d\s*for\s*\d\b"
    r"|\bfree gift\b|\bgift with purchase\b|\bcomplimentary\b"
    r"|\bspend\b[^.]{0,40}\b(?:get|save|receive|choose)\b"
    r"|\b(?:use|with|enter)\s+code\b"
    r"|(?<!product )(?<!item )(?<!barcode )\bcode:\s*\w+"
    r"|\bsave\s*[£$€]\s*\d",
    re.IGNORECASE,
)


_BARE_TIER_RE = re.compile(
    rf"^(?:at\s+least\s+|up\s+to\s+|save\s+)?{_PERCENT}\s*(?:off|discount)?$",
    re.IGNORECASE,
)

_DEAL_BADGE_RE = re.compile(
    rf"{_PERCENT}\s*off\s+(?:limited\s+time\s+deal|deal\s+of\s+the\s+day"
    r"|ends?\s+in\b|prime\s+exclusive)",
    re.IGNORECASE,
)

_FACET_URL_RE = re.compile(
    r"/(?:offers-)?(?:save|outlet|auto)[-/]?\d+|/save-\d+|/offers-save-\d+",
    re.IGNORECASE,
)


def is_browse_facet(text: str, url: Optional[str] = None) -> bool:
    """True when this "offer" is really a shop-by-discount filter."""
    if not text:
        return False

    if _DEAL_BADGE_RE.search(text):
        return True

    bare = bool(_BARE_TIER_RE.match(text.strip()))
    if not bare:
        return bool(url and _FACET_URL_RE.search(url) and _PERCENT_RE.search(text)
                    and len(text.split()) <= 5)

    if url:
        return bool(_FACET_URL_RE.search(url))
    return True


def is_confident_offer(text: str) -> bool:
    """True only when the copy names a concrete promotional mechanic."""
    if not text:
        return False

    if _custom_keywords is not None:
        return _custom_keywords.matches(text)

    if _FREE_SHIPPING_RE.search(text):
        return False
    return bool(_CONFIDENT_OFFER_RE.search(text))


def classify_promotion(text: str) -> str:
    """Bucket promotional copy into a machine-readable type."""
    if _SPEND_RE.search(text):
        return PROMO_SPEND_THRESHOLD
    if _BUNDLE_RE.search(text):
        return PROMO_BUNDLE
    if _EXPLICIT_GIFT_RE.search(text):
        return PROMO_GIFT
    if re.search(r"\bfree\b", text, re.IGNORECASE) and not _FREE_SHIPPING_RE.search(text):
        return PROMO_GIFT
    if _PRICE_MATCH_RE.search(text):
        return PROMO_PRICE_MATCH
    if _CODE_RE.search(text) and (_PERCENT_RE.search(text) or _AMOUNT_RE.search(text)):
        return PROMO_CODE
    if _PERCENT_RE.search(text):
        return PROMO_PERCENTAGE
    if _FRACTION_OFF_RE.search(text):
        return PROMO_PERCENTAGE
    if _AMOUNT_RE.search(text):
        return PROMO_AMOUNT
    if _SALE_RE.search(text):
        return PROMO_SALE
    return PROMO_OTHER
