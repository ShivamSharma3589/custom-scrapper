"""HTTP API over the scraper.

    uvicorn api:app --host 0.0.0.0 --port 8000

One POST starts anything the command line can do; the GETs read what
happened. A scrape takes between one and forty minutes, far longer than an
HTTP request survives, so `POST /scrape` returns a `run_id` immediately and
the work continues in a background process.

Asking for a retailer that is already running returns 409 with the run that
holds it, rather than starting a second crawl -- two browsers on one shop get
both of them throttled.

    POST /scrape                {"retailer": "boots", "brands": ["Clinique"]}
    GET  /runs/{run_id}         status and counts
    GET  /runs/{run_id}/products
    GET  /runs/{run_id}/campaigns
    GET  /runs/{run_id}/log
    GET  /runs                  every run, newest first
    GET  /retailers             the adapters and what each supports
    POST /compare               cross-retailer price comparison
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))

from retailscraper.adapters.base import available_adapters, get_adapter  # noqa: E402
from retailscraper.runs import retailer_folder_name  # noqa: E402

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


class CompareRequest(BaseModel):
    retailers: List[str] = Field(
        default_factory=list,
        description="Compare the newest run of each. Empty means every retailer.",
    )
    threshold: float = 0.85


# --- reading what the scraper wrote ---------------------------------------

def _run_folders() -> List[Path]:
    """Every run folder, newest first.

    A run folder is `<retailer>/YYYY/MM/DD/HH-MM-SS/`, five levels below
    output/, and always holds a manifest.
    """
    folders = []
    for manifest in OUTPUT.rglob("manifest.json"):
        parts = manifest.parent.relative_to(OUTPUT).parts
        if len(parts) == 5:
            folders.append(manifest.parent)
    return sorted(folders, key=lambda p: p.parts[-4:], reverse=True)


def _manifest(folder: Path) -> Dict[str, Any]:
    try:
        return json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _find_run(run_id: str) -> Path:
    for folder in _run_folders():
        if _manifest(folder).get("run_id") == run_id:
            return folder
    raise HTTPException(404, f"no run with id {run_id!r}")


def _records(folder: Path, key: str) -> List[Dict[str, Any]]:
    """Gather `products` or `campaigns` from a run's per-brand files."""
    seen: Dict[str, Dict[str, Any]] = {}
    rows: List[Dict[str, Any]] = []
    for path in sorted(folder.glob("*.json")):
        if path.name == "manifest.json":
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
    """The lock holder for this retailer, or None.

    `run.py` takes a lock file per retailer for the length of a run, so this
    is the same signal the scraper itself uses -- no separate bookkeeping to
    drift out of step with reality.
    """
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
    """Start a run and return immediately.

    202 Accepted, not 200: the work has been accepted, not finished. Poll
    `GET /runs/{run_id}` for the result.
    """
    try:
        adapter = get_adapter(request.retailer)
    except KeyError as exc:
        raise HTTPException(400, str(exc)) from exc

    if not request.brands and not request.campaigns_only:
        raise HTTPException(400, "give brands, or set campaigns_only")

    # One crawl per retailer. A second would share the rate limit and both
    # would come back short, so it is refused rather than queued.
    key = retailer_folder_name(adapter)
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

    # Detached, so this request returns now and the crawl outlives it.
    process = subprocess.Popen(
        command, cwd=str(HERE),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    return {
        "status": "started",
        "retailer": adapter.display_name,
        "retailer_key": key,
        "brands": request.brands,
        "pid": process.pid,
        "poll": f"/runs/latest/{key}",
        "note": "the run_id appears once the run has created its folder",
    }


@app.get("/runs")
def list_runs(
    retailer: Optional[str] = Query(None, description="Filter by retailer key."),
    limit: int = Query(20, ge=1, le=200),
) -> List[Dict[str, Any]]:
    """Every run, newest first."""
    out = []
    for folder in _run_folders():
        manifest = _manifest(folder)
        if retailer and manifest.get("retailer_key") != retailer:
            continue
        out.append({
            "run_id": manifest.get("run_id"),
            "retailer": manifest.get("retailer"),
            "status": manifest.get("status"),
            "products": manifest.get("products"),
            "campaigns": manifest.get("campaigns"),
            "started_at": manifest.get("started_at"),
            "folder": str(folder.relative_to(OUTPUT)),
        })
        if len(out) >= limit:
            break
    return out


@app.get("/runs/latest/{retailer_key}")
def latest_run(retailer_key: str) -> Dict[str, Any]:
    """The most recent run of one retailer, and whether it is still going."""
    running = _is_running(retailer_key)
    for folder in _run_folders():
        manifest = _manifest(folder)
        if manifest.get("retailer_key") == retailer_key:
            return {**manifest, "running": running is not None}
    if running:
        return {"status": "running", "retailer_key": retailer_key,
                "held_by": running,
                "note": "started, but has not written its manifest yet"}
    raise HTTPException(404, f"no runs found for {retailer_key!r}")


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> Dict[str, Any]:
    """One run's manifest: status, counts, refusals, duration."""
    folder = _find_run(run_id)
    manifest = _manifest(folder)
    return {**manifest, "folder": str(folder.relative_to(OUTPUT))}


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
    """The last lines of run.log -- the first place to look at a failure."""
    path = _find_run(run_id) / "run.log"
    if not path.exists():
        raise HTTPException(404, "this run has no log")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-tail:])


@app.post("/compare")
def compare(request: CompareRequest) -> Dict[str, Any]:
    """Compare the newest run of each retailer.

    Runs in-process rather than as a subprocess: it reads files that already
    exist, so it finishes in seconds.
    """
    from retailscraper.matching import match_across_retailers

    wanted = {r.casefold() for r in request.retailers}
    products: List[Dict[str, Any]] = []
    used: List[str] = []
    seen_retailers = set()

    for folder in _run_folders():
        manifest = _manifest(folder)
        key = manifest.get("retailer_key")
        if not key or key in seen_retailers:
            continue
        if wanted and key.casefold() not in wanted:
            continue
        if manifest.get("status") != "ok":
            continue
        seen_retailers.add(key)
        rows = _records(folder, "products")
        if rows:
            products.extend(rows)
            used.append(manifest.get("retailer", key))

    if len(used) < 2:
        raise HTTPException(400, {
            "error": "need successful runs from at least two retailers",
            "found": used,
        })

    matches, unmatched = match_across_retailers(
        products, threshold=request.threshold
    )
    return {
        "retailers": used,
        "threshold": request.threshold,
        "matched": len(matches),
        "single_retailer_products": len(unmatched),
        "products": [m.to_dict() for m in matches],
    }


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
        "runs_on_disk": len(_run_folders()),
        "running": {k: v for k, v in running.items() if v},
    }
