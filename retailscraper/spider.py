"""The generic crawler.

This spider contains no knowledge of any particular retailer. It drives a
`RetailerAdapter` through a fixed pipeline:

    sitemap -> candidate product URLs -> fetch -> adapter extraction
            -> validation -> deduplication -> collected results

and separately visits the adapter's campaign seed pages to pick up
promotions that exist at brand or site level.

Crawling, retries, throttling, robots.txt compliance and URL-level
deduplication are all provided by Scrapling's `SitemapSpider`; we do not
reimplement any of it.
"""

from __future__ import annotations

import re
from typing import Any, AsyncGenerator, Dict, List, Optional, Sequence

from scrapling.spiders import CrawlRule, LinkExtractor, Request, SitemapSpider

from .adapters.base import RetailerAdapter
from .models import Campaign, Product, RejectedRecord, SCOPE_SITEWIDE
from .validation import match_brand, validate


class RetailPromotionSpider(SitemapSpider):
    """Crawl one retailer for one set of brands."""

    name = "retail-promotions"

    # --- politeness -------------------------------------------------------
    # These defaults are deliberately conservative. The retailer is a live
    # commercial site and this job runs unattended; being slow is much cheaper
    # than being blocked. AutoThrottle adapts the delay to observed latency and
    # backs off on 429/503 responses.
    robots_txt_obey = True
    autothrottle_enabled = True
    autothrottle_start_delay = 2.0
    autothrottle_max_delay = 30.0
    concurrent_requests = 2
    concurrent_requests_per_domain = 2
    download_delay = 1.0

    def __init__(
        self,
        adapter: RetailerAdapter,
        brands: Sequence[str],
        max_products: Optional[int] = None,
        crawldir: Optional[str] = None,
        cache_dir: Optional[str] = None,
        max_listing_pages: int = 4,
        categories: Optional[Sequence[str]] = None,
        strict_brand: bool = False,
        resolve_categories: bool = False,
        campaigns_only: bool = False,
    ) -> None:
        """
        :param adapter: The retailer adapter supplying URLs and selectors.
        :param brands: Brand names to track, e.g. ["Clinique", "MAC"].
        :param max_products: Cap on product pages fetched PER BRAND. Useful for a
            quick demo run against a site with thousands of items.
        :param crawldir: Enables checkpointing, so an interrupted run resumes.
        :param cache_dir: When set, responses are cached on disk, which makes
            iterating on extraction rules free of extra requests.
        :param max_listing_pages: For retailers discovered through paginated
            listing pages rather than a sitemap, how many pages to walk per
            brand.
        :param resolve_categories: Also visit this retailer's category listing
            pages purely to learn which category each product is filed under.
            Neither retailer states a category on the product page, so on a
            sitemap-driven run this is the only way to populate the field.
            Costs one listing fetch per brand and category, so it is opt-in.
        """
        self.adapter = adapter
        self.brands = list(brands)
        self.max_products = max_products
        self.max_listing_pages = max_listing_pages
        self.categories = list(categories or [])
        self.strict_brand = strict_brand
        self.resolve_categories = resolve_categories
        self.campaigns_only = campaigns_only

        # These are class attributes on Spider/SitemapSpider; setting them on
        # the instance before super().__init__() lets one spider class serve
        # any retailer without subclassing per site.
        # A campaigns-only run wants the offer hubs and nothing else. Leaving
        # the sitemap in place would fetch the entire product catalogue to
        # produce records the caller did not ask for.
        self.sitemap_urls = [] if campaigns_only else list(adapter.sitemap_urls)
        self.allowed_domains = {adapter.domain}

        if cache_dir:
            self.development_mode = True
            self.development_cache_dir = cache_dir

        # Results, keyed so that duplicates collapse rather than accumulate.
        self.products: Dict[str, Product] = {}      # product_id -> Product
        self.campaigns: Dict[str, Campaign] = {}    # campaign_id -> Campaign
        self.rejected: List[RejectedRecord] = []

        # product_id -> category, learned from category listing pages. Applied
        # after the crawl rather than during it, because a product page may be
        # parsed before the listing that files it -- ordering the two would
        # mean serialising the crawl for no benefit.
        self.category_index: Dict[str, str] = {}

        self._dispatched = 0
        self._dispatched_by_brand: Dict[str, int] = {}

        super().__init__(crawldir=crawldir)

    # --- crawl entry points ----------------------------------------------

    def configure_sessions(self, manager) -> None:
        """Let the adapter decide how this retailer must be fetched.

        Lookfantastic is happy with plain HTTP; Boots sits behind bot
        protection and needs a real browser. Delegating here keeps that
        difference inside the adapter instead of leaking into the crawler.
        """
        self.adapter.configure_session(manager)

    async def start_requests(self) -> AsyncGenerator[Request, None]:
        """Seed the crawl with campaign pages, then whichever discovery route
        this retailer supports.

        Campaign pages go first for two reasons: they are cheap (one per
        brand) so a run interrupted early still produces something useful,
        and on sites behind bot protection the first navigation is what
        establishes the session the later requests depend on.

        Discovery is then either a sitemap or paginated listing pages,
        depending on what the retailer actually exposes.
        """
        # A retailer that challenges the first navigation of a session gets
        # one sacrificial request first, and everything real is chained behind
        # it. This applies to every run, not just campaigns-only: Boots used
        # to be warmed by whichever campaign seed happened to go out first,
        # which stopped being true the moment those seeds were removed.
        if self.adapter.warmup_url():
            yield Request(self.adapter.warmup_url(), callback=self.parse_warmup)
            return

        async for request in self._real_requests():
            yield request

    async def _real_requests(self) -> AsyncGenerator[Request, None]:
        """Everything the crawl actually needs, once any warmup has happened."""
        # Campaigns-only: scan the retailer's offer hubs and stop. No brands
        # were given, so there is nothing to look products up by.
        if self.campaigns_only:
            for url in self.adapter.campaign_discovery_urls():
                yield Request(url, callback=self.parse_campaign_directory)
            return

        for url in self.adapter.campaign_seed_urls(self.brands):
            yield Request(url, callback=self.parse_campaign_page)

        # Collect the listing pages this retailer offers for the requested
        # brands and categories. Whether they are used depends on the route.
        listing_pages = []
        for brand in self.brands:
            listing_pages.extend(
                self.adapter.product_listing_urls(
                    brand, self.categories, max_pages=self.max_listing_pages
                )
            )

        # Route 1: sitemap-driven. Skipped when a category filter is in play,
        # because a sitemap says nothing about categories -- crawling it would
        # return the whole brand and quietly ignore the filter.
        use_sitemap = bool(self.sitemap_urls) and not (
            self.categories and listing_pages
        )
        if use_sitemap:
            async for request in super().start_requests():
                yield request

            # The sitemap knows nothing about categories, so if the caller
            # wants them we visit the category listings purely as a lookup
            # table. These pages are read for the ids they list, not crawled
            # for products -- the sitemap already has better coverage.
            if self.resolve_categories:
                for page in self._category_index_pages():
                    yield Request(
                        page.url,
                        callback=self.parse_category_index,
                        sid=self.adapter.listing_session_id or "",
                        meta={
                            "brand": page.brand,
                            "category": page.category,
                            # Kept so the callback can tell whether the
                            # retailer redirected us off this category.
                            "requested_url": page.url,
                        },
                    )

        # Route 2: listing pages. The brand and category ride on the request
        # so products found there inherit both -- their URLs mention neither.
        for page in listing_pages:
            yield Request(
                page.url,
                callback=self.parse_listing_page,
                sid=self.adapter.listing_session_id or "",
                meta={"brand": page.brand, "category": page.category},
            )

    async def parse_listing_page(self, response) -> AsyncGenerator[Any, None]:
        """Harvest product links from a listing page and queue them.

        Campaigns are read here too: listing pages carry the brand-level
        promotional copy that never appears on a product page.
        """
        for campaign in self.adapter.extract_campaigns(response):
            self._record_campaign(campaign)

        meta = response.meta or {}
        inherited = {k: meta.get(k) for k in ("brand", "category") if meta.get(k)}

        # Some retailers publish the catalogue as data rather than as a grid
        # of links. When a listing carries complete products, take them and
        # do not also queue a page per product -- that would be hundreds of
        # requests for data already in hand.
        direct = self.adapter.extract_products_from_listing(response, meta.get("brand"))
        if direct:
            self.logger.info(
                f"{len(direct)} product(s) read directly from {response.url}"
            )
            for product in direct:
                self._accept_product(product, meta)
            return

        for url in self.adapter.extract_product_links(response, meta.get("brand")):
            request = Request(url, callback=self.parse_product_page,
                              meta=inherited or None)
            if self._enforce_product_cap(request, response) is not None:
                yield request

    def rules(self) -> List[CrawlRule]:
        """Dispatch sitemap URLs, keeping only plausible product pages.

        The URL filter is a cost control, not brand verification -- fetching
        all 16,000 products to find one brand's few hundred would be wasteful
        and rude. Anything that slips through is rejected later by
        `check_brand`, which uses the retailer's own product data.
        """
        if not self.sitemap_urls or self.campaigns_only or not self.brands:
            return []

        # Which URLs are products, and which belong to the requested brands,
        # are both questions only the adapter can answer. This used to build
        # a regex here -- "/p/<slug>/<digits>" -- which is Lookfantastic's URL
        # shape and nobody else's, so every other sitemap-driven retailer
        # matched nothing and reported zero products with no error.
        return [
            CrawlRule(
                link_extractor=LinkExtractor(allow_domains=[self.adapter.domain]),
                callback=self.parse_product_page,
                process_request=self._filter_and_cap,
            )
        ]

    def _filter_and_cap(self, request: Request, response) -> Optional[Request]:
        """Drop URLs the adapter does not recognise, then apply the cap.

        `select_candidates` is the adapter's own cheap pre-filter: it decides
        what looks like a product for one of the requested brands. It is
        allowed to over-include -- the brand is still proved from the page.
        """
        if not self.adapter.select_candidates([request.url], self.brands):
            return None
        return self._enforce_product_cap(request, response)

    def _enforce_product_cap(self, request: Request, _response) -> Optional[Request]:
        """Drop product requests once a brand has had its share.

        The cap is per brand, not per run. Sitemaps are ordered by the
        retailer's own product ids, so a single global cap is exhausted by
        whichever brand happens to appear first -- asking for three brands
        would return three brands' worth of one brand.
        """
        # Prefer the brand the request already carries (listing route, where
        # we know it for certain) over guessing from the URL (sitemap route).
        brand = (request.meta or {}).get("brand") or self._brand_for_url(request.url)
        seen = self._dispatched_by_brand.get(brand, 0)

        if self.max_products is not None and seen >= self.max_products:
            return None

        self._dispatched_by_brand[brand] = seen + 1
        self._dispatched += 1
        return request

    def _brand_for_url(self, url: str) -> str:
        """Which requested brand a candidate URL appears to belong to.

        Slug-based and approximate, which is fine: this only decides how the
        crawl budget is shared out. The authoritative brand still comes from
        the product page.
        """
        lowered = url.lower()
        for brand in self.brands:
            if self.adapter._brand_slug(brand) in lowered:
                return brand
        return "_unmatched"

    # --- callbacks --------------------------------------------------------

    async def parse(self, response) -> AsyncGenerator[Any, None]:
        """Fallback callback; every URL we care about has an explicit one."""
        self.logger.debug(f"No specific handler for {response.url}")
        return
        yield  # pragma: no cover - keeps this an async generator

    def _category_index_pages(self) -> List[Any]:
        """Category listing pages to consult purely as a category lookup.

        Uses the caller's `--categories` when given, otherwise everything the
        adapter knows how to look up, so a plain run still gets the field
        filled in.
        """
        wanted = self.categories or self.adapter.known_categories()
        if not wanted:
            return []

        pages = []
        for brand in self.brands:
            pages.extend(
                self.adapter.product_listing_urls(
                    brand, wanted, max_pages=self.max_listing_pages
                )
            )
        return pages

    @staticmethod
    def _listing_still_matches(requested_url: str, final_url: str) -> bool:
        """Did the category listing we asked for survive the redirect?

        Retailers redirect a category a brand does not stock to the brand's
        full range -- Lookfantastic sends /c/brands/clinique/fragrance/ to
        /c/brands/clinique/view-all/. Trusting the landing page would file
        every Clinique product as a fragrance. The final path segment naming
        the category has to still be there.
        """
        requested_slug = requested_url.rstrip("/").rsplit("/", 1)[-1].lower()
        return requested_slug in final_url.rstrip("/").lower()

    async def parse_category_index(self, response) -> AsyncGenerator[Any, None]:
        """Record which category a listing page files its products under.

        Deliberately does not queue the products it finds: this page is being
        read as a lookup table, and the sitemap route has already discovered
        the catalogue more completely than any listing page could.
        """
        meta = response.meta or {}
        category = meta.get("category")
        if not category:
            return

        requested = meta.get("requested_url") or ""
        if requested and not self._listing_still_matches(requested, response.url):
            # Redirected away from the category we asked for. Whatever is on
            # the landing page is not that category, so record nothing.
            self.logger.info(
                f"category index: skipping '{category}' -- {requested} redirected "
                f"to {response.url}, so the listing is no longer category-scoped"
            )
            return

        found = 0
        for url in self.adapter.extract_product_links(response, meta.get("brand")):
            product_id = self.adapter.product_id_from_url(url)
            # First category wins. A product legitimately sits in several
            # (a tinted moisturiser is skincare and makeup); picking the first
            # keeps the field single-valued and the choice deterministic,
            # since the pages are visited in a fixed order.
            if product_id and product_id not in self.category_index:
                self.category_index[product_id] = category
                found += 1

        self.logger.info(
            f"category index: {found} product(s) filed under '{category}' "
            f"from {response.url}"
        )
        return
        yield  # pragma: no cover - keeps this an async generator

    def apply_category_index(self) -> int:
        """Fill in categories learned from listing pages. Returns how many.

        Runs after the crawl so it does not matter whether a product page or
        the listing that files it was fetched first. Only fills blanks: a
        category that came from the listing route is already authoritative.
        """
        filled = 0
        for product in self.products.values():
            if product.category:
                continue
            category = self.category_index.get(product.product_id)
            if category:
                product.category = category
                filled += 1
        return filled

    async def parse_warmup(self, response) -> AsyncGenerator[Any, None]:
        """Discard the warmup response and queue the pages we actually want.

        The response is expected to be a bot-protection challenge, so nothing
        is read from it. Its only job was to establish the session.
        """
        self.logger.info(
            f"session warmed via {response.url} "
            f"({len(response.body)} bytes, content discarded)"
        )
        async for request in self._real_requests():
            yield request

    async def parse_campaign_directory(self, response) -> AsyncGenerator[Any, None]:
        """Collect every campaign advertised on an offers hub.

        Two passes, because they read different things: the directory scan
        walks the page's offer links, while the normal campaign extraction
        picks up the header strip banner that appears on every page.
        """
        for campaign in self.adapter.extract_campaign_directory(response):
            self._record_campaign(campaign)
        for campaign in self.adapter.extract_campaigns(response):
            self._record_campaign(campaign)

        self.logger.info(
            f"campaign directory: {len(self.campaigns)} campaign(s) known "
            f"after {response.url}"
        )
        return
        yield  # pragma: no cover - keeps this an async generator

    async def parse_campaign_page(self, response) -> AsyncGenerator[Any, None]:
        """Collect promotions from a brand or category landing page."""
        for campaign in self.adapter.extract_campaigns(response):
            self._record_campaign(campaign)
        return
        yield  # pragma: no cover

    async def parse_product_page(self, response) -> AsyncGenerator[Any, None]:
        """Extract, validate and store one product."""
        # Campaigns are collected from product pages too: the site-wide strip
        # banner is visible here, and the product's own offer only exists here.
        for campaign in self.adapter.extract_campaigns(response):
            self._record_campaign(campaign)

        product = self.adapter.extract_product(response, self.brands)
        if product is None:
            self.logger.debug(f"Not a product page: {response.url}")
            return

        self._accept_product(product, response.meta or {})
        return
        yield  # pragma: no cover

    def _accept_product(self, product, meta: Dict[str, Any]) -> None:
        """Validate, attribute and store one product.

        Shared by both discovery routes -- a product read from a page and one
        read straight out of a listing get identical treatment. Keeping this
        in one place is what stops the two paths drifting into different
        validation rules.
        """
        rejection = validate(product, self.brands, strict_brand=self.strict_brand)
        if rejection is not None:
            # Rejections are kept, never silently dropped -- they are how we
            # find out that a page changed or that extraction drifted.
            self.rejected.append(rejection)
            self.logger.info(
                f"REJECTED reason={rejection.reason} url={rejection.source_url} "
                f"detail={rejection.detail}"
            )
            return

        # Record which requested brand this maps to, so "Jo Malone London" at
        # one retailer and "Jo Malone" at another group together downstream.
        product.brand_matched_to = match_brand(product.brand, self.brands)

        # The category comes from the listing page this product was found on,
        # i.e. the retailer's own filing, never a guess from the title.
        #
        # Only fill a blank. Some retailers state the category on the product
        # page itself (John Lewis puts it in its JSON-LD), and that is the
        # better source -- overwriting it with the route's category would
        # replace a stated fact with None on every John Lewis record.
        route_category = meta.get("category")
        if route_category and not product.category:
            product.category = route_category

        # Link this product to the promotions that actually apply to it: its
        # own offer, plus any site-wide campaign running at the same time.
        product.applied_campaigns = self._campaigns_for(product)

        # Deduplicate on the retailer's product id, so the same item reached
        # through several URLs collapses into one record.
        existing = self.products.get(product.product_id)
        if existing is None:
            self.products[product.product_id] = product
        else:
            self.logger.debug(
                f"Duplicate product {product.product_id}: keeping {existing.product_url}, "
                f"discarding {product.product_url}"
            )

    # --- helpers ----------------------------------------------------------

    def _record_campaign(self, campaign: Campaign) -> None:
        """Store a campaign once, regardless of how many pages showed it."""
        if campaign.campaign_id not in self.campaigns:
            self.campaigns[campaign.campaign_id] = campaign

    def _campaigns_for(self, product: Product) -> List[str]:
        """Ids of the campaigns that apply to this product.

        Only two things qualify: a campaign whose text matches the offer shown
        on the product's own page, and site-wide campaigns, which by
        definition apply everywhere. We never attach a brand or category
        campaign to a product just because we saw it during the same run.
        """
        applicable = []
        for campaign in self.campaigns.values():
            if campaign.scope == SCOPE_SITEWIDE:
                applicable.append(campaign.campaign_id)
            elif product.promotional_copy and campaign.promotion_text == product.promotional_copy:
                applicable.append(campaign.campaign_id)
        return applicable
