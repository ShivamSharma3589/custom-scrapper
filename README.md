# Retail Promotion Scraper

Tracks the promotional campaigns a retailer is running and the products those
campaigns apply to, for a fixed set of brands. One run produces JSON and CSV on
disk and, when the cloud settings are filled in, uploads them to Cloud Storage
and loads them into BigQuery.

Built on [Scrapling](https://github.com/D4Vinci/Scrapling) 0.4.15. Python 3.13.

## Install

```bash
python -m venv .venv
.venv/Scripts/activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
scrapling install               # downloads Chromium; Boots and John Lewis need it
```

## Configure

Copy `.env.example` to `.env` and fill it in.

| Variable | Purpose |
|---|---|
| `WEBSHARE_USERNAME` / `WEBSHARE_PASSWORD` | Webshare proxy credentials |
| `WEBSHARE_PROXIES` | `host:port` of each proxy, comma-separated |
| `ALLOW_DIRECT` | `true` scrapes from this machine's own IP instead of a proxy |
| `GCP_PROJECT`, `GCS_BUCKET`, `BQ_DATASET` | Fill all three to enable publishing; leave empty and nothing is uploaded |
| `GOOGLE_APPLICATION_CREDENTIALS` | Service-account key with Storage + BigQuery access |
| `DELETE_LOCAL_AFTER_UPLOAD` | Delete a local file once the bucket confirms a copy (default `true`) |

Every request goes through a proxy unless `ALLOW_DIRECT=true`; with neither set
the run refuses to start.

## Run

One retailer, campaigns and the products on their offer pages:

```bash
python run.py --retailer lookfantastic --campaigns-only --ignore-robots --brands Clinique MAC "Tom Ford" "Jo Malone" "Estee Lauder" "Bobbi Brown" "Too Faced"
```

`python run.py --help` lists every flag. The ones that matter:

| Flag | Effect |
|---|---|
| `--retailer` | adapter name or domain; `--list-retailers` shows them |
| `--brands` | brand names to track |
| `--campaigns-only` | every campaign on the site plus the products on its offer pages. Minutes. Omit for the full product crawl — hours |
| `--ignore-robots` | crawl paths robots.txt disallows (needed for brand-filtered listings) |
| `--formats` | `json`, `csv`, or both (default) |
| `--cache-dir` | reuse responses between runs; use while changing extraction rules |
| `--refusal-limit` | share of 403/429/503 above which the run is called failed (default 0.30) |

`scrape_job.py` is the scheduled entry point: it holds the retailer list, the
brand list and the flags, then shells out to `run.py` once per retailer and
appends a line to `output/scrape_job.log`. This is what the container runs.

Exit codes: `0` success, `1` the run finished but not cleanly, `2` bad
arguments or configuration, `3` skipped because another run holds the lock.

## Output

```
output/<retailer>/
  campaigns/<run_id>.json|csv              every campaign found on the site
  filtered_campaigns/
    campaigns/<run_id>.json|csv            only campaigns proven to carry our brands
    products/<run_id>.json|csv             those campaigns' products, with campaign_ids
  <brand>/<run_id>.json|csv                per-brand products (full crawl only)
  rejected/<run_id>.csv                    records dropped, with the reason
  manifest/<run_id>.json                   status, counts, warnings, files written
  logs/<run_id>.log
```

`run_id` is `<UTC timestamp>-<8 hex>`, so files sort chronologically and never
collide across machines. The manifest is the machine-readable verdict for a
run — check `status`, `campaigns`, `products` and `requests_refused` there
rather than parsing the log.

## Publishing

With `GCP_PROJECT`, `GCS_BUCKET` and `BQ_DATASET` set, a finished run uploads
every file it wrote to the bucket under the same path it has on disk, then loads
four tables. The tables must already exist; the code never creates them.
Publishing never raises — a cloud failure is reported as a warning and the files
stay on disk.

| Table | Write mode | Key |
|---|---|---|
| `runs` | append | — |
| `products` | upsert (MERGE) | `retailer` + `product_id` |
| `campaigns` | upsert (MERGE), brand list overwritten | `retailer` + `campaign_id` |
| `campaigns_products_mapping` | append | — |

A campaign's brands are stored as one comma-separated string in
`campaigns.brand`. A campaign with no products still gets a mapping row, with a
null `product_id`. Campaigns that disappear from the site are never deleted.
Local files are removed only after the bucket confirms the upload.

## Layout

```
run.py                      CLI: one retailer, one run
scrape_job.py               scheduled entry point, loops over retailers
config.py                   .env settings
retailscraper/
  spider.py                 the generic crawler
  models.py                 Product, Campaign, RejectedRecord
  output.py                 JSON/CSV serialisation and the filtered set
  publish.py                Cloud Storage upload and BigQuery load
  runs.py                   run ids, paths, locking, manifests
  validation.py             brand and price checks
  promotions.py             classifying promotional copy
  normalize.py, matching.py, keywords.py
  adapters/
    base.py                 the contract every adapter implements
    lookfantastic.py  johnlewis.py  boots.py
    allbeauty.py  asos.py  marksandspencer.py  amazon.py  next.py
```

`lookfantastic`, `johnlewis` and `boots` are the three in active use.

## Adding a retailer

Subclass `RetailerAdapter` in a new module under `retailscraper/adapters/`,
`register()` it, and import it from `adapters/__init__.py`. `base.py` defines
what to override; the defaults handle the common case.

Anything retailer-specific — URL shapes, facet syntax, how a campaign is
stated, which departments to skip — belongs in that adapter, never in
`spider.py` or `output.py`. The flags on `base.py` (`campaigns_stated_on_product`,
`keeps_brand_named_campaigns`, `obeys_robots`) exist so shared code can stay
retailer-agnostic.

## Docker

```bash
docker build -t retail-scraper .
docker run --rm --env-file .env --shm-size 1g -v "$PWD/output:/app/output" retail-scraper
```

The image installs Chromium at build time and runs `scrape_job.py`. A browser
needs more than Docker's default 64 MB of shared memory, hence `--shm-size`.
