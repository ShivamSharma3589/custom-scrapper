"""Settings read from the .env file at the repository root."""

import os
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

RETAILERS = [r.strip().casefold() for r in os.getenv("RETAILERS", "").split(",") if r.strip()]

OUTPUT_FORMATS = [f.strip().casefold()
                  for f in os.getenv("OUTPUT_FORMATS", "csv").split(",") if f.strip()]

PROXY_USERNAME = os.getenv("WEBSHARE_USERNAME", "")
PROXY_PASSWORD = os.getenv("WEBSHARE_PASSWORD", "")

PROXY_ADDRESSES = [a.strip() for a in os.getenv("WEBSHARE_PROXIES", "").split(",") if a.strip()]

PROXY_ENDPOINT = f"{os.getenv('WEBSHARE_HOST', '').strip()}:{os.getenv('WEBSHARE_PORT', '').strip()}"
PROXY_COUNTRY = os.getenv("WEBSHARE_COUNTRY", "").strip().lower()

# Browser settings for locale and timezone.
BROWSER_LOCALE = os.getenv("BROWSER_LOCALE", "").strip() or "en-GB"
BROWSER_TIMEZONE = os.getenv("BROWSER_TIMEZONE", "").strip() or "Europe/London"


def backbone_url(session: int) -> str:
    """The proxy URL for one Webshare session. The exit IP is read from the
    username, so session 7 is a different British home from session 6."""
    name = "-".join(p for p in (PROXY_USERNAME, PROXY_COUNTRY) if p)
    return (f"http://{quote(f'{name}-{session}', safe='-')}:"
            f"{quote(PROXY_PASSWORD, safe='')}@{PROXY_ENDPOINT}")


#: True when sessions are minted on demand instead of listed in WEBSHARE_PROXIES.
BACKBONE = bool(PROXY_USERNAME and PROXY_PASSWORD and PROXY_ENDPOINT
                and not PROXY_ADDRESSES)

PROXY_URLS = [
    f"http://{quote(PROXY_USERNAME, safe='')}:{quote(PROXY_PASSWORD, safe='')}@{address}"
    if PROXY_USERNAME else f"http://{address}"
    for address in PROXY_ADDRESSES
] or ([backbone_url(1)] if BACKBONE else [])

NO_PROXIES = (
    "no proxies configured. Every request must go through Webshare, never this "
    "machine's own IP. Set WEBSHARE_USERNAME and WEBSHARE_PASSWORD in .env "
    "-- see .env.example."
)


#: Set ALLOW_DIRECT=true in .env to scrape from this machine's own IP.
ALLOW_DIRECT = os.getenv("ALLOW_DIRECT", "").strip().lower() in {"1", "true", "yes"}

if ALLOW_DIRECT:
    PROXY_URLS = []
    BACKBONE = False


def first_proxy():
    """The proxy for a one-off fetch, or None when running direct on purpose."""
    if PROXY_URLS:
        return PROXY_URLS[0]
    if ALLOW_DIRECT:
        return None
    raise RuntimeError(NO_PROXIES)


GCP_PROJECT = os.getenv("GCP_PROJECT", "").strip()
GCS_BUCKET = os.getenv("GCS_BUCKET", "").strip()
BQ_DATASET = os.getenv("BQ_DATASET", "").strip()
BQ_TABLE_RUNS = os.getenv("BQ_TABLE_RUNS", "").strip() or "runs"
BQ_TABLE_PRODUCTS = os.getenv("BQ_TABLE_PRODUCTS", "").strip() or "products"
BQ_TABLE_CAMPAIGNS = os.getenv("BQ_TABLE_CAMPAIGNS", "").strip() or "campaigns"
BQ_TABLE_MAPPING = os.getenv("BQ_TABLE_MAPPING", "").strip() or "campaigns_products_mapping"

# Delete a local file once Cloud Storage confirms it holds a copy.
DELETE_LOCAL_AFTER_UPLOAD = os.getenv(
    "DELETE_LOCAL_AFTER_UPLOAD", "true").strip().lower() in {"1", "true", "yes"}


def publishing_enabled() -> bool:
    """True when all three Google Cloud settings are filled in."""
    return bool(GCP_PROJECT and GCS_BUCKET and BQ_DATASET)
