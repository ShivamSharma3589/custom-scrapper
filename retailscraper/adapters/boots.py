"""Boots adapter.

Three things differ from every other retailer here, all found by inspecting
the live site:

**It needs a real browser.** Boots sits behind Imperva. Plain HTTP gets a
"Pardon Our Interruption" challenge returned as HTTP 200 -- worse than a 403,
because it looks like success. `looks_blocked` lets the crawler spot a
challenge page rather than parse it as a product.

**Its sitemap is unreachable.** robots.txt names one, but it returns the same
challenge even through the browser, so discovery starts at each brand page
and follows the product lists it links to, page by page (`?paging.index=N`).

**It publishes microdata, not JSON-LD.** No `application/ld+json` anywhere;
the same facts are in schema.org `itemprop` attributes.

Product URLs are `https://www.boots.com/<slug>-<numeric id>`.
"""

import re
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence
from urllib.parse import parse_qs, urlparse

from ..models import SCOPE_CATEGORY, SCOPE_PRODUCT, SCOPE_SITEWIDE, Campaign, Product
from ..normalize import (
    canonical_url,
    clean_text,
    extract_promo_code,
    parse_price,
    percent_off,
)
from ..promotions import classify_promotion, looks_like_offer
from .base import ListingPage, RetailerAdapter, register

# Boots product URLs end in the numeric product id: /clinique-...-10292729
_PRODUCT_URL_RE = re.compile(r"^https?://(?:www\.)?boots\.com/[a-z0-9][^/?#]*?-(\d{6,})/?$", re.I)

# "Was £43.00" and "Save £10.75" in the price panel.
_WAS_RE = re.compile(r"Was\s*[£$€]?\s*([\d.,]+)", re.IGNORECASE)
_SAVE_RE = re.compile(r"Save\s*[£$€]?\s*([\d.,]+)", re.IGNORECASE)

# The challenge page Imperva serves instead of content. It arrives as HTTP
# 200, so status alone cannot be trusted.
_BLOCK_MARKER = "Pardon Our Interruption"


@register
class BootsAdapter(RetailerAdapter):
    """Extraction rules for boots.com."""

    name = "boots"
    domain = "www.boots.com"
    display_name = "Boots"

    # No sitemap: the one declared in robots.txt is served the bot-protection
    # challenge page even through a real browser. Discovery uses listing pages.
    sitemap_urls: List[str] = []

    #: Sent as paging.size. Boots accepts it but shows 48 products a page anyway.
    listing_page_size = 25

    #: Safety stop for one list. At 48 a page this is far past any brand's
    #: range; it only guards against a list that never stops adding products.
    MAX_LIST_PAGES = 50

    # Boots files each brand's range under category paths such as
    # /clinique/clinique-skincare, so a category filter can be honoured by
    # crawling those instead of the full range.
    supports_categories = True

    #: Category slugs as Boots spells them, keyed by the general name a user
    #: is likely to type. Anything unknown is slugified and tried as-is.
    CATEGORY_SLUGS = {
        "skincare": "skincare",
        "makeup": "make-up",
        "make-up": "make-up",
        "make up": "make-up",
        "fragrance": "fragrance",
        "haircare": "haircare",
        "hair": "haircare",
        "men": "men",
        "mens": "men",
        "gift": "gifts",
        "gifts": "gifts",
        "bath and body": "bath-and-body",
        "bath & body": "bath-and-body",
        "home": "home-fragrance",
    }

    #: Where offer discovery starts. The other offers pages (about 30:
    #: /fragrance/fragrance-offers, /offers/makeup-offers, /sale/...) are
    #: found from the links on this one, see `offer_page_links`.
    CAMPAIGN_HUB_PATHS = ["/offers"]

    def __init__(self) -> None:
        #: Brands whose page linked no product lists, so only its featured
        #: products could be collected. Reported by `crawl_warnings`.
        self._featured_only: set = set()

    def warmup_url(self) -> Optional[str]:
        """The homepage, fetched only to get past bot protection.

        Boots challenges the first navigation of a session and trusts it
        afterwards: the challenge response is itself what sets the cookie. So
        this request is expected to come back as the challenge page, and its
        content is deliberately not used -- it exists so that the request
        after it succeeds. Without it, a campaigns-only run fetches the offers
        hub cold and gets a 6 KB challenge page instead of 1.3 MB of offers.
        """
        return f"https://{self.domain}/"

    def campaign_discovery_urls(self) -> List[str]:
        """The offers hub, for a campaigns-only run."""
        return [f"https://{self.domain}{path}" for path in self.CAMPAIGN_HUB_PATHS]

    def scope_for_href(self, href: str):
        """Read an offer's reach from where its link points.

        Boots puts brands and departments in the same slot -- /no7/... and
        /fragrance/... are the same shape -- so the first segment alone cannot
        tell them apart. We claim a category only when the segment is one we
        already know is a department, and otherwise say unresolved rather than
        guessing a brand campaign into existence.
        """
        path = href.split(f"{self.domain}", 1)[-1].lstrip("/")
        first = path.split("/", 1)[0].split("?", 1)[0].lower()

        if not first or first in {"offers", "customer-offer"} or first.startswith("campaign-list"):
            return (SCOPE_SITEWIDE, None)

        if first in set(self.CATEGORY_SLUGS.values()):
            return (SCOPE_CATEGORY, first.replace("-", " "))

        return (None, None)

    def extract_campaign_directory(self, response) -> List[Campaign]:
        """Every offer advertised on a Boots offers page.

        Sale pages are also product grids, so links to a single product are
        skipped: a product tile is not a campaign.
        """
        return [c for c in self._scan_offer_links(response)
                if not self.is_product_url(c.landing_url or "")]

    def offer_page_links(self, response) -> List[str]:
        """Every offers or sale page linked from this page.

        Links carrying a query are product lists filtered to one offer, not
        offers pages, so they are left out.
        """
        return [url for url in self._links(response)
                if "?" not in url
                and not self.is_product_url(url)
                and re.search(r"offer|/sale(/|$)", urlparse(url).path)]

    def promotion_key(self, campaign) -> Optional[str]:
        """Boots' own name for an offer, read from its "shop now" link.

        The link goes to a product list filtered to the offer, for example
        ?criteria.promotionalText=Save+up+to+20+percent+on+selected+fragrance,
        and that text is word for word the line each product in the offer
        shows on its own page.
        """
        values = parse_qs(urlparse(campaign.landing_url or "").query).get(
            "criteria.promotionalText")
        return values[0] if values else None

    def configure_session(self, manager) -> None:
        """Use a stealth browser session, kept alive across the whole crawl.

        Two things matter here. The session must persist, because the first
        navigation is what earns the cookie the later requests need -- fetching
        each page in a fresh browser gets every one of them challenged. And
        `google_search` sets a search-engine referer, which a real visitor
        would usually have.
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
                # proxy="http://dmrlxjhc:2unx9qvddqjo@31.59.20.176:6754/",
            ),
        )

    # --- discovery --------------------------------------------------------

    @staticmethod
    def _brand_slug(brand: str) -> str:
        """Brand name -> the slug Boots uses in its URLs ("Jo Malone" -> jo-malone)."""
        return re.sub(r"[^a-z0-9]+", "-", brand.strip().lower()).strip("-")

    def campaign_seed_urls(self, brands: Sequence[str]) -> List[str]:
        """None. Brand pages are crawled as listings, which read their
        campaigns too, so seeding them here would only queue them twice."""
        return []

    def product_listing_urls(
        self, brand: str, categories: Sequence[str], max_pages: int
    ) -> List[ListingPage]:
        """Where the crawl starts for one brand.

        Without categories it is the brand page. That page only shows a few
        featured products, but it links every product list for the brand,
        and `more_listing_pages` follows those. The list names cannot be
        guessed: Clinique has /clinique/clinique-full-range, MAC has
        /mac/mac-shop-all, Tom Ford has /tom-ford-/tom-ford-all-fragrances.

        With categories it is the first page of each category list, e.g.
        /clinique/clinique-skincare.
        """
        slug = self._brand_slug(brand)
        if not categories:
            return [ListingPage(url=f"https://{self.domain}/{slug}", brand=brand)]

        pages = []
        for category in categories:
            leaf = self.CATEGORY_SLUGS.get(category.strip().lower(), self._brand_slug(category))
            if not leaf.startswith(slug):
                leaf = f"{slug}-{leaf}"
            pages.append(ListingPage(url=f"https://{self.domain}/{slug}/{leaf}",
                                     brand=brand, category=category))
        return pages

    def more_listing_pages(self, response, meta: dict, added: int) -> List[str]:
        """What to crawl after a Boots listing page.

        A brand page (/tom-ford, one path segment): every product list it
        links to. A list page (/tom-ford-/tom-ford-all-fragrances): its next
        page, as long as this page added products. Past the last page Boots
        returns an empty page, so that is where it stops.
        """
        requested = urlparse(meta.get("url") or str(response.url))
        path = requested.path.strip("/")

        if "/" not in path:
            if meta.get("category"):
                return []  # a category filter was asked for; do not widen it
            # read the brand's folder after redirects: /tom-ford -> /tom-ford-
            root = urlparse(str(response.url)).path.strip("/")
            lists = [url for url in self._links(response)
                     if url.startswith(f"https://{self.domain}/{root}/")
                     and "?" not in url and not self.is_product_url(url)]
            if not lists:
                self._featured_only.add(meta.get("brand"))
            return lists

        page = int(parse_qs(requested.query).get("paging.index", ["0"])[0])
        if added and page + 1 < self.MAX_LIST_PAGES:
            return [f"https://{self.domain}/{path}?paging.index={page + 1}"
                    f"&paging.size={self.listing_page_size}"]
        return []

    def crawl_warnings(self) -> List[str]:
        return [f"{brand}: its Boots brand page links no product lists, so only "
                f"the featured products on that page were collected"
                for brand in sorted(self._featured_only)]

    def extract_product_links(self, response, brand: Optional[str] = None) -> List[str]:
        """Product URLs linked from a listing page, de-duplicated in page order.

        No brand filter is applied: Boots listing pages are already scoped to
        one brand, and its product slugs do not reliably contain the brand
        name, so filtering on it would drop genuine products.
        """
        return list(dict.fromkeys(
            canonical_url(url) for url in self._links(response) if self.is_product_url(url)
        ))

    def _links(self, response) -> List[str]:
        """Every boots.com link on a page, made absolute, once each, in page order."""
        found = []
        for node in response.css("a"):
            href = (node.attrib.get("href") or "").split("#")[0]
            if href.startswith("/"):
                href = f"https://{self.domain}{href}"
            if urlparse(href).netloc.endswith("boots.com"):
                found.append(href)
        return list(dict.fromkeys(found))

    def product_id_from_url(self, url: str) -> Optional[str]:
        """Public form of the id helper, used to join sitemap-discovered
        products to the category listings they also appear on."""
        return self._product_id_from_url(url)

    def is_product_url(self, url: str) -> bool:
        return bool(_PRODUCT_URL_RE.match(url.split("?")[0]))

    def select_candidates(self, urls: Iterable[str], brands: Sequence[str]) -> List[str]:
        """Filter URLs to those whose slug mentions a target brand.

        Only used on the sitemap route, which Boots does not take. Kept so the
        adapter satisfies the interface and still behaves sensibly if a
        sitemap becomes reachable later.
        """
        slugs = [self._brand_slug(b) for b in brands]
        return [
            u for u in urls
            if self.is_product_url(u) and any(s in u.lower() for s in slugs)
        ]

    @staticmethod
    def looks_blocked(response) -> bool:
        """True when this is the bot-protection challenge rather than content.

        Worth checking explicitly: the challenge is served as HTTP 200, so
        without this the crawler would treat it as a page with no product on
        it and quietly report zero results.
        """
        try:
            body = response.body.decode("utf-8", "replace")
        except (AttributeError, UnicodeDecodeError):
            return False
        return _BLOCK_MARKER in body

    # --- product extraction ----------------------------------------------

    def extract_product(
        self, response, target_brands: Sequence[str] = ()
    ) -> Optional[Product]:
        # Boots always publishes itemprop="Brand", so no fallback is needed
        # and target_brands goes unused here.
        if self.looks_blocked(response):
            return None
        return self.parse_product(response, str(response.url))

    def parse_product(self, sel, url: str) -> Optional[Product]:
        """Build a Product from a Boots product page.

        Split out from `extract_product` so it can run against saved HTML.
        """
        product_id = self._product_id_from_url(url)
        if not product_id:
            return None

        title = clean_text(self._itemprop(sel, "name"))
        if not title:
            return None  # not a product page

        # The retailer's own microdata, hidden in the markup for search
        # engines. This is the retailer categorising its own product, which is
        # what makes it usable as proof of brand.
        brand = clean_text(self._itemprop(sel, "Brand"))
        verified_by = "microdata_brand" if brand else "unverified"

        current_price, _ = parse_price(self._itemprop(sel, "price"))
        currency = clean_text(self._itemprop(sel, "priceCurrency"))

        availability = None
        raw_availability = self._itemprop(sel, "availability")
        if raw_availability:
            # "http://schema.org/InStock" -> "InStock"
            availability = str(raw_availability).rsplit("/", 1)[-1]

        original_price, saved_amount = self._extract_was_and_saving(sel)

        promo_text = self._product_promotion_text(sel)

        return Product(
            retailer=self.display_name,
            brand=brand or "",
            brand_verified_by=verified_by,
            product_id=product_id,
            product_title=title,
            product_url=canonical_url(url),
            source_url=url,
            currency=currency,
            current_price=current_price,
            original_price=original_price,
            discount_amount=saved_amount,
            discount_percent=percent_off(original_price, current_price),
            availability=availability,
            # Boots sells each shade/size as its own product page with its own
            # id, so a page is always exactly one variant. There is no
            # equivalent of Lookfantastic's shade group.
            variant_count=1,
            sku=product_id,
            promotional_copy=promo_text,
            scraped_at=datetime.now(timezone.utc).isoformat(),
        )

    # --- campaign extraction ---------------------------------------------

    def extract_campaigns(self, response) -> List[Campaign]:
        if self.looks_blocked(response):
            return []
        return self.parse_campaigns(response, str(response.url))

    def parse_campaigns(self, sel, url: str) -> List[Campaign]:
        """Collect the offers Boots lists against a page.

        Boots also exposes its internal buyer codes for the same promotions
        (`#promoNamesStringArray`, e.g. "RX UK PB Clinique only 25pnd
        BLOCKBUSTER"). Those are deliberately ignored -- they are warehouse
        jargon, not something to put in front of a brand team, and they are
        not reliably about the product whose page they appear on.
        """
        campaigns: List[Campaign] = []
        seen: set = set()

        for node in sel.css("li.pdp-promotion-redesign"):
            text = clean_text(node.get_all_text())
            if not text or text in seen or not looks_like_offer(text):
                continue
            seen.add(text)
            campaigns.append(
                Campaign(
                    retailer=self.display_name,
                    promotion_text=text,
                    promotion_type=classify_promotion(text),
                    # Boots renders this list as the promotions the product
                    # itself qualifies for, so product scope is what the page
                    # actually asserts. The copy may still mention other
                    # brands ("selected premium beauty and No7") -- that is the
                    # retailer's wording and is preserved verbatim rather than
                    # reinterpreted into a scope we cannot prove.
                    scope=SCOPE_PRODUCT,
                    promo_code=extract_promo_code(text),
                    source_url=url,
                )
            )
        return campaigns

    # --- helpers ----------------------------------------------------------

    @staticmethod
    def _itemprop(sel, prop: str) -> Optional[str]:
        """Read a schema.org microdata value.

        The value lives in whichever attribute suits the element: `content` on
        a `<meta>`, `href` on a `<link>`, and the element text otherwise.
        """
        for node in sel.css(f"[itemprop='{prop}']"):
            for attribute in ("content", "href"):
                value = node.attrib.get(attribute)
                if value:
                    return value
            text = node.get_all_text()
            if text and text.strip():
                return text
        return None

    @staticmethod
    def _product_id_from_url(url: str) -> Optional[str]:
        match = _PRODUCT_URL_RE.match(url.split("?")[0])
        return match.group(1) if match else None

    @staticmethod
    def _extract_was_and_saving(sel) -> tuple:
        """Read the "Was £43.00" and "Save £10.75" figures from the price panel.

        Both are optional: an undiscounted product shows neither.
        """
        original = saved = None

        for node in sel.css(".was_price"):
            match = _WAS_RE.search(clean_text(node.get_all_text()) or "")
            if match:
                original, _ = parse_price(match.group(1))
                break

        for node in sel.css(".saving"):
            match = _SAVE_RE.search(clean_text(node.get_all_text()) or "")
            if match:
                saved, _ = parse_price(match.group(1))
                break

        return original, saved

    @staticmethod
    def _product_promotion_text(sel) -> Optional[str]:
        """The customer-facing promotional copy shown against this product."""
        texts = []
        for node in sel.css("li.pdp-promotion-redesign"):
            text = clean_text(node.get_all_text())
            if text and looks_like_offer(text) and text not in texts:
                texts.append(text)
        # Several offers can apply at once; keep them all, joined readably.
        return " | ".join(texts) if texts else None
