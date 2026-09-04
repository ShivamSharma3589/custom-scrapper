"""Exercise the Boots adapter against real captured HTML.

Boots pages are captured through a stealth browser (the site is behind bot
protection), so having them as fixtures matters even more here than for
Lookfantastic -- re-fetching is slow and conspicuous.

    python tests/test_boots_offline.py
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapling.parser import Selector  # noqa: E402

from retailscraper.adapters.boots import BootsAdapter  # noqa: E402
from retailscraper.validation import validate  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

CASES = [
    (
        "discounted product",
        "boots_p1.html",
        "https://www.boots.com/clinique-moisture-surge-100h-auto-replenishing-hydrator-50ml-10292729",
        {"brand": "Clinique", "product_id": "10292729",
         "original_price": 43.0, "current_price": 32.25,
         "discount_amount": 10.75, "currency": "GBP"},
    ),
    (
        "foundation product",
        "boots_p2.html",
        "https://www.boots.com/clinique-even-better-makeup-foundation-10256734",
        {"brand": "Clinique", "product_id": "10256734"},
    ),
]


def run() -> int:
    adapter = BootsAdapter()
    failures = 0

    for label, filename, url, expected in CASES:
        path = FIXTURES / filename
        if not path.exists():
            print(f"\n=== {label} === SKIPPED (missing {filename})")
            continue

        html = path.read_text(encoding="utf-8", errors="replace")
        sel = Selector(content=html)
        product = adapter.parse_product(sel, url)

        print(f"\n=== {label} ===")
        if product is None:
            print("  FAIL: no product extracted")
            failures += 1
            continue

        for field, want in expected.items():
            got = getattr(product, field)
            ok = got == want
            failures += 0 if ok else 1
            print(f"  {'ok  ' if ok else 'FAIL'} {field:16} expected={want!r} got={got!r}")

        if product.brand_verified_by != "microdata_brand":
            print(f"  FAIL brand_verified_by   got={product.brand_verified_by!r}")
            failures += 1

        rejection = validate(product, ["Clinique"])
        ok = rejection is None
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} validation      "
              f"{rejection.reason if rejection else 'ACCEPTED'}")

        print(f"  ---- title           {product.product_title[:62]}")
        print(f"  ---- availability    {product.availability}")
        print(f"  ---- promo copy      {(product.promotional_copy or '-')[:80]}")

        for campaign in adapter.parse_campaigns(sel, url):
            print(f"       [{campaign.scope:9}] {campaign.promotion_type:20} "
                  f"{campaign.promotion_text[:60]}")

    # Listing-page discovery is the other half of the Boots adapter.
    listing = FIXTURES / "boots_list.html"
    if listing.exists():
        links = adapter.extract_product_links(
            Selector(content=listing.read_text(encoding="utf-8", errors="replace"))
        )
        ok = len(links) >= 20
        failures += 0 if ok else 1
        print("\n=== listing discovery ===")
        print(f"  {'ok  ' if ok else 'FAIL'} product links found: {len(links)}")
        for link in links[:3]:
            print(f"       {link}")

    print("\n=== the brand hub is always tried, not just -full-range ===")
    # Only Clinique has a "<slug>-full-range" page. MAC, Estee Lauder,
    # Bobbi Brown and Too Faced all 404 on it, so a seven-brand production run
    # returned 187 Clinique products and nothing at all for the other six.
    # The bare brand hub exists for those and must always be included.
    for brand in ["Clinique", "MAC", "Estee Lauder"]:
        urls = {p.url for p in adapter.product_listing_urls(brand, [], max_pages=2)}
        slug = adapter._brand_slug(brand)
        hub = f"https://{adapter.domain}/{slug}"
        ok = hub in urls
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {brand:14} hub included ({hub})")

    # With an explicit category filter the category paths are the request,
    # so the hub would widen the crawl beyond what was asked for.
    cat_urls = {p.url for p in adapter.product_listing_urls("Clinique", ["skincare"], max_pages=1)}
    ok = f"https://{adapter.domain}/clinique" not in cat_urls
    failures += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} hub NOT added when --categories is used")

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
