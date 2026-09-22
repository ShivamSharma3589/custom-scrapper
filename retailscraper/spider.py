"""The generic crawler."""

import re
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Sequence

from scrapling.spiders import CrawlRule, LinkExtractor, Request, SitemapSpider

from .adapters.base import RetailerAdapter
from .models import Campaign, Product, RejectedRecord, SCOPE_PRODUCT, SCOPE_SITEWIDE
from .validation import match_brand, validate


class RetailPromotionSpider(SitemapSpider):
    """Crawl one retailer for one set of brands."""

    name = "retail-promotions"

    robots_txt_obey = True
    autothrottle_enabled = True
    autothrottle_start_delay = 2.0
    autothrottle_max_delay = 30.0
    concurrent_requests = 2
    concurrent_requests_per_domain = 2
    download_delay = 1.0

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
        """:param adapter: The retailer adapter supplying URLs and selectors."""
        self.adapter = adapter
        self.brands = list(brands)
        self.max_products = max_products
        self.max_listing_pages = max_listing_pages
        self.categories = list(categories or [])
        self.strict_brand = strict_brand
        self.resolve_categories = resolve_categories
        self.campaigns_only = campaigns_only

        self.sitemap_urls = [] if campaigns_only else list(adapter.sitemap_urls)
        self.allowed_domains = {adapter.domain}

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

        self.products: Dict[str, Product] = {}
        self.campaigns: Dict[str, Campaign] = {}
        self.rejected: List[RejectedRecord] = []

        self.category_index: Dict[str, str] = {}

        self.blocked_by_brand: Dict[str, int] = {}
        self.on_product = on_product

        self._dispatched = 0
        self._dispatched_by_brand: Dict[str, int] = {}

        self._list_links: Dict[str, set] = {}

        self._page_offers: Dict[str, List[str]] = {}

        self.offer_members: Dict[str, set] = {}

        self._refused_in_a_row = 0
        self.switched_sessions: List[str] = []
        self._active_session = "default"

        super().__init__(crawldir=crawldir)

    def configure_sessions(self, manager) -> None:
        """Let the adapter decide how this retailer must be fetched."""
        self.adapter.configure_session(manager)

    async def start_requests(self) -> AsyncGenerator[Request, None]:
        """Seed the crawl with campaign pages, then the brand listings or the sitemap."""
        if self.adapter.warmup_url():
            yield Request(self.adapter.warmup_url(), callback=self.parse_warmup)
            return

        async for request in self._real_requests():
            yield request

    async def _real_requests(self) -> AsyncGenerator[Request, None]:
        """Everything the crawl actually needs, once any warmup has happened."""
        if self.campaigns_only:
            for url in self.adapter.campaign_discovery_urls():
                yield Request(url, callback=self.parse_campaign_directory)
            return

        seeds = list(self.adapter.campaign_seed_urls(self.brands))
        for url in self.adapter.campaign_discovery_urls():
            if url not in seeds:
                yield Request(url, callback=self.parse_campaign_directory)

        for url in seeds:
            yield Request(url, callback=self.parse_campaign_page)

        listing_pages = []
        for brand in self.brands:
            listing_pages.extend(
                self.adapter.product_listing_urls(
                    brand, self.categories, max_pages=self.max_listing_pages
                )
            )

        use_sitemap = bool(self.sitemap_urls) and not (
            self.categories and listing_pages
        )
        if use_sitemap:
            async for request in super().start_requests():
                yield request

            if self.resolve_categories:
                for page in self._category_index_pages():
                    yield Request(
                        page.url,
                        callback=self.parse_category_index,
                        sid=self.adapter.listing_session_id or "",
                        meta={
                            "brand": page.brand,
                            "category": page.category,
                            "requested_url": page.url,
                        },
                    )

        for page in listing_pages:
            yield self._listing_request(page.url, page.brand, page.category)

    def _listing_request(self, url: str, brand, category) -> Request:
        """A request for one listing page."""
        return Request(
            url,
            callback=self.parse_listing_page,
            sid=self.adapter.listing_session_id or "",
            meta={"brand": brand, "category": category, "url": url},
        )

    async def parse_listing_page(self, response) -> AsyncGenerator[Any, None]:
        """Harvest product links from a listing page and queue them."""
        listing_campaigns = self.adapter.extract_campaigns(response)
        for campaign in listing_campaigns:
            self._record_campaign(campaign)

        meta = response.meta or {}
        inherited = {k: meta.get(k) for k in ("brand", "category") if meta.get(k)}

        direct = self.adapter.extract_products_from_listing(response, meta.get("brand"))
        if direct:
            self.logger.info(
                f"{len(direct)} product(s) read directly from {response.url}"
            )
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

        list_url = str(meta.get("url") or response.url).split("?")[0]
        seen = self._list_links.setdefault(list_url, set())
        added = len(set(listed) - seen)
        seen.update(listed)

        for url in self.adapter.more_listing_pages(response, meta, added):
            yield self._listing_request(url, meta.get("brand"), meta.get("category"))

    def rules(self) -> List[CrawlRule]:
        """Dispatch sitemap URLs, keeping only plausible product pages."""
        if not self.sitemap_urls or self.campaigns_only or not self.brands:
            return []

        return [
            CrawlRule(
                link_extractor=LinkExtractor(allow_domains=[self.adapter.domain]),
                callback=self.parse_product_page,
                process_request=self._filter_and_cap,
            )
        ]

    def _filter_and_cap(self, request: Request, response) -> Optional[Request]:
        """Drop URLs the adapter does not recognise, then apply the cap."""
        if not self.adapter.select_candidates([request.url], self.brands):
            return None
        return self._enforce_product_cap(request, response)

    def _enforce_product_cap(self, request: Request, _response) -> Optional[Request]:
        """Drop product requests once a brand has had its share."""
        brand = (request.meta or {}).get("brand") or self._brand_for_url(request.url)
        seen = self._dispatched_by_brand.get(brand, 0)

        if self.max_products is not None and seen >= self.max_products:
            return None

        self._dispatched_by_brand[brand] = seen + 1
        self._dispatched += 1
        return request

    async def is_blocked(self, response) -> bool:
        """Note which brand a refused request belonged to, then defer."""
        blocked = await super().is_blocked(response)
        if blocked:
            brand = self._brand_for_url(str(response.url))
            self.blocked_by_brand[brand] = self.blocked_by_brand.get(brand, 0) + 1
        self._refused_in_a_row = self._refused_in_a_row + 1 if blocked else 0
        return blocked

    async def retry_blocked_request(self, request: Request, response) -> Request:
        """Move the crawl to the adapter's next session once this one is being refused."""
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
            # The backup takes over the retired id: queued requests already carry it.
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
        """Which requested brand a candidate URL appears to belong to."""
        lowered = url.lower()
        for brand in self.brands:
            if self.adapter._brand_slug(brand) in lowered:
                return brand
        return "_unmatched"

    async def parse(self, response) -> AsyncGenerator[Any, None]:
        """Fallback callback; every URL we care about has an explicit one."""
        self.logger.debug(f"No specific handler for {response.url}")
        return
        yield  # pragma: no cover - keeps this an async generator

    def _category_index_pages(self) -> List[Any]:
        """Category listing pages to consult purely as a category lookup."""
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
        """Did the category listing we asked for survive the redirect?"""
        requested_slug = requested_url.rstrip("/").rsplit("/", 1)[-1].lower()
        return requested_slug in final_url.rstrip("/").lower()

    async def parse_category_index(self, response) -> AsyncGenerator[Any, None]:
        """Record which category a listing page files its products under."""
        meta = response.meta or {}
        category = meta.get("category")
        if not category:
            return

        requested = meta.get("requested_url") or ""
        if requested and not self._listing_still_matches(requested, response.url):
            self.logger.info(
                f"category index: skipping '{category}' -- {requested} redirected "
                f"to {response.url}, so the listing is no longer category-scoped"
            )
            return

        found = 0
        for url in self.adapter.extract_product_links(response, meta.get("brand")):
            product_id = self.adapter.product_id_from_url(url)
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
        """Fill in categories learned from listing pages. Returns how many."""
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
        """Discard the warmup response and queue the pages we actually want."""
        self.logger.info(
            f"session warmed via {response.url} "
            f"({len(response.body)} bytes, content discarded)"
        )
        async for request in self._real_requests():
            yield request

    async def parse_campaign_directory(self, response) -> AsyncGenerator[Any, None]:
        """Collect every campaign advertised on an offers hub."""
        for campaign in self.adapter.extract_campaign_directory(response):
            self._record_campaign(campaign)
        for campaign in self.adapter.extract_campaigns(response):
            self._record_campaign(campaign)

        members = self.adapter.offer_page_products(response)
        if members:
            page = self.adapter.offer_page_of(str(response.url))
            self.offer_members.setdefault(_page_key(page), set()).update(members)

        self.logger.info(
            f"campaign directory: {len(self.campaigns)} campaign(s) known "
            f"after {response.url} ({len(members)} product(s) listed)"
        )

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
        page_offers = []
        for campaign in self.adapter.extract_campaigns(response):
            self._record_campaign(campaign)
            if campaign.scope == SCOPE_PRODUCT:
                page_offers.append(campaign.campaign_id)

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
        """Validate, attribute and store one product."""
        rejection = validate(product, self.brands, strict_brand=self.strict_brand)
        if rejection is not None:
            self.rejected.append(rejection)
            self.logger.info(
                f"REJECTED reason={rejection.reason} url={rejection.source_url} "
                f"detail={rejection.detail}"
            )
            return

        product.brand_matched_to = match_brand(product.brand, self.brands)

        route_category = meta.get("category")
        if route_category and not product.category:
            product.category = route_category

        existing = self.products.get(product.product_id)
        if existing is None:
            self.products[product.product_id] = product
            self._page_offers[product.product_id] = list(meta.get("page_offers") or [])
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

    def _record_campaign(self, campaign: Campaign) -> None:
        """Store a campaign once, regardless of how many pages showed it."""
        if campaign.campaign_id not in self.campaigns:
            self.campaigns[campaign.campaign_id] = campaign

    def apply_campaigns(self) -> int:
        """Attach campaigns to every product, once the crawl has finished."""
        touched = 0
        for product in self.products.values():
            applied = self._campaigns_for(product)
            product.applied_campaigns = applied
            if applied:
                touched += 1
        return touched

    def _campaigns_for(self, product: Product) -> List[str]:
        """Ids of the campaigns that apply to this product."""
        shown = self._page_offers.get(product.product_id, [])
        shown_text = {_plain(self.campaigns[cid].promotion_text)
                      for cid in shown if cid in self.campaigns}
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
    """A page's address without query or fragment, so every page of a list shares one key."""
    return url.split("#")[0].split("?")[0].rstrip("/").lower()
