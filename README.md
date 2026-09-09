# Retailer promotion & pricing scraper

Tracks how third-party retailers discount and promote a given set of brands,
so the business can see where its brands are being price-cut.

Give it a retailer and a list of brands; it returns the campaigns running and
the per-product pricing, as JSON and CSV.

```bash
python run.py --retailer lookfantastic --brands Clinique --max-products 15
```

## Setup

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/scrapling install
```

That last line is not optional. It downloads the browser binaries, which pip
does not — Boots, John Lewis, Amazon, ASOS and Lookfantastic all need a real
browser, and without it they fail in a way that looks like a bot block rather
than a missing dependency.

## The HTTP API

```bash
.venv/Scripts/uvicorn api:app --host 0.0.0.0 --port 8000
```

Interactive docs at `/docs`. A scrape takes between one and forty minutes, far
longer than an HTTP request survives, so `POST /scrape` returns straight away
and the work continues in the background.

```
POST /scrape        {"retailer": "boots", "brands": ["Clinique"]}   -> 202
POST /scrape        {"retailer": "boots", "campaigns_only": true}
GET  /runs/latest/boots        status of the most recent Boots run
GET  /runs/{run_id}            manifest: status, counts, refusals
GET  /runs/{run_id}/products
GET  /runs/{run_id}/campaigns
GET  /runs/{run_id}/log
GET  /retailers                the adapters and what each supports
POST /compare                  cross-retailer price comparison
```

Asking for a retailer that is already running returns **409**, not a second
crawl: two browsers on one shop get both of them throttled. The check uses the
same lock file `run.py` takes, so the API cannot drift out of step with what
is actually running.

## Usage

```bash
# What retailers are supported
# (lookfantastic, boots, johnlewis, allbeauty, asos, marksandspencer, amazon)
python run.py --list-retailers

# A quick demo run against one brand
python run.py --retailer lookfantastic --brands Clinique --max-products 15

# A full run across several brands, writing to ./output
python run.py --retailer lookfantastic \
    --brands Clinique MAC "Tom Ford" "Jo Malone" "Estee Lauder" \
    --out-dir output

# Cache responses so re-runs cost the retailer nothing.
# Strongly recommended while changing extraction rules.
python run.py --retailer lookfantastic --brands Clinique --cache-dir .cache

# Also record which category each product is filed under
python run.py --retailer lookfantastic --brands Clinique --resolve-categories
```

Brand names are matched leniently against the retailer's own naming, so
`--brands "Jo Malone"` matches Lookfantastic's `Jo Malone London`, and `MAC`
matches `MAC Cosmetics` without matching `Macadamia`. The requested name is
recorded on each record as `brand_matched_to` so results group correctly
across retailers that use different trading names.

### Every campaign on a site, with no brands

```bash
python run.py --retailer lookfantastic --campaigns-only
python run.py --retailer boots --campaigns-only
```

Answers "what is this retailer promoting right now?" rather than "what do
these brands cost here". No products are fetched.

Each campaign carries the landing page it was advertised on, and a scope read
from that link rather than guessed:

| Retailer | Campaigns found | Scopes |
|---|---|---|
| Lookfantastic | 16 | 6 brand, 3 category, 7 sitewide |
| Boots | 26 | 2 category, 4 sitewide, 20 unresolved |

Boots leaves more unresolved on purpose. It puts brands and departments in the
same URL slot -- `/no7/...` and `/fragrance/...` are the same shape -- so a
brand cannot be asserted from the path. Saying `unresolved` is honest;
guessing would invent brand campaigns.

Two things this mode does *not* cover: promotions that never render on the
site (email-only, app-only, affiliate codes), and the membership of a
"selected lines" offer, which the page usually does not state.

### Run retailers one at a time

Do not run the three retailers concurrently. Two of the three drive a real
browser, and running them together starves DNS and browser resources on one
machine: a Lookfantastic run that takes **54 minutes on its own took 30 hours**
alongside the other two, with 51 requests failing on `ERR_NAME_NOT_RESOLVED`
-- a local DNS failure, not anything the retailers did.

Sequentially:

```bash
python run.py --retailer lookfantastic --brands Clinique MAC "Tom Ford" --archive
python run.py --retailer boots        --brands Clinique MAC "Tom Ford" --archive
python run.py --retailer johnlewis    --brands Clinique MAC "Tom Ford" --archive
```

A full seven-brand pass is roughly an hour per retailer, so a nightly job
should allow three to four hours and run them in series.

### Custom offer keywords

What counts as an offer is configurable. Point the run at a keyword file and
**only those keywords are used** -- the built-in retail rules are switched
off, not extended:

```bash
python run.py --retailer boots --brands Clinique     --offer-keywords keywords/black-friday.txt
```

Two packs ship with the project:

| Pack | For |
|---|---|
| `keywords/black-friday.txt` | Black Friday / Cyber Week wording, **plus** the everyday retail wording, so a seasonal run still sees normal offers |
| `keywords/betting.txt` | A different vertical entirely -- "free spins", "no deposit", "5x wager" |

Replacing rather than extending is deliberate: you can read the file and know
exactly what the run will match. The effect is visible on Lookfantastic's
offers hub -- the built-in rules find 17 campaigns, the betting pack finds 0,
because none of its wording appears on a beauty retailer.

A file is one pattern per line (`#` for comments), or JSON:

```json
{"name": "black-friday", "keywords": ["black friday", "[0-9]+% off"]}
```

Each keyword is a regular expression, so `bet .* get` and `[0-9]+x wager`
work as written. A plain phrase gets word boundaries, so `bonus` does not
match `bonuses`. A broken pattern stops the run immediately and names the
keyword -- a typo that silently matched nothing would look identical to a
site with no promotions.

Note that `promotion_type` is still assigned by the built-in retail rules, so
a custom match that fits no known shape is typed `other`.

### Categories

Neither Lookfantastic nor Boots states a category on the product page itself.
Lookfantastic's breadcrumb is brand-shaped (`Brands / Clinique / Moisture
Surge`) and every "Skincare" elsewhere on the page is site navigation; Boots
leaves its `gtm_categoryTree` empty in served HTML. The category only exists
in the retailer's *listing* structure.

So there are two ways to get it:

```bash
# Filter to a category -- discovery goes through category listings,
# and every record is that category by construction
python run.py --retailer lookfantastic --brands Clinique --categories skincare

# Keep full sitemap coverage, and look the category up separately
python run.py --retailer lookfantastic --brands Clinique --resolve-categories
```

`--resolve-categories` visits each brand's category listings purely as a
lookup table, builds `product_id -> category`, and fills in the blanks after
the crawl. It costs one listing fetch per brand and category, which is why it
is opt-in.

A product that appears on no consulted listing keeps `category: null` rather
than being guessed at.

**Redirects are checked.** Asking for a category a brand does not stock gets
redirected -- `/c/brands/clinique/fragrance/` lands on
`/c/brands/clinique/view-all/`. Recording that would file all 46 Clinique
products as fragrance, so a listing that redirects off its category is
discarded and logged.

### Verifying results against the live site

```bash
# Check a random sample from a previous run
python spotcheck.py --retailer lookfantastic --from-output output/lookfantastic_clinique.json --sample 10

# Or check specific pages
python spotcheck.py --retailer lookfantastic --urls "https://www.lookfantastic.com/p/..."
```

Open the same URLs in a browser and compare. This is the only way to catch
extraction that is confidently wrong, which internal validation cannot do on
its own.

### Tests

```bash
python run_tests.py          # everything
python run_tests.py -q       # summary only
```

Five suites, all offline against saved fixtures, so they send no requests and
run in seconds. Exit code is non-zero if anything regressed -- run this after
every change.

The fixtures in `tests/fixtures/` are genuine captured pages covering the
three layouts that behave differently (discounted single-variant, discounted
multi-variant, and full-price). They let extraction be re-checked after any
change without sending a request.

## Running on a schedule

`run_all.py` is the entry point a cron job calls. It runs every retailer in
sequence and returns one exit code, so the schedule is a single line:

```bash
# 06:00 and 18:00 UTC
0 6,18 * * *  cd /srv/scraper && .venv/bin/python run_all.py
```

Retailers run **one at a time, never together**. Three browser-driven crawls
at once starved DNS badly enough to turn a 54-minute run into 30 hours. Each
retailer is also given a wall-clock limit, so a browser session that hangs
costs one retailer rather than blocking the sweep for ever.

### What the exit code means

| Code | Meaning | What to do |
|---|---|---|
| `0` | Every retailer produced usable data | Load it all |
| `1` | Some retailers failed | Load the rest, investigate the failures |
| `2` | Every retailer failed | Alert; load nothing |
| `3` | *(per retailer)* skipped, a previous run was still going | Nothing — the lock did its job |

A retailer **fails** when more than 30% of its requests were refused
(403/429/503), or a brand the retailer *confirmed it stocks* returned
nothing, or a `--campaigns-only` run found no campaigns at all. Before this
existed every run exited `0`, including one that had 1,501 of its 1,840
requests refused and produced five products.

Two thresholds are deliberately adjustable, because failing a *good* run is
just as expensive. `REFUSAL_LIMITS` in `run_all.py` raises the bar for a
retailer that refuses often but still delivers — John Lewis produced 770
products in a run that refused 33.8% of its requests. And only an adapter
declaring `confirms_brand_stocking` is judged on empty brands: ASOS stocks
neither Jo Malone nor Tom Ford, and failing a 775-product run over two brands
it never had would page someone for nothing.

### Where a run puts its results

```
output/john_lewis/2026/09/07/14-30-00/
    johnlewis_clinique_products.csv   one per brand
    johnlewis_campaigns.csv           one per retailer
    johnlewis_rejected.csv
    johnlewis.json
    run.log                           every request, warning and error
    manifest.json                     what happened, and whether to trust it
```

Timestamps are **UTC** and zero-padded, and both matter: British clocks move
twice a year, so in local time 01:30 happens twice each October — two runs,
one folder, the second erasing the first — and unpadded, month `10` sorts
before month `2`.

Nothing is ever overwritten, so `changes.py --latest` compares a retailer's
two most recent scheduled runs directly.

### manifest.json

One per run, and the row worth keeping in a warehouse:

```
run_id · retailer · started_at · finished_at · duration_seconds
status · reason
products · campaigns · rejected
requests_total · requests_refused
brands_requested · brands_empty · warnings · files
```

`status` is `ok`, `failed`, or `incomplete` — the last meaning the run was
killed part-way and the folder holds `products.partial.jsonl` instead of
finished output. Products are flushed there every 50 records, so a run that
dies leaves usable data rather than nothing.

The same `run_id` is stamped on **every product, campaign and rejection row**,
in the JSON and the CSVs alike. That is what lets you trace a price back to
the run that produced it, remove a bad run's rows cleanly, and avoid loading
one folder twice.

### Checking a night's runs

```bash
# Did it pass?
tail -20 output/nightly.log

# Every run's verdict
find output -name manifest.json -newermt "-14 hours" \
  -exec python -c "import json,sys; d=json.load(open(sys.argv[1])); \
  print(f\"{d['retailer']:18} {d['status']:11} {d['products']:>5}p\")" {} \;

# Do the records contradict themselves?
python audit.py
```

`audit.py` re-derives every check from the values in the output files and
shares no code with extraction, so a mistake common to the scraper and its
own tests cannot hide from it. It checks prices, currencies, discount
arithmetic, URLs, deduplication and campaign attachment across every run it
finds.

## Output

Each run writes one JSON document, one products CSV per brand, plus a
campaigns CSV and a rejections CSV:

| File | Contents |
|---|---|
| `<retailer>_<brands>.json` | The full record. This is what downstream systems consume. |
| `<retailer>_<brand>_products.csv` | Flat product table, **one file per brand** |
| `<retailer>_<brands>_campaigns.csv` | Flat campaign table |
| `<retailer>_<brands>_rejected.csv` | Records validation refused, with the reason |

Products are split per brand because a CSV is opened by a person looking at
one brand at a time. The JSON keeps every brand together, because the
campaigns are shared between them -- a site-wide banner applies to all brands
at once, and copying it into per-brand files would misrepresent it as several
separate campaigns.

Results are written to the `output` folder next to `run.py`, regardless of
which directory you run the command from. Use `--out-dir` to change that.

### A "from" price is never ranked as a real price

A John Lewis size range quotes "£33.60 - £156.00", so `current_price` is the
cheapest size and `price_is_from` is true. `compare.py` excludes those from
the cheapest/price-gap ranking and reports them under `from_price_offers`
instead -- the offer stays visible, it just is not compared. Ranking it would
have named John Lewis cheapest by £52.80 against Lookfantastic's £86.40 for
the 50ml, a gap that does not exist.

### A brand that returns nothing says so

Asking for two brands and getting six records reads as "six found", not "one
brand produced nothing". The run summary breaks the count down per brand and
prints a note for any that returned zero, because an unstocked brand and a
brand with no promotions otherwise look identical. Boots does not stock Tom
Ford, and now says so.

### Change detection refuses incomparable runs

`changes.py` compares two runs of the same retailer AND the same brands, in
chronological order. Diffing different brand sets would report every product
in the first as removed and every one in the second as added; reversing the
order reports every price cut as a rise. Both raise rather than producing
plausible-looking nonsense.

### Products and campaigns are separate

Campaigns are **not** nested inside products, and products are not nested
inside campaigns. They are two collections joined by id:

```
campaigns[].campaign_id  <--  products[].applied_campaigns[]
```

This is deliberate. One product can be hit by several offers at once (a
site-wide code plus its own markdown), some products are discounted with no
campaign behind them, and some campaigns have a reach that cannot be
established. Nesting either duplicates products across campaigns or silently
drops the ones no campaign covers. A report can still present the data as
"campaign, and the products under it" by following the join.

Each campaign carries an honest `scope`:

| scope | meaning |
|---|---|
| `sitewide` | Shown across the whole site (the header strip banner) |
| `brand` | Tied to one brand |
| `product` | The offer on one product's own page |
| `unresolved` | The offer is real but its reach can't be proved from the page |

A site-wide banner appearing on a Clinique page is `sitewide`, never a
Clinique campaign. Getting this wrong is the main way this kind of scraper
produces confident nonsense.

### Rejections are output, not hidden

A record that fails validation is written to the output with a
machine-readable reason, never silently dropped. A rejection usually means
the page changed or extraction drifted, so hiding it just makes the dataset
look healthier than it is.

| reason | meaning |
|---|---|
| `missing_price` | No current price could be read |
| `invalid_discount` | "Was" price below the current price -- impossible pair |
| `discount_mismatch` | Advertised % disagrees with the two prices |
| `unverified_brand` | Brand could not be proved from the retailer's own data |
| `weak_brand_evidence` | `--strict-brand` only: brand rests on corroboration |
| `brand_mismatch` | Verified brand is not one we were asked about |
| `variant_conflict` | SKU belongs to a different product than the URL |

## Architecture

```
run.py                     CLI: scrape one retailer
compare.py                 cross-retailer price comparison
changes.py                 what changed between two runs
spotcheck.py               live verification tool
run_tests.py               run every test suite
retailscraper/
  models.py                Product, Campaign, RejectedRecord
  normalize.py             price parsing, URL canonicalisation, text cleanup
  promotions.py            offer detection and classification (shared)
  validation.py            the validation rules and brand matching
  output.py                JSON + CSV writers
  matching.py              cross-retailer product matching
  history.py               change detection between runs
  spider.py                the generic crawler (no retailer knowledge)
  adapters/
    base.py                the adapter interface + registry
    lookfantastic.py       Lookfantastic selectors and rules
    boots.py               Boots selectors and rules
```

Everything except `adapters/` is retailer-agnostic. Crawling, retries,
throttling, robots.txt compliance and URL-level deduplication come from
Scrapling's `SitemapSpider` and are not reimplemented here.

### Adding a retailer

Subclass `RetailerAdapter`, decorate it with `@register`, and implement
`is_product_url`, `extract_product` and `extract_campaigns`, plus one
discovery route:

| The retailer has... | Implement |
|---|---|
| a reachable sitemap | `sitemap_urls` + `select_candidates` |
| no usable sitemap | `product_listing_urls` + `extract_product_links` |

Override `configure_session` if the site needs more than plain HTTP. Nothing
outside your new file should need to change -- adding Boots required no
change to validation, deduplication, output or the data model.

Before writing any selector, inspect the retailer's actual HTML. Do not
assume a structure. Check specifically for `schema.org` JSON-LD -- where it
exists, most of the extraction is already done for you and is far more stable
than CSS selectors.

## Notes on Lookfantastic

Findings from inspecting the live site, recorded because they drove the
design:

- **Discovery uses the sitemap, not the brand pages.** The brand landing
  pages render their grid with JavaScript. A static fetch of
  `/c/brands/clinique/` returns only a handful of carousel items mixed with
  beauty boxes and gift vouchers -- 18 URLs, of which several are not
  Clinique. The sitemap returns all 234 Clinique products in one request.
- **Brand is verified from the breadcrumb**, which links to
  `/c/brands/<brand>/` -- the retailer's own taxonomy. The URL slug and the
  page title are not treated as proof.
- **Variants change the JSON-LD shape.** A single product is a `Product` with
  `sku` and `offers`; a shade range is a `ProductGroup` with `hasVariant` and
  a null `offers`. For a range we report the group id and leave `sku` empty,
  because quoting one shade's SKU would misrepresent a 33-shade foundation as
  a single item.
- **Prices come from `#product-price`**, keyed on the screen-reader labels
  ("Recommended Retail Price:", "Current price:") rather than the utility CSS
  classes around them. Three layouts exist and all are handled: both labels,
  current-price label only, and a bare unlabelled price.
- **A product page carries seven `[data-track="promoClick"]` elements**, six
  of which belong to recommended products in the "you may also like" rails.
  The product's own offer is read from `[data-e2e="pdp-pap-banner"]`, which
  appears exactly once. Using the looser selector attaches other products'
  offers to this one.

## Politeness

`robots.txt` is obeyed, AutoThrottle is on, and concurrency is capped at 2
with a 1s floor between requests. A run takes roughly 3 seconds per product,
so a full multi-brand pass is an overnight job rather than something to demo
live. Use `--max-products` for demos and `--cache-dir` while developing.


## Retailers at a glance

| Retailer | Fetch | Discovery | Brands stocked | Notes |
|---|---|---|---|---|
| **AllBeauty** | HTTP | Shopify JSON API | 6 of 7 | 475 products in 18s. Richest source: brand, category, was-price all stated |
| **Lookfantastic** | browser | sitemap | 7 of 7 | Full catalogue, ~1,050 products |
| **Boots** | browser | brand listings | Clinique + 3 | Imperva bot wall; needs a warmed session |
| **John Lewis** | browser | brand pages | 6 of 7 | Gzipped sitemaps; brand codes resolved and cached |
| **ASOS** | browser | search | 5 of 7 | No Tom Ford or Jo Malone. Search is not a brand filter |
| **M&S** | HTTP | beauty sitemap | Clinique only | Search disallowed by robots; 19 products |
| **Amazon** | browser | search | — | **Refuses to publish**: prices come back in the local currency without a UK IP |

Two retailers were investigated and rejected:

- **Next** does not stock any of these brands. Its brand sitemap lists 1,467
  brands and none of the seven appear, so there is no adapter.
- **Google search ads** are disallowed by Google's own robots.txt
  (`Disallow: /search`). The legitimate route is a paid SERP API.

## Notes on Amazon

The crawl works; the data does not, from outside the UK.

Amazon prices by the visitor's location rather than by domain, so
amazon.co.uk returns local pricing -- every price came back as `INR 4,239.84`
during development. Setting `locale`, `Accept-Language`, the `i18n-prefs=GBP`
cookie and `?language=en_GB` does not change it: Amazon geolocates by IP.

Rather than publish plausible-looking wrong numbers, the adapter **refuses
any record not priced in GBP** and reports how many it refused. Add a UK
proxy to `AmazonAdapter.configure_session` and it produces correct data with
no other change.

Two deliberate limits: it reads the buy-box price only, not the other-sellers
panel; and its brand evidence (`amazon_brand_line`) is a rendered attribute
rather than structured data, so `--strict-brand` rejects it.

## Notes on John Lewis

The third retailer, and the one that most justifies the adapter pattern: it
shares no discovery mechanism with either of the others.

- **Plain HTTP hangs.** A request does not get refused, it never returns --
  worse than a block, because it looks like a slow network. Everything goes
  through a browser session.
- **The sitemap is real but gzipped**, and a browser asked to navigate to a
  `.gz` starts a download instead of loading a page, so `SitemapSpider` cannot
  read it. Discovery uses brand landing pages instead, which robots.txt
  explicitly allows (`Allow: /brand/*/_/N-*`).
- **Brand pages need a code** John Lewis assigns (`/brand/clinique/_/N-1z13ywm`).
  Those codes only exist in the gzipped grid sitemaps, so the adapter fetches
  them via `fetch()` from inside a loaded page -- not a navigation, so no
  download starts -- and caches the result in `.jl_brand_codes.json`. A guessed
  code returns 404, so guessing is not an option.
- **The JSON-LD is the richest of the three.** `brand` is an explicit Brand
  object and `category` is a real taxonomy path, so John Lewis needs neither
  breadcrumb inference nor `--resolve-categories`.
- **Two identifier schemes.** The page id (`p47865`) and the stock code
  (`230631076`) are different by design, so the variant-conflict check is
  disabled here via `Product.sku_matches_product_id`. Leaving it on would
  reject every John Lewis record.
- **Price ranges.** A fragrance sold in several sizes quotes
  "£33.60 - £156.00" and has no single price. Those records carry
  `price_is_from = true`, because comparing a from-price against another
  retailer's single-size price would understate that retailer's discount.
  John Lewis states the price in an accessible label with four shapes, all
  handled: single price, single discount ("was X, now Y"), a plain range, and
  a *discounted* range ("was £220 - £310, now £187 - £263.50"). The last was
  missed at first, which reported reduced multi-size products as full price.

## Notes on Boots

Boots was built second, deliberately, to test whether the framework was
genuinely generic or just shaped around Lookfantastic. It differs in all
three of the ways that matter, and none of them required changing the core:

- **It needs a real browser.** Boots is behind Imperva bot protection. Plain
  HTTP with convincing headers still gets a "Pardon Our Interruption"
  challenge, served as **HTTP 200** -- which is worse than a 403, because it
  looks like success. The adapter registers a stealth browser session and
  exposes `looks_blocked()` so a challenge page is never parsed as a product.
  The session is kept alive across the crawl: the first navigation earns the
  cookie the rest depend on, so fetching each page in a fresh browser gets
  every one of them challenged.
- **Its sitemap is unreachable.** The URL in `robots.txt` returns the same
  challenge even through the browser. Discovery uses the paginated brand
  listings at `/<brand>/<brand>-full-range?paging.index=N` instead, which do
  work and yield ~46 products per page.
- **It publishes microdata, not JSON-LD.** There is no `application/ld+json`
  anywhere on a Boots product page. The same fields are present as schema.org
  microdata (`itemprop="name"`, `itemprop="Brand"`, `itemprop="price"`), which
  is equally authoritative. Brand is taken from `itemprop="Brand"`.
- Boots also exposes its **internal buyer codes** for promotions
  (`RX UK PB Clinique only 25pnd BLOCKBUSTER`). Those are ignored: they are
  warehouse jargon, not something to put in front of a brand team, and they
  are not reliably about the product whose page they appear on. The
  customer-facing copy in `li.pdp-promotion-redesign` is used instead.

Because Boots drives a browser, it runs at roughly 15 seconds per page rather
than Lookfantastic's 3. Keep `--max-products` small for Boots.


## Brand evidence

Every product records **how** its brand was established, in
`brand_verified_by`:

| value | meaning | strength |
|---|---|---|
| `breadcrumb_link` | Lookfantastic's breadcrumb links to `/c/brands/<brand>/` | structured |
| `microdata_brand` | Boots' `itemprop="Brand"` | structured |
| `title_and_url` | title starts with a requested brand **and** the URL slug contains it | corroboration |
| `unverified` | neither -- the record is rejected | none |

`title_and_url` exists because some Lookfantastic product pages ship a
truncated breadcrumb (`Home > product`) with no brand link at all, and carry
no other machine-readable brand: every `/c/brands/` link on them is global
navigation. Rejecting those loses genuine products.

It is deliberately narrow. It only tests brands already requested, so it can
never invent one; it needs the retailer to have described the product the same
way twice, independently; and the title must *start* with the brand rather
than merely mention it. A product whose own name matches its slug ("The Summer
Edit") is not in the requested list and cannot match.

Pass `--strict-brand` to reject anything short of structured data. On a
Clinique skincare sample that trades 5 records for 1 -- fewer records, higher
confidence. The field is in the output either way, so downstream can make its
own call.

## Status

| | |
|---|---|
| Lookfantastic | working, verified against live pages |
| Boots | working, verified against live pages |
| John Lewis | **blocked** -- see below |
| Amazon | **not viable as a simple adapter** -- see below |

**John Lewis** resists every route available: plain HTTP times out at network
level, the site navigation is client-side so brand URLs cannot be discovered
from the HTML, and its sitemap shards are gzipped, which a browser downloads
rather than renders. Getting in needs a different technique (an authenticated
session, or fetching the gzipped sitemaps through the browser's own network
layer) -- not more selectors.

**Amazon** is reachable and its `robots.txt` permits `/s?k=` and `/dp/<ASIN>`,
but it is not a config change:

- Prices came back in **INR** on `amazon.co.uk` -- the session is geo-located,
  so locale has to be forced before any number can be trusted as UK pricing.
- Listings have **multiple sellers** at different prices; the one-price-per-
  product model does not describe that.
- Brand cannot be verified the way it can elsewhere: marketplace listings are
  seller-authored, so a "Clinique" listing is not evidence of a Clinique
  product.

Shipping it half-built would produce confident wrong data, which is the exact
failure this project is designed to prevent.
