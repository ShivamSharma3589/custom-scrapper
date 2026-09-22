"""AllBeauty adapter."""

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from config import first_proxy
from ..models import SCOPE_SITEWIDE, SCOPE_UNRESOLVED, Campaign, Product
from ..normalize import canonical_url, clean_text, extract_promo_code, strip_call_to_action
from ..promotions import (
    classify_promotion,
    is_browse_facet,
    is_confident_offer,
)
from .base import ListingPage, RetailerAdapter, register

_PRODUCT_URL_RE = re.compile(
    r"^https?://(?:www\.)?allbeauty\.com/products/([a-z0-9-]+)/?$", re.I
)

_PAGE_SIZE = 250

_SHOPIFY_CURRENCY_RE = re.compile(
    r'Shopify\.currency\s*=\s*\{[^}]*"active"\s*:\s*"([A-Z]{3})"'
)

@register
class AllBeautyAdapter(RetailerAdapter):
    """Extraction rules for allbeauty.com (Shopify)."""

    name = "allbeauty"
    domain = "allbeauty.com"
    display_name = "AllBeauty"

    sitemap_urls: List[str] = []

    supports_categories = True

    COLLECTION_HANDLES = {
        "mac": "mac",
        "jo malone": "jo-malone",
        "jo malone london": "jo-malone",
        "estee lauder": "estee-lauder",
    }

    CATEGORY_SLUGS = {
        "skincare": "skincare",
        "makeup": "cosmetics",
        "make-up": "cosmetics",
        "make up": "cosmetics",
        "fragrance": "fragrance",
        "haircare": "haircare",
        "hair": "haircare",
        "men": "mens",
        "mens": "mens",
        "gift": "gifts",
        "gifts": "gifts",
    }

    CAMPAIGN_HUB_PATHS = ["/pages/offers"]

    MAX_OFFER_PAGES = 20

    EXPECTED_CURRENCY = "GBP"

    def __init__(self) -> None:
        self.storefront_currency = self.EXPECTED_CURRENCY

    def configure_session(self, manager) -> None:
        """Plain HTTP. There is no JavaScript to run and no bot wall."""
        from scrapling.fetchers import FetcherSession

        self.add_proxied_sessions(manager, lambda proxy: FetcherSession(proxy=proxy))

    def prepare(self, brands: Sequence[str]) -> List[str]:
        """Confirm the storefront is still quoting sterling."""
        from scrapling.fetchers import Fetcher

        try:
            html = Fetcher.get(f"https://{self.domain}/", stealthy_headers=True,
                               timeout=60, proxy=first_proxy()).body.decode(
                "utf-8", "replace"
            )
        except Exception:
            return []

        found = _SHOPIFY_CURRENCY_RE.search(html)
        if not found:
            return [
                f"{self.display_name} did not state its currency, so prices "
                f"are recorded as {self.EXPECTED_CURRENCY} on the store's "
                f"usual behaviour rather than on evidence"
            ]

        self.storefront_currency = found.group(1)
        if self.storefront_currency != self.EXPECTED_CURRENCY:
            return [
                f"{self.display_name} is quoting {self.storefront_currency}, "
                f"not {self.EXPECTED_CURRENCY}. Its prices are NOT comparable "
                f"with the other retailers in this run."
            ]
        return []

    @staticmethod
    def _brand_slug(brand: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", brand.strip().lower()).strip("-")

    def _collection(self, brand: str) -> str:
        """The collection handle for a brand."""
        key = brand.strip().lower()
        return self.COLLECTION_HANDLES.get(key, self._brand_slug(brand))

    def product_listing_urls(
        self, brand: str, categories: Sequence[str], max_pages: int
    ) -> List[ListingPage]:
        """Paginated JSON for one brand's collection."""
        handle = self._collection(brand)
        return [
            ListingPage(
                url=(
                    f"https://{self.domain}/collections/{handle}/products.json"
                    f"?limit={_PAGE_SIZE}&page={page}"
                ),
                brand=brand,
                category=None,
            )
            for page in range(1, max_pages + 1)
        ]

    def campaign_discovery_urls(self) -> List[str]:
        return [f"https://{self.domain}{path}" for path in self.CAMPAIGN_HUB_PATHS]

    def campaign_seed_urls(self, brands: Sequence[str]) -> List[str]:
        """None. The offers page is read on every run, so its offers are followed."""
        return []

    def scope_for_href(self, href: str):
        """AllBeauty's offer links are collection pages."""
        path = href.split(self.domain, 1)[-1]
        if re.search(r"/collections/(sale|offers)/?$", path):
            return (SCOPE_SITEWIDE, None)
        return (None, None)

    @staticmethod
    def _handle(url: str) -> Optional[str]:
        """The collection handle in a collection URL, or None."""
        match = re.search(r"/collections/([a-z0-9-]+)", url or "")
        return match.group(1) if match else None

    @staticmethod
    def _is_saving_tier(handle: str) -> bool:
        """"At Least 30% Off" and similar: a filter by size of saving, not an offer."""
        return bool(re.fullmatch(r"offers-(save|outlet)-\d+", handle))

    def _offer_collections(self, response) -> List[tuple]:
        """(link text, collection handle) for every offer collection linked here."""
        found = {}
        for node in response.css("a[href]"):
            href = node.attrib.get("href") or ""
            handle = self._handle(href)
            if not handle or self._is_saving_tier(handle) or "/products/" in href:
                continue
            text = strip_call_to_action(clean_text(node.get_all_text())) or ""
            offer_text = is_confident_offer(text) and not is_browse_facet(text, href)
            if offer_text or handle.startswith("offers-") or handle in ("offers", "sale"):
                if handle not in found or (offer_text and not is_confident_offer(found[handle])):
                    found[handle] = text
        return [(text, handle) for handle, text in found.items()]

    def extract_campaign_directory(self, response) -> List[Campaign]:
        """Every offer an offers page links to."""
        campaigns = [c for c in self._scan_offer_links(response)
                     if "/products/" not in (c.landing_url or "")]
        worded = {self._handle(c.landing_url or "") for c in campaigns}
        for text, handle in self._offer_collections(response):
            if handle in worded or not text or not handle.startswith("offers-"):
                continue
            campaigns.append(Campaign(
                retailer=self.display_name,
                promotion_text=text,
                promotion_type=classify_promotion(text),
                scope=SCOPE_UNRESOLVED,
                promo_code=extract_promo_code(text),
                source_url=canonical_url(str(response.url)),
                landing_url=f"https://{self.domain}/collections/{handle}",
            ))
        return campaigns

    def offer_page_links(self, response) -> List[str]:
        """What to read after an offers page."""
        url = str(response.url)
        payload = self._json(response)
        if payload is not None:
            page = int((re.search(r"[?&]page=(\d+)", url) or [None, "1"])[1])
            if len(payload.get("products") or []) >= _PAGE_SIZE and page < self.MAX_OFFER_PAGES:
                return [re.sub(r"([?&]page=)\d+", rf"\g<1>{page + 1}", url)]
            return []

        # offer=1 does nothing at Shopify; it just keeps these URLs distinct
        # from the same collection fetched as a brand listing.
        links = [f"https://{self.domain}/collections/{handle}/products.json"
                 f"?limit={_PAGE_SIZE}&page=1&offer=1"
                 for _, handle in self._offer_collections(response)]
        for node in response.css("a[href]"):
            href = (node.attrib.get("href") or "").split("?")[0]
            text = strip_call_to_action(clean_text(node.get_all_text())) or ""
            if re.match(r"(https://allbeauty\.com)?/pages/[a-z0-9-]+$", href) and is_confident_offer(text):
                links.append(href if href.startswith("http") else f"https://{self.domain}{href}")
        return list(dict.fromkeys(links))

    def offer_page_of(self, url: str) -> str:
        """The collection an offer product list belongs to."""
        return re.sub(r"/products\.json.*$", "", url)

    def offer_page_products(self, response) -> List[str]:
        """Ids of the products in an offer collection's product list."""
        payload = self._json(response)
        if not payload:
            return []
        return [str(p["id"]) for p in payload.get("products") or [] if p.get("id")]

    def is_product_url(self, url: str) -> bool:
        return bool(_PRODUCT_URL_RE.match(url.split("?")[0]))

    def product_id_from_url(self, url: str) -> Optional[str]:
        """AllBeauty product URLs carry a handle, not the numeric id."""
        return None

    def select_candidates(self, urls: Iterable[str], brands: Sequence[str]) -> List[str]:
        """Unused: discovery is the collection API, not a URL list."""
        return []

    def extract_products_from_listing(
        self, response, brand: Optional[str] = None
    ) -> List[Product]:
        """Turn a Shopify collection page into complete Product records."""
        payload = self._json(response)
        if not payload:
            return []

        now = datetime.now(timezone.utc).isoformat()
        products = []
        for raw in payload.get("products") or []:
            product = self._product_from_json(raw, now)
            if product is not None:
                products.append(product)
        return products

    def _product_from_json(self, raw: Dict[str, Any], scraped_at: str) -> Optional[Product]:
        """One Shopify product -> one Product record."""
        product_id = raw.get("id")
        vendor = clean_text(raw.get("vendor"))
        title = clean_text(raw.get("title"))
        handle = raw.get("handle")
        if not (product_id and vendor and title and handle):
            return None

        variants = [v for v in (raw.get("variants") or []) if isinstance(v, dict)]
        prices = [self._money(v.get("price")) for v in variants]
        prices = [p for p in prices if p is not None]
        if not prices:
            return None

        current = min(prices)
        price_is_from = len(set(prices)) > 1

        cheapest = min(
            (v for v in variants if self._money(v.get("price")) == current),
            key=lambda v: self._money(v.get("price")),
            default=None,
        )
        original = self._money((cheapest or {}).get("compare_at_price"))
        if original is not None and original <= current:
            original = None

        return Product(
            retailer=self.display_name,
            brand=vendor,
            brand_verified_by="shopify_vendor",
            product_id=str(product_id),
            product_title=title,
            product_url=canonical_url(f"https://{self.domain}/products/{handle}"),
            source_url=canonical_url(f"https://{self.domain}/products/{handle}"),
            sku=clean_text((cheapest or {}).get("sku")) if len(variants) == 1 else None,
            # Shopify numbers products and stock units separately, so they never match.
            sku_matches_product_id=False,
            current_price=current,
            price_is_from=price_is_from,
            original_price=original,
            discount_amount=(
                round(original - current, 2) if original is not None else None
            ),
            discount_percent=(
                round((original - current) / original * 100, 2)
                if original is not None and original > 0
                else None
            ),
            currency=self.storefront_currency,
            availability="InStock" if any(v.get("available") for v in variants) else "OutOfStock",
            category=self._category(raw.get("product_type")),
            variant_count=len(variants) or None,
            promotional_copy=None,
            scraped_at=scraped_at,
        )

    @staticmethod
    def _money(value: Any) -> Optional[float]:
        """Shopify quotes prices as decimal strings ("16.10")."""
        if value in (None, ""):
            return None
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _category(product_type: Optional[str]) -> Optional[str]:
        """The most specific segment of "Cosmetics > Eyeliner"."""
        text = clean_text(product_type)
        if not text:
            return None
        return text.split(">")[-1].strip().lower() or None

    @staticmethod
    def _json(response) -> Optional[Dict[str, Any]]:
        """Parse a JSON response body, or None when it is not JSON."""
        try:
            body = response.body
        except AttributeError:  # pragma: no cover - defensive
            return None
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    def extract_product(
        self, response, target_brands: Sequence[str] = ()
    ) -> Optional[Product]:
        """Not used: products come from the collection API, not from pages."""
        return None

    def extract_campaigns(self, response) -> List[Campaign]:
        """Offers stated on a collection page."""
        if self._json(response) is not None:
            return []

        campaigns = self._scan_offer_links(response)
        seen = {c.promotion_text for c in campaigns}

        url = str(response.url)
        for node in response.css("h1, h2, [class*='banner'], [class*='promo']"):
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
                    scope=SCOPE_UNRESOLVED,
                    scope_value=None,
                    promo_code=extract_promo_code(text),
                    source_url=canonical_url(url),
                    landing_url=None,
                )
            )
        return campaigns
