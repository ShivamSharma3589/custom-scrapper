r"""The scheduled job. Edit the settings below, then run it.

Windows Task Scheduler:

    Program    <this folder>\.venv\Scripts\python.exe
    Arguments  scrape_job.py
    Start in   <this folder>

Retailers run one after another. Every run appends a line per retailer to
output/scrape_job.log.
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Uncomment the shops to scrape. Names: python run.py --list-retailers
RETAILERS = [
    "lookfantastic", 
    # "boots",
    # "johnlewis",
    # "allbeauty",
    # "asos",
    # "next",
    # "amazon",
]

BRANDS = [
    "Clinique",
    "MAC", 
    "Tom Ford", 
    "Jo Malone",
    "Estee Lauder", 
    "Bobbi Brown", 
    "Too Faced",
]

# True = offers only, a minute or two. False = the full crawl, hours.
CAMPAIGNS_ONLY = False

MAX_PRODUCTS = None

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "output"
LOG = OUTPUT / "scrape_job.log"

TIMEOUT_SECONDS = 3 * 60 * 60

REFUSAL_LIMITS = {"johnlewis": 0.9}


def log(message: str) -> None:
    """Write one timestamped line to the log file and the console."""
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}"
    print(line)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def newest_manifest(retailer_folder: Path):
    """The most recent manifest for one retailer, or None."""
    manifests = sorted((retailer_folder / "manifest").glob("*.json"))
    return manifests[-1] if manifests else None


def folder_for(retailer: str) -> Path:
    """Where this retailer writes, e.g. johnlewis -> output/john_lewis."""
    sys.path.insert(0, str(HERE))
    from retailscraper.adapters.base import get_adapter
    from retailscraper.runs import retailer_folder_name

    return OUTPUT / retailer_folder_name(get_adapter(retailer))


def scrape(retailer: str) -> dict:
    """Run one retailer and report what its manifest said."""
    retailer_folder = folder_for(retailer)
    before = newest_manifest(retailer_folder)

    command = [sys.executable, str(HERE / "run.py"),
               "--retailer", retailer, "--out-dir", str(OUTPUT)]
    if CAMPAIGNS_ONLY:
        command.append("--campaigns-only")
    else:
        command += ["--brands", *BRANDS]
        if MAX_PRODUCTS:
            command += ["--max-products", str(MAX_PRODUCTS)]
    if retailer in REFUSAL_LIMITS:
        command += ["--refusal-limit", str(REFUSAL_LIMITS[retailer])]

    started = datetime.now()
    try:
        finished = subprocess.run(command, cwd=str(HERE), timeout=TIMEOUT_SECONDS,
                                  capture_output=True, text=True, errors="replace")
        exit_code = finished.returncode
    except subprocess.TimeoutExpired:
        exit_code = None

    empty = {"products": 0, "campaigns": 0, "refused": 0, "total": 0,
             "seconds": int((datetime.now() - started).total_seconds())}

    if exit_code is None:
        return {**empty, "status": "timeout"}
    if exit_code == 3:
        return {**empty, "status": "skipped"}

    after = newest_manifest(retailer_folder)
    if after is None or after == before:
        return {**empty, "status": "no output"}

    try:
        manifest = json.loads(after.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {**empty, "status": "unreadable"}

    refused = manifest.get("requests_refused") or 0
    total = manifest.get("requests_total") or 0
    products = manifest.get("products") or 0
    campaigns = manifest.get("campaigns") or 0
    status = manifest.get("status") or "unknown"

    if total and not refused and not products and not campaigns:
        status = "blocked"

    return {"status": status, "products": products, "campaigns": campaigns,
            "refused": refused, "total": total, "seconds": empty["seconds"]}


def main() -> int:
    if not CAMPAIGNS_ONLY and not BRANDS:
        print("error: BRANDS is empty. Add brands, or set CAMPAIGNS_ONLY = True.",
              file=sys.stderr)
        return 2

    log(f"--- start: {len(RETAILERS)} retailer(s), "
        f"campaigns_only={CAMPAIGNS_ONLY} ---")

    products = campaigns = blocked = worked = 0

    for retailer in RETAILERS:
        result = scrape(retailer)
        products += result["products"]
        campaigns += result["campaigns"]
        if result["status"] in ("blocked", "timeout"):
            blocked += 1
        if result["status"] == "ok":
            worked += 1

        log(f"{retailer:14} {result['status']:9} "
            f"products={result['products']:<6} "
            f"campaigns={result['campaigns']:<4} "
            f"refused={result['refused']}/{result['total']:<7} "
            f"{result['seconds']}s")

    log(f"--- done: {products} products, {campaigns} campaigns, "
        f"{blocked} blocked ---")

    return 0 if worked else 1


if __name__ == "__main__":
    raise SystemExit(main())
