"""Polite HTTP helpers — UA rotation, 2-5s jitter, exponential backoff.

Contract from docs/prd.md v1.A.3:
- randomized realistic User-Agent per request
- 2-5s jittered delay before every request
- exponential backoff on 429 / 502 / 503 / 504 and network errors
- never crash the pipeline on a single bad page (caller checks for None)

Also provides a `cffi_fetcher` context manager — a curl_cffi-backed session
with byte-identical real-Chrome TLS handshake (defeats Akamai/Cloudflare TLS
fingerprinting), plus cookie injection.
"""

from __future__ import annotations

import logging
import random
import time
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

import httpx

log = logging.getLogger(__name__)

_USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:128.0) Gecko/20100101 Firefox/128.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
]

RETRY_STATUS = {429, 502, 503, 504}


def random_ua() -> str:
    return random.choice(_USER_AGENTS)


def jitter_sleep(low: float = 2.0, high: float = 5.0) -> None:
    time.sleep(random.uniform(low, high))


def build_client(timeout: float = 30.0) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        },
    )


def polite_get(
    client: httpx.Client,
    url: str,
    *,
    max_retries: int = 4,
    extra_headers: Optional[dict] = None,
) -> Optional[httpx.Response]:
    """GET with jitter, UA rotation, exponential backoff. Returns the final
    Response (which may still be an error) or None if all attempts raised."""
    headers = {"User-Agent": random_ua()}
    if extra_headers:
        headers.update(extra_headers)

    last_resp: Optional[httpx.Response] = None
    for attempt in range(max_retries):
        jitter_sleep()
        try:
            last_resp = client.get(url, headers=headers)
        except httpx.RequestError as e:
            log.warning("GET %s failed (attempt %d/%d): %r", url, attempt + 1, max_retries, e)
            if attempt == max_retries - 1:
                return None
            time.sleep(1.5 ** (attempt + 1) + random.uniform(0, 1))
            continue

        if last_resp.status_code in RETRY_STATUS:
            wait = 1.5 ** (attempt + 1) + random.uniform(0, 1)
            log.warning(
                "GET %s -> %d (attempt %d/%d); sleeping %.1fs",
                url, last_resp.status_code, attempt + 1, max_retries, wait,
            )
            time.sleep(wait)
            continue

        return last_resp

    return last_resp


@contextmanager
def cffi_fetcher(
    *,
    cookies: Optional[list[dict]] = None,
    impersonate: str = "chrome131",
) -> Iterator[Callable[[str], Optional[str]]]:
    """Yield a `fetch(url) -> str | None` callable backed by curl_cffi.

    curl_cffi reproduces real Chrome's TLS ClientHello (cipher order, extensions,
    ALPN, GREASE) byte-for-byte, defeating JA3/JA4-based bot detection used by
    Akamai / Cloudflare / Datadome.

    `cookies` is the Playwright-shaped list returned by `cookies.load_cookies_for()`.
    We extract (name, value, domain) for each and load into the cffi session, so
    the same session token your browser uses gets sent on every request.
    """
    from curl_cffi import requests as cffi_requests

    session = cffi_requests.Session(impersonate=impersonate)
    if cookies:
        loaded = 0
        for c in cookies:
            try:
                session.cookies.set(c["name"], c["value"], domain=c.get("domain"))
                loaded += 1
            except Exception:  # noqa: BLE001
                pass
        log.info("cffi: loaded %d/%d cookies into session", loaded, len(cookies))

    def fetch(url: str) -> Optional[str]:
        jitter_sleep()
        try:
            resp = session.get(url, allow_redirects=True, timeout=30)
        except Exception as e:  # noqa: BLE001
            log.warning("cffi GET %s failed: %r", url, e)
            return None
        if resp.status_code != 200:
            log.warning("cffi GET %s -> %d", url, resp.status_code)
            return None
        return resp.text

    try:
        yield fetch
    finally:
        try:
            session.close()
        except Exception:  # noqa: BLE001
            pass
