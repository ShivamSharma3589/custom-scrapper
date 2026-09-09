"""HTTP API over the scraper.

    uvicorn api:app --host 0.0.0.0 --port 8000

A scrape takes 1-40 minutes, longer than an HTTP request lives, so POST
/scrape queues the job and hands back an id. The GETs read the results.
Asking twice for the same retailer gets a 409, not a second crawl.

    POST /scrape                {"retailer": "boots", "brands": ["Clinique"]}
    GET  /runs/{run_id}         status and counts
    GET  /runs/{run_id}/products
    GET  /runs/{run_id}/campaigns
    GET  /runs/{run_id}/log
    GET  /runs                  every run, newest first
    GET  /retailers             the adapters and what each supports
"""

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))

from retailscraper.adapters.base import available_adapters, get_adapter  # noqa: E402
from retailscraper.runs import retailer_folder_name  # noqa: E402
import queue_worker  # noqa: E402

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "output"

app = FastAPI(
    title="Retail promotion scraper",
    description="Track competitor pricing and promotions for a set of brands.",
    version="1.0",
)


class ScrapeRequest(BaseModel):
    """The command-line options, as JSON."""

    retailer: str = Field(..., description="Adapter name, domain or URL.")
    brands: List[str] = Field(default_factory=list)
    campaigns_only: bool = False
    categories: List[str] = Field(default_factory=list)
    max_products: Optional[int] = Field(
        None, description="Cap per brand. Omit for a full run."
    )
    strict_brand: bool = False
    resolve_categories: bool = False
    offer_keywords: Optional[str] = Field(
        None, description="Path to a keyword file that REPLACES the built-in rules."
    )
    refusal_limit: Optional[float] = Field(
        None, description="Share of refused requests above which the run fails."
    )


# --- reading what the scraper wrote ---------------------------------------

def _runs() -> List[Path]:
    """Every run's manifest file, newest first.

    A manifest lives at `<retailer>/manifest/<timestamp>.json`, so the list of
    manifests is the list of runs, and the filename is the run's timestamp.
    """
    return sorted(OUTPUT.glob("*/manifest/*.json"),
                  key=lambda p: p.stem, reverse=True)


def _manifest(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _find_run(run_id: str) -> Path:
    for path in _runs():
        if _manifest(path).get("run_id") == run_id:
            return path
    raise HTTPException(404, f"no run with id {run_id!r}")


def _records(manifest_path: Path, key: str) -> List[Dict[str, Any]]:
    """Gather `products`, `campaigns` or `rejected` from one run's files.

    A run's files are the ones sharing its timestamp, spread across the
    retailer's brand folders.
    """
    root = manifest_path.parent.parent
    stamp = manifest_path.stem

    seen: Dict[str, Dict[str, Any]] = {}
    rows: List[Dict[str, Any]] = []
    for path in sorted(root.glob(f"*/{stamp}.json")):
        if path.parent.name == "manifest":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for row in payload.get(key) or []:
            if key == "campaigns":
                # Campaigns repeat in every brand's file; keep one of each.
                seen.setdefault(row.get("campaign_id"), row)
            else:
                rows.append(row)
    return list(seen.values()) if key == "campaigns" else rows


def _is_running(retailer_key: str) -> Optional[str]:
    """Who holds this retailer's lock, or None. Same file run.py writes."""
    lock = OUTPUT / f".{retailer_key}.lock"
    if not lock.exists():
        return None
    try:
        return lock.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


# --- endpoints -------------------------------------------------------------

@app.get("/retailers")
def list_retailers() -> List[Dict[str, Any]]:
    """The adapters, and what each one supports."""
    out = []
    for name in available_adapters():
        adapter = get_adapter(name)
        key = retailer_folder_name(adapter)
        out.append({
            "name": name,
            "display_name": adapter.display_name,
            "domain": adapter.domain,
            "supports_categories": adapter.supports_categories,
            "confirms_brand_stocking": adapter.confirms_brand_stocking,
            "has_campaign_hubs": bool(adapter.campaign_discovery_urls()),
            "running": _is_running(key) is not None,
        })
    return out


@app.post("/scrape", status_code=202)
def start_scrape(request: ScrapeRequest) -> Dict[str, Any]:
    """Queue a run and return straight away. 202 means accepted, not done."""
    try:
        adapter = get_adapter(request.retailer)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc

    if not request.brands and not request.campaigns_only:
        raise HTTPException(400, "give brands, or set campaigns_only")

    key = retailer_folder_name(adapter)

    # Asking twice for the same retailer is a duplicate, not a second crawl.
    existing = queue_worker.queued_for(key)
    if existing:
        raise HTTPException(409, {
            "error": f"{adapter.display_name} is already {existing['status']}",
            "job": existing,
        })
    holder = _is_running(key)
    if holder:
        raise HTTPException(409, {
            "error": f"{adapter.display_name} is already being scraped",
            "held_by": holder,
        })

    command = [sys.executable, str(HERE / "run.py"),
               "--retailer", request.retailer,
               "--out-dir", str(OUTPUT)]
    if request.brands:
        command += ["--brands", *request.brands]
    if request.campaigns_only:
        command.append("--campaigns-only")
    if request.categories:
        command += ["--categories", *request.categories]
    if request.max_products:
        command += ["--max-products", str(request.max_products)]
    if request.strict_brand:
        command.append("--strict-brand")
    if request.resolve_categories:
        command.append("--resolve-categories")
    if request.offer_keywords:
        command += ["--offer-keywords", request.offer_keywords]
    if request.refusal_limit is not None:
        command += ["--refusal-limit", str(request.refusal_limit)]

    # Queued, not started: two browser crawls at once slow each other down.
    job = queue_worker.submit(key, adapter.display_name, command)
    return {
        **job,
        "brands": request.brands,
        "poll": f"/runs/latest/{key}",
    }


@app.get("/runs")
def list_runs(
    retailer: Optional[str] = Query(None, description="Filter by retailer key."),
    limit: int = Query(20, ge=1, le=200),
) -> List[Dict[str, Any]]:
    """Every run, newest first."""
    out = []
    for path in _runs():
        manifest = _manifest(path)
        if retailer and manifest.get("retailer_key") != retailer:
            continue
        out.append({
            "run_id": manifest.get("run_id"),
            "retailer": manifest.get("retailer"),
            "status": manifest.get("status"),
            "products": manifest.get("products"),
            "campaigns": manifest.get("campaigns"),
            "started_at": manifest.get("started_at"),
            "timestamp": path.stem,
        })
        if len(out) >= limit:
            break
    return out


@app.get("/runs/latest/{retailer_key}")
def latest_run(retailer_key: str) -> Dict[str, Any]:
    """The most recent run of one retailer, and whether it is still going."""
    running = _is_running(retailer_key)
    for path in _runs():
        manifest = _manifest(path)
        if manifest.get("retailer_key") == retailer_key:
            return {**manifest, "timestamp": path.stem,
                    "running": running is not None}
    if running:
        return {"status": "running", "retailer_key": retailer_key,
                "held_by": running,
                "note": "started, but has not written its manifest yet"}
    raise HTTPException(404, f"no runs found for {retailer_key!r}")


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> Dict[str, Any]:
    """One run's manifest: status, counts, refusals, duration."""
    path = _find_run(run_id)
    return {**_manifest(path), "timestamp": path.stem}


@app.get("/runs/{run_id}/products")
def get_products(
    run_id: str,
    brand: Optional[str] = Query(None, description="Filter to one brand."),
) -> Dict[str, Any]:
    rows = _records(_find_run(run_id), "products")
    if brand:
        wanted = brand.casefold()
        rows = [r for r in rows
                if (r.get("brand_matched_to") or "").casefold() == wanted]
    return {"run_id": run_id, "count": len(rows), "products": rows}


@app.get("/runs/{run_id}/campaigns")
def get_campaigns(run_id: str) -> Dict[str, Any]:
    rows = _records(_find_run(run_id), "campaigns")
    return {"run_id": run_id, "count": len(rows), "campaigns": rows}


@app.get("/runs/{run_id}/rejected")
def get_rejected(run_id: str) -> Dict[str, Any]:
    """What the run refused to publish, and why."""
    rows = _records(_find_run(run_id), "rejected")
    return {"run_id": run_id, "count": len(rows), "rejected": rows}


@app.get("/runs/{run_id}/log", response_class=PlainTextResponse)
def get_log(run_id: str, tail: int = Query(200, ge=1, le=10000)) -> str:
    """The last lines of this run's log -- the first place to look at a failure."""
    manifest_path = _find_run(run_id)
    path = (manifest_path.parent.parent / "logs" / f"{manifest_path.stem}.log")
    if not path.exists():
        raise HTTPException(404, "this run has no log")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-tail:])


@app.get("/health")
def health() -> Dict[str, Any]:
    running = {
        retailer_folder_name(get_adapter(n)): _is_running(
            retailer_folder_name(get_adapter(n))
        )
        for n in available_adapters()
    }
    return {
        "ok": True,
        "output_dir": str(OUTPUT),
        "runs_on_disk": len(_runs()),
        "running": {k: v for k, v in running.items() if v},
    }


# --- the queue -------------------------------------------------------------

@app.get("/queue")
def get_queue() -> Dict[str, Any]:
    """What is running, what is waiting, and what finished recently."""
    return queue_worker.state()


@app.delete("/queue/{job_id}")
def cancel_job(job_id: str) -> Dict[str, Any]:
    """Drop a job that has not started yet."""
    if not queue_worker.cancel(job_id):
        raise HTTPException(404, f"no queued job {job_id!r} -- it may have started")
    return {"cancelled": job_id}


@app.post("/queue/stop")
def stop_current(clear_queue: bool = Query(
    False, description="Also drop everything still waiting.")
) -> Dict[str, Any]:
    """Stop the crawl in progress.

    Whatever it already wrote to disk stays. There is no manifest, which is
    how you tell the run did not finish.
    """
    stopped = queue_worker.stop_running(clear_queue=clear_queue)
    if stopped is None:
        return {"stopped": None, "note": "nothing was running"}
    return {"stopped": stopped, "queue_cleared": clear_queue}
