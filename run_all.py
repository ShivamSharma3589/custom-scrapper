"""Run every retailer in sequence and return one exit code.

    python run_all.py

**Never in parallel.** Three browser-driven crawls at once starved DNS badly
enough to turn a 54-minute run into 30 hours.

    0  every retailer produced usable data
    1  some failed          -- load the good ones, alert
    2  every retailer failed -- alert, load nothing

A failed retailer does not stop the sweep: a Boots outage is no reason to
skip Lookfantastic.
"""

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from retailscraper.adapters.base import available_adapters, get_adapter  # noqa: E402
from retailscraper.runs import retailer_folder_name, utc_now  # noqa: E402

#: The brands this sweep tracks, in one place.
DEFAULT_BRANDS = [
    "Clinique", "MAC", "Tom Ford", "Jo Malone",
    "Estee Lauder", "Bobbi Brown", "Too Faced",
]

#: Retailers left out of the sweep, and why -- listed rather than deleted
#: so the exclusion is visible and reversible.
EXCLUDED: Dict[str, str] = {
    "next": "refuses roughly four requests in five; revisit from the UK server",
}

#: How long one retailer may take before the sweep gives up. Without it a
#: hung browser blocks the sweep for ever. Generous, because John Lewis at
#: 30s per request is slow but not stuck.
DEFAULT_TIMEOUT_SECONDS = 4 * 60 * 60

#: Retailers allowed longer, because their catalogue genuinely warrants it.
TIMEOUT_SECONDS: Dict[str, int] = {
    "johnlewis": 6 * 60 * 60,
}

#: Retailers that refuse often but still deliver. John Lewis produced 770
#: products in a run that refused 33.8% of its requests.
REFUSAL_LIMITS: Dict[str, float] = {
    "johnlewis": 0.45,
}


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run every retailer in sequence and report one exit code.",
    )
    parser.add_argument(
        "--brands", nargs="+", default=DEFAULT_BRANDS,
        help="Brands to track across every retailer.",
    )
    parser.add_argument(
        "--retailers", nargs="+", default=None,
        help="Limit the sweep to these adapters. Default: all but the "
             f"excluded ones ({', '.join(EXCLUDED) or 'none'}).",
    )
    parser.add_argument(
        "--out-dir", type=Path, default=None,
        help="Base output directory (default: ./output).",
    )
    parser.add_argument(
        "--include-excluded", action="store_true",
        help="Also run the retailers normally left out of the sweep.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would run, and run nothing.",
    )
    return parser.parse_args(argv)


def retailers_to_run(args: argparse.Namespace) -> List[str]:
    if args.retailers:
        return list(args.retailers)
    names = list(available_adapters())
    if args.include_excluded:
        return names
    return [n for n in names if n not in EXCLUDED]


def run_one(
    name: str, brands: Sequence[str], out_dir: Path
) -> subprocess.CompletedProcess:
    """Run one retailer as its own process.

    A separate process because these browser sessions do not always shut down
    cleanly -- a crashed child then costs one retailer, not the sweep.
    """
    command = [
        sys.executable, str(Path(__file__).resolve().parent / "run.py"),
        "--retailer", name,
        "--brands", *brands,
        "--out-dir", str(out_dir),
    ]
    limit = REFUSAL_LIMITS.get(name)
    if limit is not None:
        command += ["--refusal-limit", str(limit)]

    timeout = TIMEOUT_SECONDS.get(name, DEFAULT_TIMEOUT_SECONDS)
    try:
        return subprocess.run(command, timeout=timeout)
    except subprocess.TimeoutExpired:
        # the killed child never reached its `finally`, so its lock outlives
        # it -- clear it or the next run skips this retailer for six hours
        print(f"  {name} exceeded {timeout / 3600:.0f}h and was stopped",
              file=sys.stderr)
        lock = out_dir / f".{retailer_folder_name(get_adapter(name))}.lock"
        lock.unlink(missing_ok=True)
        return subprocess.CompletedProcess(command, returncode=2)


def main(argv=None) -> int:
    args = parse_args(argv)
    out_dir = args.out_dir or (Path(__file__).resolve().parent / "output")
    names = retailers_to_run(args)
    started = utc_now()

    print(f"sweep starting {started.isoformat()}")
    print(f"  retailers : {', '.join(names)}")
    print(f"  brands    : {', '.join(args.brands)}")
    for name, reason in EXCLUDED.items():
        if name not in names:
            print(f"  excluded  : {name} -- {reason}")
    print()

    if args.dry_run:
        for name in names:
            print(f"  would run {name}")
        return 0

    results: Dict[str, int] = {}
    for name in names:
        adapter = get_adapter(name)
        print(f"--- {adapter.display_name} ---", flush=True)
        began = utc_now()
        try:
            completed = run_one(name, args.brands, out_dir)
            code = completed.returncode
        except Exception as exc:  # pragma: no cover - a child that would not start
            print(f"  {name} could not start: {exc}", file=sys.stderr)
            code = 2
        results[name] = code
        minutes = (utc_now() - began).total_seconds() / 60
        print(f"--- {adapter.display_name}: exit {code} after {minutes:.0f} min\n",
              flush=True)

    # exit 3 is the lock working, not a failure
    failed = [n for n, c in results.items() if c not in (0, 3)]
    skipped = [n for n, c in results.items() if c == 3]

    print("--- sweep summary ---")
    for name, code in results.items():
        state = {0: "ok", 3: "skipped (already running)"}.get(code, "FAILED")
        print(f"  {name:18} {state}")
    if skipped:
        print(f"\n  {len(skipped)} skipped because a previous run was still going")

    elapsed = (utc_now() - started).total_seconds() / 60
    print(f"\n  {len(results) - len(failed)}/{len(results)} succeeded "
          f"in {elapsed:.0f} min")

    if not failed:
        return 0
    return 2 if len(failed) == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
