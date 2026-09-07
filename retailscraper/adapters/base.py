"""The contract every retailer adapter implements.

This is the seam that keeps the framework generic. The crawler, validation,
deduplication and output layers know only about this interface -- they never
contain a selector or a URL pattern for any particular shop.

An adapter is responsible for exactly three retailer-specific questions:

  1. Where do this retailer's product URLs live?      -> `sitemap_urls`
                                                         `is_product_url`
                                                         `select_candidates`
  2. How do I read one product page?                  -> `extract_product`
  3. How do I read the promotions on a page?          -> `extract_campaigns`

Everything else is shared.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, TYPE_CHECKING

from ..models import Campaign, Product


@dataclass(frozen=True)
class ListingPage:
    """A listing page to crawl, tagged with what it is a listing OF.

    Carrying the brand and category alongside the URL is what lets products
    found on the page inherit both, instead of the crawler trying to infer
    them from a URL slug that often does not mention either.
    """

    url: str
    brand: str
    category: Optional[str] = None

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from scrapling.engines.toolbelt.custom import Response


class RetailerAdapter(ABC):
    """Base class for a single retailer's extraction logic."""

    #: Short machine name used in output and on the command line, e.g. "lookfantastic".
    name: str = ""

    #: The retailer's public domain, used to scope the crawl.
    domain: str = ""

    #: Human-readable retailer name as it should appear in output records.
    display_name: str = ""

    #: True when this retailer exposes category-scoped listing pages, so a
    #: `--categories` filter can be honoured. Declared rather than assumed:
    #: silently ignoring a category filter would hand back the wrong dataset
    #: while looking like it worked.
    supports_categories: bool = False

    #: Session id used for listing pages. Some retailers render their product
    #: grid with JavaScript and need a browser for listings while their
    #: product pages remain fine over plain HTTP -- paying for a browser only
    #: where it is actually needed.
    listing_session_id: str = ""

    #: Category slugs as this retailer spells them, keyed by the general name
    #: a user is likely to type ("makeup" -> "make-up"). Adapters override
    #: this; the empty default keeps `known_categories()` safe on an adapter
    #: that has no category pages at all.
    CATEGORY_SLUGS: Dict[str, str] = {}

    #: How hard this retailer may be crawled, when the shared default is too
    #: aggressive for it. John Lewis refuses requests once cumulative volume
    #: builds up: a seven-brand run had 144 of 348 refused, all of them the
    #: two brands crawled last, and one of those was then reported as
    #: possibly-unstocked when it is stocked. None means "use the default".
    crawl_delay: Optional[float] = None
    max_concurrent_requests: Optional[int] = None

    #: Sitemap (or robots.txt) URLs used to discover products. Sitemaps are
    #: preferred where available: they are complete, cheap, and explicitly
    #: published by the retailer for this purpose. Leave empty when the
    #: retailer has no usable sitemap and use `product_listing_urls` instead.
    sitemap_urls: List[str] = []

    #: Pages to visit for campaign/offer discovery, in addition to product
    #: pages. Typically brand or category landing pages.
    def campaign_seed_urls(self, brands: Sequence[str]) -> List[str]:
        """Return landing pages worth scanning for campaigns for these brands."""
        return []

    def product_listing_urls(
        self, brand: str, categories: Sequence[str], max_pages: int
    ) -> List["ListingPage"]:
        """Listing pages for ONE brand, optionally scoped to categories.

        The alternative discovery route for retailers without a reachable
        sitemap, and the only way to honour a category filter on retailers
        that do not put a category on the product page itself.

        Taking a single brand (rather than the whole list) lets the crawler
        remember which brand each listing belongs to, so products found there
        are attributed correctly even when their URL never mentions the brand.
        The same applies to the category.
        """
        return []

    def extract_product_variants(
        self, response: "Response", target_brands: Sequence[str] = ()
    ) -> List[Product]:
        """Every separately-priced size on one product page.

        Some retailers sell several sizes behind a single page and quote a
        range for them: John Lewis publishes Bobbi Brown Vitamin Enriched
        Face Base at 15ml/50ml/100ml for 19.00/54.00/84.00 and displays
        "from £19.00". Recorded as one product, that 19.00 is not the price
        of anything comparable -- it made John Lewis look like the cheapest
        stockist of a product it sells at 84.00, which is why a from-price is
        excluded from ranking entirely.

        An adapter that can read the individual sizes returns one product per
        size here, and the crawler stores those instead of the single ranged
        record. Returning nothing (the default) keeps the `extract_product`
        path, which is right for every retailer that prices a page once.

        Only sizes belong here. Shades are not separate products -- retailers
        list one "(Various Shades)" entry and so do we -- and expanding them
        would multiply the catalogue while breaking matching against every
        retailer that does not.
        """
        return []

    def extract_products_from_listing(
        self, response: "Response", brand: Optional[str] = None
    ) -> List[Product]:
        """Products readable directly from a listing, with no further fetch.

        Most retailers publish a grid of links and keep the real data on each
        product page, so the crawler follows those links. Some publish the
        catalogue itself as data -- AllBeauty is a Shopify store whose
        `/collections/<brand>/products.json` returns 250 complete products,
        brand and prices included, in one request. Fetching a page per product
        there would mean 475 requests for data already in hand.

        Returning a non-empty list tells the crawler this listing IS the
        product data, so it will not also follow `extract_product_links`.
        The default is empty, which keeps every existing adapter on the
        link-following path.
        """
        return []

    def extract_product_links(self, response, brand: Optional[str] = None) -> List[str]:
        """Product URLs found on a listing page.

        `brand` is the brand this listing belongs to, so adapters whose
        listing pages also carry cross-sell carousels can drop the obviously
        irrelevant links. That is a cost control, not brand verification --
        anything that slips through is still checked against the retailer's
        own product data later.

        Only needed by adapters that use `product_listing_urls`.
        """
        return []

    def configure_session(self, manager) -> None:
        """Register the fetch session this retailer needs.

        The default is a plain HTTP session, which is the cheapest thing that
        works and is all most retailers require. Override when a site needs
        something heavier -- a real browser to get past bot protection, for
        instance. Keeping this on the adapter means one retailer's defences
        never impose their cost on every other retailer.
        """
        from scrapling.fetchers import FetcherSession

        manager.add("default", FetcherSession())

    def known_categories(self) -> List[str]:
        """Category names this adapter can look products up by.

        Used to resolve categories on a sitemap-driven run, where no category
        filter was given but the records still need one. Returns the caller-
        facing names, not the retailer's slugs.
        """
        return sorted(set(self.CATEGORY_SLUGS.values()))

    def product_id_from_url(self, url: str) -> Optional[str]:
        """The retailer's own product id for a product URL, or None.

        Needed to join a product found through the sitemap to the category
        listing page it also appears on, since neither retailer states a
        category on the product page itself.
        """
        return None

    @abstractmethod
    def is_product_url(self, url: str) -> bool:
        """True if this URL is a product detail page for this retailer."""

    @abstractmethod
    def select_candidates(self, urls: Iterable[str], brands: Sequence[str]) -> List[str]:
        """Narrow all discovered URLs down to likely products for these brands.

        This is a cheap pre-filter to avoid fetching the retailer's entire
        catalogue -- it is allowed to be approximate and to over-include.
        It must never be treated as brand verification: the brand is proved
        later, from the product page itself, in `extract_product`.
        """

    @abstractmethod
    def extract_product(
        self, response: "Response", target_brands: Sequence[str] = ()
    ) -> Optional[Product]:
        """Build a Product from a product detail page.

        Returns None when the page is not a usable product page at all.
        Implementations must set `Product.brand_verified_by` to record how the
        brand was established, and must not guess a brand from the URL slug.

        `target_brands` is supplied so an adapter may, as a LAST resort on
        pages carrying no structured brand, check whether an already-requested
        brand is corroborated by independent signals. It must never be used to
        widen what counts as a match.
        """

    def prepare(self, brands: Sequence[str]) -> List[str]:
        """One-off setup before the crawl, e.g. resolving retailer ids.

        Called by the runner BEFORE the spider starts, so an adapter that
        needs its own network calls makes them outside the crawler's event
        loop -- starting a second browser session inside a running loop fails,
        and a swallowed failure here means the whole run quietly returns
        nothing.

        Returns a list of human-readable problems (e.g. brands it could not
        resolve). An empty list means everything is ready.
        """
        return []

    def warmup_url(self) -> Optional[str]:
        """A page to fetch first, purely to establish the session.

        Some retailers challenge the first navigation of a browser session
        and trust it afterwards -- the challenge response itself is what sets
        the cookie. Those adapters return a throwaway URL here, and the
        crawler fetches it before anything whose content it actually needs.

        Returning None (the default) means the first real request can go
        straight out.
        """
        return None

    def campaign_discovery_urls(self) -> List[str]:
        """Pages to scan when asked for every campaign on the site.

        Used by a campaigns-only run, where no brands were given. Return the
        retailer's offers hub and any dedicated sale landing pages. An adapter
        that has no such pages returns nothing, and the run says so rather
        than pretending it found none.
        """
        return []

    def extract_campaign_directory(self, response: "Response") -> List[Campaign]:
        """Campaigns advertised in this page's offer navigation or hub tiles.

        Kept separate from `extract_campaigns` on purpose. That method reads
        the promo slots a page reserves for offers, and runs on every product
        page. This one scans a whole page of links, which is only sensible on
        an offers hub -- doing it on every product page would attach the
        retailer's entire offer menu to each product.
        """
        return []

    def _scan_offer_links(self, response: "Response") -> List[Campaign]:
        """Shared implementation of `extract_campaign_directory`.

        Every link whose visible text names a concrete promotional mechanic
        becomes a campaign, with its href kept as the landing page. Scope
        comes from the adapter's own URL structure via `scope_for_href`.

        Uses the strict offer test, not the permissive one: a retailer's
        offers page is mostly navigation ("gift finder", "gift cards"), and
        the permissive test would let all of it through.
        """
        from ..models import SCOPE_UNRESOLVED
        from ..normalize import clean_text
        from ..promotions import classify_promotion, is_browse_facet, is_confident_offer
        from ..normalize import extract_promo_code

        campaigns: List[Campaign] = []
        seen = set()

        for node in response.css("a[href]"):
            text = clean_text(node.get_all_text())
            if not text or text in seen or not is_confident_offer(text):
                continue

            href = node.attrib.get("href") or ""
            if href.startswith("/"):
                href = f"https://{self.domain}{href}"
            elif not href.startswith("http"):
                continue

            # A bare discount tier ("At Least 30% Off" -> /offers-save-30) is
            # a shop-by-saving FILTER, not a campaign. Recording it as one
            # made every site-wide campaign apply to every product, so a
            # product discounted 29% carried "At Least 70% Off".
            if is_browse_facet(text, href):
                continue

            seen.add(text)
            scope, scope_value = self.scope_for_href(href)
            campaigns.append(
                Campaign(
                    retailer=self.display_name,
                    promotion_text=text,
                    promotion_type=classify_promotion(text),
                    scope=scope or SCOPE_UNRESOLVED,
                    scope_value=scope_value,
                    promo_code=extract_promo_code(text),
                    source_url=str(response.url),
                    landing_url=href,
                )
            )
        return campaigns

    def scope_for_href(self, href: str) -> "tuple[Optional[str], Optional[str]]":
        """What an offer link's destination says about the offer's reach.

        Returns (scope, scope_value). The default admits it does not know,
        which is the honest answer for an adapter that has not been taught
        this retailer's URL structure -- better than guessing "sitewide" and
        having a category offer applied to the whole catalogue.
        """
        return (None, None)

    @abstractmethod
    def extract_campaigns(self, response: "Response") -> List[Campaign]:
        """Extract promotions visible on this page.

        Implementations must set each campaign's `scope` honestly: a banner
        shown in the site header is `sitewide`, not a campaign for whichever
        brand's page it happened to appear on. When the reach of an offer
        cannot be established from the page structure, use `unresolved`.
        """

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return f"<{type(self).__name__} {self.name!r}>"


# --- adapter registry -----------------------------------------------------
# Adapters register themselves here so the runner can resolve a retailer by
# name or by domain without importing every adapter explicitly.

_REGISTRY: dict = {}


def register(adapter_cls) -> type:
    """Class decorator that adds an adapter to the registry."""
    instance = adapter_cls()
    _REGISTRY[instance.name] = adapter_cls
    return adapter_cls


def get_adapter(name_or_domain: str) -> RetailerAdapter:
    """Resolve an adapter by its short name or by its domain.

    Accepts "lookfantastic", "lookfantastic.com" or a full URL, so the CLI can
    take whatever form of the retailer the user types.
    """
    from . import allbeauty, amazon, asos, boots, johnlewis, lookfantastic, marksandspencer  # noqa: F401  (imports register adapters)

    key = name_or_domain.strip().lower()
    for prefix in ("https://", "http://"):
        if key.startswith(prefix):
            key = key[len(prefix):]
    key = key.split("/")[0]
    if key.startswith("www."):
        key = key[4:]

    if key in _REGISTRY:
        return _REGISTRY[key]()

    # Fall back to matching on the adapter's declared domain.
    # removeprefix, not lstrip: lstrip("www.") strips any leading run of those
    # characters, so a domain like "wow.example.com" would lose its "w".
    for adapter_cls in _REGISTRY.values():
        instance = adapter_cls()
        if instance.domain.lower().removeprefix("www.") == key:
            return instance

    available = ", ".join(sorted(_REGISTRY)) or "none registered"
    raise KeyError(f"No adapter for {name_or_domain!r}. Available: {available}")


def available_adapters() -> List[str]:
    """List registered adapter names."""
    from . import allbeauty, amazon, asos, boots, johnlewis, lookfantastic, marksandspencer  # noqa: F401

    return sorted(_REGISTRY)
