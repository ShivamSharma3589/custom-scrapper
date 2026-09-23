"""Marks & Spencer adapter."""

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlsplit

from config import first_proxy
from ..models import (
    SCOPE_BRAND,
    SCOPE_CATEGORY,
    SCOPE_PRODUCT,
    SCOPE_SITEWIDE,
    SCOPE_UNRESOLVED,
    Campaign,
    Product,
)
from ..normalize import canonical_url, clean_text, extract_promo_code, percent_off
from ..promotions import classify_promotion, is_browse_facet, is_confident_offer
from .base import ListingPage, RetailerAdapter, register

_PRODUCT_URL_RE = re.compile(
    r"^https?://(?:www\.)?marksandspencer\.com/[^?#]*/p/([a-z0-9]+)/?$", re.I
)

_PREVIOUS_PRICE_RE = re.compile(r'"previousPrice"\s*:\s*"?(\d+(?:\.\d+)?)"?')

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)

_RESULTS_PATH = (
    "props", "pageProps", "serverSideGqlResponseFed",
    "productPageData", "search", "results",
)

_LISTING_PAGE_SIZE = 48

_ID_PREFIX_RE = re.compile(r"^[a-z]+", re.IGNORECASE)


@register
class MarksAndSpencerAdapter(RetailerAdapter):
    """Extraction rules for marksandspencer.com."""

    name = "marksandspencer"
    domain = "www.marksandspencer.com"
    display_name = "Marks & Spencer"

    sitemap_urls: List[str] = []

    supports_categories = False

    confirms_brand_stocking = True

    MAX_LIST_PAGES = 40

    BRAND_SLUG_OVERRIDES = {
        "estée lauder": "estee-lauder",
        "esteé lauder": "estee-lauder",
        "jo malone": "jo-malone-london",
        "jo malone london": "jo-malone-london",
    }

    def configure_session(self, manager) -> None:
        """Plain HTTP: product pages carry full JSON-LD without a browser."""
        from scrapling.fetchers import FetcherSession

        self.add_proxied_sessions(manager, lambda proxy: FetcherSession(proxy=proxy))

    @classmethod
    def _brand_slug(cls, brand: str) -> str:
        """Brand name -> the slug M&S uses in its landing-page URLs."""
        key = brand.strip().lower()
        if key in cls.BRAND_SLUG_OVERRIDES:
            return cls.BRAND_SLUG_OVERRIDES[key]
        return re.sub(r"[^a-z0-9]+", "-", key).strip("-")

    def product_listing_urls(
        self, brand: str, categories: Sequence[str], max_pages: int
    ) -> List[ListingPage]:
        """The brand's landing page. Later pages follow it."""
        slug = self._brand_slug(brand)
        return [ListingPage(url=f"https://{self.domain}/l/beauty/{slug}",
                            brand=brand, category=None)]

    def more_listing_pages(self, response, meta: dict, added: int) -> List[str]:
        """The next page, while M&S says there are more products than we have seen."""
        url = str(meta.get("url") or response.url)
        node = self._results_node(response)
        if not node:
            return []

        shown = len(node.get("products") or [])
        total = node.get("totalItems")
        paging = node.get("pagination") or {}
        offset = paging.get("offset") or 0
        if not isinstance(total, int) or offset + shown >= total:
            return []

        page = int((re.search(r"[?&]page=(\d+)", url) or [None, "1"])[1])
        if page >= self.MAX_LIST_PAGES:
            self._capped.add(meta.get("brand") or url)
            return []
        base = url.split("?")[0]
        return [f"{base}?page={page + 1}"]

    def expected_product_count(self, brands: Sequence[str]) -> Optional[int]:
        """What M&S itself said it lists for these brands."""
        totals = [self._stated_totals[b] for b in brands if b in self._stated_totals]
        return sum(totals) if totals else None

    def crawl_warnings(self) -> List[str]:
        return [f"{brand} has more than {self.MAX_LIST_PAGES * _LISTING_PAGE_SIZE} "
                f"products; the rest were not read" for brand in sorted(self._capped)]

    def __init__(self) -> None:
        self._stated_totals: Dict[str, int] = {}
        self._capped: set = set()

    def prepare(self, brands: Sequence[str]) -> List[str]:
        """Check each brand has a landing page, and note how many products it claims."""
        from scrapling.fetchers import Fetcher

        warnings: List[str] = []
        for brand in brands:
            url = f"https://{self.domain}/l/beauty/{self._brand_slug(brand)}"
            try:
                page = Fetcher.get(url, stealthy_headers=True, timeout=30,
                                   proxy=first_proxy())
                status = page.status
                node = self._results_node(page) or {}
                if isinstance(node.get("totalItems"), int):
                    self._stated_totals[brand] = node["totalItems"]
            except Exception as exc:
                warnings.append(
                    f"{self.display_name} could not be reached for '{brand}' ({exc})"
                )
                continue
            if status == 404:
                warnings.append(
                    f"{self.display_name} has no landing page for "
                    f"'{brand}' ({url}) -- it does not stock this brand"
                )
            elif status != 200:
                warnings.append(
                    f"{self.display_name} returned HTTP {status} for "
                    f"'{brand}' ({url})"
                )
        return warnings

    def is_product_url(self, url: str) -> bool:
        return bool(_PRODUCT_URL_RE.match(url.split("?")[0]))

    def product_id_from_url(self, url: str) -> Optional[str]:
        match = _PRODUCT_URL_RE.match(url.split("?")[0])
        return match.group(1) if match else None

    def select_candidates(self, urls: Iterable[str], brands: Sequence[str]) -> List[str]:
        """Keep sitemap URLs whose slug mentions a requested brand."""
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

    def extract_products_from_listing(
        self, response, brand: Optional[str] = None
    ) -> List[Product]:
        """Read a whole brand landing page out of its embedded state."""
        products: List[Product] = []
        now = datetime.now(timezone.utc).isoformat()

        for record in self._listing_records(response):
            if not isinstance(record, dict):
                continue

            seo_path = (record.get("seoPath") or "").split("?")[0]
            product_id = self.product_id_from_url(
                f"https://{self.domain}{seo_path}"
            )
            record_brand = clean_text(record.get("brand"))
            title = clean_text(record.get("title"))
            if not product_id or not record_brand or not title:
                continue

            price = record.get("price")
            price = price if isinstance(price, dict) else {}
            current, price_is_from = self._listing_price(price.get("listPrice"))
            if current is None:
                continue

            original = self._money(price.get("previousPrice"))
            if original is not None and original <= current:
                original = None

            external_id = clean_text(record.get("productExternalId")) or ""
            variants = record.get("variants")

            products.append(Product(
                retailer=self.display_name,
                brand=record_brand,
                brand_verified_by="ms_brand_field",
                product_id=product_id,
                product_title=title,
                product_url=canonical_url(f"https://{self.domain}{seo_path}"),
                source_url=canonical_url(str(response.url)),
                sku=_ID_PREFIX_RE.sub("", external_id) or None,
                current_price=current,
                price_is_from=price_is_from,
                original_price=original,
                discount_amount=(
                    round(original - current, 2) if original is not None else None
                ),
                discount_percent=percent_off(original, current),
                currency=clean_text(price.get("currency")) or "GBP",
                availability=None,
                category=clean_text(record.get("productDefinition")),
                variant_count=len(variants) if isinstance(variants, list) else None,
                sku_matches_product_id=False,
                promotional_copy=self._listing_promo(record),
                scraped_at=now,
            ))

        return products

    @classmethod
    def _results_node(cls, response) -> Optional[Dict[str, Any]]:
        """The search results block inside the page's `__NEXT_DATA__`."""
        body = response.body
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        match = _NEXT_DATA_RE.search(body or "")
        if not match:
            return None
        try:
            node: Any = json.loads(match.group(1))
        except (ValueError, TypeError):
            return None

        for key in _RESULTS_PATH:
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        return node if isinstance(node, dict) else None

    @classmethod
    def _listing_records(cls, response) -> List[Any]:
        """The product array inside the page's `__NEXT_DATA__`, or empty."""
        node = cls._results_node(response) or {}
        products = node.get("products")
        return products if isinstance(products, list) else []

    @classmethod
    def _listing_price(cls, list_price: Any) -> tuple:
        """(price, is_from) for a listing record's `listPrice`."""
        if not isinstance(list_price, dict):
            return None, False
        single = cls._money(list_price.get("amount"))
        if single is not None:
            return single, False
        low = cls._money(list_price.get("minimumAmount"))
        high = cls._money(list_price.get("maximumAmount"))
        if low is None:
            return None, False
        return low, high is not None and high > low

    @staticmethod
    def _listing_promo(record: Dict[str, Any]) -> Optional[str]:
        """The promotional badge M&S prints on the product's tile."""
        parts: List[str] = []
        for label in record.get("labels") or []:
            if not isinstance(label, dict) or label.get("name") != "Promotion":
                continue
            for value in label.get("values") or []:
                text = clean_text(value)
                if text and text not in parts:
                    parts.append(text)
        return " | ".join(parts) or None

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

    CAMPAIGN_HUB_PATH = "/l/beauty"

    CAMPAIGN_HUB_PATHS = ("/l/beauty", "/c/beauty")

    def _is_campaign_hub(self, url: str) -> bool:
        path = urlsplit(url).path.rstrip("/") or "/"
        return path in self.CAMPAIGN_HUB_PATHS

    def campaign_discovery_urls(self) -> List[str]:
        """One page is enough: M&S ships its whole offer menu in every nav."""
        return [f"https://{self.domain}{self.CAMPAIGN_HUB_PATH}"]

    def campaign_seed_urls(self, brands: Sequence[str]) -> List[str]:
        """Visit the beauty hub on a brand run too, for the same menu."""
        return [f"https://{self.domain}{self.CAMPAIGN_HUB_PATH}"]

    def extract_campaign_directory(self, response) -> List[Campaign]:
        """Every promotion M&S advertises, read out of its navigation."""
        brands_by_path = self._nav_brand_index(response)
        campaigns: List[Campaign] = []
        seen: set = set()

        for name, href in self._nav_entries(response):
            if not is_confident_offer(name) or is_browse_facet(name, href):
                continue
            key = (name, href)
            if key in seen:
                continue
            seen.add(key)

            scope, scope_value = self._nav_scope(href, brands_by_path)
            campaigns.append(Campaign(
                retailer=self.display_name,
                promotion_text=name,
                promotion_type=classify_promotion(name),
                scope=scope,
                scope_value=scope_value,
                promo_code=extract_promo_code(name),
                source_url=canonical_url(str(response.url)),
                landing_url=(
                    f"https://{self.domain}{href}" if href else None
                ),
            ))
        return campaigns

    @classmethod
    def _nav_root(cls, response) -> List[Any]:
        """The navigation category list inside `__NEXT_DATA__`, or empty."""
        body = response.body
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        match = _NEXT_DATA_RE.search(body or "")
        if not match:
            return []
        try:
            node: Any = json.loads(match.group(1))
        except (ValueError, TypeError):
            return []
        for key in ("props", "pageProps", "appProps", "headerProps", "navigation"):
            if not isinstance(node, dict):
                return []
            node = node.get(key)
        categories = (node or {}).get("categories") if isinstance(node, dict) else None
        return categories if isinstance(categories, list) else []

    @classmethod
    def _nav_entries(cls, response) -> List[tuple]:
        """Every (name, path) pair anywhere in the navigation tree."""
        found: List[tuple] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                name = clean_text(node.get("name"))
                url = node.get("url")
                if name and isinstance(url, str):
                    found.append((name, cls._nav_path(url)))
                elif name:
                    found.append((name, None))
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(cls._nav_root(response))
        return found

    @classmethod
    def _nav_brand_index(cls, response) -> Dict[str, str]:
        """Path -> brand name, for every page M&S files under "Brands"."""
        index: Dict[str, str] = {}
        for category in cls._nav_root(response):
            if not isinstance(category, dict) or category.get("name") != "Brands":
                continue

            def collect(node: Any) -> None:
                if isinstance(node, dict):
                    name = clean_text(node.get("name"))
                    url = node.get("url")
                    path = cls._nav_path(url) if isinstance(url, str) else None
                    if (
                        name
                        and path
                        and path.startswith("/l/")
                        and not node.get("items")
                        and not is_confident_offer(name)
                    ):
                        index[path] = name
                    for value in node.values():
                        collect(value)
                elif isinstance(node, list):
                    for value in node:
                        collect(value)

            collect(category)
        return index

    @staticmethod
    def _nav_path(url: Optional[str]) -> Optional[str]:
        """The path of a nav url, without its `#intid=` tracking fragment."""
        if not url:
            return None
        return url.split("#")[0].split("?")[0].rstrip("/") or None

    @staticmethod
    def _nav_scope(path: Optional[str], brands_by_path: Dict[str, str]) -> tuple:
        """(scope, scope_value) for a promotion pointing at `path`."""
        if not path:
            return SCOPE_UNRESOLVED, None
        if path in brands_by_path:
            return SCOPE_BRAND, brands_by_path[path]
        if path.startswith("/l/"):
            segments = [s for s in path[len("/l/"):].split("/") if s]
            if "fs5" in segments:
                segments = segments[: segments.index("fs5")]
            if segments:
                return SCOPE_CATEGORY, segments[-1]
        return SCOPE_UNRESOLVED, None

    def extract_campaigns(self, response) -> List[Campaign]:
        """Offers stated on the page."""
        url = str(response.url)
        if self._is_campaign_hub(url):
            return self.extract_campaign_directory(response)

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
