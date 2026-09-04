"""AllBeauty adapter.

The fourth retailer, and the one that proves the framework is not merely a
"parse HTML" pipeline: AllBeauty is a Shopify store, so its catalogue is
available as JSON and there is nothing to scrape at all.

    https://allbeauty.com/collections/<brand>/products.json?limit=250&page=N

One request returns up to 250 complete products. Each carries everything the
model needs, stated by the retailer rather than inferred:

    vendor            -> the brand ("M.A.C", "Estée Lauder")
    product_type      -> the category ("Cosmetics > Eyeliner")
    variants[].price  -> the current price
    variants[].compare_at_price -> the was-price
    variants[].sku, .available, .title

That makes discovery roughly 70x cheaper than the page-per-product retailers:
475 products across seven brands in seven requests, against 1,083 page
fetches taking an hour on Lookfantastic.

Because the listing IS the data, this adapter implements
`extract_products_from_listing` rather than `extract_product_links`, and the
crawler never fetches an individual product page.

Two details found by inspection, both of which the brand matcher already
handles and neither of which should be "fixed" here:

  * the vendor for MAC is "M.A.C", with dots
  * the vendor for Estee Lauder is "Estée Lauder", with an accent

The shop is registered in Guernsey but trades in GBP (`/meta.json`), so
prices need no conversion.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..models import SCOPE_SITEWIDE, Campaign, Product
from ..normalize import canonical_url, clean_text, extract_promo_code
from ..promotions import classify_promotion, is_confident_offer
from .base import ListingPage, RetailerAdapter, register

# Product pages are /products/<handle>; the numeric id lives in the JSON.
_PRODUCT_URL_RE = re.compile(
    r"^https?://(?:www\.)?allbeauty\.com/products/([a-z0-9-]+)/?$", re.I
)

#: Shopify caps a collection page at 250 products.
_PAGE_SIZE = 250


@register
class AllBeautyAdapter(RetailerAdapter):
    """Extraction rules for allbeauty.com (Shopify)."""

    name = "allbeauty"
    domain = "allbeauty.com"
    display_name = "AllBeauty"

    # No sitemap route: the Shopify collection API is both cheaper and
    # brand-scoped, which the sitemap is not.
    sitemap_urls: List[str] = []

    # The category is stated on every product as `product_type`, so a filter
    # is applied to the data we already have rather than by choosing a
    # different listing.
    supports_categories = True

    #: Brand -> Shopify collection handle, where it differs from the slug.
    #: Verified against the live store: each of these collections contains
    #: exactly one vendor.
    COLLECTION_HANDLES = {
        "mac": "mac",                    # vendor is "M.A.C"
        "jo malone": "jo-malone",        # not "jo-malone-london" here
        "jo malone london": "jo-malone",
        "estee lauder": "estee-lauder",  # vendor is "Estée Lauder"
    }

    CATEGORY_SLUGS = {
        "skincare": "skincare",
        "makeup": "cosmetics",
        "make-up": "cosmetics",
        "make up": "cosmetics",
        "fragrance": "fragrance",
        "haircare": "haircare",
        "hair": "haircare",
        "men": "mens",
        "mens": "mens",
        "gift": "gifts",
        "gifts": "gifts",
    }

    #: Collections that list what the store is promoting. Both verified to
    #: exist; `/collections/special-offers` and `/clearance` return 404.
    CAMPAIGN_HUB_PATHS = ["/collections/offers", "/collections/sale"]

    def configure_session(self, manager) -> None:
        """Plain HTTP. There is no JavaScript to run and no bot wall."""
        from scrapling.fetchers import FetcherSession

        manager.add("default", FetcherSession())

    # --- discovery --------------------------------------------------------

    @staticmethod
    def _brand_slug(brand: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", brand.strip().lower()).strip("-")

    def _collection(self, brand: str) -> str:
        """The collection handle for a brand."""
        key = brand.strip().lower()
        return self.COLLECTION_HANDLES.get(key, self._brand_slug(brand))

    def product_listing_urls(
        self, brand: str, categories: Sequence[str], max_pages: int
    ) -> List[ListingPage]:
        """Paginated JSON for one brand's collection.

        Categories are deliberately not part of the URL: `product_type` is on
        every product, so filtering happens on the data rather than by picking
        a narrower collection. That keeps one request serving every category.
        """
        handle = self._collection(brand)
        return [
            ListingPage(
                url=(
                    f"https://{self.domain}/collections/{handle}/products.json"
                    f"?limit={_PAGE_SIZE}&page={page}"
                ),
                brand=brand,
                category=None,
            )
            for page in range(1, max_pages + 1)
        ]

    def campaign_discovery_urls(self) -> List[str]:
        return [f"https://{self.domain}{path}" for path in self.CAMPAIGN_HUB_PATHS]

    def campaign_seed_urls(self, brands: Sequence[str]) -> List[str]:
        """The offer hubs, visited on a normal brand run too.

        Every other adapter picks up campaigns for free, because the listings
        it crawls are HTML and the promotional copy is on them. AllBeauty's
        listings are `products.json` -- pure data, with no promotional copy
        anywhere in it -- so without seeding these a brand run reports zero
        campaigns for a site that is in fact running ten.

        These are HTML pages and distinct from the JSON listing URLs, so
        there is no risk of one URL being queued with two callbacks.
        """
        return self.campaign_discovery_urls()

    def scope_for_href(self, href: str):
        """AllBeauty's offer links are collection pages.

        A collection can be a brand or a theme and the URL does not say which,
        so anything that is not clearly the sitewide sale is left unresolved
        rather than guessed into a brand campaign.
        """
        path = href.split(self.domain, 1)[-1]
        if re.search(r"/collections/(sale|offers)\b", path):
            return (SCOPE_SITEWIDE, None)
        return (None, None)

    def extract_campaign_directory(self, response) -> List[Campaign]:
        return self._scan_offer_links(response)

    def is_product_url(self, url: str) -> bool:
        return bool(_PRODUCT_URL_RE.match(url.split("?")[0]))

    def product_id_from_url(self, url: str) -> Optional[str]:
        """AllBeauty product URLs carry a handle, not the numeric id.

        Identity comes from the JSON instead, so this returns None rather
        than inventing an id from the slug.
        """
        return None

    def select_candidates(self, urls: Iterable[str], brands: Sequence[str]) -> List[str]:
        """Unused: discovery is the collection API, not a URL list."""
        return []

    # --- extraction -------------------------------------------------------

    def extract_products_from_listing(
        self, response, brand: Optional[str] = None
    ) -> List[Product]:
        """Turn a Shopify collection page into complete Product records."""
        payload = self._json(response)
        if not payload:
            return []

        now = datetime.now(timezone.utc).isoformat()
        products = []
        for raw in payload.get("products") or []:
            product = self._product_from_json(raw, now)
            if product is not None:
                products.append(product)
        return products

    def _product_from_json(self, raw: Dict[str, Any], scraped_at: str) -> Optional[Product]:
        """One Shopify product -> one Product record."""
        product_id = raw.get("id")
        vendor = clean_text(raw.get("vendor"))
        title = clean_text(raw.get("title"))
        handle = raw.get("handle")
        if not (product_id and vendor and title and handle):
            return None

        variants = [v for v in (raw.get("variants") or []) if isinstance(v, dict)]
        prices = [self._money(v.get("price")) for v in variants]
        prices = [p for p in prices if p is not None]
        if not prices:
            return None

        current = min(prices)
        # A range of shades at different prices has no single price, so the
        # low end is reported as a from-price rather than passed off as the
        # product's price.
        price_is_from = len(set(prices)) > 1

        # compare_at_price is Shopify's was-price. Take it from the variant
        # the reported price came from, so the pair describes one item.
        cheapest = min(
            (v for v in variants if self._money(v.get("price")) == current),
            key=lambda v: self._money(v.get("price")),
            default=None,
        )
        original = self._money((cheapest or {}).get("compare_at_price"))
        # Shopify leaves compare_at_price set to the same value (or lower)
        # when nothing is discounted; only a genuinely higher number is a was-price.
        if original is not None and original <= current:
            original = None

        return Product(
            retailer=self.display_name,
            brand=vendor,
            # The vendor field is the retailer's own structured statement of
            # the brand -- the same standing as John Lewis's JSON-LD brand.
            brand_verified_by="shopify_vendor",
            product_id=str(product_id),
            product_title=title,
            product_url=canonical_url(f"https://{self.domain}/products/{handle}"),
            source_url=canonical_url(f"https://{self.domain}/products/{handle}"),
            sku=clean_text((cheapest or {}).get("sku")) if len(variants) == 1 else None,
            # Shopify numbers products and stock units separately: the product
            # id is 8456134033558 while the variant SKU is the supplier's
            # 12947142. They are never expected to match, so the
            # variant-conflict check would reject every record here.
            sku_matches_product_id=False,
            current_price=current,
            price_is_from=price_is_from,
            original_price=original,
            discount_amount=(
                round(original - current, 2) if original is not None else None
            ),
            discount_percent=(
                round((original - current) / original * 100, 2)
                if original is not None and original > 0
                else None
            ),
            currency="GBP",
            availability="InStock" if any(v.get("available") for v in variants) else "OutOfStock",
            category=self._category(raw.get("product_type")),
            variant_count=len(variants) or None,
            # Shopify's product JSON carries no promotional copy; offers are
            # expressed as compare_at_price and on the collection pages.
            promotional_copy=None,
            scraped_at=scraped_at,
        )

    @staticmethod
    def _money(value: Any) -> Optional[float]:
        """Shopify quotes prices as decimal strings ("16.10")."""
        if value in (None, ""):
            return None
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _category(product_type: Optional[str]) -> Optional[str]:
        """The most specific segment of "Cosmetics > Eyeliner"."""
        text = clean_text(product_type)
        if not text:
            return None
        return text.split(">")[-1].strip().lower() or None

    @staticmethod
    def _json(response) -> Optional[Dict[str, Any]]:
        """Parse a JSON response body, or None when it is not JSON.

        A collection that does not exist returns the storefront HTML with a
        200, so a parse failure here means "no such collection" rather than
        an error worth raising.
        """
        try:
            body = response.body
        except AttributeError:  # pragma: no cover - defensive
            return None
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    def extract_product(
        self, response, target_brands: Sequence[str] = ()
    ) -> Optional[Product]:
        """Not used: products come from the collection API, not from pages."""
        return None

    def extract_campaigns(self, response) -> List[Campaign]:
        """Offers stated on a collection page.

        The product JSON carries no campaign text, so campaigns come only
        from the HTML offer pages.
        """
        if self._json(response) is not None:
            return []  # a JSON listing has no promotional copy

        # AllBeauty states its offers as LINKS to collections ("At Least 30%
        # off", "Extra 5% off ... Use Code: EXTRA5"), not as headings or
        # banner text. Scanning only headings found nothing on a site running
        # ten live campaigns, so the link scan runs first.
        campaigns = self._scan_offer_links(response)
        seen = {c.promotion_text for c in campaigns}

        url = str(response.url)
        for node in response.css("h1, h2, [class*='banner'], [class*='promo']"):
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
                    scope=SCOPE_SITEWIDE,
                    scope_value=None,
                    promo_code=extract_promo_code(text),
                    source_url=canonical_url(url),
                    landing_url=None,
                )
            )
        return campaigns
