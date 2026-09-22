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


def first_proxy() -> str:
    """The proxy for a one-off fetch made outside the crawl."""
    if not PROXY_URLS:
        raise RuntimeError(NO_PROXIES)
    return PROXY_URLS[0]
