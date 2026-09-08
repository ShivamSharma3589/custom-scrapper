"""Classifying promotional copy.

Retailer-agnostic: the wording of offers is a retail convention, not a
property of any one shop, and both adapters use these. They live here rather
than in an adapter so that adding a retailer never means copying this logic.
"""

import re
from typing import Optional

from .models import (
    PROMO_BUNDLE,
    PROMO_CODE,
    PROMO_GIFT,
    PROMO_OTHER,
    PROMO_PERCENTAGE,
    PROMO_SPEND_THRESHOLD,
)

_PERCENT = r"(?:\d+\s*%|\d+\s*per\s?cent)"

# Deliberately broad: a stray match costs one reviewable row, while being
# too strict silently loses real campaigns.
_OFFER_SIGNAL_RE = re.compile(
    rf"{_PERCENT}|\boff\b|\bsave\b|\bfree\b|\bspend\b|\bgift\b|\bcode\b|"
    r"\bbuy\s*\d|\bdiscount\b|\bhalf price\b|\bbogof\b",
    re.IGNORECASE,
)

# The same offer either way round, so both orders match.
_SPEND_RE = re.compile(
    r"\bspend\b.*\b(get|save|receive|choose)\b|\b(get|save|receive|choose)\b.*\bspend\b",
    re.IGNORECASE | re.DOTALL,
)

_BUNDLE_RE = re.compile(r"\bbuy\s*\d+\b.*\bget\b|\b\d\s*for\s*\d\b", re.IGNORECASE)
_EXPLICIT_GIFT_RE = re.compile(
    r"\bfree gift\b|\bgift with purchase\b|\bcomplimentary\b", re.IGNORECASE
)
# "free next day delivery" is shipping, not a promotion on the goods.
_FREE_SHIPPING_RE = re.compile(
    r"\bfree\b[\w\s]{0,15}\b(delivery|shipping|returns?|postage)\b", re.IGNORECASE
)
_PERCENT_RE = re.compile(_PERCENT, re.IGNORECASE)
_FRACTION_OFF_RE = re.compile(r"\bhalf price\b|\b\d\s*/\s*\d\s*(?:price|off)\b", re.IGNORECASE)
_CODE_RE = re.compile(r"\bcode\b", re.IGNORECASE)


# A caller-supplied vocabulary REPLACES the built-in rules rather than
# adding to them. None means the built-in retail rules are in force.
_custom_keywords = None


def set_offer_keywords(keyword_set) -> None:
    """Replace the built-in offer vocabulary for this process.

    Pass a `KeywordSet` from `keywords.load_keywords`, or None to restore the
    built-in rules. Both offer tests consult it, so a custom vocabulary
    applies to product banners and open page scans alike.
    """
    global _custom_keywords
    _custom_keywords = keyword_set


def active_keywords():
    """The custom vocabulary in force, or None when using the built-in rules."""
    return _custom_keywords


def looks_like_offer(text: str) -> bool:
    """True when the copy plausibly describes a promotion.

    Filters out badges like "NEW IN" or "BESTSELLER", which sit in the same
    markup as real offers but promise nothing.
    """
    if _custom_keywords is not None:
        return _custom_keywords.matches(text)
    return bool(_OFFER_SIGNAL_RE.search(text))


# Stricter than `looks_like_offer`, for scanning a whole page rather than a
# known promo slot. Every alternative names a concrete mechanic.
#
# Bare "gift" and "free" are deliberately absent: on an offers page they
# match "gift finder", "gift cards" and "Boots Free Online NHS Repeat
# Prescription Service" -- all navigation.
_CONFIDENT_OFFER_RE = re.compile(
    rf"{_PERCENT}\s*(?:off|discount)"
    rf"|(?:up\s*to|save|extra|flat)\s*{_PERCENT}"
    rf"|{_PERCENT}\s*(?:off|discount)?\s*(?:selected|when you)"
    # the fraction form UK retailers use: Boots writes "SAVE UP TO 1/2 PRICE"
    r"|\bhalf price\b|\b\d\s*/\s*\d\s*(?:price|off)\b"
    r"|\bbogof\b|\bbuy\s*\d+\s*get\b|\b\d\s*for\s*\d\b"
    r"|\bfree gift\b|\bgift with purchase\b|\bcomplimentary\b"
    r"|\bspend\b[^.]{0,40}\b(?:get|save|receive|choose)\b"
    # a discount code, not a catalogue number -- John Lewis prints
    # "Product code: 46026731" on every page
    r"|\b(?:use|with|enter)\s+code\b"
    r"|(?<!product )(?<!item )(?<!barcode )\bcode:\s*\w+"
    r"|\bsave\s*[£$€]\s*\d",
    re.IGNORECASE,
)


# A bare tier ("50% Off", "At Least 30% off") is a shop-by-saving FILTER,
# not an offer -- it promises nothing beyond what each price already says.
_BARE_TIER_RE = re.compile(
    rf"^(?:at\s+least\s+|up\s+to\s+|save\s+)?{_PERCENT}\s*(?:off|discount)?$",
    re.IGNORECASE,
)

# A discount attached to one product rather than to a campaign: a countdown
# ("Ends in 04:12:23") or a marketplace deal label. These are prices, not
# promotions, and they carry no scope.
_DEAL_BADGE_RE = re.compile(
    rf"{_PERCENT}\s*off\s+(?:limited\s+time\s+deal|deal\s+of\s+the\s+day"
    r"|ends?\s+in\b|prime\s+exclusive)",
    re.IGNORECASE,
)

# Collection slugs a retailer uses for those filters.
_FACET_URL_RE = re.compile(
    r"/(?:offers-)?(?:save|outlet|auto)[-/]?\d+|/save-\d+|/offers-save-\d+",
    re.IGNORECASE,
)


def is_browse_facet(text: str, url: Optional[str] = None) -> bool:
    """True when this "offer" is really a shop-by-discount filter.

    A product discounted 29% had "At Least 70% Off" attached to it, along
    with every other tier, because each tier was recorded as a site-wide
    campaign and site-wide campaigns apply to everything. The tiers are not
    campaigns at all -- they are the retailer's navigation.

    Requires the text to be a bare tier with nothing else in it, so a real
    campaign that happens to contain a percentage ("LF Seasonal Sale | Up To
    50% Off", "Up To 20% Off + Extra 10% Off Selected | Use Code: EXTRA10")
    is untouched.
    """
    if not text:
        return False

    # A per-product deal badge: the discount on one item, with no campaign
    # named and nothing to scope it to. Amazon's deals page is built from
    # these -- "15% off Limited time deal", "27% off Ends in 04:12:23" --
    # and recording them made 32 site-wide campaigns out of 32 individual
    # products' prices.
    if _DEAL_BADGE_RE.search(text):
        return True

    bare = bool(_BARE_TIER_RE.match(text.strip()))
    if not bare:
        # A tier-shaped destination corroborates it even when the wording
        # varies.
        return bool(url and _FACET_URL_RE.search(url) and _PERCENT_RE.search(text)
                    and len(text.split()) <= 5)

    # A bare tier with a link is a filter only when the link is one: an
    # offers hub's own banner reads exactly the same as a shop-by-saving
    # tier, and ASOS's outlet headline really is "Up to 40% off". Judging on
    # the wording alone discarded it along with the filters.
    if url:
        return bool(_FACET_URL_RE.search(url))
    return True


def is_confident_offer(text: str) -> bool:
    """True only when the copy names a concrete promotional mechanic.

    Use this when scanning arbitrary page text -- an offers hub, a navigation
    menu -- where most of what you see is not a promotion. `looks_like_offer`
    is the right test in the opposite situation: reading a slot the retailer
    reserves for offers, where the question is only whether this particular
    one is a badge rather than a deal.

    Shipping terms are excluded: "free next day delivery" is a delivery
    policy, not a promotion on the goods.
    """
    if not text:
        return False

    # A custom vocabulary replaces these rules entirely -- including the
    # shipping exclusion, which is a fact about retail wording and may be
    # exactly what another vertical wants to match.
    if _custom_keywords is not None:
        return _custom_keywords.matches(text)

    if _FREE_SHIPPING_RE.search(text):
        return False
    return bool(_CONFIDENT_OFFER_RE.search(text))


def classify_promotion(text: str) -> str:
    """Bucket promotional copy into a machine-readable type.

    Deliberately conservative: anything we cannot confidently classify becomes
    `other` rather than being forced into a category. Order matters -- a
    "spend £50 get 20% off" is a spend threshold first and a percentage
    second, because the threshold is the condition the shopper has to meet.
    """
    if _SPEND_RE.search(text):
        return PROMO_SPEND_THRESHOLD
    if _BUNDLE_RE.search(text):
        return PROMO_BUNDLE
    if _EXPLICIT_GIFT_RE.search(text):
        return PROMO_GIFT
    if re.search(r"\bfree\b", text, re.IGNORECASE) and not _FREE_SHIPPING_RE.search(text):
        return PROMO_GIFT
    if _CODE_RE.search(text) and _PERCENT_RE.search(text):
        return PROMO_CODE
    if _PERCENT_RE.search(text):
        return PROMO_PERCENTAGE
    # "Half Price Fragrance" and "SAVE UP TO 1/2 PRICE" are percentage
    # discounts written without a number.
    if _FRACTION_OFF_RE.search(text):
        return PROMO_PERCENTAGE
    return PROMO_OTHER
