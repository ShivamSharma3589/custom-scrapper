"""John Lewis adapter.

The third retailer, and the one that most clearly justifies the adapter
pattern: it shares no discovery mechanism with either of the others. Every
detail below was read off the live site.

**Fetching.** Plain HTTP hangs -- a request to the sitemap index never
returns rather than being refused, which is worse than a block because it
looks like a slow network. A browser session works normally, so
`configure_session` registers a stealth session and every route uses it.

**Discovery.** John Lewis publishes a real sitemap, but every shard is
gzipped, and a browser asked to navigate to a `.gz` starts a download instead
of loading a page ("Page.goto: Download is starting"), so `SitemapSpider`
cannot read them. Discovery therefore goes through brand landing pages
instead, which robots.txt explicitly allows:

    Allow: /brand/*/_/N-*

Those pages need a code John Lewis assigns to each brand
(`/brand/clinique/_/N-1z13ywm`). The codes are only published in the gzipped
grid sitemaps, so `_brand_codes` fetches those through the browser's own
`fetch()` from inside a loaded page -- which is not a navigation, so no
download is triggered -- and caches the result on disk. Guessing a code is
not an option: a wrong one returns HTTP 404.

**Product pages** carry a JSON-LD `Product` that is richer than either other
retailer's:

  * `brand` is an explicit `{"@type": "Brand", "name": "Clinique"}` object,
    so the brand is stated outright rather than inferred from a breadcrumb.
  * `category` is a real taxonomy path ("Beauty > Skin Care & Treatments >
    View all Skin Care"), so no separate category lookup is needed here.
  * `offers` carries sku, price, currency and availability.

The JSON-LD holds only the current price, so the "was" price comes from the
PDP price block. That block is identified by `data-testid="price-prev"` /
`"price-now"`; the recommendation carousels on the same page use
`product-card-price-prev`, a different testid, so scoping to the former keeps
a recommended product's discount from being attached to this one.
"""

from __future__ import annotations

import base64
import gzip
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..models import SCOPE_PRODUCT, SCOPE_SITEWIDE, Campaign, Product
from ..normalize import (
    canonical_url,
    clean_text,
    extract_promo_code,
    parse_price,
    percent_off,
)
from ..promotions import classify_promotion, is_confident_offer
from .base import ListingPage, RetailerAdapter, register

# Product URLs end in /p<id>, optionally after a colour segment:
#   /clinique-redness-solutions-daily-relief-cream-50ml/p47865
#   /whistles-camilla-wide-leg-trousers/black/p5966220
_PRODUCT_URL_RE = re.compile(
    r"^https?://(?:www\.)?johnlewis\.com/(?:[a-z0-9-]+/)+p(\d+)/?$", re.I
)

# A brand landing page and the code John Lewis assigns it.
_BRAND_PAGE_RE = re.compile(
    r"^https://www\.johnlewis\.com/brand/([a-z0-9-]+)/_/N-(\w+)$"
)

# The PDP price block states both numbers in one accessible label, which is
# steadier than reading two sibling elements: "The price was £50.00, now £40.00"
_ARIA_PRICE_RE = re.compile(
    r"price\s+was\s*£\s*([\d,.]+)\s*,\s*now\s*£\s*([\d,.]+)", re.IGNORECASE
)

# A size range rather than one price: "The price is £33.60 – £156.00".
# John Lewis uses an en dash; a hyphen is allowed for safety.
_ARIA_RANGE_RE = re.compile(
    r"price\s+is\s*£\s*([\d,.]+)\s*[–—-]\s*£\s*([\d,.]+)", re.IGNORECASE
)

# A DISCOUNTED range -- several sizes, all reduced:
#   "The price was £220.00 – £310.00, now £187.00 – £263.50"
# Both ends move, so the comparable pair is the two low ends. Without this the
# simple was/now pattern does not match and the discount is lost entirely,
# reporting a reduced product as full price.
_ARIA_RANGE_DISCOUNT_RE = re.compile(
    r"price\s+was\s*£\s*([\d,.]+)\s*[–—-]\s*£\s*[\d,.]+\s*,\s*"
    r"now\s*£\s*([\d,.]+)\s*[–—-]\s*£\s*[\d,.]+",
    re.IGNORECASE,
)

# Fetch a URL from inside an already-loaded page. This uses the browser's own
# connection, cookies and TLS fingerprint but is not a navigation, so a .gz
# comes back as bytes instead of triggering a download.
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

#: How many grid shards to read before giving up on a brand. Shard 0 alone
#: holds ~1,950 brand pages and covers most of what we look for; the cap stops
#: an unstocked brand from walking all 199.
_MAX_GRID_SHARDS = 6


@register
class JohnLewisAdapter(RetailerAdapter):
    """Extraction rules for johnlewis.com."""

    name = "johnlewis"
    domain = "www.johnlewis.com"
    display_name = "John Lewis"

    # No sitemap route: the shards are gzipped and the browser will not
    # navigate to them. See the module docstring.
    sitemap_urls: List[str] = []

    # Category comes from the product's own JSON-LD, so a category filter is
    # honoured by discarding products whose stated category does not match --
    # no separate listing lookup is needed, unlike the other two retailers.
    supports_categories = True

    #: Products per brand listing page, as John Lewis renders them.
    listing_page_size = 24

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

    # --- fetching ---------------------------------------------------------

    def configure_session(self, manager) -> None:
        """A stealth browser for everything.

        Plain HTTP does not merely fail here, it hangs, so there is no cheap
        route to fall back on for product pages the way Lookfantastic has.
        """
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

    # --- brand code resolution -------------------------------------------

    @staticmethod
    def _brand_slug(brand: str) -> str:
        """Brand name -> the slug John Lewis uses ("Jo Malone" -> jo-malone)."""
        return re.sub(r"[^a-z0-9]+", "-", brand.strip().lower()).strip("-")

    @property
    def _cache_path(self) -> Path:
        return Path(__file__).resolve().parent.parent.parent / ".jl_brand_codes.json"

    def _load_cached_codes(self) -> Dict[str, str]:
        try:
            return json.loads(self._cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_cached_codes(self, codes: Dict[str, str]) -> None:
        try:
            self._cache_path.write_text(
                json.dumps(codes, indent=2, sort_keys=True), encoding="utf-8"
            )
        except Exception:  # pragma: no cover - a cache miss is not fatal
            pass

    def prepare(self, brands: Sequence[str]) -> List[str]:
        """Resolve and cache every requested brand's page code up front.

        This has to happen before the crawl: resolving a code needs its own
        browser session, and starting one inside the crawler's running event
        loop fails. Doing it here also means an unstocked brand is reported
        as a warning instead of silently contributing nothing.
        """
        codes = self.brand_codes(brands, resolve=True)
        return [
            f"no John Lewis brand page found for {brand!r} "
            f"(searched {_MAX_GRID_SHARDS} sitemap shards) -- it may not be "
            f"stocked. Continuing without it."
            for brand in brands
            if self._brand_slug(brand) not in codes
        ]

    def brand_codes(self, brands: Sequence[str], resolve: bool = False) -> Dict[str, str]:
        """Map each requested brand to its John Lewis brand-page code.

        `resolve` controls whether a cache miss is allowed to hit the network.
        It defaults to False so that callers running inside the crawler never
        trigger a lookup: resolving needs its own browser session, and
        starting one inside a running asyncio loop raises
        "Playwright Sync API inside the asyncio loop" -- which killed an
        entire seven-brand run because ONE brand could not be resolved and the
        lookup was retried mid-crawl.

        Only `prepare()` passes resolve=True, and it runs before the crawl
        starts. A brand that still cannot be found is simply absent from the
        result; the caller reports it rather than fetching a 404.
        """
        cached = self._load_cached_codes()
        wanted = {self._brand_slug(b) for b in brands}

        if resolve:
            missing = wanted - set(cached)
            if missing:
                found = self._scan_grid_sitemaps(missing)
                if found:
                    cached.update(found)
                    self._save_cached_codes(cached)

        return {slug: cached[slug] for slug in wanted if slug in cached}

    def _scan_grid_sitemaps(self, wanted: Iterable[str]) -> Dict[str, str]:
        """Walk the gzipped grid sitemaps for the brand pages we still need."""
        from scrapling.fetchers import StealthySession

        wanted = set(wanted)
        found: Dict[str, str] = {}
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
                            found[match.group(1)] = match.group(2)
                    if wanted <= set(found):
                        break
        except Exception as exc:
            # Do not swallow this. A failure here means every brand resolves
            # to nothing and the run returns zero products while reporting
            # success -- which is exactly what happened before `prepare()`
            # moved this call outside the crawler's event loop.
            raise RuntimeError(
                f"could not read John Lewis brand codes from the grid sitemaps: {exc}"
            ) from exc

        return found

    # --- discovery --------------------------------------------------------

    def product_listing_urls(
        self, brand: str, categories: Sequence[str], max_pages: int
    ) -> List[ListingPage]:
        """Paginated brand landing pages.

        The category is not applied here: John Lewis states it on the product
        page itself, so filtering happens at extraction instead of by choosing
        a different listing. That keeps one product page as the single source
        of that fact.
        """
        slug = self._brand_slug(brand)
        code = self.brand_codes([brand]).get(slug)
        if not code:
            return []

        base = f"https://{self.domain}/brand/{slug}/_/N-{code}"
        pages = [ListingPage(url=base, brand=brand, category=None)]
        pages.extend(
            ListingPage(url=f"{base}?page={index}", brand=brand, category=None)
            for index in range(2, max_pages + 1)
        )
        return pages

    def campaign_seed_urls(self, brands: Sequence[str]) -> List[str]:
        """None -- the brand listing already carries the campaigns.

        Deliberately empty. John Lewis states a brand's promotions on its
        brand landing page, which is the same URL `product_listing_urls`
        returns. Seeding it here as well queues one URL twice with two
        different callbacks; the crawler deduplicates by URL, so whichever
        was scheduled first wins and the other silently never runs. When the
        campaign callback won, the page was fetched, campaigns were recorded,
        and no products were ever queued -- a run that reported success and
        returned nothing.

        `parse_listing_page` already extracts campaigns from every listing it
        visits, so nothing is lost by leaving this empty.
        """
        return []

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
            # Cheap pre-filter only; brand is still proved from the product's
            # own JSON-LD in extract_product.
            if slug and slug not in href.lower():
                continue
            found.append(href)
        # One request per product, keeping the site's own URLs.
        return self._one_url_per_product(found)

    def _one_url_per_product(self, urls: Iterable[str]) -> List[str]:
        """Keep one URL per product id, discarding the other shades.

        John Lewis links every shade separately, all pointing at the same
        product:

            /too-faced-cloud-crush-blush/tequila-sunset/p110055590
            /too-faced-cloud-crush-blush/pink-sunset/p110055590

        The crawler deduplicates on URL, so each shade was fetched as if it
        were a different product and then collapsed to one record afterwards
        -- a dozen requests to keep one. That waste pushed a seven-brand run
        into rate limiting: 165 of 372 requests came back 403.

        The first URL for each id is kept, exactly as the site published it.
        Rewriting a shade URL to a bare `<slug>/p<id>` looks equivalent and is
        not: some products 404 without their shade segment, so an invented URL
        loses the product entirely.
        """
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
        """Keep URLs whose slug mentions a requested brand.

        A pre-filter, not verification -- John Lewis puts the brand first in
        the product slug, which makes this cheap and accurate enough to avoid
        fetching the whole catalogue.
        """
        slugs = [self._brand_slug(b) for b in brands]
        keep = []
        for url in urls:
            if not self.is_product_url(url):
                continue
            lowered = url.lower()
            if not slugs or any(slug in lowered for slug in slugs):
                keep.append(url)
        return self._one_url_per_product(keep)

    # --- extraction -------------------------------------------------------

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

        # John Lewis states the brand as a Brand object. That is the retailer's
        # own structured claim, so it is treated as proof in the same way
        # Lookfantastic's breadcrumb link is.
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

        # parse_price returns (amount, currency_code).
        current, _ = parse_price(offers.get("price"))
        original, aria_current, price_is_from = self._pdp_prices(response)
        # The accessible label and the JSON-LD should agree; prefer the
        # JSON-LD, which is the retailer's structured statement.
        if current is None:
            current = aria_current

        availability = (offers.get("availability") or "").rsplit("/", 1)[-1] or None

        promo = self._page_offer_text(response)

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
            # A from-price covers several sizes, so this page is not a
            # single-variant listing and must not be validated as one.
            variant_count=None if price_is_from else 1,
            price_is_from=price_is_from,
            # John Lewis numbers pages (p47865) and stock units (230631076)
            # separately, so these two identifiers never match by design.
            sku_matches_product_id=False,
            promotional_copy=promo,
            scraped_at=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _category(block: Dict[str, Any]) -> Optional[str]:
        """The retailer's own category, reduced to its most specific segment.

        John Lewis states a full path -- "Beauty > Skin Care & Treatments >
        View all Skin Care". The leading "View all" is navigation wording, not
        part of the category name.
        """
        raw = clean_text(block.get("category"))
        if not raw:
            return None
        leaf = raw.split(">")[-1].strip()
        leaf = re.sub(r"^view all\s+", "", leaf, flags=re.IGNORECASE)
        return leaf.lower() or None

    def _pdp_prices(self, response):
        """(was, now, is_from) for THIS product, from its own price block.

        Scoped to the PDP price element. The recommendation carousels on the
        same page carry their own was/now pairs under `product-card-price-*`,
        and reading one of those would attach another product's discount to
        this one.
        """
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
                # Checked first: a discounted range also contains "was ... now",
                # and the single-price pattern must not claim it.
                spread = _ARIA_RANGE_DISCOUNT_RE.search(text)
                if spread:
                    return pair(spread.group(1), spread.group(2), is_from=True)

                match = _ARIA_PRICE_RE.search(text)
                if match:
                    return pair(match.group(1), match.group(2))

                # A range means several sizes behind one page. There is no
                # single "was"; the low end is a from-price and is flagged as
                # such rather than passed off as the product's price.
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

    def extract_campaigns(self, response) -> List[Campaign]:
        """Promotions stated on this page.

        John Lewis has no site-wide strip banner of the kind Lookfantastic
        runs, and no central offers hub -- its promotions are stated per brand
        and per product. Anything found on a product page is therefore scoped
        to that product; anything on a brand listing is left unresolved rather
        than claimed as a brand-wide campaign, because the same wording
        ("Save 20% on selected products") explicitly does not cover the whole
        brand.
        """
        url = str(response.url)
        campaigns: List[Campaign] = []
        seen: set = set()

        on_product = self.is_product_url(url)

        for node in response.css("[data-testid='promotion'], [class*='Promotion'], [class*='offer']"):
            text = clean_text(node.get_all_text())
            if not text or text in seen or not is_confident_offer(text):
                continue
            if len(text) > 160:  # a container, not a promotion label
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

    # --- helpers ----------------------------------------------------------

    @staticmethod
    def _product_json_ld(response) -> Optional[Dict[str, Any]]:
        """The page's JSON-LD Product block, if it has one."""
        for node in response.css('script[type="application/ld+json"]'):
            # `.text` not `.get_all_text()`: the latter returns empty for a
            # script element, since it collects rendered text.
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

    def looks_blocked(self, response) -> bool:
        """John Lewis hangs rather than serving a challenge page, so there is
        no marker to match on; a timeout surfaces as a request failure."""
        return False
