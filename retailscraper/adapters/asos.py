"""ASOS adapter.

Search-driven: no per-brand catalogue URL, but `/search/?q=<brand>&page=N`
is allowed by robots.txt and embeds its whole result set as data. Like
AllBeauty, the listing IS the data -- no product page is ever fetched.

Four things that bite if assumed:

  * plain HTTP hangs rather than failing, so everything uses a browser
  * the embedded blob is not valid JSON -- it holds `\'`, which json.loads
    rejects, so it is unescaped first
  * `price` is the WAS price; `reducedPrice` is what you pay
  * search is not a brand filter -- "Tom Ford" returns Tommy Jeans, so every
    result is checked against its own `brandName`

Not stocked: Tom Ford, Jo Malone.
"""

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..models import SCOPE_SITEWIDE, Campaign, Product
from ..normalize import canonical_url, clean_text, extract_promo_code
from ..promotions import (
    classify_promotion,
    is_browse_facet,
    is_confident_offer,
)
from ..validation import match_brand
from .base import ListingPage, RetailerAdapter, register

# Product URLs end in /prd/<numeric id>, usually with a colourWayId fragment.
_PRODUCT_URL_RE = re.compile(
    r"^https?://(?:www\.)?asos\.com/.+/prd/(\d+)", re.I
)

#: Results per search page, as ASOS renders them.
_PAGE_SIZE = 72

#: "179 styles found" -- the total behind a search, used only for logging.
_TOTAL_RE = re.compile(r"(\d[\d,]*)\s+styles found", re.IGNORECASE)

#: The storefront states its own currency. ASOS geo-prices, so it is read
#: rather than assumed -- a page from outside the UK can quote "$67.89".
_CURRENCY_RE = re.compile(r'"currency"\s*:\s*\{[^}]*?"currency"\s*:\s*"([A-Z]{3})"')


@register
class AsosAdapter(RetailerAdapter):
    """Extraction rules for asos.com."""

    name = "asos"
    domain = "www.asos.com"
    display_name = "ASOS"

    # ASOS's sitemap covers the whole catalogue with no brand scoping, so
    # discovery uses search instead.
    sitemap_urls: List[str] = []

    # The search payload carries no category, so --categories cannot work.
    supports_categories = False

    #: The only currency this adapter will publish.
    EXPECTED_CURRENCY = "GBP"

    def __init__(self) -> None:
        # so a run that returns nothing can say why
        self.wrong_currency_pages = 0
        self.currencies_seen: set = set()

    def configure_session(self, manager) -> None:
        """A browser for everything: plain HTTP hangs rather than failing."""
        from scrapling.fetchers import AsyncStealthySession

        manager.add(
            "default",
            AsyncStealthySession(
                headless=True,
                google_search=True,
                network_idle=True,
                timeout=120_000,
                max_pages=2,
            ),
        )

    # --- discovery --------------------------------------------------------

    def product_listing_urls(
        self, brand: str, categories: Sequence[str], max_pages: int
    ) -> List[ListingPage]:
        """Paginated search results for one brand."""
        query = re.sub(r"\s+", "+", brand.strip())
        return [
            ListingPage(
                url=f"https://{self.domain}/search/?q={query}&page={page}",
                brand=brand,
                category=None,
            )
            for page in range(1, max_pages + 1)
        ]

    def is_product_url(self, url: str) -> bool:
        return bool(_PRODUCT_URL_RE.match(url.split("?")[0]))

    def product_id_from_url(self, url: str) -> Optional[str]:
        match = _PRODUCT_URL_RE.match(url.split("?")[0])
        return match.group(1) if match else None

    def select_candidates(self, urls: Iterable[str], brands: Sequence[str]) -> List[str]:
        """Unused: discovery reads the search payload, not a URL list."""
        return []

    # --- extraction -------------------------------------------------------

    def extract_products_from_listing(
        self, response, brand: Optional[str] = None
    ) -> List[Product]:
        """Turn an ASOS search page into Product records."""
        raw_products = self._embedded_products(response)
        if not raw_products:
            return []

        # refuse the whole page rather than publish non-UK prices
        currency = self.page_currency(response)
        if currency:
            self.currencies_seen.add(currency)
        if currency and currency != self.EXPECTED_CURRENCY:
            self.wrong_currency_pages += 1
            return []

        targets = [brand] if brand else []
        now = datetime.now(timezone.utc).isoformat()

        products = []
        for raw in raw_products:
            vendor = clean_text(raw.get("brandName"))
            # a search for "Tom Ford" returns Tommy Jeans
            if targets and (not vendor or match_brand(vendor, targets) is None):
                continue
            product = self._product_from_json(raw, vendor, now)
            if product is not None:
                products.append(product)
        return products

    def _product_from_json(
        self, raw: Dict[str, Any], vendor: Optional[str], scraped_at: str
    ) -> Optional[Product]:
        product_id = raw.get("id")
        title = clean_text(raw.get("description"))
        path = raw.get("url")
        if not (product_id and title and path and vendor):
            return None

        # `price` is the was-price whenever `reducedPrice` is present.
        was = self._money(raw.get("price"))
        reduced = self._money(raw.get("reducedPrice"))
        current = reduced if reduced is not None else was
        original = was if reduced is not None else None
        if current is None:
            return None
        if original is not None and original <= current:
            original = None

        url = canonical_url(f"https://{self.domain}/{path.lstrip('/')}".split("#")[0])

        return Product(
            retailer=self.display_name,
            brand=vendor,
            brand_verified_by="asos_brand_field",
            product_id=str(product_id),
            product_title=title,
            product_url=url,
            source_url=url,
            sku=str(raw["productCode"]) if raw.get("productCode") else None,
            current_price=current,
            # the colours behind this tile are not all the same price
            price_is_from=bool(raw.get("hasMultiplePrices")),
            original_price=original,
            discount_amount=(
                round(original - current, 2) if original is not None else None
            ),
            discount_percent=(
                round((original - current) / original * 100, 2)
                if original is not None and original > 0
                else None
            ),
            currency=self.EXPECTED_CURRENCY,
            availability="InStock",
            category=None,
            variant_count=None,
            # id and productCode are different numbering schemes
            sku_matches_product_id=False,
            promotional_copy=None,
            scraped_at=scraped_at,
        )

    @staticmethod
    def _money(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return None

    def _embedded_products(self, response) -> List[Dict[str, Any]]:
        """Pull the search results out of the page's embedded state."""
        try:
            body = response.body
        except AttributeError:  # pragma: no cover - defensive
            return []
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")

        marker = '"products":['
        start = body.find(marker)
        if start < 0:
            return []
        start += len('"products":')

        # walk to the matching bracket -- nested objects, so no regex
        depth = 0
        end = None
        for index in range(start, len(body)):
            char = body[index]
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        if end is None:
            return []

        # \' is legal in JS but not JSON -- one apostrophe in one product
        # title would otherwise cost the whole page
        payload = body[start:end].replace("\\'", "'")
        try:
            parsed = json.loads(payload)
        except (ValueError, TypeError):
            return []
        return [p for p in parsed if isinstance(p, dict)]

    def page_currency(self, response) -> Optional[str]:
        """The currency this storefront says its prices are in, or None."""
        body = response.body
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        match = _CURRENCY_RE.search(body)
        return match.group(1) if match else None

    def total_for_search(self, response) -> Optional[str]:
        """The "N styles found" figure, for logging and coverage checks."""
        body = response.body
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        match = _TOTAL_RE.search(body)
        return match.group(1) if match else None

    def extract_product(
        self, response, target_brands: Sequence[str] = ()
    ) -> Optional[Product]:
        """Not used: products come from the search payload, not from pages."""
        return None

    def campaign_discovery_urls(self) -> List[str]:
        """ASOS's outlet, which is where it states its reductions."""
        return [
        "https://www.asos.com/outlet/",
        ]

    def extract_campaign_directory(self, response) -> List[Campaign]:
        """Every offer linked from a hub page, via the shared scan."""
        return self._scan_offer_links(response)

    def extract_campaigns(self, response) -> List[Campaign]:
        """Offers stated on a search or landing page.

        Scoped sitewide: a search page's banner is the same one shown
        everywhere, and nothing ties it to one brand.
        """
        url = str(response.url)
        campaigns: List[Campaign] = []
        seen: set = set()

        for node in response.css("[class*='banner'], [class*='promo'], [class*='Banner'], h1, h2"):
            text = clean_text(node.get_all_text())
            if not text or text in seen or len(text) > 160:
                continue
            if not is_confident_offer(text):
                continue
            # a bare tier ("Save 12%") is a filter, not a campaign -- as a
            # sitewide campaign it would attach to every product
            if is_browse_facet(text, url):
                continue
            seen.add(text)
            campaigns.append(
                Campaign(
                    retailer=self.display_name,
                    promotion_text=text,
                    promotion_type=classify_promotion(text),
                    scope=SCOPE_SITEWIDE,
                    scope_value=None,
                    promo_code=extract_promo_code(text),
                    source_url=canonical_url(url.split("#")[0]),
                    landing_url=None,
                )
            )
        return campaigns
