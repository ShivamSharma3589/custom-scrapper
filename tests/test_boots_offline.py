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

    def check(label, ok, detail=""):
        nonlocal failures
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  {detail}" if not ok else ""))

    print("\n=== the crawl starts at the brand page, not a guessed list name ===")
    # Only Clinique has "<slug>-full-range". A run that guessed it collected
    # just the few featured products for MAC, Tom Ford, Estee Lauder and more.
    urls = [p.url for p in adapter.product_listing_urls("Tom Ford", [], max_pages=4)]
    check("only the brand page", urls == ["https://www.boots.com/tom-ford"], urls)
    cat_urls = [p.url for p in adapter.product_listing_urls("Clinique", ["skincare"], max_pages=4)]
    check("with --categories, the category list instead",
          cat_urls == ["https://www.boots.com/clinique/clinique-skincare"], cat_urls)

    print("\n=== a brand page leads to every list it links ===")
    # Boots redirects /tom-ford to /tom-ford-, so the lists live under that.
    brand_page = Selector(content="""<html><body>
        <a href="/tom-ford-/tom-ford-all-fragrances">All</a>
        <a href="/tom-ford-/tom-ford-gift-sets">Gifts</a>
        <a href="/tom-ford-grey-vetiver-eau-de-parfum-100ml-10223350">product</a>
        <a href="/fragrance/fragrance-offers">elsewhere</a>
        <a href="/tom-ford-/tom-ford-new-in?sort=price">filtered</a>
    </body></html>""", url="https://www.boots.com/tom-ford-")
    meta = {"brand": "Tom Ford", "url": "https://www.boots.com/tom-ford"}
    lists = adapter.more_listing_pages(brand_page, meta, added=1)
    check("both lists, nothing else", lists == [
        "https://www.boots.com/tom-ford-/tom-ford-all-fragrances",
        "https://www.boots.com/tom-ford-/tom-ford-gift-sets"], lists)
    check("not widened when a category was asked for",
          adapter.more_listing_pages(brand_page, {**meta, "category": "gifts"}, 1) == [])

    empty = Selector(content="<html><body></body></html>", url="https://www.boots.com/too-faced")
    adapter.more_listing_pages(empty, {"brand": "Too Faced", "url": "https://www.boots.com/too-faced"}, 1)
    warnings = adapter.crawl_warnings()
    check("a brand page with no lists is reported", any("Too Faced" in w for w in warnings), warnings)
    check("without quotes, which the verdict would read as 'not stocked'",
          not any("'Too Faced'" in w for w in warnings))

    print("\n=== a list is paged until a page adds nothing ===")
    list_meta = {"brand": "Tom Ford", "url": "https://www.boots.com/tom-ford-/tom-ford-all-fragrances"}
    nxt = adapter.more_listing_pages(Selector(content="<html></html>"), list_meta, added=48)
    check("page 0 leads to page 1",
          nxt == ["https://www.boots.com/tom-ford-/tom-ford-all-fragrances?paging.index=1&paging.size=25"], nxt)
    page_three = {**list_meta, "url": list_meta["url"] + "?paging.index=3&paging.size=25"}
    check("page 3 leads to page 4",
          adapter.more_listing_pages(Selector(content="<html></html>"), page_three, 5)[0].endswith("paging.index=4&paging.size=25"))
    check("a page adding nothing ends it",
          adapter.more_listing_pages(Selector(content="<html></html>"), page_three, 0) == [])

    print("\n=== offers pages are found from links, not a fixed list ===")
    offers = Selector(content="""<html><body>
        <a href="/fragrance/fragrance-offers">Fragrance offers</a>
        <a href="/offers/makeup-offers">Makeup offers</a>
        <a href="/sale/beauty-sale">Beauty sale</a>
        <a href="/fragrance/shop-all-fragrance?criteria.promotionalText=Save+20">Shop now</a>
        <a href="/clinique-take-the-day-off-cleansing-balm-offer-10292729">product</a>
        <a href="/salon-hair">not a sale page</a>
    </body></html>""", url="https://www.boots.com/offers")
    found = adapter.offer_page_links(offers)
    check("offers and sale pages only", found == [
        "https://www.boots.com/fragrance/fragrance-offers",
        "https://www.boots.com/offers/makeup-offers",
        "https://www.boots.com/sale/beauty-sale"], found)

    print("\n=== an offer's link names the offer ===")
    from retailscraper.models import Campaign
    hub_offer = Campaign(
        retailer="Boots", promotion_text="Fragrance Save up to 20% SHOP NOW",
        promotion_type="percentage_discount", scope="category",
        source_url="https://www.boots.com/offers",
        landing_url="https://www.boots.com/fragrance/shop-all-fragrance"
                    "?criteria.promotionalText=Save+up+to+20+percent+on+selected+fragrance")
    key = adapter.promotion_key(hub_offer)
    check("decoded from the link", key == "Save up to 20 percent on selected fragrance", key)

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
