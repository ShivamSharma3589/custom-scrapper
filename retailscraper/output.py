"""Output serialisation: one run, written as JSON and as CSV."""

import csv
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from .models import Campaign, Product, RejectedRecord
from .validation import match_brand

OFFER_PRODUCT_COLUMNS = [
    "retailer",
    "brand", "product_title", "product_id", "offer_text",
    "current_price", "original_price", "discount_amount", "discount_percent",
    "currency", "product_url", "campaign_ids",
    # "offer_urls",   # every offer page a product appeared on
]

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
    "landing_url", "source_urls",
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
    run_id: str = "",
    campaigns_only: bool = False,
    offer_products: Iterable[Dict[str, Any]] = (),
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
        "campaigns_only": campaigns_only,
        "offer_products": list(offer_products or []),
        "campaigns": [c.to_dict() for c in campaign_list],
        "products": [p.to_dict() for p in product_list],
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


def _as_campaign(row: Dict[str, Any]):
    """A campaign row read back as the object the adapter expects."""
    from .models import Campaign
    return Campaign(
        retailer=row.get("retailer") or "",
        promotion_text=row.get("promotion_text") or "",
        promotion_type=row.get("promotion_type") or "",
        scope=row.get("scope") or "",
        scope_value=row.get("scope_value"),
        promo_code=row.get("promo_code"),
        source_url=row.get("source_url"),
        landing_url=row.get("landing_url"),
    )


def _write_filtered(payload, campaigns, shared, put_json, put_csv, adapter=None) -> List[Path]:
    """The client-facing set: campaigns proven to carry our brands, and those products"""
    offers = payload.get("offer_products") or []
    by_offer_text = bool(adapter is not None
                         and getattr(adapter, "campaigns_stated_on_product", False))

    by_landing: Dict[str, List[str]] = {}
    by_text: Dict[str, List[str]] = {}
    for campaign in campaigns:
        cid = campaign.get("campaign_id")
        if not cid:
            continue
        landing = (campaign.get("landing_url") or "").split("?")[0].rstrip("/")
        if landing:
            by_landing.setdefault(landing, []).append(cid)
        text = (campaign.get("promotion_text") or "").strip()
        if by_offer_text and text:
            by_text.setdefault(text, []).append(cid)

    merged: Dict[str, Dict[str, Any]] = {}
    brands_by_campaign: Dict[str, set] = {}
    for row in offers:
        offer = (row.get("offer_url") or "").split("?")[0].rstrip("/")
        stated = row.get("offer_texts") or [row.get("offer_text")]
        ids = list(dict.fromkeys(
            by_landing.get(offer, [])
            + [cid for text in stated
               for cid in by_text.get((text or "").strip(), [])]))
        for cid in ids:
            brands_by_campaign.setdefault(cid, set()).add(row["brand"])

        seen = merged.get(row["product_id"])
        if seen is None:
            merged[row["product_id"]] = {
                "retailer": payload.get("retailer"),
                **row, "campaign_ids": set(ids),
                # "offer_urls": {offer},
            }
            continue
        seen["campaign_ids"].update(ids)
        # seen["offer_urls"].add(offer)
        if seen.get("original_price") is None and row.get("original_price") is not None:
            for field in ("original_price", "current_price",
                          "discount_amount", "discount_percent", "offer_text"):
                seen[field] = row.get(field)

    rows = [{**r, "campaign_ids": sorted(r["campaign_ids"])}
            # "offer_urls": sorted(r["offer_urls"]),
            for r in merged.values()]
    for row in rows:
        row.pop("offer_url", None)
        row.pop("offer_texts", None)

    named: Dict[str, List[str]] = {}
    if adapter is not None and getattr(adapter, "keeps_brand_named_campaigns", False):
        wanted = payload.get("target_brands") or []
        for campaign in campaigns:
            cid = campaign.get("campaign_id")
            if not cid or cid in brands_by_campaign:
                continue
            ours = adapter.campaign_named_brands(campaign, wanted)
            if ours:
                named[cid] = ours

    kept = [{**c, "brands": sorted(brands_by_campaign.get(c["campaign_id"])
                                   or named[c["campaign_id"]])}
            for c in campaigns
            if c.get("campaign_id") in brands_by_campaign
            or c.get("campaign_id") in named]

    put_json({**shared, "campaigns": kept}, "filtered_campaigns/campaigns")
    put_csv([{**c, "brands": ";".join(c["brands"]),
              "source_urls": ";".join(c.get("source_urls") or [])} for c in kept],
            CAMPAIGN_COLUMNS + ["brands"], "filtered_campaigns/campaigns")

    put_json({**shared, "products": rows}, "filtered_campaigns/products")
    put_csv([{**r,
              "campaign_ids": ";".join(r["campaign_ids"]),
              # "offer_urls": ";".join(r["offer_urls"]),
              } for r in rows],
            OFFER_PRODUCT_COLUMNS, "filtered_campaigns/products")
    return []


def write_all(payload: Dict[str, Any], paths, adapter=None,
              formats: Sequence[str] = ("json", "csv")) -> List[Path]:
    """Write one run's results into this retailer's folders."""
    written: List[Path] = []
    formats = {f.lower() for f in formats}

    def put_json(body: Dict[str, Any], folder: str, suffix: str = ".json") -> None:
        if "json" in formats:
            written.append(write_json(body, paths.file(folder, suffix)))

    def put_csv(rows: List[Dict[str, Any]], columns: List[str], folder: str,
                suffix: str = ".csv") -> None:
        if "csv" in formats:
            written.append(_write_csv(rows, columns, paths.file(folder, suffix)))

    products = _flat_products(payload)
    shared = {
        key: payload.get(key)
        for key in ("retailer", "domain", "scraped_at", "run_stats")
    }
    campaigns = payload.get("campaigns") or []
    campaigns_only = bool(payload.get("campaigns_only"))

    if campaigns_only:
        put_json({**shared, "campaigns": campaigns}, "campaigns")
        put_csv([{**c, "source_urls": ";".join(c.get("source_urls") or [])}
                 for c in campaigns], CAMPAIGN_COLUMNS, "campaigns")
        for path in _write_filtered(payload, campaigns, shared, put_json, put_csv, adapter):
            written.append(path)
        put_csv(_flat_rejections(payload), REJECTED_COLUMNS, "rejected")
        return written

    brands_by_campaign: Dict[str, set] = {}

    for brand, rows in sorted(group_by_brand(payload["products"]).items()):
        applied = {cid for row in rows for cid in (row.get("applied_campaigns") or [])}
        for cid in applied:
            brands_by_campaign.setdefault(cid, set()).add(brand)
        put_json({
            **shared,
            "brand": brand,
            "target_brands": [brand],
            "products": rows,
            "campaigns": [c for c in campaigns if c.get("campaign_id") in applied],
            "rejected": [
                r for r in payload.get("rejected") or []
                if match_brand((r.get("payload") or {}).get("brand") or "", [brand])
            ],
        }, _brand_filename_part(brand))

    for brand, rows in sorted(group_by_brand(products).items()):
        put_csv(rows, PRODUCT_COLUMNS, _brand_filename_part(brand))

    put_json({**shared, "campaigns": campaigns}, "campaigns")
    put_csv([{**c, "source_urls": ";".join(c.get("source_urls") or [])}
             for c in campaigns], CAMPAIGN_COLUMNS, "campaigns")
    put_csv(_flat_rejections(payload), REJECTED_COLUMNS, "rejected")
    return written
