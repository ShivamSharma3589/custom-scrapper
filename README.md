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
pip install -r app/requirements.txt
scrapling install               # downloads Chromium; Boots and John Lewis need it
```

## Configure

Copy `.env.example` to `.env` at the repository root and fill it in.

| Variable | Purpose |
|---|---|
| `RETAILERS` | **Required.** Which retailers this instance scrapes, comma-separated: `RETAILERS=johnlewis,boots`. One per instance in production; several run one after another |
| `OUTPUT_FORMATS` | `csv`, `json`, or both comma-separated. Default `csv` |
| `WEBSHARE_USERNAME` / `WEBSHARE_PASSWORD` | Webshare proxy credentials |
| `WEBSHARE_COUNTRY` | two-letter country for the exit IPs, e.g. `gb`. Blank means anywhere |
| `WEBSHARE_SESSIONS` | how many exit IPs to hold at once (default 3) |
| `WEBSHARE_ENDPOINT` | Webshare's backbone host (default `p.webshare.io:80`) |
| `WEBSHARE_PROXIES` | `host:port` of each proxy, comma-separated |
| `ALLOW_DIRECT` | `true` scrapes from this machine's own IP instead of a proxy |
| `GCP_PROJECT`, `GCS_BUCKET`, `BQ_DATASET` | Fill all three to enable publishing; leave empty and nothing is uploaded |
| `GOOGLE_APPLICATION_CREDENTIALS` | Service-account key with Storage + BigQuery access |
| `DELETE_LOCAL_AFTER_UPLOAD` | Delete a local file once the bucket confirms a copy (default `true`) |

Every request goes through a proxy unless `ALLOW_DIRECT=true`; with neither set
the run refuses to start.

### How the proxies are built

Webshare's backbone endpoint reads the exit IP out of the **username**:
`<user>-<country>-<n>` pins session `n` to one IP. So `WEBSHARE_SESSIONS=3`
with `WEBSHARE_COUNTRY=gb` produces three different UK exit IPs from one
endpoint:

```
http://<user>-gb-1:<pass>@p.webshare.io:80
http://<user>-gb-2:<pass>@p.webshare.io:80
http://<user>-gb-3:<pass>@p.webshare.io:80
```

The crawl uses the first; when a site starts refusing it, the spider moves to
the second, then the third. Rotating residential has no IP list to configure —
this is the only way to use it. Pin the country: Webshare's own docs say
choosing one in their web UI does nothing, the username is what sets it.

`WEBSHARE_PROXIES` is the older style, an explicit `host:port` list. If it is
filled in it **overrides** the endpoint above; leave it blank for rotating
residential.

## Run

One retailer, campaigns and the products on their offer pages:

```bash
python app/run.py --retailer lookfantastic --campaigns-only --ignore-robots --brands Clinique MAC "Tom Ford" "Jo Malone" "Estee Lauder" "Bobbi Brown" "Too Faced"
```

`python app/run.py --help` lists every flag. The ones that matter:

| Flag | Effect |
|---|---|
| `--retailer` | adapter name or domain; `--list-retailers` shows them |
| `--brands` | brand names to track |
| `--campaigns-only` | every campaign on the site plus the products on its offer pages. Minutes. Omit for the full product crawl — hours |
| `--ignore-robots` | crawl paths robots.txt disallows (needed for brand-filtered listings) |
| `--formats` | `json`, `csv`, or both (default) |
| `--cache-dir` | reuse responses between runs; use while changing extraction rules |
| `--refusal-limit` | share of 403/429/503 above which the run is called failed (default 0.30) |

`app/scrape_job.py` is the scheduled entry point: it takes the retailer list
from `RETAILERS`, holds the brand list and the flags, then shells out to
`run.py` once per retailer and appends a line to `app/output/scrape_job.log`.
This is what the container runs, so three instances of one image each scrape a
different retailer by setting `RETAILERS` differently. It refuses to start if
`RETAILERS` is missing, blank or names a retailer that has no adapter, rather
than scraping the wrong thing or nothing.

Exit codes: `0` success, `1` the run finished but not cleanly, `2` bad
arguments or configuration, `3` skipped because another run holds the lock.

## Output

```
app/output/<retailer>/
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

The run log is uploaded too, including when a run crashes — on Cloud Run the
local disk is RAM and disappears with the container, so the bucket is the only
place a post-mortem can read it from. The log handler is closed before the
upload, so the file is complete.

Cloud Storage and BigQuery are independent: BigQuery is loaded from memory, not
from a staged file in the bucket, so either can fail without taking the other
down. The rows are still written to the bucket as `bigquery/<run_id>.<table>.ndjson`,
but only as an archive to replay a failed load from.

Every upload and every BigQuery load is tried up to three times, waiting 2s then
4s. This sits on top of the client libraries' own backoff, which does not cover
`load_table_from_file` at all and never retries a 403.

## Infrastructure

The Google Cloud resources are Terraform, under `terraform/`, one folder per
environment (`terraform/dev/`). `tables.tf` is where the four BigQuery tables
come from — which is why the scraper only loads into them and never creates
them. A schema change belongs there, applied before the next run, not in
`publish.py`. The project, bucket and dataset it creates must be the ones `.env`
points at. `terraform/dev/readme.md` covers running it.

## Layout

```
README.md  Dockerfile  .dockerignore      repository root
.env.example  .env
terraform/dev/                            the BigQuery tables and the rest of
                                          the Google Cloud resources
app/
  run.py                    CLI: one retailer, one run
  scrape_job.py             scheduled entry point, loops over retailers
  config.py                 .env settings
  requirements.txt          Python dependencies
  output/                   where runs are written
  retailscraper/
    spider.py               the generic crawler
    models.py               Product, Campaign, RejectedRecord
    output.py               JSON/CSV serialisation and the filtered set
    publish.py              Cloud Storage upload and BigQuery load
    runs.py                 run ids, paths, locking, manifests
    validation.py           brand and price checks
    promotions.py           classifying promotional copy
    normalize.py, matching.py, keywords.py
    adapters/
      base.py               the contract every adapter implements
      lookfantastic.py  johnlewis.py  boots.py
      allbeauty.py  asos.py  marksandspencer.py  amazon.py  next.py
```

`lookfantastic`, `johnlewis` and `boots` are the three in active use.

Everything the scraper needs lives in `app/`, which becomes `/app` in the image,
so container paths match the repository. The `Dockerfile` stays at the root, as
does `.env`, which `config.py` reads one level up from itself.

The Dockerfile copies `app/requirements.txt` on its own before copying the rest
of `app/`. That is deliberate: `pip install` is the slowest layer, and giving it
its own layer means editing a `.py` file reuses the cached install instead of
reinstalling every dependency on every build.

## Adding a retailer

Subclass `RetailerAdapter` in a new module under `app/retailscraper/adapters/`,
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
docker run --rm --env-file .env --shm-size 1g -v "$PWD/app/output:/app/output" retail-scraper
```

The image copies `app/` to `/app`, installs Chromium at build time and runs
`scrape_job.py`. A browser needs more than Docker's default 64 MB of shared
memory, hence `--shm-size`. The `.env` file is never copied into the image —
pass the environment in at run time.
