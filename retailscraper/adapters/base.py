"""The contract every retailer adapter implements.

The crawler, validation and output layers know only about this interface --
they hold no selector or URL pattern for any particular shop. Each adapter
answers three questions about its own retailer:

  1. Where are the product URLs?   -> sitemap_urls, is_product_url,
                                      select_candidates
  2. How do I read a product page? -> extract_product
  3. What promotions are on it?    -> extract_campaigns

Everything else is shared.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, TYPE_CHECKING

from ..models import Campaign, Product


@dataclass(frozen=True)
class ListingPage:
    """A listing page, tagged with the brand and category it lists.

    Carrying them here lets products found on the page inherit both, rather
    than guessing from a URL slug that often mentions neither.
    """

    url: str
    brand: str
    category: Optional[str] = None


if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from scrapling.engines.toolbelt.custom import Response


class RetailerAdapter(ABC):
    """Base class for a single retailer's extraction logic."""

    #: Short name used on the command line, e.g. "lookfantastic".
    name: str = ""

    #: The retailer's public domain, used to scope the crawl.
    domain: str = ""

    #: Retailer name as it should appear in output records.
    display_name: str = ""

    #: True when this retailer has category listing pages, so --categories
    #: can be honoured. Ignoring the filter silently would return the wrong
    #: dataset while looking like it worked.
    supports_categories: bool = False

    #: True when prepare() can tell whether the shop stocks each brand.
    #: Decides whether an empty brand fails the run: where True a zero means
    #: something broke, where False it may simply not be stocked.
    confirms_brand_stocking: bool = False

    #: Session used for listing pages, when the grid needs a browser but the
    #: product pages do not.
    listing_session_id: str = ""

    #: Category slugs as this retailer spells them, keyed by the name a user
    #: would type ("makeup" -> "make-up").
    CATEGORY_SLUGS: Dict[str, str] = {}

    #: Crawl rate for retailers that refuse requests under load.
    #: None means use the shared default.
    crawl_delay: Optional[float] = None
    max_concurrent_requests: Optional[int] = None

    #: Sitemaps used to discover products. Preferred where available: they
    #: are complete and published by the retailer for this purpose. Leave
    #: empty and use product_listing_urls when there is no usable sitemap.
    sitemap_urls: List[str] = []

    def campaign_seed_urls(self, brands: Sequence[str]) -> List[str]:
        """Landing pages worth scanning for these brands' campaigns."""
        return []

    def product_listing_urls(
        self, brand: str, categories: Sequence[str], max_pages: int
    ) -> List["ListingPage"]:
        """Listing pages for ONE brand, optionally scoped to categories.

        The discovery route for retailers without a usable sitemap. Taking one
        brand at a time lets the crawler remember which brand each listing
        belongs to, since the product URLs often do not say.
        """
        return []

    def extract_product_variants(
        self, response: "Response", target_brands: Sequence[str] = ()
    ) -> List[Product]:
        """Every separately-priced size on one product page.

        Some retailers sell several sizes behind one page and show "from
        £19.00". That figure is not the price of anything comparable, so an
        adapter that can read the individual sizes returns one product each.

        Sizes only. Shades are one product everywhere else, and splitting them
        would break matching against every other retailer.
        """
        return []

    def extract_products_from_listing(
        self, response: "Response", brand: Optional[str] = None
    ) -> List[Product]:
        """Products readable straight off a listing, with no further fetch.

        For retailers that publish the catalogue as data -- AllBeauty's
        Shopify JSON returns 250 complete products in one request. Returning
        anything here tells the crawler not to follow product links as well.
        """
        return []

    def extract_product_links(self, response, brand: Optional[str] = None) -> List[str]:
        """Product URLs found on a listing page.

        `brand` lets an adapter drop obvious cross-sell links. That is a cost
        control, not brand verification -- anything that slips through is
        still checked against the retailer's own data later.
        """
        return []

    def configure_session(self, manager) -> None:
        """Register the fetch session this retailer needs.

        Plain HTTP by default. Override for a site that needs a browser, so
        one retailer's defences do not slow down every other retailer.
        """
        from scrapling.fetchers import FetcherSession

        manager.add("default", FetcherSession())

    def known_categories(self) -> List[str]:
        """Category names this adapter can look products up by."""
        return sorted(set(self.CATEGORY_SLUGS.values()))

    def product_id_from_url(self, url: str) -> Optional[str]:
        """The retailer's product id for a URL, or None.

        Used to join a product found via the sitemap to the category listing
        it also appears on.
        """
        return None

    @abstractmethod
    def is_product_url(self, url: str) -> bool:
        """True if this URL is a product detail page for this retailer."""

    @abstractmethod
    def select_candidates(self, urls: Iterable[str], brands: Sequence[str]) -> List[str]:
        """Narrow discovered URLs down to likely products for these brands.

        A cheap pre-filter to avoid fetching the whole catalogue. It may
        over-include; the brand is proved later from the product page itself.
        """

    @abstractmethod
    def extract_product(
        self, response: "Response", target_brands: Sequence[str] = ()
    ) -> Optional[Product]:
        """Build a Product from a product detail page, or None if it is not one.

        Must set `brand_verified_by` to record how the brand was established,
        and must never guess a brand from the URL slug.

        `target_brands` is a last resort for pages with no structured brand --
        never a way to widen what counts as a match.
        """

    def prepare(self, brands: Sequence[str]) -> List[str]:
        """One-off setup before the crawl, e.g. resolving retailer ids.

        Runs before the crawler starts, so an adapter needing its own network
        calls makes them outside the crawler's event loop. Returns a list of
        problems; empty means everything is ready.
        """
        return []

    def expected_product_count(self, brands: Sequence[str]) -> Optional[int]:
        """How many products the retailer says it has for these brands.

        The difference between "we collected 336" and "we collected 336 of
        the 1,232 this shop says it has". None means it publishes no total.
        """
        return None

    def warmup_url(self) -> Optional[str]:
        """A throwaway page to fetch first, to establish the session.

        Some retailers challenge the first navigation and trust it afterwards.
        None means the first real request can go straight out.
        """
        return None

    def campaign_discovery_urls(self) -> List[str]:
        """The retailer's offers hub and sale landing pages."""
        return []

    def extract_campaign_directory(self, response: "Response") -> List[Campaign]:
        """Campaigns advertised in a page's offer navigation or hub tiles.

        Separate from `extract_campaigns` on purpose: this scans a whole page
        of links, which only makes sense on an offers hub. Doing it on every
        product page would attach the shop's entire offer menu to each one.
        """
        return []

    def _scan_offer_links(self, response: "Response") -> List[Campaign]:
        """Shared implementation of `extract_campaign_directory`.

        Every link whose text names a real promotion becomes a campaign.
        Uses the strict offer test, because an offers page is mostly
        navigation ("gift finder", "gift cards").
        """
        from ..models import SCOPE_UNRESOLVED
        from ..normalize import clean_text, extract_promo_code
        from ..promotions import classify_promotion, is_browse_facet, is_confident_offer

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

            # "At Least 30% Off" -> /offers-save-30 is a shop-by-saving
            # filter, not a campaign. Recorded as one, it applied to every
            # product, so an item discounted 29% carried "At Least 70% Off".
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

        The default admits it does not know, which beats guessing "sitewide"
        and applying a category offer to the whole catalogue.
        """
        return (None, None)

    @abstractmethod
    def extract_campaigns(self, response: "Response") -> List[Campaign]:
        """Extract promotions visible on this page.

        Set each campaign's `scope` honestly: a header banner is `sitewide`,
        not a campaign for whichever brand's page it appeared on. Use
        `unresolved` when the reach cannot be established.
        """

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return f"<{type(self).__name__} {self.name!r}>"


# --- adapter registry -----------------------------------------------------
# Adapters register themselves here, so the runner can resolve a retailer by
# name or domain. Importing this package runs adapters/__init__.py, which
# imports every adapter and therefore fills the registry.

_REGISTRY: dict = {}


def register(adapter_cls) -> type:
    """Class decorator that adds an adapter to the registry."""
    instance = adapter_cls()
    _REGISTRY[instance.name] = adapter_cls
    return adapter_cls


def get_adapter(name_or_domain: str) -> RetailerAdapter:
    """Resolve an adapter by short name, domain or full URL."""
    key = name_or_domain.strip().lower()
    for prefix in ("https://", "http://"):
        if key.startswith(prefix):
            key = key[len(prefix):]
    key = key.split("/")[0]
    if key.startswith("www."):
        key = key[4:]

    if key in _REGISTRY:
        return _REGISTRY[key]()

    # removeprefix, not lstrip: lstrip("www.") strips any leading run of
    # those characters, so "wow.example.com" would lose its "w".
    for adapter_cls in _REGISTRY.values():
        instance = adapter_cls()
        if instance.domain.lower().removeprefix("www.") == key:
            return instance

    available = ", ".join(sorted(_REGISTRY)) or "none registered"
    raise KeyError(f"No adapter for {name_or_domain!r}. Available: {available}")


def available_adapters() -> List[str]:
    """List registered adapter names."""
    return sorted(_REGISTRY)
