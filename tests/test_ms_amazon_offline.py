"""Marks & Spencer and Amazon extraction, against saved real pages.

The Amazon half is unusual: its fixture is a page of INR pricing captured
from a non-UK IP, and the thing being tested is that the adapter REFUSES it.
A passing Amazon run that emitted those prices would be the worst possible
outcome, so the test asserts zero records and a non-zero refusal count.

    python tests/test_ms_amazon_offline.py
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapling.parser import Selector  # noqa: E402

from retailscraper.adapters.base import get_adapter  # noqa: E402
from retailscraper.validation import validate  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FakeResponse:
    def __init__(self, path: Path, url: str):
        self.body = path.read_bytes()
        self.url = url
        self._sel = Selector(content=self.body.decode("utf-8", "replace"))

    def css(self, query):
        return self._sel.css(query)


def run() -> int:
    failures = 0

    def check(label, ok, detail=""):
        nonlocal failures
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  ' + str(detail)) if detail and not ok else ''}")

    # ------------------------------------------------------------------
    print("=== Marks & Spencer ===")
    ms = get_adapter("marksandspencer")
    path = FIXTURES / "marksandspencer_product.html"
    if not path.exists():
        print("  MISS marksandspencer_product.html not saved")
        failures += 1
    else:
        url = "https://www.marksandspencer.com/clinique-for-men-cream-shave-125ml/p/hbp60562352"
        product = ms.extract_product(FakeResponse(path, url))
        check("a product is extracted", product is not None)
        if product:
            for field, want in [
                ("brand", "Clinique"),
                ("brand_verified_by", "json_ld_brand"),
                ("product_id", "hbp60562352"),
                ("sku", "60562352"),
                ("current_price", 20.8),
                # The was-price is NOT in the JSON-LD; it comes from the
                # page's "previousPrice" state. Missing it would report a
                # discounted product at full price.
                ("original_price", 26.0),
                ("discount_percent", 20.0),
                ("currency", "GBP"),
                ("price_is_from", False),
            ]:
                got = getattr(product, field)
                check(f"{field:20} == {want!r}", got == want, got)
            check("validates", validate(product, ["Clinique"]) is None)

        check("search is not used as a route (robots disallows it)",
              not any("search" in u for u in ms.sitemap_urls))

    # ------------------------------------------------------------------
    # The sitemap route was abandoned after it was found to report products
    # M&S sells as products M&S does not stock: it filtered URLs by the brand
    # in the slug, and M&S mostly does not put the brand in the slug. Across
    # all nine UK product sitemaps -- 29,772 URLs -- 19 mention Clinique and
    # none mention any other ELC brand, while M&S's own Clinique page lists
    # 168 and its Estee Lauder page 105.
    print("\n=== M&S: brand landing pages are the discovery route ===")
    check("the sitemap is no longer crawled", ms.sitemap_urls == [], ms.sitemap_urls)

    pages = ms.product_listing_urls("Clinique", [], max_pages=3)
    check("three pages requested", len(pages) == 3, len(pages))
    check("page one carries no paging parameter",
          pages[0].url == "https://www.marksandspencer.com/l/beauty/clinique",
          pages[0].url)
    check("later pages use ?page=N",
          [p.url.rsplit("?", 1)[-1] for p in pages[1:]] == ["page=2", "page=3"],
          [p.url for p in pages[1:]])
    check("every page is tagged with the brand it was asked for",
          all(p.brand == "Clinique" for p in pages))
    # "estee lauder" slugifies correctly, but the accented spelling the site
    # itself uses would become "est-e-lauder" without an override.
    check("accented brand names still slugify",
          ms._brand_slug("Estée Lauder") == "estee-lauder",
          ms._brand_slug("Estée Lauder"))

    print("\n=== M&S: the listing IS the product data ===")
    listing = FIXTURES / "ms_brand_listing.html"
    if not listing.exists():
        print("  MISS ms_brand_listing.html not saved")
        failures += 1
    else:
        response = FakeResponse(listing, "https://www.marksandspencer.com/l/beauty/clinique")
        products = ms.extract_products_from_listing(response, "Clinique")
        by_title = {p.product_title: p for p in products}

        check("products read without fetching a page each", len(products) == 4, len(products))
        check("every record validates",
              all(validate(p, ["Clinique"]) is None for p in products))
        check("brand comes from the record, not the page we asked for",
              {p.brand for p in products} == {"Clinique"}, {p.brand for p in products})
        check("brand provenance is recorded",
              {p.brand_verified_by for p in products} == {"ms_brand_field"})
        check("swatch parameters are stripped from the product url",
              all("?" not in p.product_url for p in products))
        check("the product code is the id, the number is the sku",
              all(p.product_id.startswith("hbp") and (p.sku or "").isdigit()
                  for p in products),
              [(p.product_id, p.sku) for p in products])

        lotion = by_title.get("Jumbo Dramatically Different™ Moisturizing Lotion 200ml")
        check("a single price is read exactly",
              lotion is not None and (lotion.current_price, lotion.original_price)
              == (44.0, 55.0),
              lotion and (lotion.current_price, lotion.original_price))
        check("its discount is computed, not copied",
              lotion is not None
              and (lotion.discount_amount, lotion.discount_percent) == (11.0, 20.0),
              lotion and (lotion.discount_amount, lotion.discount_percent))
        check("the on-tile promotion is captured",
              lotion is not None and lotion.promotional_copy == "20% off",
              lotion and lotion.promotional_copy)
        check("the product's own type is recorded as its category",
              lotion is not None and lotion.category == "Moisturiser",
              lotion and lotion.category)

        # 22 shades priced 26.25-26.95. Quoting 26.25 as "the price" would
        # make M&S look cheapest against another retailer's single size.
        ranged = by_title.get("Anti-Blemish Solutions™ Liquid Makeup 30ml")
        check("a shade range is flagged as a from-price",
              ranged is not None and ranged.price_is_from is True)
        check("the range floor is the quoted price",
              ranged is not None and ranged.current_price == 26.25,
              ranged and ranged.current_price)
        check("variant count survives", ranged is not None and ranged.variant_count == 22,
              ranged and ranged.variant_count)

        # M&S publishes "Multi-DiClinique For Menional" for "Multi-Dimensional"
        # in their own <title>, JSON-LD and listing JSON. It is their defect,
        # and it is recorded verbatim: repairing a retailer's copy by guessing
        # would be a silent fiction in the output.
        check("a title M&S itself corrupts is recorded verbatim",
              any("Multi-DiClinique For Menional" in t for t in by_title),
              list(by_title)[:1])

        # Both tiers are live at once, which is exactly the multi-tier
        # promotion the comparison is meant to surface.
        check("both promotional tiers are visible",
              {p.promotional_copy for p in products} == {"20% off", "30% off"},
              {p.promotional_copy for p in products})

    print("\n=== M&S: campaigns come from its navigation ===")
    # M&S has no offers hub. Its navigation is the offer menu, embedded as
    # data on every page and named for the promotions running right now.
    nav = FIXTURES / "ms_beauty_nav.html"
    if not nav.exists():
        print("  MISS ms_beauty_nav.html not saved")
        failures += 1
    else:
        response = FakeResponse(nav, "https://www.marksandspencer.com/l/beauty")
        campaigns = ms.extract_campaign_directory(response)
        texts = {c.promotion_text for c in campaigns}
        scoped = {c.promotion_text: (c.scope, c.scope_value) for c in campaigns}

        check("the offer menu is read", len(campaigns) >= 20, len(campaigns))
        check("brand promotions are found", "20% off Clinique" in texts)
        check("scoped to the brand, from M&S's own brand list",
              scoped.get("20% off Clinique") == ("brand", "Clinique"),
              scoped.get("20% off Clinique"))
        check("department promotions are scoped to the department",
              scoped.get("30% off Makeup") == ("category", "makeup"),
              scoped.get("30% off Makeup"))
        check("every campaign has a type",
              all(c.promotion_type for c in campaigns))
        check("no navigation leaked in",
              not any(t.lower() in {"beauty", "shop all", "new in", "gift finder"}
                      for t in texts))
        # "Up to 30% off Beauty" sits in the nav's "Top Brand Offers" group
        # alongside real brands. Treating those leaves as brands made a
        # promotion its own scope_value.
        check("a promotion is never mistaken for a brand",
              not any(c.scope == "brand" and c.scope_value in texts
                      for c in campaigns),
              [c.scope_value for c in campaigns
               if c.scope == "brand" and c.scope_value in texts])
        # M&S's own nav lists which pages are brand pages. Its beauty brands
        # are Beauty of Joseon, Benefit, Clinique, Color Wow, Estee Lauder,
        # Hair by Sam McKnight and Medicube -- corroborating the 404s that
        # `prepare` reports for the other five ELC brands.
        index = ms._nav_brand_index(response)
        beauty = {n for p, n in index.items() if p.startswith("/l/beauty/")}
        check("M&S's beauty brand list is read",
              {"Clinique", "Estée Lauder"} <= beauty, sorted(beauty))
        check("and it names no other ELC brand",
              not ({"MAC", "Tom Ford", "Jo Malone", "Bobbi Brown", "Too Faced"}
                   & beauty),
              sorted(beauty))
        # The brand pages are queued as listings already. Seeding one as a
        # campaign page too would give one URL two callbacks, and the
        # deduplication that follows silently cost Boots its listing once.
        seeds = set(ms.campaign_seed_urls(["Clinique"]))
        listings = {p.url for p in ms.product_listing_urls("Clinique", [], 4)}
        check("campaign seeds never collide with listings", not (seeds & listings),
              seeds & listings)

        # M&S 301s /l/beauty to /c/beauty. Recognising only the requested
        # path meant the hub was never identified on arrival, and a run that
        # should have found 29 campaigns found 1.
        check("the hub is recognised after its redirect",
              ms._is_campaign_hub("https://www.marksandspencer.com/c/beauty"))
        check("and at the address it was asked for",
              ms._is_campaign_hub("https://www.marksandspencer.com/l/beauty/"))
        # Every page carries the same menu, so reading it anywhere else would
        # attach M&S's whole promotion list to every product found.
        check("a brand listing is not treated as the hub",
              not ms._is_campaign_hub("https://www.marksandspencer.com/l/beauty/clinique"))
        hub_only = ms.extract_campaigns(
            FakeResponse(listing, "https://www.marksandspencer.com/l/beauty/clinique")
        )
        check("so a listing yields no menu campaigns", hub_only == [], len(hub_only))

    # ------------------------------------------------------------------
    print("\n=== Amazon: refuses non-UK pricing ===")
    amazon = get_adapter("amazon")
    path = FIXTURES / "amazon_search_inr.html"
    if not path.exists():
        print("  MISS amazon_search_inr.html not saved")
        failures += 1
    else:
        response = FakeResponse(path, "https://www.amazon.co.uk/s?k=clinique&i=beauty")
        products = amazon.extract_products_from_listing(response, None)

        # The page genuinely holds 60 result cards, so an empty result must
        # be the guard firing rather than a parser that found nothing.
        cards = response.css('[data-component-type="s-search-result"]')
        check("the fixture really holds result cards", len(cards) >= 50, len(cards))
        check("NO products are emitted", products == [], len(products))
        check("records were refused on currency",
              amazon.wrong_currency_seen > 0, amazon.wrong_currency_seen)
        check("the wrong currency is reported",
              "INR" in amazon.currencies_seen, amazon.currencies_seen)
        check("GBP was never seen on this page",
              "GBP" not in amazon.currencies_seen, amazon.currencies_seen)

        # Sponsored placements put the ad label where the brand goes. Reading
        # it would produce records for a brand called "Sponsored".
        check("sponsored placements are skipped",
              amazon.sponsored_skipped > 0, amazon.sponsored_skipped)

        check("the run is warned before it starts",
              any("UK IP" in w for w in amazon.prepare(["Clinique"])))

        # Brand evidence must NOT be treated as strong: a marketplace brand
        # line is rendered text, not structured data.
        from retailscraper.validation import STRONG_BRAND_EVIDENCE  # noqa: E402
        check("amazon brand evidence is not classed as strong",
              "amazon_brand_line" not in STRONG_BRAND_EVIDENCE)

        check("ASIN parses out of a product URL",
              amazon.product_id_from_url("https://www.amazon.co.uk/dp/B0847KBZ14") == "B0847KBZ14")

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
