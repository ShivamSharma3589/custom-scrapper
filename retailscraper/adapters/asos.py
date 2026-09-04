"""ASOS adapter.

ASOS is a search-driven retailer: there is no per-brand catalogue URL, but
`/search/?q=<brand>&page=N` is permitted by robots.txt and embeds its full
result set in the page as data. So, like AllBeauty, the listing IS the
product data and no product page is ever fetched.

Four things were established by inspecting the live site, and each one is a
trap if assumed rather than checked.

**1. Plain HTTP hangs.** A request to robots.txt never returns rather than
being refused -- the same tarpit as John Lewis. Everything uses a browser.

**2. The embedded blob is not valid JSON.** It sits in a JavaScript string
context, so a product called

    Clinique Chubby Stick Cheek Colour Balm- Amp\\' Up Apple

contains `\\'`, which `json.loads` rejects outright. Unescaping it first is
the difference between 72 products and an exception that looks like "ASOS
stocks nothing".

**3. `price` is the WAS price, not the current one.** On a reduced item ASOS
reports `price: 50` and `reducedPrice: 39.99`. Reading `price` as the current
price would report every discounted product at full price -- the exact
failure this project exists to prevent. When `reducedPrice` is absent,
`price` is the only price and there is no discount.

**4. Search is not a brand filter.** Asking for "Tom Ford" returns 54
products, none of them Tom Ford: Tommy Jeans, Tommy Hilfiger, Toms. ASOS
simply does not stock the brand, and the query fuzzy-matched. Every result is
therefore filtered on its own `brandName` field before becoming a record.

Coverage, measured: Clinique 179, MAC 402, Estee Lauder 107, Bobbi Brown 171,
Too Faced 142 (filed as "Too Faced Cosmetics"). Tom Ford and Jo Malone are
not stocked.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..models import SCOPE_SITEWIDE, Campaign, Product
from ..normalize import canonical_url, clean_text, extract_promo_code
from ..promotions import classify_promotion, is_confident_offer
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


@register
class AsosAdapter(RetailerAdapter):
    """Extraction rules for asos.com."""

    name = "asos"
    domain = "www.asos.com"
    display_name = "ASOS"

    # Discovery is the search endpoint, not a sitemap: ASOS publishes one but
    # it covers the whole catalogue with no brand scoping, which would mean
    # walking hundreds of thousands of URLs to find a few hundred.
    sitemap_urls: List[str] = []

    # The search payload carries no category for a product ("productType" is
    # the literal string "Product"), so a category filter cannot be honoured.
    supports_categories = False

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
        """Turn an ASOS search page into Product records.

        `brand` is the brand this search was for. Results are filtered against
        it on their own `brandName`, because the query is a search box and not
        a filter -- see the module docstring.
        """
        raw_products = self._embedded_products(response)
        if not raw_products:
            return []

        targets = [brand] if brand else []
        now = datetime.now(timezone.utc).isoformat()

        products = []
        for raw in raw_products:
            vendor = clean_text(raw.get("brandName"))
            # A search for "Tom Ford" returns Tommy Jeans. Drop anything whose
            # own brand field does not map to what we asked for; validation
            # still proves the brand afterwards.
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
            # ASOS states the brand as its own field on every search result.
            brand_verified_by="asos_brand_field",
            product_id=str(product_id),
            product_title=title,
            product_url=url,
            source_url=url,
            sku=str(raw["productCode"]) if raw.get("productCode") else None,
            current_price=current,
            # `hasMultiplePrices` means the colours behind this tile are not
            # all the same price, so the quoted figure is a from-price.
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
            currency="GBP",
            availability="InStock",
            # The search payload carries no category: `productType` is the
            # literal string "Product" for every result.
            category=None,
            variant_count=None,
            # ASOS numbers products (id) and stock units (productCode)
            # separately, so the two never match by design.
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
        """Pull the search results out of the page's embedded state.

        Returns an empty list when the blob is absent or unparseable, which
        is the honest answer for a search that returned nothing.
        """
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

        # Walk to the matching bracket: the array holds nested objects, so a
        # regex cannot bound it correctly.
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

        # The blob lives in a JS string context where \' is legal; JSON
        # rejects it. Without this, one apostrophe in one product title costs
        # the entire page.
        payload = body[start:end].replace("\\'", "'")
        try:
            parsed = json.loads(payload)
        except (ValueError, TypeError):
            return []
        return [p for p in parsed if isinstance(p, dict)]

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

    def extract_campaigns(self, response) -> List[Campaign]:
        """Offers stated on a search or landing page.

        ASOS advertises site-wide promotions in banner copy. Anything found
        is scoped sitewide: a search page's banner is the same banner shown
        everywhere, and nothing on it ties an offer to one brand.
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
