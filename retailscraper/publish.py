"""Send a finished run to Cloud Storage and BigQuery.

Does nothing unless GCP_PROJECT, GCS_BUCKET and BQ_DATASET are set, so a
laptop run stays on local disk. Never raises: the scrape is the expensive
part and its files are already safe, so an upload problem is logged and the
run still counts as a success.
"""

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

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
PRODUCT_CAMPAIGNS = "product_campaigns"


def _schemas():
    """The four table schemas, built lazily so the library stays optional."""
    from google.cloud import bigquery as bq

    F = bq.SchemaField
    return {
        RUNS: [
            F("run_id", "STRING", mode="REQUIRED"),
            F("retailer", "STRING", mode="REQUIRED"),
            F("retailer_key", "STRING", mode="REQUIRED"),
            F("domain", "STRING"),
            F("started_at", "TIMESTAMP", mode="REQUIRED"),
            F("finished_at", "TIMESTAMP"),
            F("duration_seconds", "FLOAT64"),
            F("status", "STRING"),
            F("reason", "STRING"),
            F("products", "INT64"),
            F("expected_products", "INT64"),
            F("campaigns", "INT64"),
            F("rejected", "INT64"),
            F("requests_total", "INT64"),
            F("requests_refused", "INT64"),
            F("brands_requested", "STRING", mode="REPEATED"),
            F("brands_empty", "STRING", mode="REPEATED"),
            F("warnings", "STRING", mode="REPEATED"),
        ],
        PRODUCTS: [
            F("run_id", "STRING", mode="REQUIRED"),
            F("run_started_at", "TIMESTAMP", mode="REQUIRED"),
            F("retailer", "STRING", mode="REQUIRED"),
            F("product_id", "STRING", mode="REQUIRED"),
            F("brand", "STRING"),
            F("brand_matched_to", "STRING"),
            F("category", "STRING"),
            F("product_title", "STRING"),
            F("product_url", "STRING"),
            F("current_price", "NUMERIC"),
            F("original_price", "NUMERIC"),
            F("discount_amount", "NUMERIC"),
            F("discount_percent", "NUMERIC"),
            F("price_is_from", "BOOL"),
            F("availability", "STRING"),
            F("variant_count", "INT64"),
            F("promotional_copy", "STRING"),
            F("scraped_at", "TIMESTAMP"),
        ],
        CAMPAIGNS: [
            F("run_id", "STRING", mode="REQUIRED"),
            F("run_started_at", "TIMESTAMP", mode="REQUIRED"),
            F("retailer", "STRING", mode="REQUIRED"),
            F("campaign_id", "STRING", mode="REQUIRED"),
            F("promotion_text", "STRING"),
            F("promotion_type", "STRING"),
            F("scope", "STRING"),
            F("promo_code", "STRING"),
            F("landing_url", "STRING"),
            F("source_url", "STRING"),
        ],
        PRODUCT_CAMPAIGNS: [
            F("run_id", "STRING", mode="REQUIRED"),
            F("run_started_at", "TIMESTAMP", mode="REQUIRED"),
            F("retailer", "STRING", mode="REQUIRED"),
            F("product_id", "STRING", mode="REQUIRED"),
            F("campaign_id", "STRING", mode="REQUIRED"),
        ],
    }


_PARTITION_FIELD = {
    RUNS: "started_at",
    PRODUCTS: "run_started_at",
    CAMPAIGNS: "run_started_at",
    PRODUCT_CAMPAIGNS: "run_started_at",
}

_CLUSTER_FIELDS = {
    RUNS: ["retailer_key"],
    PRODUCTS: ["retailer", "brand_matched_to"],
    CAMPAIGNS: ["retailer", "campaign_id"],
    PRODUCT_CAMPAIGNS: ["retailer", "campaign_id"],
}


def _money(value: Any) -> Optional[str]:
    """NUMERIC wants a string, so floats never lose a penny in transit."""
    return None if value is None else str(value)


def _run_row(manifest: Dict[str, Any]) -> Dict[str, Any]:
    keep = ("run_id", "retailer", "retailer_key", "domain", "started_at",
            "finished_at", "duration_seconds", "status", "reason", "products",
            "expected_products", "campaigns", "rejected", "requests_total",
            "requests_refused", "brands_requested", "brands_empty", "warnings")
    return {key: manifest.get(key) for key in keep}


def _product_rows(payload, run_id, started_at, retailer) -> List[Dict[str, Any]]:
    rows = []
    for p in payload.get("products") or []:
        rows.append({
            "run_id": run_id,
            "run_started_at": started_at,
            "retailer": retailer,
            "product_id": p.get("product_id"),
            "brand": p.get("brand"),
            "brand_matched_to": p.get("brand_matched_to"),
            "category": p.get("category"),
            "product_title": p.get("product_title"),
            "product_url": p.get("product_url"),
            "current_price": _money(p.get("current_price")),
            "original_price": _money(p.get("original_price")),
            "discount_amount": _money(p.get("discount_amount")),
            "discount_percent": _money(p.get("discount_percent")),
            "price_is_from": p.get("price_is_from"),
            "availability": p.get("availability"),
            "variant_count": p.get("variant_count"),
            "promotional_copy": p.get("promotional_copy"),
            "scraped_at": p.get("scraped_at"),
        })
    return rows


def _campaign_rows(payload, run_id, started_at, retailer) -> List[Dict[str, Any]]:
    rows = []
    for c in payload.get("campaigns") or []:
        rows.append({
            "run_id": run_id,
            "run_started_at": started_at,
            "retailer": retailer,
            "campaign_id": c.get("campaign_id"),
            "promotion_text": c.get("promotion_text"),
            "promotion_type": c.get("promotion_type"),
            "scope": c.get("scope"),
            "promo_code": c.get("promo_code"),
            "landing_url": c.get("landing_url"),
            "source_url": c.get("source_url"),
        })
    return rows


def _bridge_rows(payload, run_id, started_at, retailer) -> List[Dict[str, Any]]:
    rows = []
    for p in payload.get("products") or []:
        for campaign_id in p.get("applied_campaigns") or []:
            rows.append({
                "run_id": run_id,
                "run_started_at": started_at,
                "retailer": retailer,
                "product_id": p.get("product_id"),
                "campaign_id": campaign_id,
            })
    return rows


def _ensure_table(client, table_id: str, name: str):
    from google.cloud import bigquery as bq

    table = bq.Table(table_id, schema=_schemas()[name])
    table.time_partitioning = bq.TimePartitioning(field=_PARTITION_FIELD[name])
    table.clustering_fields = _CLUSTER_FIELDS[name]
    return client.create_table(table, exists_ok=True)


def _already_loaded(client, table_id: str, run_id: str) -> bool:
    from google.cloud import bigquery as bq

    job = client.query(
        f"SELECT COUNT(*) AS n FROM `{table_id}` WHERE run_id = @run_id",
        job_config=bq.QueryJobConfig(query_parameters=[
            bq.ScalarQueryParameter("run_id", "STRING", run_id)]),
    )
    return next(iter(job.result())).n > 0


def _upload(bucket, local: Path, blob_name: str) -> bool:
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(str(local))
    return bucket.blob(blob_name).exists()


def _load(client, bucket_name: str, blob_name: str, table_id: str) -> int:
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


def publish(payload: Dict[str, Any], manifest: Dict[str, Any],
            files: Sequence[Path], run_folder: str) -> List[str]:
    """Archive one run's files and load it into BigQuery. Returns warnings.

    Never raises. run.py calls this inside its own try block, so an exception
    here would turn a finished run into a failed one.
    """
    if not publishing_enabled():
        return []
    try:
        return _publish(payload, manifest, files, run_folder)
    except Exception as exc:  # pragma: no cover - the files are safe on disk
        log.exception("publishing failed")
        return [f"publishing to Google Cloud failed ({exc}); the files are still on disk"]


def _stamp(files: Sequence[Path]) -> Optional[str]:
    """The run's timestamp, taken from any of its filenames."""
    for path in files:
        name = Path(path).stem
        if re.fullmatch(r"\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d", name):
            return name
    return None


def _publish(payload: Dict[str, Any], manifest: Dict[str, Any],
             files: Sequence[Path], run_folder: str) -> List[str]:

    warnings: List[str] = []
    run_id = manifest.get("run_id")
    started_at = manifest.get("started_at")
    retailer = manifest.get("retailer")
    status = manifest.get("status")
    stamp = _stamp(files) or run_id

    try:
        from google.cloud import bigquery, storage
    except ImportError:
        return ["google-cloud-storage and google-cloud-bigquery are not installed, "
                "so nothing was uploaded"]

    try:
        bucket = storage.Client(project=GCP_PROJECT).bucket(GCS_BUCKET)
        client = bigquery.Client(project=GCP_PROJECT)
    except Exception as exc:
        return [f"could not reach Google Cloud ({exc}); the files are still on disk"]

    uploaded = []
    for path in files:
        path = Path(path)
        if not path.exists():
            continue
        blob_name = f"{run_folder}/{path.parent.name}/{path.name}"
        try:
            if _upload(bucket, path, blob_name):
                uploaded.append((path, blob_name))
            else:
                warnings.append(f"{path.name} did not arrive in the bucket")
        except Exception as exc:
            warnings.append(f"could not upload {path.name} ({exc})")

    log.info("uploaded %d file(s) to gs://%s/%s", len(uploaded), GCS_BUCKET, stamp)

    tables = {name: f"{GCP_PROJECT}.{BQ_DATASET}.{name}"
              for name in (RUNS, PRODUCTS, CAMPAIGNS, PRODUCT_CAMPAIGNS)}
    try:
        for name, table_id in tables.items():
            _ensure_table(client, table_id, name)
    except Exception as exc:
        warnings.append(f"could not create the BigQuery tables ({exc})")
        return warnings

    # A failed run still gets its runs row, so the morning check shows it.
    batches = {RUNS: [_run_row(manifest)]}
    if status == "ok":
        batches[PRODUCTS] = _product_rows(payload, run_id, started_at, retailer)
        batches[CAMPAIGNS] = _campaign_rows(payload, run_id, started_at, retailer)
        batches[PRODUCT_CAMPAIGNS] = _bridge_rows(payload, run_id, started_at, retailer)
    else:
        warnings.append(f"run status is {status!r}, so only the runs row was loaded")

    # Checked per table, not once: if products failed last time but runs did
    # not, a retry has to load products rather than skip everything.
    skipped = []
    for name, rows in batches.items():
        if not rows:
            continue
        try:
            if _already_loaded(client, tables[name], run_id):
                skipped.append(name)
                continue
        except Exception as exc:
            warnings.append(f"could not check whether {run_id} is already in {name} ({exc})")
            continue

        blob_name = f"{run_folder}/bigquery/{stamp}.{name}.ndjson"
        body = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows)
        try:
            bucket.blob(blob_name).upload_from_string(body, content_type="application/json")
            loaded = _load(client, GCS_BUCKET, blob_name, tables[name])
            log.info("loaded %d row(s) into %s", loaded, name)
        except Exception as exc:
            warnings.append(f"could not load {name} into BigQuery ({exc})")

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
