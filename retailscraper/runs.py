"""Everything a run needs to be repeatable, traceable and safe to schedule."""

import json
import logging
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_INCOMPLETE = "incomplete"

DEFAULT_REFUSAL_LIMIT = 0.30

MIN_COVERAGE = 0.5

STALE_LOCK_SECONDS = 6 * 60 * 60


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_run_id(when: Optional[datetime] = None) -> str:
    """An id that sorts chronologically and is unique across machines."""
    when = when or utc_now()
    return f"{when.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"


def retailer_folder_name(adapter) -> str:
    """The folder a retailer's runs live under: "John Lewis" -> john_lewis."""
    name = (getattr(adapter, "display_name", "") or getattr(adapter, "name", "")
            or "unknown")
    name = name.replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_") or "unknown"


class RunPaths:
    """Where one run's files go: `<base>/<retailer>/<folder>/<timestamp>.<ext>`."""

    def __init__(self, base: Path, adapter, when: Optional[datetime] = None) -> None:
        when = when or utc_now()
        self.root = Path(base) / retailer_folder_name(adapter)
        self.stamp = when.strftime("%Y-%m-%d_%H-%M-%S")

    def file(self, folder: str, suffix: str) -> Path:
        """One file in one of this retailer's folders."""
        return self.root / folder / f"{self.stamp}{suffix}"


def open_run_log(path: Path, level: int = logging.INFO) -> logging.Handler:
    """Send everything logged during this run to its own log file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    handler.setLevel(level)
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level > level:
        root.setLevel(level)
    return handler


def capture_logger(handler: Optional[logging.Handler], logger: Any) -> None:
    """Also send one non-propagating logger's records to the run log."""
    if handler is None or logger is None:
        return
    if handler not in logger.handlers:
        logger.addHandler(handler)


def close_run_log(handler: Optional[logging.Handler]) -> None:
    """Detach the run log from every logger, then close it."""
    if handler is None:
        return
    logging.getLogger().removeHandler(handler)
    for name in list(logging.root.manager.loggerDict):
        existing = logging.getLogger(name)
        if handler in getattr(existing, "handlers", ()):
            existing.removeHandler(handler)
    handler.close()


class RunLockBusy(Exception):
    """Raised when another run of the same retailer is still going."""


@contextmanager
def run_lock(base: Path, adapter) -> Iterator[Path]:
    """Hold a per-retailer lock for the duration of a run."""
    base.mkdir(parents=True, exist_ok=True)
    path = base / f".{retailer_folder_name(adapter)}.lock"
    details = f"pid={os.getpid()} started={utc_now().isoformat()}"

    def claim() -> bool:
        """Create the lock, or return False because someone else holds it."""
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
            age = None
        if age is not None and age < STALE_LOCK_SECONDS:
            held = path.read_text(encoding="utf-8", errors="replace").strip()
            raise RunLockBusy(
                f"{adapter.display_name} is already running "
                f"({held or 'no details'}, {int(age / 60)} min ago)"
            )
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


REFUSAL_STATUSES = (403, 429, 503)


def refusal_rate(stats: Dict[str, Any]) -> Tuple[int, int, float]:
    """(refused, total, share) for one run."""
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
    """Did this run produce data worth loading? Returns (status, reason)."""
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

    if products == 0 and (brands_expected or brands_empty):
        return STATUS_FAILED, "no products at all"

    if campaigns is not None and campaigns == 0:
        return STATUS_FAILED, "no campaigns found on a campaigns-only run"

    if expected_products and products < expected_products * MIN_COVERAGE:
        return STATUS_FAILED, (
            f"collected {products} of the {expected_products} products the "
            f"retailer says it lists ({products / expected_products:.0%})"
        )

    return STATUS_OK, None


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
    """The record of one run: what was asked for, what came back, and why."""
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


def write_manifest(path: Path, manifest: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return path


def read_manifest(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


class PartialWriter:
    """Flushes products to disk while a run is still going."""

    def __init__(self, path: Path, every: int = 50) -> None:
        self.path = Path(path)
        self.every = max(1, every)
        self._pending: List[Dict[str, Any]] = []
        self.written = 0

    def add(self, record: Dict[str, Any]) -> None:
        self._pending.append(record)
        if len(self._pending) >= self.every:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            for record in self._pending:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.written += len(self._pending)
        self._pending.clear()

    def discard(self) -> None:
        """Drop the partial file once the finished output has been written."""
        self._pending.clear()
        self.path.unlink(missing_ok=True)
