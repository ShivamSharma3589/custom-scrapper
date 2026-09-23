"""Generic, retailer-agnostic validation."""

import re
from typing import Callable, List, Optional, Sequence

from .models import Product, RejectedRecord
from .normalize import fold_accents, percent_off

REASON_MISSING_PRICE = "missing_price"
REASON_INVALID_DISCOUNT = "invalid_discount"
REASON_DISCOUNT_MISMATCH = "discount_mismatch"
REASON_UNVERIFIED_BRAND = "unverified_brand"
REASON_BRAND_MISMATCH = "brand_mismatch"
REASON_VARIANT_CONFLICT = "variant_conflict"
REASON_MISSING_TITLE = "missing_title"
REASON_WEAK_BRAND = "weak_brand_evidence"

STRONG_BRAND_EVIDENCE = frozenset({"breadcrumb_link", "microdata_brand", "listing_data"})

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
    """Reject impossible price pairs."""
    original, current = product.original_price, product.current_price
    if original is None or current is None:
        return None

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
    """The advertised discount must agree with the two prices we extracted."""
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
    """Correct a brand name the user typed, if it is a known variant."""
    key = re.sub(r"[^a-z0-9 ]+", " ", fold_accents(name).casefold())
    key = re.sub(r"\s+", " ", key).strip()
    return BRAND_ALIASES.get(key, name)


def suggest_brand(wanted: str, seen: Sequence[str]) -> Optional[str]:
    """The brand from `seen` that `wanted` was most likely meant to be."""
    from difflib import SequenceMatcher

    target = fold_accents(wanted).casefold()
    best, best_score = None, 0.0
    for candidate in seen:
        folded = fold_accents(candidate).casefold()
        if folded == target:
            continue
        score = SequenceMatcher(None, target, folded).ratio()
        if score > best_score:
            best, best_score = candidate, score
    return best if best_score >= 0.8 else None


def _tokens(brand: str) -> List[str]:
    """Split a brand name into comparable lowercase word tokens."""
    raw = [t for t in re.split(r"[^a-z0-9]+", fold_accents(brand).casefold()) if t]

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
    """Map a retailer's brand name onto the brand we were asked to track."""
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
    """The product must be provably one of the brands we were asked about."""
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
    """Catch a SKU that belongs to a different product than the URL."""
    if not product.sku or not product.product_id:
        return None

    if not product.sku_matches_product_id:
        return None

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
    """Reject brands established only by corroboration, not by structured data."""
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
    """Run every validator, returning the first rejection or None if clean."""
    checks = list(validators or DEFAULT_VALIDATORS)
    if strict_brand:
        checks.append(check_brand_evidence_strength)

    for validator in checks:
        rejection = validator(product, target_brands)
        if rejection is not None:
            return rejection
    return None
