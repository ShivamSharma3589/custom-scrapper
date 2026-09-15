"""The generic crawler.

Knows nothing about any particular retailer. It drives a `RetailerAdapter`
through a fixed pipeline:

    sitemap -> candidate URLs -> fetch -> adapter extraction
            -> validation -> deduplication -> results

and separately visits the adapter's campaign pages for brand and site-wide
promotions.

Crawling, retries, throttling, robots.txt and URL deduplication all come
from Scrapling's `SitemapSpider` -- none of it is reimplemented here.
"""

import re
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Sequence

from scrapling.spiders import CrawlRule, LinkExtractor, Request, SitemapSpider

from .adapters.base import RetailerAdapter
from .models import Campaign, Product, RejectedRecord, SCOPE_PRODUCT, SCOPE_SITEWIDE
from .validation import match_brand, validate


class RetailPromotionSpider(SitemapSpider):
    """Crawl one retailer for one set of brands."""

    name = "retail-promotions"

    # --- politeness -------------------------------------------------------
    # deliberately slow: these are live commercial sites and the job runs
    # unattended, so being slow beats being blocked
    robots_txt_obey = True
    autothrottle_enabled = True
    autothrottle_start_delay = 2.0
    autothrottle_max_delay = 30.0
    concurrent_requests = 2
    concurrent_requests_per_domain = 2
    download_delay = 1.0

    #: Refusals in a row after which the crawl moves to a backup session.
    SWITCH_AFTER_REFUSALS = 3

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
        on_product: Optional[Callable[[Any], None]] = None,
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

        # set on the instance before super().__init__() so one spider class
        # serves every retailer. A campaigns-only run drops the sitemap, or
        # it would crawl the whole catalogue for records nobody asked for.
        self.sitemap_urls = [] if campaigns_only else list(adapter.sitemap_urls)
        self.allowed_domains = {adapter.domain}

        # set before super().__init__() so the engine picks it up
        if adapter.crawl_delay is not None:
            self.download_delay = adapter.crawl_delay
            self.autothrottle_start_delay = max(
                self.autothrottle_start_delay, adapter.crawl_delay
            )
        if adapter.max_concurrent_requests is not None:
            self.concurrent_requests = adapter.max_concurrent_requests
            self.concurrent_requests_per_domain = adapter.max_concurrent_requests

        if cache_dir:
            self.development_mode = True
            self.development_cache_dir = cache_dir

        self.products: Dict[str, Product] = {}      # product_id -> Product
        self.campaigns: Dict[str, Campaign] = {}    # campaign_id -> Campaign
        self.rejected: List[RejectedRecord] = []

        # applied after the crawl: a product page may be parsed before the
        # listing that files it
        self.category_index: Dict[str, str] = {}

        # without this, a rate-limited brand is indistinguishable from one
        # the shop does not stock -- a wrong business conclusion
        self.blocked_by_brand: Dict[str, int] = {}
        #: Called with each newly accepted product, so a caller can save as
        #: the crawl goes rather than only at the end.
        self.on_product = on_product

        self._dispatched = 0
        self._dispatched_by_brand: Dict[str, int] = {}

        # product links already seen on each list, so paging stops at the
        # first page that adds nothing new. Keyed by the list URL without
        # its page number.
        self._list_links: Dict[str, set] = {}

        # product id -> ids of the offers its own page showed, so a product
        # carrying two offers gets both, not neither
        self._page_offers: Dict[str, List[str]] = {}

        # offers page (URL without its page number) -> ids of the products it
        # lists. A campaign landing on that page applies to exactly these.
        self.offer_members: Dict[str, set] = {}

        # refusals since the last page that loaded, and the sessions given up
        # on because of them, in order
        self._refused_in_a_row = 0
        self.switched_sessions: List[str] = []
        #: which browser is serving requests now: "default", then "proxy1" ...
        self._active_session = "default"

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
        # one sacrificial request first for retailers that challenge a cold
        # session, with everything real chained behind it
        if self.adapter.warmup_url():
            yield Request(self.adapter.warmup_url(), callback=self.parse_warmup)
            return

        async for request in self._real_requests():
            yield request

    async def _real_requests(self) -> AsyncGenerator[Request, None]:
        """Everything the crawl actually needs, once any warmup has happened."""
        # campaigns-only: scan the offer hubs and stop
        if self.campaigns_only:
            for url in self.adapter.campaign_discovery_urls():
                yield Request(url, callback=self.parse_campaign_directory)
            return

        # the offers hub on every run, not just campaigns-only -- otherwise
        # campaigns.json is a sample of what the crawl happened to pass
        seeds = list(self.adapter.campaign_seed_urls(self.brands))
        for url in self.adapter.campaign_discovery_urls():
            if url not in seeds:
                yield Request(url, callback=self.parse_campaign_directory)

        for url in seeds:
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
            yield self._listing_request(page.url, page.brand, page.category)

    def _listing_request(self, url: str, brand, category) -> Request:
        """A request for one listing page.

        The requested URL rides along in meta because `response.url` can lose
        it: Boots redirects /tom-ford to /tom-ford-, and drops ?paging.index
        from the address it reports.
        """
        return Request(
            url,
            callback=self.parse_listing_page,
            sid=self.adapter.listing_session_id or "",
            meta={"brand": brand, "category": category, "url": url},
        )

    async def parse_listing_page(self, response) -> AsyncGenerator[Any, None]:
        """Harvest product links from a listing page and queue them.

        Campaigns are read here too: listing pages carry the brand-level
        promotional copy that never appears on a product page.
        """
        listing_campaigns = self.adapter.extract_campaigns(response)
        for campaign in listing_campaigns:
            self._record_campaign(campaign)

        meta = response.meta or {}
        inherited = {k: meta.get(k) for k in ("brand", "category") if meta.get(k)}

        # Some retailers publish the catalogue as data rather than as a grid
        # of links. When a listing carries complete products, take them and
        # do not also queue a page per product -- that would be hundreds of
        # requests for data already in hand. Only the pages the adapter still
        # needs are queued.
        direct = self.adapter.extract_products_from_listing(response, meta.get("brand"))
        if direct:
            self.logger.info(
                f"{len(direct)} product(s) read directly from {response.url}"
            )
            # a tile's own offers are the listing campaigns with its wording
            by_text = {c.promotion_text: c.campaign_id for c in listing_campaigns}
            for product in direct:
                own = [by_text[part] for part in (product.promotional_copy or "").split(" | ")
                       if part in by_text]
                self._accept_product(product, {**meta, "page_offers": own})
            links = self.adapter.product_pages_to_read(response, meta.get("brand"))
            listed = [p.product_id for p in direct] + links
        else:
            links = self.adapter.extract_product_links(response, meta.get("brand"))
            listed = links

        for url in links:
            request = Request(url, callback=self.parse_product_page,
                              meta=inherited or None)
            if self._enforce_product_cap(request, response) is not None:
                yield request

        # count what this page added to its list, then let the adapter decide
        # what comes next: lists a brand page links to, or the next page
        list_url = str(meta.get("url") or response.url).split("?")[0]
        seen = self._list_links.setdefault(list_url, set())
        added = len(set(listed) - seen)
        seen.update(listed)

        for url in self.adapter.more_listing_pages(response, meta, added):
            yield self._listing_request(url, meta.get("brand"), meta.get("category"))

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

    async def is_blocked(self, response) -> bool:
        """Note which brand a refused request belonged to, then defer.

        The crawler already retries and backs off; this only records the
        attribution so the run summary can tell "blocked" apart from "not
        stocked".
        """
        blocked = await super().is_blocked(response)
        if blocked:
            brand = self._brand_for_url(str(response.url))
            self.blocked_by_brand[brand] = self.blocked_by_brand.get(brand, 0) + 1
        self._refused_in_a_row = self._refused_in_a_row + 1 if blocked else 0
        return blocked

    async def retry_blocked_request(self, request: Request, response) -> Request:
        """Move the crawl to the adapter's next session once the current one
        is refused several times in a row.

        One refusal can be a blip. A run of them is the retailer blocking
        this IP: John Lewis answered every request with 403 from its 153rd
        page load onwards.

        The backup browser is started and put in place of the refused one
        under the same session id. That id matters: the crawler stamps it on
        every request as it is queued, so dropping the refused session instead
        left hundreds of queued requests pointing at a session that no longer
        existed, and each one failed silently.
        """
        manager = self._session_manager
        current = manager.default_session_id
        if self._refused_in_a_row < self.SWITCH_AFTER_REFUSALS or (request.sid or current) != current:
            return request

        for backup_id in [sid for sid in self.adapter.backup_session_ids if sid in manager.session_ids]:
            backup = manager.pop(backup_id)
            try:
                await backup.__aenter__()
            except Exception as exc:
                self.logger.warning(f"backup session {backup_id!r} would not start, trying the next: {exc}")
                continue

            retired = manager.pop(current)
            manager.add(current, backup, default=True)
            self._refused_in_a_row = 0
            self.switched_sessions.append(self._active_session)
            self.logger.warning(
                f"session {self._active_session!r} refused {self.SWITCH_AFTER_REFUSALS} requests "
                f"in a row; moving to {backup_id!r}"
            )
            self._active_session = backup_id
            try:
                await retired.close()
            except Exception as exc:  # pragma: no cover - closing is best effort
                self.logger.debug(f"closing the refused session: {exc}")
            break
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

        # the products this offers page lists, so its offer can be attached
        # to exactly those. Every page of a paged offer adds to the same set.
        members = self.adapter.offer_page_products(response)
        if members:
            page = self.adapter.offer_page_of(str(response.url))
            self.offer_members.setdefault(_page_key(page), set()).update(members)

        self.logger.info(
            f"campaign directory: {len(self.campaigns)} campaign(s) known "
            f"after {response.url} ({len(members)} product(s) listed)"
        )

        # other offers pages this one links to. The crawler skips any URL it
        # has already fetched, so pages linking to each other do not loop.
        for url in self.adapter.offer_page_links(response):
            yield Request(url, callback=self.parse_campaign_directory)

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
        page_offers = []
        for campaign in self.adapter.extract_campaigns(response):
            self._record_campaign(campaign)
            if campaign.scope == SCOPE_PRODUCT:
                page_offers.append(campaign.campaign_id)

        # Some pages sell several sizes at different prices behind one URL.
        # Where the adapter can read them, each size is its own record: the
        # alternative is a single "from" price that is not the price of any
        # specific item and cannot be compared with another retailer's.
        meta = {**(response.meta or {}), "page_offers": page_offers}
        variants = self.adapter.extract_product_variants(response, self.brands)
        if variants:
            self.logger.info(
                f"{len(variants)} size(s) read from {response.url}"
            )
            for variant in variants:
                self._accept_product(variant, meta)
            return

        product = self.adapter.extract_product(response, self.brands)
        if product is None:
            self.logger.debug(f"Not a product page: {response.url}")
            return

        self._accept_product(product, meta)
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

        # the category comes from the listing this product was found on.
        # only fill a blank -- some retailers state a better one on the
        # product page itself, and that must not be overwritten.
        route_category = meta.get("category")
        if route_category and not product.category:
            product.category = route_category

        # Campaigns are attached after the crawl, by apply_campaigns(), not
        # here: a campaign found later applies to a product found earlier,
        # and attaching as we go made the result depend on page order.

        # Deduplicate on the retailer's product id, so the same item reached
        # through several URLs collapses into one record.
        existing = self.products.get(product.product_id)
        if existing is None:
            self.products[product.product_id] = product
            self._page_offers[product.product_id] = list(meta.get("page_offers") or [])
            # Hand the record to whatever is saving as the run goes. A run
            # that dies otherwise leaves nothing at all: the John Lewis crawl
            # was stopped at 422 requests and wrote no file, losing about 35
            # minutes of work.
            if self.on_product is not None:
                try:
                    self.on_product(product)
                except Exception as exc:  # pragma: no cover - never fatal
                    self.logger.warning(f"incremental save failed: {exc}")
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

    def apply_campaigns(self) -> int:
        """Attach campaigns to every product, once the crawl has finished.

        This has to happen at the end rather than as each product is
        accepted, because a campaign found on page 40 applies just as much to
        the product read on page 1. Attaching during the crawl made
        `applied_campaigns` depend on the order pages happened to arrive: an
        ASOS run gave its one site-wide campaign to 255 of 775 products and
        left the other 520 without it, though a site-wide campaign applies to
        everything by definition.

        Returns how many products had campaigns attached.
        """
        touched = 0
        for product in self.products.values():
            applied = self._campaigns_for(product)
            product.applied_campaigns = applied
            if applied:
                touched += 1
        return touched

    def _campaigns_for(self, product: Product) -> List[str]:
        """Ids of the campaigns that apply to this product.

        When the campaign's own offers page was read, that page's product list
        decides: the campaign applies to the products it lists and nothing
        else, even if it is labelled site-wide. "Up to 20% off selected
        beauty" is not on every product.

        Otherwise a campaign qualifies when:
          * it is site-wide, so it applies everywhere
          * the product's own page showed it
          * its text matches the product's promotional copy exactly
          * its link names the retailer's offer, and the product's own page
            showed an offer with that same name

        We never attach a brand or category campaign to a product just
        because we saw it during the same run.
        """
        shown = self._page_offers.get(product.product_id, [])
        shown_text = {_plain(self.campaigns[cid].promotion_text)
                      for cid in shown if cid in self.campaigns}
        # an offers page lists page ids; a size split out of a page has its
        # own id, so the page's id is read back from the product's URL
        ids = {product.product_id, self.adapter.product_id_from_url(product.product_url or "")}

        applicable = []
        for campaign in self.campaigns.values():
            members = self.offer_members.get(_page_key(campaign.landing_url or ""))
            if members is not None:
                if ids & members:
                    applicable.append(campaign.campaign_id)
                continue

            key = self.adapter.promotion_key(campaign)
            if (campaign.scope == SCOPE_SITEWIDE
                    or campaign.campaign_id in shown
                    or (product.promotional_copy
                        and campaign.promotion_text == product.promotional_copy)
                    or (key and _plain(key) in shown_text)):
                applicable.append(campaign.campaign_id)
        return applicable


def _plain(text: str) -> str:
    """Text compared loosely: case and spacing ignored."""
    return " ".join(text.split()).casefold()


def _page_key(url: str) -> str:
    """A page's address without query or fragment, so page 1 and page 7 of
    one offer share a key: .../offers/tiered/?pageNumber=7 -> .../offers/tiered/"""
    return url.split("#")[0].split("?")[0].rstrip("/").lower()
