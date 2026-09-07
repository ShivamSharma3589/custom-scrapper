"""Amazon UK adapter.

The crawl works. The data does not — from a non-UK IP — and that distinction
is the whole point of this adapter's design.

**Amazon prices by the visitor's location, not by the domain.** Requesting
amazon.co.uk from outside the UK returned international pricing: this adapter
was developed from India and every price came back as `INR 4,239.84`.

`i18n-prefs=GBP` -- Amazon's own currency preference, the cookie its currency
selector sets -- fixes the currency, and the search page went from zero pound
signs to 507 of them. It has to be set as a real browser cookie: this adapter
passed it in `extra_headers` as `Cookie:` for months and it did nothing,
because the browser context keeps its own cookie jar and overrides the
header. Every run therefore saw local pricing, refused every record on the
guard below, and read as "Amazon stocks none of these brands".

Currency is not catalogue. Amazon still varies which offers it shows by
region, so a UK proxy is still what makes the data match what a UK shopper
sees. The guard stays either way.

Those are real numbers, correctly extracted, and completely wrong for a UK
pricing report — the exact "confident nonsense" failure this project exists
to prevent. So this adapter **refuses to emit a record whose price is not in
the expected currency**, and says so loudly, rather than publishing plausible
figures. Point it through a UK proxy (Scrapling's stealth session takes a
`proxy=` argument) and it produces correct data unchanged.

What was verified on the live site:

  * robots.txt permits `/dp/` product pages and `/s?` search; only specific
    sub-paths like `/dp/product-availability/` are disallowed.
  * A stealth browser reaches both; plain HTTP returns a stub.
  * A search page carries 60 result cards, each with `data-asin`, a
    `[data-cy="title-recipe"]` whose first line is the brand, `.a-price` for
    the current price and `.a-text-price` for the was-price.
  * There is NO JSON-LD anywhere on Amazon, so extraction is DOM-class based
    and will need revisiting when Amazon changes its markup.

Two things this adapter deliberately does not do:

  * It does not read the "other sellers" panel. One ASIN can be sold by many
    merchants at different prices; the card price is the buy-box price, and
    that is what a shopper sees. Capturing every seller is a different job.
  * It does not treat its brand evidence as strong. Amazon's brand line is a
    rendered attribute, not structured data, so `--strict-brand` will reject
    these records. That is correct: marketplace listings are inconsistent.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence

from ..models import SCOPE_PRODUCT, SCOPE_SITEWIDE, Campaign, Product
from ..normalize import canonical_url, clean_text, extract_promo_code, percent_off
from ..promotions import (
    classify_promotion,
    is_browse_facet,
    is_confident_offer,
)
from .base import ListingPage, RetailerAdapter, register

_PRODUCT_URL_RE = re.compile(
    r"^https?://(?:www\.)?amazon\.co\.uk/(?:.*/)?(?:dp|gp/product)/([A-Z0-9]{10})", re.I
)

#: Currency symbols Amazon renders, mapped to ISO codes. Anything not in here
#: is treated as "not the currency we asked for" and refused.
_SYMBOLS = {"£": "GBP", "GBP": "GBP", "$": "USD", "€": "EUR", "INR": "INR", "₹": "INR"}

_AMOUNT_RE = re.compile(r"([\d][\d,]*(?:\.\d{1,2})?)")


@register
class AmazonAdapter(RetailerAdapter):
    """Extraction rules for amazon.co.uk."""

    name = "amazon"
    domain = "www.amazon.co.uk"
    display_name = "Amazon UK"

    #: Amazon publishes no sitemap in robots.txt; discovery is search.
    sitemap_urls: List[str] = []

    # Search results carry no category we can rely on.
    supports_categories = False

    #: The only currency this adapter will publish. A record priced in
    #: anything else is refused rather than converted or relabelled.
    EXPECTED_CURRENCY = "GBP"

    #: Search results per page, as Amazon renders them.
    listing_page_size = 60

    def __init__(self) -> None:
        # Counts records dropped for being priced in the wrong currency, so
        # `prepare()` and the run summary can report the real reason a run
        # returned nothing.
        self.wrong_currency_seen = 0
        # Sponsored placements carry the ad label where the brand belongs.
        self.sponsored_skipped = 0
        self.currencies_seen: set = set()

    def configure_session(self, manager) -> None:
        """A stealth browser asking Amazon for sterling.

        `i18n-prefs` is Amazon's own currency preference, the cookie its
        currency selector sets. With it, amazon.co.uk quotes GBP even when
        the request comes from outside the UK -- the search page went from
        zero pound signs to 507 of them.

        It has to be a real browser cookie, not a `Cookie:` header. This
        adapter passed one in `extra_headers` for months and it did nothing:
        the browser context keeps its own cookie jar and overrides the
        header, so every run saw local pricing and refused every record,
        which read as "Amazon stocks none of these brands".

        Currency is not the same as catalogue. Amazon still varies which
        offers it shows by region, so a UK proxy remains the way to see what
        a UK shopper sees; add `proxy="http://user:pass@endpoint:port"` here.
        The currency guard in `extract_product` stays either way.
        """
        from scrapling.fetchers import AsyncStealthySession

        manager.add(
            "default",
            AsyncStealthySession(
                headless=True,
                google_search=True,
                network_idle=True,
                timeout=120_000,
                max_pages=2,
                locale="en-GB",
                cookies=[
                    {"name": "i18n-prefs", "value": "GBP",
                     "domain": ".amazon.co.uk", "path": "/"},
                    {"name": "lc-acbuk", "value": "en_GB",
                     "domain": ".amazon.co.uk", "path": "/"},
                ],
                extra_headers={
                    "Accept-Language": "en-GB,en;q=0.9",
                },
            ),
        )

    def warmup_url(self) -> Optional[str]:
        """The homepage. Amazon challenges a cold session's first navigation."""
        return f"https://{self.domain}/"

    # --- discovery --------------------------------------------------------

    def product_listing_urls(
        self, brand: str, categories: Sequence[str], max_pages: int
    ) -> List[ListingPage]:
        """Paginated beauty search for one brand.

        `i=beauty` scopes the search to the beauty department, which keeps a
        query like "MAC" from returning laptops.
        """
        query = re.sub(r"\s+", "+", brand.strip())
        return [
            ListingPage(
                url=(
                    f"https://{self.domain}/s?k={query}&i=beauty"
                    f"&language=en_GB&page={page}"
                ),
                brand=brand,
                category=None,
            )
            for page in range(1, max_pages + 1)
        ]

    def is_product_url(self, url: str) -> bool:
        return bool(_PRODUCT_URL_RE.match(url.split("?")[0]))

    def product_id_from_url(self, url: str) -> Optional[str]:
        match = _PRODUCT_URL_RE.match(url.split("?")[0])
        return match.group(1).upper() if match else None

    def select_candidates(self, urls: Iterable[str], brands: Sequence[str]) -> List[str]:
        """Unused: discovery reads search result cards, not a URL list."""
        return []

    # --- extraction -------------------------------------------------------

    def extract_products_from_listing(
        self, response, brand: Optional[str] = None
    ) -> List[Product]:
        """Read the search result cards on this page."""
        from ..validation import match_brand

        now = datetime.now(timezone.utc).isoformat()
        targets = [brand] if brand else []
        products: List[Product] = []

        for card in response.css('[data-component-type="s-search-result"]'):
            asin = (card.attrib.get("data-asin") or "").strip().upper()
            if not asin:
                continue

            # The title block renders the brand on its own first line.
            title_node = card.css('[data-cy="title-recipe"]')
            if not title_node:
                continue
            lines = [
                line.strip()
                for line in (title_node[0].get_all_text() or "").splitlines()
                if line.strip()
            ]
            if len(lines) < 2:
                continue

            # A sponsored placement puts the ad label where the brand goes,
            # so its first line reads "Sponsored" and the rest is disclosure
            # text ("You're seeing this ad based on..."). Reading that as a
            # brand produces records for a brand called "Sponsored". The
            # listing's real brand is not in this block at all, so the card
            # is skipped rather than guessed at.
            if lines[0].strip().casefold() == "sponsored":
                self.sponsored_skipped += 1
                continue

            vendor, title = clean_text(lines[0]), clean_text(" ".join(lines[1:]))
            if not (vendor and title):
                continue

            # A search box is not a brand filter: scope the results to the
            # brand we asked for before anything else.
            if targets and match_brand(vendor, targets) is None:
                continue

            current, currency = self._price(card, ".a-price .a-offscreen")
            was, was_currency = self._price(card, ".a-text-price .a-offscreen")
            if current is None:
                continue

            # THE GUARD. A price in the wrong currency is not a UK price, and
            # publishing it would understate or overstate every comparison.
            for seen in (currency, was_currency):
                if seen:
                    self.currencies_seen.add(seen)
            if currency != self.EXPECTED_CURRENCY:
                self.wrong_currency_seen += 1
                continue

            original = was if (was is not None and was > current
                               and was_currency == self.EXPECTED_CURRENCY) else None

            url = canonical_url(f"https://{self.domain}/dp/{asin}")
            products.append(Product(
                retailer=self.display_name,
                brand=vendor,
                # Amazon renders its brand attribute as a line of text rather
                # than publishing structured data. Weaker than a JSON-LD
                # brand, and named so it can be audited; --strict-brand
                # rejects it, which is the right call for a marketplace.
                brand_verified_by="amazon_brand_line",
                product_id=asin,
                product_title=title,
                product_url=url,
                source_url=canonical_url(str(response.url)),
                sku=asin,
                current_price=current,
                original_price=original,
                discount_amount=(
                    round(original - current, 2) if original is not None else None
                ),
                discount_percent=percent_off(original, current),
                currency=self.EXPECTED_CURRENCY,
                availability="InStock",
                category=None,
                variant_count=None,
                promotional_copy=None,
                scraped_at=now,
            ))
        return products

    def _price(self, card, selector: str):
        """(amount, currency) from a price element, or (None, None)."""
        nodes = card.css(selector)
        if not nodes:
            return None, None
        text = clean_text(nodes[0].get_all_text()) or ""

        currency = None
        for symbol, code in _SYMBOLS.items():
            if symbol in text:
                currency = code
                break

        match = _AMOUNT_RE.search(text.replace("\xa0", " "))
        if not match:
            return None, currency
        try:
            return round(float(match.group(1).replace(",", "")), 2), currency
        except ValueError:
            return None, currency

    def prepare(self, brands: Sequence[str]) -> List[str]:
        """Warn before the crawl that this needs a UK IP to be trustworthy."""
        return [
            "Amazon prices by visitor location, not by domain. Unless this "
            "machine has a UK IP (or a UK proxy is configured in "
            "AmazonAdapter.configure_session), every price will be in the "
            "local currency and will be REFUSED rather than published."
        ]

    def extract_product(
        self, response, target_brands: Sequence[str] = ()
    ) -> Optional[Product]:
        """Not used: products come from search cards, not product pages."""
        return None

    def campaign_discovery_urls(self) -> List[str]:
        """Amazon's deals page. Permitted by robots.txt, unlike `/s?k=`
        search which it disallows for some agents.
        """
        return [
        "https://www.amazon.co.uk/deals",
        ]

    def extract_campaign_directory(self, response) -> List[Campaign]:
        """Every offer linked from a hub page, via the shared scan."""
        return self._scan_offer_links(response)

    def extract_campaigns(self, response) -> List[Campaign]:
        """Deal badges and promotional copy on a search or product page."""
        url = str(response.url)
        on_product = self.is_product_url(url)
        campaigns: List[Campaign] = []
        seen: set = set()

        for node in response.css('[class*="badge"], [class*="deal"], [class*="promo"], h1'):
            text = clean_text(node.get_all_text())
            if not text or text in seen or len(text) > 160:
                continue
            if not is_confident_offer(text):
                continue
            # A bare discount tier ("Save 12%") is a shop-by-saving filter
            # or a per-card badge, not a campaign. Recorded as one -- and
            # these scans have no scope to give it but site-wide -- it
            # attaches to every product found, which is how a product
            # discounted 29% came to carry "At Least 70% Off".
            if is_browse_facet(text, url):
                continue
            seen.add(text)
            campaigns.append(Campaign(
                retailer=self.display_name,
                promotion_text=text,
                promotion_type=classify_promotion(text),
                scope=SCOPE_PRODUCT if on_product else SCOPE_SITEWIDE,
                scope_value=None,
                promo_code=extract_promo_code(text),
                source_url=canonical_url(url),
                landing_url=None,
            ))
        return campaigns
