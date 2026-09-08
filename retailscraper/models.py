"""The common data model shared by every retailer adapter.

Two record types are kept deliberately separate:

  * `Product`  -- a single purchasable item with its own price/discount.
  * `Campaign` -- a retailer promotion that exists independently of any one
                  product (a site-wide banner, a brand-wide offer, ...).

A campaign is NOT a property of a product. Products reference the campaigns
that apply to them by id (`Product.applied_campaigns`), which lets one
campaign apply to many products, and one product carry several campaigns,
without duplicating or losing data.
"""

import hashlib
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# --- Campaign scope -------------------------------------------------------
# How widely a campaign applies. Claim a narrow scope only when the page
# structure says so, else `unresolved` -- mislabelling a site-wide banner as
# a brand campaign is the commonest way a scraper produces confident nonsense.

SCOPE_SITEWIDE = "sitewide"      # e.g. a header strip banner shown on every page
SCOPE_BRAND = "brand"            # tied to one brand
SCOPE_CATEGORY = "category"      # tied to a department, e.g. "20% Off Luxury Beauty"
SCOPE_PRODUCT = "product"        # tied to one specific product
SCOPE_UNRESOLVED = "unresolved"  # we found the offer but cannot prove its reach


# --- Promotion type -------------------------------------------------------

PROMO_PERCENTAGE = "percentage_discount"  # "25% off"
PROMO_CODE = "code_discount"              # "Extra 10% off | Use Code: EXTRA10"
PROMO_BUNDLE = "bundle"                   # "Buy 2 get 1 free"
PROMO_GIFT = "gift_with_purchase"         # "Free gift when you spend £X"
PROMO_SPEND_THRESHOLD = "spend_threshold"  # "Spend £50 get £10 off"
PROMO_OTHER = "other"


def _stable_id(*parts: Optional[str]) -> str:
    """Build a short deterministic id from the given parts.

    Deterministic matters: re-running the scraper on unchanged data must
    produce the same ids, otherwise every run looks like a full changeset to
    whatever consumes the output.
    """
    joined = "||".join((p or "") for p in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]


@dataclass
class Campaign:
    """A retailer promotion that is not intrinsic to a single product."""

    retailer: str
    promotion_text: str          # cleaned, human-readable copy
    promotion_type: str          # one of the PROMO_* constants
    scope: str                   # one of the SCOPE_* constants
    source_url: str              # the page we actually saw this on
    scope_value: Optional[str] = None    # brand name when scope == brand
    promo_code: Optional[str] = None     # discount code, when the offer needs one
    landing_url: Optional[str] = None    # the campaign's own page, if it links out
    campaign_id: str = ""
    record_type: str = "campaign"

    def __post_init__(self) -> None:
        # source URL excluded on purpose: one banner seen on twenty pages
        # is one campaign, not twenty
        if not self.campaign_id:
            self.campaign_id = _stable_id(
                self.retailer, self.promotion_text, self.scope, self.scope_value
            )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Product:
    """A single product offering at one retailer, at one point in time."""

    retailer: str
    brand: str
    product_id: str              # retailer's own stable id, parsed from the URL
    product_title: str
    product_url: str             # canonical URL, deduplicated on
    source_url: str              # where we found it (may differ from canonical)

    # --- brand provenance -------------------------------------------------
    # `brand` is the retailer's own name ("Jo Malone London");
    # `brand_matched_to` is the name we asked for ("Jo Malone"), so results
    # group across shops. `brand_verified_by` records HOW the brand was
    # proved, so weak evidence stays visible and auditable.
    brand_matched_to: Optional[str] = None
    brand_verified_by: str = "unverified"

    # taken from the listing it was found on, never inferred from the title
    category: Optional[str] = None

    # --- pricing ----------------------------------------------------------
    currency: Optional[str] = None
    current_price: Optional[float] = None
    original_price: Optional[float] = None    # RRP / was-price, when shown
    discount_amount: Optional[float] = None
    discount_percent: Optional[float] = None
    availability: Optional[str] = None

    # --- variants ---------------------------------------------------------
    # a variant product may legitimately report a shade-level SKU that
    # differs from the URL id; a single-variant product may not
    variant_count: Optional[int] = None
    sku: Optional[str] = None

    # True when the retailer numbers products and stock units in the same
    # space, so a single-variant product's SKU should equal its URL id.
    # True for Lookfantastic and Boots; John Lewis uses two schemes
    # (`p47865` vs `230631076`), so comparing them there rejects everything.
    sku_matches_product_id: bool = True

    # True when the page quoted a RANGE ("£33.60 – £156.00") rather than one
    # price, so `current_price` is the lowest of several sizes, not the price
    # of a specific item. Comparing a "from" price against another retailer's
    # single-size price would understate that retailer's discount, so this
    # has to travel with the number.
    price_is_from: bool = False

    # --- promotions -------------------------------------------------------
    # Only promotions genuinely attached to THIS product. Site-wide banners do
    # not belong here; they live in the campaign list and are referenced by id.
    promotional_copy: Optional[str] = None
    applied_campaigns: List[str] = field(default_factory=list)

    scraped_at: Optional[str] = None
    record_type: str = "product"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RejectedRecord:
    """A record validation refused to emit, kept with its reason.

    Rejections are part of the output, never silently dropped -- a rejection
    is a signal that the page changed or that our extraction is wrong, and
    hiding it just makes the dataset look healthier than it is.
    """

    reason: str                  # machine-readable, e.g. "invalid_discount"
    detail: str                  # human-readable explanation
    source_url: str
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
