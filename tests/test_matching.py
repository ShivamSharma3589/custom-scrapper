"""Cross-retailer product matching.

The cases below are drawn from real titles the scrapers produced, including
the ones that are dangerous to get wrong -- notably the 125ml and 200ml
Cleansing Balms, which share nearly every word.

    python tests/test_matching.py
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retailscraper.matching import (  # noqa: E402
    extract_size,
    match_across_retailers,
    normalize_title,
)


def product(retailer, brand, title, price, **extra):
    row = {
        "retailer": retailer,
        "brand": brand,
        "brand_matched_to": extra.pop("matched_to", brand),
        "product_title": title,
        "product_url": f"https://example.test/{abs(hash(title)) % 10**6}",
        "current_price": price,
    }
    row.update(extra)
    return row


# --- size parsing ---------------------------------------------------------
SIZE_CASES = [
    ("Clinique Take The Day Off Cleansing Balm 125ml", "volume:125.0"),
    ("Clinique Take the Day off Cleansing Balm 200ml", "volume:200.0"),
    ("Clinique Almost Lipstick 1.9g - Black Honey", "weight:1.9"),
    ("Jo Malone London Basil & Neroli Cologne - 100ml", "volume:100.0"),
    ("Some Serum 1L Value Size", "volume:1000.0"),
    ("Clinique Moisture Surge 100H Auto-Replenishing Hydrator", None),
    ("Gift Set 3 x 30ml", "volume:90.0"),
]

# --- matching -------------------------------------------------------------
CATALOGUE = [
    # Same product, two retailers, differently punctuated -> must match.
    product("Lookfantastic", "Clinique",
            "Clinique Anti Blemish Solutions Cleansing Foam 125ml", 27.0),
    product("Boots", "Clinique",
            "Clinique Anti-Blemish Solutions™ Cleansing Foam 125ml", 22.4),

    # Same range, DIFFERENT size -> must NOT match.
    product("Lookfantastic", "Clinique",
            "Clinique Take The Day Off Cleansing Balm 125ml", 25.5),
    product("Boots", "Clinique",
            "Clinique Take The Day Off Cleansing Balm 200ml", 36.0),

    # Retailer trading names differ; brand_matched_to resolves them.
    product("Lookfantastic", "Jo Malone London",
            "Jo Malone London Basil & Neroli Cologne - 100ml", 125.0,
            matched_to="Jo Malone"),
    product("Boots", "Jo Malone",
            "Jo Malone London Basil and Neroli Cologne 100ml", 118.0,
            matched_to="Jo Malone"),

    # Only one retailer stocks it -> unmatched, not dropped.
    product("Boots", "Clinique", "Clinique Superpowder Double Face Powder 35g", 28.8),

    # Different brands, similar words -> must NOT match.
    product("Lookfantastic", "Clinique", "Clinique Smart Clinical Serum 50ml", 70.0),
    product("Boots", "Estee Lauder", "Estee Lauder Advanced Night Serum 50ml", 70.0),

    # Products stating NO size at all. Real catalogues are full of these, and
    # they must group and sort alongside sized products without blowing up.
    product("Lookfantastic", "Clinique",
            "Clinique Moisture Surge 100H Auto-Replenishing Hydrator", 42.0),
    product("Boots", "Clinique",
            "Clinique Moisture Surge 100H Auto Replenishing Hydrator", 32.25),
]


def run() -> int:
    failures = 0

    print("=== size extraction ===")
    for title, expected in SIZE_CASES:
        got = extract_size(title)
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {title[:48]:50} expected={expected!r} got={got!r}")

    print("\n=== title normalisation drops the brand ===")
    tokens = normalize_title("Clinique Anti-Blemish Solutions™ Cleansing Foam 125ml", "Clinique")
    ok = "clinique" not in tokens and "cleansing" in tokens
    failures += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} brand stripped, content kept -> {tokens}")

    print("\n=== cross-retailer matching ===")
    matches, unmatched = match_across_retailers(CATALOGUE)

    checks = [
        ("three products matched across retailers", len(matches) == 3),
        ("sizeless products still match",
         any("Moisture Surge" in m.title for m in matches)),
        ("cleansing foam matched",
         any("Cleansing Foam" in m.title for m in matches)),
        ("cologne matched across trading names",
         any("Cologne" in m.title for m in matches)),
        ("125ml and 200ml balms NOT matched",
         not any("Cleansing Balm" in m.title for m in matches)),
        ("different brands NOT matched",
         not any(m.brand.casefold() == "estee lauder" for m in matches)),
        ("single-retailer product kept as unmatched",
         any("Superpowder" in (p.get("product_title") or "") for p in unmatched)),
    ]
    for label, ok in checks:
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")

    print("\n  matches found:")
    for m in matches:
        gap = f"gap £{m.price_gap}" if m.price_gap is not None else "gap -"
        print(f"    {m.brand:12} {m.title[:44]:46} score={m.score:.2f} {gap}")
        for o in m.offers:
            print(f"        {o['retailer']:14} {o['current_price']}")

    # The headline the whole feature exists to produce.
    foam = next((m for m in matches if "Cleansing Foam" in m.title), None)
    ok = foam is not None and foam.cheapest["retailer"] == "Boots" and foam.price_gap == 4.6
    failures += 0 if ok else 1
    print(f"\n  {'ok  ' if ok else 'FAIL'} cheapest retailer and gap computed correctly")

    print("\n=== one retailer never appears twice in a match ===")
    # A retailer sells a given product once. Two of its items in one cluster
    # means they are DIFFERENT products that merely read alike, and merging
    # them invents a price gap: a real run put Lookfantastic at 86.40, 108.00
    # and 290.00 in a single Tom Ford cluster and reported a 208.00 spread.
    lookalikes = [
        {"retailer": "Lookfantastic", "brand": "TOM FORD", "brand_matched_to": "Tom Ford",
         "product_title": "TOM FORD Black Orchid Eau de Parfum 50ml",
         "product_url": "lf1", "current_price": 86.40, "currency": "GBP"},
        {"retailer": "Lookfantastic", "brand": "TOM FORD", "brand_matched_to": "Tom Ford",
         "product_title": "TOM FORD Black Orchid Eau de Parfum 50ml Refill",
         "product_url": "lf2", "current_price": 290.00, "currency": "GBP"},
        {"retailer": "AllBeauty", "brand": "Tom Ford", "brand_matched_to": "Tom Ford",
         "product_title": "TOM FORD Black Orchid Eau de Parfum 50ml",
         "product_url": "ab1", "current_price": 82.00, "currency": "GBP"},
    ]
    matched, _ = match_across_retailers(lookalikes, threshold=0.6)
    for label, ok in [
        ("no cluster holds one retailer twice",
         all(len({o["retailer"] for o in m.offers}) == len(m.offers) for m in matched)),
        ("a genuine cross-retailer pair is still found",
         any(len(m.offers) >= 2 for m in matched)),
        ("the invented 208.00 gap is gone",
         not any((m.price_gap or 0) > 200 for m in matched)),
    ]:
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")

    print("\n=== a from-price is never ranked against a real price ===")
    # John Lewis quotes "£33.60 - £156.00" for a size range, so 33.60 is the
    # cheapest SIZE, not the product's price. Ranking it against another
    # retailer's single-size price named John Lewis cheapest by £52.80 -- a
    # gap that does not exist.
    ranged = [
        {"retailer": "John Lewis", "brand": "TOM FORD", "brand_matched_to": "Tom Ford",
         "product_title": "TOM FORD Black Orchid Eau de Parfum", "product_url": "jl",
         "current_price": 33.60, "price_is_from": True, "currency": "GBP"},
        {"retailer": "Lookfantastic", "brand": "TOM FORD", "brand_matched_to": "Tom Ford",
         "product_title": "TOM FORD Black Orchid Eau de Parfum", "product_url": "lf",
         "current_price": 86.40, "price_is_from": False, "currency": "GBP"},
    ]
    matched, _ = match_across_retailers(ranged)
    row = matched[0].to_dict() if matched else {}
    for label, ok in [
        ("from-price retailer is not named cheapest",
         row.get("cheapest_retailer") == "Lookfantastic"),
        ("no phantom price gap", row.get("price_gap") is None),
        ("the excluded offer is still reported", row.get("from_price_offers") == 1),
        ("both offers stay visible to the reader", len(row.get("offers") or []) == 2),
    ]:
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
