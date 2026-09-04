"""Marks & Spencer adapter.

M&S is the simplest of the browser-free retailers, but it needed the most
care about *where* to look.

**Search is off-limits.** robots.txt disallows `/*search?q=` outright, so the
search route that works for ASOS is not available here. Discovery uses the
sitemap instead, which robots.txt declares.

**Beauty lives in its own sitemap.** `uk_sitemap_brands_products.xml` sounds
like the right file and contains 623 URLs, none of them a beauty brand. The
ELC products are in `uk_sitemap_beauty_products.xml` (1,756 URLs). Picking
the obvious-sounding file would have returned zero and looked like "M&S does
not stock these brands".

**M&S stocks Clinique and nothing else from this list.** Verified by scanning
every UK product sitemap: 19 Clinique URLs, and no MAC, Estee Lauder, Bobbi
Brown, Too Faced, Tom Ford or Jo Malone anywhere.

**Plain HTTP is enough.** Product pages return complete JSON-LD without a
browser, which makes this the cheapest adapter after AllBeauty.

Product pages carry:

    JSON-LD Product .brand.name              -> the brand, stated
                    .sku                     -> the stock code
                    .offers.priceSpecification.price / min / max
                    .offers.availability
    "previousPrice": 26                      -> the was-price

The was-price is NOT in the JSON-LD -- `priceSpecification` holds only the
current price -- so it is read from the page's embedded state, the same
split Lookfantastic has.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..models import SCOPE_PRODUCT, SCOPE_SITEWIDE, Campaign, Product
from ..normalize import canonical_url, clean_text, extract_promo_code, percent_off
from ..promotions import classify_promotion, is_confident_offer
from .base import RetailerAdapter, register

# Product URLs end in /p/<code>, e.g. /clinique-for-men-cream-shave-125ml/p/hbp60562352
_PRODUCT_URL_RE = re.compile(
    r"^https?://(?:www\.)?marksandspencer\.com/[^?#]*/p/([a-z0-9]+)/?$", re.I
)

# The was-price, which the JSON-LD does not carry.
_PREVIOUS_PRICE_RE = re.compile(r'"previousPrice"\s*:\s*"?(\d+(?:\.\d+)?)"?')


@register
class MarksAndSpencerAdapter(RetailerAdapter):
    """Extraction rules for marksandspencer.com."""

    name = "marksandspencer"
    domain = "www.marksandspencer.com"
    display_name = "Marks & Spencer"

    #: The beauty products sitemap. Deliberately NOT the brands sitemap --
    #: see the module docstring.
    sitemap_urls = [
        "https://www.marksandspencer.com/sitemap/uk_sitemap_beauty_products.xml"
    ]

    # The product page states no category we can rely on, and search (which
    # would allow a category facet) is disallowed by robots.txt.
    supports_categories = False

    def configure_session(self, manager) -> None:
        """Plain HTTP: product pages carry full JSON-LD without a browser."""
        from scrapling.fetchers import FetcherSession

        manager.add("default", FetcherSession())

    # --- discovery --------------------------------------------------------

    @staticmethod
    def _brand_slug(brand: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", brand.strip().lower()).strip("-")

    def is_product_url(self, url: str) -> bool:
        return bool(_PRODUCT_URL_RE.match(url.split("?")[0]))

    def product_id_from_url(self, url: str) -> Optional[str]:
        match = _PRODUCT_URL_RE.match(url.split("?")[0])
        return match.group(1) if match else None

    def select_candidates(self, urls: Iterable[str], brands: Sequence[str]) -> List[str]:
        """Keep sitemap URLs whose slug mentions a requested brand.

        A cheap pre-filter only: M&S puts the brand at the start of the slug,
        which is accurate enough to avoid fetching 1,756 beauty products to
        find 19. The brand is still proved from each page's JSON-LD.
        """
        slugs = [self._brand_slug(b) for b in brands]
        keep: List[str] = []
        for url in urls:
            if not self.is_product_url(url):
                continue
            lowered = url.lower()
            if not slugs or any(slug in lowered for slug in slugs):
                clean = canonical_url(url)
                if clean not in keep:
                    keep.append(clean)
        return keep

    # --- extraction -------------------------------------------------------

    def extract_product(
        self, response, target_brands: Sequence[str] = ()
    ) -> Optional[Product]:
        url = str(response.url)
        product_id = self.product_id_from_url(url)
        if not product_id:
            return None

        block = self._product_json_ld(response)
        if not block:
            return None

        brand_field = block.get("brand")
        brand = None
        if isinstance(brand_field, dict):
            brand = clean_text(brand_field.get("name"))
        elif isinstance(brand_field, str):
            brand = clean_text(brand_field)
        if not brand:
            return None

        offers = block.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else None
        offers = offers if isinstance(offers, dict) else {}
        spec = offers.get("priceSpecification")
        spec = spec if isinstance(spec, dict) else {}

        current = self._money(spec.get("price"))
        low = self._money(spec.get("minPrice"))
        high = self._money(spec.get("maxPrice"))
        # A genuine spread means several sizes behind one page, so the quoted
        # figure is a from-price rather than the product's price.
        price_is_from = (
            low is not None and high is not None and high > low
        )
        if current is None:
            current = low
        if current is None:
            return None

        original = self._previous_price(response)
        if original is not None and original <= current:
            original = None

        availability = (offers.get("availability") or "").rsplit("/", 1)[-1] or None

        return Product(
            retailer=self.display_name,
            brand=brand,
            brand_verified_by="json_ld_brand",
            product_id=product_id,
            product_title=clean_text(block.get("name")) or "",
            product_url=canonical_url(url),
            source_url=canonical_url(url),
            sku=clean_text(block.get("sku")),
            current_price=current,
            price_is_from=price_is_from,
            original_price=original,
            discount_amount=(
                round(original - current, 2) if original is not None else None
            ),
            discount_percent=percent_off(original, current),
            currency=clean_text(spec.get("priceCurrency")) or "GBP",
            availability=availability,
            category=None,
            variant_count=None,
            # The product code in the URL ("hbp60562352") and the SKU
            # ("60562352") are related but not equal, so comparing them would
            # reject every record.
            sku_matches_product_id=False,
            promotional_copy=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _money(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _previous_price(response) -> Optional[float]:
        """The was-price, which the JSON-LD does not carry."""
        body = response.body
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        match = _PREVIOUS_PRICE_RE.search(body)
        if not match:
            return None
        try:
            return round(float(match.group(1)), 2)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _product_json_ld(response) -> Optional[Dict[str, Any]]:
        for node in response.css('script[type="application/ld+json"]'):
            raw = node.text
            if not raw:
                continue
            try:
                data = json.loads(raw.strip())
            except (ValueError, TypeError):
                continue
            for item in data if isinstance(data, list) else [data]:
                if isinstance(item, dict) and item.get("@type") == "Product":
                    return item
        return None

    def extract_campaigns(self, response) -> List[Campaign]:
        """Offers stated on the page.

        Anything on a product page is scoped to that product; anything
        elsewhere is left sitewide, because M&S states no brand or category
        reach in its promotional copy.
        """
        url = str(response.url)
        on_product = self.is_product_url(url)
        campaigns: List[Campaign] = []
        seen: set = set()

        for node in response.css("[class*='promo'], [class*='offer'], [class*='banner'], h1, h2"):
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
                    scope=SCOPE_PRODUCT if on_product else SCOPE_SITEWIDE,
                    scope_value=None,
                    promo_code=extract_promo_code(text),
                    source_url=canonical_url(url),
                    landing_url=None,
                )
            )
        return campaigns
