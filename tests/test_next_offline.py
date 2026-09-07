"""Next extraction, against saved real pages.

Next is the retailer that looked unscrapable: Akamai Bot Manager answers 403
to plain HTTP, to curl with a full Chrome header set, to a warmed-up session
carrying Akamai's own cookies, and to a stealth browser. What gets through is
a trusted TLS handshake, and the adapter documents which ones work.

That makes one check here more important than the extraction itself: the
adapter must keep asking for a fingerprint Next actually serves. A Chrome
value looks harmless and returns 403 or a 2 KB stub, which would read as
"Next stocks nothing" rather than as a broken adapter.

    python tests/test_next_offline.py
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapling.parser import Selector  # noqa: E402

from retailscraper.adapters.base import get_adapter  # noqa: E402
from retailscraper.adapters.next import _IMPERSONATE  # noqa: E402
from retailscraper.validation import validate  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FakeResponse:
    def __init__(self, html: str, url: str):
        self._sel = Selector(content=html)
        self.url = url
        # Next's product links and JSON-LD are read from the raw body.
        self.html_content = html

    def css(self, query):
        return self._sel.css(query)


def run() -> int:
    adapter = get_adapter("next")
    failures = 0

    def check(label, ok, detail=None):
        nonlocal failures
        failures += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label}"
              f"{('  ' + str(detail)) if detail is not None and not ok else ''}")

    print("=== the TLS fingerprint is load-bearing ===")
    # firefox and safari are served the page; every Chrome value tested was
    # refused or handed a 2 KB stub. Akamai scrutinises Chrome hardest,
    # presumably because it is what scrapers reach for first.
    check("a fingerprint Next serves is requested",
          _IMPERSONATE in {"firefox", "safari"}, _IMPERSONATE)

    print("\n=== brand slugs come from Next's own sitemap ===")
    # Next drops the accent rather than folding it: "Estée Lauder" is
    # `este-lauder`, not `estee-lauder`. Searching for "estee" found nothing
    # and made Next look like it stocked none of these brands at all.
    for brand, want in [
        ("Estee Lauder", "este-lauder"),
        ("Estée Lauder", "este-lauder"),
        ("Clinique", "clinique"),
    ]:
        got = adapter._brand_slug(brand)
        check(f"{brand:14} -> {got}", got == want, got)

    print("\n=== product URLs ===")
    for label, url, want in [
        ("a product", "https://www.next.co.uk/style/su449110/af1610", "su449110-af1610"),
        ("with a fragment", "https://www.next.co.uk/style/su449110/af1610#af1610",
         "su449110-af1610"),
        ("a brand page is not a product",
         "https://www.next.co.uk/brands/este-lauder", None),
    ]:
        got = adapter.product_id_from_url(url)
        check(f"{label:30} -> {got}", got == want, got)

    print("\n=== links are harvested from a brand listing ===")
    listing = FIXTURES / "next_listing.html"
    if not listing.exists():
        print("  MISS next_listing.html not saved")
        failures += 1
    else:
        html = listing.read_text(encoding="utf-8", errors="replace")
        response = FakeResponse(html, "https://www.next.co.uk/brands/este-lauder")
        links = adapter.extract_product_links(response, "Estee Lauder")
        check("products found", len(links) >= 10, len(links))
        check("every one is a product url",
              all(adapter.is_product_url(u) for u in links))
        # Next prints each product twice, as an image link and a title link,
        # with differing fragments.
        check("no duplicates", len(links) == len(set(links)), len(links))

    print("\n=== a product page ===")
    product_page = FIXTURES / "next_product.html"
    if not product_page.exists():
        print("  MISS next_product.html not saved")
        failures += 1
    else:
        html = product_page.read_text(encoding="utf-8", errors="replace")
        response = FakeResponse(
            html, "https://www.next.co.uk/style/su449110/af1610"
        )
        product = adapter.extract_product(response, ["Estee Lauder"])
        check("a product is extracted", product is not None)

        if product is not None:
            for field, want in [
                ("brand", "Estée Lauder"),
                ("brand_verified_by", "json_ld_brand"),
                ("product_id", "su449110-af1610"),
                ("currency", "GBP"),
                ("price_is_from", False),
                # Next publishes no was-price anywhere, so claiming a
                # discount would be inventing one.
                ("original_price", None),
                ("discount_percent", None),
            ]:
                got = getattr(product, field)
                check(f"{field:20} == {want!r}", got == want, got)

            check("a price is read", isinstance(product.current_price, float)
                  and product.current_price > 0, product.current_price)
            check("the title survives", bool(product.product_title),
                  product.product_title)
            # The accented brand must still match the plain name the
            # business asks for.
            check("validates against the requested brand",
                  validate(product, ["Estee Lauder"]) is None,
                  validate(product, ["Estee Lauder"]))
            # Three Product blocks appear on a Next page and only the first
            # carries brand, sku and offers; the others repeat the name with
            # nulls, and reading one of those rejects the record.
            check("the block with the offer is the one read",
                  product.sku is not None, product.sku)

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
