"""Output serialisation: one run, written as JSON and as CSV.

The JSON is the full record and is what downstream systems should consume.
The CSVs are flat views of the same data for people who want to open the
results in Excel -- they carry no information the JSON lacks.

Products and campaigns stay in separate collections, joined by
`Product.applied_campaigns` -> `Campaign.campaign_id`. A report can present
this as "campaign, and the products under it" without the data model having
to duplicate products across campaigns or drop the ones no campaign covers.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from .models import Campaign, Product, RejectedRecord

# Column order for the flat exports. Fixed so that a diff between two runs
# shows changed data rather than reshuffled columns.
PRODUCT_COLUMNS = [
    "retailer", "brand", "brand_matched_to", "category",
    "product_title", "product_id", "sku",
    "current_price", "price_is_from", "original_price", "discount_amount", "discount_percent",
    "currency", "availability", "variant_count",
    "promotional_copy", "applied_campaigns", "product_url",
    "brand_verified_by", "scraped_at",
]

CAMPAIGN_COLUMNS = [
    "campaign_id", "retailer", "scope", "scope_value",
    "promotion_type", "promotion_text", "promo_code",
    "landing_url", "source_url",
]

REJECTED_COLUMNS = ["reason", "detail", "source_url", "product_title", "product_id"]


def build_payload(
    retailer: str,
    domain: str,
    brands: Sequence[str],
    products: Iterable[Product],
    campaigns: Iterable[Campaign],
    rejected: Iterable[RejectedRecord],
    stats: Dict[str, Any],
) -> Dict[str, Any]:
    """Assemble the full result document for one run."""
    product_list = sorted(products, key=lambda p: (p.brand, p.product_title))
    campaign_list = sorted(campaigns, key=lambda c: (c.scope, c.promotion_text))
    rejected_list = list(rejected)

    return {
        "retailer": retailer,
        "domain": domain,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "target_brands": list(brands),
        "campaigns": [c.to_dict() for c in campaign_list],
        "products": [p.to_dict() for p in product_list],
        # Rejections are part of the output on purpose: a consumer can see
        # what we refused to vouch for, and why.
        "rejected": [r.to_dict() for r in rejected_list],
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
    # utf-8-sig so Excel on Windows shows £ and accented brand names correctly.
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) for column in columns})
    return path


def _brand_filename_part(brand: str) -> str:
    """Turn a brand name into a filename-safe fragment ("Jo Malone" -> jo-malone)."""
    return re.sub(r"[^a-z0-9]+", "-", brand.casefold()).strip("-") or "unknown"


def write_csvs(payload: Dict[str, Any], prefix: Path) -> List[Path]:
    """Write the flat CSV views of a run.

    Products are split into one file per brand. The JSON keeps every brand
    together because the campaigns are shared between them, but a CSV is
    opened by a person looking at one brand at a time -- and a combined file
    ends up named after every brand in the run, which becomes unusable once
    there are seven of them.
    """
    products = []
    for row in payload["products"]:
        row = dict(row)
        # A list is not a CSV value; join the campaign ids into one cell.
        row["applied_campaigns"] = ";".join(row.get("applied_campaigns") or [])
        products.append(row)

    rejected = []
    for row in payload["rejected"]:
        payload_fields = row.get("payload") or {}
        rejected.append({
            "reason": row.get("reason"),
            "detail": row.get("detail"),
            "source_url": row.get("source_url"),
            "product_title": payload_fields.get("product_title"),
            "product_id": payload_fields.get("product_id"),
        })

    written: List[Path] = []

    # One products file per requested brand. Group on `brand_matched_to` (the
    # name we were asked for) rather than `brand` (the retailer's own naming),
    # so "Jo Malone London" lands in the jo-malone file.
    by_brand: Dict[str, List[Dict[str, Any]]] = {}
    for row in products:
        key = row.get("brand_matched_to") or row.get("brand") or "unknown"
        by_brand.setdefault(key, []).append(row)

    retailer_part = prefix.name.split("_")[0]
    for brand, rows in sorted(by_brand.items()):
        filename = f"{retailer_part}_{_brand_filename_part(brand)}_products.csv"
        written.append(_write_csv(rows, PRODUCT_COLUMNS, prefix.with_name(filename)))

    # Campaigns and rejections stay whole: campaigns are shared across brands,
    # and rejections are per-run diagnostics rather than per-brand reporting.
    written.append(_write_csv(payload["campaigns"], CAMPAIGN_COLUMNS,
                              prefix.with_name(prefix.name + "_campaigns.csv")))
    written.append(_write_csv(rejected, REJECTED_COLUMNS,
                              prefix.with_name(prefix.name + "_rejected.csv")))
    return written


def write_all(payload: Dict[str, Any], out_dir: Path, basename: str) -> List[Path]:
    """Write the JSON document and the CSV views for one run."""
    prefix = out_dir / basename
    written = [write_json(payload, prefix.with_suffix(".json"))]
    written.extend(write_csvs(payload, prefix))
    return written
