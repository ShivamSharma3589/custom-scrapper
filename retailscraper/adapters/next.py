"""Next adapter.

The retailer that looked impossible. Next sits behind Akamai Bot Manager,
and the pages that hold products -- brand, listing, search -- answer 403
"Access Denied" to plain HTTP, to curl with a complete Chrome header set,
to a warmed-up session carrying Akamai's own cookies, and to a stealth
browser. Only the homepage and the sitemaps came back.

**What actually gets through is the TLS fingerprint, not the headers.**
Akamai scores the TLS handshake, and every attempt above presented either
Python's OpenSSL fingerprint or a headless Chromium one. `FetcherSession`
impersonates a real browser's handshake, and the choice of which browser is
the whole difference:

    impersonate="firefox"   -> HTTP 200 on brand pages, 403 on /sale
    impersonate="safari"    -> HTTP 200 everywhere            <- used here
    impersonate="chrome"    -> HTTP 200, but a 2 KB stub
    impersonate="chrome131" -> HTTP 403
    impersonate="chrome124" -> HTTP 403
    impersonate="edge"      -> HTTP 403

Chrome fingerprints are the ones Akamai scrutinises, presumably because they
are what scrapers reach for first. No JavaScript sensor is defeated and no
challenge is solved: Next simply serves the page to a client whose handshake
it trusts. robots.txt permits these paths -- only `*/search/search` and
`*brand-beaverbrooks*` are disallowed, neither of which is used here.

**Discovery.** `/brands/<slug>` paginated with `?p=N`, which yields new
products until it runs out (Estee Lauder ends at page 16 with 443 products).
Next writes "Estée Lauder" as `este-lauder`, dropping the accent entirely
rather than folding it to "ee", so neither our slug rule nor a naive accent
fold produces it -- hence the override below.

**The brands sitemap is not the catalogue.** It lists 1,467 brand pages and
names only Estee Lauder of the seven we track, which is how this retailer
came to be written off as stocking one brand. It stocks at least six:
`/brands/mac` serves 34 products, `/brands/bobbi-brown` 27, `/brands/
too-faced` 26, and Clinique and Tom Ford have pages too. Only Jo Malone
returns nothing. `prepare` therefore fetches each brand page rather than
trusting the index.

**Product URLs are opaque** -- `/style/su449110/af1610` names neither brand
nor product -- so the product sitemap is useless for finding a brand's
items. The brand listing is the only route, which is why the 403 mattered
so much.

**Extraction.** Product pages carry a JSON-LD `Product`:

    .name                      -> the title, brand included
    .brand.name                -> "Estée Lauder", stated
    .sku                       -> "AF1-610-01"
    .offers.price / .priceCurrency / .availability

Three `Product` blocks appear on a page and only the first carries `offers`;
the others repeat the name with nulls, so the one with an offer is the one
to read.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..models import SCOPE_PRODUCT, SCOPE_SITEWIDE, Campaign, Product
from ..normalize import canonical_url, clean_text, extract_promo_code, percent_off
from ..promotions import (
    classify_promotion,
    is_browse_facet,
    is_confident_offer,
)
from .base import ListingPage, RetailerAdapter, register

# /style/<style-code>/<item-code>, e.g. /style/su449110/af1610
_PRODUCT_URL_RE = re.compile(
    r"^https?://(?:www\.)?next\.co\.uk/style/([a-z0-9]+)/([a-z0-9]+)/?$", re.I
)

# Next links its products with absolute URLs, not paths.
_PRODUCT_LINK_RE = re.compile(
    r"https://www\.next\.co\.uk/style/[a-z0-9]+/[a-z0-9]+", re.I
)

#: The TLS fingerprint Next serves pages to. See the module docstring -- this
#: is load-bearing, and a Chrome value here returns 403 or an empty stub.
_IMPERSONATE = "safari"

#: Products per brand listing page. Next varies this (10 to 33 observed), so
#: it is only used to decide how many pages to ask for.
_LISTING_PAGE_SIZE = 10


@register
class NextAdapter(RetailerAdapter):
    """Extraction rules for next.co.uk."""

    name = "next"
    domain = "www.next.co.uk"
    display_name = "Next"

    # Next answers 403 rather than 429 when it decides a client is asking
    # too fast, and it does not recover within a run: a seven-brand crawl at
    # full speed had 1,128 of its 1,154 requests refused and returned five
    # products. Two seconds apart, one at a time, it serves every page.
    crawl_delay = 2.5
    max_concurrent_requests = 1

    # No sitemap route to products: /style/su449110/af1610 names neither the
    # brand nor the product, so the 3,811 URLs per shard cannot be filtered
    # down to one brand without fetching every one of them.
    sitemap_urls: List[str] = []

    # Next files products under category facets (/brands/<slug>/f/category-
    # serums), but the vocabulary is Next's own and differs per brand, so a
    # `--categories` filter cannot be honoured faithfully.
    supports_categories = False

    #: Brands whose Next slug is not the obvious slugification. Next drops
    #: accented characters rather than folding them, so "Estée Lauder"
    #: becomes "este-lauder" and not "estee-lauder".
    BRAND_SLUG_OVERRIDES = {
        "estee lauder": "este-lauder",
        "estée lauder": "este-lauder",
    }

    # --- fetching ---------------------------------------------------------

    def configure_session(self, manager) -> None:
        """A session whose TLS handshake Next trusts.

        No browser is involved: the pages are server-rendered and complete
        without JavaScript, so this is the cheapest adapter of the lot once
        the handshake is right.
        """
        from scrapling.fetchers import FetcherSession

        manager.add(
            "default",
            FetcherSession(
                impersonate=_IMPERSONATE,
                stealthy_headers=True,
                timeout=90,
            ),
        )

    # --- discovery --------------------------------------------------------

    @classmethod
    def _brand_slug(cls, brand: str) -> str:
        key = brand.strip().lower()
        if key in cls.BRAND_SLUG_OVERRIDES:
            return cls.BRAND_SLUG_OVERRIDES[key]
        return re.sub(r"[^a-z0-9]+", "-", key).strip("-")

    def prepare(self, brands: Sequence[str]) -> List[str]:
        """Confirm each brand has a Next brand page before crawling.

        The page itself is fetched, not the brands sitemap. The sitemap was
        the obvious cheap check -- one request for 1,467 brands -- and it was
        wrong: it lists no page for MAC, Tom Ford, Clinique, Bobbi Brown or
        Too Faced, yet `/brands/mac` serves 34 products and `/brands/
        bobbi-brown` serves 27. Reporting "Next does not stock this brand"
        from an incomplete index is the worst answer this scraper can give,
        because it looks like a finding rather than a gap.
        """
        from scrapling.fetchers import FetcherSession

        warnings: List[str] = []
        with FetcherSession(
            impersonate=_IMPERSONATE, stealthy_headers=True, timeout=60
        ) as session:
            for brand in brands:
                slug = self._brand_slug(brand)
                url = f"https://{self.domain}/brands/{slug}"
                try:
                    response = session.get(url)
                except Exception:
                    # Unreachable is not absent; let the crawl try.
                    continue

                if response.status == 404:
                    warnings.append(
                        f"{self.display_name} has no brand page for {brand!r} "
                        f"({url}) -- it does not stock this brand"
                    )
                elif response.status != 200:
                    warnings.append(
                        f"{self.display_name} returned HTTP {response.status} "
                        f"for {brand!r} ({url}) -- this says nothing about "
                        f"whether the brand is stocked"
                    )
                elif not self._body_has_products(response):
                    warnings.append(
                        f"{self.display_name} lists no products for {brand!r} "
                        f"({url})"
                    )
        return warnings

    @classmethod
    def _body_has_products(cls, response) -> bool:
        return bool(_PRODUCT_LINK_RE.search(cls._body(response)))

    def product_listing_urls(
        self, brand: str, categories: Sequence[str], max_pages: int
    ) -> List[ListingPage]:
        """The brand's listing, paginated with `?p=N`.

        Next gives no product count and no page count, and page size varies
        from 10 to 33, so depth cannot be computed from a stated total the
        way it can at John Lewis or M&S. Pages that run past the end simply
        repeat the last set, and those duplicates deduplicate away on id.
        """
        slug = self._brand_slug(brand)
        base = f"https://{self.domain}/brands/{slug}"
        return [
            ListingPage(
                url=base if page == 1 else f"{base}?p={page}",
                brand=brand,
                category=None,
            )
            for page in range(1, max_pages + 1)
        ]

    def is_product_url(self, url: str) -> bool:
        return bool(_PRODUCT_URL_RE.match(url.split("?")[0].split("#")[0]))

    def product_id_from_url(self, url: str) -> Optional[str]:
        match = _PRODUCT_URL_RE.match(url.split("?")[0].split("#")[0])
        return f"{match.group(1)}-{match.group(2)}".lower() if match else None

    def extract_product_links(self, response, brand: Optional[str] = None) -> List[str]:
        """Product URLs on a brand listing.

        Read from the raw body rather than from anchors: Next repeats each
        product as an image link and a title link, and the fragment
        (`#af1610`) differs between them, so matching the URL shape and
        deduplicating is steadier than walking the DOM.
        """
        body = self._body(response)
        found: List[str] = []
        for url in _PRODUCT_LINK_RE.findall(body or ""):
            clean = canonical_url(url)
            if clean not in found:
                found.append(clean)
        return found

    def select_candidates(self, urls: Iterable[str], brands: Sequence[str]) -> List[str]:
        """Every product URL, unfiltered.

        There is nothing to filter on: Next's product URLs name neither the
        brand nor the product. They arrive from a brand listing, so they are
        already brand-scoped, and each one's brand is proved from its own
        JSON-LD afterwards.
        """
        keep: List[str] = []
        for url in urls:
            if self.is_product_url(url):
                clean = canonical_url(url)
                if clean not in keep:
                    keep.append(clean)
        return keep

    # --- extraction -------------------------------------------------------

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

        current = self._money(offers.get("price"))
        if current is None:
            return None

        currency = clean_text(offers.get("priceCurrency")) or "GBP"
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
            price_is_from=False,
            # Next states one price. No was-price appears in the markup or
            # the structured data on any product examined, so claiming a
            # discount here would be inventing one.
            original_price=None,
            discount_amount=None,
            discount_percent=percent_off(None, current),
            currency=currency,
            availability=availability,
            category=None,
            variant_count=None,
            # "AF1-610-01" and the URL's "su449110-af1610" are related but
            # not equal, so comparing them would reject every record.
            sku_matches_product_id=False,
            promotional_copy=None,
            scraped_at=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _body(response) -> str:
        body = getattr(response, "html_content", None) or getattr(response, "body", None)
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        return body or ""

    @staticmethod
    def _money(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return None

    def _product_json_ld(self, response) -> Optional[Dict[str, Any]]:
        """The `Product` block that carries an offer.

        A Next product page holds three. Only the first has `brand`, `sku`
        and `offers`; the rest repeat the name with nulls, and taking one of
        those would reject the record for having no brand.
        """
        for raw in re.findall(
            r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>',
            self._body(response),
            re.DOTALL,
        ):
            try:
                data = json.loads(raw.strip())
            except (ValueError, TypeError):
                continue
            for item in data if isinstance(data, list) else [data]:
                if (
                    isinstance(item, dict)
                    and item.get("@type") == "Product"
                    and item.get("offers")
                ):
                    return item
        return None

    def campaign_discovery_urls(self) -> List[str]:
        """Next's sale and clearance hubs.

        Both are linked from its own homepage and permitted by robots.txt.
        They need the Safari fingerprint specifically: with `firefox` the
        brand pages load but these two answer 403, which is why the adapter
        impersonates Safari rather than Firefox.
        """
        return [
        "https://www.next.co.uk/sale",
        "https://www.next.co.uk/clearance",
        ]

    def extract_campaign_directory(self, response) -> List[Campaign]:
        """Every offer linked from a hub page, via the shared scan."""
        return self._scan_offer_links(response)

    def extract_campaigns(self, response) -> List[Campaign]:
        """Promotional copy stated on the page.

        Next advertises far less than the beauty specialists do -- no offer
        strip, and no was-price on any Estee Lauder product examined -- so
        this usually finds nothing, which is the honest answer rather than a
        gap.
        """
        url = str(response.url)
        on_product = self.is_product_url(url)
        campaigns: List[Campaign] = []
        seen: set = set()

        for node in response.css("[class*='promo'], [class*='offer'], [data-testid*='promo'], h1, h2"):
            text = clean_text(node.get_all_text())
            if not text or text in seen or len(text) > 160:
                continue
            if not is_confident_offer(text):
                continue
            if is_browse_facet(text, url):
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
