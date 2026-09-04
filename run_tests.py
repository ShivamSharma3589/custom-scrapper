"""Run every test suite and fail loudly if anything regressed.

This is the safety net. It runs entirely against saved HTML fixtures and
in-memory records, so it sends no requests to any retailer and takes a couple
of seconds -- which means there is no excuse for not running it after every
change.

    python run_tests.py            # run everything
    python run_tests.py -q         # summary only

Exit code is 0 only if every suite passed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TESTS_DIR = HERE / "tests"

SUITES = [
    ("adapter contract", "test_adapter_contract.py"),
    ("validation rules", "test_validation.py"),
    ("Lookfantastic extraction", "test_adapter_offline.py"),
    ("Boots extraction", "test_boots_offline.py"),
    ("John Lewis extraction", "test_johnlewis_offline.py"),
    ("AllBeauty extraction", "test_allbeauty_offline.py"),
    ("ASOS extraction", "test_asos_offline.py"),
    ("M&S + Amazon extraction", "test_ms_amazon_offline.py"),
    ("cross-retailer matching", "test_matching.py"),
    ("change detection", "test_history.py"),
    ("category resolution", "test_categories.py"),
    ("campaign discovery", "test_campaigns.py"),
    ("custom offer keywords", "test_keywords.py"),
]


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    quiet = "-q" in argv or "--quiet" in argv

    results = []
    for label, filename in SUITES:
        path = TESTS_DIR / filename
        if not path.exists():
            # A suite that has not been written yet is reported, not silently
            # skipped -- an absent test is a gap, not a pass.
            results.append((label, "MISSING"))
            continue

        proc = subprocess.run(
            [sys.executable, str(path)],
            capture_output=True,
            text=True,
            cwd=str(HERE),
        )
        passed = proc.returncode == 0
        results.append((label, "PASS" if passed else "FAIL"))

        if not quiet or not passed:
            print(f"\n{'=' * 62}\n{label}\n{'=' * 62}")
            print(proc.stdout.rstrip())
            if proc.stderr.strip():
                print("--- stderr ---")
                print(proc.stderr.rstrip())

    print(f"\n{'=' * 62}\nSUMMARY\n{'=' * 62}")
    for label, status in results:
        marker = {"PASS": "  ok  ", "FAIL": " FAIL ", "MISSING": " MISS "}[status]
        print(f" {marker} {label}")

    failed = [label for label, status in results if status != "PASS"]
    if failed:
        print(f"\n{len(failed)} suite(s) not passing: {', '.join(failed)}")
        return 1

    print(f"\nAll {len(results)} suites passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
