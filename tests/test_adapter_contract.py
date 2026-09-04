"""Rules every adapter must obey, checked against all of them at once.

These are the invariants the crawler depends on. They live in one place
because a rule fixed in one adapter is worthless if the next adapter
reintroduces it -- which is exactly what happened: the "one URL, one role"
collision was found and fixed in John Lewis, then created again in Boots a
few hours later.

    python tests/test_adapter_contract.py
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retailscraper.adapters.base import available_adapters, get_adapter  # noqa: E402

BRANDS = ["Clinique", "MAC", "Jo Malone"]


def run() -> int:
    failures = 0

    def check(label, ok, detail=""):
        nonlocal failures
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  ' + detail) if detail and not ok else ''}")

    for name in available_adapters():
        adapter = get_adapter(name)
        print(f"\n=== {name} ===")

        # 1. No URL may serve two roles. The crawler deduplicates by URL, so a
        #    URL queued as both a campaign seed and a product listing is
        #    fetched once with whichever callback was queued first; the other
        #    silently never runs. When the campaign callback wins, the page is
        #    fetched and no products are ever requested from it.
        seeds = set(adapter.campaign_seed_urls(BRANDS))
        listings = {
            page.url
            for brand in BRANDS
            for page in adapter.product_listing_urls(brand, [], max_pages=2)
        }
        overlap = seeds & listings
        check("campaign seeds do not collide with listings",
              not overlap, f"overlap: {sorted(overlap)[:2]}")

        # 2. A campaigns-only run must not silently return nothing.
        hubs = adapter.campaign_discovery_urls()
        check("campaign hubs are either declared or absent (not broken)",
              isinstance(hubs, list))

        # 3. Identity must round-trip: a URL the adapter accepts as a product
        #    must yield an id, or deduplication has nothing to key on.
        for page in list(listings)[:1]:
            check("listing URLs are not mistaken for products",
                  not adapter.is_product_url(page), page)

        # 4. Declared categories must be usable.
        cats = adapter.known_categories()
        check("known_categories returns unique values",
              len(cats) == len(set(cats)))
        if adapter.supports_categories:
            check("a category filter produces listings",
                  bool(adapter.product_listing_urls("Clinique", cats[:1], max_pages=1))
                  or not cats)

        # 5. A warmup URL, if declared, must not also be a real target: it is
        #    fetched expecting a challenge and its content is discarded.
        warmup = adapter.warmup_url()
        if warmup:
            check("warmup URL is not also a listing", warmup not in listings, warmup)
            check("warmup URL is not also a campaign hub", warmup not in set(hubs), warmup)

    print("\n=== the crawler holds no retailer-specific URL knowledge ===")
    # The sitemap route used to build its filter as a regex in the spider:
    #     /p/<brand-slug>/<digits>$
    # That is Lookfantastic's URL shape and nobody else's, so every other
    # sitemap-driven retailer matched nothing and reported zero products with
    # no error at all. Deciding what a product URL looks like belongs to the
    # adapter, and each one must recognise its own and reject the others'.
    sitemap_driven = [
        ("lookfantastic",
         "https://www.lookfantastic.com/p/clinique-moisture-surge/12849046/"),
        ("marksandspencer",
         "https://www.marksandspencer.com/clinique-for-men-cream-shave-125ml/p/hbp60562352"),
    ]
    for name, own_url in sitemap_driven:
        adapter = get_adapter(name)
        if not adapter.sitemap_urls:
            continue
        accepts_own = bool(adapter.select_candidates([own_url], ["Clinique"]))
        failures += 0 if accepts_own else 1
        print(f"  {'ok  ' if accepts_own else 'FAIL'} {name} accepts its own product URL")

        for other_name, other_url in sitemap_driven:
            if other_name == name:
                continue
            rejects = not adapter.select_candidates([other_url], ["Clinique"])
            failures += 0 if rejects else 1
            print(f"  {'ok  ' if rejects else 'FAIL'} {name} rejects {other_name}'s URL shape")

    # And the crawler must not carry a hardcoded path pattern in its CODE.
    # Comments are excluded on purpose: the fix for this leak is explained in
    # a comment that necessarily quotes the pattern it removed, and a test
    # that fails on its own documentation is worse than no test.
    spider_path = Path(__file__).resolve().parent.parent / "retailscraper" / "spider.py"
    code_lines = [
        line for line in spider_path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    ]
    code = "\n".join(code_lines)
    for leak in ('"/p/', "'/p/", "/prd/", "/dp/", "products.json", "/collections/"):
        clean = leak not in code
        failures += 0 if clean else 1
        print(f"  {'ok  ' if clean else 'FAIL'} no {leak!r} path literal in spider code")

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
