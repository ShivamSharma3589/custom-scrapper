"""Report what changed between two runs of the same retailer.

    # explicit files
    python changes.py output/history/lookfantastic_clinique_2026-08-30.json \
                      output/lookfantastic_clinique.json

    # or let it find the two most recent archived runs
    python changes.py --latest --retailer lookfantastic --brands Clinique

Run `run.py --archive` to keep a timestamped copy of each run, which is what
gives this something to compare against.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from retailscraper.history import diff_runs  # noqa: E402

CHANGE_COLUMNS = ["kind", "retailer", "brand", "subject", "detail", "before", "after", "url"]

DEFAULT_OUTPUT = Path(__file__).resolve().parent / "output"


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show what changed between two runs of the same retailer.",
    )
    parser.add_argument("runs", nargs="*", type=Path,
                        help="Two run JSON files: the earlier one first.")
    parser.add_argument("--latest", action="store_true",
                        help="Use the two most recent archived runs instead of "
                             "naming files explicitly.")
    parser.add_argument("--retailer", help="Filter archived runs by retailer (with --latest).")
    parser.add_argument("--brands", nargs="*", default=[],
                        help="Filter archived runs by brand (with --latest).")
    parser.add_argument("--history-dir", type=Path, default=DEFAULT_OUTPUT / "history",
                        help="Where archived runs live (default: output/history).")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT,
                        help="Where to write changes.json / changes.csv.")
    return parser.parse_args(argv)


def find_latest(history_dir: Path, retailer: str, brands) -> list:
    """The two most recent archived runs matching the filters.

    Archived filenames carry retailer, brands and a timestamp, so a plain
    lexical sort puts the newest last.
    """
    if not history_dir.exists():
        return []

    stem_parts = []
    if retailer:
        stem_parts.append(retailer.lower())
    stem_parts.extend(b.lower().replace(" ", "-") for b in brands)

    candidates = [
        p for p in history_dir.glob("*.json")
        if all(part in p.name.lower() for part in stem_parts)
    ]
    if not candidates:
        return []

    # Group by everything before the timestamp, so "the two most recent" means
    # two runs of the SAME retailer and brands. Sorting the whole directory
    # lexically instead would pair a Clinique run with a Tom Ford one --
    # <retailer>_<brands>_<timestamp> only sorts chronologically within one
    # prefix, and the mismatch is invisible in the output.
    groups: dict = {}
    for path in candidates:
        prefix = path.stem.rsplit("_", 1)[0]
        groups.setdefault(prefix, []).append(path)

    # Prefer the group whose newest run is the newest overall, so an
    # unfiltered --latest still means "whatever I scraped most recently".
    def newest(paths):
        return max(p.stem.rsplit("_", 1)[-1] for p in paths)

    best = max(groups.values(), key=newest)
    return sorted(best)[-2:]


def main(argv=None) -> int:
    args = parse_args(argv)

    if args.latest:
        runs = find_latest(args.history_dir, args.retailer, args.brands)
        if len(runs) < 2:
            print(f"error: need two archived runs in {args.history_dir} to compare, "
                  f"found {len(runs)}. Run the scraper twice with --archive.",
                  file=sys.stderr)
            return 2
    elif len(args.runs) == 2:
        runs = args.runs
    else:
        print("error: give two run files, or use --latest", file=sys.stderr)
        return 2

    before_path, after_path = runs
    with before_path.open(encoding="utf-8") as handle:
        before = json.load(handle)
    with after_path.open(encoding="utf-8") as handle:
        after = json.load(handle)

    try:
        result = diff_runs(before, after)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"{result['retailer']}")
    print(f"  before: {result['before_scraped_at']}  ({before_path.name})")
    print(f"  after : {result['after_scraped_at']}  ({after_path.name})")
    print()

    if not result["changes"]:
        print("Nothing changed between these two runs.")
    else:
        print(f"{result['change_count']} change(s):\n")
        for kind, count in result["changes_by_kind"].items():
            print(f"  {kind:24} {count}")
        print()
        for change in result["changes"]:
            print(f"  [{change['kind']}] {change['subject'][:56]}")
            print(f"      {change['detail']}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / "changes.json"
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, ensure_ascii=False)

    csv_path = args.out_dir / "changes.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CHANGE_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result["changes"])

    print(f"\n  written: {json_path}")
    print(f"           {csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
