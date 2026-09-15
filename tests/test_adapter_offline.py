"""Exercise the Lookfantastic adapter against real captured HTML.

The fixtures in `tests/fixtures/` are genuine pages saved from the live site,
covering the three layouts that behave differently:

  * lf_product.html -- discounted, single variant  (RRP + current price)
  * lf_variant.html -- discounted, 33 shades       (ProductGroup JSON-LD)
  * lf_jm.html      -- full price, no discount     (bare price, no labels)

Running against fixtures means extraction can be re-checked after any change
without sending a single request to the retailer.

    python tests/test_adapter_offline.py
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapling.parser import Selector  # noqa: E402

from retailscraper.adapters.lookfantastic import LookfantasticAdapter  # noqa: E402
from retailscraper.promotions import looks_like_offer  # noqa: E402
from retailscraper.validation import validate  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

# (label, fixture, url, expected fields) -- expectations were read off the
# live pages at capture time.
CASES = [
    (
        "discounted, single variant",
        "lf_product.html",
        "https://www.lookfantastic.com/p/clinique-moisture-surge-100-hour-auto-replenishing-hydrator-50ml/12849046/",
        {"brand": "Clinique", "product_id": "12849046", "sku": "12849046",
         "variant_count": 1, "original_price": 42.0, "current_price": 31.5},
    ),
    (
        "discounted, 33 shades",
        "lf_variant.html",
        "https://www.lookfantastic.com/p/clinique-even-better-clinical-serum-foundation-spf20-30ml-various-shades/13035221/",
        {"brand": "Clinique", "product_id": "13035221", "sku": None,
         "variant_count": 33, "original_price": 39.0, "current_price": 29.25},
    ),
    (
        "full price, no discount",
        "lf_jm.html",
        "https://www.lookfantastic.com/p/jo-malone-london-basil-and-neroli-cologne-100ml/12078995/",
        {"brand": "Jo Malone London", "product_id": "12078995",
         "original_price": None, "current_price": 125.0},
    ),
]


def run() -> int:
    adapter = LookfantasticAdapter()
    failures = 0

    for label, filename, url, expected in CASES:
        html = (FIXTURES / filename).read_text(encoding="utf-8", errors="replace")
        product = adapter.parse_product(Selector(content=html), url)

        print(f"\n=== {label} ===")
        if product is None:
            print("  FAIL: no product extracted")
            failures += 1
            continue

        for field, want in expected.items():
            got = getattr(product, field)
            status = "ok  " if got == want else "FAIL"
            if got != want:
                failures += 1
            print(f"  {status} {field:16} expected={want!r} got={got!r}")

        # The brand must come from the retailer's own taxonomy, never a slug.
        if product.brand_verified_by != "breadcrumb_link":
            print(f"  FAIL brand_verified_by   got={product.brand_verified_by!r}")
            failures += 1

        # A merchandising badge ("NEW IN") must never reach promotional_copy.
        copy = product.promotional_copy
        badge_ok = copy is None or looks_like_offer(copy)
        if not badge_ok:
            print(f"  FAIL promo copy is an offer, got {copy!r}")
            failures += 1
        else:
            print(f"  ok   promo copy is an offer   {copy!r}")

        rejection = validate(product, [product.brand])
        print(f"  {'ok  ' if rejection is None else 'FAIL'} validation      "
              f"{rejection.reason if rejection else 'ACCEPTED'}")
        if rejection is not None:
            failures += 1

        campaigns = adapter.parse_campaigns(Selector(content=html), url)
        scopes = sorted({c.scope for c in campaigns})
        print(f"  ---- campaigns       {len(campaigns)} found, scopes={scopes}")
        for campaign in campaigns:
            print(f"       [{campaign.scope:9}] {campaign.promotion_type:20} "
                  f"{campaign.promotion_text[:52]}")

    print("\n=== a title Lookfantastic truncates is repaired from its own h1 ===")
    # Lookfantastic's ProductGroup JSON-LD cuts some names at a hyphen. These
    # are the real pairs from the live site: the `name` it publishes, and the
    # `<h1>` on the same page. About 20 products in a 1,069-product run are
    # affected, and each is unmatchable against another retailer until fixed.
    TITLE_CASES = [
        # (json-ld name, page h1, expected)
        ("Clinique Anti",
         " Clinique Anti-Blemish Solutions Liquid Makeup 30ml (Various Shades) ",
         "Clinique Anti-Blemish Solutions Liquid Makeup 30ml (Various Shades)"),
        ("TOM FORD Ultra",
         "TOM FORD Ultra-Shine Lip Color 3.3g (Various Shades)",
         "TOM FORD Ultra-Shine Lip Color 3.3g (Various Shades)"),
        # Not truncated: the h1 must not override a name that is already whole.
        ("Clinique for Men Aloe Shave Gel 125ml",
         "Clinique for Men Aloe Shave Gel 125ml",
         "Clinique for Men Aloe Shave Gel 125ml"),
        # The h1 disagrees without being a continuation of the name. Choosing
        # between two things the retailer said is not repair, so the
        # structured data stands.
        ("Clinique Smart Clinical Repair Serum 50ml",
         "Buy Clinique Smart Clinical Repair Serum",
         "Clinique Smart Clinical Repair Serum 50ml"),
        # No h1 at all.
        ("MAC Studio Fix Fluid SPF15 30ml", None,
         "MAC Studio Fix Fluid SPF15 30ml"),
    ]
    for name, heading, expected in TITLE_CASES:
        head_html = f"<h1>{heading}</h1>" if heading is not None else ""
        sel = Selector(content=f"<html><body>{head_html}</body></html>")
        got = adapter._product_title({"name": name}, sel)
        ok = got == expected
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {name[:38]:40} -> {got!r}")

    failures += offer_page_checks()

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


def offer_page_checks() -> int:
    """Offers pages: their heading, their own products, and the pages after them.

    A run used to read 2 of the 57 offer pages Lookfantastic lists, and never
    tied an offer to the products it covers. "15% Off Selected | Use Code:
    TREAT" lists Clinique products whose own pages only mention code SAVE.
    """
    from retailscraper.models import SCOPE_SITEWIDE, Campaign, Product
    from retailscraper.spider import RetailPromotionSpider

    failures = 0

    def check(label, ok, detail=""):
        nonlocal failures
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  {detail}" if not ok else ""))

    adapter = LookfantasticAdapter()
    offer_url = "https://www.lookfantastic.com/c/offers/tiered/email/4/"
    page_one = Selector(url=offer_url, content="""<html><body>
        <a class="strip-banner" href="/c/offers/tiered/">UP TO 20% OFF SELECTED BEAUTY | USE CODE: SAVE</a>
        <a href="/c/offers/auto/save-25/">25% Off</a>
        <h1>15% Off Selected | Use Code: TREAT</h1>
        <div id="product-list">
          <a href="/p/clinique-take-the-day-off-cleansing-balm-125ml/11144730/">balm</a>
          <a href="/p/clinique-take-the-day-off-cleansing-balm-125ml/11144730/">balm again</a>
          <a href="/p/clinique-moisture-surge-100-hour-50ml/12959012/">surge</a>
          <a href="/p/tom-ford-taormina-orange-eau-de-parfum-50ml/17784989/">New Taormina Orange 20% off</a>
        </div>
        <a href="/p/some-recommended-thing/10000001/">you may also like</a>
        <a href="/c/offers/tiered/email/4/?pageNumber=2">2</a>
    </body></html>""")

    print("\n=== an offers page: its heading is its offer ===")
    campaigns = adapter.extract_campaign_directory(page_one)
    texts = [c.promotion_text for c in campaigns]
    heading = next((c for c in campaigns if c.promotion_text.startswith("15% Off Selected")), None)
    check("the heading becomes a campaign", heading is not None, texts)
    check("landing on its own page, so its product list decides",
          heading is not None and heading.landing_url == offer_url)
    check("a shop-by-saving filter is not a campaign", "25% Off" not in texts, texts)
    check("a product tile is not a campaign",
          not any("Taormina" in t for t in texts), texts)
    sale = Selector(url="https://www.lookfantastic.com/c/brands/mac/sale/",
                    content="<html><body><h1>MAC Cosmetics Sale</h1></body></html>")
    mac = [c for c in adapter.extract_campaign_directory(sale) if c.promotion_text == "MAC Cosmetics Sale"]
    check("'MAC Cosmetics Sale' counts, though it names no discount", len(mac) == 1)
    check("and is scoped to the brand",
          bool(mac) and (mac[0].scope, mac[0].scope_value) == ("brand", "Mac"),
          mac and (mac[0].scope, mac[0].scope_value))
    faqs = Selector(url="https://www.lookfantastic.com/c/offers/charity-donation/faqs/",
                    content="<html><body><h1>FAQs</h1></body></html>")
    check("a heading that is not an offer is ignored", adapter.extract_campaign_directory(faqs) == [])

    print("\n=== an offers page: only its grid counts as its products ===")
    ids = adapter.offer_page_products(page_one)
    check("grid products, once each, recommendations left out",
          ids == ["11144730", "12959012", "17784989"], ids)

    print("\n=== an offers page: what to read next ===")
    links = adapter.offer_page_links(page_one)
    check("the next page, while the grid has products",
          offer_url + "?pageNumber=2" in links, links)
    check("the landing page of each offer linked",
          "https://www.lookfantastic.com/c/offers/tiered/" in links, links)
    check("not the shop-by-saving filter pages",
          not any("/offers/auto/" in u for u in links), links)
    check("not product pages", not any("/p/" in u for u in links), links)
    empty_last = Selector(url=offer_url + "?pageNumber=9", content="""<html><body>
        <div id="product-list"></div><a href="?pageNumber=10">10</a></body></html>""")
    check("an empty grid ends the offer",
          not any("pageNumber=10" in u for u in adapter.offer_page_links(empty_last)))
    no_next = Selector(url=offer_url + "?pageNumber=2", content="""<html><body>
        <div id="product-list"><a href="/p/x/12345678/">x</a></div>
        <a href="?pageNumber=1">1</a><a href="?pageNumber=12">12</a></body></html>""")
    check("page 2 linking only 1 and 12 does not queue page 3",
          not any("pageNumber=3" in u for u in adapter.offer_page_links(no_next)))

    print("\n=== an offer attaches to the products its page lists ===")
    spider = RetailPromotionSpider(adapter=adapter, brands=["Clinique"])

    def product(pid):
        return Product(retailer="Lookfantastic", brand="Clinique", brand_verified_by="breadcrumb_link",
                       product_id=pid, product_title=f"Clinique {pid}",
                       product_url=f"https://www.lookfantastic.com/p/x/{pid}/",
                       source_url=f"https://www.lookfantastic.com/p/x/{pid}/",
                       current_price=10.0, currency="GBP", scraped_at="2026-09-15T00:00:00+00:00")

    listed, unlisted = product("11144730"), product("99999999")
    for item in (listed, unlisted):
        spider.products[item.product_id] = item
    treat = heading
    selected = Campaign(retailer="Lookfantastic", promotion_text="UP TO 20% OFF SELECTED BEAUTY | USE CODE: SAVE",
                        promotion_type="code_discount", scope=SCOPE_SITEWIDE,
                        source_url=offer_url, landing_url="https://www.lookfantastic.com/c/offers/tiered/")
    everywhere = Campaign(retailer="Lookfantastic", promotion_text="Free delivery weekend 20% off",
                          promotion_type="percentage_discount", scope=SCOPE_SITEWIDE,
                          source_url=offer_url, landing_url="https://www.lookfantastic.com/c/never-read/")
    for item in (treat, selected, everywhere):
        spider.campaigns[item.campaign_id] = item

    # both pages of the TREAT offer list the balm; the tiered page lists nothing of ours
    spider.offer_members["https://www.lookfantastic.com/c/offers/tiered/email/4"] = {"11144730", "12959012"}
    spider.offer_members["https://www.lookfantastic.com/c/offers/tiered"] = {"55555555"}
    spider.apply_campaigns()

    check("TREAT reaches the product its page lists", treat.campaign_id in listed.applied_campaigns)
    check("and not one it does not list", treat.campaign_id not in unlisted.applied_campaigns)
    check("a 'site-wide' offer whose page was read goes only where that page says",
          selected.campaign_id not in listed.applied_campaigns)
    check("a site-wide offer whose page was not read still applies everywhere",
          everywhere.campaign_id in listed.applied_campaigns
          and everywhere.campaign_id in unlisted.applied_campaigns)

    return failures


if __name__ == "__main__":
    raise SystemExit(run())
