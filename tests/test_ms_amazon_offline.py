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
        check("the beauty sitemap is the discovery route",
              any("beauty_products" in u for u in ms.sitemap_urls), ms.sitemap_urls)

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
