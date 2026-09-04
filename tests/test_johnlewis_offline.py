"""John Lewis extraction, against saved copies of real pages.

Covers the two page shapes that behave differently: a single-price product
with a discount, and a size range that quotes "£33.60 - £156.00" and so has
no single price at all.

    python tests/test_johnlewis_offline.py
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapling.parser import Selector  # noqa: E402

from retailscraper.adapters.base import get_adapter  # noqa: E402
from retailscraper.validation import validate  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FakeResponse:
    def __init__(self, html: str, url: str):
        self._sel = Selector(content=html)
        self.url = url

    def css(self, query):
        return self._sel.css(query)


CASES = [
    (
        "discounted single product",
        "johnlewis_p1.html",
        "https://www.johnlewis.com/clinique-redness-solutions-daily-relief-cream-50ml/p47865",
        {
            "brand": "Clinique",
            "brand_verified_by": "json_ld_brand",
            "product_id": "47865",
            "sku": "230631076",
            "current_price": 40.0,
            "original_price": 50.0,
            "discount_percent": 20.0,
            "currency": "GBP",
            "category": "skin care",
            "price_is_from": False,
        },
    ),
    (
        "size range, no single price",
        "johnlewis_p2_range.html",
        "https://www.johnlewis.com/tom-ford-black-orchid-eau-de-parfum/p96285",
        {
            "brand": "TOM FORD",
            "brand_verified_by": "json_ld_brand",
            "product_id": "96285",
            # The low end of the range, explicitly flagged as a from-price so
            # it is never compared against another retailer's single-size price.
            "price_is_from": True,
            "original_price": None,
        },
    ),
]


def run() -> int:
    adapter = get_adapter("johnlewis")
    failures = 0

    for label, filename, url, expected in CASES:
        path = FIXTURES / filename
        print(f"\n=== {label} ===")
        if not path.exists():
            print(f"  MISS fixture not saved: {filename}")
            failures += 1
            continue

        html = path.read_text(encoding="utf-8", errors="replace")
        product = adapter.extract_product(FakeResponse(html, url))

        if product is None:
            print("  FAIL extraction returned None")
            failures += 1
            continue

        for field, want in expected.items():
            got = getattr(product, field)
            ok = got == want
            failures += 0 if ok else 1
            print(f"  {'ok  ' if ok else 'FAIL'} {field:20} expected={want!r} got={got!r}")

        # The brand must come from the retailer's structured data, never a slug.
        if product.brand_verified_by != "json_ld_brand":
            print(f"  FAIL brand evidence      got={product.brand_verified_by!r}")
            failures += 1

        rejection = validate(product, [product.brand])
        ok = rejection is None
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} validation           "
              f"{rejection.reason if rejection else 'ACCEPTED'}")

    print("\n=== a from-price is not treated as single-variant ===")
    html = (FIXTURES / "johnlewis_p2_range.html").read_text(encoding="utf-8", errors="replace")
    ranged = adapter.extract_product(
        FakeResponse(html, "https://www.johnlewis.com/tom-ford-black-orchid-eau-de-parfum/p96285")
    )
    ok = ranged is not None and ranged.variant_count != 1
    failures += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} variant_count is not 1  "
          f"got={getattr(ranged, 'variant_count', 'n/a')!r}")

    print("\n=== the four price-label shapes ===")
    # John Lewis states the price in an accessible label, in four shapes. The
    # discounted-range one was missed at first, which reported a reduced
    # multi-size product as full price.
    from retailscraper.adapters.johnlewis import (  # noqa: E402
        _ARIA_PRICE_RE,
        _ARIA_RANGE_DISCOUNT_RE,
        _ARIA_RANGE_RE,
    )

    label_cases = [
        ("single, discounted", "The price was £50.00, now £40.00",
         ("single", "50.00", "40.00")),
        ("range, no discount", "The price is £33.60 – £156.00",
         ("range", "33.60", None)),
        ("range, discounted",
         "The price was £220.00 – £310.00, now £187.00 – £263.50",
         ("range_discount", "220.00", "187.00")),
        ("single, no discount", "The price is £299.00", ("none", None, None)),
    ]

    for label, text, (kind, first, second) in label_cases:
        spread = _ARIA_RANGE_DISCOUNT_RE.search(text)
        single = _ARIA_PRICE_RE.search(text)
        span = _ARIA_RANGE_RE.search(text)

        if spread:
            got = ("range_discount", spread.group(1), spread.group(2))
        elif single:
            got = ("single", single.group(1), single.group(2))
        elif span:
            got = ("range", span.group(1), None)
        else:
            got = ("none", None, None)

        ok = got == (kind, first, second)
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label:22} expected={(kind, first, second)} got={got}")

    print("\n=== the retailer's own slug wins over the brand name ===")
    # John Lewis files the brand at /brand/jo-malone-london/. Slugifying the
    # canonical name "Jo Malone" gives "jo-malone", which has no brand page --
    # so the moment brand aliases started normalising "Jo Malone London" down
    # to "Jo Malone", this retailer silently went from 48 products to 0.
    for brand, want in [
        ("Jo Malone", "jo-malone-london"),
        ("Jo Malone London", "jo-malone-london"),
        ("Clinique", "clinique"),
        ("Tom Ford", "tom-ford"),
    ]:
        got = adapter._brand_slug(brand)
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {brand:20} -> {got}")

    print("\n=== URL parsing ===")
    checks = [
        ("plain product", "https://www.johnlewis.com/clinique-x/p47865", "47865"),
        ("with colour segment", "https://www.johnlewis.com/whistles-x/black/p5966220", "5966220"),
        ("brand page is not a product",
         "https://www.johnlewis.com/brand/clinique/_/N-1z13ywm", None),
    ]
    for label, url, want in checks:
        got = adapter.product_id_from_url(url)
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label:28} expected={want!r} got={got!r}")

    print("\n=== one request per product, using the site's own URLs ===")
    # John Lewis links every shade separately, all pointing at the same
    # product id. The crawler deduplicates on URL, so each shade was fetched
    # as a separate product -- a dozen requests to keep one record. That waste
    # pushed a seven-brand run into rate limiting (165 of 372 came back 403).
    #
    # The fix must NOT rewrite the URL. Collapsing
    # "<slug>/<shade>/p<id>" to "<slug>/p<id>" looks equivalent, but some
    # products 404 without their shade segment -- verified live on
    # /too-faced-better-than-sex-mascara/p3033343 and
    # /bobbi-brown-long-wear-cream-shadow-stick/p2688101.
    shades = [
        "https://www.johnlewis.com/too-faced-cloud-crush-blush/tequila-sunset/p110055590",
        "https://www.johnlewis.com/too-faced-cloud-crush-blush/pink-sunset/p110055590",
        "https://www.johnlewis.com/too-faced-better-than-sex-mascara/black/p3033343",
    ]
    kept = adapter.select_candidates(shades, ["Too Faced"])
    ids = {adapter.product_id_from_url(u) for u in kept}

    for label, ok in [
        ("three shade URLs collapse to two products", len(kept) == 2),
        ("both product ids survive", ids == {"110055590", "3033343"}),
        # Every kept URL must be one the site actually published.
        ("kept URLs are unmodified originals", all(u in shades for u in kept)),
        ("no invented bare-slug URL",
         not any(u.count("/") == 3 for u in kept)),
    ]:
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")

    print("\n=== an unresolvable brand cannot crash the crawl ===")
    # A brand with no cached code must NOT trigger a network lookup from
    # inside the crawl: resolving needs its own browser session, and starting
    # one in a running asyncio loop raises "Playwright Sync API inside the
    # asyncio loop". That exception killed an entire seven-brand production
    # run because one brand (Estee Lauder) could not be resolved.
    import retailscraper.adapters.johnlewis as jl_module

    calls = []
    original = jl_module.JohnLewisAdapter._scan_grid_sitemaps

    def spy(self, wanted):
        calls.append(set(wanted))
        raise AssertionError("network lookup attempted from inside the crawl")

    jl_module.JohnLewisAdapter._scan_grid_sitemaps = spy
    try:
        pages = adapter.product_listing_urls("Definitely Not A Brand", [], max_pages=2)
        seeds = adapter.campaign_seed_urls(["Definitely Not A Brand"])
        crashed = False
    except Exception as exc:  # noqa: BLE001 - the point is that nothing raises
        pages, seeds, crashed = None, None, True
        print(f"  FAIL raised {type(exc).__name__}: {exc}")
    finally:
        jl_module.JohnLewisAdapter._scan_grid_sitemaps = original

    for label, ok in [
        ("no exception during the crawl", not crashed),
        ("no network lookup attempted", not calls),
        ("unknown brand yields no listings", pages == []),
        ("unknown brand yields no seeds", seeds == []),
    ]:
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")

    print("\n=== listing pages are not also campaign seeds ===")
    # The same URL queued twice with two callbacks is deduplicated by the
    # crawler, and whichever loses silently never runs. That produced a run
    # which fetched the brand page and returned zero products.
    seeds = set(adapter.campaign_seed_urls(["Clinique"]))
    listings = {page.url for page in adapter.product_listing_urls("Clinique", [], max_pages=2)}
    ok = not (seeds & listings)
    failures += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} no URL serves both roles  overlap={seeds & listings or 'none'}")

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
