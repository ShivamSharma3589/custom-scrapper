"""ASOS adapter."""

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
from config import first_proxy

_PRODUCT_URL_RE = re.compile(
    r"^https?://(?:www\.)?asos\.com/.+/prd/(\d+)", re.I
)

_PAGE_SIZE = 72

_TOTAL_RE = re.compile(r"(\d[\d,]*)\s+styles found", re.IGNORECASE)

_CURRENCY_RE = re.compile(r'"currency"\s*:\s*\{[^}]*?"currency"\s*:\s*"([A-Z]{3})"')

_UK_SECTION_RE = re.compile(r"^/(?:women|men|outlet)(?:/|$)", re.I)

_PROMO_PATH_RE = re.compile(r"/ctas/(?:generic|coded|core)-promos?/", re.I)

_SALE_PATH_RE = re.compile(r"/sale/", re.I)


@register
class AsosAdapter(RetailerAdapter):
    """Extraction rules for asos.com."""

    name = "asos"
    domain = "www.asos.com"
    display_name = "ASOS"

    sitemap_urls: List[str] = []

    supports_categories = False

    EXPECTED_CURRENCY = "GBP"

    listing_session_id = "listings"

    MAX_OFFER_PAGES = 8

    def __init__(self) -> None:
        self.wrong_currency_pages = 0
        self.currencies_seen: set = set()
        self._capped: set = set()
        self._wanted_slugs: List[str] = []
        self._offer_depth: Dict[str, int] = {}
        self._offer_seen: set = set()
        self._offer_capped = False

    def prepare(self, brands: Sequence[str]) -> List[str]:
        """Remember the brands, so the offer walk can tell ours from the rest."""
        self._wanted_slugs = [
            re.sub(r"[^a-z0-9]+", " ", b.lower()).strip() for b in brands
        ]
        return []

    def configure_session(self, manager) -> None:
        """A browser for everything, resources included."""
        from scrapling.fetchers import AsyncStealthySession

        def browser(proxy) -> AsyncStealthySession:
            return AsyncStealthySession(
                headless=True,
                google_search=True,
                network_idle=True,
                timeout=120_000,
                max_pages=2,
                proxy=proxy,
                extra_flags=["--disable-http2"],
            )

        self.add_proxied_sessions(manager, browser)
        manager.add(self.listing_session_id, browser(first_proxy()), lazy=True)

    MAX_LIST_PAGES = 40

    def product_listing_urls(
        self, brand: str, categories: Sequence[str], max_pages: int
    ) -> List[ListingPage]:
        """The first page of search results. The rest follow it."""
        query = re.sub(r"\s+", "+", brand.strip())
        return [ListingPage(url=f"https://{self.domain}/search/?q={query}&page=1",
                            brand=brand, category=None)]

    def more_listing_pages(self, response, meta: dict, added: int) -> List[str]:
        """The next page, while ASOS says there are more results than we have seen."""
        url = str(meta.get("url") or response.url)
        if "/search/" not in url:
            return []

        body = response.body
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        stated = re.search(r'"itemCount"\s*:\s*(\d+)', body or "")
        if not stated:
            return []

        total = int(stated.group(1))
        page = int((re.search(r"[?&]page=(\d+)", url) or [None, "1"])[1])
        if page * _PAGE_SIZE >= total:
            return []
        if page >= self.MAX_LIST_PAGES:
            self._capped.add(meta.get("brand") or url)
            return []
        return [re.sub(r"([?&]page=)\d+", rf"\g<1>{page + 1}", url)]

    def crawl_warnings(self) -> List[str]:
        warnings = [f"{brand} has more than {self.MAX_LIST_PAGES * _PAGE_SIZE} "
                    f"search results; the rest were not read"
                    for brand in sorted(self._capped)]
        if self._offer_capped:
            warnings.append(
                f"stopped after {self.MAX_OFFER_PAGES} ASOS offer pages; "
                f"any offer beyond that was not read"
            )
        return warnings

    def is_product_url(self, url: str) -> bool:
        return bool(_PRODUCT_URL_RE.match(url.split("?")[0]))

    def product_id_from_url(self, url: str) -> Optional[str]:
        match = _PRODUCT_URL_RE.match(url.split("?")[0])
        return match.group(1) if match else None

    def select_candidates(self, urls: Iterable[str], brands: Sequence[str]) -> List[str]:
        """Unused: discovery reads the search payload, not a URL list."""
        return []

    def extract_products_from_listing(
        self, response, brand: Optional[str] = None
    ) -> List[Product]:
        """Turn an ASOS search page into Product records."""
        raw_products = self._embedded_products(response)
        if not raw_products:
            return []

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
        """The outlet, whose navigation links every promotion ASOS is running."""
        return ["https://www.asos.com/outlet/"]

    def _is_promo_path(self, path: str) -> bool:
        """True for a UK page that states a promotion."""
        return bool(_UK_SECTION_RE.match(path) and _PROMO_PATH_RE.search(path))

    def _is_sale_path(self, path: str) -> bool:
        """True for a UK sale listing, which is only worth reading for our brands."""
        return bool(_UK_SECTION_RE.match(path) and _SALE_PATH_RE.search(path))

    def _names_wanted_brand(self, *texts: str) -> bool:
        """True when a URL or link text names one of the requested brands."""
        words = re.sub(r"[^a-z0-9]+", " ", " ".join(texts).lower())
        return any(re.search(rf"(?<![a-z0-9]){re.escape(slug)}(?![a-z0-9])", words)
                   for slug in self._wanted_slugs)

    def offer_page_links(self, response) -> List[str]:
        """The promotion pages ASOS links from its navigation"""
        page = str(response.url)
        root = f"https://{self.domain}"
        links: List[str] = []

        for node in response.css("a[href]"):
            if len(self._offer_seen) >= self.MAX_OFFER_PAGES:
                self._offer_capped = True
                break

            href = (node.attrib.get("href") or "").split("#")[0]
            if href.startswith("/"):
                href = f"{root}{href}"
            if not href.startswith(f"{root}/"):
                continue
            if not self._is_promo_path(href[len(root):].split("?")[0]):
                continue
            if "cid=" not in href:
                continue

            key = href
            if key in self._offer_seen or key == page:
                continue
            self._offer_seen.add(key)
            links.append(href)

        return links

    def offer_page_products(self, response) -> List[str]:
        """Ids of the products an offer page lists."""
        return [str(tile["id"]) for tile in self._embedded_products(response)
                if tile.get("id") is not None]

    def _wrong_storefront(self, response) -> bool:
        """True when this page is another country's site, priced in its money."""
        currency = self.page_currency(response)
        return bool(currency and currency != self.EXPECTED_CURRENCY)

    def extract_campaign_directory(self, response) -> List[Campaign]:
        """Every offer linked from a hub page, via the shared scan."""
        if self._wrong_storefront(response):
            return []
        return self._scan_offer_links(response)

    def _title_offer(self, response) -> Optional[str]:
        """The offer a promo landing page names in its title."""
        nodes = response.css("title")
        if not nodes:
            return None
        text = clean_text(nodes[0].get_all_text()) or ""
        text = re.sub(r"\s*\|\s*ASOS\s*$", "", text)
        text = re.sub(r"^\s*Page\s+\d+\s*[-–—]\s*", "", text, flags=re.I)
        return text.strip() or None

    def extract_campaigns(self, response) -> List[Campaign]:
        """Offers stated on a search or landing page."""
        url = str(response.url)
        campaigns: List[Campaign] = []
        seen: set = set()

        if self._wrong_storefront(response):
            return []

        path = url[len(f"https://{self.domain}"):] if url.startswith(f"https://{self.domain}") else ""
        if self._is_promo_path(path.split("?")[0]):
            title = self._title_offer(response)
            if title and is_confident_offer(title) and not is_browse_facet(title, url):
                seen.add(title)
                campaigns.append(Campaign(
                    retailer=self.display_name,
                    promotion_text=title,
                    promotion_type=classify_promotion(title),
                    scope=SCOPE_SITEWIDE,
                    scope_value=None,
                    promo_code=extract_promo_code(title),
                    source_url=canonical_url(url),
                    landing_url=canonical_url(url),
                ))

        for node in response.css("[class*='banner'], [class*='promo'], [class*='Banner'], h1, h2"):
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
                    scope=SCOPE_SITEWIDE,
                    scope_value=None,
                    promo_code=extract_promo_code(text),
                    source_url=canonical_url(url.split("#")[0]),
                    landing_url=None,
                )
            )
        return campaigns
