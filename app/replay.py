"""Load archived NDJSON rows into BigQuery.

Give it the files; it reads the table from each filename, upserts products and
campaigns, and appends runs and mapping unless the run is already there.

    python app/replay.py <file-or-gs-uri> ...
    python app/replay.py "gs://elc-dtest/lookfantastic/bigquery/20261112T082435Z-33b59ec9.*.ndjson"
    python app/replay.py out/lookfantastic/2026-11-12/bigquery/*.ndjson
"""

import argparse
import sys
from fnmatch import fnmatch
from glob import glob
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from retailscraper import publish as P  # noqa: E402

TABLES = (P.RUNS, P.PRODUCTS, P.CAMPAIGNS, P.MAPPING)


def _storage():
    from google.cloud import storage

    return storage.Client(project=config.GCP_PROJECT)


def _expand(arg, gcs):
    if not arg.startswith("gs://"):
        return sorted(glob(arg)) or [arg]
    bucket, _, pattern = arg[5:].partition("/")
    if not any(c in pattern for c in "*?["):
        return [arg]
    prefix = min((pattern.split(c)[0] for c in "*?["), key=len)
    names = (b.name for b in gcs().list_blobs(bucket, prefix=prefix))
    return sorted(f"gs://{bucket}/{n}" for n in names if fnmatch(n, pattern))


def _read(ref, gcs):
    if ref.startswith("gs://"):
        bucket, _, name = ref[5:].partition("/")
        return gcs().bucket(bucket).blob(name).download_as_text()
    return Path(ref).read_text(encoding="utf-8")


def _name(ref):
    return ref.replace("\\", "/").rstrip("/").rpartition("/")[2]


def _parts(ref):
    """`<run_id>.<table>.ndjson` -> (table, run_id)"""
    stem = _name(ref)
    for suffix in (".ndjson", ".jsonl", ".json"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    run_id, _, table = stem.rpartition(".")
    return table, run_id


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+",
                        help="NDJSON files: local paths or gs:// URIs, globs allowed")
    parser.add_argument("--table", help="table for every file, when the names do not say")
    parser.add_argument("--force", action="store_true",
                        help="load runs and mapping even if the run is already in them")
    args = parser.parse_args()

    if not (config.GCP_PROJECT and config.BQ_DATASET):
        print("GCP_PROJECT and BQ_DATASET must be set in .env", file=sys.stderr)
        return 2

    from google.cloud import bigquery as bq

    cached = []

    def gcs():
        if not cached:
            cached.append(_storage())
        return cached[0]

    client = bq.Client(project=config.GCP_PROJECT)
    print(f"target: {config.GCP_PROJECT}.{P._dataset_name()}")

    refs = [r for arg in args.files for r in _expand(arg, gcs)]
    if not refs:
        print("no files matched", file=sys.stderr)
        return 2

    failed = 0
    for ref in refs:
        table, run_id = _parts(ref)
        if args.table:
            table = args.table
        label = f"{_name(ref)} -> {table or '?'}"

        if table not in TABLES:
            print(f"  skip   {label}: not one of {', '.join(TABLES)} (use --table)")
            continue

        drift = P._schema_drift(client, table)
        if drift:
            print(f"  FAIL   {label}: {drift[0]}", file=sys.stderr)
            failed += 1
            continue

        if table in P._APPEND_ONLY and run_id and not args.force:
            try:
                if P._already_loaded(client, table, run_id):
                    print(f"  skip   {label}: {run_id} is already in it (--force to load anyway)")
                    continue
            except Exception as exc:
                print(f"  FAIL   {label}: {exc}", file=sys.stderr)
                failed += 1
                continue

        try:
            body = _read(ref, gcs).strip()
            if not body:
                print(f"  skip   {label}: empty file")
                continue
            if table in P._UPSERT:
                print(f"  ok     {label}: upserted {P._merge(client, body, table)} row(s)")
            else:
                print(f"  ok     {label}: loaded "
                      f"{P._load(client, body, P._table_ref(table))} row(s)")
        except Exception as exc:
            print(f"  FAIL   {label}: {exc}", file=sys.stderr)
            failed += 1

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
