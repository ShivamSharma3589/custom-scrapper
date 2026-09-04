"""Lookfantastic adapter.

Every selector in this file was read off the live Lookfantastic HTML, not
assumed. The notes below record what the page actually provides, because that
is what makes the extraction safe to change later.

Product pages (`/p/<slug>/<id>/`) carry two JSON-LD blocks:

  * The product itself, whose @type tells us the variant situation outright:
      - `Product`      : a single variant. Has `sku` and `offers`
                         (price, currency, availability).
      - `ProductGroup` : multiple variants. Has `productGroupID` and
                         `hasVariant` (one entry per shade, each with its own
                         sku and price), and a null `offers`.
  * A `BreadcrumbList`. One of its entries links to `/c/brands/<slug>/`, which
    is the retailer's own brand taxonomy. That link -- not the URL slug, not
    the page title -- is what we treat as proof of brand.

Prices are NOT in the JSON-LD for the discounted case (it carries only the
current price), so they are read from `#product-price`, where screen-reader
labels ("Recommended Retail Price:", "Current price:") identify which number
is which. Those labels are far more stable than the utility CSS classes
around them.

Promotions appear in two structurally distinct places, which is what lets us
separate product-level offers from site-wide ones without guessing:

  * `#pap-banner` / `[data-track="promoClick"]` -- the offer that applies to
    THIS product, with the copy in `data-track-push`.
  * `a.strip-banner` -- the site header strip, shown on every page. This is a
    site-wide campaign and must never be recorded as a brand's campaign.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..models import (
    SCOPE_BRAND,
    SCOPE_CATEGORY,
    SCOPE_PRODUCT,
    SCOPE_SITEWIDE,
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
from ..promotions import classify_promotion, looks_like_offer
from .base import ListingPage, RetailerAdapter, register

# `/p/<slug>/<numeric id>/` -- the trailing number is Lookfantastic's stable
# product id and is what we use for identity and deduplication. The same
# product is reachable under several slugs, but the id does not change.
_PRODUCT_URL_RE = re.compile(r"/p/(?:[^/]+/)*?([0-9]{5,})/?$")

# A brand landing page: exactly one path segment under /c/brands/.
_BRAND_PAGE_RE = re.compile(r"/c/brands/([^/]+)/?$")

# "Save £9.75" and "( 25% Off )" inside the price block.
_SAVE_AMOUNT_RE = re.compile(r"Save\s*[£$€]?\s*([\d.,]+)", re.IGNORECASE)
_SAVE_PERCENT_RE = re.compile(r"\(\s*([\d.]+)\s*%\s*Off\s*\)", re.IGNORECASE)


@register
class LookfantasticAdapter(RetailerAdapter):
    """Extraction rules for lookfantastic.com."""

    name = "lookfantastic"
    domain = "www.lookfantastic.com"
    display_name = "Lookfantastic"

    # Category pages exist at /c/brands/<brand>/<category>/ and are the only
    # place Lookfantastic states a product's category: the product page itself
    # carries no category taxonomy at all (every "Skincare" on it is site
    # navigation or another brand's name).
    supports_categories = True

    # Those category pages render their grid with JavaScript, so listings need
    # a browser -- while product pages are plain HTML and do not. Splitting the
    # sessions means we pay for a browser on a handful of listing pages
    # instead of on every product.
    listing_session_id = "browser"

    #: Category slugs as Lookfantastic spells them, keyed by the general name
    #: a user is likely to type. Anything not in here is passed through as-is.
    CATEGORY_SLUGS = {
        "skincare": "skincare",
        "makeup": "makeup",
        "make-up": "makeup",
        "make up": "makeup",
        "fragrance": "fragrance",
        "haircare": "haircare",
        "hair": "haircare",
        "men": "mens",
        "mens": "mens",
        "gift": "gift",
        "gifts": "gift",
    }

    def configure_session(self, manager) -> None:
        """Plain HTTP by default, plus a lazily-started browser for listings.

        The browser session is marked lazy so it is only launched if a
        category crawl actually needs it -- a sitemap-driven run never pays
        the startup cost.
        """
        from scrapling.fetchers import AsyncDynamicSession, FetcherSession

        manager.add("default", FetcherSession(), default=True)
        manager.add(
            self.listing_session_id,
            AsyncDynamicSession(headless=True, network_idle=True, timeout=90_000),
            lazy=True,
        )

    def product_listing_urls(
        self, brand: str, categories: Sequence[str], max_pages: int
    ) -> List[ListingPage]:
        """Category pages for one brand.

        Only used when a category filter is in play. Without one, the sitemap
        is a far cheaper and more complete route.
        """
        if not categories:
            return []

        slug = self._brand_slug(brand)
        pages = []
        for category in categories:
            category_slug = self.CATEGORY_SLUGS.get(
                category.strip().lower(), self._brand_slug(category)
            )
            pages.append(
                ListingPage(
                    url=f"https://{self.domain}/c/brands/{slug}/{category_slug}/",
                    brand=brand,
                    category=category,
                )
            )
        return pages

    def extract_product_links(self, response, brand: Optional[str] = None) -> List[str]:
        """Product URLs from a rendered category page.

        Category pages carry cross-sell carousels above the grid -- beauty
        boxes, mystery boxes and gift vouchers, none of them the brand being
        crawled. Without a filter those consume the crawl budget before the
        real products are reached, and are then rejected as unverifiable
        brands, so the run does a lot of work to produce nothing.

        Filtering on the brand slug is the same cheap pre-filter the sitemap
        route uses. It is not verification: the brand is still proved from the
        breadcrumb on each product page.
        """
        slug = self._brand_slug(brand) if brand else None
        found = []
        for node in response.css("a"):
            href = node.attrib.get("href") or ""
            if href.startswith("/"):
                href = f"https://{self.domain}{href}"
            if not (href.startswith("http") and self.is_product_url(href)):
                continue
            if slug and slug not in href.lower():
                continue
            clean = canonical_url(href)
            if clean not in found:
                found.append(clean)
        return found

    # Product discovery comes from the sitemap declared in robots.txt. This is
    # deliberate: the brand landing pages render their grid with JavaScript, so
    # a static fetch of /c/brands/clinique/ yields only a handful of carousel
    # items mixed with beauty boxes and gift vouchers. The sitemap gives the
    # complete catalogue in one request, and is published by the retailer for
    # exactly this purpose.
    sitemap_urls = ["https://www.lookfantastic.com/sitemapindex-product.xml.gz"]

    def campaign_seed_urls(self, brands: Sequence[str]) -> List[str]:
        """Brand landing pages, which carry the brand-level promotional copy."""
        return [
            f"https://www.lookfantastic.com/c/brands/{self._brand_slug(brand)}/"
            for brand in brands
        ]

    @staticmethod
    def _brand_slug(brand: str) -> str:
        """Turn a brand name into the slug Lookfantastic uses in its URLs."""
        return re.sub(r"[^a-z0-9]+", "-", brand.strip().lower()).strip("-")

    # --- discovery --------------------------------------------------------

    def product_id_from_url(self, url: str) -> Optional[str]:
        """Public form of the id helper, used to join sitemap-discovered
        products to the category listings they also appear on."""
        return self._product_id_from_url(url)

    def is_product_url(self, url: str) -> bool:
        return bool(_PRODUCT_URL_RE.search(url.split("?")[0]))

    def select_candidates(self, urls: Iterable[str], brands: Sequence[str]) -> List[str]:
        """Pick sitemap URLs whose slug mentions one of the target brands.

        This is only a cheap pre-filter so we do not fetch all 16,000 products
        to find one brand's few hundred. It is NOT brand verification -- the
        brand is proved from the breadcrumb on each product page, and any
        candidate that turns out to belong to another brand is rejected there.
        """
        slugs = [self._brand_slug(b) for b in brands]
        selected = []
        for url in urls:
            if not self.is_product_url(url):
                continue
            lowered = url.lower()
            if any(slug in lowered for slug in slugs):
                selected.append(url)
        return selected

    # --- product extraction ----------------------------------------------

    def extract_product(self, response, target_brands: Sequence[str] = ()) -> Optional[Product]:
        return self.parse_product(response, str(response.url), target_brands)

    def parse_product(self, sel, url: str, target_brands: Sequence[str] = ()) -> Optional[Product]:
        """Build a Product from a parsed product page.

        Split out from `extract_product` so it can be exercised against saved
        HTML without a live request.
        """
        blocks = self._json_ld_blocks(sel)
        # A single-variant page is a `Product`; a shade/size range is a
        # `ProductGroup`. Both describe one purchasable listing to us.
        product_ld = self._find_type(blocks, "Product") or self._find_type(blocks, "ProductGroup")
        if product_ld is None:
            return None  # not a product page

        product_id = self._product_id_from_url(url)
        if not product_id:
            return None

        brand, verified_by = self._verify_brand(blocks)
        if not brand:
            # Some product pages ship a truncated breadcrumb ("Home > product")
            # with no brand link at all, and carry no other machine-readable
            # brand for themselves -- every /c/brands/ link on them is global
            # navigation. Rejecting those loses genuine products, so fall back
            # to requiring TWO independent retailer-authored signals to agree:
            # the title must start with the brand AND the canonical URL slug
            # must too. Either alone is untrustworthy, which is why neither is
            # used alone. The weaker provenance is recorded distinctly so a
            # consumer can filter it out (see --strict-brand).
            brand, verified_by = self._verify_brand_weakly(
                clean_text(product_ld.get("name")), url, target_brands
            )
        variants = product_ld.get("hasVariant") or []
        variant_count = len(variants) if variants else 1

        # Prices come from the rendered price block, which is the only place
        # the RRP appears. The JSON-LD offer carries the current price only.
        original_price, current_price, currency = self._extract_prices(sel)
        saved_amount, stated_percent = self._extract_saving(sel)

        # Fall back to the JSON-LD offer if the price block was unreadable, so
        # a layout tweak degrades the record rather than losing it entirely.
        offer = product_ld.get("offers")
        if isinstance(offer, list):
            offer = offer[0] if offer else None
        if current_price is None and isinstance(offer, dict):
            current_price = self._as_float(offer.get("price"))
            currency = currency or offer.get("priceCurrency")

        # For a multi-variant product there is no single meaningful SKU --
        # reporting one shade's SKU would misrepresent a 33-shade foundation as
        # one item. We report the group id instead and leave `sku` empty.
        sku = product_ld.get("sku") if variant_count == 1 else None
        if sku is None and variant_count == 1 and isinstance(offer, dict):
            sku = offer.get("sku")

        # "https://schema.org/InStock" -> "InStock". A ProductGroup has no
        # offer of its own, so a range counts as available when any one of its
        # shades is in stock.
        availability = None
        if isinstance(offer, dict) and offer.get("availability"):
            availability = str(offer["availability"]).rsplit("/", 1)[-1]
        elif variants:
            states = set()
            for variant in variants:
                variant_offer = variant.get("offers") if isinstance(variant, dict) else None
                if isinstance(variant_offer, list):
                    variant_offer = variant_offer[0] if variant_offer else None
                if isinstance(variant_offer, dict) and variant_offer.get("availability"):
                    states.add(str(variant_offer["availability"]).rsplit("/", 1)[-1])
            if states:
                availability = "InStock" if "InStock" in states else sorted(states)[0]

        promo_text = self._product_promotion_text(sel)

        return Product(
            retailer=self.display_name,
            brand=brand or "",
            brand_verified_by=verified_by,
            product_id=product_id,
            product_title=clean_text(product_ld.get("name")) or "",
            product_url=canonical_url(url),
            source_url=url,
            currency=currency,
            current_price=current_price,
            original_price=original_price,
            discount_amount=saved_amount,
            discount_percent=(
                stated_percent
                if stated_percent is not None
                else percent_off(original_price, current_price)
            ),
            availability=availability,
            variant_count=variant_count,
            sku=str(sku) if sku else None,
            promotional_copy=promo_text,
            scraped_at=datetime.now(timezone.utc).isoformat(),
        )

    # --- campaign extraction ---------------------------------------------

    #: The offers hub, plus the standing sale landing pages it links to. Taken
    #: from the `sitemap-list` sitemap, which lists 57 offer pages; these are
    #: the hubs that lead to the rest rather than the whole set.
    CAMPAIGN_HUB_PATHS = [
        "/c/health-beauty/offers/view-all/",
        "/c/health-beauty/offers/",
        "/c/offers/sale/",
    ]

    def campaign_discovery_urls(self) -> List[str]:
        """Offer hub pages, for a campaigns-only run."""
        return [f"https://{self.domain}{path}" for path in self.CAMPAIGN_HUB_PATHS]

    def scope_for_href(self, href: str):
        """Read an offer's reach from where its link points.

        Lookfantastic's URL structure states this plainly, so it can be
        asserted rather than guessed:

            /c/brands/<brand>/...          -> that brand
            /c/health-beauty/<category>/   -> that department
            /c/offers/...                  -> the whole site

        Anything else is left unresolved rather than assumed.
        """
        path = href.split(f"{self.domain}", 1)[-1]

        brand = re.search(r"/c/brands/([^/]+)/", path)
        if brand:
            return (SCOPE_BRAND, brand.group(1).replace("-", " ").title())

        category = re.search(r"/c/health-beauty/([^/]+)/", path)
        # "offers" and "sale" sit in the category slot but name no department:
        # /c/health-beauty/offers/winter-sale/ is the site sale, not a
        # "category: offers" campaign.
        if category and category.group(1) not in {"offers", "offer", "sale"}:
            return (SCOPE_CATEGORY, category.group(1).replace("-", " "))

        if "/c/offers/" in path or "/offers/" in path or "/sale/" in path:
            return (SCOPE_SITEWIDE, None)

        return (None, None)

    def extract_campaign_directory(self, response) -> List[Campaign]:
        """Every offer advertised on this page's links.

        Lookfantastic advertises its live campaigns in the site navigation
        (`a.navigation-item`), which means one fetch of the offers hub returns
        the current campaign set. That nav is on every page, which is exactly
        why this is not part of `extract_campaigns` -- doing it there would
        staple the whole offer menu onto every product.
        """
        return self._scan_offer_links(response)

    def extract_campaigns(self, response) -> List[Campaign]:
        return self.parse_campaigns(response, str(response.url))

    def parse_campaigns(self, sel, url: str) -> List[Campaign]:
        """Collect promotions from a page, each with an honest scope.

        The two sources are structurally different elements, which is what
        allows the scope to be asserted rather than guessed.
        """
        campaigns: List[Campaign] = []
        seen: set = set()

        def add(text: Optional[str], scope: str, landing: Optional[str] = None) -> None:
            cleaned = clean_text(text)
            if not cleaned or cleaned in seen:
                return
            # These elements also carry merchandising badges ("NEW IN",
            # "BESTSELLER") that are not offers. Requiring some sign of an
            # actual promotion keeps those out of the campaign list.
            if not looks_like_offer(cleaned):
                return
            seen.add(cleaned)
            campaigns.append(
                Campaign(
                    retailer=self.display_name,
                    promotion_text=cleaned,
                    promotion_type=classify_promotion(cleaned),
                    scope=scope,
                    scope_value=None,
                    promo_code=extract_promo_code(cleaned),
                    source_url=url,
                    landing_url=landing,
                )
            )

        # Site-wide header strip. Present on every page, so its reach is the
        # whole site regardless of which brand's page we happen to be on.
        for node in sel.css("a.strip-banner"):
            href = node.attrib.get("href")
            landing = f"https://{self.domain}{href}" if href and href.startswith("/") else href
            add(node.get_all_text(), SCOPE_SITEWIDE, landing)

        # The offer attached to this specific product, taken from the PDP's own
        # promotion banner. Only meaningful on a product page.
        if self.is_product_url(url):
            add(self._product_promotion_text(sel), SCOPE_PRODUCT)

        return campaigns

    # --- helpers ----------------------------------------------------------

    @staticmethod
    def _json_ld_blocks(sel) -> List[Dict[str, Any]]:
        """Parse every JSON-LD script on the page into dicts.

        Malformed blocks are skipped rather than raising: one bad block should
        not cost us the whole page.
        """
        blocks: List[Dict[str, Any]] = []
        for node in sel.css('script[type="application/ld+json"]'):
            raw = node.text
            if not raw:
                continue
            try:
                parsed = json.loads(raw.strip())
            except (ValueError, TypeError):
                continue
            blocks.extend(parsed if isinstance(parsed, list) else [parsed])
        return blocks

    @staticmethod
    def _find_type(blocks: List[Dict[str, Any]], wanted: str) -> Optional[Dict[str, Any]]:
        """Return the first JSON-LD block whose @type matches."""
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("@type")
            types = block_type if isinstance(block_type, list) else [block_type]
            if wanted in types:
                return block
        return None

    @classmethod
    def _verify_brand(cls, blocks: List[Dict[str, Any]]) -> tuple:
        """Establish the brand from the breadcrumb's link into /c/brands/.

        Returns (brand_name, how_verified). The breadcrumb is the retailer's
        own categorisation of the product, which is why it counts as proof
        where a URL slug or a title match would not.
        """
        crumbs = cls._find_type(blocks, "BreadcrumbList")
        if not crumbs:
            return None, "unverified"

        for entry in crumbs.get("itemListElement") or []:
            if not isinstance(entry, dict):
                continue
            item = entry.get("item")
            item_id = item.get("@id") if isinstance(item, dict) else item
            if item_id and _BRAND_PAGE_RE.search(str(item_id)):
                name = clean_text(entry.get("name"))
                if name:
                    return name, "breadcrumb_link"
        return None, "unverified"

    @classmethod
    def _verify_brand_weakly(
        cls, title: Optional[str], url: str, target_brands: Sequence[str]
    ) -> tuple:
        """Corroborate one of the REQUESTED brands from the title and the URL.

        Only used when the page offers no breadcrumb brand link. Returns
        (brand, "title_and_url") when a requested brand both starts the title
        and appears in the URL slug, else (None, "unverified").

        Two properties make this safe enough to use as a fallback. It tests
        only brands we were already asked about, so it can never invent one.
        And it needs the retailer to have described the same product the same
        way twice, independently -- a page that merely mentions a brand in
        marketing copy satisfies neither condition, and a product whose own
        name happens to match its slug ("The Summer Edit") is not in the
        requested list and so cannot match at all.
        """
        if not title or not target_brands:
            return None, "unverified"

        path = url.split("?")[0].lower()
        title_folded = title.casefold()

        for brand in target_brands:
            slug = cls._brand_slug(brand)
            if not slug:
                continue
            # The title must START with the brand, not merely contain it.
            if not title_folded.startswith(brand.strip().casefold()):
                continue
            if f"/{slug}-" in path or f"/{slug}/" in path:
                return brand, "title_and_url"
        return None, "unverified"

    @staticmethod
    def _product_id_from_url(url: str) -> Optional[str]:
        match = _PRODUCT_URL_RE.search(url.split("?")[0])
        return match.group(1) if match else None

    @staticmethod
    def _as_float(value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_prices(sel) -> tuple:
        """Read RRP and current price from `#product-price`.

        The block interleaves screen-reader labels with the values:

            <span class="sr-only">Recommended Retail Price:</span>
            <span>£42.00</span>
            <span class="sr-only">Current price:</span>
            <span>£31.50</span>

        so we walk the spans in order and let each label claim the next value.
        Keying on the label text rather than on the utility classes means a
        restyle does not silently swap the two numbers.

        Three layouts occur in practice and all three must work:
          1. discounted    -- both labels present
          2. full price    -- only the "Current price:" label
          3. full price    -- no labels at all, just one bare price span
        """
        original = current = None
        currency = None
        pending: Optional[str] = None
        unlabelled: List[float] = []

        for span in sel.css("#product-price span"):
            text = clean_text(span.get_all_text())
            if not text:
                continue

            lowered = text.lower()
            if lowered.startswith("recommended retail price"):
                pending = "original"
                continue
            if lowered.startswith("current price"):
                pending = "current"
                continue

            amount, symbol_currency = parse_price(text)
            if amount is None:
                continue
            currency = currency or symbol_currency

            if pending == "original":
                original = amount
            elif pending == "current":
                current = amount
            else:
                unlabelled.append(amount)
            pending = None

        # Layout 3: an undiscounted product shows a single unlabelled price.
        # Take the first amount in the block -- the only one on offer.
        if current is None and unlabelled:
            current = unlabelled[0]

        # Defensive: if only an RRP was labelled, it is the price being asked.
        if current is None and original is not None:
            current, original = original, None

        return original, current, currency

    @staticmethod
    def _extract_saving(sel) -> tuple:
        """Read the retailer's own advertised saving, e.g. "Save £9.75 (25% Off)".

        Keeping the stated percentage lets validation cross-check it against
        the percentage implied by the two prices.
        """
        nodes = sel.css("#product-price")
        if not nodes:
            return None, None

        text = clean_text(nodes[0].get_all_text()) or ""
        amount_match = _SAVE_AMOUNT_RE.search(text)
        percent_match = _SAVE_PERCENT_RE.search(text)

        amount = None
        if amount_match:
            amount, _ = parse_price(amount_match.group(1))

        percent = float(percent_match.group(1)) if percent_match else None
        return amount, percent

    @staticmethod
    def _product_promotion_text(sel) -> Optional[str]:
        """The promotional copy that applies to this product specifically.

        Anchored on `[data-e2e="pdp-pap-banner"]`, which appears exactly once
        per product page. The looser `[data-track="promoClick"]` must NOT be
        used here: a product page carries seven of them, six belonging to the
        recommended products in the "you may also like" rails. Reading those
        would attach another product's offer to this one -- real text from the
        page describing something that isn't true of this product.
        """
        for node in sel.css("[data-e2e='pdp-pap-banner']"):
            text = clean_text(node.attrib.get("data-track-push"))
            # The same banner slot is reused for merchandising badges ("NEW
            # IN", "BESTSELLER"). Those are not offers, and letting one land
            # in promotional_copy tells a brand team a product is promoted
            # when it is not.
            if text and looks_like_offer(text):
                return text
        return None
