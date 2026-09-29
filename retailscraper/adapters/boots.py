"""Boots adapter."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence
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
from config import first_proxy

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

    crawl_delay = 15.0
    max_concurrent_requests = 1

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
        self._empty_lists: set = set()
        self._stocked: Optional[List[str]] = None

    def warmup_url(self) -> Optional[str]:
        """The homepage, fetched only to get past bot protection."""
        return f"https://{self.domain}/"

    #: The A-Z, one page per letter: /brands is A, then /brands-b ... /brands-z,
    #: and a 27th for names starting with a digit.
    BRAND_INDEX_PAGES = ["https://www.boots.com/brands"] + [
        f"https://www.boots.com/brands-{letter}"
        for letter in "bcdefghijklmnopqrstuvwxyz"
    ] + ["https://www.boots.com/brands-0-9"]

    #: Only this container holds brands; the rest of the page is site navigation.
    BRAND_LIST_SELECTOR = "#brand_list_viewer a[href]"

    @property
    def _brand_cache(self) -> Path:
        return Path(__file__).resolve().parent.parent.parent / ".boots_brands.json"

    def stocked_brands(self) -> List[str]:
        """Every brand in Boots' A-Z, read once and cached on disk.

        Boots serves its bot wall to plain HTTP, so this needs a browser across
        26 letter pages -- too slow to repeat, hence the cache.
        """
        if self._stocked is not None:
            return self._stocked

        try:
            cached = json.loads(self._brand_cache.read_text(encoding="utf-8"))
            if isinstance(cached, list) and cached:
                self._stocked = cached
                return self._stocked
        except Exception:
            pass

        self._stocked = self._read_brand_index()
        if self._stocked:
            try:
                self._brand_cache.write_text(
                    json.dumps(self._stocked, indent=2, ensure_ascii=False),
                    encoding="utf-8")
            except Exception:  # pragma: no cover - a cache miss is not fatal
                pass
        return self._stocked

    def _read_brand_index(self) -> List[str]:
        """Walk the A-Z pages with a browser and collect the brand names."""
        from scrapling.fetchers import StealthySession

        names: set = set()

        def fetch(session, url, tries: int = 3):
            """Boots answers the first request with its wall and the next with
            the page, so a single attempt is not enough here."""
            for _ in range(tries):
                try:
                    response = session.fetch(url)
                except Exception:
                    continue
                if not self.looks_blocked(response):
                    return response
            return None

        try:
            with StealthySession(headless=True, google_search=True,
                                 network_idle=True, timeout=45_000,
                                 proxy=first_proxy()) as session:
                fetch(session, f"https://{self.domain}/")
                for url in self.BRAND_INDEX_PAGES:
                    response = fetch(session, url)
                    if response is None:
                        continue
                    for node in response.css(self.BRAND_LIST_SELECTOR):
                        text = clean_text(node.get_all_text()) or ""
                        if 1 < len(text) < 40:
                            names.add(text)
        except Exception:
            return sorted(names)
        return sorted(names)

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
            timeout=20_000,
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

        if not added:
            self._empty_lists.add(requested.path)

        page = int(parse_qs(requested.query).get("paging.index", ["0"])[0])
        if added and page + 1 < self.MAX_LIST_PAGES:
            return [f"https://{self.domain}/{path}?paging.index={page + 1}"
                    f"&paging.size={self.listing_page_size}"]
        return []

    def crawl_warnings(self) -> List[str]:
        warnings = [f"{brand}: its Boots brand page links no product lists, so only "
                    f"the featured products on that page were collected"
                    for brand in sorted(self._featured_only)]
        if self._empty_lists:
            warnings.append(
                f"{len(self._empty_lists)} product list(s) returned no products at "
                f"all, so nothing behind them was read: "
                + ", ".join(sorted(self._empty_lists)[:8])
            )
        return warnings

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

    _TILE = "[class*='oct-teaser--theme-productTile']"
    _BARE_PRICE_RE = re.compile(r"^\s*£\s?[\d,]+\.\d{2}\s*$")

    def extract_products_from_listing(
        self, response, brand: Optional[str] = None
    ) -> List[Product]:
        """Products read from a listing page's own tiles"""
        if self.looks_blocked(response):
            return []

        now = datetime.now(timezone.utc).isoformat()
        source = str(response.url)
        found: Dict[str, Product] = {}

        for tile in response.css(self._TILE):
            url = self._tile_url(tile)
            if not url:
                continue
            product_id = self._product_id_from_url(url)
            if not product_id or product_id in found:
                continue

            title = self._tile_title(tile)
            if not title:
                continue

            original, saving = self._tile_was_and_saving(tile)
            current = self._tile_current(tile, original, saving)
            if current is None:
                continue

            found[product_id] = Product(
                retailer=self.display_name,
                brand=clean_text(brand) or "",
                brand_verified_by="listing_title" if brand else "unverified",
                product_id=product_id,
                product_title=title,
                product_url=canonical_url(url),
                source_url=source,
                currency="GBP",
                current_price=current,
                original_price=original,
                discount_amount=saving,
                discount_percent=percent_off(original, current),
                availability=None,
                variant_count=1,
                sku=product_id,
                promotional_copy=self._tile_promotion(tile),
                scraped_at=now,
            )

        return list(found.values())

    def _tile_url(self, tile) -> Optional[str]:
        """The product this tile links to."""
        for node in tile.css("a"):
            href = (node.attrib.get("href") or "").split("#")[0]
            if href.startswith("/"):
                href = f"https://{self.domain}{href}"
            if self.is_product_url(href):
                return href
        return None

    @staticmethod
    def _tile_title(tile) -> Optional[str]:
        """The tile's product name, taking the innermost title element."""
        best = None
        for node in tile.css("[class*='title']"):
            text = clean_text(node.get_all_text()) or ""
            text = re.sub(r"^none\s+", "", text)
            if text and text.lower() != "none" and (best is None or len(text) < len(best)):
                best = text
        return best

    @staticmethod
    def _tile_was_and_saving(tile):
        """(was, saving) as the tile states them, or (None, None)."""
        def amount(selector, pattern):
            for node in tile.css(selector):
                match = re.search(pattern, clean_text(node.get_all_text()) or "", re.I)
                if match:
                    value, _ = parse_price(match.group(1))
                    return value
            return None

        was = amount(".oct-teaser__productPriceWas", r"was\s*£\s?([\d,]+\.\d{2})")
        saving = amount(".oct-teaser__product-price-wrapper", r"save\s*£\s?([\d,]+\.\d{2})")
        return was, saving

    def _tile_current(self, tile, was, saving) -> Optional[float]:
        """What the tile is selling for now"""
        if was is not None and saving is not None:
            return round(was - saving, 2)
        for node in tile.css("[class*='oct-text']"):
            text = clean_text(node.get_all_text()) or ""
            if self._BARE_PRICE_RE.match(text):
                value, _ = parse_price(text)
                if value is not None:
                    return value
        return None

    @staticmethod
    def _tile_promotion(tile) -> Optional[str]:
        """Any offer wording the tile carries, beyond the bare "Offer" flag."""
        for node in tile.css("[class*='promo'], [class*='flash']"):
            text = clean_text(node.get_all_text()) or ""
            if text and text.lower() not in {"offer", "none"} and len(text) < 160:
                return text
        return None

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
