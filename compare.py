"""Compare prices for the same products across retailers.

Takes the JSON output of two or more runs and reports where one retailer
undercuts another on the same product -- the question the whole project
exists to answer.

    python compare.py output/lookfantastic_clinique.json output/boots_clinique.json

    # everything scraped so far
    python compare.py output/*.json --out-dir output
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from retailscraper.matching import match_across_retailers  # noqa: E402

COMPARISON_COLUMNS = [
    "brand", "title", "size", "match_score", "retailer_count",
    "cheapest_retailer", "cheapest_price", "price_gap",
]


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find where retailers undercut each other on the same products.",
    )
    parser.add_argument("runs", nargs="+", type=Path,
                        help="JSON files produced by run.py (two or more).")
    parser.add_argument("--threshold", type=float, default=0.85,
                        help="Title similarity required to call two products the "
                             "same, 0-1 (default: 0.85). Lower it for more matches, at the cost of pairing products that only read alike.")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="Write comparison.json and comparison.csv here "
                             "(default: the 'output' folder next to this script).")
    parser.add_argument("--show-unmatched", action="store_true",
                        help="Also list products found at only one retailer.")
    return parser.parse_args(argv)


def load_products(paths) -> list:
    """Read every product record from the given run files."""
    products = []
    for path in paths:
        if not path.exists():
            print(f"warning: {path} does not exist, skipping", file=sys.stderr)
            continue
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        products.extend(payload.get("products", []))
    return products


def main(argv=None) -> int:
    args = parse_args(argv)

    products = load_products(args.runs)
    retailers = sorted({p.get("retailer") for p in products if p.get("retailer")})

    if len(retailers) < 2:
        print(f"error: need products from at least two retailers, found "
              f"{retailers or 'none'}. Run the scraper against another retailer first.",
              file=sys.stderr)
        return 2

    print(f"Comparing {len(products)} products across {len(retailers)} retailers: "
          f"{', '.join(retailers)}\n")

    matches, unmatched = match_across_retailers(products, threshold=args.threshold)

    if not matches:
        print("No products matched across retailers.")
        print("That usually means the runs covered different parts of the catalogue "
              "rather than that the retailers share nothing -- try scraping the same "
              "brand and category at each.")
    else:
        print(f"{len(matches)} product(s) sold by more than one retailer:\n")
        for match in matches:
            gap = f"£{match.price_gap}" if match.price_gap is not None else "-"
            print(f"  {match.brand} — {match.title}")
            print(f"    price gap {gap}   (match confidence {match.score:.2f})")
            for offer in sorted(match.offers,
                                key=lambda o: (o.get("current_price") is None,
                                               o.get("current_price"))):
                price = offer.get("current_price")
                was = offer.get("original_price")
                was_text = f" (was {was})" if was else ""
                print(f"      {offer['retailer']:16} {price}{was_text}")
            print()

    print(f"{len(unmatched)} product(s) found at only one retailer.")
    if args.show_unmatched:
        for row in unmatched:
            print(f"    {row.get('retailer'):16} {row.get('product_title', '')[:60]}")

    out_dir = args.out_dir or (Path(__file__).resolve().parent / "output")
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "retailers": retailers,
        "threshold": args.threshold,
        "matched_products": [m.to_dict() for m in matches],
        "single_retailer_products": len(unmatched),
    }
    json_path = out_dir / "comparison.json"
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)

    csv_path = out_dir / "comparison.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COMPARISON_COLUMNS,
                                extrasaction="ignore")
        writer.writeheader()
        for match in matches:
            writer.writerow(match.to_dict())

    print(f"\n  written: {json_path}")
    print(f"           {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
