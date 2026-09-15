"""John Lewis adapter.

**Fetching.** Plain HTTP hangs rather than failing -- worse than a block,
because it looks like a slow network. Everything uses a browser.

**Discovery.** The sitemap shards are gzipped, and a browser asked to
navigate to a `.gz` starts a download instead of loading a page, so they
cannot be crawled. Discovery uses brand landing pages, which robots.txt
allows: `Allow: /brand/*/_/N-*`

Those need a code John Lewis assigns each brand
(`/brand/clinique/_/N-1z13ywm`). Codes come from the A-Z brand index in one
request and are cached on disk. Guessing one returns HTTP 404.

Two traps in that lookup, both of which made a stocked brand read as absent:
the slug can be percent-encoded (`est%C3%A9e-lauder`), and the catalogue is
deeper than one page -- John Lewis states `pagesAvailable` on the listing,
and the crawl uses that rather than a fixed depth.

**Product pages** carry a rich JSON-LD `Product`: `brand` as an explicit
Brand object, `category` as a real taxonomy path, and `offers` with sku,
price, currency and availability. A product sold in shades publishes a
`ProductGroup` instead, and is read as one product.

**Offers** start at the "Sale & Offers" page (/special-offers/c50000110) and
follow its /special-offers/ links. An offer listing's products are what its
offer is attached to.

The JSON-LD holds only the current price, so the was-price comes from the PDP
price block (`data-testid="price-prev"` / `"price-now"`). The recommendation
carousels use `product-card-price-prev` -- a different testid -- so scoping to
the former keeps a recommended product's discount off this one.

Sizes are priced separately and shown as "from £19.00", so
`extract_product_variants` emits one record per size.
"""

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

#: The A-Z of every brand John Lewis stocks. One request, ~3,956 brand pages.
_BRAND_INDEX = "https://www.johnlewis.com/brands/all"

#: A brand-page link on that index. The slug accepts percent-encoding because
#: John Lewis keeps accents in it: Estee Lauder is `est%C3%A9e-lauder`.
_BRAND_LINK_RE = re.compile(r"/brand/([A-Za-z0-9%-]+)/_/N-(\w+)")

#: John Lewis states the size of a brand listing in its own page state:
#: `"results":236,"pagesAvailable":10`. Crawl depth is taken from that rather
#: than from the framework's default, which stopped at four pages of ten.
_LISTING_SIZE_RE = re.compile(
    r'"results"\s*:\s*(\d+)\s*,\s*"pagesAvailable"\s*:\s*(\d+)'
)

#: The Next.js state blob. The per-size prices live here and nowhere in the
#: rendered page, which shows only "from £24.00".
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)

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

    # John Lewis rate-limits under sustained load. A seven-brand run had 144
    # of 348 requests refused with HTTP 403 -- all of them the last two
    # brands crawled, which then reported zero products for a brand the shop
    # actually stocks. One request at a time, more slowly, avoids it.
    #
    # Kept slightly above the old 2.5s now that a run covers the whole
    # catalogue rather than four pages per brand. John Lewis answers with
    # HTTP 404 rather than 429 when it throttles, which is indistinguishable
    # from a page that does not exist -- so the rate matters more here than
    # at a retailer whose refusals are honest.
    crawl_delay = 3.0
    max_concurrent_requests = 1

    # No sitemap route: the shards are gzipped and the browser will not
    # navigate to them. See the module docstring.
    sitemap_urls: List[str] = []

    # Category comes from the product's own JSON-LD, so a category filter is
    # honoured by discarding products whose stated category does not match --
    # no separate listing lookup is needed, unlike the other two retailers.
    supports_categories = True

    # prepare() resolves this retailer's brand page for each brand and
    # warns about the ones it cannot find, so an empty brand that DID
    # resolve is a fault rather than an absence.
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
        #: brand slug -> the product total its listing states on this run
        self._stated_results: Dict[str, int] = {}
        #: slugs of the brands asked for, so only their offers are followed
        self._wanted_slugs: List[str] = []
        #: offers page -> links from the Sale & Offers page (0 is that page)
        self._offer_depth: Dict[str, int] = {}

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

    #: Where John Lewis's own slug differs from the brand name we ask for.
    #: Its page is /brand/jo-malone-london/, so slugifying "Jo Malone" gives
    #: "jo-malone", which has no brand page and returns nothing. This cost 48
    #: products the moment the brand aliases started normalising "Jo Malone
    #: London" down to "Jo Malone".
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
        """The cache, with entries written by older versions upgraded.

        Entries used to be a bare code string, which assumed the brand's URL
        slug was our slug. That is false for Estee Lauder, whose page is
        `/brand/est%C3%A9e-lauder/_/N-1z13yah`, so the slug is now stored
        alongside the code. An old entry keeps working: its slug is ours.
        """
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
                # Older caches also hold page counts and category lists.
                # Those go stale and are read live from the listing instead.
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
        """Resolve and cache every requested brand's page code up front.

        This has to happen before the crawl: resolving a code needs its own
        browser session, and starting one inside the crawler's running event
        loop fails. Doing it here also means an unstocked brand is reported
        as a warning instead of silently contributing nothing.
        """
        # John Lewis's slug and the plain one: its Jo Malone pages say
        # jo-malone-london, an offer link may just say "Jo Malone"
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
        """The total John Lewis's listings stated during this run.

        It is what makes a soft-blocked crawl visible: a run that collects
        48 of the 234 Clinique products John Lewis says it has would
        otherwise report success. Read live from each brand's first listing
        page, never from the cache, whose counts go stale.
        """
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
        """Map each requested brand to its John Lewis brand page.

        Each value holds the page `code` and the `slug` John Lewis uses,
        which is not always ours -- Estee Lauder keeps its accent,
        percent-encoded.

        `resolve` decides whether a cache miss may hit the network, and
        defaults to False: resolving needs its own browser session, and
        starting one inside the running crawler raises "Playwright Sync API
        inside the asyncio loop". Only `prepare()` passes True, before the
        crawl starts.
        """
        cached = self._load_cached_codes()
        wanted = {self._brand_slug(b) for b in brands}

        if resolve:
            missing = wanted - set(cached)
            if missing:
                found = self._read_brand_index(missing)
                if not found:
                    # The A-Z page is the whole index in one request. Only
                    # fall back to walking gzipped sitemap shards if it fails.
                    found = self._scan_grid_sitemaps(missing)
                if found:
                    cached.update(found)
                    self._save_cached_codes(cached)

        return {slug: dict(cached[slug]) for slug in wanted if slug in cached}

    def _read_brand_index(self, wanted: Iterable[str]) -> Dict[str, Dict[str, str]]:
        """Resolve brand pages from John Lewis's A-Z index.

        One request returns every brand John Lewis stocks -- 3,956 of them --
        which replaced walking the gzipped grid sitemaps six shards at a time.
        That scan missed Estee Lauder twice over: its page is in a shard past
        the sixth of 199, and its slug is percent-encoded, which the shard
        scanner's pattern could not match. The brand reported zero products
        and zero requests, and read as "John Lewis does not stock it".
        """
        from scrapling.fetchers import StealthyFetcher

        wanted = set(wanted)
        try:
            response = StealthyFetcher.fetch(
                _BRAND_INDEX, headless=True, network_idle=True
            )
            html = getattr(response, "html_content", None) or str(response)
        except Exception:
            # Not fatal: the caller falls back to the sitemap scan.
            return {}

        found: Dict[str, Dict[str, str]] = {}
        for slug, code in _BRAND_LINK_RE.findall(html):
            key = self._fold_slug(slug)
            if key in wanted:
                found[key] = {"code": code, "slug": slug}
        return found

    @staticmethod
    def _fold_slug(slug: str) -> str:
        """A site slug reduced to the form `_brand_slug` produces.

        `est%C3%A9e-lauder` decodes to `estée-lauder`, which folds to
        `estee-lauder` -- the slug the caller asked for.
        """
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
        """The first page of the brand's listing. `more_listing_pages` adds the rest.

        The category is not applied here: John Lewis states it on the product
        page itself, so filtering happens at extraction instead of by choosing
        a different listing.
        """
        page = self.brand_pages([brand]).get(self._brand_slug(brand))
        if not page:
            return []
        # John Lewis's own slug, not ours: Estee Lauder's page is
        # /brand/est%C3%A9e-lauder/, and /brand/estee-lauder/ is a 404.
        return [ListingPage(
            url=f"https://{self.domain}/brand/{page['slug']}/_/N-{page['code']}",
            brand=brand,
        )]

    def more_listing_pages(self, response, meta: dict, added: int) -> List[str]:
        """Every other page of a brand listing, read off its first page.

        The first page states `"results":234,"pagesAvailable":4`. Pages hold
        72 products, and asking for the page after the last returns 404.
        Walking to `pagesAvailable` collected every product for all seven
        brands on 15 Sep 2026, e.g. Clinique 234 of 234 and Jo Malone 294 of
        294. The total is kept, so a run that falls short is reported.
        """
        requested = meta.get("url") or str(response.url)
        if "?" in requested:
            return []  # only the first page adds pages
        size = _LISTING_SIZE_RE.search(response.html_content or "")
        if not size:
            return []
        results, available = int(size.group(1)), int(size.group(2))
        if meta.get("brand"):
            self._stated_results[self._brand_slug(meta["brand"])] = results
        return [f"{requested}?page={n}" for n in range(2, available + 1)]

    def campaign_seed_urls(self, brands: Sequence[str]) -> List[str]:
        """None -- the brand listing already carries the campaigns.

        Deliberately empty. John Lewis states promotions on the brand landing
        page, which is the URL `product_listing_urls` already returns. Seeding
        it here would queue one URL under two callbacks; the crawler
        deduplicates by URL, so one silently never runs -- and when the
        campaign callback won, no products were queued at all.
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

        John Lewis links every shade separately at the same product id:

            /too-faced-cloud-crush-blush/tequila-sunset/p110055590
            /too-faced-cloud-crush-blush/pink-sunset/p110055590

        The crawler deduplicates on URL, so each shade was fetched and then
        collapsed to one record -- a dozen requests to keep one, which pushed
        a run into rate limiting (165 of 372 refused).

        The site's own URL is kept, never rewritten to a bare `<slug>/p<id>`:
        some products 404 without their shade segment.
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

    def extract_product_variants(
        self, response, target_brands: Sequence[str] = ()
    ) -> List[Product]:
        """One record per size, when John Lewis prices the sizes separately.

        The page state carries each size with its own price, stock code,
        availability and URL -- Dramatically Different Moisturising Lotion is
        50ml/125ml/200ml at 24.00/30.40/39.75 -- while the page itself shows
        only "from £24.00". Fifty-six products in a seven-brand run were
        recorded at a from-price, which is excluded from ranking, so John
        Lewis was absent from the comparison for every one of them.

        Everything except the size, price and stock code is inherited from
        the page's own product record, so the brand is still proved from the
        JSON-LD exactly as it is for a single-size product.
        """
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

            # `reductionHistory` is newest-first by `chronology`, so entry 0
            # is the price this one replaced.
            history = [
                h for h in (price.get("reductionHistory") or [])
                if isinstance(h, dict)
            ]
            history.sort(key=lambda h: h.get("chronology", 0))
            original = self._decimal(history[0].get("value")) if history else None
            if original is not None and original <= current:
                original = None

            path = (variant.get("pdpURL") or {}).get("url") or ""
            # Per-size, because the saving differs by size: the 50ml is
            # "Price matched: save £19" and the 100ml "save £25.60".
            variant_promo = " | ".join(
                self._promotional_titles(variant.get("messaging"))
            )
            available = (variant.get("availability") or {}).get("availableToOrder")

            products.append(replace(
                base,
                product_title=clean_text(variant.get("title")) or base.product_title,
                # Each size is a distinct purchasable item with its own
                # John Lewis stock code, so it gets its own id rather than
                # sharing the page's -- otherwise all three deduplicate to one.
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
                # A specific size has a specific price. That is the whole
                # point of splitting them out.
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
        """The page's variants, but only when they differ by SIZE.

        Shades are deliberately excluded: every other retailer lists a
        multi-shade product once, so expanding shades here would both inflate
        John Lewis's catalogue and stop those products matching anywhere else.
        """
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
        # Distinct sizes only. One size repeated is one product.
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
        """The promotional entries in a John Lewis `messaging` list.

        Entries are typed by John Lewis: `"type": "promotional"` marks an
        offer, and other types carry delivery and stock notes that are not
        promotions.
        """
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

        # John Lewis states its own promotions in the page data, typed
        # `"type": "promotional"`. A badge with no number ("Price matched")
        # never passed the keyword test that read the rendered page, so most
        # promoted products recorded no promotional copy at all.
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
            # A from-price covers several sizes, so this page is not a
            # single-variant listing and must not be validated as one.
            variant_count=None if price_is_from else (block.get("shade_count") or 1),
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

    def campaign_discovery_urls(self) -> List[str]:
        """John Lewis's "Sale & Offers" page, linked from its homepage.

        The two pages used before, /browse/offers/_/N-7ofb and
        /content/offers, list no offers any more: on 15 Sep 2026 the first
        stated 0 results. The rest are found from this page's links, see
        `offer_page_links`.
        """
        return ["https://www.johnlewis.com/special-offers/c50000110"]

    def extract_campaign_directory(self, response) -> List[Campaign]:
        """Every offer linked from an offers page, via the shared scan."""
        return self._scan_offer_links(response)

    def offer_page_links(self, response) -> List[str]:
        """Offers pages to read after this one, limited to the requested brands.

        Following every /special-offers/ link walked the whole shop: a run for
        Bobbi Brown and Too Faced spent 272 of 435 requests on furniture and
        other departments' offers, and John Lewis answered 403 from then on.
        Following every link from pages that list a requested brand was still
        too wide: inside beauty it walked every filtered view (lipsticks,
        foundations, perfumes), 80 pages with 95 more queued. Those views add
        no campaigns. A product's own offers come from its product page.

        So a link is followed when one of these holds:

          * this is the Sale & Offers page: every department's offers page
          * this is a department's offers page listing a requested brand's
            product: every offer it links, read once
          * the link names a requested brand, e.g. "15% off Bobbi Brown"

        An offer is read past its first page (?page=N, up to its stated
        `pagesAvailable`) only when its own URL names a requested brand, so
        all of its products are known.
        """
        url = str(response.url)
        page = url.split("?")[0].rstrip("/")
        hubs = {u.rstrip("/") for u in self.campaign_discovery_urls()}
        # unknown pages get the strictest rule; a redirect lands here too
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
        """True when a URL or link text names one of the requested brands.

        Accents and percent-encoding are folded first, so "Estée Lauder" and
        /est%C3%A9e-lauder/ both match estee-lauder. Whole words only, so MAC
        does not match /macadamia-oil/.
        """
        words = re.sub(r"[^a-z0-9]+", " ", " ".join(self._fold_slug(t) for t in texts))
        return any(re.search(rf"(?<![a-z0-9]){slug.replace('-', ' ')}(?![a-z0-9])", words)
                   for slug in self._wanted_slugs)

    def offer_page_products(self, response) -> List[str]:
        """Ids of the products an offers listing shows.

        John Lewis offers listings carry no recommendation carousels: the
        Bobbi Brown offer states 44 results and links exactly 44 products.
        """
        return [pid for pid in (self.product_id_from_url(u)
                                for u in self.extract_product_links(response)) if pid]

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

        # John Lewis labels its own promotions in the page state, typed
        # `"type": "promotional"`. Taking them from there rather than from the
        # rendered text means the retailer decides what counts as a promotion
        # instead of a keyword test guessing: "Price matched" names no
        # percentage or amount, so `is_confident_offer` rejected it, and 17 of
        # 24 Clinique products carrying that badge recorded no campaign at all
        # while Tom Ford's quantified "Price matched: save 15%" came through.
        #
        # Off a product page these are UNRESOLVED, never site-wide. They are
        # read from each tile's own `messaging` on a brand listing, so they
        # describe individual products -- "Price matched: save £58" is one
        # item's saving. Calling that site-wide attaches it to every product
        # in the run, which is how all 770 John Lewis records came to carry
        # seventeen promotions that belonged to a handful of them.
        for text in self._stated_promotions(response):
            if text in seen:
                continue
            seen.add(text)
            campaigns.append(
                Campaign(
                    retailer=self.display_name,
                    promotion_text=text,
                    promotion_type=classify_promotion(text),
                    scope=SCOPE_PRODUCT if on_product else SCOPE_UNRESOLVED,
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
            # A bare discount tier ("Save 12%") is a shop-by-saving filter
            # or a per-card badge, not a campaign. Recorded as one -- and
            # these scans have no scope to give it but site-wide -- it
            # attaches to every product found, which is how a product
            # discounted 29% came to carry "At Least 70% Off".
            if is_browse_facet(text, url):
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
        """The page's JSON-LD product, as one Product block.

        A product sold in shades is published as a `ProductGroup` instead:
        brand, name and category on the group, each shade's price and sku in
        `hasVariant`. It is read as one product, the shade on this page
        supplying the offer, because every other retailer lists a multi-shade
        product once. Reading only `Product` dropped every shade product --
        all six MAC products in a sample, and most makeup.
        """
        items = []
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
        """John Lewis hangs rather than serving a challenge page, so there is
        no marker to match on; a timeout surfaces as a request failure."""
        return False
