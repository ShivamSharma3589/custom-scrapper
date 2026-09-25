"""John Lewis adapter."""

import base64
import gzip
import json
import unicodedata
import re
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import unquote

from ..models import (
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
from ..promotions import (
    classify_promotion,
    is_browse_facet,
    is_confident_offer,
)
from .base import ListingPage, RetailerAdapter, register
from config import first_proxy

_PRODUCT_URL_RE = re.compile(
    r"^https?://(?:www\.)?johnlewis\.com/(?:[a-z0-9-]+/)+p(\d+)/?$", re.I
)

_BRAND_PAGE_RE = re.compile(
    r"^https://www\.johnlewis\.com/brand/([a-z0-9-]+)/_/N-(\w+)$"
)

_ARIA_PRICE_RE = re.compile(
    r"price\s+was\s*£\s*([\d,.]+)\s*,\s*now\s*£\s*([\d,.]+)", re.IGNORECASE
)

_ARIA_RANGE_RE = re.compile(
    r"price\s+is\s*£\s*([\d,.]+)\s*[–—-]\s*£\s*([\d,.]+)", re.IGNORECASE
)

_ARIA_RANGE_DISCOUNT_RE = re.compile(
    r"price\s+was\s*£\s*([\d,.]+)\s*[–—-]\s*£\s*[\d,.]+\s*,\s*"
    r"now\s*£\s*([\d,.]+)\s*[–—-]\s*£\s*[\d,.]+",
    re.IGNORECASE,
)

_FETCH_AS_BASE64 = """
async (url) => {
  const r = await fetch(url, {credentials: 'include'});
  if (!r.ok) return 'ERR:' + r.status;
  const bytes = new Uint8Array(await r.arrayBuffer());
  let bin = ''; const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(bin);
}
"""

_GRID_INDEX = "https://www.johnlewis.com/sitemap/grids/grids.xml"

_BRAND_INDEX = "https://www.johnlewis.com/brands/all"

_BRAND_LINK_RE = re.compile(r"/brand/([A-Za-z0-9%-]+)/_/N-(\w+)")

_LISTING_SIZE_RE = re.compile(
    r'"results"\s*:\s*(\d+)\s*,\s*"pagesAvailable"\s*:\s*(\d+)'
)

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)

_MAX_GRID_SHARDS = 6


@register
class JohnLewisAdapter(RetailerAdapter):
    """Extraction rules for johnlewis.com."""

    name = "johnlewis"
    domain = "www.johnlewis.com"
    display_name = "John Lewis"

    crawl_delay = 3.0
    max_concurrent_requests = 1

    sitemap_urls: List[str] = []

    supports_categories = True

    confirms_brand_stocking = True

    CATEGORY_SLUGS = {
        "skincare": "skin care",
        "makeup": "make-up",
        "make-up": "make-up",
        "make up": "make-up",
        "fragrance": "fragrance",
        "haircare": "hair care",
        "hair": "hair care",
        "men": "men",
        "mens": "men",
        "gift": "gifts",
        "gifts": "gifts",
        "bath and body": "bath & body",
        "home": "home fragrance",
    }

    def __init__(self) -> None:
        self._codes: Optional[Dict[str, str]] = None
        self._stated_results: Dict[str, int] = {}
        self._wanted_slugs: List[str] = []
        self._offer_depth: Dict[str, int] = {}

    def configure_session(self, manager) -> None:
        """A stealth browser per proxy: the first fetches, the rest are spares."""
        from scrapling.fetchers import AsyncStealthySession

        def browser(proxy: str) -> AsyncStealthySession:
            return AsyncStealthySession(
                headless=True,
                google_search=True,
                network_idle=True,
                timeout=120_000,
                max_pages=2,
                proxy=proxy,
                disable_resources=True,
                extra_flags=["--disable-http2"],
            )

        self.add_proxied_sessions(manager, browser)

    BRAND_SLUG_OVERRIDES = {
        "jo malone": "jo-malone-london",
        "jo malone london": "jo-malone-london",
        "mac": "mac",
        "estee lauder": "estee-lauder",
    }

    @classmethod
    def _brand_slug(cls, brand: str) -> str:
        """Brand name -> the slug John Lewis uses in its brand-page URLs."""
        key = brand.strip().lower()
        if key in cls.BRAND_SLUG_OVERRIDES:
            return cls.BRAND_SLUG_OVERRIDES[key]
        return re.sub(r"[^a-z0-9]+", "-", key).strip("-")

    @property
    def _cache_path(self) -> Path:
        return Path(__file__).resolve().parent.parent.parent / ".jl_brand_codes.json"

    def _load_cached_codes(self) -> Dict[str, Dict[str, str]]:
        """The cache, with entries written by older versions upgraded."""
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        upgraded: Dict[str, Dict[str, str]] = {}
        for key, value in raw.items():
            if isinstance(value, str):
                upgraded[key] = {"code": value, "slug": key}
            elif isinstance(value, dict) and value.get("code"):
                upgraded[key] = {"code": value["code"], "slug": value.get("slug") or key}
        return upgraded

    def _save_cached_codes(self, codes: Dict[str, Dict[str, str]]) -> None:
        try:
            self._cache_path.write_text(
                json.dumps(codes, indent=2, sort_keys=True), encoding="utf-8"
            )
        except Exception:  # pragma: no cover - a cache miss is not fatal
            pass

    def prepare(self, brands: Sequence[str]) -> List[str]:
        """Resolve and cache every requested brand's page code up front."""
        self._wanted_slugs = list(dict.fromkeys(
            slug for b in brands
            for slug in (self._brand_slug(b), re.sub(r"[^a-z0-9]+", "-", b.lower()).strip("-"))))
        pages = self.brand_pages(brands, resolve=True)
        return [
            f"no John Lewis brand page found for {brand!r} in the A-Z index "
            f"-- it may not be stocked. Continuing without it."
            for brand in brands
            if self._brand_slug(brand) not in pages
        ]

    def expected_product_count(self, brands: Sequence[str]) -> Optional[int]:
        """The total John Lewis's listings stated during this run."""
        totals = [self._stated_results[self._brand_slug(b)]
                  for b in brands if self._brand_slug(b) in self._stated_results]
        return sum(totals) if totals else None

    def brand_codes(self, brands: Sequence[str], resolve: bool = False) -> Dict[str, str]:
        """Just the page codes, for callers that do not need the slug."""
        return {
            slug: page["code"]
            for slug, page in self.brand_pages(brands, resolve=resolve).items()
        }

    def brand_pages(
        self, brands: Sequence[str], resolve: bool = False
    ) -> Dict[str, Dict[str, str]]:
        """Map each requested brand to its John Lewis brand page."""
        cached = self._load_cached_codes()
        wanted = {self._brand_slug(b) for b in brands}

        if resolve:
            missing = wanted - set(cached)
            if missing:
                found = self._read_brand_index(missing)
                if not found:
                    found = self._scan_grid_sitemaps(missing)
                if found:
                    cached.update(found)
                    self._save_cached_codes(cached)

        return {slug: dict(cached[slug]) for slug in wanted if slug in cached}

    def _read_brand_index(self, wanted: Iterable[str]) -> Dict[str, Dict[str, str]]:
        """Resolve brand pages from John Lewis's A-Z index."""
        from scrapling.fetchers import StealthyFetcher

        wanted = set(wanted)
        try:
            response = StealthyFetcher.fetch(
                _BRAND_INDEX, headless=True, network_idle=True, proxy=first_proxy()
            )
            html = getattr(response, "html_content", None) or str(response)
        except Exception:
            return {}

        found: Dict[str, Dict[str, str]] = {}
        for slug, code in _BRAND_LINK_RE.findall(html):
            key = self._fold_slug(slug)
            if key in wanted:
                found[key] = {"code": code, "slug": slug}
        return found

    @staticmethod
    def _fold_slug(slug: str) -> str:
        """A site slug reduced to the form `_brand_slug` produces."""
        decoded = unquote(slug)
        stripped = "".join(
            ch for ch in unicodedata.normalize("NFKD", decoded)
            if not unicodedata.combining(ch)
        )
        return stripped.lower()

    def _scan_grid_sitemaps(self, wanted: Iterable[str]) -> Dict[str, Dict[str, str]]:
        """Walk the gzipped grid sitemaps for the brand pages we still need."""
        from scrapling.fetchers import StealthySession

        wanted = set(wanted)
        found: Dict[str, Dict[str, str]] = {}
        box: Dict[str, str] = {}

        def action_for(url):
            def act(page):
                try:
                    box[url] = page.evaluate(_FETCH_AS_BASE64, url)
                except Exception as exc:  # pragma: no cover - network shape
                    box[url] = f"ERR:{exc}"
                return page
            return act

        def grab(session, url: str) -> str:
            """Fetch a gzipped sitemap through an already-loaded page."""
            session.fetch(f"https://{self.domain}/", page_action=action_for(url))
            payload = box.get(url, "")
            if not payload or payload.startswith("ERR"):
                return ""
            raw = base64.b64decode(payload)
            try:
                return gzip.decompress(raw).decode("utf-8", "replace")
            except (OSError, EOFError):
                return raw.decode("utf-8", "replace")

        try:
            with StealthySession(
                headless=True, google_search=True, network_idle=True, timeout=120_000
            ) as session:
                index = grab(session, _GRID_INDEX)
                shards = re.findall(r"<loc>([^<]+)</loc>", index)

                for shard in shards[:_MAX_GRID_SHARDS]:
                    xml = grab(session, shard)
                    if not xml:
                        continue
                    for url in re.findall(r"<loc>([^<]+)</loc>", xml):
                        match = _BRAND_PAGE_RE.match(url)
                        if match and match.group(1) in wanted:
                            found[match.group(1)] = {
                                "code": match.group(2), "slug": match.group(1),
                            }
                    if wanted <= set(found):
                        break
        except Exception as exc:
            raise RuntimeError(
                f"could not read John Lewis brand codes from the grid sitemaps: {exc}"
            ) from exc

        return found

    def product_listing_urls(
        self, brand: str, categories: Sequence[str], max_pages: int
    ) -> List[ListingPage]:
        """The first page of the brand's listing. `more_listing_pages` adds the rest."""
        page = self.brand_pages([brand]).get(self._brand_slug(brand))
        if not page:
            return []
        return [ListingPage(
            url=f"https://{self.domain}/brand/{page['slug']}/_/N-{page['code']}",
            brand=brand,
        )]

    def more_listing_pages(self, response, meta: dict, added: int) -> List[str]:
        """Every other page of a brand listing, read off its first page."""
        requested = meta.get("url") or str(response.url)
        if "?" in requested:
            return []
        size = _LISTING_SIZE_RE.search(response.html_content or "")
        if not size:
            return []
        results, available = int(size.group(1)), int(size.group(2))
        if meta.get("brand"):
            self._stated_results[self._brand_slug(meta["brand"])] = results
        return [f"{requested}?page={n}" for n in range(2, available + 1)]

    def campaign_seed_urls(self, brands: Sequence[str]) -> List[str]:
        """None -- the brand listing already carries the campaigns."""
        return []

    def _listing_tiles(self, response) -> List[Dict[str, Any]]:
        """The product tiles in a brand listing's page state."""
        page = self._page_state(response).get("props", {}).get("pageProps", {})
        listing = page.get("productListingData") if isinstance(page, dict) else None
        tiles = listing.get("products") if isinstance(listing, dict) else None
        return [t for t in tiles or [] if isinstance(t, dict) and t.get("productId")]

    @staticmethod
    def _one_price(tile: Dict[str, Any]) -> bool:
        """True when every size and shade of the tile sells at one price."""
        prices = tile.get("variantPriceRange") or {}
        now = prices.get("value") or {}
        history = [h for h in prices.get("reductionHistory") or [] if isinstance(h, dict)]
        was = (history[0].get("display") or {}) if history else {}
        return bool(now.get("min")) and now.get("min") == now.get("max") and was.get("min") == was.get("max")

    def extract_products_from_listing(self, response, brand: Optional[str] = None) -> List[Product]:
        """Products read straight from a brand listing, where its data is exact."""
        products = []
        now_utc = datetime.now(timezone.utc).isoformat()
        for tile in self._listing_tiles(response):
            if not self._one_price(tile):
                continue
            prices = tile["variantPriceRange"]
            current = self._decimal(prices["value"]["min"])
            history = [h for h in prices.get("reductionHistory") or [] if isinstance(h, dict)]
            original, currency = parse_price((history[0].get("display") or {}).get("min")) if history else (None, None)
            if original is not None and current is not None and original <= current:
                original = None
            url = canonical_url(f"https://{self.domain}{tile.get('url') or ''}")
            available = tile.get("isAvailableToOrder")
            products.append(Product(
                retailer=self.display_name,
                brand=clean_text(tile.get("brand")) or "",
                brand_verified_by="listing_data",
                product_id=str(tile["productId"]),
                product_title=clean_text(tile.get("title")) or "",
                product_url=url,
                source_url=canonical_url(str(response.url)),
                sku=clean_text(tile.get("defaultSkuId")),
                current_price=current,
                original_price=original,
                discount_amount=round(original - current, 2) if original is not None else None,
                discount_percent=percent_off(original, current),
                currency=currency or "GBP",
                availability="InStock" if available else "OutOfStock" if available is not None else None,
                variant_count=len(tile.get("colorSwatches") or []) or 1,
                price_is_from=False,
                sku_matches_product_id=False,
                promotional_copy=" | ".join(self._promotional_titles(tile.get("messaging"))) or None,
                scraped_at=now_utc,
            ))
        return products

    def product_pages_to_read(self, response, brand: Optional[str] = None) -> List[str]:
        """Product pages for the tiles the listing cannot price exactly."""
        wanted = {str(t["productId"]) for t in self._listing_tiles(response) if not self._one_price(t)}
        return [url for url in self.extract_product_links(response)
                if self.product_id_from_url(url) in wanted]

    def extract_product_links(self, response, brand: Optional[str] = None) -> List[str]:
        """Product URLs from a rendered brand listing page."""
        slug = self._brand_slug(brand) if brand else None
        found: List[str] = []
        for node in response.css("a"):
            href = node.attrib.get("href") or ""
            if href.startswith("/"):
                href = f"https://{self.domain}{href}"
            if not (href.startswith("http") and self.is_product_url(href)):
                continue
            if slug and slug not in href.lower():
                continue
            found.append(href)
        return self._one_url_per_product(found)

    def _one_url_per_product(self, urls: Iterable[str]) -> List[str]:
        """Keep one URL per product id, discarding the other shades."""
        seen: Dict[str, str] = {}
        for url in urls:
            clean = canonical_url(url)
            product_id = self.product_id_from_url(clean)
            if product_id is None:
                continue
            seen.setdefault(product_id, clean)
        return list(seen.values())

    def is_product_url(self, url: str) -> bool:
        return bool(_PRODUCT_URL_RE.match(url.split("?")[0]))

    def product_id_from_url(self, url: str) -> Optional[str]:
        match = _PRODUCT_URL_RE.match(url.split("?")[0])
        return match.group(1) if match else None

    def select_candidates(self, urls: Iterable[str], brands: Sequence[str]) -> List[str]:
        """Keep URLs whose slug mentions a requested brand."""
        slugs = [self._brand_slug(b) for b in brands]
        keep = []
        for url in urls:
            if not self.is_product_url(url):
                continue
            lowered = url.lower()
            if not slugs or any(slug in lowered for slug in slugs):
                keep.append(url)
        return self._one_url_per_product(keep)

    def extract_product_variants(
        self, response, target_brands: Sequence[str] = ()
    ) -> List[Product]:
        """One record per size, when John Lewis prices the sizes separately."""
        sizes = self._size_variants(response)
        if len(sizes) < 2:
            return []

        base = self.extract_product(response, target_brands)
        if base is None:
            return []

        products: List[Product] = []
        for variant in sizes:
            price = variant.get("price") or {}
            current = self._decimal(price.get("value"))
            if current is None:
                continue

            history = [
                h for h in (price.get("reductionHistory") or [])
                if isinstance(h, dict)
            ]
            history.sort(key=lambda h: h.get("chronology", 0))
            original = self._decimal(history[0].get("value")) if history else None
            if original is not None and original <= current:
                original = None

            path = (variant.get("pdpURL") or {}).get("url") or ""
            variant_promo = " | ".join(
                self._promotional_titles(variant.get("messaging"))
            )
            available = (variant.get("availability") or {}).get("availableToOrder")

            products.append(replace(
                base,
                product_title=clean_text(variant.get("title")) or base.product_title,
                product_id=clean_text(variant.get("displayCode")) or base.product_id,
                sku=clean_text(variant.get("displayCode")),
                sku_matches_product_id=True,
                product_url=(
                    canonical_url(f"https://{self.domain}{path}")
                    if path else base.product_url
                ),
                current_price=current,
                original_price=original,
                discount_amount=(
                    round(original - current, 2) if original is not None else None
                ),
                discount_percent=percent_off(original, current),
                price_is_from=False,
                variant_count=1,
                promotional_copy=variant_promo or base.promotional_copy,
                availability=(
                    "InStock" if available
                    else "OutOfStock" if available is not None
                    else base.availability
                ),
            ))

        return products if len(products) >= 2 else []

    @staticmethod
    def _size_variants(response) -> List[Dict[str, Any]]:
        """The page's variants, but only when they differ by SIZE."""
        body = getattr(response, "html_content", None) or getattr(response, "body", None)
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        match = _NEXT_DATA_RE.search(body or "")
        if not match:
            return []
        try:
            data = json.loads(match.group(1))
        except (ValueError, TypeError):
            return []

        product = (
            data.get("props", {}).get("pageProps", {}).get("product")
            if isinstance(data, dict) else None
        )
        variants = product.get("variants") if isinstance(product, dict) else None
        if not isinstance(variants, list):
            return []

        sized = [
            v for v in variants
            if isinstance(v, dict)
            and (v.get("differentiators") or {}).get("size")
            and not (v.get("differentiators") or {}).get("colour")
        ]
        seen = {(v.get("differentiators") or {}).get("size") for v in sized}
        return sized if len(seen) == len(sized) else []

    @classmethod
    def _page_state(cls, response) -> Dict[str, Any]:
        """The page's `__NEXT_DATA__`, or an empty dict."""
        body = getattr(response, "html_content", None) or getattr(response, "body", None)
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        match = _NEXT_DATA_RE.search(body or "")
        if not match:
            return {}
        try:
            data = json.loads(match.group(1))
        except (ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _promotional_titles(messaging: Any) -> List[str]:
        """The promotional entries in a John Lewis `messaging` list."""
        titles: List[str] = []
        for entry in messaging or []:
            if not isinstance(entry, dict) or entry.get("type") != "promotional":
                continue
            text = clean_text(entry.get("title"))
            if text and text not in titles:
                titles.append(text)
        return titles

    @classmethod
    def _stated_promotions(cls, response) -> List[str]:
        """Every promotion John Lewis states on this page, product or listing."""
        props = cls._page_state(response).get("props", {})
        page = props.get("pageProps", {}) if isinstance(props, dict) else {}
        if not isinstance(page, dict):
            return []

        found: List[str] = []
        product = page.get("product")
        if isinstance(product, dict):
            found.extend(cls._promotional_titles(product.get("messaging")))
            for variant in product.get("variants") or []:
                if isinstance(variant, dict):
                    found.extend(cls._promotional_titles(variant.get("messaging")))

        listing = page.get("productListingData")
        if isinstance(listing, dict):
            for item in listing.get("products") or []:
                if isinstance(item, dict):
                    found.extend(cls._promotional_titles(item.get("messaging")))

        ordered: List[str] = []
        for text in found:
            if text not in ordered:
                ordered.append(text)
        return ordered

    @staticmethod
    def _decimal(value: Any) -> Optional[float]:
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return None

    def extract_product(
        self, response, target_brands: Sequence[str] = ()
    ) -> Optional[Product]:
        """Build a Product from the page's JSON-LD, plus the PDP price block."""
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

        current, _ = parse_price(offers.get("price"))
        original, aria_current, price_is_from = self._pdp_prices(response)
        if current is None:
            current = aria_current

        availability = (offers.get("availability") or "").rsplit("/", 1)[-1] or None

        stated = self._promotional_titles(
            (self._page_state(response)
             .get("props", {}).get("pageProps", {}).get("product") or {})
            .get("messaging")
        )
        promo = " | ".join(stated) or self._page_offer_text(response)

        return Product(
            retailer=self.display_name,
            brand=brand,
            brand_verified_by="json_ld_brand",
            product_id=product_id,
            product_title=clean_text(block.get("name")) or "",
            product_url=canonical_url(url),
            source_url=canonical_url(url),
            sku=clean_text(offers.get("sku")),
            current_price=current,
            original_price=original,
            discount_amount=(
                round(original - current, 2)
                if original is not None and current is not None and original > current
                else None
            ),
            discount_percent=percent_off(original, current),
            currency=clean_text(offers.get("priceCurrency")) or "GBP",
            availability=availability,
            category=self._category(block),
            variant_count=None if price_is_from else (block.get("shade_count") or 1),
            price_is_from=price_is_from,
            sku_matches_product_id=False,
            promotional_copy=promo,
            scraped_at=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _category(block: Dict[str, Any]) -> Optional[str]:
        """The retailer's own category, reduced to its most specific segment."""
        raw = clean_text(block.get("category"))
        if not raw:
            return None
        leaf = raw.split(">")[-1].strip()
        leaf = re.sub(r"^view all\s+", "", leaf, flags=re.IGNORECASE)
        return leaf.lower() or None

    def _pdp_prices(self, response):
        """(was, now, is_from) for THIS product, from its own price block."""
        def pair(was_text: str, now_text: str, is_from: bool = False):
            was_amount, _ = parse_price(was_text)
            now_amount, _ = parse_price(now_text)
            return was_amount, now_amount, is_from

        for node in response.css("[data-testid='price-and-price-promise']"):
            texts = [node.get_all_text() or ""]
            texts.extend(
                holder.attrib.get("aria-label") or "" for holder in node.css("[aria-label]")
            )

            for text in texts:
                spread = _ARIA_RANGE_DISCOUNT_RE.search(text)
                if spread:
                    return pair(spread.group(1), spread.group(2), is_from=True)

                match = _ARIA_PRICE_RE.search(text)
                if match:
                    return pair(match.group(1), match.group(2))

                span = _ARIA_RANGE_RE.search(text)
                if span:
                    return pair("", span.group(1), is_from=True)

            was = node.css("[data-testid='price-prev']")
            now = node.css("[data-testid='price-now']")
            if was or now:
                return pair(
                    was[0].get_all_text() if was else "",
                    now[0].get_all_text() if now else "",
                )
        return (None, None, False)

    def _page_offer_text(self, response) -> Optional[str]:
        """The first genuine offer stated on this product's page."""
        for node in response.css("[data-testid='price-and-price-promise'], .PriceAndPricePromise_container__t20gZ"):
            text = clean_text(node.get_all_text())
            if text and is_confident_offer(text) and "was" not in text.lower():
                return text
        return None

    def campaign_discovery_urls(self) -> List[str]:
        """John Lewis's "Sale & Offers" page, linked from its homepage."""
        return ["https://www.johnlewis.com/special-offers/c50000110"]

    def extract_campaign_directory(self, response) -> List[Campaign]:
        """Every offer linked from an offers page, via the shared scan."""
        return self._scan_offer_links(response)

    def offer_page_links(self, response) -> List[str]:
        """Offers pages to read after this one, limited to the requested brands."""
        url = str(response.url)
        page = url.split("?")[0].rstrip("/")
        hubs = {u.rstrip("/") for u in self.campaign_discovery_urls()}
        depth = 0 if page in hubs else self._offer_depth.get(page, 2)

        lists_ours = any(self._names_wanted_brand(link)
                         for link in self.extract_product_links(response))
        follow_all = depth == 0 or (depth == 1 and lists_ours)

        links = []
        for node in response.css("a"):
            href = (node.attrib.get("href") or "").split("#")[0].split("?")[0].rstrip("/")
            if href.startswith("/"):
                href = f"https://{self.domain}{href}"
            path = href[len(f"https://{self.domain}"):] if href.startswith(f"https://{self.domain}/") else ""
            if not path.startswith(("/special-offers/", "/browse/special-offers/")):
                continue
            if follow_all or self._names_wanted_brand(href, clean_text(node.get_all_text()) or ""):
                links.append(href)
                self._offer_depth[href] = min(self._offer_depth.get(href, depth + 1), depth + 1)

        size = _LISTING_SIZE_RE.search(response.html_content or "")
        if "?" not in url and size and self._names_wanted_brand(page):
            links += [f"{url}?page={n}" for n in range(2, int(size.group(2)) + 1)]

        return list(dict.fromkeys(links))

    def _names_wanted_brand(self, *texts: str) -> bool:
        """True when a URL or link text names one of the requested brands."""
        words = re.sub(r"[^a-z0-9]+", " ", " ".join(self._fold_slug(t) for t in texts))
        return any(re.search(rf"(?<![a-z0-9]){slug.replace('-', ' ')}(?![a-z0-9])", words)
                   for slug in self._wanted_slugs)

    def offer_page_products(self, response) -> List[str]:
        """Ids of the products an offers listing shows."""
        return [pid for pid in (self.product_id_from_url(u)
                                for u in self.extract_product_links(response)) if pid]

    def extract_campaigns(self, response) -> List[Campaign]:
        """Promotions stated on this page."""
        url = str(response.url)
        campaigns: List[Campaign] = []
        seen: set = set()

        on_product = self.is_product_url(url)

        for text in self._stated_promotions(response):
            if text in seen:
                continue
            seen.add(text)
            campaigns.append(
                Campaign(
                    retailer=self.display_name,
                    promotion_text=text,
                    promotion_type=classify_promotion(text),
                    scope=SCOPE_PRODUCT,
                    scope_value=None,
                    promo_code=extract_promo_code(text),
                    source_url=canonical_url(url),
                    landing_url=None,
                )
            )

        for node in response.css("[data-testid='promotion'], [class*='Promotion'], [class*='offer']"):
            text = clean_text(node.get_all_text())
            if not text or text in seen or not is_confident_offer(text):
                continue
            if is_browse_facet(text, url):
                continue
            if len(text) > 160:
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

    @staticmethod
    def _product_json_ld(response) -> Optional[Dict[str, Any]]:
        """The page's JSON-LD product, as one Product block."""
        items = []
        for node in response.css('script[type="application/ld+json"]'):
            raw = node.text
            if not raw:
                continue
            try:
                data = json.loads(raw.strip())
            except (ValueError, TypeError):
                continue
            items += [i for i in (data if isinstance(data, list) else [data]) if isinstance(i, dict)]

        for item in items:
            if item.get("@type") == "Product":
                return item

        for group in items:
            if group.get("@type") != "ProductGroup":
                continue
            shades = [v for v in group.get("hasVariant") or [] if isinstance(v, dict)]
            page = canonical_url(str(response.url))
            this_shade = next((v for v in shades if canonical_url(v.get("url") or "") == page),
                              shades[0] if shades else {})
            return {**group, "offers": this_shade.get("offers"), "shade_count": len(shades)}
        return None

    def looks_blocked(self, response) -> bool:
        """John Lewis hangs instead of serving a challenge page, so a timeout counts as a refusal."""
        return False
