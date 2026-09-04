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
import sys
from datetime import datetime, timezone
from pathlib import Path

# Make the package importable no matter which directory the script is invoked
# from, so `python code/run.py ...` works from the repo root as well as
# `python run.py ...` from inside code/.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from retailscraper.adapters.base import available_adapters, get_adapter  # noqa: E402
from retailscraper.keywords import KeywordFileError, describe, load_keywords  # noqa: E402
from retailscraper.promotions import set_offer_keywords  # noqa: E402
from retailscraper.output import build_payload, write_all, write_json  # noqa: E402
from retailscraper.spider import RetailPromotionSpider  # noqa: E402


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

    # Adapter setup that needs its own network calls happens here, before the
    # crawler's event loop exists. Warnings are printed rather than fatal: a
    # brand one retailer does not stock should not stop the others.
    for warning in adapter.prepare(args.brands):
        print(f"warning: {warning}", file=sys.stderr)

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
    )

    result = spider.start()

    # Categories are learned from listing pages during the crawl and applied
    # here, once every product is in hand.
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
    )

    if args.brands:
        slug = "-".join(b.lower().replace(" ", "-") for b in args.brands)
    else:
        slug = "site"
    basename = f"{adapter.name}_{slug}"
    written = write_all(payload, out_dir, basename)

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
            print(f"\n  NOTE: {', '.join(empty)} returned no products at "
                  f"{adapter.display_name}. That may mean the retailer does not "
                  f"stock the brand, not that it has no promotions.")

    print(f"  products accepted : {run_stats['product_records']}")
    print(f"  campaigns found   : {run_stats['campaign_records']}")
    print(f"  records rejected  : {run_stats['rejected_records']}")
    for reason, count in run_stats["rejections_by_reason"].items():
        print(f"      {reason}: {count}")
    print("\n  files written:")
    for path in written:
        print(f"      {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
