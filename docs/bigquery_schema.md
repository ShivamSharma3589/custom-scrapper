# BigQuery schema — retail promotion scraper

How each scrape run is stored so that price and promotion history can be
queried over time.

---

## Principles

1. **Append only.** One row per product *per run*. Nothing is ever updated, so
   any past day can be replayed exactly as it was scraped.
2. **The manifest is the gate.** A run is loaded only when its manifest says
   `status = "ok"`. A failed or partial scrape is never loaded, so it can never
   look like a real catalogue collapse.
3. **Files stay the source of truth.** BigQuery is a copy. If a load goes
   wrong, reload from `output/`.
4. **One run loads as one unit.** All four tables receive the same `run_id`, so
   a run is either fully present or fully absent.

---

## Data flow

```
 scrape_job.py  ──►  run.py  ──►  output/<retailer>/…
   settings          one              JSON + CSV per run
                     retailer
                     at a time
                                          │
                                          ▼
                                 read manifest first
                                   status == "ok" ?
                                    │          │
                                 NO │          │ YES
                                    ▼          ▼
                              skip + alert   load, WRITE_APPEND
                                                  │
                                                  ▼
                                            BigQuery, 4 tables
                                                  │
                                                  ▼
                                      views: price history,
                                      campaign lifespan, coverage
```

### Which file feeds which table

| File | Table | Grain |
|---|---|---|
| `manifest/<ts>.json` | `runs` | 1 row per run |
| `<brand>/<ts>.json` → `products[]` | `products` | 1 row per product per run |
| `campaigns/<ts>.json` → `campaigns[]` | `campaigns` | 1 row per campaign per run |
| `<brand>/<ts>.json` → `applied_campaigns[]` | `product_campaigns` | 1 row per product-offer pair |
| `rejected/<ts>.csv` | — | not loaded |
| `brands_campaigns/<ts>.json` | — | derived by joining, not stored |

`<ts>` is the run timestamp, `YYYY-MM-DD_HH-MM-SS`, shared by every file of
one run.

---

## Tables

### `runs` — one row per scrape

The health record of a run, and the gate that decides whether the rest loads.

```sql
CREATE TABLE retail.runs (
  run_id            STRING    NOT NULL,   -- 20260923T025241Z-11c10e29
  retailer          STRING    NOT NULL,   -- "Lookfantastic"
  retailer_key      STRING    NOT NULL,   -- "lookfantastic"
  domain            STRING,
  started_at        TIMESTAMP NOT NULL,
  finished_at       TIMESTAMP,
  duration_seconds  FLOAT64,
  status            STRING,               -- ok | failed | incomplete
  reason            STRING,               -- why it failed, if it did
  products          INT64,
  expected_products INT64,                -- what the site said it lists
  campaigns         INT64,
  rejected          INT64,
  requests_total    INT64,
  requests_refused  INT64,
  brands_requested  ARRAY<STRING>,
  brands_empty      ARRAY<STRING>,
  warnings          ARRAY<STRING>
)
PARTITION BY DATE(started_at)
CLUSTER BY retailer_key;
```

`products / expected_products` is the coverage figure. Anything well below 1.0
means the scrape missed part of the catalogue.

### `products` — one row per product per run

```sql
CREATE TABLE retail.products (
  run_id            STRING    NOT NULL,
  run_started_at    TIMESTAMP NOT NULL,
  retailer          STRING    NOT NULL,
  product_id        STRING    NOT NULL,
  brand             STRING,               -- the retailer's spelling, "Estée Lauder"
  brand_matched_to  STRING,               -- the tracked brand, "Estee Lauder"
  category          STRING,
  product_title     STRING,
  product_url       STRING,
  current_price     NUMERIC,
  original_price    NUMERIC,              -- the was-price, null if not reduced
  discount_amount   NUMERIC,
  discount_percent  NUMERIC,
  price_is_from     BOOL,                 -- true when sizes differ in price
  availability      STRING,
  variant_count     INT64,
  promotional_copy  STRING,
  scraped_at        TIMESTAMP
)
PARTITION BY DATE(run_started_at)
CLUSTER BY retailer, brand_matched_to;
```

Grain: one row per `(run_id, retailer, product_id)`.

Group by `brand_matched_to`, not `brand` — retailers spell the same brand
differently ("M.A.C" and "MAC", "Estée Lauder" and "Estee Lauder").

`price_is_from` marks a low-end price for a product whose sizes are priced
differently. Exclude those rows from price comparisons.

### `campaigns` — one row per campaign per run

```sql
CREATE TABLE retail.campaigns (
  run_id         STRING    NOT NULL,
  run_started_at TIMESTAMP NOT NULL,
  retailer       STRING    NOT NULL,
  campaign_id    STRING    NOT NULL,   -- stable hash, same across runs
  promotion_text STRING,               -- "At Least 20% Off SPF"
  promotion_type STRING,               -- percentage_discount | bundle | …
  scope          STRING,               -- sitewide | brand | category | product | unresolved
  promo_code     STRING,
  landing_url    STRING,               -- the page where the offer is advertised
  source_url     STRING                -- the page it was scraped from
)
PARTITION BY DATE(run_started_at)
CLUSTER BY retailer, campaign_id;
```

`campaign_id` is a hash of retailer, text and scope, so the same offer keeps the
same id across runs. That is what makes campaign lifespan measurable.

`promotion_type` is one of: `percentage_discount`, `code_discount`,
`amount_discount`, `bundle`, `gift_with_purchase`, `spend_threshold`,
`price_match`, `sale`, `other`.

### `product_campaigns` — which offer applied to which product

```sql
CREATE TABLE retail.product_campaigns (
  run_id         STRING    NOT NULL,
  run_started_at TIMESTAMP NOT NULL,
  retailer       STRING    NOT NULL,
  product_id     STRING    NOT NULL,
  campaign_id    STRING    NOT NULL
)
PARTITION BY DATE(run_started_at)
CLUSTER BY retailer, campaign_id;
```

This replaces the `applied_campaigns` array on each product, so joins stay
simple. It is also how "which of our brands does this offer touch" is answered,
by joining through to `products.brand_matched_to`.

---

## Conventions

**Money is `NUMERIC`, never `FLOAT64`.** £22.06 must stay £22.06 when summed.

**`product_id` is unique only within a retailer.** Boots and AllBeauty both use
numeric ids. Always filter or join on `retailer` as well.

**Every table carries `run_id` and `run_started_at`.** The timestamp is
repeated on purpose so a query can prune partitions without joining `runs`
first.

**Partition by run date, cluster by retailer.** Without this, every query scans
all history.

**Re-runs on the same day are normal and expected.** They are separate
`run_id`s. For "current state", take the newest `run_id` per retailer; every
earlier run stays queryable.

---

## Loading

1. A run finishes and writes its files.
2. Read `manifest/<ts>.json`. If `status` is not `ok`, stop and alert.
3. Insert into all four tables with `WRITE_APPEND`, sharing one `run_id`.
4. Never delete the files.

---

## Example queries

**Current price of every Clinique product at each retailer**

```sql
WITH latest AS (
  SELECT retailer, MAX(run_id) AS run_id
  FROM retail.runs
  WHERE status = 'ok'
  GROUP BY retailer
)
SELECT p.retailer, p.product_title, p.current_price, p.original_price
FROM retail.products p
JOIN latest USING (retailer, run_id)
WHERE p.brand_matched_to = 'Clinique'
ORDER BY p.retailer, p.current_price;
```

**How long has each campaign been running**

```sql
SELECT retailer,
       campaign_id,
       ANY_VALUE(promotion_text)  AS promotion_text,
       MIN(run_started_at)        AS first_seen,
       MAX(run_started_at)        AS last_seen
FROM retail.campaigns
GROUP BY retailer, campaign_id
ORDER BY first_seen;
```

**Price changes for one product over time**

```sql
SELECT run_started_at, current_price, original_price, discount_percent
FROM retail.products
WHERE retailer = 'Lookfantastic'
  AND product_id = '11144733'
ORDER BY run_started_at;
```

**Which offers touch our brands right now**

```sql
WITH latest AS (
  SELECT retailer, MAX(run_id) AS run_id
  FROM retail.runs WHERE status = 'ok' GROUP BY retailer
)
SELECT c.retailer,
       c.promotion_text,
       c.promotion_type,
       ARRAY_AGG(DISTINCT p.brand_matched_to IGNORE NULLS) AS brands,
       COUNT(DISTINCT p.product_id)                        AS products
FROM retail.product_campaigns pc
JOIN latest USING (retailer, run_id)
JOIN retail.campaigns c USING (run_id, retailer, campaign_id)
JOIN retail.products  p USING (run_id, retailer, product_id)
GROUP BY c.retailer, c.campaign_id, c.promotion_text, c.promotion_type
ORDER BY products DESC;
```

**Run health, to spot a scraper going wrong**

```sql
SELECT retailer_key,
       DATE(started_at) AS day,
       status,
       products,
       expected_products,
       SAFE_DIVIDE(products, expected_products) AS coverage,
       requests_refused,
       requests_total
FROM retail.runs
ORDER BY started_at DESC
LIMIT 50;
```

---

## Deliberately left out

**Rejected records.** Useful for debugging a scraper, not for analysis.
They remain in `output/<retailer>/rejected/`.

**A cross-retailer product key.** Needed only to answer "where is this exact
product cheapest". It can be added later as a view over the stored titles and
sizes, with no change to these tables and no reload.

**Stored `first_seen` / `last_seen` columns.** Writing them back would mean
updating rows, which breaks append-only. They are computed on demand instead.
