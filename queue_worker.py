"""A one-at-a-time queue for scrape jobs."""

import subprocess
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent

_pending: deque = deque()

_current: Optional[Dict[str, Any]] = None

_finished: deque = deque(maxlen=100)

_lock = threading.Lock()

_worker: Optional[threading.Thread] = None

_process: Optional[subprocess.Popen] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def submit(retailer_key: str, display_name: str, command: List[str]) -> Dict[str, Any]:
    """Add a job, starting the worker if it is idle. Says where it sits."""
    global _worker

    job = {
        "job_id": uuid.uuid4().hex[:12],
        "retailer_key": retailer_key,
        "retailer": display_name,
        "command": command,
        "status": "queued",
        "queued_at": _now(),
        "started_at": None,
        "finished_at": None,
        "exit_code": None,
    }

    with _lock:
        _pending.append(job)
        position = len(_pending)
        running = _current is not None
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run_queue, daemon=True)
            _worker.start()

    return {**_summary(job), "position": position, "will_start_now": not running}


def queued_for(retailer_key: str) -> Optional[Dict[str, Any]]:
    """The queued or running job for this retailer, if there is one."""
    with _lock:
        if _current and _current["retailer_key"] == retailer_key:
            return _summary(_current)
        for job in _pending:
            if job["retailer_key"] == retailer_key:
                return _summary(job)
    return None


def state() -> Dict[str, Any]:
    """Everything the queue knows, for GET /queue."""
    with _lock:
        return {
            "running": _summary(_current) if _current else None,
            "queued": [_summary(j) for j in _pending],
            "recent": [_summary(j) for j in list(_finished)[::-1][:20]],
        }


def cancel(job_id: str) -> bool:
    """Remove a job that has not started yet."""
    with _lock:
        for job in list(_pending):
            if job["job_id"] == job_id:
                _pending.remove(job)
                job["status"] = "cancelled"
                job["finished_at"] = _now()
                _finished.append(job)
                return True
    return False


def stop_running(clear_queue: bool = False) -> Optional[Dict[str, Any]]:
    """Stop the crawl in progress, and optionally drop what is waiting."""
    with _lock:
        job = _current
        process = _process
        if clear_queue:
            while _pending:
                dropped = _pending.popleft()
                dropped["status"] = "cancelled"
                dropped["finished_at"] = _now()
                _finished.append(dropped)

    if job is None or process is None:
        return None

    process.terminate()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)

    (HERE / "output" / f".{job['retailer_key']}.lock").unlink(missing_ok=True)

    with _lock:
        job["status"] = "stopped"
    return _summary(job)


def _summary(job: Dict[str, Any]) -> Dict[str, Any]:
    """A job without its command line, which is noise to the caller."""
    return {k: v for k, v in job.items() if k != "command"}


def _run_queue() -> None:
    """Run queued jobs one at a time, then exit. submit() starts it again."""
    global _current, _process

    while True:
        with _lock:
            if not _pending:
                _current = None
                return
            _current = _pending.popleft()
            _current["status"] = "running"
            _current["started_at"] = _now()
            command = _current["command"]

        try:
            process = subprocess.Popen(
                command, cwd=str(HERE),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            with _lock:
                _process = process
            code = process.wait()
        except Exception as exc:  # pragma: no cover - a child that would not start
            code = 2
            with _lock:
                _current["error"] = str(exc)

        with _lock:
            _current["exit_code"] = code
            _current["status"] = {
                0: "ok", 1: "failed", 2: "failed", 3: "skipped",
            }.get(code, "failed")
            _current["finished_at"] = _now()
            _finished.append(_current)
            _current = None
            _process = None
