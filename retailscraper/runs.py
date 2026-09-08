"""Everything a run needs to be repeatable, traceable and safe to schedule.

Run by hand, a person looks at the output and decides whether it seems
right. A scheduled run has nobody doing that, so this module supplies it:

  * a folder per run, so runs never overwrite each other
  * a log beside the data it produced
  * a manifest saying what happened, including when it went wrong
  * a verdict, so a blocked run exits non-zero instead of reporting success
  * a lock, so two runs of one retailer never compete for the network

The verdict matters most: before it, every run exited 0 -- including one
that had 1,501 of its 1,840 requests refused and produced five products.
"""

import json
import logging
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

#: A run that produced usable data.
STATUS_OK = "ok"
#: A run that finished but cannot be trusted -- refused too often, or a brand
#: the retailer confirmed it stocks came back empty.
STATUS_FAILED = "failed"
#: A run that was killed part-way. Its data is real but partial.
STATUS_INCOMPLETE = "incomplete"

#: Share of requests a retailer may refuse before the run is not believable.
#: Refusals are normal in small numbers -- a facet with no second page 404s --
#: so this is not zero. It is a default, not a law: a John Lewis run that
#: refused 417 of 1,235 requests (33.8%) still produced 770 good products
#: across seven brands, so raise it per retailer rather than failing good runs.
DEFAULT_REFUSAL_LIMIT = 0.30

#: The share of a retailer's own stated catalogue a run must reach before it
#: is believable. Well below 1.0 because a stated total counts things a brand
#: crawl legitimately will not return -- other colours of one product, items
#: out of stock -- but far enough above zero to catch a crawl that was cut
#: short: John Lewis says 236 Clinique products and, while soft-blocking deep
#: pagination, serves 48.
MIN_COVERAGE = 0.5

#: A lock older than this is assumed to belong to a run that died without
#: cleaning up, rather than to one still going. Longer than the slowest
#: retailer's sweep (John Lewis, about an hour) with room to spare.
STALE_LOCK_SECONDS = 6 * 60 * 60


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_run_id(when: Optional[datetime] = None) -> str:
    """An id that sorts chronologically and is unique across machines.

    The timestamp prefix means a directory listing of run ids is in run order;
    the random suffix keeps two retailers starting in the same second apart.
    """
    when = when or utc_now()
    return f"{when.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def retailer_folder_name(adapter) -> str:
    """The folder a retailer's runs live under: "John Lewis" -> john_lewis."""
    name = (getattr(adapter, "display_name", "") or getattr(adapter, "name", "")
            or "unknown")
    name = name.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_") or "unknown"


def run_folder(base: Path, adapter, when: Optional[datetime] = None) -> Path:
    """`<base>/<retailer>/YYYY/MM/DD/HH-MM-SS`, in UTC.

    UTC rather than local time because British clocks move twice a year. In
    October 01:30 happens twice, so two runs would land in one folder and the
    second would erase the first; in March it does not happen at all.

    Zero-padded because folder names sort as text: unpadded, month 10 sorts
    before month 2.
    """
    when = when or utc_now()
    return (
        base
        / retailer_folder_name(adapter)
        / f"{when.year:04d}"
        / f"{when.month:02d}"
        / f"{when.day:02d}"
        / when.strftime("%H-%M-%S")
    )


# --- logging --------------------------------------------------------------

def open_run_log(folder: Path, level: int = logging.INFO) -> logging.Handler:
    """Send everything logged during this run to `run.log` in its folder.

    Attached to the root logger so the crawler's own output is captured too --
    the request-by-request record is what makes a failed run diagnosable
    afterwards, and it is the part that used to vanish with the terminal.
    """
    folder.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(folder / "run.log", encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    handler.setLevel(level)
    root = logging.getLogger()
    root.addHandler(handler)
    # without this the root level (WARNING) discards the crawler's INFO
    # records and run.log holds only errors
    if root.level > level:
        root.setLevel(level)
    return handler


def capture_logger(handler: Optional[logging.Handler], logger: Any) -> None:
    """Also send one non-propagating logger's records to the run log.

    The crawler names its logger `scrapling.spiders.<name>` and sets
    `propagate = False`, so nothing it logs ever reaches the root handler --
    which left run.log holding two lines of our own and none of the
    request-by-request record that makes a failed run diagnosable. The logger
    does not exist until the spider is constructed, so this is called then
    rather than when the file is opened.
    """
    if handler is None or logger is None:
        return
    if handler not in logger.handlers:
        logger.addHandler(handler)


def close_run_log(handler: Optional[logging.Handler]) -> None:
    """Detach the run log from every logger, then close it.

    Every logger, not just the root one: `capture_logger` attaches this
    handler to the crawler's non-propagating logger too, and removing it from
    root alone leaves a closed file attached there. Anything logged
    afterwards -- a browser teardown warning, or a second run driven in the
    same process -- would then raise "I/O operation on closed file" from
    inside logging itself.
    """
    if handler is None:
        return
    logging.getLogger().removeHandler(handler)
    for name in list(logging.root.manager.loggerDict):
        existing = logging.getLogger(name)
        if handler in getattr(existing, "handlers", ()):
            existing.removeHandler(handler)
    handler.close()


# --- locking --------------------------------------------------------------

class RunLockBusy(Exception):
    """Raised when another run of the same retailer is still going."""


@contextmanager
def run_lock(base: Path, adapter) -> Iterator[Path]:
    """Hold a per-retailer lock for the duration of a run.

    Two runs of one retailer at once is worse than a skipped run: they share
    the rate limit, so both get throttled and both come back short. Running
    different retailers concurrently is its own hazard -- three browser-driven
    crawls at once starved DNS badly enough to turn a 54-minute run into 30
    hours -- which is why the scheduled entry point runs them in sequence.
    """
    base.mkdir(parents=True, exist_ok=True)
    path = base / f".{retailer_folder_name(adapter)}.lock"
    details = f"pid={os.getpid()} started={utc_now().isoformat()}"

    def claim() -> bool:
        """Create the lock, or return False because someone else holds it.

        `O_CREAT | O_EXCL` makes creation atomic: the file is created only if
        it does not exist, and the check and the write are one operation.
        Testing `exists()` and then writing left a window in which a manual
        run and the scheduled sweep could both pass the check and crawl the
        same retailer at once -- which shares its rate limit and is exactly
        what the lock is for.
        """
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(handle, "w", encoding="utf-8") as writer:
            writer.write(details)
        return True

    if not claim():
        age = None
        try:
            age = utc_now().timestamp() - path.stat().st_mtime
        except OSError:
            # gone between the claim and the stat -- whoever held it finished
            age = None
        if age is not None and age < STALE_LOCK_SECONDS:
            held = path.read_text(encoding="utf-8", errors="replace").strip()
            raise RunLockBusy(
                f"{adapter.display_name} is already running "
                f"({held or 'no details'}, {int(age / 60)} min ago)"
            )
        # older than any real run, so the previous process died
        logging.getLogger(__name__).warning(
            "removing stale lock %s (%s hours old)", path, int((age or 0) / 3600)
        )
        path.unlink(missing_ok=True)
        if not claim():
            raise RunLockBusy(
                f"{adapter.display_name} was claimed by another run "
                f"while this one was clearing a stale lock"
            )

    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


# --- verdict --------------------------------------------------------------

#: Statuses meaning "turned away". 404 is deliberately absent -- it means
#: the page is not there, which is an answer. Counting it failed a correct
#: run: M&S 404s all four listing pages for a brand it does not stock.
REFUSAL_STATUSES = (403, 429, 503)


def refusal_rate(stats: Dict[str, Any]) -> Tuple[int, int, float]:
    """(refused, total, share) for one run.

    Counted from the response statuses the crawler recorded rather than from
    its blocked-request counter, because that counter also includes retries
    the crawler recovered from.
    """
    statuses = stats.get("response_status_count") or {}
    total = int(stats.get("requests_count") or 0)
    refused = 0
    for key, count in statuses.items():
        match = re.search(r"(\d{3})", str(key))
        if match and int(match.group(1)) in REFUSAL_STATUSES:
            refused += int(count or 0)
    share = (refused / total) if total else 0.0
    return refused, total, share


def verdict(
    stats: Dict[str, Any],
    products: int,
    brands_expected: Sequence[str] = (),
    brands_empty: Sequence[str] = (),
    refusal_limit: float = DEFAULT_REFUSAL_LIMIT,
    campaigns: Optional[int] = None,
    expected_products: Optional[int] = None,
) -> Tuple[str, Optional[str]]:
    """Did this run produce data worth loading? Returns (status, reason).

    Two ways to fail, both drawn from real runs:

      * Too many refusals. Next answered 1,501 of 1,840 requests with 403 and
        still reported success, having produced five products.
      * A brand the retailer confirmed it stocks came back empty. John Lewis
        returned nothing for Estee Lauder for weeks because its brand code
        never resolved, and a silent zero is indistinguishable from a brand
        the shop does not carry.

    `brands_expected` is the list `prepare()` did NOT warn about, so a brand
    genuinely not stocked never triggers this.
    """
    refused, total, share = refusal_rate(stats)
    if total and share > refusal_limit:
        return STATUS_FAILED, (
            f"{refused} of {total} requests refused "
            f"({share:.0%}, limit {refusal_limit:.0%})"
        )

    missing = [b for b in brands_empty if b in set(brands_expected)]
    if missing:
        return STATUS_FAILED, (
            "no products for stocked brand(s): " + ", ".join(sorted(missing))
        )

    if products == 0 and brands_expected:
        return STATUS_FAILED, "no products at all"

    # a campaigns-only run has no products to judge, so without this it
    # reported success however little it found. None on a normal run.
    if campaigns is not None and campaigns == 0:
        return STATUS_FAILED, "no campaigns found on a campaigns-only run"

    # a crawl cut short is not a successful crawl: collecting a fraction of
    # the retailer's own total means pagination was blocked or a route broke
    if expected_products and products < expected_products * MIN_COVERAGE:
        return STATUS_FAILED, (
            f"collected {products} of the {expected_products} products the "
            f"retailer says it lists ({products / expected_products:.0%})"
        )

    return STATUS_OK, None


# --- manifest -------------------------------------------------------------

def build_manifest(
    run_id: str,
    adapter,
    started_at: datetime,
    finished_at: datetime,
    status: str,
    stats: Dict[str, Any],
    products: int,
    campaigns: int,
    rejected: int,
    brands_requested: Sequence[str],
    brands_empty: Sequence[str],
    expected_products: Optional[int] = None,
    reason: Optional[str] = None,
    warnings: Sequence[str] = (),
    files: Sequence[str] = (),
) -> Dict[str, Any]:
    """The record of one run: what was asked for, what came back, and why.

    This is the row that becomes a warehouse run record. It is deliberately
    flat and free of nested objects so it can be loaded without a schema
    argument, and it carries the `run_id` that also appears on every product
    and campaign the run produced.
    """
    refused, total, _ = refusal_rate(stats)
    return {
        "run_id": run_id,
        "retailer": adapter.display_name,
        "retailer_key": retailer_folder_name(adapter),
        "domain": adapter.domain,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": round((finished_at - started_at).total_seconds(), 1),
        "status": status,
        "reason": reason,
        "products": products,
        # the gap between this and `products` is the run's real coverage
        "expected_products": expected_products,
        "campaigns": campaigns,
        "rejected": rejected,
        "requests_total": total,
        "requests_refused": refused,
        "brands_requested": list(brands_requested),
        "brands_empty": list(brands_empty),
        "warnings": list(warnings),
        "files": list(files),
    }


def write_manifest(folder: Path, manifest: Dict[str, Any]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "manifest.json"
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


def read_manifest(folder: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return None


# --- incremental saving ---------------------------------------------------

class PartialWriter:
    """Flushes products to disk while a run is still going.

    A run that dies used to leave nothing at all: the John Lewis crawl was
    stopped at 422 requests and wrote no file, losing about 35 minutes of
    work. Products are appended here as newline-delimited JSON, which can be
    written one record at a time and read back after a crash.

    The partial file is deleted once the real output is written, so a folder
    holding one is itself the signal that the run did not finish.
    """

    FILENAME = "products.partial.jsonl"

    def __init__(self, folder: Path, every: int = 50) -> None:
        self.folder = folder
        self.every = max(1, every)
        self.path = folder / self.FILENAME
        self._pending: List[Dict[str, Any]] = []
        self.written = 0

    def add(self, record: Dict[str, Any]) -> None:
        self._pending.append(record)
        if len(self._pending) >= self.every:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        self.folder.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for record in self._pending:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.written += len(self._pending)
        self._pending.clear()

    def discard(self) -> None:
        """Drop the partial file once the finished output has been written."""
        self._pending.clear()
        self.path.unlink(missing_ok=True)
