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
