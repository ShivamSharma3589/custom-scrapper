"""Send a finished run to Cloud Storage and BigQuery.

Does nothing unless GCP_PROJECT, GCS_BUCKET and BQ_DATASET are set, so a
laptop run stays on local disk. Never raises: the scrape is the expensive
part and its files are already safe, so an upload problem is logged and the
run still counts as a success.
"""

import io
import json
import logging
import uuid
from pathlib import Path
from time import sleep
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
            F("product_offer", "STRING"),
        ],
        CAMPAIGNS: [
            F("retailer", "STRING", mode="REQUIRED"),
            F("brand", "STRING", mode="REQUIRED"),
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
            F("product_id", "STRING"),
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
    "product_offer",
]

_CAMPAIGN_KEYS = ["retailer", "campaign_id"]

_CAMPAIGN_FIELDS = [
    "brand",
    "promotion_text",
    "promotion_type",
    "scope",
    "scope_value",
    "promo_code",
    "landing_url",
    "source_url",
]

#: A row missing any of these would be rejected by a NOT NULL column.
_REQUIRED = {
    RUNS: ["run_id", "retailer", "started_at"],
    PRODUCTS: ["retailer", "product_id", "first_seen", "last_seen", "scraped_at"],
    CAMPAIGNS: ["retailer", "brand", "campaign_id",
                "promotion_text", "promotion_type"],
    MAPPING: ["run_id", "run_at", "campaign_id"],
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
            "product_offer": (p.get("product_offer") or p.get("offer_text")
                              or p.get("promotional_copy")),
        })
    return rows

#: Query them back with UNNEST(SPLIT(brand, ',')).
BRAND_SEPARATOR = ","


def _narrowness(url: str):
    """How far a listing has been filtered down: fewer is broader."""
    path = url.split("?")[0]
    head, _, facets = path.partition("/_/")
    return (len([s for s in head.split("/") if s]), facets.count("Z"), len(path))


def _widest_url(urls: Sequence[str]) -> Optional[str]:
    """One page that covers every brand the campaign row names, or None if no URL is known"""
    if not urls:
        return None
    if len(urls) == 1:
        return urls[0]

    origins, heads, codes = set(), [], []
    for url in urls:
        head, sep, facets = url.split("?")[0].partition("/_/")
        if not sep or "//" not in head:
            return min(urls, key=_narrowness)
        origin, _, path = head.partition("//")[2].partition("/")
        origins.add(head.partition("//")[0] + "//" + origin)
        heads.append([s for s in path.split("/") if s])
        codes.append(facets.split("Z"))

    shared: List[str] = []
    for parts in zip(*heads):
        if len(set(parts)) != 1:
            break
        shared.append(parts[0])

    # Every listing under one category opens with that category's own code.
    root = codes[0][0] if len({c[0] for c in codes}) == 1 else None
    if len(origins) != 1 or not root or not shared:
        return min(urls, key=_narrowness)

    parent = f"{origins.pop()}/{'/'.join(shared)}/_/N-{root.removeprefix('N-')}"
    return min([parent, *urls], key=_narrowness)


def _campaign_rows(campaigns, started_at, retailer) -> List[Dict[str, Any]]:
    """One row per campaign, with every brand it applies to in `brand`"""
    rows = []
    for c in campaigns:
        urls = c.get("source_urls") or ([c["source_url"]] if c.get("source_url") else [])
        rows.append({
            "retailer": c.get("retailer") or retailer,
            "brand": BRAND_SEPARATOR.join(c.get("brands") or []) or None,
            "campaign_id": c.get("campaign_id"),
            "promotion_text": c.get("promotion_text"),
            "promotion_type": c.get("promotion_type"),
            "scope": c.get("scope"),
            "scope_value": c.get("scope_value"),
            "promo_code": c.get("promo_code"),
            "landing_url": c.get("landing_url"),
            "source_url": _widest_url(urls),
        })
    return rows


def _mapping_rows(products, campaigns, run_id, started_at) -> List[Dict[str, Any]]:
    """Which products each campaign covered on this run"""
    rows = []
    seen = set()
    mapped = set()
    for p in products:
        for campaign_id in p.get("campaign_ids") or p.get("applied_campaigns") or []:
            key = (p.get("product_id"), campaign_id)
            if key in seen:
                continue
            seen.add(key)
            mapped.add(campaign_id)
            rows.append({
                "run_id": run_id,
                "run_at": started_at,
                "product_id": p.get("product_id"),
                "campaign_id": campaign_id,
            })

    for c in campaigns:
        campaign_id = c.get("campaign_id")
        if campaign_id and campaign_id not in mapped:
            rows.append({
                "run_id": run_id,
                "run_at": started_at,
                "product_id": None,
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


def _dataset_name() -> str:
    """BQ_DATASET as a bare dataset name.

    A dataset is often written with its project attached, as
    `project.dataset` or the legacy `project:dataset`. Pasting either into
    BQ_DATASET used to build a four-part table id, which BigQuery rejects with
    "ProjectId must be non-empty", so only the last segment is kept.
    """
    return BQ_DATASET.replace(":", ".").rstrip(".").split(".")[-1].strip()


def _table_ref(name: str):
    """A typed reference to one of our tables, so a malformed id cannot reach SQL."""
    from google.cloud import bigquery as bq

    return bq.DatasetReference(GCP_PROJECT, _dataset_name()).table(name)


def _table_id(name: str) -> str:
    """The backticked form, for the one statement that has to be text."""
    return f"{GCP_PROJECT}.{_dataset_name()}.{name}"


def _schema_drift(client, name: str) -> List[str]:
    """Columns this script writes that the live table has not got"""
    try:
        live = {f.name for f in client.get_table(_table_ref(name)).schema}
    except Exception as exc:
        return [f"could not read the schema of {_table_id(name)} ({exc})"]
    unknown = [f.name for f in _schemas()[name] if f.name not in live]
    if unknown:
        return [f"{_table_id(name)} has no column(s) {', '.join(unknown)}; "
                f"nothing was loaded into it"]
    return []


def _already_loaded(client, name: str, run_id: str) -> bool:
    """Whether this run is already in an append-only table.

    The table is named by a reference rather than interpolated into the SQL:
    the project and dataset then come from the client's own objects instead of
    a string that may be missing a part.
    """
    from google.cloud import bigquery as bq

    job = client.query(
        f"SELECT COUNT(*) AS n FROM `{name}` WHERE run_id = @run_id",
        job_config=bq.QueryJobConfig(
            default_dataset=bq.DatasetReference(GCP_PROJECT, _dataset_name()),
            query_parameters=[bq.ScalarQueryParameter("run_id", "STRING", run_id)],
        ),
    )
    return next(iter(job.result())).n > 0


def _blob_name(path: Path, run_folder: str) -> str:
    """Where a local file belongs in the bucket: the same path it has on disk"""
    parts = list(path.parts)
    if run_folder in parts:
        start = len(parts) - 1 - parts[::-1].index(run_folder)
        return "/".join(parts[start:])
    return f"{run_folder}/{path.name}"


RETRY_ATTEMPTS = 3
RETRY_FIRST_DELAY = 2.0


def _retry(what: str, call):
    """Call it up to RETRY_ATTEMPTS times, doubling the wait in between."""
    delay = RETRY_FIRST_DELAY
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return call()
        except Exception as exc:
            if attempt == RETRY_ATTEMPTS:
                raise
            log.warning("%s failed on attempt %d of %d (%s); retrying in %.0fs",
                        what, attempt, RETRY_ATTEMPTS, exc, delay)
            sleep(delay)
            delay *= 2


def _upload(bucket, local: Path, blob_name: str) -> bool:
    _retry(f"uploading {local.name}",
           lambda: bucket.blob(blob_name).upload_from_filename(str(local)))
    return bucket.blob(blob_name).exists()


def _load(client, body: str, table_id) -> int:
    """Append the rows to the table. Returns how many were loaded."""
    from google.cloud import bigquery as bq

    def attempt():
        # A fresh stream each time: a retry cannot read a consumed one.
        job = client.load_table_from_file(
            io.BytesIO(body.encode("utf-8")),
            table_id,
            job_config=bq.LoadJobConfig(
                source_format=bq.SourceFormat.NEWLINE_DELIMITED_JSON,
                write_disposition=bq.WriteDisposition.WRITE_APPEND,
            ),
        )
        job.result()
        return job.output_rows or 0

    return _retry(f"loading {table_id}", attempt)


#: What each upserted table matches on, and what a later run overwrites.
_UPSERT = {
    PRODUCTS: (_PRODUCT_KEYS, _PRODUCT_FIELDS, True),
    CAMPAIGNS: (_CAMPAIGN_KEYS, _CAMPAIGN_FIELDS, False),
}


def _merge(client, body: str, name: str) -> int:
    """Upsert one table's rows on its own key"""
    from google.cloud import bigquery as bq

    keys, fields, track_seen = _UPSERT[name]
    table_id = _table_id(name)
    staging = _table_ref(f"{name}_staging_{uuid.uuid4().hex[:8]}")
    staging_id = f"{GCP_PROJECT}.{_dataset_name()}.{staging.table_id}"

    def stage():
        # A fresh stream each time: a retry cannot read a consumed one.
        load = client.load_table_from_file(
            io.BytesIO(body.encode("utf-8")),
            staging,
            job_config=bq.LoadJobConfig(
                source_format=bq.SourceFormat.NEWLINE_DELIMITED_JSON,
                schema=_schemas()[name],
                write_disposition=bq.WriteDisposition.WRITE_TRUNCATE,
            ),
        )
        load.result()

    _retry(f"staging {name}", stage)

    seen_cols = ["first_seen", "last_seen"] if track_seen else []
    columns = [*keys, *fields, *seen_cols]
    key_list = ", ".join(keys)
    # One run can see the same key on several pages, so the staged rows are
    # collapsed to one per key before the MERGE sees them.
    picked = ", ".join(f"ANY_VALUE({f}) AS {f}" for f in fields)
    if track_seen:
        picked += ", MIN(first_seen) AS first_seen, MAX(last_seen) AS last_seen"
    not_null = " AND ".join(f"{k} IS NOT NULL" for k in keys)
    on = " AND ".join(f"target.{k} = source.{k}" for k in keys)
    updates = [f"{f} = source.{f}" for f in fields]
    if track_seen:
        updates.append("last_seen = GREATEST(target.last_seen, source.last_seen)")
    values = ", ".join(f"source.{c}" for c in columns)

    sql = (
        f"MERGE `{table_id}` AS target "
        f"USING (SELECT {key_list}, {picked} "
        f"FROM `{staging_id}` WHERE {not_null} "
        f"GROUP BY {key_list}) AS source "
        f"ON {on} "
        f"WHEN MATCHED THEN UPDATE SET {', '.join(updates)} "
        f"WHEN NOT MATCHED THEN INSERT ({', '.join(columns)}) VALUES ({values})"
    )

    def merge():
        job = client.query(sql)
        job.result()
        return job.num_dml_affected_rows or 0

    try:
        return _retry(f"merging {name}", merge)
    finally:
        client.delete_table(staging, not_found_ok=True)


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

    # Storage and BigQuery stand on their own: either can fail without
    # stopping the other.
    bucket = client = None
    try:
        bucket = storage.Client(project=GCP_PROJECT).bucket(GCS_BUCKET)
    except Exception as exc:
        warnings.append(f"could not reach Cloud Storage ({exc}); "
                        f"the files are still on disk")
    try:
        client = bq.Client(project=GCP_PROJECT)
    except Exception as exc:
        warnings.append(f"could not reach BigQuery ({exc})")
    if bucket is None and client is None:
        return warnings

    uploaded = []
    for path in files if bucket is not None else ():
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

    if client is not None:
        if _dataset_name() != BQ_DATASET.strip():
            warnings.append(
                f"BQ_DATASET is {BQ_DATASET!r}; reading it as dataset "
                f"{_dataset_name()!r} in project {GCP_PROJECT!r}")
        log.info("BigQuery target: %s.%s", GCP_PROJECT, _dataset_name())
    tables = {name: _table_ref(name) for name in (RUNS, PRODUCTS, CAMPAIGNS, MAPPING)}

    # A failed run still gets its runs row, so the morning check shows it.
    batches = {RUNS: [_run_row(manifest)]}
    if status == "ok":
        campaigns, products = _client_facing(payload, adapter)
        batches[RUNS] = [{**batches[RUNS][0], "campaigns": len(campaigns),
                          "products": len(products)}]
        batches[PRODUCTS] = _product_rows(products, started_at, retailer)
        batches[CAMPAIGNS] = _campaign_rows(campaigns, started_at, retailer)
        batches[MAPPING] = _mapping_rows(products, campaigns, run_id, started_at)
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

        body = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows)

        # Archived whatever BigQuery does, so a failed load can be replayed.
        if bucket is not None:
            blob_name = f"{run_folder}/bigquery/{run_id}.{name}.ndjson"
            try:
                _retry(f"archiving the {name} rows",
                       lambda: bucket.blob(blob_name).upload_from_string(
                           body, content_type="application/json"))
            except Exception as exc:
                warnings.append(f"could not archive the {name} rows to the bucket ({exc})")

        if client is None:
            continue

        # Products and campaigns are upserted, so re-running is the point. The
        # tables that carry a run_id must not take the same run twice.
        if name in _APPEND_ONLY:
            try:
                if _already_loaded(client, name, run_id):
                    skipped.append(name)
                    continue
            except Exception as exc:
                warnings.append(
                    f"could not check whether {run_id} is already in "
                    f"{GCP_PROJECT}.{_dataset_name()}.{name} ({exc})")
                continue

        drift = _schema_drift(client, name)
        if drift:
            warnings.extend(drift)
            continue

        try:
            if name in _UPSERT:
                loaded = _merge(client, body, name)
                log.info("upserted %d row(s) into %s", loaded, name)
            else:
                loaded = _load(client, body, tables[name])
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
