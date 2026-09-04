"""ASOS extraction, against saved copies of real search pages.

Each check here corresponds to a trap found by inspecting the live site.
None of them are hypothetical.

    python tests/test_asos_offline.py
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retailscraper.adapters.base import get_adapter  # noqa: E402
from retailscraper.validation import validate  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FakeResponse:
    def __init__(self, path: Path, url: str):
        self.body = path.read_bytes()
        self.url = url

    def css(self, query):  # pragma: no cover - campaigns tested separately
        return []


def run() -> int:
    adapter = get_adapter("asos")
    failures = 0

    def check(label, ok, detail=""):
        nonlocal failures
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label}{('  ' + str(detail)) if detail and not ok else ''}")

    url = "https://www.asos.com/search/?q=clinique&page=1"

    print("=== a stocked brand ===")
    path = FIXTURES / "asos_clinique.html"
    if not path.exists():
        print("  MISS asos_clinique.html not saved")
        return 1

    response = FakeResponse(path, url)
    products = adapter.extract_products_from_listing(response, "Clinique")

    check("72 products on a full page", len(products) == 72, len(products))
    check("the page states its total", adapter.total_for_search(response) == "179",
          adapter.total_for_search(response))
    check("every product is Clinique", all(p.brand == "Clinique" for p in products))
    check("brand evidence is ASOS's own field",
          all(p.brand_verified_by == "asos_brand_field" for p in products))
    check("ids are unique", len({p.product_id for p in products}) == len(products))
    check("URLs carry no colourway fragment",
          all("#" not in p.product_url for p in products))
    check("every product validates",
          all(validate(p, ["Clinique"]) is None for p in products))

    print("\n=== `price` is the WAS price, not the current one ===")
    # ASOS reports price: 50, reducedPrice: 39.99 on a reduced item. Reading
    # `price` as the current price would report every discount at full price.
    reduced = [p for p in products if p.original_price is not None]
    check("some products are reduced", len(reduced) >= 5, len(reduced))
    check("current price is below the was-price on every reduced product",
          all(p.current_price < p.original_price for p in reduced))
    check("discount percent agrees with the two prices",
          all(abs(((p.original_price - p.current_price) / p.original_price * 100)
                  - p.discount_percent) < 0.01 for p in reduced))
    # A product with no reduction must not invent a was-price.
    full = [p for p in products if p.original_price is None]
    check("unreduced products have no discount",
          all(p.discount_percent is None for p in full))

    print("\n=== search is not a brand filter ===")
    # Asking ASOS for "Tom Ford" returns 54 products, none of them Tom Ford:
    # Tommy Jeans, Tommy Hilfiger, Toms. ASOS does not stock the brand.
    tf_path = FIXTURES / "asos_tom_ford.html"
    if tf_path.exists():
        tf_response = FakeResponse(tf_path, "https://www.asos.com/search/?q=Tom+Ford")
        tf = adapter.extract_products_from_listing(tf_response, "Tom Ford")
        check("an unstocked brand yields no records", tf == [], len(tf))

        # Without the brand filter the page does hold products -- proving the
        # filter is what rejects them, not an empty page.
        unfiltered = adapter.extract_products_from_listing(tf_response, None)
        check("the page itself is not empty", len(unfiltered) > 0, len(unfiltered))
        check("none of them are Tom Ford",
              not any((p.brand or "").lower() == "tom ford" for p in unfiltered))
    else:
        print("  MISS asos_tom_ford.html not saved")
        failures += 1

    print("\n=== the embedded blob is not valid JSON ===")
    # One product is titled "... Balm- Amp\\' Up Apple". The \\' is legal in a
    # JavaScript string and rejected by json.loads, so without unescaping it
    # the whole page yields nothing.
    raw = path.read_text(encoding="utf-8", errors="replace")
    check("the fixture really does contain a JS-escaped apostrophe",
          "\\'" in raw)
    check("parsing still returns a full page", len(products) == 72)

    print("\n=== identifiers ===")
    check("product id and SKU are different spaces",
          all(p.sku_matches_product_id is False for p in products))
    check("SKU is populated", sum(1 for p in products if p.sku) >= 70)
    check("no category is claimed",
          all(p.category is None for p in products))

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
