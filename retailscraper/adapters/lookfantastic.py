"""Lookfantastic adapter."""

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import parse_qs, urlparse

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
from ..normalize import (
    canonical_url,
    clean_text,
    extract_promo_code,
    parse_price,
    percent_off,
)
from ..promotions import classify_promotion, looks_like_offer
from .base import ListingPage, RetailerAdapter, register

_PRODUCT_URL_RE = re.compile(r"/p/(?:[^/]+/)*?([0-9]{5,})/?$")

_BRAND_PAGE_RE = re.compile(r"/c/brands/([^/]+)/?$")

_SAVE_AMOUNT_RE = re.compile(r"Save\s*[£$€]?\s*([\d.,]+)", re.IGNORECASE)
_SAVE_PERCENT_RE = re.compile(r"\(\s*([\d.]+)\s*%\s*Off\s*\)", re.IGNORECASE)


@register
class LookfantasticAdapter(RetailerAdapter):
    """Extraction rules for lookfantastic.com."""

    name = "lookfantastic"
    domain = "www.lookfantastic.com"
    display_name = "Lookfantastic"

    supports_categories = True

    listing_session_id = "browser"

    CATEGORY_SLUGS = {
        "skincare": "skincare",
        "makeup": "makeup",
        "make-up": "makeup",
        "make up": "makeup",
        "fragrance": "fragrance",
        "haircare": "haircare",
        "hair": "haircare",
        "men": "mens",
        "mens": "mens",
        "gift": "gift",
        "gifts": "gift",
    }

    def configure_session(self, manager) -> None:
        """Plain HTTP by default, plus a lazily-started browser for listings."""
        from scrapling.fetchers import AsyncDynamicSession, FetcherSession

        self.add_proxied_sessions(manager, lambda proxy: FetcherSession(proxy=proxy))
        manager.add(
            self.listing_session_id,
            AsyncDynamicSession(headless=True, network_idle=True, timeout=90_000,
                                proxy=first_proxy()),
            lazy=True,
        )

    def product_listing_urls(
        self, brand: str, categories: Sequence[str], max_pages: int
    ) -> List[ListingPage]:
        """Category pages for one brand."""
        if not categories:
            return []

        slug = self._brand_slug(brand)
        pages = []
        for category in categories:
            category_slug = self.CATEGORY_SLUGS.get(
                category.strip().lower(), self._brand_slug(category)
            )
            pages.append(
                ListingPage(
                    url=f"https://{self.domain}/c/brands/{slug}/{category_slug}/",
                    brand=brand,
                    category=category,
                )
            )
        return pages

    def extract_product_links(self, response, brand: Optional[str] = None) -> List[str]:
        """Product URLs from a rendered category page."""
        slug = self._brand_slug(brand) if brand else None
        found = []
        for node in response.css("a"):
            href = node.attrib.get("href") or ""
            if href.startswith("/"):
                href = f"https://{self.domain}{href}"
            if not (href.startswith("http") and self.is_product_url(href)):
                continue
            if slug and slug not in href.lower():
                continue
            clean = canonical_url(href)
            if clean not in found:
                found.append(clean)
        return found

    sitemap_urls = ["https://www.lookfantastic.com/sitemapindex-product.xml.gz"]

    def campaign_seed_urls(self, brands: Sequence[str]) -> List[str]:
        """Brand landing pages, which carry the brand-level promotional copy."""
        return [
            f"https://www.lookfantastic.com/c/brands/{self._brand_slug(brand)}/"
            for brand in brands
        ]

    @staticmethod
    def _brand_slug(brand: str) -> str:
        """Turn a brand name into the slug Lookfantastic uses in its URLs."""
        return re.sub(r"[^a-z0-9]+", "-", brand.strip().lower()).strip("-")

    def product_id_from_url(self, url: str) -> Optional[str]:
        """Public form of the id helper, for joining sitemap URLs to scraped products."""
        return self._product_id_from_url(url)

    def is_product_url(self, url: str) -> bool:
        return bool(_PRODUCT_URL_RE.search(url.split("?")[0]))

    def select_candidates(self, urls: Iterable[str], brands: Sequence[str]) -> List[str]:
        """Pick sitemap URLs whose slug mentions one of the target brands."""
        slugs = [self._brand_slug(b) for b in brands]
        selected = []
        for url in urls:
            if not self.is_product_url(url):
                continue
            lowered = url.lower()
            if any(slug in lowered for slug in slugs):
                selected.append(url)
        return selected

    def extract_product(self, response, target_brands: Sequence[str] = ()) -> Optional[Product]:
        return self.parse_product(response, str(response.url), target_brands)

    def parse_product(self, sel, url: str, target_brands: Sequence[str] = ()) -> Optional[Product]:
        """Build a Product from a parsed product page."""
        blocks = self._json_ld_blocks(sel)
        product_ld = self._find_type(blocks, "Product") or self._find_type(blocks, "ProductGroup")
        if product_ld is None:
            return None

        product_id = self._product_id_from_url(url)
        if not product_id:
            return None

        brand, verified_by = self._verify_brand(blocks)
        if not brand:
            brand, verified_by = self._verify_brand_weakly(
                clean_text(product_ld.get("name")), url, target_brands
            )
        variants = product_ld.get("hasVariant") or []
        variant_count = len(variants) if variants else 1

        original_price, current_price, currency = self._extract_prices(sel)
        saved_amount, stated_percent = self._extract_saving(sel)

        offer = product_ld.get("offers")
        if isinstance(offer, list):
            offer = offer[0] if offer else None
        if current_price is None and isinstance(offer, dict):
            current_price = self._as_float(offer.get("price"))
            currency = currency or offer.get("priceCurrency")

        sku = product_ld.get("sku") if variant_count == 1 else None
        if sku is None and variant_count == 1 and isinstance(offer, dict):
            sku = offer.get("sku")

        availability = None
        if isinstance(offer, dict) and offer.get("availability"):
            availability = str(offer["availability"]).rsplit("/", 1)[-1]
        elif variants:
            states = set()
            for variant in variants:
                variant_offer = variant.get("offers") if isinstance(variant, dict) else None
                if isinstance(variant_offer, list):
                    variant_offer = variant_offer[0] if variant_offer else None
                if isinstance(variant_offer, dict) and variant_offer.get("availability"):
                    states.add(str(variant_offer["availability"]).rsplit("/", 1)[-1])
            if states:
                availability = "InStock" if "InStock" in states else sorted(states)[0]

        promo_text = self._product_promotion_text(sel)

        return Product(
            retailer=self.display_name,
            brand=brand or "",
            brand_verified_by=verified_by,
            product_id=product_id,
            product_title=self._product_title(product_ld, sel),
            product_url=canonical_url(url),
            source_url=url,
            currency=currency,
            current_price=current_price,
            original_price=original_price,
            discount_amount=saved_amount,
            discount_percent=(
                stated_percent
                if stated_percent is not None
                else percent_off(original_price, current_price)
            ),
            availability=availability,
            variant_count=variant_count,
            sku=str(sku) if sku else None,
            promotional_copy=promo_text,
            scraped_at=datetime.now(timezone.utc).isoformat(),
        )

    CAMPAIGN_HUB_PATHS = [
        "/c/health-beauty/offers/view-all/",
        "/c/health-beauty/offers/",
        "/c/offers/sale/",
    ]

    OFFER_SITEMAP = "https://www.lookfantastic.com/sitemapindex-list.xml.gz"

    MAX_OFFER_PAGES = 100

    def __init__(self) -> None:
        self.listed_offer_pages: List[str] = []
        self._capped_offers: set = set()

    def prepare(self, brands: Sequence[str]) -> List[str]:
        """Read every offer and sale page from the list sitemap."""
        import gzip
        from scrapling.fetchers import Fetcher

        def locations(url: str) -> List[str]:
            body = Fetcher.get(url, stealthy_headers=True, timeout=60,
                               proxy=first_proxy()).body
            if body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            return re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body.decode("utf-8", "replace"))

        try:
            pages = [url for sitemap in locations(self.OFFER_SITEMAP)
                     for url in locations(sitemap)]
        except Exception as exc:
            return [f"could not read the offer pages from {self.OFFER_SITEMAP} ({exc}), "
                    f"so only the offer pages linked from the hubs were read"]

        self.listed_offer_pages = list(dict.fromkeys(
            url for url in pages if re.search(r"/[^/]*(?:offer|sale)[^/]*/", url)))
        return []

    def campaign_discovery_urls(self) -> List[str]:
        """The hubs, plus every offer page the list sitemap names."""
        hubs = [f"https://{self.domain}{path}" for path in self.CAMPAIGN_HUB_PATHS]
        return list(dict.fromkeys(hubs + self.listed_offer_pages))

    def scope_for_href(self, href: str):
        """Read an offer's reach from where its link points."""
        path = href.split(f"{self.domain}", 1)[-1]

        brand = re.search(r"/c/brands/([^/]+)/", path)
        if brand:
            return (SCOPE_BRAND, brand.group(1).replace("-", " ").title())

        category = re.search(r"/c/health-beauty/([^/]+)/", path)
        if category and category.group(1) not in {"offers", "offer", "sale"}:
            return (SCOPE_CATEGORY, category.group(1).replace("-", " "))

        if "/c/offers/" in path or "/offers/" in path or "/sale/" in path:
            return (SCOPE_SITEWIDE, None)

        return (None, None)

    def extract_campaign_directory(self, response) -> List[Campaign]:
        """Every offer on an offers page: its links, and the page's own heading."""
        campaigns = [c for c in self._scan_offer_links(response)
                     if not self.is_product_url(c.landing_url or "")]

        heading = clean_text(response.css("h1")[0].get_all_text()) if response.css("h1") else None
        if heading and (looks_like_offer(heading) or re.search(r"\bsale\b", heading, re.I)):
            page = str(response.url).split("?")[0]
            scope, scope_value = self.scope_for_href(page)
            campaigns.append(Campaign(
                retailer=self.display_name,
                promotion_text=heading,
                promotion_type=classify_promotion(heading),
                scope=scope or SCOPE_UNRESOLVED,
                scope_value=scope_value,
                promo_code=extract_promo_code(heading),
                source_url=str(response.url),
                landing_url=page,
            ))
        return campaigns

    def offer_page_products(self, response) -> List[str]:
        """Ids of the products in this offer, from the page's product grid."""
        ids = []
        for node in response.css("#product-list a"):
            href = (node.attrib.get("href") or "").split("?")[0]
            if href.startswith("/"):
                href = f"https://{self.domain}{href}"
            if self.is_product_url(href):
                ids.append(self._product_id_from_url(href))
        return list(dict.fromkeys(i for i in ids if i))

    def offer_page_links(self, response) -> List[str]:
        """Offers pages to read after this one."""
        links = []
        for campaign in self._scan_offer_links(response):
            url = (campaign.landing_url or "").split("?")[0]
            if urlparse(url).netloc == self.domain and not self.is_product_url(url):
                links.append(url)

        address = urlparse(str(response.url))
        page = int(parse_qs(address.query).get("pageNumber", ["1"])[0])
        next_page = f"https://{self.domain}{address.path}?pageNumber={page + 1}"
        linked = re.search(rf"pageNumber={page + 1}(?!\d)", response.html_content)
        if linked and self.offer_page_products(response):
            if page + 1 <= self.MAX_OFFER_PAGES:
                links.append(next_page)
            else:
                self._capped_offers.add(address.path)

        return list(dict.fromkeys(links))

    def crawl_warnings(self) -> List[str]:
        return [f"offer page {path} has more than {self.MAX_OFFER_PAGES} pages; "
                f"products after that were not linked to its offer"
                for path in sorted(self._capped_offers)]

    def extract_campaigns(self, response) -> List[Campaign]:
        return self.parse_campaigns(response, str(response.url))

    def parse_campaigns(self, sel, url: str) -> List[Campaign]:
        """Collect promotions from a page, each with an honest scope."""
        campaigns: List[Campaign] = []
        seen: set = set()

        def add(text: Optional[str], scope: str, landing: Optional[str] = None) -> None:
            cleaned = clean_text(text)
            if not cleaned or cleaned in seen:
                return
            if not looks_like_offer(cleaned):
                return
            seen.add(cleaned)
            campaigns.append(
                Campaign(
                    retailer=self.display_name,
                    promotion_text=cleaned,
                    promotion_type=classify_promotion(cleaned),
                    scope=scope,
                    scope_value=None,
                    promo_code=extract_promo_code(cleaned),
                    source_url=url,
                    landing_url=landing,
                )
            )

        for node in sel.css("a.strip-banner"):
            href = node.attrib.get("href")
            landing = f"https://{self.domain}{href}" if href and href.startswith("/") else href
            add(node.get_all_text(), SCOPE_SITEWIDE, landing)

        if self.is_product_url(url):
            add(self._product_promotion_text(sel), SCOPE_PRODUCT)

        return campaigns

    @staticmethod
    def _json_ld_blocks(sel) -> List[Dict[str, Any]]:
        """Parse every JSON-LD script on the page into dicts."""
        blocks: List[Dict[str, Any]] = []
        for node in sel.css('script[type="application/ld+json"]'):
            raw = node.text
            if not raw:
                continue
            try:
                parsed = json.loads(raw.strip())
            except (ValueError, TypeError):
                continue
            blocks.extend(parsed if isinstance(parsed, list) else [parsed])
        return blocks

    @staticmethod
    def _find_type(blocks: List[Dict[str, Any]], wanted: str) -> Optional[Dict[str, Any]]:
        """Return the first JSON-LD block whose @type matches."""
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("@type")
            types = block_type if isinstance(block_type, list) else [block_type]
            if wanted in types:
                return block
        return None

    @classmethod
    def _verify_brand(cls, blocks: List[Dict[str, Any]]) -> tuple:
        """Establish the brand from the breadcrumb's link into /c/brands/."""
        crumbs = cls._find_type(blocks, "BreadcrumbList")
        if not crumbs:
            return None, "unverified"

        for entry in crumbs.get("itemListElement") or []:
            if not isinstance(entry, dict):
                continue
            item = entry.get("item")
            item_id = item.get("@id") if isinstance(item, dict) else item
            if item_id and _BRAND_PAGE_RE.search(str(item_id)):
                name = clean_text(entry.get("name"))
                if name:
                    return name, "breadcrumb_link"
        return None, "unverified"

    @classmethod
    def _verify_brand_weakly(
        cls, title: Optional[str], url: str, target_brands: Sequence[str]
    ) -> tuple:
        """Corroborate a REQUESTED brand from the title and the URL."""
        if not title or not target_brands:
            return None, "unverified"

        path = url.split("?")[0].lower()
        title_folded = title.casefold()

        for brand in target_brands:
            slug = cls._brand_slug(brand)
            if not slug:
                continue
            if not title_folded.startswith(brand.strip().casefold()):
                continue
            if f"/{slug}-" in path or f"/{slug}/" in path:
                return brand, "title_and_url"
        return None, "unverified"

    @staticmethod
    def _product_id_from_url(url: str) -> Optional[str]:
        match = _PRODUCT_URL_RE.search(url.split("?")[0])
        return match.group(1) if match else None

    @staticmethod
    def _as_float(value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_prices(sel) -> tuple:
        """Read RRP and current price from `#product-price`."""
        original = current = None
        currency = None
        pending: Optional[str] = None
        unlabelled: List[float] = []

        for span in sel.css("#product-price span"):
            text = clean_text(span.get_all_text())
            if not text:
                continue

            lowered = text.lower()
            if lowered.startswith("recommended retail price"):
                pending = "original"
                continue
            if lowered.startswith("current price"):
                pending = "current"
                continue

            amount, symbol_currency = parse_price(text)
            if amount is None:
                continue
            currency = currency or symbol_currency

            if pending == "original":
                original = amount
            elif pending == "current":
                current = amount
            else:
                unlabelled.append(amount)
            pending = None

        if current is None and unlabelled:
            current = unlabelled[0]

        if current is None and original is not None:
            current, original = original, None

        return original, current, currency

    @staticmethod
    def _extract_saving(sel) -> tuple:
        """Read the retailer's own advertised saving, e.g. "Save £9.75 (25% Off)"."""
        nodes = sel.css("#product-price")
        if not nodes:
            return None, None

        text = clean_text(nodes[0].get_all_text()) or ""
        amount_match = _SAVE_AMOUNT_RE.search(text)
        percent_match = _SAVE_PERCENT_RE.search(text)

        amount = None
        if amount_match:
            amount, _ = parse_price(amount_match.group(1))

        percent = float(percent_match.group(1)) if percent_match else None
        return amount, percent

    @staticmethod
    def _product_title(product_ld: Dict[str, Any], sel) -> str:
        """The product's name, repaired when Lookfantastic truncates it."""
        name = clean_text(product_ld.get("name")) or ""
        heading = clean_text(" ".join(
            str(part) for part in sel.css("h1::text")
        ))
        if (
            heading
            and len(heading) > len(name)
            and heading.casefold().startswith(name.casefold())
        ):
            return heading
        return name

    @staticmethod
    def _product_promotion_text(sel) -> Optional[str]:
        """The promotional copy that applies to this product specifically."""
        for node in sel.css("[data-e2e='pdp-pap-banner']"):
            text = clean_text(node.attrib.get("data-track-push"))
            if text and looks_like_offer(text):
                return text
        return None
