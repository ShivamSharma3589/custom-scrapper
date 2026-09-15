"""Settings read from the .env file beside this one.

Secrets live in .env, which git ignores. The code reads them only from here:

    from config import PROXY_URLS

.env.example lists what .env needs.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

#: Webshare account the proxies log in with.
PROXY_USERNAME = os.getenv("WEBSHARE_USERNAME", "")
PROXY_PASSWORD = os.getenv("WEBSHARE_PASSWORD", "")

#: Each proxy as host:port, in the order they are tried.
PROXY_ADDRESSES = [a.strip() for a in os.getenv("WEBSHARE_PROXIES", "").split(",") if a.strip()]

#: The proxies ready to use: http://username:password@host:port
PROXY_URLS = [
    f"http://{PROXY_USERNAME}:{PROXY_PASSWORD}@{address}" if PROXY_USERNAME else f"http://{address}"
    for address in PROXY_ADDRESSES
]
