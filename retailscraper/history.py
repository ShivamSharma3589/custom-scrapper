"""Detecting what changed between two runs.

A single snapshot says what prices are today. The question the business
actually asks is "what changed?" -- a retailer going from 20% to 30% off a
brand is the event worth acting on, and it is invisible in any one run.

Two runs of the same retailer and brands are compared on the retailer's own
product id, which is stable across runs even when titles and URLs are
reworded. Campaigns are compared on their deterministic `campaign_id`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# What kind of change was observed. Kept machine-readable so a consumer can
# alert on some kinds and ignore others.
CHANGE_PRICE_DROP = "price_drop"
CHANGE_PRICE_RISE = "price_rise"
CHANGE_DISCOUNT_DEEPENED = "discount_deepened"
CHANGE_DISCOUNT_REDUCED = "discount_reduced"
CHANGE_DISCOUNT_STARTED = "discount_started"
CHANGE_DISCOUNT_ENDED = "discount_ended"
CHANGE_AVAILABILITY = "availability_changed"
CHANGE_PRODUCT_ADDED = "product_added"
CHANGE_PRODUCT_REMOVED = "product_removed"
CHANGE_CAMPAIGN_STARTED = "campaign_started"
CHANGE_CAMPAIGN_ENDED = "campaign_ended"

# Prices are floats read off a page; treat sub-penny wobble as no change.
PRICE_EPSILON = 0.005


@dataclass
class Change:
    """One observed difference between two runs."""

    kind: str
    retailer: str
    subject: str                     # product title or campaign text
    detail: str                      # human-readable summary
    url: Optional[str] = None
    before: Optional[Any] = None
    after: Optional[Any] = None
    brand: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "retailer": self.retailer,
            "brand": self.brand,
            "subject": self.subject,
            "detail": self.detail,
            "before": self.before,
            "after": self.after,
            "url": self.url,
        }


def _money(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.2f}"


def _changed(a: Optional[float], b: Optional[float]) -> bool:
    """True when two optional numbers genuinely differ."""
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    return abs(a - b) > PRICE_EPSILON


def _index(records: List[Dict[str, Any]], key: str) -> Dict[str, Dict[str, Any]]:
    return {r[key]: r for r in records if r.get(key)}


def compare_products(
    before: List[Dict[str, Any]], after: List[Dict[str, Any]]
) -> List[Change]:
    """Differences in the product set between two runs.

    Matched on `product_id` -- the retailer's own identifier. Titles and URLs
    get reworded between runs; ids do not, which is why identity lives there.
    """
    old_index = _index(before, "product_id")
    new_index = _index(after, "product_id")
    changes: List[Change] = []

    for product_id, new in new_index.items():
        old = old_index.get(product_id)
        title = new.get("product_title") or product_id
        retailer = new.get("retailer") or ""
        brand = new.get("brand_matched_to") or new.get("brand")
        url = new.get("product_url")

        if old is None:
            changes.append(Change(
                kind=CHANGE_PRODUCT_ADDED, retailer=retailer, brand=brand,
                subject=title, url=url, after=new.get("current_price"),
                detail=f"New product at {_money(new.get('current_price'))}.",
            ))
            continue

        old_price, new_price = old.get("current_price"), new.get("current_price")
        if _changed(old_price, new_price):
            dropped = (old_price or 0) > (new_price or 0)
            changes.append(Change(
                kind=CHANGE_PRICE_DROP if dropped else CHANGE_PRICE_RISE,
                retailer=retailer, brand=brand, subject=title, url=url,
                before=old_price, after=new_price,
                detail=f"Price {'fell' if dropped else 'rose'} "
                       f"{_money(old_price)} -> {_money(new_price)}.",
            ))

        old_disc, new_disc = old.get("discount_percent"), new.get("discount_percent")
        if _changed(old_disc, new_disc):
            if old_disc is None:
                kind, wording = CHANGE_DISCOUNT_STARTED, "Discount started"
            elif new_disc is None:
                kind, wording = CHANGE_DISCOUNT_ENDED, "Discount ended"
            elif new_disc > old_disc:
                kind, wording = CHANGE_DISCOUNT_DEEPENED, "Discount deepened"
            else:
                kind, wording = CHANGE_DISCOUNT_REDUCED, "Discount reduced"
            changes.append(Change(
                kind=kind, retailer=retailer, brand=brand, subject=title, url=url,
                before=old_disc, after=new_disc,
                detail=f"{wording}: {_money(old_disc)}% -> {_money(new_disc)}%.",
            ))

        if (old.get("availability") or None) != (new.get("availability") or None):
            changes.append(Change(
                kind=CHANGE_AVAILABILITY, retailer=retailer, brand=brand,
                subject=title, url=url,
                before=old.get("availability"), after=new.get("availability"),
                detail=f"Availability {old.get('availability')} -> {new.get('availability')}.",
            ))

    for product_id, old in old_index.items():
        if product_id not in new_index:
            changes.append(Change(
                kind=CHANGE_PRODUCT_REMOVED,
                retailer=old.get("retailer") or "",
                brand=old.get("brand_matched_to") or old.get("brand"),
                subject=old.get("product_title") or product_id,
                url=old.get("product_url"), before=old.get("current_price"),
                detail="No longer found in this run.",
            ))

    return changes


def compare_campaigns(
    before: List[Dict[str, Any]], after: List[Dict[str, Any]]
) -> List[Change]:
    """Campaigns that started or ended between two runs."""
    old_index = _index(before, "campaign_id")
    new_index = _index(after, "campaign_id")
    changes: List[Change] = []

    for campaign_id, new in new_index.items():
        if campaign_id not in old_index:
            changes.append(Change(
                kind=CHANGE_CAMPAIGN_STARTED,
                retailer=new.get("retailer") or "",
                subject=new.get("promotion_text") or campaign_id,
                url=new.get("landing_url") or new.get("source_url"),
                after=new.get("promotion_type"),
                detail=f"New {new.get('scope')} campaign.",
            ))

    for campaign_id, old in old_index.items():
        if campaign_id not in new_index:
            changes.append(Change(
                kind=CHANGE_CAMPAIGN_ENDED,
                retailer=old.get("retailer") or "",
                subject=old.get("promotion_text") or campaign_id,
                url=old.get("landing_url") or old.get("source_url"),
                before=old.get("promotion_type"),
                detail=f"{(old.get('scope') or 'unknown').capitalize()} campaign no longer seen.",
            ))

    return changes


def diff_runs(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    """Full comparison of two run documents.

    Only compares runs of the same retailer: diffing Boots against
    Lookfantastic would report every product as added and removed, which is
    noise dressed up as a result.
    """
    old_retailer = before.get("retailer")
    new_retailer = after.get("retailer")
    if old_retailer != new_retailer:
        raise ValueError(
            f"Cannot diff different retailers ({old_retailer!r} vs {new_retailer!r}). "
            "Compare two runs of the same retailer, or use compare.py for "
            "cross-retailer price comparison."
        )

    # The same check for brands. Two runs of one retailer covering different
    # brands would report every product in the first as removed and every
    # product in the second as added -- noise dressed up as a result, and
    # indistinguishable in the output from a retailer genuinely dropping a
    # brand overnight.
    old_brands = {b.casefold() for b in before.get("target_brands") or []}
    new_brands = {b.casefold() for b in after.get("target_brands") or []}
    if old_brands != new_brands:
        raise ValueError(
            f"Cannot diff runs covering different brands "
            f"({sorted(old_brands) or 'none'} vs {sorted(new_brands) or 'none'}). "
            "Compare two runs of the same brands."
        )

    # Ordering matters: 'before' must actually precede 'after', or every
    # change is reported backwards ("price rose" for a cut).
    old_at, new_at = before.get("scraped_at"), after.get("scraped_at")
    if old_at and new_at and old_at > new_at:
        raise ValueError(
            f"The 'before' run ({old_at}) is newer than the 'after' run ({new_at}). "
            "Pass the earlier run first."
        )

    changes = (
        compare_products(before.get("products", []), after.get("products", []))
        + compare_campaigns(before.get("campaigns", []), after.get("campaigns", []))
    )

    by_kind: Dict[str, int] = {}
    for change in changes:
        by_kind[change.kind] = by_kind.get(change.kind, 0) + 1

    return {
        "retailer": new_retailer,
        "before_scraped_at": before.get("scraped_at"),
        "after_scraped_at": after.get("scraped_at"),
        "change_count": len(changes),
        "changes_by_kind": dict(sorted(by_kind.items())),
        "changes": [c.to_dict() for c in changes],
    }
