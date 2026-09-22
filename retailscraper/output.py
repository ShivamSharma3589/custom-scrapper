"""Output serialisation: one run, written as JSON and as CSV."""

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from .models import Campaign, Product, RejectedRecord
from .validation import match_brand

PRODUCT_COLUMNS = [
    "run_id",
    "retailer", "brand", "brand_matched_to", "category",
    "product_title", "product_id", "sku",
    "current_price", "price_is_from", "original_price", "discount_amount", "discount_percent",
    "currency", "availability", "variant_count",
    "promotional_copy", "applied_campaigns", "product_url",
    "brand_verified_by", "scraped_at",
]

CAMPAIGN_COLUMNS = [
    "run_id",
    "campaign_id", "retailer", "scope", "scope_value",
    "promotion_type", "promotion_text", "promo_code",
    "landing_url", "source_url",
]

REJECTED_COLUMNS = ["run_id", "reason", "detail", "source_url", "product_title", "product_id"]


def build_payload(
    retailer: str,
    domain: str,
    brands: Sequence[str],
    products: Iterable[Product],
    campaigns: Iterable[Campaign],
    rejected: Iterable[RejectedRecord],
    stats: Dict[str, Any],
    run_id: str = "",
) -> Dict[str, Any]:
    """Assemble the full result document for one run."""
    product_list = sorted(products, key=lambda p: (p.brand, p.product_title))
    campaign_list = sorted(campaigns, key=lambda c: (c.scope, c.promotion_text))
    rejected_list = list(rejected)

    def stamped(record: Dict[str, Any]) -> Dict[str, Any]:
        return {"run_id": run_id, **record}

    return {
        "run_id": run_id,
        "retailer": retailer,
        "domain": domain,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "target_brands": list(brands),
        "campaigns": [stamped(c.to_dict()) for c in campaign_list],
        "products": [stamped(p.to_dict()) for p in product_list],
        "rejected": [stamped(r.to_dict()) for r in rejected_list],
        "run_stats": {
            **stats,
            "campaign_records": len(campaign_list),
            "product_records": len(product_list),
            "rejected_records": len(rejected_list),
            "rejections_by_reason": _count_by_reason(rejected_list),
        },
    }


def _count_by_reason(rejected: List[RejectedRecord]) -> Dict[str, int]:
    """Summarise rejections so run health is visible at a glance."""
    counts: Dict[str, int] = {}
    for record in rejected:
        counts[record.reason] = counts.get(record.reason, 0) + 1
    return dict(sorted(counts.items()))


def write_json(payload: Dict[str, Any], path: Path) -> Path:
    """Write the full result document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    return path


def _write_csv(rows: List[Dict[str, Any]], columns: List[str], path: Path) -> Path:
    """Write rows as CSV, keeping only the declared columns and their order."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) for column in columns})
    return path


def _brand_filename_part(brand: str) -> str:
    """Turn a brand name into a filename-safe fragment ("Jo Malone" -> jo-malone)."""
    return re.sub(r"[^a-z0-9]+", "-", brand.casefold()).strip("-") or "unknown"


def _flat_products(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Product rows with the campaign list flattened for CSV."""
    rows = []
    for row in payload["products"]:
        row = dict(row)
        row["applied_campaigns"] = ";".join(row.get("applied_campaigns") or [])
        rows.append(row)
    return rows


def _flat_rejections(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Rejection rows, reduced to the fields a person reads."""
    rows = []
    for row in payload["rejected"]:
        fields = row.get("payload") or {}
        rows.append({
            "run_id": row.get("run_id"),
            "reason": row.get("reason"),
            "detail": row.get("detail"),
            "source_url": row.get("source_url"),
            "product_title": fields.get("product_title"),
            "product_id": fields.get("product_id"),
        })
    return rows


def group_by_brand(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Split records by the brand they were requested as."""
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        key = row.get("brand_matched_to") or row.get("brand") or "unknown"
        grouped.setdefault(key, []).append(row)
    return grouped


def write_all(payload: Dict[str, Any], paths) -> List[Path]:
    """Write one run's results into this retailer's folders."""
    written: List[Path] = []

    products = _flat_products(payload)
    shared = {
        key: payload.get(key)
        for key in ("run_id", "retailer", "domain", "scraped_at", "run_stats")
    }
    campaigns = payload.get("campaigns") or []
    brands_by_campaign: Dict[str, set] = {}

    for brand, rows in sorted(group_by_brand(payload["products"]).items()):
        applied = {cid for row in rows for cid in (row.get("applied_campaigns") or [])}
        for cid in applied:
            brands_by_campaign.setdefault(cid, set()).add(brand)
        written.append(write_json({
            **shared,
            "brand": brand,
            "target_brands": [brand],
            "products": rows,
            "campaigns": [c for c in campaigns if c.get("campaign_id") in applied],
            "rejected": [
                r for r in payload.get("rejected") or []
                if match_brand((r.get("payload") or {}).get("brand") or "", [brand])
            ],
        }, paths.file(_brand_filename_part(brand), ".json")))

    for brand, rows in sorted(group_by_brand(products).items()):
        written.append(_write_csv(
            rows, PRODUCT_COLUMNS,
            paths.file(_brand_filename_part(brand), ".csv"),
        ))

    written.append(write_json({**shared, "campaigns": campaigns},
                              paths.file("campaigns", ".json")))
    written.append(_write_csv(campaigns, CAMPAIGN_COLUMNS,
                              paths.file("campaigns", ".csv")))

    on_brands = [{**c, "brands": sorted(brands_by_campaign[c["campaign_id"]])}
                 for c in campaigns if c.get("campaign_id") in brands_by_campaign]
    written.append(write_json({**shared, "campaigns": on_brands},
                              paths.file("brands_campaigns", ".json")))
    written.append(_write_csv(
        [{**c, "brands": ";".join(c["brands"])} for c in on_brands],
        CAMPAIGN_COLUMNS + ["brands"], paths.file("brands_campaigns", ".csv")))
    written.append(_write_csv(_flat_rejections(payload), REJECTED_COLUMNS,
                              paths.file("rejected", ".csv")))
    return written
