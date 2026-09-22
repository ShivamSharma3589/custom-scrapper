"""The common data model shared by every retailer adapter."""

import hashlib
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


SCOPE_SITEWIDE = "sitewide"
SCOPE_BRAND = "brand"
SCOPE_CATEGORY = "category"
SCOPE_PRODUCT = "product"
SCOPE_UNRESOLVED = "unresolved"


PROMO_PERCENTAGE = "percentage_discount"
PROMO_CODE = "code_discount"
PROMO_BUNDLE = "bundle"
PROMO_GIFT = "gift_with_purchase"
PROMO_SPEND_THRESHOLD = "spend_threshold"
PROMO_AMOUNT = "amount_discount"
PROMO_PRICE_MATCH = "price_match"
PROMO_SALE = "sale"
PROMO_OTHER = "other"


def _stable_id(*parts: Optional[str]) -> str:
    """Build a short deterministic id from the given parts."""
    joined = "||".join((p or "") for p in parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:12]


@dataclass
class Campaign:
    """A retailer promotion that is not intrinsic to a single product."""

    retailer: str
    promotion_text: str
    promotion_type: str
    scope: str
    source_url: str
    scope_value: Optional[str] = None
    promo_code: Optional[str] = None
    landing_url: Optional[str] = None
    campaign_id: str = ""
    record_type: str = "campaign"

    def __post_init__(self) -> None:
        from .normalize import strip_call_to_action
        self.promotion_text = strip_call_to_action(self.promotion_text)

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
    product_id: str
    product_title: str
    product_url: str
    source_url: str

    brand_matched_to: Optional[str] = None
    brand_verified_by: str = "unverified"

    category: Optional[str] = None

    currency: Optional[str] = None
    current_price: Optional[float] = None
    original_price: Optional[float] = None
    discount_amount: Optional[float] = None
    discount_percent: Optional[float] = None
    availability: Optional[str] = None

    variant_count: Optional[int] = None
    sku: Optional[str] = None

    sku_matches_product_id: bool = True

    price_is_from: bool = False

    promotional_copy: Optional[str] = None
    applied_campaigns: List[str] = field(default_factory=list)

    scraped_at: Optional[str] = None
    record_type: str = "product"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class RejectedRecord:
    """A record validation refused to emit, kept with its reason."""

    reason: str
    detail: str
    source_url: str
    payload: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
