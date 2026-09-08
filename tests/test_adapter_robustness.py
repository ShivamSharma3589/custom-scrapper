"""Every adapter, fed pages it was not expecting.

A scheduled run meets these for real. A retailer under load serves an error
page; a bot check serves a challenge; a CDN serves a truncated response. None
of that should raise, because an exception inside one adapter's extraction
takes down the whole retailer's run -- and, before the crash handling was
added, left neither data nor a manifest behind.

The rule being tested is narrow and absolute: an adapter may return nothing
from a page it cannot read, but it may not raise, and it may never invent a
record out of a page that holds no product.

    python tests/test_adapter_robustness.py
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scrapling.parser import Selector  # noqa: E402

from retailscraper.adapters.base import available_adapters, get_adapter  # noqa: E402
from retailscraper.validation import validate  # noqa: E402

#: Pages a retailer really does serve when something is wrong. The Akamai and
#: Imperva bodies are the shapes Next and Boots return; the truncated JSON-LD
#: is what a cut-off response leaves behind.
HOSTILE_PAGES = {
    "empty": "",
    "whitespace": "   \n\t  ",
    "not html": "\x00\x01\x02 binary rubbish \xff\xfe",
    "access denied": (
        "<html><head><title>Access Denied</title></head><body>"
        "<h1>Access Denied</h1>You don't have permission to access this "
        "resource.</body></html>"
    ),
    "bot challenge": (
        "<html><head><title>Pardon Our Interruption</title></head><body>"
        "<p>As you were browsing something about your browser made us think "
        "you were a bot.</p></body></html>"
    ),
    "server error": "<html><body><h1>503 Service Unavailable</h1></body></html>",
    "truncated json-ld": (
        '<html><head><script type="application/ld+json">{"@type": "Product", '
        '"name": "Half a produ'
    ),
    "json-ld that is not a product": (
        '<html><head><script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Organization","name":"Shop"}'
        "</script></head><body></body></html>"
    ),
    "empty next data": (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        "{}</script></body></html>"
    ),
    "next data that is not json": (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        "{not valid json at all]</script></body></html>"
    ),
    "product page with no price": (
        '<html><head><script type="application/ld+json">'
        '{"@type":"Product","name":"A Thing","brand":{"name":"Clinique"}}'
        "</script></head><body><h1>A Thing</h1></body></html>"
    ),
}


class FakeResponse:
    """A response carrying whatever the retailer actually sent."""

    def __init__(self, body: str, url: str):
        self.url = url
        self.body = body.encode("utf-8", "replace")
        self.html_content = body
        self._sel = Selector(content=body or "<html></html>")

    def css(self, query):
        return self._sel.css(query)


def run() -> int:
    failures = 0
    checked = 0

    print("=== no adapter raises on a page it cannot read ===")
    for name in available_adapters():
        adapter = get_adapter(name)
        # A plausible product URL for this retailer, so extraction is really
        # attempted rather than refused at the URL check.
        url = {
            "lookfantastic": "https://www.lookfantastic.com/p/thing/12345678/",
            "boots": "https://www.boots.com/clinique-thing-10123456",
            "johnlewis": "https://www.johnlewis.com/clinique-thing/p47865",
            "allbeauty": "https://allbeauty.com/products/p-thing",
            "asos": "https://www.asos.com/clinique/thing/prd/12345678",
            "marksandspencer": "https://www.marksandspencer.com/thing/p/hbp60562352",
            "amazon": "https://www.amazon.co.uk/dp/B08XYZ1234",
            "next": "https://www.next.co.uk/style/su449110/af1610",
        }.get(name, f"https://{adapter.domain}/thing")

        for label, body in HOSTILE_PAGES.items():
            response = FakeResponse(body, url)
            for method, args in (
                ("extract_product", (response, ["Clinique"])),
                ("extract_campaigns", (response,)),
                ("extract_products_from_listing", (response, "Clinique")),
                ("extract_product_links", (response, "Clinique")),
                ("extract_product_variants", (response, ["Clinique"])),
                ("extract_campaign_directory", (response,)),
            ):
                checked += 1
                try:
                    result = getattr(adapter, method)(*args)
                except Exception as exc:
                    failures += 1
                    print(f"  FAIL {name}.{method} raised on {label!r}: "
                          f"{type(exc).__name__}: {exc}")
                    continue

                # A record must never REACH THE OUTPUT from a page like this.
                # Returning one is allowed -- Lookfantastic and John Lewis
                # build a record with no price from a stub product page, and
                # validation then rejects it as `missing_price`, which is
                # recorded rather than hidden. What must not happen is a
                # record that survives validation and is published as a fact.
                if method in ("extract_product", "extract_product_variants",
                              "extract_products_from_listing"):
                    made = result if isinstance(result, list) else (
                        [] if result is None else [result]
                    )
                    for record in made:
                        if validate(record, ["Clinique"]) is None:
                            failures += 1
                            print(f"  FAIL {name}.{method} published a record "
                                  f"from {label!r}: {record.product_title!r} "
                                  f"at {record.current_price}")

    print(f"  {checked} calls across {len(list(available_adapters()))} adapters "
          f"and {len(HOSTILE_PAGES)} hostile pages")

    print("\n=== every adapter declares what it can be trusted to know ===")
    for name in available_adapters():
        adapter = get_adapter(name)
        for attribute, kind in (
            ("supports_categories", bool),
            ("confirms_brand_stocking", bool),
            ("display_name", str),
            ("domain", str),
        ):
            value = getattr(adapter, attribute, None)
            ok = isinstance(value, kind) and (value != "" if kind is str else True)
            failures += 0 if ok else 1
            if not ok:
                print(f"  FAIL {name}.{attribute} is {value!r}")
    print(f"  ok   all {len(list(available_adapters()))} adapters declare their capabilities")

    print(f"\n{'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
