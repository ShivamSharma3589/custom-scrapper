"""Classifying promotional copy.

Retailer-agnostic: the wording of offers is a retail convention, not a
property of any one shop, and both adapters use these. They live here rather
than in an adapter so that adding a retailer never means copying this logic.
"""

from __future__ import annotations

import re

from .models import (
    PROMO_BUNDLE,
    PROMO_CODE,
    PROMO_GIFT,
    PROMO_OTHER,
    PROMO_PERCENTAGE,
    PROMO_SPEND_THRESHOLD,
)

# A discount can be written "25%" or "25 percent"; both appear in the wild.
_PERCENT = r"(?:\d+\s*%|\d+\s*per\s?cent)"

# Words and shapes that indicate text is an actual offer rather than a
# merchandising badge. Kept deliberately broad -- the cost of letting an
# oddity through is one reviewable row, while being too strict silently loses
# real campaigns.
_OFFER_SIGNAL_RE = re.compile(
    rf"{_PERCENT}|\boff\b|\bsave\b|\bfree\b|\bspend\b|\bgift\b|\bcode\b|"
    r"\bbuy\s*\d|\bdiscount\b|\bhalf price\b|\bbogof\b",
    re.IGNORECASE,
)

# "Spend £40 and receive X" and "Receive X when you spend £40" are the same
# offer, so the two words are matched in either order.
_SPEND_RE = re.compile(
    r"\bspend\b.*\b(get|save|receive|choose)\b|\b(get|save|receive|choose)\b.*\bspend\b",
    re.IGNORECASE | re.DOTALL,
)

_BUNDLE_RE = re.compile(r"\bbuy\s*\d+\b.*\bget\b|\b\d\s*for\s*\d\b", re.IGNORECASE)
_EXPLICIT_GIFT_RE = re.compile(
    r"\bfree gift\b|\bgift with purchase\b|\bcomplimentary\b", re.IGNORECASE
)
# "free next day delivery" is a shipping term, not a promotion on the goods.
# The words may be separated, so allow a short gap.
_FREE_SHIPPING_RE = re.compile(
    r"\bfree\b[\w\s]{0,15}\b(delivery|shipping|returns?|postage)\b", re.IGNORECASE
)
_PERCENT_RE = re.compile(_PERCENT, re.IGNORECASE)
# "half price" / "1/2 price" -- a percentage discount with no number in it.
_FRACTION_OFF_RE = re.compile(r"\bhalf price\b|\b\d\s*/\s*\d\s*(?:price|off)\b", re.IGNORECASE)
_CODE_RE = re.compile(r"\bcode\b", re.IGNORECASE)


# An optional caller-supplied vocabulary. When set, it REPLACES the built-in
# rules below rather than adding to them, so a run using a keyword file
# matches exactly what that file says and nothing else. None means the
# built-in retail rules are in force, which is the default and the behaviour
# every existing run keeps.
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


# A much stricter test than `looks_like_offer`, for when we are scanning a
# whole page rather than reading a known promo slot. Every alternative below
# names a concrete mechanic -- a percentage, a money saving, a bundle, a code,
# a spend threshold, or an explicit gift-with-purchase.
#
# Bare "gift" and bare "free" are deliberately absent. On a retailer's offers
# page they match "gift finder", "gift cards", "gift by occasion" and "Boots
# Free Online NHS Repeat Prescription Service" -- all navigation, none of them
# promotions.
_CONFIDENT_OFFER_RE = re.compile(
    rf"{_PERCENT}\s*(?:off|discount)"
    rf"|(?:up\s*to|save|extra|flat)\s*{_PERCENT}"
    rf"|{_PERCENT}\s*(?:off|discount)?\s*(?:selected|when you)"
    # "half price", and the fraction form UK retailers also use: "1/2 price",
    # "1/3 off". Boots writes "SAVE UP TO 1/2 PRICE".
    r"|\bhalf price\b|\b\d\s*/\s*\d\s*(?:price|off)\b"
    r"|\bbogof\b|\bbuy\s*\d+\s*get\b|\b\d\s*for\s*\d\b"
    r"|\bfree gift\b|\bgift with purchase\b|\bcomplimentary\b"
    r"|\bspend\b[^.]{0,40}\b(?:get|save|receive|choose)\b"
    # A discount code, but not a catalogue number. John Lewis prints
    # "Product code: 46026731" on every product page, which is an identifier,
    # not an offer.
    r"|\b(?:use|with|enter)\s+code\b"
    r"|(?<!product )(?<!item )(?<!barcode )\bcode:\s*\w+"
    r"|\bsave\s*[£$€]\s*\d",
    re.IGNORECASE,
)


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
