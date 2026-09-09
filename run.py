"""Command-line runner.

Examples
--------
Discover what the scraper can target: `python run.py --list-retailers`

A small demo run against one brand: `python run.py --retailer lookfantastic --brands Clinique --max-products 15`

A full nightly run across several brands:

    python run.py --retailer lookfantastic \
        --brands Clinique MAC "Tom Ford" "Jo Malone" "Estee Lauder" \
        --out-dir output
"""

from __future__ import annotations

import argparse
import logging
import sys

from pathlib import Path
from typing import List

# Let this file be imported, not just run: an importer's working directory is
# not this folder, so `retailscraper` would not be found.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from retailscraper.adapters.base import available_adapters, get_adapter
from retailscraper.keywords import KeywordFileError, describe, load_keywords
from retailscraper.validation import (
    KNOWN_BRANDS,
    canonical_brand,
    suggest_brand,
)
from retailscraper.promotions import set_offer_keywords
from retailscraper.output import build_payload, write_all
from retailscraper.spider import RetailPromotionSpider
from retailscraper.runs import (
    DEFAULT_REFUSAL_LIMIT,
    STATUS_INCOMPLETE,
    STATUS_OK,
    build_manifest,
    capture_logger,
    close_run_log,
    new_run_id,
    PartialWriter,
    RunLockBusy,
    RunPaths,
    open_run_log,
    refusal_rate,
    run_lock,
    utc_now,
    verdict,
    write_manifest,
)


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Track competitor retailer promotions and pricing for a set of brands.",
    )
    parser.add_argument(
        "--retailer",
        help="Retailer to scrape: an adapter name ('lookfantastic') or a domain.",
    )
    parser.add_argument(
        "--brands",
        nargs="+",
        default=[],
        help="Brand names to track, e.g. --brands Clinique MAC 'Tom Ford'",
    )
    parser.add_argument(
        "--categories",
        nargs="+",
        default=[],
        help="Limit to these categories, e.g. --categories skincare fragrance. "
             "Only honoured by retailers that expose category listings; the "
             "run warns and stops if the chosen retailer does not.",
    )
    parser.add_argument(
        "--max-products",
        type=int,
        default=None,
        help="Stop after this many product pages PER BRAND. Use for quick demo "
             "runs; omit for a full pass.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Directory for the JSON and CSV results "
             "(default: the 'output' folder next to this script).",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Cache responses here so re-runs cost no extra requests. "
             "Strongly recommended while iterating on extraction rules.",
    )
    parser.add_argument(
        "--crawl-dir",
        type=Path,
        default=None,
        help="Enable checkpointing so an interrupted run can resume.",
    )
    parser.add_argument(
        "--strict-brand",
        action="store_true",
        help="Only accept products whose brand comes from the retailer's "
             "structured data. Rejects records verified by title+URL "
             "corroboration. Fewer records, higher confidence.",
    )
    parser.add_argument(
        "--resolve-categories",
        action="store_true",
        help="Also record which category each product is filed under. Costs "
             "one extra listing fetch per brand and category, because neither "
             "retailer states a category on the product page itself.",
    )
    parser.add_argument(
        "--offer-keywords",
        type=Path,
        default=None,
        help="A keyword file defining what counts as an offer. REPLACES the "
             "built-in retail rules rather than adding to them, so only your "
             "keywords are matched. See keywords/black-friday.txt.",
    )
    parser.add_argument(
        "--campaigns-only",
        action="store_true",
        help="Scrape every campaign running on the site and no products. "
             "Needs only --retailer; --brands is not required.",
    )
    parser.add_argument(
        "--list-retailers",
        action="store_true",
        help="List the available retailer adapters and exit.",
    )
    parser.add_argument(
        "--refusal-limit",
        type=float,
        default=DEFAULT_REFUSAL_LIMIT,
        metavar="SHARE",
        help="Share of refused requests (403/404/429/503) above which the run "
             f"is reported as failed. Default {DEFAULT_REFUSAL_LIMIT:.2f}. "
             "Raise it for a retailer that refuses often but still delivers.",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.list_retailers:
        for name in available_adapters():
            print(name)
        return 0

    if not args.retailer:
        print("error: --retailer is required", file=sys.stderr)
        return 2

    if not args.brands and not args.campaigns_only:
        print("error: --brands is required unless you pass --campaigns-only",
              file=sys.stderr)
        return 2

    adapter = get_adapter(args.retailer)

    # fix misspellings in what the user typed, e.g. "Bobbie Brown" -> "Bobbi Brown"
    corrected = []
    for raw_brand in args.brands:
        fixed = canonical_brand(raw_brand)
        if fixed != raw_brand:
            print(f"note: reading {raw_brand!r} as {fixed!r}")
        corrected.append(fixed)
    args.brands = corrected

    # If no offer pages are available in the adapter.campaign_discovery_urls() list, 
    # then --campaigns-only would find nothing. Refuse rather than silently ignore.
    if args.campaigns_only and not adapter.campaign_discovery_urls():
        print(
            f"error: no campaign hub pages are known for {adapter.display_name}, "
            f"so --campaigns-only would find nothing. Add "
            f"CAMPAIGN_HUB_PATHS to its adapter first.",
            file=sys.stderr,
        )
        return 2

    # Selects folder to store the output and by default stores the output next to where the script lives
    out_dir = args.out_dir or (Path(__file__).resolve().parent / "output")

    if args.categories and not adapter.supports_categories:
        print(
            f"error: {adapter.display_name} does not expose category listings, "
            f"so --categories cannot be honoured. Re-run without it.",
            file=sys.stderr,
        )
        return 2

    # Pass your own keyword file to override the built-in rules
    keyword_set = None
    if args.offer_keywords:
        try:
            keyword_set = load_keywords(args.offer_keywords)
        except KeywordFileError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        set_offer_keywords(keyword_set)

    # Logs the details of the run
    print(f"Retailer : {adapter.display_name} ({adapter.domain})")
    print(f"Offers   : {describe(keyword_set)}")
    if args.campaigns_only:
        print("Mode     : campaigns only (no product pages)")
        for url in adapter.campaign_discovery_urls():
            print(f"           {url}")
    else:
        print(f"Brands   : {', '.join(args.brands)}")
        print(f"Categories: {', '.join(args.categories) if args.categories else 'all'}")
        print(f"Limit    : {args.max_products or 'no limit'}")
    print("Starting crawl. This is deliberately slow so we stay within the "
          "retailer's rate limits.\n")

    # Every file this run writes is stamped with the same timestamp, so a
    # scheduled run never overwrites the one before it.
    started_at = utc_now()
    run_id = new_run_id(started_at)
    paths = RunPaths(out_dir, adapter, started_at)

    # skip if this retailer is already running
    lock = None
    try:
        lock = run_lock(out_dir, adapter)
        lock.__enter__()
    except RunLockBusy as exc:
        print(f"skipped: {exc}", file=sys.stderr)
        return 3

    # always release the lock and close the log, even if the crawl crashes
    log_handler = None
    partial_writer = None
    prepare_warnings: List[str] = []
    try:
        log_handler = open_run_log(paths.file("logs", ".log"))
        logging.getLogger(__name__).info(
            "run %s starting: %s, brands=%s", run_id, adapter.display_name,
            ", ".join(args.brands) or "(campaigns only)",
        )

        # resolve brands before crawling; warnings name brands the shop
        # does not stock, which the verdict below needs
        for warning in adapter.prepare(args.brands):
            prepare_warnings.append(warning)
            logging.getLogger(__name__).warning(warning)
            print(f"warning: {warning}", file=sys.stderr)

        # Save as the crawl goes, so a run that is killed leaves usable data.
        partial_writer = PartialWriter(paths.file("partial", ".jsonl"))

        spider = RetailPromotionSpider(
            adapter=adapter,
            brands=args.brands,
            categories=args.categories,
            strict_brand=args.strict_brand,
            resolve_categories=args.resolve_categories,
            campaigns_only=args.campaigns_only,
            max_products=args.max_products,
            crawldir=str(args.crawl_dir) if args.crawl_dir else None,
            cache_dir=str(args.cache_dir) if args.cache_dir else None,
            on_product=((lambda product: partial_writer.add(product.to_dict()))
                        if partial_writer else None),
        )

        # attach the log file to the crawler's logger too -- it doesn't pass
        # messages up, so run.log would miss everything the crawl did
        capture_logger(log_handler, getattr(spider, "logger", None))

        result = spider.start()

        # both need the whole crawl first: a campaign found on the last page
        # still applies to the first product
        with_campaigns = spider.apply_campaigns()
        if with_campaigns:
            print(f"campaigns attached to {with_campaigns} product(s)")

        if args.resolve_categories:
            filled = spider.apply_category_index()
            print(f"\ncategory index: {len(spider.category_index)} product(s) mapped, "
                  f"{filled} record(s) filled in")

        # tolerate a future Scrapling release changing CrawlStats
        try:
            stats = dict(vars(result.stats))
        except TypeError:
            stats = {}

        payload = build_payload(
            retailer=adapter.display_name,
            domain=f"https://{adapter.domain}",
            brands=args.brands,
            products=spider.products.values(),
            campaigns=spider.campaigns.values(),
            rejected=spider.rejected,
            stats=stats,
            run_id=run_id,
        )

        if partial_writer:
            partial_writer.flush()

        written = write_all(payload, paths)

        # a partial file left behind is the signal that a run never finished
        if partial_writer:
            partial_writer.discard()

        run_stats = payload["run_stats"]
        print("\n--- run summary ---")

        # count per brand -- a total alone hides a brand that returned zero
        if args.brands:
            found = {b: 0 for b in args.brands}
            for product in spider.products.values():
                key = product.brand_matched_to or product.brand
                if key in found:
                    found[key] += 1
            for brand, count in found.items():
                marker = "  " if count else "  <- nothing found"
                print(f"  {brand:22} {count:>4} product(s){marker}")
            empty = [b for b, c in found.items() if not c]
            if empty:
                # every brand the crawl saw, rejections included
                seen_brands = {p.brand for p in spider.products.values() if p.brand}
                seen_brands |= {
                    (r.payload or {}).get("brand")
                    for r in spider.rejected
                    if (r.payload or {}).get("brand")
                }
                # also check known brands: a misspelling returns nothing, so the
                # crawl never reveals the right spelling on its own
                candidates = sorted(seen_brands | set(KNOWN_BRANDS))
                for brand in empty:
                    did_you_mean = suggest_brand(brand, candidates)
                    if did_you_mean:
                        print(f"\n  DID YOU MEAN: {brand!r} found nothing, but "
                              f"{adapter.display_name} stocks {did_you_mean!r}. "
                              f"Re-run with that spelling.")

                # a brand whose requests were refused says nothing about stock
                blocked = getattr(spider, "blocked_by_brand", {}) or {}
                throttled = [b for b in empty if blocked.get(b)]
                genuinely_empty = [b for b in empty if not blocked.get(b)]

                for brand in throttled:
                    print(f"\n  BLOCKED: {brand} returned no products because "
                          f"{adapter.display_name} refused {blocked[brand]} request(s). "
                          f"This says nothing about whether the brand is stocked -- "
                          f"re-run it on its own, or more slowly.")

                if genuinely_empty:
                    print(f"\n  NOTE: {', '.join(genuinely_empty)} returned no products at "
                          f"{adapter.display_name}. That may mean the retailer does not "
                          f"stock the brand, not that it has no promotions.")

        print(f"  products accepted : {run_stats['product_records']}")
        print(f"  campaigns found   : {run_stats['campaign_records']}")
        print(f"  records rejected  : {run_stats['rejected_records']}")
        for reason, count in run_stats["rejections_by_reason"].items():
            print(f"      {reason}: {count}")

        # a brand prepare() warned about is not stocked, so its zero is expected
        unstocked = {
            brand for brand in args.brands
            if any(repr(brand) in w or f"'{brand}'" in w for w in prepare_warnings)
        }
        # only an adapter that resolves brand pages can tell "empty" from
        # "not stocked"; the rest are not judged on empty brands
        expected = (
            [b for b in args.brands if b not in unstocked]
            if adapter.confirms_brand_stocking else []
        )
        empty = [b for b in args.brands if found.get(b, 0) == 0] if args.brands else []

        # what the retailer says it stocks, where it says so at all
        expected_products = adapter.expected_product_count(args.brands)

        status, reason = verdict(
            stats=stats,
            products=run_stats["product_records"],
            brands_expected=expected,
            brands_empty=empty,
            refusal_limit=args.refusal_limit,
            # only judged on a campaigns-only run
            campaigns=(run_stats["campaign_records"]
                       if args.campaigns_only else None),
            expected_products=expected_products,
        )
        refused, total, share = refusal_rate(stats)
        if total:
            print(f"  requests          : {total} ({refused} refused, {share:.0%})")
        print(f"  verdict           : {status.upper()}"
              + (f" -- {reason}" if reason else ""))

        # deliberate skips, stated so they don't look like an unstocked brand
        for label, attribute in (
            ("priced in another currency", "wrong_currency_seen"),
            ("sponsored placements", "sponsored_skipped"),
            ("no featured offer (marketplace only)", "no_buy_box_skipped"),
        ):
            count = getattr(adapter, attribute, 0)
            if count:
                print(f"  skipped           : {count} {label}")
        # written whatever the verdict -- a failed run's record is the useful one
        manifest = build_manifest(
            run_id=run_id,
            adapter=adapter,
            started_at=started_at,
            finished_at=utc_now(),
            status=status,
            stats=stats,
            products=run_stats["product_records"],
            campaigns=run_stats["campaign_records"],
            rejected=run_stats["rejected_records"],
            brands_requested=args.brands,
            brands_empty=empty,
            reason=reason,
            warnings=prepare_warnings,
            files=[str(Path(p).relative_to(paths.root)) for p in written],
        )
        written.append(
            write_manifest(paths.file("manifest", ".json"), manifest))

        print("\n  files written:")
        for path in written:
            print(f"      {path}")

        logging.getLogger(__name__).info("run %s finished: %s", run_id, status)

        # a run that cannot be trusted must not exit like one that worked
        return 0 if status == STATUS_OK else 1
    except Exception as exc:
        # a crashed run still writes a manifest, so partial data is not
        # mistaken for complete data
        logging.getLogger(__name__).exception("run %s failed", run_id)
        print(f"error: {exc}", file=sys.stderr)
        saved = 0
        if partial_writer:
            partial_writer.flush()
            saved = partial_writer.written
        write_manifest(paths.file("manifest", ".json"), build_manifest(
            run_id=run_id,
            adapter=adapter,
            started_at=started_at,
            finished_at=utc_now(),
            status=STATUS_INCOMPLETE,
            stats={},
            products=saved,
            campaigns=0,
            rejected=0,
            brands_requested=args.brands,
            brands_empty=[],
            reason=f"{type(exc).__name__}: {exc}",
            warnings=prepare_warnings,
            files=[str(paths.file("partial", ".jsonl").relative_to(paths.root))]
                  if saved else [],
        ))
        return 2
    finally:
        close_run_log(log_handler)
        if lock is not None:
            lock.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())
