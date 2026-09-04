"""AllBeauty extraction, against saved copies of the real Shopify API.

AllBeauty is the one retailer whose listing IS the product data, so this
suite exercises `extract_products_from_listing` rather than a page parser.

    python tests/test_allbeauty_offline.py
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retailscraper.adapters.base import get_adapter  # noqa: E402
from retailscraper.validation import validate  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FakeResponse:
    """Stands in for a Scrapling Response over a saved JSON body."""

    def __init__(self, path: Path, url: str):
        self.body = path.read_bytes()
        self.url = url

    def css(self, query):  # pragma: no cover - JSON listings are never queried
        return []


def run() -> int:
    adapter = get_adapter("allbeauty")
    failures = 0

    def check(label, ok, detail=""):
        nonlocal failures
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  ' + str(detail)) if detail and not ok else ''}")

    for name, expect_vendor, least in [
        ("allbeauty_clinique.json", "Clinique", 150),
        # Boots and AllBeauty both file MAC as "M.A.C". The brand matcher has
        # to fold that to "MAC" or every record is rejected as the wrong brand.
        ("allbeauty_mac.json", "M.A.C", 30),
    ]:
        path = FIXTURES / name
        print(f"\n=== {name} ===")
        if not path.exists():
            print(f"  MISS fixture not saved: {name}")
            failures += 1
            continue

        url = "https://allbeauty.com/collections/x/products.json?limit=250&page=1"
        products = adapter.extract_products_from_listing(FakeResponse(path, url))

        check(f"at least {least} products", len(products) >= least, len(products))
        check("every product has the expected vendor",
              all(p.brand == expect_vendor for p in products))
        check("brand evidence is the vendor field",
              all(p.brand_verified_by == "shopify_vendor" for p in products))
        check("every product has an id", all(p.product_id for p in products))
        check("every product has a title", all(p.product_title for p in products))
        check("every product has a price",
              all(p.current_price is not None for p in products))
        check("every product has a category", all(p.category for p in products))
        check("URLs are canonical https",
              all(p.product_url.startswith("https://allbeauty.com/products/")
                  for p in products))
        check("ids are unique",
              len({p.product_id for p in products}) == len(products))

        # Shopify numbers products and stock units separately, so the
        # variant-conflict rule must be switched off or every record is
        # rejected -- which is exactly what happened on the first live run.
        check("sku/id mismatch is declared, not treated as a conflict",
              all(p.sku_matches_product_id is False for p in products))

        # A was-price must be strictly higher than the price. Shopify leaves
        # compare_at_price populated even when nothing is reduced.
        bad = [p for p in products
               if p.original_price is not None and p.original_price <= p.current_price]
        check("no was-price below or equal to the price", not bad, len(bad))

        # A multi-variant product with differing shade prices has no single
        # price, so its low end must be flagged.
        spreads = [p for p in products if p.price_is_from]
        check("multi-price products are flagged as from-prices",
              all((p.variant_count or 0) > 1 for p in spreads))

        rejections = [validate(p, [expect_vendor]) for p in products]
        refused = [r for r in rejections if r is not None]
        check("every product passes validation", not refused,
              refused[0].reason if refused else "")

    print("\n=== the brand matcher handles this retailer's spellings ===")
    from retailscraper.validation import match_brand  # noqa: E402

    targets = ["Clinique", "MAC", "Estee Lauder", "Tom Ford", "Jo Malone"]
    for vendor, want in [("M.A.C", "MAC"), ("Estée Lauder", "Estee Lauder"),
                         ("Clinique", "Clinique"), ("Jo Malone", "Jo Malone")]:
        got = match_brand(vendor, targets)
        check(f"{vendor!r} -> {want!r}", got == want, got)

    print("\n=== collection handles ===")
    # Verified against the live store: these are the handles that return a
    # brand-pure collection. "jo-malone-london" and "m-a-c" return nothing.
    for brand, want in [("Clinique", "clinique"), ("MAC", "mac"),
                        ("Jo Malone", "jo-malone"),
                        ("Jo Malone London", "jo-malone"),
                        ("Estee Lauder", "estee-lauder")]:
        url = adapter.product_listing_urls(brand, [], max_pages=1)[0].url
        check(f"{brand:18} -> /collections/{want}/", f"/collections/{want}/" in url, url)

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
