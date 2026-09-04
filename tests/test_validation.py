"""Validation rules, checked against the real bugs found in production data.

Each case asserts the expected verdict, so a regression fails the run rather
than printing something wrong that nobody notices.

    python tests/test_validation.py
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retailscraper.models import Product  # noqa: E402
from retailscraper.validation import (  # noqa: E402
    KNOWN_BRANDS,
    canonical_brand,
    match_brand,
    suggest_brand,
    validate,
)


def make(**overrides):
    """A known-good Clinique record, with fields overridden per case."""
    base = dict(
        retailer="Lookfantastic",
        brand="Clinique",
        product_id="13467795",
        product_title="Clinique Take the Day off Cleansing Balm 200ml",
        product_url="https://www.lookfantastic.com/p/x/13467795/",
        source_url="https://www.lookfantastic.com/p/x/13467795/",
        brand_verified_by="breadcrumb_link",
        current_price=36.0,
        original_price=48.0,
        sku="13467795",
        variant_count=1,
    )
    base.update(overrides)
    return Product(**base)


# (label, product, expected rejection reason or None for accepted)
CASES = [
    ("clean record", make(), None),
    ("impossible pair (now 50, was 30)",
     make(current_price=50.0, original_price=30.0), "invalid_discount"),
    ("200ml page carrying the 125ml SKU",
     make(sku="11144730"), "variant_conflict"),
    ("shade SKU on a 3-variant product is fine",
     make(sku="11144730", variant_count=3), None),
    ("brand we did not ask for",
     make(brand="Estee Lauder"), "brand_mismatch"),
    ("brand not verifiable",
     make(brand_verified_by="unverified"), "unverified_brand"),
    ("says 40% off, prices say 25%",
     make(discount_percent=40.0), "discount_mismatch"),
    ("says 25% off, prices agree",
     make(discount_percent=25.0), None),
    ("no price at all",
     make(current_price=None), "missing_price"),
    ("no title",
     make(product_title=""), "missing_title"),
    # Retailers use their own trading names; these must still be accepted.
    ("retailer's fuller brand name",
     make(brand="Clinique Laboratories"), None),
]

# (verified brand, expected match against the target list, label)
BRAND_CASES = [
    ("Jo Malone London", "Jo Malone"),
    ("MAC Cosmetics", "MAC"),
    ("TOM FORD", "Tom Ford"),
    ("Clinique", "Clinique"),
    ("Estee Lauder", "Estee Lauder"),
    # Accents must fold. A seven-brand production run rejected 133 genuine
    # Estee Lauder products as brand_mismatch because Lookfantastic files the
    # brand as "Estée Lauder" while the business asks for "Estee Lauder".
    ("Estée Lauder", "Estee Lauder"),
    ("ESTÉE LAUDER", "Estee Lauder"),
    # An initialism written with separators is the same brand. Boots files
    # MAC as "M.A.C", which cost every MAC product in that same run.
    ("M.A.C", "MAC"),
    ("M·A·C", "MAC"),
    ("M A C", "MAC"),
    # Must NOT match: a different brand that merely starts with the letters.
    ("Macadamia Natural Oil", None),
    ("Bobbi Brown", None),
    # Folding accents must not make unrelated brands collide.
    ("Lancôme", None),
]

TARGETS = ["Clinique", "Jo Malone", "MAC", "Tom Ford", "Estee Lauder"]


def run() -> int:
    failures = 0

    print("=== validation rules ===")
    for label, product, expected in CASES:
        rejection = validate(product, ["Clinique", "Clinique Laboratories"])
        actual = rejection.reason if rejection else None
        ok = actual == expected
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label:38} "
              f"expected={expected or 'ACCEPTED'} got={actual or 'ACCEPTED'}")

    print("\n=== brand alias matching ===")
    for verified, expected in BRAND_CASES:
        actual = match_brand(verified, TARGETS)
        ok = actual == expected
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {verified:26} "
              f"expected={expected!r} got={actual!r}")

    print("\n=== correcting the brand names a user types ===")
    # The brief said "Bobbie Brown". Every retailer spells it "Bobbi Brown",
    # so the typo returned zero products at all six and read as "not stocked".
    # Only the QUESTION is normalised -- never the retailer's answer, which
    # would be the fuzzy matching that turns Tommy Jeans into Tom Ford.
    for typed, want in [
        ("Bobbie Brown", "Bobbi Brown"),
        ("bobby brown", "Bobbi Brown"),
        ("MAC Cosmetics", "MAC"),
        ("Jo Malone London", "Jo Malone"),
        ("Bobbi Brown", "Bobbi Brown"),
        ("Clinique", "Clinique"),
        # An unknown brand passes through untouched rather than being
        # silently rewritten into something else.
        ("Some Brand We Do Not Know", "Some Brand We Do Not Know"),
    ]:
        got = canonical_brand(typed)
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {typed:28} -> {got!r}")

    print("\n=== suggesting a correction for an unrecognised brand ===")
    seen = ["Clinique", "Estee Lauder", "Tommy Jeans", "Bobbi Brown"]
    for typed, want in [
        ("Clinque", "Clinique"),
        ("Bobbie Brown", "Bobbi Brown"),
        # Must NOT suggest across genuinely different brands: that is the
        # failure which returns Tommy Jeans records for a Tom Ford query.
        ("Tom Ford", None),
        ("Totally Made Up Brand", None),
        # Never suggest the name that was already asked for. Doing so printed
        # "'Jo Malone' found nothing, but John Lewis stocks 'Jo Malone'" --
        # advice nobody can act on, which also hid the real cause.
        ("Clinique", None),
        ("Bobbi Brown", None),
    ]:
        got = suggest_brand(typed, seen)
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {typed:24} -> {got!r} (want {want!r})")

    canonical_ok = all(canonical_brand(b) == b for b in KNOWN_BRANDS)
    failures += 0 if canonical_ok else 1
    print(f"  {'ok  ' if canonical_ok else 'FAIL'} every KNOWN_BRANDS entry is already canonical")

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
