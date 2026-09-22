"""Next adapter."""

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..models import SCOPE_PRODUCT, SCOPE_SITEWIDE, Campaign, Product
from ..normalize import canonical_url, clean_text, extract_promo_code, percent_off
from ..promotions import (
    classify_promotion,
    is_browse_facet,
    is_confident_offer,
)
from .base import ListingPage, RetailerAdapter, register

_PRODUCT_URL_RE = re.compile(
    r"^https?://(?:www\.)?next\.co\.uk/style/([a-z0-9]+)/([a-z0-9]+)/?$", re.I
)

_PRODUCT_LINK_RE = re.compile(
    r"https://www\.next\.co\.uk/style/[a-z0-9]+/[a-z0-9]+", re.I
)

_IMPERSONATE = "safari"

_LISTING_PAGE_SIZE = 10


@register
class NextAdapter(RetailerAdapter):
    """Extraction rules for next.co.uk."""

    name = "next"
    domain = "www.next.co.uk"
    display_name = "Next"

    crawl_delay = 2.5
    max_concurrent_requests = 1

    sitemap_urls: List[str] = []

    supports_categories = False

    confirms_brand_stocking = True

    BRAND_SLUG_OVERRIDES = {
        "estee lauder": "este-lauder",
        "estée lauder": "este-lauder",
    }

    def configure_session(self, manager) -> None:
        """A session whose TLS handshake Next trusts."""
        from scrapling.fetchers import FetcherSession

        manager.add(
            "default",
            FetcherSession(
                impersonate=_IMPERSONATE,
                stealthy_headers=True,
                timeout=90,
            ),
        )

    @classmethod
    def _brand_slug(cls, brand: str) -> str:
        key = brand.strip().lower()
        if key in cls.BRAND_SLUG_OVERRIDES:
            return cls.BRAND_SLUG_OVERRIDES[key]
        return re.sub(r"[^a-z0-9]+", "-", key).strip("-")

    def prepare(self, brands: Sequence[str]) -> List[str]:
        """Confirm each brand has a Next brand page before crawling."""
        from scrapling.fetchers import FetcherSession

        warnings: List[str] = []
        with FetcherSession(
            impersonate=_IMPERSONATE, stealthy_headers=True, timeout=60
        ) as session:
            for brand in brands:
                slug = self._brand_slug(brand)
                url = f"https://{self.domain}/brands/{slug}"
                try:
                    response = session.get(url)
                except Exception:
                    continue

                if response.status == 404:
                    warnings.append(
                        f"{self.display_name} has no brand page for {brand!r} "
                        f"({url}) -- it does not stock this brand"
                    )
                elif response.status != 200:
                    warnings.append(
                        f"{self.display_name} returned HTTP {response.status} "
                        f"for {brand!r} ({url}) -- this says nothing about "
                        f"whether the brand is stocked"
                    )
                elif not self._body_has_products(response):
                    warnings.append(
                        f"{self.display_name} lists no products for {brand!r} "
                        f"({url})"
                    )
        return warnings

    @classmethod
    def _body_has_products(cls, response) -> bool:
        return bool(_PRODUCT_LINK_RE.search(cls._body(response)))

    def product_listing_urls(
        self, brand: str, categories: Sequence[str], max_pages: int
    ) -> List[ListingPage]:
        """The brand's listing, paginated with `?p=N`."""
        slug = self._brand_slug(brand)
        base = f"https://{self.domain}/brands/{slug}"
        return [
            ListingPage(
                url=base if page == 1 else f"{base}?p={page}",
                brand=brand,
                category=None,
            )
            for page in range(1, max_pages + 1)
        ]

    def is_product_url(self, url: str) -> bool:
        return bool(_PRODUCT_URL_RE.match(url.split("?")[0].split("#")[0]))

    def product_id_from_url(self, url: str) -> Optional[str]:
        match = _PRODUCT_URL_RE.match(url.split("?")[0].split("#")[0])
        return f"{match.group(1)}-{match.group(2)}".lower() if match else None

    def extract_product_links(self, response, brand: Optional[str] = None) -> List[str]:
        """Product URLs on a brand listing."""
        body = self._body(response)
        found: List[str] = []
        for url in _PRODUCT_LINK_RE.findall(body or ""):
            clean = canonical_url(url)
            if clean not in found:
                found.append(clean)
        return found

    def select_candidates(self, urls: Iterable[str], brands: Sequence[str]) -> List[str]:
        """Every product URL, unfiltered."""
        keep: List[str] = []
        for url in urls:
            if self.is_product_url(url):
                clean = canonical_url(url)
                if clean not in keep:
                    keep.append(clean)
        return keep

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

        current = self._money(offers.get("price"))
        if current is None:
            return None

        currency = clean_text(offers.get("priceCurrency")) or "GBP"
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
            price_is_from=False,
            original_price=None,
            discount_amount=None,
            discount_percent=percent_off(None, current),
            currency=currency,
            availability=availability,
            category=None,
            variant_count=None,
            sku_matches_product_id=False,
            promotional_copy=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _body(response) -> str:
        body = getattr(response, "html_content", None) or getattr(response, "body", None)
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        return body or ""

    @staticmethod
    def _money(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return None

    def _product_json_ld(self, response) -> Optional[Dict[str, Any]]:
        """The `Product` block that carries an offer."""
        for raw in re.findall(
            r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>',
            self._body(response),
            re.DOTALL,
        ):
            try:
                data = json.loads(raw.strip())
            except (ValueError, TypeError):
                continue
            for item in data if isinstance(data, list) else [data]:
                if (
                    isinstance(item, dict)
                    and item.get("@type") == "Product"
                    and item.get("offers")
                ):
                    return item
        return None

    def campaign_discovery_urls(self) -> List[str]:
        """Next's sale and clearance hubs."""
        return [
        "https://www.next.co.uk/sale",
        "https://www.next.co.uk/clearance",
        ]

    def extract_campaign_directory(self, response) -> List[Campaign]:
        """Every offer linked from a hub page, via the shared scan."""
        return self._scan_offer_links(response)

    def extract_campaigns(self, response) -> List[Campaign]:
        """Promotional copy stated on the page."""
        url = str(response.url)
        on_product = self.is_product_url(url)
        campaigns: List[Campaign] = []
        seen: set = set()

        for node in response.css("[class*='promo'], [class*='offer'], [data-testid*='promo'], h1, h2"):
            text = clean_text(node.get_all_text())
            if not text or text in seen or len(text) > 160:
                continue
            if not is_confident_offer(text):
                continue
            if is_browse_facet(text, url):
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
