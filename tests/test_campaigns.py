"""Campaigns-only mode: offer detection, scope resolution, and hub extraction.

Runs against the saved offers-hub pages, so it needs no network.

    python tests/test_campaigns.py
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapling.parser import Selector  # noqa: E402

from retailscraper.adapters.base import get_adapter  # noqa: E402
from retailscraper.models import (  # noqa: E402
    SCOPE_BRAND,
    SCOPE_CATEGORY,
    SCOPE_SITEWIDE,
    SCOPE_UNRESOLVED,
)
from retailscraper.promotions import (  # noqa: E402
    classify_promotion,
    is_browse_facet,
    is_confident_offer,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FakeResponse:
    """Minimal stand-in for a Scrapling Response over saved HTML."""

    def __init__(self, html: str, url: str):
        self._sel = Selector(content=html)
        self.url = url

    def css(self, query):
        return self._sel.css(query)


# Real strings taken off both retailers' offers hubs.
REAL_OFFERS = [
    "Up To 20% Off + Extra 10% Off Selected | Use Code: EXTRA10",
    "LF Seasonal Sale | Up To 50% Off",
    "20% Off Luxury Beauty",
    "Half Price Fragrance",
    "SAVE UP TO 1/2 PRICE",
    "No7 better than half price collections",
    "Christmas 3 for 2 mix & match",
    "free gift card with purchase",
    "Save 15% when you spend £40 on selected baby",
    "Buy 2 Get 1 Free",
]

# Navigation from the same pages. None of these promise anything, and the
# permissive `looks_like_offer` lets most of them through -- which is why
# open-ended scanning uses the strict test instead.
NAVIGATION = [
    "gift finder",
    "gift cards",
    "gift by occasion",
    "fragrance gift sets",
    "Christmas gift guide",
    "free next day delivery",
    "Boots Free Online NHS Repeat Prescription Service",
    "New In Gift Sets",
    "Makeup Gift Sets",
    "Beauty Treats of the Week",
    "NEW IN",
    "Shop All",
]


def run() -> int:
    failures = 0

    def check(label, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label:56} expected={want!r} got={got!r}")

    print("=== real offers are recognised ===")
    for text in REAL_OFFERS:
        check(text[:52], is_confident_offer(text), True)

    print("\n=== navigation is not mistaken for an offer ===")
    for text in NAVIGATION:
        check(text[:52], is_confident_offer(text), False)

    print("\n=== promotion types ===")
    check("percentage", classify_promotion("20% Off Luxury Beauty"), "percentage_discount")
    check("half price counts as percentage",
          classify_promotion("Half Price Fragrance"), "percentage_discount")
    check("fraction counts as percentage",
          classify_promotion("SAVE UP TO 1/2 PRICE"), "percentage_discount")
    check("code", classify_promotion("Extra 10% off | Use Code: EXTRA10"), "code_discount")
    check("bundle", classify_promotion("Christmas 3 for 2 mix & match"), "bundle")
    check("spend threshold",
          classify_promotion("Save £10 when you spend £20 on selected No7"),
          "spend_threshold")

    print("\n=== Lookfantastic reads scope from the link ===")
    lf = get_adapter("lookfantastic")
    check("brand page -> brand scope",
          lf.scope_for_href("https://www.lookfantastic.com/c/brands/medik8/offers/"),
          (SCOPE_BRAND, "Medik8"))
    check("department -> category scope",
          lf.scope_for_href("https://www.lookfantastic.com/c/health-beauty/luxury/offer/"),
          (SCOPE_CATEGORY, "luxury"))
    # "offers" occupies the category slot but names no department.
    check("offers path is sitewide, not a category",
          lf.scope_for_href("https://www.lookfantastic.com/c/health-beauty/offers/winter-sale/"),
          (SCOPE_SITEWIDE, None))
    check("generic offers path -> sitewide",
          lf.scope_for_href("https://www.lookfantastic.com/c/offers/auto/save-50/"),
          (SCOPE_SITEWIDE, None))

    print("\n=== Boots does not guess brand from a path segment ===")
    boots = get_adapter("boots")
    check("known department -> category",
          boots.scope_for_href("https://www.boots.com/fragrance/shop-all"),
          (SCOPE_CATEGORY, "fragrance"))
    check("customer-offer -> sitewide",
          boots.scope_for_href("https://www.boots.com/customer-offer"),
          (SCOPE_SITEWIDE, None))
    check("campaign-list -> sitewide",
          boots.scope_for_href("https://www.boots.com/campaign-list-three"),
          (SCOPE_SITEWIDE, None))
    # /no7/ and /fragrance/ are the same shape, so a brand cannot be asserted.
    check("ambiguous segment stays unresolved",
          boots.scope_for_href("https://www.boots.com/no7/no7-shop-all"),
          (None, None))

    print("\n=== extracting a whole hub page ===")
    for name, adapter, url, least in [
        ("lookfantastic", lf, "https://www.lookfantastic.com/c/health-beauty/offers/view-all/", 5),
        ("boots", boots, "https://www.boots.com/offers", 20),
    ]:
        fixture = FIXTURES / f"{name}_offers.html"
        if not fixture.exists():
            print(f"  MISS fixture not saved: {fixture.name}")
            failures += 1
            continue

        html = fixture.read_text(encoding="utf-8", errors="replace")
        campaigns = adapter.extract_campaign_directory(FakeResponse(html, url))

        check(f"{name}: found at least {least}", len(campaigns) >= least, True)
        check(f"{name}: every campaign has text",
              all(c.promotion_text for c in campaigns), True)
        check(f"{name}: every campaign has a landing url",
              all(c.landing_url for c in campaigns), True)
        check(f"{name}: every scope is legal",
              all(c.scope in {SCOPE_BRAND, SCOPE_CATEGORY, SCOPE_SITEWIDE, SCOPE_UNRESOLVED}
                  for c in campaigns), True)
        # A site-wide campaign applies everywhere by definition, so tagging it
        # to one brand or department would be a claim we cannot support.
        check(f"{name}: sitewide carries no scope_value",
              all(c.scope_value is None for c in campaigns if c.scope == SCOPE_SITEWIDE), True)
        check(f"{name}: no navigation leaked in",
              any(c.promotion_text.lower() in {"gift finder", "shop all", "new in"}
                  for c in campaigns), False)
        # A bare discount tier ("At Least 30% Off" -> /offers-save-30) is a
        # shop-by-saving filter, not a campaign. Recording them made every
        # site-wide campaign attach to every product, so a product discounted
        # 29% carried "At Least 70% Off" alongside seven other tiers.
        tiers = [c.promotion_text for c in campaigns
                 if is_browse_facet(c.promotion_text, c.landing_url)]
        check(f"{name}: no shop-by-saving tiers recorded", tiers, [])

    print("\n=== Boots declares a warmup, Lookfantastic does not ===")
    check("boots warmup is its homepage", boots.warmup_url(), "https://www.boots.com/")
    check("lookfantastic needs none", lf.warmup_url(), None)

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
