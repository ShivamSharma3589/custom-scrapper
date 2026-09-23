"""Settings read from the .env file beside this one."""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

PROXY_USERNAME = os.getenv("WEBSHARE_USERNAME", "")
PROXY_PASSWORD = os.getenv("WEBSHARE_PASSWORD", "")

PROXY_ADDRESSES = [a.strip() for a in os.getenv("WEBSHARE_PROXIES", "").split(",") if a.strip()]

PROXY_URLS = [
    f"http://{PROXY_USERNAME}:{PROXY_PASSWORD}@{address}" if PROXY_USERNAME else f"http://{address}"
    for address in PROXY_ADDRESSES
]

NO_PROXIES = (
    "no proxies configured. Every request must go through Webshare, never this "
    "machine's own IP. Set WEBSHARE_USERNAME, WEBSHARE_PASSWORD and "
    "WEBSHARE_PROXIES in .env -- see .env.example."
)


#: Set ALLOW_DIRECT=true in .env to scrape from this machine's own IP.
ALLOW_DIRECT = os.getenv("ALLOW_DIRECT", "").strip().lower() in {"1", "true", "yes"}

if ALLOW_DIRECT:
    PROXY_URLS = []


def first_proxy():
    """The proxy for a one-off fetch, or None when running direct on purpose."""
    if PROXY_URLS:
        return PROXY_URLS[0]
    if ALLOW_DIRECT:
        return None
    raise RuntimeError(NO_PROXIES)


#: Google Cloud. Leave these empty on a laptop and nothing is uploaded; fill
#: them in production and every successful run is archived and loaded.
GCP_PROJECT = os.getenv("GCP_PROJECT", "").strip()
GCS_BUCKET = os.getenv("GCS_BUCKET", "").strip()
BQ_DATASET = os.getenv("BQ_DATASET", "").strip()

#: Delete a local file once Cloud Storage confirms it holds a copy.
DELETE_LOCAL_AFTER_UPLOAD = os.getenv(
    "DELETE_LOCAL_AFTER_UPLOAD", "true").strip().lower() in {"1", "true", "yes"}


def publishing_enabled() -> bool:
    """True when all three Google Cloud settings are filled in."""
    return bool(GCP_PROJECT and GCS_BUCKET and BQ_DATASET)
