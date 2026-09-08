"""Command-line runner.

Examples
--------
Discover what the scraper can target:

    python run.py --list-retailers

A small demo run against one brand:

    python run.py --retailer lookfantastic --brands Clinique --max-products 15

A full nightly run across several brands:

    python run.py --retailer lookfantastic \
        --brands Clinique MAC "Tom Ford" "Jo Malone" "Estee Lauder" \
        --out-dir output
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List

# Make the package importable no matter which directory the script is invoked
# from, so `python code/run.py ...` works from the repo root as well as
# `python run.py ...` from inside code/.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from retailscraper.adapters.base import available_adapters, get_adapter  # noqa: E402
from retailscraper.keywords import KeywordFileError, describe, load_keywords  # noqa: E402
from retailscraper.validation import (  # noqa: E402
    KNOWN_BRANDS,
    canonical_brand,
    suggest_brand,
)
from retailscraper.promotions import set_offer_keywords  # noqa: E402
from retailscraper.output import build_payload, write_all, write_json  # noqa: E402
from retailscraper.spider import RetailPromotionSpider  # noqa: E402
from retailscraper.runs import (  # noqa: E402
    DEFAULT_REFUSAL_LIMIT,
    STATUS_INCOMPLETE,
    STATUS_OK,
    build_manifest,
    capture_logger,
    close_run_log,
    new_run_id,
    PartialWriter,
    RunLockBusy,
    open_run_log,
    refusal_rate,
    run_folder,
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
        "--archive",
        action="store_true",
        help="Also keep a timestamped copy of this run under output/history/, "
             "so changes.py can show what moved between runs.",
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
        "--no-run-folders",
        dest="run_folders",
        action="store_false",
        help="Write straight into --out-dir instead of a timestamped run "
             "folder. Handy when experimenting by hand; a scheduled run "
             "should keep the folders so it never overwrites the run before.",
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

    # A campaigns-only run needs no brands: it asks what the whole site is
    # promoting, not what one brand's products cost.
    if not args.brands and not args.campaigns_only:
        print("error: --brands is required unless you pass --campaigns-only",
              file=sys.stderr)
        return 2

    adapter = get_adapter(args.retailer)

    # Correct known misspellings in what the USER typed, before anything uses
    # it. Every retailer spells the brand "Bobbi Brown"; the brief said
    # "Bobbie Brown", which matched nothing anywhere and read as "not
    # stocked". Only the question is normalised -- never the retailer's answer.
    corrected = []
    for raw_brand in args.brands:
        fixed = canonical_brand(raw_brand)
        if fixed != raw_brand:
            print(f"note: reading {raw_brand!r} as {fixed!r}")
        corrected.append(fixed)
    args.brands = corrected

    # Refuse rather than return an empty file that looks like "no promotions".
    if args.campaigns_only and not adapter.campaign_discovery_urls():
        print(
            f"error: no campaign hub pages are known for {adapter.display_name}, "
            f"so --campaigns-only would find nothing. Add "
            f"CAMPAIGN_HUB_PATHS to its adapter first.",
            file=sys.stderr,
        )
        return 2

    # Default the output next to this script rather than to the current
    # directory, so results always land in one predictable place regardless of
    # where the command was run from.
    out_dir = args.out_dir or (Path(__file__).resolve().parent / "output")

    # Refuse rather than silently ignore: a category filter that quietly does
    # nothing hands back the whole brand while looking like it worked.
    if args.categories and not adapter.supports_categories:
        print(
            f"error: {adapter.display_name} does not expose category listings, "
            f"so --categories cannot be honoured. Re-run without it.",
            file=sys.stderr,
        )
        return 2

    # A custom offer vocabulary replaces the built-in rules for the whole run.
    # Loaded before anything else so a broken keyword file fails immediately
    # rather than after a long crawl that quietly matched nothing.
    keyword_set = None
    if args.offer_keywords:
        try:
            keyword_set = load_keywords(args.offer_keywords)
        except KeywordFileError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        set_offer_keywords(keyword_set)

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

    # --- run identity ----------------------------------------------------
    # Every run gets its own folder, so a scheduled run never overwrites the
    # one before it, and its log sits beside the data it produced.
    started_at = utc_now()
    run_id = new_run_id(started_at)
    base_dir = out_dir
    if args.run_folders:
        out_dir = run_folder(base_dir, adapter, started_at)

    # Two runs of one retailer at once share its rate limit, so both come
    # back short. Skipping is the better failure.
    lock = None
    if args.run_folders:
        try:
            lock = run_lock(base_dir, adapter)
            lock.__enter__()
        except RunLockBusy as exc:
            print(f"skipped: {exc}", file=sys.stderr)
            return 3
    # Everything from here is guarded: whatever happens, the lock is
    # released and the log is closed. Releasing only on success meant a
    # browser dying mid-crawl left the lock behind, and STALE_LOCK_SECONDS
    # then skipped the next two scheduled sweeps for this retailer while
    # run_all.py reported them as a harmless "already running".
    log_handler = None
    partial_writer = None
    prepare_warnings: List[str] = []
    try:
        log_handler = open_run_log(out_dir) if args.run_folders else None
        logging.getLogger(__name__).info(
            "run %s starting: %s, brands=%s", run_id, adapter.display_name,
            ", ".join(args.brands) or "(campaigns only)",
        )

        # Adapter setup that needs its own network calls happens here, before the
        # crawler's event loop exists. Warnings are printed rather than fatal: a
        # brand one retailer does not stock should not stop the others.
        #
        # They are kept, too: a warning names a brand the retailer told us it does
        # not stock, and a brand in that list returning nothing is expected rather
        # than a failure. Without the distinction, every unstocked brand would
        # fail the run.
        for warning in adapter.prepare(args.brands):
            prepare_warnings.append(warning)
            logging.getLogger(__name__).warning(warning)
            print(f"warning: {warning}", file=sys.stderr)

        # Save as the crawl goes, so a run that is killed leaves usable data.
        partial_writer = PartialWriter(out_dir) if args.run_folders else None

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

        # The crawler's logger does not propagate, so it has to be attached
        # directly or run.log records nothing the crawl did.
        capture_logger(log_handler, getattr(spider, "logger", None))

        result = spider.start()

        # Categories are learned from listing pages during the crawl and applied
        # here, once every product is in hand.
        # A campaign found on the last page applies to the first product
        # too, so attachment happens once everything is in hand.
        with_campaigns = spider.apply_campaigns()
        if with_campaigns:
            print(f"campaigns attached to {with_campaigns} product(s)")

        if args.resolve_categories:
            filled = spider.apply_category_index()
            print(f"\ncategory index: {len(spider.category_index)} product(s) mapped, "
                  f"{filled} record(s) filled in")

        # `CrawlStats` is a dataclass-like object; fall back gracefully if the
        # attribute set changes in a future Scrapling release.
        try:
            stats = dict(vars(result.stats))
        except TypeError:  # pragma: no cover
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

        if args.brands:
            slug = "-".join(b.lower().replace(" ", "-") for b in args.brands)
        else:
            slug = "site"
        basename = f"{adapter.name}_{slug}"
        # Everything that was saved as we went is now in the finished files, so
        # the partial copy is removed -- a folder still holding one is itself the
        # signal that the run did not get this far.
        if partial_writer:
            partial_writer.flush()

        written = write_all(payload, out_dir, basename)

        if partial_writer:
            partial_writer.discard()

        if args.archive:
            # A timestamped copy is what makes change detection possible at all:
            # without it every run overwrites the only evidence of what came
            # before. The timestamp is filename-safe and sorts chronologically.
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
            archive_path = out_dir / "history" / f"{basename}_{stamp}.json"
            written.append(write_json(payload, archive_path))

        run_stats = payload["run_stats"]
        print("\n--- run summary ---")

        # Per-brand counts, and a loud line for any brand that returned nothing.
        # A total on its own hides a brand that yielded zero -- asking for two
        # brands and getting six records reads as "six found", not "one brand
        # produced nothing at all", so an unstocked brand looks identical to a
        # brand with no promotions.
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
                # Every brand the crawl actually saw, including ones it rejected
                # for being the wrong brand. Comparing an empty request against
                # that catches a typo without anyone having to predict it: the
                # brief's "Bobbie Brown" returned nothing at all six retailers
                # and read as "not stocked".
                seen_brands = {p.brand for p in spider.products.values() if p.brand}
                seen_brands |= {
                    (r.payload or {}).get("brand")
                    for r in spider.rejected
                    if (r.payload or {}).get("brand")
                }
                # Check against the brands we track as well as those seen: a
                # retailer that returns nothing for a misspelling never reveals
                # the right spelling, which is precisely when help is needed.
                candidates = sorted(seen_brands | set(KNOWN_BRANDS))
                for brand in empty:
                    did_you_mean = suggest_brand(brand, candidates)
                    if did_you_mean:
                        print(f"\n  DID YOU MEAN: {brand!r} found nothing, but "
                              f"{adapter.display_name} stocks {did_you_mean!r}. "
                              f"Re-run with that spelling.")

                # A brand whose requests were refused is NOT evidence of anything
                # about stock. Saying "may not be stocked" there turns a rate
                # limit into a false business conclusion: John Lewis blocked all
                # 96 Too Faced requests and the run reported it as possibly
                # unstocked, when John Lewis stocks it.
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

        # --- verdict ---------------------------------------------------------
        # A brand `prepare()` warned about is one the retailer said it does not
        # stock, so its empty result is expected. Every other requested brand is
        # expected to return something, and a silent zero there is the failure
        # that hid John Lewis's missing Estee Lauder for weeks.
        unstocked = {
            brand for brand in args.brands
            if any(repr(brand) in w or f"'{brand}'" in w for w in prepare_warnings)
        }
        # Only an adapter that positively resolves a brand page can tell an
        # empty brand apart from one the shop does not carry. Where it cannot,
        # nothing is expected and an empty brand is not held against the run.
        expected = (
            [b for b in args.brands if b not in unstocked]
            if adapter.confirms_brand_stocking else []
        )
        empty = [b for b in args.brands if found.get(b, 0) == 0] if args.brands else []

        # What the retailer itself says it lists, where it says so. Only
        # John Lewis and M&S publish a total; the rest return None and are
        # judged on what they found.
        expected_products = adapter.expected_product_count(args.brands)

        status, reason = verdict(
            stats=stats,
            products=run_stats["product_records"],
            brands_expected=expected,
            brands_empty=empty,
            refusal_limit=args.refusal_limit,
            # Only judged on a campaigns-only run; a normal run is judged on
            # its products, which the checks above already cover.
            campaigns=(run_stats["campaign_records"]
                       if args.campaigns_only else None),
            expected_products=expected_products,
        )
        refused, total, share = refusal_rate(stats)
        if total:
            print(f"  requests          : {total} ({refused} refused, {share:.0%})")
        print(f"  verdict           : {status.upper()}"
              + (f" -- {reason}" if reason else ""))

        # Products an adapter dropped before they ever became records. These are
        # deliberate skips, but silent ones look exactly like a brand the
        # retailer does not stock, so the counts are stated.
        for label, attribute in (
            ("priced in another currency", "wrong_currency_seen"),
            ("sponsored placements", "sponsored_skipped"),
            ("no featured offer (marketplace only)", "no_buy_box_skipped"),
        ):
            count = getattr(adapter, attribute, 0)
            if count:
                print(f"  skipped           : {count} {label}")
        # --- manifest --------------------------------------------------------
        # Written whatever the verdict: a failed run's record is the one worth
        # keeping, because it is what a blocking report is built from.
        if args.run_folders:
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
                files=[Path(p).name for p in written],
            )
            written.append(write_manifest(out_dir, manifest))

        print("\n  files written:")
        for path in written:
            print(f"      {path}")

        logging.getLogger(__name__).info("run %s finished: %s", run_id, status)

        # Cron reads this. A run that could not be trusted must not look like one
        # that worked -- see the module docstring of retailscraper/runs.py.
        return 0 if status == STATUS_OK else 1
    except Exception as exc:
        # A crashed run still owes an account of itself. Without a manifest,
        # the folder holds a half-written products.partial.jsonl and nothing
        # to say the run never finished, so a loader cannot tell partial data
        # from complete data.
        logging.getLogger(__name__).exception("run %s failed", run_id)
        print(f"error: {exc}", file=sys.stderr)
        if args.run_folders:
            saved = partial_writer.written if partial_writer else 0
            if partial_writer:
                partial_writer.flush()
                saved = partial_writer.written
            write_manifest(out_dir, build_manifest(
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
                files=[PartialWriter.FILENAME] if saved else [],
            ))
        return 2
    finally:
        close_run_log(log_handler)
        if lock is not None:
            lock.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())
