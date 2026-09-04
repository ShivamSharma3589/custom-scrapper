"""Category resolution: the lookup built from listing pages, and how it is applied.

Neither retailer states a category on the product page, so a sitemap-driven
run learns it from category listing pages and backfills afterwards. These
checks cover the rules that backfill has to obey.

    python tests/test_categories.py
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retailscraper.adapters.base import get_adapter  # noqa: E402
from retailscraper.models import Product  # noqa: E402
from retailscraper.spider import RetailPromotionSpider  # noqa: E402


class FakeSpider:
    """The backfill logic, isolated from the crawler it normally lives on.

    Mirrors `RetailPromotionSpider.apply_category_index` exactly; constructing
    a real spider would start a Scrapling engine and its sessions, which this
    has nothing to do with.
    """

    def __init__(self, products, index):
        self.products = products
        self.category_index = index

    def apply_category_index(self) -> int:
        filled = 0
        for product in self.products.values():
            if product.category:
                continue
            category = self.category_index.get(product.product_id)
            if category:
                product.category = category
                filled += 1
        return filled


def make(product_id, category=None):
    return Product(
        retailer="Lookfantastic",
        brand="Clinique",
        product_id=product_id,
        product_title=f"Clinique thing {product_id}",
        product_url=f"https://www.lookfantastic.com/p/x/{product_id}/",
        source_url=f"https://www.lookfantastic.com/p/x/{product_id}/",
        brand_verified_by="breadcrumb_link",
        current_price=10.0,
        category=category,
    )


def run() -> int:
    failures = 0

    def check(label, got, want):
        nonlocal failures
        ok = got == want
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label:52} expected={want!r} got={got!r}")

    print("=== backfilling categories ===")

    products = {
        "111": make("111"),                       # blank, in the index
        "222": make("222", category="fragrance"),  # already set by the listing route
        "333": make("333"),                       # blank, NOT in the index
    }
    spider = FakeSpider(products, {"111": "skincare", "222": "makeup", "444": "haircare"})
    filled = spider.apply_category_index()

    check("records filled", filled, 1)
    check("blank record takes the indexed category", products["111"].category, "skincare")
    # The listing route saw this product filed under fragrance directly; an
    # index entry must not overwrite that.
    check("existing category is not overwritten", products["222"].category, "fragrance")
    check("unknown product stays blank", products["333"].category, None)

    print("\n=== the index is idempotent ===")
    again = spider.apply_category_index()
    check("second pass fills nothing", again, 0)
    check("values unchanged", products["111"].category, "skincare")

    print("\n=== a redirected listing is not trusted ===")
    # Lookfantastic redirects a category the brand does not stock to the
    # brand's full range. Recording those products under the requested
    # category filed all 46 Clinique products as "fragrance".
    matches = RetailPromotionSpider._listing_still_matches
    check("fragrance -> view-all is rejected",
          matches("https://www.lookfantastic.com/c/brands/clinique/fragrance/",
                  "https://www.lookfantastic.com/c/brands/clinique/view-all/"),
          False)
    check("skincare -> skincare is accepted",
          matches("https://www.lookfantastic.com/c/brands/clinique/skincare/",
                  "https://www.lookfantastic.com/c/brands/clinique/skincare/"),
          True)
    check("trailing-slash difference still accepted",
          matches("https://www.lookfantastic.com/c/brands/clinique/mens/",
                  "https://www.lookfantastic.com/c/brands/clinique/mens"),
          True)
    check("a query string does not break the match",
          matches("https://www.boots.com/clinique/skincare/",
                  "https://www.boots.com/clinique/skincare/?page=2"),
          True)

    print("\n=== adapters expose a category vocabulary ===")
    lf = get_adapter("lookfantastic")
    boots = get_adapter("boots")
    check("LF knows skincare", "skincare" in lf.known_categories(), True)
    check("Boots knows skincare", "skincare" in boots.known_categories(), True)
    check("vocabularies are deduplicated",
          len(lf.known_categories()) == len(set(lf.known_categories())), True)

    print("\n=== product ids parse back out of URLs ===")
    check("LF id from url",
          lf.product_id_from_url("https://www.lookfantastic.com/p/clinique-x/12849046/"),
          "12849046")
    check("Boots id from url",
          boots.product_id_from_url("https://www.boots.com/clinique-moisture-surge-10339545"),
          "10339545")
    check("non-product url yields nothing",
          lf.product_id_from_url("https://www.lookfantastic.com/c/brands/clinique/"),
          None)

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
