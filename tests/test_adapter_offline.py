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

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
