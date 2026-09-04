"""Fetch specific product pages live and print what we extract from each.

This exists for manual verification: open the same URL in a browser and
compare the fields side by side. Checking a sample against the live site is
the only way to catch extraction that is confidently wrong, which no amount
of internal validation can do on its own.

    python spotcheck.py --retailer lookfantastic --urls URL [URL ...]
    python spotcheck.py --retailer lookfantastic --from-output output/lf.json --sample 10
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

# Importable from any working directory (see run.py).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scrapling.fetchers import Fetcher  # noqa: E402

from retailscraper.adapters.base import get_adapter  # noqa: E402
from retailscraper.validation import validate  # noqa: E402


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Spot-check extraction against live pages.")
    parser.add_argument("--retailer", required=True, help="Adapter name or domain.")
    parser.add_argument("--urls", nargs="*", default=[], help="Product URLs to check.")
    parser.add_argument(
        "--from-output",
        type=Path,
        help="Take URLs from a previous run's JSON output instead of --urls.",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=10,
        help="How many URLs to sample from --from-output (default: 10).",
    )
    parser.add_argument("--brands", nargs="*", default=[], help="Brands to validate against.")
    return parser.parse_args(argv)


def collect_urls(args) -> list:
    """Build the list of URLs to check, from flags or a previous run."""
    if args.urls:
        return args.urls

    if args.from_output:
        with args.from_output.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        urls = [p["product_url"] for p in payload.get("products", [])]
        # A random sample beats the first N: the first N share whatever
        # ordering the sitemap has, so they tend to be the same kind of page.
        return random.sample(urls, min(args.sample, len(urls)))

    return []


def main(argv=None) -> int:
    args = parse_args(argv)
    adapter = get_adapter(args.retailer)

    urls = collect_urls(args)
    if not urls:
        print("error: give --urls or --from-output", file=sys.stderr)
        return 2

    print(f"Spot-checking {len(urls)} page(s) against {adapter.display_name} live.\n")

    for index, url in enumerate(urls, start=1):
        response = Fetcher.get(url, stealthy_headers=True, follow_redirects=True)
        print(f"[{index}/{len(urls)}] HTTP {response.status}  {url}")

        if response.status != 200:
            print("    could not fetch\n")
            continue

        # Pass the brands through: an adapter may use them for
        # last-resort brand corroboration on pages with no structured brand.
        product = adapter.parse_product(response, str(response.url), args.brands)
        if product is None:
            print("    no product extracted from this page\n")
            continue

        rejection = validate(product, args.brands or [product.brand])
        print(f"    title      : {product.product_title}")
        print(f"    brand      : {product.brand}  (via {product.brand_verified_by})")
        print(f"    id / sku   : {product.product_id} / {product.sku}   variants: {product.variant_count}")
        print(f"    was -> now : {product.original_price} -> {product.current_price} {product.currency}")
        print(f"    discount   : {product.discount_amount} ({product.discount_percent}%)")
        print(f"    stock      : {product.availability}")
        print(f"    promo      : {product.promotional_copy}")
        print(f"    verdict    : {rejection.reason if rejection else 'ACCEPTED'}")
        if rejection:
            print(f"                 {rejection.detail}")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
