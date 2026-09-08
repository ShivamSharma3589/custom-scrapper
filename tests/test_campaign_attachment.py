"""Which campaigns end up on which products.

A site-wide campaign applies to every product by definition, so whether a
product carries it must not depend on when it was found. It used to: campaigns
were attached as each product was accepted, so a campaign discovered on page
40 reached nothing found before it. A real ASOS run gave its single site-wide
campaign to 255 of 775 products and left 520 without it.

    python tests/test_campaign_attachment.py
"""

from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retailscraper.adapters.base import get_adapter  # noqa: E402
from retailscraper.models import (  # noqa: E402
    SCOPE_BRAND,
    SCOPE_SITEWIDE,
    Campaign,
    Product,
)
from retailscraper.spider import RetailPromotionSpider  # noqa: E402


def product(product_id: str, promo=None) -> Product:
    return Product(
        retailer="ASOS",
        brand="Clinique",
        brand_verified_by="asos_brand_field",
        product_id=product_id,
        product_title=f"Clinique Thing {product_id}",
        product_url=f"https://example.test/{product_id}",
        source_url=f"https://example.test/{product_id}",
        current_price=10.0,
        currency="GBP",
        promotional_copy=promo,
        scraped_at=datetime.now(timezone.utc).isoformat(),
    )


def campaign(text: str, scope: str) -> Campaign:
    return Campaign(
        retailer="ASOS",
        promotion_text=text,
        promotion_type="percentage_discount",
        scope=scope,
        scope_value=None,
        source_url="https://example.test/offers",
    )


def run() -> int:
    failures = 0

    def check(label, ok, detail=None):
        nonlocal failures
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label}"
              f"{('  ' + str(detail)) if detail is not None and not ok else ''}")

    spider = RetailPromotionSpider(adapter=get_adapter("asos"), brands=["Clinique"])

    print("=== a site-wide campaign reaches every product ===")
    # Products first, campaign afterwards -- the order a real crawl produces
    # when the offer banner appears on a page late in the run.
    early = [product(f"p{i}") for i in range(5)]
    for item in early:
        spider.products[item.product_id] = item
    late = campaign("20% off everything", SCOPE_SITEWIDE)
    spider.campaigns[late.campaign_id] = late

    touched = spider.apply_campaigns()
    check("every product got it, whenever it was found",
          all(late.campaign_id in p.applied_campaigns for p in early),
          [len(p.applied_campaigns) for p in early])
    check("the count is reported", touched == 5, touched)

    print("\n=== a brand campaign is not stapled to everything ===")
    # Attaching these was the "At Least 70% Off on a 29%-off product" bug:
    # a campaign that covers part of the shop is not a fact about any
    # particular product unless that product says so itself.
    brand_wide = campaign("20% off Clinique", SCOPE_BRAND)
    spider.campaigns[brand_wide.campaign_id] = brand_wide
    spider.apply_campaigns()
    check("brand-scoped campaigns stay off products",
          all(brand_wide.campaign_id not in p.applied_campaigns for p in early))

    print("\n=== a product's own offer is matched to its campaign ===")
    own = product("p-own", promo="Save 25% on this")
    spider.products[own.product_id] = own
    matching = campaign("Save 25% on this", SCOPE_BRAND)
    spider.campaigns[matching.campaign_id] = matching
    spider.apply_campaigns()
    check("the product's own promotion attaches",
          matching.campaign_id in own.applied_campaigns,
          own.applied_campaigns)
    check("and not to products that do not carry it",
          all(matching.campaign_id not in p.applied_campaigns for p in early))

    print("\n=== attachment is repeatable, not cumulative ===")
    # apply_campaigns() runs once per crawl today, but a second call must not
    # double up -- a list that grows on every pass would corrupt the CSV.
    before = list(early[0].applied_campaigns)
    spider.apply_campaigns()
    spider.apply_campaigns()
    check("running it again changes nothing",
          early[0].applied_campaigns == before,
          (before, early[0].applied_campaigns))

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
