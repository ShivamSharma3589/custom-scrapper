"""The scheduled job. Edit the three settings below, then run it.

Built for Windows Task Scheduler:

    Program    <this folder>\\.venv\\Scripts\\python.exe
    Arguments  scrape_job.py
    Start in   <this folder>

Retailers run one after another, never together -- browser crawls starve each
other, and three at once once turned a 54-minute run into 30 hours.

Every run appends to output/scrape_job.log:

    2026-09-09 14:30:00  --- start: 3 retailer(s), campaigns_only=False ---
    2026-09-09 15:24:11  lookfantastic  ok       products=1069  campaigns=9  refused=0/1083   3251s
    2026-09-09 16:02:40  boots          ok       products=252   campaigns=20 refused=0/289    2309s
    2026-09-09 16:05:02  john_lewis     blocked  products=0     campaigns=0  refused=0/2      142s
    2026-09-09 16:05:02  --- done: 1321 products, 29 campaigns, 1 blocked ---

The refused column says whether a shop turned us away. `blocked` means every
page loaded but came back empty, which is how the bigger shops soft-block us.
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Settings. This is the only part to edit.
# ---------------------------------------------------------------------------

#: Which shops to scrape, in order. Names come from `python run.py --list-retailers`.
RETAILERS = ["lookfantastic", "boots", "johnlewis"]

#: Which brands to look for. Ignored when CAMPAIGNS_ONLY is True, because a
#: campaign belongs to the whole shop rather than to one brand.
BRANDS = ["Clinique", "MAC", "Tom Ford", "Jo Malone",
          "Estee Lauder", "Bobbi Brown", "Too Faced"]

#: True  = offers only. A few page fetches per shop, done in a minute or two.
#: False = the full crawl, products and offers. Hours, so schedule it nightly.
CAMPAIGNS_ONLY = True

#: Cap per brand, for a quick test. None means the whole catalogue.
MAX_PRODUCTS = None

# ---------------------------------------------------------------------------

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "output"
LOG = OUTPUT / "scrape_job.log"

#: Give up on a shop after this long, so one hung browser cannot hold up the
#: rest of the list. Generous: a full Lookfantastic crawl is about an hour.
TIMEOUT_SECONDS = 3 * 60 * 60

#: John Lewis throttles hard and refuses a share of requests. At the default
#: limit the run fails outright and publishes nothing, so allow the refusals
#: and let the log report them instead.
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
        # run.py found a lock, so this shop is still busy from an earlier
        # trigger. That is the lock doing its job, not a failure.
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

    # Every page loaded and nothing came back: the shop served us empty
    # pages rather than an error, which is how the big ones soft-block.
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

    # Task Scheduler shows this as the Last Run Result. Non-zero only when
    # nothing worked, so a partial sweep does not look like a failure.
    return 0 if worked else 1


if __name__ == "__main__":
    raise SystemExit(main())
