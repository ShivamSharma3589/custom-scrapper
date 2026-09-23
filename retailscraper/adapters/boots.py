"""Boots adapter."""

import re
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence
from urllib.parse import parse_qs, urlparse

from ..models import SCOPE_CATEGORY, SCOPE_PRODUCT, SCOPE_SITEWIDE, Campaign, Product
from ..normalize import (
    canonical_url,
    clean_text,
    extract_promo_code,
    parse_price,
    percent_off,
)
from ..promotions import classify_promotion, looks_like_offer
from .base import ListingPage, RetailerAdapter, register

_PRODUCT_URL_RE = re.compile(r"^https?://(?:www\.)?boots\.com/[a-z0-9][^/?#]*?-(\d{6,})/?$", re.I)

_WAS_RE = re.compile(r"Was\s*[£$€]?\s*([\d.,]+)", re.IGNORECASE)
_SAVE_RE = re.compile(r"Save\s*[£$€]?\s*([\d.,]+)", re.IGNORECASE)

_BLOCK_MARKER = "Pardon Our Interruption"

_WALL_MARKER = "_Incapsula_Resource"
_WALL_MAX_CHARS = 20_000


@register
class BootsAdapter(RetailerAdapter):
    """Extraction rules for boots.com."""

    name = "boots"
    domain = "www.boots.com"
    display_name = "Boots"

    sitemap_urls: List[str] = []

    listing_page_size = 25

    MAX_LIST_PAGES = 50

    supports_categories = True

    CATEGORY_SLUGS = {
        "skincare": "skincare",
        "makeup": "make-up",
        "make-up": "make-up",
        "make up": "make-up",
        "fragrance": "fragrance",
        "haircare": "haircare",
        "hair": "haircare",
        "men": "men",
        "mens": "men",
        "gift": "gifts",
        "gifts": "gifts",
        "bath and body": "bath-and-body",
        "bath & body": "bath-and-body",
        "home": "home-fragrance",
    }

    CAMPAIGN_HUB_PATHS = ["/offers"]

    def __init__(self) -> None:
        self._featured_only: set = set()

    def warmup_url(self) -> Optional[str]:
        """The homepage, fetched only to get past bot protection."""
        return f"https://{self.domain}/"

    def campaign_discovery_urls(self) -> List[str]:
        """The offers hub, for a campaigns-only run."""
        return [f"https://{self.domain}{path}" for path in self.CAMPAIGN_HUB_PATHS]

    def scope_for_href(self, href: str):
        """Read an offer's reach from where its link points."""
        path = href.split(f"{self.domain}", 1)[-1].lstrip("/")
        first = path.split("/", 1)[0].split("?", 1)[0].lower()

        if not first or first in {"offers", "customer-offer"} or first.startswith("campaign-list"):
            return (SCOPE_SITEWIDE, None)

        if first in set(self.CATEGORY_SLUGS.values()):
            return (SCOPE_CATEGORY, first.replace("-", " "))

        return (None, None)

    def extract_campaign_directory(self, response) -> List[Campaign]:
        """Every offer advertised on a Boots offers page."""
        return [c for c in self._scan_offer_links(response)
                if not self.is_product_url(c.landing_url or "")]

    def offer_page_links(self, response) -> List[str]:
        """Every offers or sale page linked from this page."""
        return [url for url in self._links(response)
                if "?" not in url
                and not self.is_product_url(url)
                and re.search(r"offer|/sale(/|$)", urlparse(url).path)]

    def promotion_key(self, campaign) -> Optional[str]:
        """Boots' own name for an offer, read from its "shop now" link."""
        values = parse_qs(urlparse(campaign.landing_url or "").query).get(
            "criteria.promotionalText")
        return values[0] if values else None

    def configure_session(self, manager) -> None:
        """Use a stealth browser session, kept alive across the whole crawl."""
        from scrapling.fetchers import AsyncStealthySession

        self.add_proxied_sessions(manager, lambda proxy: AsyncStealthySession(
            headless=True,
            google_search=True,
            network_idle=True,
            timeout=120_000,
            max_pages=2,
            proxy=proxy,
        ))

    @staticmethod
    def _brand_slug(brand: str) -> str:
        """Brand name -> the slug Boots uses in its URLs ("Jo Malone" -> jo-malone)."""
        return re.sub(r"[^a-z0-9]+", "-", brand.strip().lower()).strip("-")

    def campaign_seed_urls(self, brands: Sequence[str]) -> List[str]:
        """None. Brand pages are crawled as listings, which read their offers already."""
        return []

    def product_listing_urls(
        self, brand: str, categories: Sequence[str], max_pages: int
    ) -> List[ListingPage]:
        """Where the crawl starts for one brand."""
        slug = self._brand_slug(brand)
        if not categories:
            return [ListingPage(url=f"https://{self.domain}/{slug}", brand=brand)]

        pages = []
        for category in categories:
            leaf = self.CATEGORY_SLUGS.get(category.strip().lower(), self._brand_slug(category))
            if not leaf.startswith(slug):
                leaf = f"{slug}-{leaf}"
            pages.append(ListingPage(url=f"https://{self.domain}/{slug}/{leaf}",
                                     brand=brand, category=category))
        return pages

    def more_listing_pages(self, response, meta: dict, added: int) -> List[str]:
        """What to crawl after a Boots listing page."""
        requested = urlparse(meta.get("url") or str(response.url))
        path = requested.path.strip("/")

        if "/" not in path:
            if meta.get("category"):
                return []
            root = urlparse(str(response.url)).path.strip("/")
            lists = [url for url in self._links(response)
                     if url.startswith(f"https://{self.domain}/{root}/")
                     and "?" not in url and not self.is_product_url(url)]
            if not lists:
                self._featured_only.add(meta.get("brand"))
            return lists

        page = int(parse_qs(requested.query).get("paging.index", ["0"])[0])
        if added and page + 1 < self.MAX_LIST_PAGES:
            return [f"https://{self.domain}/{path}?paging.index={page + 1}"
                    f"&paging.size={self.listing_page_size}"]
        return []

    def crawl_warnings(self) -> List[str]:
        return [f"{brand}: its Boots brand page links no product lists, so only "
                f"the featured products on that page were collected"
                for brand in sorted(self._featured_only)]

    def extract_product_links(self, response, brand: Optional[str] = None) -> List[str]:
        """Product URLs linked from a listing page, de-duplicated in page order."""
        return list(dict.fromkeys(
            canonical_url(url) for url in self._links(response) if self.is_product_url(url)
        ))

    def _links(self, response) -> List[str]:
        """Every boots.com link on a page, made absolute, once each, in page order."""
        found = []
        for node in response.css("a"):
            href = (node.attrib.get("href") or "").split("#")[0]
            if href.startswith("/"):
                href = f"https://{self.domain}{href}"
            if urlparse(href).netloc.endswith("boots.com"):
                found.append(href)
        return list(dict.fromkeys(found))

    def product_id_from_url(self, url: str) -> Optional[str]:
        """Public form of the id helper, for joining sitemap URLs to scraped products."""
        return self._product_id_from_url(url)

    def is_product_url(self, url: str) -> bool:
        return bool(_PRODUCT_URL_RE.match(url.split("?")[0]))

    def select_candidates(self, urls: Iterable[str], brands: Sequence[str]) -> List[str]:
        """Filter URLs to those whose slug mentions a target brand."""
        slugs = [self._brand_slug(b) for b in brands]
        return [
            u for u in urls
            if self.is_product_url(u) and any(s in u.lower() for s in slugs)
        ]

    @staticmethod
    def looks_blocked(response) -> bool:
        """True when this is the bot-protection challenge rather than content."""
        try:
            body = response.body.decode("utf-8", "replace")
        except (AttributeError, UnicodeDecodeError):
            return False
        if _BLOCK_MARKER in body:
            return True
        return (_WALL_MARKER in body and len(body) < _WALL_MAX_CHARS
                and "<title" not in body.lower())

    def extract_product(
        self, response, target_brands: Sequence[str] = ()
    ) -> Optional[Product]:
        if self.looks_blocked(response):
            return None
        return self.parse_product(response, str(response.url))

    def parse_product(self, sel, url: str) -> Optional[Product]:
        """Build a Product from a Boots product page."""
        product_id = self._product_id_from_url(url)
        if not product_id:
            return None

        title = clean_text(self._itemprop(sel, "name"))
        if not title:
            return None

        brand = clean_text(self._itemprop(sel, "Brand"))
        verified_by = "microdata_brand" if brand else "unverified"

        current_price, _ = parse_price(self._itemprop(sel, "price"))
        currency = clean_text(self._itemprop(sel, "priceCurrency"))

        availability = None
        raw_availability = self._itemprop(sel, "availability")
        if raw_availability:
            availability = str(raw_availability).rsplit("/", 1)[-1]

        original_price, saved_amount = self._extract_was_and_saving(sel)

        promo_text = self._product_promotion_text(sel)

        return Product(
            retailer=self.display_name,
            brand=brand or "",
            brand_verified_by=verified_by,
            product_id=product_id,
            product_title=title,
            product_url=canonical_url(url),
            source_url=url,
            currency=currency,
            current_price=current_price,
            original_price=original_price,
            discount_amount=saved_amount,
            discount_percent=percent_off(original_price, current_price),
            availability=availability,
            variant_count=1,
            sku=product_id,
            promotional_copy=promo_text,
            scraped_at=datetime.now(timezone.utc).isoformat(),
        )

    def extract_campaigns(self, response) -> List[Campaign]:
        if self.looks_blocked(response):
            return []
        return self.parse_campaigns(response, str(response.url))

    def parse_campaigns(self, sel, url: str) -> List[Campaign]:
        """Collect the offers Boots lists against a page."""
        campaigns: List[Campaign] = []
        seen: set = set()

        for node in sel.css("li.pdp-promotion-redesign"):
            text = clean_text(node.get_all_text())
            if not text or text in seen or not looks_like_offer(text):
                continue
            seen.add(text)
            campaigns.append(
                Campaign(
                    retailer=self.display_name,
                    promotion_text=text,
                    promotion_type=classify_promotion(text),
                    scope=SCOPE_PRODUCT,
                    promo_code=extract_promo_code(text),
                    source_url=url,
                )
            )
        return campaigns

    @staticmethod
    def _itemprop(sel, prop: str) -> Optional[str]:
        """Read a schema.org microdata value."""
        for node in sel.css(f"[itemprop='{prop}']"):
            for attribute in ("content", "href"):
                value = node.attrib.get(attribute)
                if value:
                    return value
            text = node.get_all_text()
            if text and text.strip():
                return text
        return None

    @staticmethod
    def _product_id_from_url(url: str) -> Optional[str]:
        match = _PRODUCT_URL_RE.match(url.split("?")[0])
        return match.group(1) if match else None

    @staticmethod
    def _extract_was_and_saving(sel) -> tuple:
        """Read the "Was £43.00" and "Save £10.75" figures from the price panel."""
        original = saved = None

        for node in sel.css(".was_price"):
            match = _WAS_RE.search(clean_text(node.get_all_text()) or "")
            if match:
                original, _ = parse_price(match.group(1))
                break

        for node in sel.css(".saving"):
            match = _SAVE_RE.search(clean_text(node.get_all_text()) or "")
            if match:
                saved, _ = parse_price(match.group(1))
                break

        return original, saved

    @staticmethod
    def _product_promotion_text(sel) -> Optional[str]:
        """The customer-facing promotional copy shown against this product."""
        texts = []
        for node in sel.css("li.pdp-promotion-redesign"):
            text = clean_text(node.get_all_text())
            if text and looks_like_offer(text) and text not in texts:
                texts.append(text)
        return " | ".join(texts) if texts else None
