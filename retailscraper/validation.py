"""Generic, retailer-agnostic validation.

The guiding rule of this project: it is better to emit 290 trustworthy
records and 10 explicit rejections than 1000 records of unknown quality. So
every check here answers "can I prove this record describes a real product
state?" -- and when it cannot, the record is rejected WITH a reason rather
than quietly cleaned up.

Each validator takes a `Product` and returns either None (the record is
fine) or a `RejectedRecord` carrying a machine-readable reason.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Callable, List, Optional, Sequence

from .models import Product, RejectedRecord
from .normalize import percent_off

# Machine-readable rejection reasons. Downstream consumers group on these, so
# they are part of the output contract -- don't reword them casually.
REASON_MISSING_PRICE = "missing_price"
REASON_INVALID_DISCOUNT = "invalid_discount"
REASON_DISCOUNT_MISMATCH = "discount_mismatch"
REASON_UNVERIFIED_BRAND = "unverified_brand"
REASON_BRAND_MISMATCH = "brand_mismatch"
REASON_VARIANT_CONFLICT = "variant_conflict"
REASON_MISSING_TITLE = "missing_title"
REASON_WEAK_BRAND = "weak_brand_evidence"

# Provenance values we treat as authoritative: the retailer's own structured
# product data. Anything else is corroboration, not proof.
STRONG_BRAND_EVIDENCE = frozenset({"breadcrumb_link", "microdata_brand", "listing_data"})

# How far the retailer's own advertised discount may drift from the one we
# compute from the two prices before we treat it as a contradiction. A small
# tolerance absorbs the retailer's own rounding (e.g. 25.02% shown as 25%).
DISCOUNT_TOLERANCE_PCT = 1.0


def _reject(product: Product, reason: str, detail: str) -> RejectedRecord:
    """Build a rejection that carries enough context to debug it later."""
    return RejectedRecord(
        reason=reason,
        detail=detail,
        source_url=product.source_url,
        payload={
            "product_title": product.product_title,
            "product_id": product.product_id,
            "sku": product.sku,
            "current_price": product.current_price,
            "original_price": product.original_price,
            "brand": product.brand,
            "variant_count": product.variant_count,
        },
    )


def check_title(product: Product, _targets: Sequence[str]) -> Optional[RejectedRecord]:
    """A record with no title is not usable by anyone downstream."""
    if not product.product_title:
        return _reject(product, REASON_MISSING_TITLE, "No product title could be extracted.")
    return None


def check_price_present(product: Product, _targets: Sequence[str]) -> Optional[RejectedRecord]:
    """A product with no sellable price tells us nothing about discounting."""
    if product.current_price is None:
        return _reject(
            product,
            REASON_MISSING_PRICE,
            "No current price could be extracted from the page.",
        )
    return None


def check_price_pair(product: Product, _targets: Sequence[str]) -> Optional[RejectedRecord]:
    """Reject impossible price pairs.

    An "original" (RRP/was) price below the current price cannot describe a
    discount. This usually means the two numbers were read from unrelated
    parts of the page -- exactly the failure that makes scraper output look
    plausible while being wrong.
    """
    original, current = product.original_price, product.current_price
    if original is None or current is None:
        return None  # nothing to contradict; handled by check_price_present

    if original < current:
        return _reject(
            product,
            REASON_INVALID_DISCOUNT,
            f"Original price {original} is below current price {current}; "
            "these two numbers cannot describe the same discount.",
        )
    return None


def check_discount_consistency(
    product: Product, _targets: Sequence[str]
) -> Optional[RejectedRecord]:
    """The advertised discount must agree with the two prices we extracted.

    If the page says "25% off" but the prices imply 40%, one of the three
    figures came from the wrong element and the record cannot be trusted.
    """
    if product.discount_percent is None:
        return None

    computed = percent_off(product.original_price, product.current_price)
    if computed is None:
        return None

    if abs(computed - product.discount_percent) > DISCOUNT_TOLERANCE_PCT:
        return _reject(
            product,
            REASON_DISCOUNT_MISMATCH,
            f"Stated discount {product.discount_percent}% disagrees with the "
            f"{computed}% implied by {product.original_price} -> {product.current_price}.",
        )
    return None


#: Corrections applied to the brand names a USER types, keyed on the folded,
#: punctuation-stripped form. This normalises the question, never the
#: retailer's answer -- correcting our own input is safe, while "correcting"
#: what a retailer says a product is would be exactly the fuzzy matching that
#: turns Tommy Jeans into Tom Ford.
#:
#: "Bobbie Brown" is here because the original brief was written that way.
#: Every retailer spells it "Bobbi Brown", so the typo silently returned zero
#: products at all six and read as "not stocked".
BRAND_ALIASES = {
    "bobbie brown": "Bobbi Brown",
    "bobby brown": "Bobbi Brown",
    "estee lauder company": "Estee Lauder",
    "esteelauder": "Estee Lauder",
    "mac cosmetics": "MAC",
    "m a c": "MAC",
    "jo malone london": "Jo Malone",
    "too faced cosmetics": "Too Faced",
    "tomford": "Tom Ford",
}


#: Brands this project tracks, in their correct spelling. Used only to
#: suggest a correction when a requested brand returns nothing -- never to
#: restrict what can be asked for. Suggesting from the brands a crawl
#: happened to see is not enough: a retailer that returns nothing for a
#: misspelled brand never reveals the right spelling, so "Clinque" would go
#: uncorrected exactly when the help is needed.
KNOWN_BRANDS = [
    "Clinique",
    "MAC",
    "Tom Ford",
    "Jo Malone",
    "Estee Lauder",
    "Bobbi Brown",
    "Too Faced",
    "La Mer",
    "Aveda",
    "Origins",
    "Bumble and bumble",
    "Le Labo",
    "Aerin",
    "Aramis",
]


def canonical_brand(name: str) -> str:
    """Correct a brand name the user typed, if it is a known variant.

    Returns the name unchanged when it is not a known alias, so an unknown
    brand is still passed through and reported honestly rather than being
    silently rewritten into something else.
    """
    key = re.sub(r"[^a-z0-9 ]+", " ", _fold_accents(name).casefold())
    key = re.sub(r"\s+", " ", key).strip()
    return BRAND_ALIASES.get(key, name)


def suggest_brand(wanted: str, seen: Sequence[str]) -> Optional[str]:
    """The brand from `seen` that `wanted` was most likely meant to be.

    Used when a requested brand returns nothing: comparing it against the
    brands the crawl actually encountered catches a typo without anyone
    having to predict it in advance. Returns None unless the match is close
    enough to be worth suggesting.
    """
    from difflib import SequenceMatcher

    target = _fold_accents(wanted).casefold()
    best, best_score = None, 0.0
    for candidate in seen:
        score = SequenceMatcher(None, target, _fold_accents(candidate).casefold()).ratio()
        if score > best_score:
            best, best_score = candidate, score
    # 0.8 keeps "Bobbie Brown" -> "Bobbi Brown" (0.96) while rejecting
    # "Tom Ford" -> "Tommy Jeans" (0.55).
    return best if best_score >= 0.8 else None


def _fold_accents(text: str) -> str:
    """Strip diacritics so "Estée" and "Estee" compare equal.

    Retailers write brand names with their proper accents; a business asking
    for these brands types them on an ASCII keyboard. Without folding, a
    seven-brand production run rejected 133 genuine Estee Lauder products as
    `brand_mismatch` because Lookfantastic files the brand as "Estée Lauder".
    The same applies to Lancôme, L'Oréal and Kérastase.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _tokens(brand: str) -> List[str]:
    """Split a brand name into comparable lowercase word tokens.

    Runs of single characters are joined back up, so an initialism written
    with separators matches the same name written solid: Boots files MAC as
    "M.A.C", which splits to ['m','a','c'] and would never match ['mac'].
    That cost every MAC product in a seven-brand run -- they were extracted
    correctly, then rejected as the wrong brand.

    Only runs of two or more single characters collapse, so "Jo Malone" and
    "Estee Lauder" are untouched.
    """
    raw = [t for t in re.split(r"[^a-z0-9]+", _fold_accents(brand).casefold()) if t]

    tokens: List[str] = []
    run: List[str] = []
    for token in raw:
        if len(token) == 1:
            run.append(token)
            continue
        if len(run) > 1:
            tokens.append("".join(run))
        elif run:
            tokens.append(run[0])
        run = []
        tokens.append(token)

    if len(run) > 1:
        tokens.append("".join(run))
    elif run:
        tokens.append(run[0])
    return tokens


def match_brand(verified: str, targets: Sequence[str]) -> Optional[str]:
    """Map a retailer's brand name onto the brand we were asked to track.

    Retailers use their own full trading names: the business asks for
    "Jo Malone" while Lookfantastic files it as "Jo Malone London", and "MAC"
    may appear as "MAC Cosmetics". Requiring an exact string match would
    reject every one of those.

    Matching is on whole-word prefixes in either direction, never on raw
    substrings -- "MAC" must match "MAC Cosmetics" without also matching
    "Macadamia". Returns the requested brand name it maps to, or None.
    """
    verified_tokens = _tokens(verified)
    if not verified_tokens:
        return None

    for target in targets:
        target_tokens = _tokens(target)
        if not target_tokens:
            continue
        shorter, longer = sorted((target_tokens, verified_tokens), key=len)
        if longer[: len(shorter)] == shorter:
            return target
    return None


def check_brand(product: Product, targets: Sequence[str]) -> Optional[RejectedRecord]:
    """The product must be provably one of the brands we were asked about.

    A brand appearing in the URL, the page title or nearby marketing copy is
    not proof -- retailer pages routinely mention other brands in
    recommendations, cross-sells and campaign text. The adapter is required to
    say how it established the brand, and an unverified brand is rejected.
    """
    if product.brand_verified_by == "unverified" or not product.brand:
        return _reject(
            product,
            REASON_UNVERIFIED_BRAND,
            "Brand could not be verified from the retailer's own product data.",
        )

    if targets and match_brand(product.brand, targets) is None:
        return _reject(
            product,
            REASON_BRAND_MISMATCH,
            f"Verified brand '{product.brand}' is not among the requested brands "
            f"({', '.join(sorted(targets))}).",
        )
    return None


def check_variant_coherence(
    product: Product, _targets: Sequence[str]
) -> Optional[RejectedRecord]:
    """Catch a SKU that belongs to a different product than the URL.

    A product page selling exactly one variant must report a SKU matching the
    product id in its own URL. When it doesn't, fields have been stitched
    together from different products -- the failure that produced a 200ml balm
    carrying the 125ml balm's SKU.

    Multi-variant products are exempt: there the SKU legitimately identifies
    the selected shade/size and differs from the parent product id by design.
    """
    if not product.sku or not product.product_id:
        return None

    # Some retailers number stock units separately from product pages, so the
    # two identifiers are never expected to match and comparing them would
    # reject every record. The adapter declares which scheme it uses.
    if not product.sku_matches_product_id:
        return None

    # Only single-variant products are expected to match exactly.
    if product.variant_count is not None and product.variant_count > 1:
        return None

    if product.sku != product.product_id:
        return _reject(
            product,
            REASON_VARIANT_CONFLICT,
            f"SKU '{product.sku}' does not match product id '{product.product_id}' from "
            "the URL on a single-variant product; fields may come from different products.",
        )
    return None


# Order affects only which reason is reported first; each validator is
# independent. Adapters may extend this list with retailer-specific rules.
DEFAULT_VALIDATORS: List[Callable[[Product, Sequence[str]], Optional[RejectedRecord]]] = [
    check_title,
    check_price_present,
    check_price_pair,
    check_discount_consistency,
    check_brand,
    check_variant_coherence,
]


def check_brand_evidence_strength(
    product: Product, _targets: Sequence[str]
) -> Optional[RejectedRecord]:
    """Reject brands established only by corroboration, not by structured data.

    Not part of the default set -- it is opt-in via `strict_brand`, for
    consumers who would rather have fewer records than any record whose brand
    rests on the title and URL agreeing.
    """
    if product.brand_verified_by not in STRONG_BRAND_EVIDENCE:
        return _reject(
            product,
            REASON_WEAK_BRAND,
            f"Brand evidence is '{product.brand_verified_by}', which is "
            "corroboration rather than the retailer's structured product data.",
        )
    return None


def validate(
    product: Product,
    target_brands: Sequence[str],
    validators: Optional[Sequence[Callable]] = None,
    strict_brand: bool = False,
) -> Optional[RejectedRecord]:
    """Run every validator, returning the first rejection or None if clean.

    :param strict_brand: also require the brand to come from the retailer's
        structured data, rejecting records verified only by corroboration.
    """
    checks = list(validators or DEFAULT_VALIDATORS)
    if strict_brand:
        checks.append(check_brand_evidence_strength)

    for validator in checks:
        rejection = validator(product, target_brands)
        if rejection is not None:
            return rejection
    return None
