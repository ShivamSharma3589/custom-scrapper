"""Amazon UK adapter."""

import re
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence
from urllib.parse import quote

from ..models import SCOPE_PRODUCT, SCOPE_SITEWIDE, Campaign, Product
from ..normalize import canonical_url, clean_text, extract_promo_code, percent_off
from ..promotions import (
    classify_promotion,
    is_browse_facet,
    is_confident_offer,
)
from .base import ListingPage, RetailerAdapter, register

_PRODUCT_URL_RE = re.compile(
    r"^https?://(?:www\.)?amazon\.co\.uk/(?:.*/)?(?:dp|gp/product)/([A-Z0-9]{10})", re.I
)

_SYMBOLS = {"£": "GBP", "GBP": "GBP", "$": "USD", "€": "EUR", "INR": "INR", "₹": "INR"}

_AMOUNT_RE = re.compile(r"([\d][\d,]*(?:\.\d{1,2})?)")

_NO_BUY_BOX_RE = re.compile(r"no featured offers? available", re.IGNORECASE)


@register
class AmazonAdapter(RetailerAdapter):
    """Extraction rules for amazon.co.uk."""

    name = "amazon"
    domain = "www.amazon.co.uk"
    display_name = "Amazon UK"

    sitemap_urls: List[str] = []

    supports_categories = False

    EXPECTED_CURRENCY = "GBP"

    listing_page_size = 60

    def __init__(self) -> None:
        self.wrong_currency_seen = 0
        self.sponsored_skipped = 0
        self.no_buy_box_skipped = 0
        self.currencies_seen: set = set()

    def configure_session(self, manager) -> None:
        """A stealth browser asking Amazon for sterling."""
        from scrapling.fetchers import AsyncStealthySession

        self.add_proxied_sessions(manager, lambda proxy: AsyncStealthySession(
            headless=True,
            google_search=True,
            network_idle=True,
            timeout=120_000,
            max_pages=2,
            locale="en-GB",
            proxy=proxy,
            cookies=[
                {"name": "i18n-prefs", "value": "GBP",
                 "domain": ".amazon.co.uk", "path": "/"},
                {"name": "lc-acbuk", "value": "en_GB",
                 "domain": ".amazon.co.uk", "path": "/"},
            ],
            extra_headers={
                "Accept-Language": "en-GB,en;q=0.9",
            },
        ))

    def warmup_url(self) -> Optional[str]:
        """The homepage. Amazon challenges a cold session's first navigation."""
        return f"https://{self.domain}/"

    def product_listing_urls(
        self, brand: str, categories: Sequence[str], max_pages: int
    ) -> List[ListingPage]:
        """Beauty search for one brand, asking Amazon to filter to it."""
        query = re.sub(r"\s+", "+", brand.strip())
        facet = quote(brand.strip())
        return [
            ListingPage(
                url=(
                    f"https://{self.domain}/s?k={query}&i=beauty"
                    f"&rh=p_89%3A{facet}"
                    f"&language=en_GB&page={page}"
                ),
                brand=brand,
                category=None,
            )
            for page in range(1, max_pages + 1)
        ]

    def is_product_url(self, url: str) -> bool:
        return bool(_PRODUCT_URL_RE.match(url.split("?")[0]))

    def product_id_from_url(self, url: str) -> Optional[str]:
        match = _PRODUCT_URL_RE.match(url.split("?")[0])
        return match.group(1).upper() if match else None

    def select_candidates(self, urls: Iterable[str], brands: Sequence[str]) -> List[str]:
        """Unused: discovery reads search result cards, not a URL list."""
        return []

    def extract_products_from_listing(
        self, response, brand: Optional[str] = None
    ) -> List[Product]:
        """Read the search result cards on this page."""
        from ..validation import match_brand

        now = datetime.now(timezone.utc).isoformat()
        targets = [brand] if brand else []
        products: List[Product] = []

        for card in response.css('[data-component-type="s-search-result"]'):
            asin = (card.attrib.get("data-asin") or "").strip().upper()
            if not asin:
                continue

            title_node = card.css('[data-cy="title-recipe"]')
            if not title_node:
                continue
            lines = [
                line.strip()
                for line in (title_node[0].get_all_text() or "").splitlines()
                if line.strip()
            ]
            if len(lines) < 2:
                continue

            if lines[0].strip().casefold() == "sponsored":
                self.sponsored_skipped += 1
                continue

            vendor, title = clean_text(lines[0]), clean_text(" ".join(lines[1:]))
            if not (vendor and title):
                continue

            if targets and match_brand(vendor, targets) is None:
                continue

            current, currency = self._price(card, ".a-price .a-offscreen")
            was, was_currency = self._price(card, ".a-text-price .a-offscreen")
            if current is None:
                if _NO_BUY_BOX_RE.search(card.get_all_text() or ""):
                    self.no_buy_box_skipped += 1
                continue

            for seen in (currency, was_currency):
                if seen:
                    self.currencies_seen.add(seen)
            if currency != self.EXPECTED_CURRENCY:
                self.wrong_currency_seen += 1
                continue

            original = was if (was is not None and was > current
                               and was_currency == self.EXPECTED_CURRENCY) else None

            url = canonical_url(f"https://{self.domain}/dp/{asin}")
            products.append(Product(
                retailer=self.display_name,
                brand=vendor,
                brand_verified_by="amazon_brand_line",
                product_id=asin,
                product_title=title,
                product_url=url,
                source_url=canonical_url(str(response.url)),
                sku=asin,
                current_price=current,
                original_price=original,
                discount_amount=(
                    round(original - current, 2) if original is not None else None
                ),
                discount_percent=percent_off(original, current),
                currency=self.EXPECTED_CURRENCY,
                availability="InStock",
                category=None,
                variant_count=None,
                promotional_copy=None,
                scraped_at=now,
            ))
        return products

    def _price(self, card, selector: str):
        """(amount, currency) from a price element, or (None, None)."""
        nodes = card.css(selector)
        if not nodes:
            return None, None
        text = clean_text(nodes[0].get_all_text()) or ""

        currency = None
        for symbol, code in _SYMBOLS.items():
            if symbol in text:
                currency = code
                break

        match = _AMOUNT_RE.search(text.replace("\xa0", " "))
        if not match:
            return None, currency
        try:
            return round(float(match.group(1).replace(",", "")), 2), currency
        except ValueError:
            return None, currency

    def prepare(self, brands: Sequence[str]) -> List[str]:
        """Warn before the crawl that this needs a UK IP to be trustworthy."""
        return [
            "Amazon prices by visitor location, not by domain. Unless this "
            "machine has a UK IP (or a UK proxy is configured in "
            "AmazonAdapter.configure_session), every price will be in the "
            "local currency and will be REFUSED rather than published."
        ]

    def extract_product(
        self, response, target_brands: Sequence[str] = ()
    ) -> Optional[Product]:
        """Not used: products come from search cards, not product pages."""
        return None

    def campaign_discovery_urls(self) -> List[str]:
        """Amazon's deals page, which robots.txt allows."""
        return [
        "https://www.amazon.co.uk/deals",
        ]

    def extract_campaign_directory(self, response) -> List[Campaign]:
        """Every offer linked from a hub page, via the shared scan."""
        return self._scan_offer_links(response)

    def extract_campaigns(self, response) -> List[Campaign]:
        """Deal badges and promotional copy on a search or product page."""
        url = str(response.url)
        on_product = self.is_product_url(url)
        campaigns: List[Campaign] = []
        seen: set = set()

        for node in response.css('[class*="badge"], [class*="deal"], [class*="promo"], h1'):
            text = clean_text(node.get_all_text())
            if not text or text in seen or len(text) > 160:
                continue
            if not is_confident_offer(text):
                continue
            if is_browse_facet(text, url):
                continue
            seen.add(text)
            campaigns.append(Campaign(
                retailer=self.display_name,
                promotion_text=text,
                promotion_type=classify_promotion(text),
                scope=SCOPE_PRODUCT if on_product else SCOPE_SITEWIDE,
                scope_value=None,
                promo_code=extract_promo_code(text),
                source_url=canonical_url(url),
                landing_url=None,
            ))
        return campaigns
