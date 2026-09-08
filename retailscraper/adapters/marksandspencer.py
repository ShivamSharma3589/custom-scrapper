"""Marks & Spencer adapter.

**Search is off-limits** -- robots.txt disallows `/*search?q=`.

**The sitemap is not the catalogue.** Discovery used to filter sitemap URLs
whose slug named the brand, which was wrong twice over: M&S does not put the
brand in most slugs, and across all nine UK product sitemaps (29,772 URLs)
only 19 mention Clinique while M&S's own Clinique page lists 168. Both errors
pointed the same way -- reporting products M&S sells as products it does not.

Discovery is therefore the brand landing page, `/l/beauty/<slug>`, paginated
with `?page=N`. A brand M&S genuinely does not stock 404s there, which is a
real answer rather than an empty result that looks like one.

**The listing IS the data.** Each landing page embeds `__NEXT_DATA__` with 48
complete products, so a brand costs three or four requests instead of one per
product. `extract_product` still reads a single page for anything arriving by
URL. Plain HTTP throughout -- no browser needed.

**A caveat about titles.** M&S publishes some corrupted names --
"Multi-DiClinique For Menional" for "Multi-Dimensional" -- in their `<title>`,
JSON-LD and listing JSON alike. That is their defect, and it is recorded
verbatim: guessing what a retailer meant to publish is not this scraper's job.

Product pages carry JSON-LD `Product` (brand, sku, price, availability). The
was-price is not in it -- `"previousPrice"` comes from the embedded state.
"""

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlsplit

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

# Product URLs end in /p/<code>, e.g. /clinique-for-men-cream-shave-125ml/p/hbp60562352
_PRODUCT_URL_RE = re.compile(
    r"^https?://(?:www\.)?marksandspencer\.com/[^?#]*/p/([a-z0-9]+)/?$", re.I
)

# The was-price, which the JSON-LD does not carry.
_PREVIOUS_PRICE_RE = re.compile(r'"previousPrice"\s*:\s*"?(\d+(?:\.\d+)?)"?')

# The Next.js state blob every M&S page embeds. The listing route reads its
# product array rather than the rendered grid, because the grid is assembled
# client-side and a plain HTTP fetch never sees it.
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)

# Where the products sit inside that blob.
_RESULTS_PATH = (
    "props", "pageProps", "serverSideGqlResponseFed",
    "productPageData", "search", "results",
)

# M&S serves 48 per page and ignores every other paging parameter we tried
# (`pg`, `start`, `offset` all silently return page one).
_LISTING_PAGE_SIZE = 48

# A product code with its letter prefix stripped: "P60745720" -> "60745720",
# which is the number M&S puts in the JSON-LD `sku` on the product page.
_ID_PREFIX_RE = re.compile(r"^[a-z]+", re.IGNORECASE)

# `prepare` runs before the crawler exists, so it uses urllib rather than the
# session the crawler will build. M&S serves a challenge page to an obviously
# scripted agent, and a challenge is not a 404 -- the distinction this probe
# is here to draw.
_PROBE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


@register
class MarksAndSpencerAdapter(RetailerAdapter):
    """Extraction rules for marksandspencer.com."""

    name = "marksandspencer"
    domain = "www.marksandspencer.com"
    display_name = "Marks & Spencer"

    #: Empty on purpose. The beauty sitemap holds 19 of the 168 Clinique
    #: products M&S actually sells and names no other ELC brand, so crawling
    #: it alongside the brand pages would add requests and no products. See
    #: the module docstring.
    sitemap_urls: List[str] = []

    # M&S offers category facets under the brand page (/l/beauty/clinique/fs5/
    # anti-ageing), but the facet vocabulary is M&S's own and differs per
    # brand, so a `--categories` filter cannot be honoured faithfully. The
    # listing does state each product's own type, which is recorded.
    supports_categories = False

    # prepare() resolves this retailer's brand page for each brand and
    # warns about the ones it cannot find, so an empty brand that DID
    # resolve is a fault rather than an absence.
    confirms_brand_stocking = True

    #: Brands whose landing-page slug is not the obvious slugification of
    #: their name. Accented spellings are listed because the slug rule strips
    #: accents to nothing ("estée lauder" -> "est-e-lauder").
    BRAND_SLUG_OVERRIDES = {
        "estée lauder": "estee-lauder",
        "esteé lauder": "estee-lauder",
        "jo malone": "jo-malone-london",
        "jo malone london": "jo-malone-london",
    }

    def configure_session(self, manager) -> None:
        """Plain HTTP: product pages carry full JSON-LD without a browser."""
        from scrapling.fetchers import FetcherSession

        manager.add("default", FetcherSession())

    # --- discovery --------------------------------------------------------

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
        """The brand's landing page, paginated.

        `categories` is accepted and ignored, as `supports_categories`
        declares. Page one carries no parameter because `?page=1` and the bare
        URL return the same 48 products; asking past the last page returns an
        empty product array, which simply yields nothing.
        """
        slug = self._brand_slug(brand)
        base = f"https://{self.domain}/l/beauty/{slug}"
        return [
            ListingPage(
                url=base if page == 1 else f"{base}?page={page}",
                brand=brand,
                category=None,
            )
            for page in range(1, max_pages + 1)
        ]

    def prepare(self, brands: Sequence[str]) -> List[str]:
        """Check each brand has a landing page before the crawl starts.

        M&S 404s the page for a brand it does not carry, so this separates
        "we found nothing" from "there is nothing to find" -- the ambiguity
        that made five genuinely-unstocked brands and one badly-missed brand
        look identical in the old sitemap-driven output.

        Failures here are warnings, never fatal: a 404 costs the caller
        nothing but an explanation, and a network hiccup must not stop a run.
        """
        import urllib.error
        import urllib.request

        warnings: List[str] = []
        for brand in brands:
            url = f"https://{self.domain}/l/beauty/{self._brand_slug(brand)}"
            request = urllib.request.Request(
                url, method="HEAD", headers={"User-Agent": _PROBE_USER_AGENT}
            )
            try:
                urllib.request.urlopen(request, timeout=30).close()
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    warnings.append(
                        f"{self.display_name} has no landing page for "
                        f"'{brand}' ({url}) -- it does not stock this brand"
                    )
                else:
                    warnings.append(
                        f"{self.display_name} returned HTTP {exc.code} for "
                        f"'{brand}' ({url})"
                    )
            except Exception:
                # Unreachable is not the same as absent. Say nothing and let
                # the crawl try for itself.
                pass
        return warnings

    def is_product_url(self, url: str) -> bool:
        return bool(_PRODUCT_URL_RE.match(url.split("?")[0]))

    def product_id_from_url(self, url: str) -> Optional[str]:
        match = _PRODUCT_URL_RE.match(url.split("?")[0])
        return match.group(1) if match else None

    def select_candidates(self, urls: Iterable[str], brands: Sequence[str]) -> List[str]:
        """Keep sitemap URLs whose slug mentions a requested brand.

        A cheap pre-filter only: M&S puts the brand at the start of the slug,
        which is accurate enough to avoid fetching 1,756 beauty products to
        find 19. The brand is still proved from each page's JSON-LD.
        """
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

    # --- extraction -------------------------------------------------------

    def extract_products_from_listing(
        self, response, brand: Optional[str] = None
    ) -> List[Product]:
        """Read a whole brand landing page out of its embedded state.

        Returns complete products, so the crawler will not also queue a fetch
        per product -- 168 pages saved on Clinique alone.

        The brand is taken from each record's own `brand` field, never from
        the page we asked for. A landing page is M&S's answer to "show me this
        brand", but it is still their answer, and a record claiming another
        brand is rejected downstream rather than relabelled.
        """
        products: List[Product] = []
        now = datetime.now(timezone.utc).isoformat()

        for record in self._listing_records(response):
            if not isinstance(record, dict):
                continue

            # The seoPath carries colour and image parameters that identify
            # one swatch rather than the product, and `canonical_url` keeps
            # unknown query parameters on purpose, so they are dropped here.
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
                # `stockLevelIndicator` reads "M" on every record we have
                # seen, so its vocabulary is unknown. Guessing "InStock" from
                # a code we cannot decode would be inventing the field.
                availability=None,
                category=clean_text(record.get("productDefinition")),
                variant_count=len(variants) if isinstance(variants, list) else None,
                # As on the product page: "hbp60745720" and "60745720" are
                # related but not equal.
                sku_matches_product_id=False,
                promotional_copy=self._listing_promo(record),
                scraped_at=now,
            ))

        return products

    @classmethod
    def _listing_records(cls, response) -> List[Any]:
        """The product array inside the page's `__NEXT_DATA__`, or empty."""
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

        for key in _RESULTS_PATH:
            if not isinstance(node, dict):
                return []
            node = node.get(key)
        products = (node or {}).get("products") if isinstance(node, dict) else None
        return products if isinstance(products, list) else []

    @classmethod
    def _listing_price(cls, list_price: Any) -> tuple:
        """(price, is_from) for a listing record's `listPrice`.

        M&S quotes a single amount for most products and a min/max range for
        those sold in several shades at different prices -- Anti-Blemish
        Solutions Liquid Makeup spans 26.25 to 26.95 across 22 variants. The
        range's floor is flagged as a from-price so it is never ranked against
        another retailer's single-size price.
        """
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
        """The promotional badge M&S prints on the product's tile.

        These are the per-product half of the campaign picture: "20% off" on
        117 Clinique products and "30% off" on 51 of them is exactly the
        multi-tier promotion the business wants to see.
        """
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
        # A genuine spread means several sizes behind one page, so the quoted
        # figure is a from-price rather than the product's price.
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
            # The product code in the URL ("hbp60562352") and the SKU
            # ("60562352") are related but not equal, so comparing them would
            # reject every record.
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

    # --- campaigns --------------------------------------------------------

    #: The page treated as this retailer's offers hub. Every M&S page carries
    #: the offer menu, so one has to be nominated; the beauty hub is the
    #: natural choice and is not a listing for any brand.
    CAMPAIGN_HUB_PATH = "/l/beauty"

    #: M&S 301s `/l/beauty` to `/c/beauty`, so the page that arrives is not
    #: the page that was asked for. Recognising only the requested path meant
    #: the hub was never identified and the offer menu was never read.
    CAMPAIGN_HUB_PATHS = ("/l/beauty", "/c/beauty")

    def _is_campaign_hub(self, url: str) -> bool:
        path = urlsplit(url).path.rstrip("/") or "/"
        return path in self.CAMPAIGN_HUB_PATHS

    def campaign_discovery_urls(self) -> List[str]:
        """One page is enough: M&S ships its whole offer menu in every nav."""
        return [f"https://{self.domain}{self.CAMPAIGN_HUB_PATH}"]

    def campaign_seed_urls(self, brands: Sequence[str]) -> List[str]:
        """Visit the beauty hub on a brand run too, for the same menu.

        Deliberately not a brand landing page. Those are already queued as
        listings, and a URL wearing two callbacks gets deduplicated down to
        one -- which silently cost Boots its product listing once already.
        """
        return [f"https://{self.domain}{self.CAMPAIGN_HUB_PATH}"]

    def extract_campaign_directory(self, response) -> List[Campaign]:
        """Every promotion M&S advertises, read out of its navigation.

        M&S does not have an offers hub in the sense Boots and Lookfantastic
        do. What it has is a navigation tree, embedded as data on every page,
        whose entries are named for the promotions currently running: "20% off
        Clinique", "30% off Makeup", "Up to 30% off Beauty".

        Scope is resolved from M&S's own classification rather than guessed at
        from the URL. The nav has a "Brands" section listing which pages are
        brand pages, so `/l/beauty/clinique` is known to be a brand and
        `/l/beauty/skincare` is not -- the exact `/no7/` vs `/fragrance/`
        ambiguity that Boots has to leave unresolved.
        """
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
        """Path -> brand name, for every page M&S files under "Brands".

        This is M&S stating which of its landing pages are brands, which is
        what makes `SCOPE_BRAND` here an observation rather than a guess.
        """
        index: Dict[str, str] = {}
        for category in cls._nav_root(response):
            if not isinstance(category, dict) or category.get("name") != "Brands":
                continue

            def collect(node: Any) -> None:
                if isinstance(node, dict):
                    name = clean_text(node.get("name"))
                    url = node.get("url")
                    path = cls._nav_path(url) if isinstance(url, str) else None
                    # Only leaves are brands: the groupings above them
                    # ("Beauty Brands") carry no url or point at /c/brands.
                    # The "Top Brand Offers" grouping sits in the same section
                    # but its leaves are promotions, not brands -- indexing
                    # those made "Up to 30% off Beauty" its own brand.
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
            # `/l/beauty/skincare/fs5/cleanser` is the cleanser facet OF
            # skincare, so the department is what the promotion covers.
            segments = [s for s in path[len("/l/"):].split("/") if s]
            if "fs5" in segments:
                segments = segments[: segments.index("fs5")]
            if segments:
                return SCOPE_CATEGORY, segments[-1]
        return SCOPE_UNRESOLVED, None

    def extract_campaigns(self, response) -> List[Campaign]:
        """Offers stated on the page.

        Anything on a product page is scoped to that product; anything
        elsewhere is left sitewide, because M&S states no brand or category
        reach in its promotional copy.

        On the beauty hub -- and only there -- this also reads the offer menu
        out of the navigation. The menu is present on every M&S page, so
        reading it everywhere would staple the retailer's entire promotion
        list onto every product it found. The hub is the one page whose
        purpose is that menu, and it is seeded for exactly this.
        """
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
