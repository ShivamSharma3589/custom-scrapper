"""Send a finished run to Cloud Storage and BigQuery.

Does nothing unless GCP_PROJECT, GCS_BUCKET and BQ_DATASET are set, so a
laptop run stays on local disk. Never raises: the scrape is the expensive
part and its files are already safe, so an upload problem is logged and the
run still counts as a success.
"""

import json
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from config import (
    BQ_DATASET,
    DELETE_LOCAL_AFTER_UPLOAD,
    GCP_PROJECT,
    GCS_BUCKET,
    publishing_enabled,
)

log = logging.getLogger(__name__)

RUNS = "runs"
PRODUCTS = "products"
CAMPAIGNS = "campaigns"
MAPPING = "campaigns_products_mapping"

#: Tables carrying a run_id, so the same run must not be loaded twice.
_APPEND_ONLY = (RUNS, MAPPING)


def _schemas():
    """The four table schemas, built once the BigQuery library is available."""
    from google.cloud import bigquery as bq

    F = bq.SchemaField
    return {
        RUNS: [
            F("run_id", "STRING", mode="REQUIRED"),
            F("retailer", "STRING", mode="REQUIRED"),
            F("domain", "STRING"),
            F("started_at", "TIMESTAMP", mode="REQUIRED"),
            F("finished_at", "TIMESTAMP"),
            F("duration_seconds", "FLOAT64"),
            F("status", "STRING"),
            F("products", "INT64"),
            F("expected_products", "INT64"),
            F("campaigns", "INT64"),
            F("rejected", "INT64"),
            F("requests_total", "INT64"),
        ],
        PRODUCTS: [
            F("first_seen", "TIMESTAMP", mode="REQUIRED"),
            F("last_seen", "TIMESTAMP", mode="REQUIRED"),
            F("retailer", "STRING", mode="REQUIRED"),
            F("product_id", "STRING", mode="REQUIRED"),
            F("brand", "STRING"),
            F("category", "STRING"),
            F("product_title", "STRING"),
            F("product_url", "STRING"),
            F("current_price", "NUMERIC"),
            F("original_price", "NUMERIC"),
            F("discount_amount", "NUMERIC"),
            F("discount_percent", "NUMERIC"),
            F("price_is_from", "BOOL"),
            F("currency", "STRING"),
            F("variant_count", "STRING"),
            F("scraped_at", "TIMESTAMP", mode="REQUIRED"),
        ],
        CAMPAIGNS: [
            F("first_seen", "TIMESTAMP", mode="REQUIRED"),
            F("brand", "STRING"),
            F("last_seen", "TIMESTAMP", mode="REQUIRED"),
            F("retailer", "STRING", mode="REQUIRED"),
            F("campaign_id", "STRING", mode="REQUIRED"),
            F("promotion_text", "STRING", mode="REQUIRED"),
            F("promotion_type", "STRING", mode="REQUIRED"),
            F("scope", "STRING"),
            F("scope_value", "STRING"),
            F("promo_code", "STRING"),
            F("landing_url", "STRING"),
            F("source_url", "STRING"),
        ],
        MAPPING: [
            F("run_id", "STRING", mode="REQUIRED"),
            F("run_at", "TIMESTAMP", mode="REQUIRED"),
            F("product_id", "STRING", mode="REQUIRED"),
            F("campaign_id", "STRING", mode="REQUIRED"),
        ],
    }


#: What identifies a product, and what a later sighting refreshes.
_PRODUCT_KEYS = ["retailer", "product_id"]

_PRODUCT_FIELDS = [
    "brand",
    "category",
    "product_title",
    "product_url",
    "current_price",
    "original_price",
    "discount_amount",
    "discount_percent",
    "price_is_from",
    "currency",
    "variant_count",
    "scraped_at",
]

#: A row missing any of these would be rejected by a NOT NULL column.
_REQUIRED = {
    RUNS: ["run_id", "retailer", "started_at"],
    PRODUCTS: ["retailer", "product_id", "first_seen", "last_seen", "scraped_at"],
    CAMPAIGNS: ["retailer", "campaign_id", "promotion_text", "promotion_type",
                "first_seen", "last_seen"],
    MAPPING: ["run_id", "run_at", "product_id", "campaign_id"],
}


def _money(value: Any) -> Optional[str]:
    """NUMERIC wants a string, so floats never lose a penny in transit."""
    return None if value is None else str(value)


def _text(value: Any) -> Optional[str]:
    """A STRING column, for a value the scrape may hold as a number."""
    return None if value is None else str(value)


def _run_row(manifest: Dict[str, Any]) -> Dict[str, Any]:
    keep = ("run_id", "retailer", "domain", "started_at", "finished_at",
            "duration_seconds", "status", "products", "expected_products",
            "campaigns", "rejected", "requests_total")
    return {key: manifest.get(key) for key in keep}


def _product_rows(products, started_at, retailer) -> List[Dict[str, Any]]:
    """One row per product. A product already in BigQuery keeps its
    `first_seen`; `last_seen` and the current values move forward."""
    rows = []
    for p in products:
        rows.append({
            "first_seen": started_at,
            "last_seen": started_at,
            "retailer": p.get("retailer") or retailer,
            "product_id": p.get("product_id"),
            "brand": p.get("brand"),
            "category": p.get("category"),
            "product_title": p.get("product_title"),
            "product_url": p.get("product_url"),
            "current_price": _money(p.get("current_price")),
            "original_price": _money(p.get("original_price")),
            "discount_amount": _money(p.get("discount_amount")),
            "discount_percent": _money(p.get("discount_percent")),
            "price_is_from": p.get("price_is_from"),
            "currency": p.get("currency"),
            "variant_count": _text(p.get("variant_count")),
            "scraped_at": p.get("scraped_at") or started_at,
        })
    return rows


def _campaign_rows(campaigns, started_at, retailer) -> List[Dict[str, Any]]:
    """One row per campaign, per brand it applies to"""
    rows = []
    for c in campaigns:
        urls = c.get("source_urls") or ([c["source_url"]] if c.get("source_url") else [])
        base = {
            "first_seen": started_at,
            "last_seen": started_at,
            "retailer": c.get("retailer") or retailer,
            "campaign_id": c.get("campaign_id"),
            "promotion_text": c.get("promotion_text"),
            "promotion_type": c.get("promotion_type"),
            "scope": c.get("scope"),
            "scope_value": c.get("scope_value"),
            "promo_code": c.get("promo_code"),
            "landing_url": c.get("landing_url"),
            "source_url": urls[0] if urls else None,
        }
        for brand in (c.get("brands") or [None]):
            rows.append({**base, "brand": brand})
    return rows


def _mapping_rows(products, run_id, started_at) -> List[Dict[str, Any]]:
    rows = []
    seen = set()
    for p in products:
        for campaign_id in p.get("campaign_ids") or p.get("applied_campaigns") or []:
            key = (p.get("product_id"), campaign_id)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "run_id": run_id,
                "run_at": started_at,
                "product_id": p.get("product_id"),
                "campaign_id": campaign_id,
            })
    return rows


def _drop_invalid(name: str, rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Remove rows that would break a NOT NULL column. Returns (kept, dropped)."""
    required = _REQUIRED.get(name)
    if not required:
        return rows, 0
    kept = [r for r in rows if all(r.get(col) not in (None, "") for col in required)]
    return kept, len(rows) - len(kept)


def _already_loaded(client, table_id: str, run_id: str) -> bool:
    from google.cloud import bigquery as bq

    job = client.query(
        f"SELECT COUNT(*) AS n FROM `{table_id}` WHERE run_id = @run_id",
        job_config=bq.QueryJobConfig(query_parameters=[
            bq.ScalarQueryParameter("run_id", "STRING", run_id)]),
    )
    return next(iter(job.result())).n > 0


def _blob_name(path: Path, run_folder: str) -> str:
    """Where a local file belongs in the bucket: the same path it has on disk"""
    parts = list(path.parts)
    if run_folder in parts:
        start = len(parts) - 1 - parts[::-1].index(run_folder)
        return "/".join(parts[start:])
    return f"{run_folder}/{path.name}"


def _upload(bucket, local: Path, blob_name: str) -> bool:
    bucket.blob(blob_name).upload_from_filename(str(local))
    return bucket.blob(blob_name).exists()


def _load(client, bucket_name: str, blob_name: str, table_id: str) -> int:
    """Append the staged rows to the table. Returns how many were loaded."""
    from google.cloud import bigquery as bq

    job = client.load_table_from_uri(
        f"gs://{bucket_name}/{blob_name}",
        table_id,
        job_config=bq.LoadJobConfig(
            source_format=bq.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=bq.WriteDisposition.WRITE_APPEND,
        ),
    )
    job.result()
    return job.output_rows or 0


def _merge_products(client, bucket_name: str, blob_name: str, table_id: str) -> int:
    """Upsert products on (retailer, product_id)."""
    from google.cloud import bigquery as bq

    staging_id = f"{table_id}_staging_{uuid.uuid4().hex[:8]}"
    load = client.load_table_from_uri(
        f"gs://{bucket_name}/{blob_name}",
        staging_id,
        job_config=bq.LoadJobConfig(
            source_format=bq.SourceFormat.NEWLINE_DELIMITED_JSON,
            schema=_schemas()[PRODUCTS],
            write_disposition=bq.WriteDisposition.WRITE_TRUNCATE,
        ),
    )
    load.result()

    columns = [*_PRODUCT_KEYS, *_PRODUCT_FIELDS, "first_seen", "last_seen"]
    keys = ", ".join(_PRODUCT_KEYS)
    # One run can see a product on several pages, so the staged rows are
    # collapsed to one per product before the MERGE sees them.
    picked = ", ".join(f"ANY_VALUE({f}) AS {f}" for f in _PRODUCT_FIELDS)
    picked += ", MIN(first_seen) AS first_seen, MAX(last_seen) AS last_seen"
    not_null = " AND ".join(f"{k} IS NOT NULL" for k in _PRODUCT_KEYS)
    on = " AND ".join(f"target.{k} = source.{k}" for k in _PRODUCT_KEYS)
    updates = [f"{f} = source.{f}" for f in _PRODUCT_FIELDS]
    updates.append("last_seen = GREATEST(target.last_seen, source.last_seen)")
    values = ", ".join(f"source.{c}" for c in columns)

    try:
        merge = client.query(
            f"MERGE `{table_id}` AS target "
            f"USING (SELECT {keys}, {picked} "
            f"FROM `{staging_id}` WHERE {not_null} "
            f"GROUP BY {keys}) AS source "
            f"ON {on} "
            f"WHEN MATCHED THEN UPDATE SET {', '.join(updates)} "
            f"WHEN NOT MATCHED THEN INSERT ({', '.join(columns)}) VALUES ({values})"
        )
        merge.result()
        return merge.num_dml_affected_rows or 0
    finally:
        client.delete_table(staging_id, not_found_ok=True)


def publish(payload: Dict[str, Any], manifest: Dict[str, Any],
            files: Sequence[Path], run_folder: str, adapter=None) -> List[str]:
    """Archive one run's files and load it into BigQuery. Returns warnings.

    Never raises. run.py calls this inside its own try block, so an exception
    here would turn a finished run into a failed one.
    """
    if not publishing_enabled():
        return []
    try:
        return _publish(payload, manifest, files, run_folder, adapter)
    except Exception as exc:  # pragma: no cover - the files are safe on disk
        log.exception("publishing failed")
        return [f"publishing to Google Cloud failed ({exc}); the files are still on disk"]


def _client_facing(payload: Dict[str, Any], adapter) -> Tuple[List[Dict], List[Dict]]:
    """The campaigns and products as written to `filtered_campaigns/`."""
    from .output import build_filtered

    campaigns = payload.get("campaigns") or []
    if payload.get("campaigns_only"):
        return build_filtered(payload, campaigns, adapter)
    return campaigns, list(payload.get("products") or [])


def _publish(payload: Dict[str, Any], manifest: Dict[str, Any],
             files: Sequence[Path], run_folder: str, adapter=None) -> List[str]:

    warnings: List[str] = []
    run_id = manifest.get("run_id")
    started_at = manifest.get("started_at")
    retailer = manifest.get("retailer")
    status = manifest.get("status")

    try:
        from google.cloud import bigquery as bq
        from google.cloud import storage
    except ImportError:
        return ["google-cloud-storage and google-cloud-bigquery are not installed, "
                "so nothing was uploaded"]

    try:
        bucket = storage.Client(project=GCP_PROJECT).bucket(GCS_BUCKET)
        client = bq.Client(project=GCP_PROJECT)
    except Exception as exc:
        return [f"could not reach Google Cloud ({exc}); the files are still on disk"]

    uploaded = []
    for path in files:
        path = Path(path)
        if not path.exists():
            continue
        blob_name = _blob_name(path, run_folder)
        try:
            if _upload(bucket, path, blob_name):
                uploaded.append((path, blob_name))
            else:
                warnings.append(f"{path.name} did not arrive in the bucket")
        except Exception as exc:
            warnings.append(f"could not upload {path.name} ({exc})")

    log.info("uploaded %d file(s) to gs://%s/%s/", len(uploaded), GCS_BUCKET, run_folder)

    tables = {name: f"{GCP_PROJECT}.{BQ_DATASET}.{name}"
              for name in (RUNS, PRODUCTS, CAMPAIGNS, MAPPING)}

    # A failed run still gets its runs row, so the morning check shows it.
    batches = {RUNS: [_run_row(manifest)]}
    if status == "ok":
        campaigns, products = _client_facing(payload, adapter)
        batches[PRODUCTS] = _product_rows(products, started_at, retailer)
        batches[CAMPAIGNS] = _campaign_rows(campaigns, started_at, retailer)
        batches[MAPPING] = _mapping_rows(products, run_id, started_at)
    else:
        warnings.append(f"run status is {status!r}, so only the runs row was loaded")

    # Checked per table, not once: if products failed last time but runs did
    # not, a retry has to load products rather than skip everything.
    skipped = []
    for name, rows in batches.items():
        rows, dropped = _drop_invalid(name, rows)
        if dropped:
            warnings.append(
                f"{dropped} row(s) for {name} were skipped because a required "
                f"field was empty ({', '.join(_REQUIRED[name])})")
        if not rows:
            continue

        # Products are upserted, so re-running is the point. The tables that
        # carry a run_id must not take the same run twice.
        if name in _APPEND_ONLY:
            try:
                if _already_loaded(client, tables[name], run_id):
                    skipped.append(name)
                    continue
            except Exception as exc:
                warnings.append(
                    f"could not check whether {run_id} is already in {name} ({exc})")
                continue

        blob_name = f"{run_folder}/bigquery/{run_id}.{name}.ndjson"
        body = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows)
        try:
            bucket.blob(blob_name).upload_from_string(body, content_type="application/json")
            if name == PRODUCTS:
                loaded = _merge_products(client, GCS_BUCKET, blob_name, tables[name])
                log.info("upserted %d product row(s)", loaded)
            else:
                loaded = _load(client, GCS_BUCKET, blob_name, tables[name])
                log.info("loaded %d row(s) into %s", loaded, name)
        except Exception as exc:
            warnings.append(f"could not load {name} into BigQuery ({exc}); "
                            f"the table must already exist and match the schema")

    if skipped:
        warnings.append(f"run {run_id} was already in {', '.join(skipped)}, "
                        f"so those were not loaded again")

    if DELETE_LOCAL_AFTER_UPLOAD and not warnings:
        removed = 0
        for path, _ in uploaded:
            # The manifest stays: scrape_job.py reads it after the run exits.
            if path.parent.name == "manifest":
                continue
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                warnings.append(f"could not delete {path.name} after upload ({exc})")
        log.info("removed %d local file(s) now held in Cloud Storage", removed)

    return warnings
