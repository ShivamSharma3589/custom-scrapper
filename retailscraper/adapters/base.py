"""The contract every retailer adapter implements."""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, TYPE_CHECKING

from ..models import Campaign, Product


@dataclass(frozen=True)
class ListingPage:
    """A listing page, tagged with the brand and category it lists."""

    url: str
    brand: str
    category: Optional[str] = None


if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from scrapling.engines.toolbelt.custom import Response


class RetailerAdapter(ABC):
    """Base class for a single retailer's extraction logic."""

    name: str = ""

    domain: str = ""

    display_name: str = ""

    supports_categories: bool = False

    confirms_brand_stocking: bool = False

    listing_session_id: str = ""

    CATEGORY_SLUGS: Dict[str, str] = {}

    crawl_delay: Optional[float] = None
    max_concurrent_requests: Optional[int] = None

    sitemap_urls: List[str] = []

    def campaign_seed_urls(self, brands: Sequence[str]) -> List[str]:
        """Landing pages worth scanning for these brands' campaigns."""
        return []

    def product_listing_urls(
        self, brand: str, categories: Sequence[str], max_pages: int
    ) -> List["ListingPage"]:
        """Listing pages for ONE brand, optionally scoped to categories."""
        return []

    def extract_product_variants(
        self, response: "Response", target_brands: Sequence[str] = ()
    ) -> List[Product]:
        """Every separately-priced size on one product page."""
        return []

    def extract_products_from_listing(
        self, response: "Response", brand: Optional[str] = None
    ) -> List[Product]:
        """Products readable straight off a listing, with no further fetch."""
        return []

    def product_pages_to_read(self, response: "Response", brand: Optional[str] = None) -> List[str]:
        """Product pages still needed after a listing gave products it could not describe exactly."""
        return []

    backup_session_ids: Sequence[str] = ()

    def extract_product_links(self, response, brand: Optional[str] = None) -> List[str]:
        """Product URLs found on a listing page."""
        return []

    def configure_session(self, manager) -> None:
        """Register the fetch session this retailer needs."""
        from scrapling.fetchers import FetcherSession

        manager.add("default", FetcherSession())

    def known_categories(self) -> List[str]:
        """Category names this adapter can look products up by."""
        return sorted(set(self.CATEGORY_SLUGS.values()))

    def product_id_from_url(self, url: str) -> Optional[str]:
        """The retailer's product id for a URL, or None."""
        return None

    @staticmethod
    def _brand_slug(brand: str) -> str:
        """Brand name as it appears in a URL. Adapters override where theirs differs."""
        return re.sub(r"[^a-z0-9]+", "-", brand.strip().lower()).strip("-")

    @abstractmethod
    def is_product_url(self, url: str) -> bool:
        """True if this URL is a product detail page for this retailer."""

    @abstractmethod
    def select_candidates(self, urls: Iterable[str], brands: Sequence[str]) -> List[str]:
        """Narrow discovered URLs down to likely products for these brands."""

    @abstractmethod
    def extract_product(
        self, response: "Response", target_brands: Sequence[str] = ()
    ) -> Optional[Product]:
        """Build a Product from a product detail page, or None if it is not one."""

    def prepare(self, brands: Sequence[str]) -> List[str]:
        """One-off setup before the crawl, e.g. resolving retailer ids."""
        return []

    def expected_product_count(self, brands: Sequence[str]) -> Optional[int]:
        """How many products the retailer says it has for these brands."""
        return None

    def more_listing_pages(self, response: "Response", meta: dict, added: int) -> List[str]:
        """Listing pages to visit after this one: what a brand page links to, or the next page."""
        return []

    def offer_page_links(self, response: "Response") -> List[str]:
        """Other offers pages linked from this one."""
        return []

    def offer_page_products(self, response: "Response") -> List[str]:
        """Ids of the products an offers page lists as being in its offer."""
        return []

    def offer_page_of(self, url: str) -> str:
        """The offers page a fetched URL belongs to, for tying an offer to its products."""
        return url

    def promotion_key(self, campaign: Campaign) -> Optional[str]:
        """The retailer's own name for an offer, when its link carries one."""
        return None

    def crawl_warnings(self) -> List[str]:
        """Problems noticed during the crawl, for the run summary and manifest."""
        return []

    def warmup_url(self) -> Optional[str]:
        """A throwaway page to fetch first, to establish the session."""
        return None

    def campaign_discovery_urls(self) -> List[str]:
        """The retailer's offers hub and sale landing pages."""
        return []

    def extract_campaign_directory(self, response: "Response") -> List[Campaign]:
        """Campaigns advertised in a page's offer navigation or hub tiles."""
        return []

    def _scan_offer_links(self, response: "Response") -> List[Campaign]:
        """Shared implementation of `extract_campaign_directory`."""
        from ..models import SCOPE_UNRESOLVED
        from ..normalize import clean_text, extract_promo_code, strip_call_to_action
        from ..promotions import classify_promotion, is_browse_facet, is_confident_offer

        campaigns: List[Campaign] = []
        seen = set()

        for node in response.css("a[href]"):
            text = strip_call_to_action(clean_text(node.get_all_text()))
            if not text or text in seen or not is_confident_offer(text):
                continue

            href = node.attrib.get("href") or ""
            if href.startswith("/"):
                href = f"https://{self.domain}{href}"
            elif not href.startswith("http"):
                continue

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
        """What an offer link's destination says about the offer's reach."""
        return (None, None)

    @abstractmethod
    def extract_campaigns(self, response: "Response") -> List[Campaign]:
        """Extract promotions visible on this page."""

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience
        return f"<{type(self).__name__} {self.name!r}>"


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

    for adapter_cls in _REGISTRY.values():
        instance = adapter_cls()
        if instance.domain.lower().removeprefix("www.") == key:
            return instance

    available = ", ".join(sorted(_REGISTRY)) or "none registered"
    raise KeyError(f"No adapter for {name_or_domain!r}. Available: {available}")


def available_adapters() -> List[str]:
    """List registered adapter names."""
    return sorted(_REGISTRY)
