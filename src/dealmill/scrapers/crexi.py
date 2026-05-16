"""Crexi scraper (v1.F — commercial real estate marketplace).

Crexi (www.crexi.com) is a CRE listings platform comparable to LoopNet but
typically less aggressively bot-protected. Default browser is `cffi` (real
Chrome TLS + cookies) for consistency with the other CRE source; switch to
`playwright` if SPA rendering is needed for detail pages.

Discovery walks `https://www.crexi.com/properties/for-sale`. Detail pages
are at `https://www.crexi.com/properties/<id>/<slug>`. Best-effort parser
prefers LD+JSON (RealEstateListing / Product), falls back to og:meta + CSS.
"""

from __future__ import annotations

import json
import logging
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from dealmill import db
from dealmill.cookies import add_cookies_resilient, load_cookies_for
from dealmill.env import (
    category_matches,
    max_asking_price_cents,
    price_matches,
    target_categories,
)
from dealmill.polite_http import build_client, cffi_fetcher, jitter_sleep, polite_get, random_ua

log = logging.getLogger(__name__)

BASE = "https://www.crexi.com"
# `/properties` is the live for-sale browse index (auto-redirects from
# /properties/for-sale on current Crexi).
INDEX = f"{BASE}/properties"
SOURCE = "crexi"

# Crexi listing URL shape (best guess; refine when we see real HTML):
#   /properties/<id>/<slug>  or  /properties/<id>
_DETAIL_HREF_RE = re.compile(r"^/properties/\d+(?:/[^/]+)?/?$")

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEBUG_DIR = _PROJECT_ROOT / "data" / "debug"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_urls(fetch_html, max_pages: int = 5) -> Iterator[str]:
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        url = INDEX if page == 1 else f"{INDEX}?page={page}"
        html = fetch_html(url)
        if not html:
            log.warning("Crexi index page %d returned no html; stopping", page)
            return
        page_urls = list(_extract_detail_urls(html))
        if not page_urls:
            _debug_dump_index(page, html)
            log.info("Crexi index page %d yielded no detail URLs; stopping", page)
            return
        for href in page_urls:
            full = href if href.startswith("http") else urljoin(BASE, href)
            if full in seen:
                continue
            seen.add(full)
            yield full


def _extract_detail_urls(html: str) -> Iterator[str]:
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # Strip the host if absolute, then test against the path-only regex.
        if href.startswith("http"):
            if "crexi.com" not in href:
                continue
            href = href.split("crexi.com", 1)[1] or "/"
        if _DETAIL_HREF_RE.match(href):
            yield href


def _debug_dump_index(page: int, html: str) -> None:
    _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    p = _DEBUG_DIR / f"crexi_index_p{page}_{int(time.time())}.html"
    p.write_text(html, encoding="utf-8")
    log.info("dumped Crexi index html (%d bytes) -> %s", len(html), p)
    lowered = html.lower()
    for marker in ("access denied", "akam", "captcha", "cf-challenge", "checking your browser"):
        if marker in lowered:
            log.warning("Crexi html contains '%s' marker — likely anti-bot block", marker)


# ---------------------------------------------------------------------------
# Detail parsing
# ---------------------------------------------------------------------------

def parse_detail(url: str, html: str) -> dict:
    """Crexi exposes its useful data via meta tags, NOT LD+JSON:

      <meta property="og:title" content="<address> | Crexi.com">
      <meta property="og:description" content="<PROPERTY_TYPE> property for sale at <ADDRESS>. ...">

    Many listings show "Contact for Price" with no $-amount in the rendered
    HTML, so asking_price_cents stays NULL for those (correctly).
    """
    soup = BeautifulSoup(html, "lxml")
    row: dict = {
        "source": SOURCE,
        "url": url,
        "raw_html": html.encode("utf-8") if html else None,
        # Crexi listings are broker-dominated.
        "is_broker": True,
    }

    # --- Title from og:title; strip "| Crexi.com" suffix
    og_title = soup.find("meta", {"property": "og:title"})
    if og_title and og_title.get("content"):
        t = og_title["content"].strip()
        t = re.sub(r"\s*\|\s*Crexi\.com\s*$", "", t)
        row["title"] = t

    # --- Category + full address from meta description
    desc_meta = (
        soup.find("meta", {"property": "og:description"})
        or soup.find("meta", {"name": "description"})
    )
    if desc_meta and desc_meta.get("content"):
        full_desc = desc_meta["content"].strip()
        row["description"] = full_desc
        # Pattern: "<Property Type> property for sale at <STREET, CITY, ST ZIP>."
        m = re.search(r"^(.+?)\s+property for sale at\s+(.+?)(?:\.\s|$)", full_desc)
        if m:
            prop_type = m.group(1).strip()
            address = m.group(2).strip()
            row["category"] = f"commercial real estate / {prop_type}"
            row["location_raw"] = address
            # "STREET, CITY, ST ZIP" or "STREET, CITY, ST"
            addr_m = re.search(r",\s*([^,]+?),\s*([A-Z]{2})(?:\s+\d{5}.*)?$", address)
            if addr_m:
                row["location_city"] = addr_m.group(1).strip()
                row["location_state"] = addr_m.group(2).strip()

    if "category" not in row:
        row["category"] = "commercial real estate"

    # --- Asking price: Crexi wraps the listing's headline price in
    # `class="cui-tooltip-target"`. The "similar properties" sidebar uses
    # different classes (`ctw:text-heading-6 ctw:truncate`), so this
    # selector distinguishes THIS listing's price from siblings.
    # If a Crexi listing is "Contact for Price", no cui-tooltip-target with
    # a $-amount exists, and asking_price_cents stays NULL — correct.
    for el in soup.find_all(class_="cui-tooltip-target"):
        text = el.get_text(" ", strip=True)
        cents = _first_dollar_amount_to_cents(text)
        if cents is not None and cents >= 1_000_00:  # $1,000 floor; ignores per-sf rates
            row["asking_price_cents"] = cents
            break

    return row


def _find_postal_address(obj) -> Optional[dict]:
    if isinstance(obj, dict):
        if obj.get("@type") == "PostalAddress":
            return obj
        for v in obj.values():
            r = _find_postal_address(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_postal_address(v)
            if r:
                return r
    return None


_NON_DIGIT_RE = re.compile(r"[^\d.]")
_DOLLAR_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")


def _to_cents(price) -> Optional[int]:
    if price is None:
        return None
    if isinstance(price, (int, float)):
        return int(round(float(price) * 100))
    s = _NON_DIGIT_RE.sub("", str(price))
    if not s:
        return None
    try:
        return int(round(float(s) * 100))
    except ValueError:
        return None


def _first_dollar_amount_to_cents(text: str) -> Optional[int]:
    m = _DOLLAR_RE.search(text)
    if not m:
        return None
    try:
        return int(round(float(m.group(1).replace(",", "")) * 100))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Playwright fallback (when SPA rendering matters)
# ---------------------------------------------------------------------------

@contextmanager
def _playwright_fetcher(*, cookies: Optional[list[dict]] = None):
    from playwright.sync_api import sync_playwright
    from playwright_stealth import stealth_sync

    ua = random_ua()
    p = sync_playwright().start()
    browser = p.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    context = browser.new_context(
        user_agent=ua,
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
        timezone_id="America/Los_Angeles",
    )
    if cookies:
        add_cookies_resilient(context, cookies)

    def fetch(url: str) -> str:
        jitter_sleep()
        page = context.new_page()
        try:
            stealth_sync(page)
            # Crexi is an Angular SPA. networkidle alone is insufficient; the
            # listing-fetch XHR may not have fired yet. Wait for at least one
            # anchor inside the property-items wrapper to materialize, then
            # scroll once to trigger any lazy-load.
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector(
                    ".property-items-list-table-view a, .property-items-wrapper a, "
                    "a[href*='/properties/'][href*='/']",
                    timeout=20000,
                    state="attached",
                )
            except Exception:
                pass  # fall through; we'll dump what we have for debugging
            page.evaluate("window.scrollBy(0, 1500)")
            page.wait_for_timeout(3000)
            return page.content()
        finally:
            page.close()

    try:
        yield fetch
    finally:
        context.close()
        browser.close()
        p.stop()


# ---------------------------------------------------------------------------
# Top-level run
# ---------------------------------------------------------------------------

def run(
    *,
    limit: Optional[int] = None,
    max_pages: int = 5,
    browser: str = "cffi",
) -> dict:
    if browser not in ("httpx", "playwright", "cffi"):
        raise ValueError(f"browser must be 'httpx' | 'playwright' | 'cffi', got {browser!r}")

    # Crexi is a JS SPA — listing anchors only exist after Angular hydrates.
    # httpx and cffi will see the empty wrapper containers and return nothing.
    if browser in ("httpx", "cffi"):
        log.warning(
            "Crexi is a JS SPA (Angular); %s sees only the unrendered shell. "
            "Forcing browser=playwright for this source.",
            browser,
        )
        browser = "playwright"

    cats = target_categories()
    max_cents = max_asking_price_cents()

    stats = dict(discovered=0, fetched=0, inserted=0, updated=0, skipped_filter=0, failed=0)

    client: Optional[httpx.Client] = None
    pw_cm = None
    pw_fetch = None
    cffi_cm = None

    if browser == "httpx":
        client = build_client()

        def fetch_html(url: str) -> Optional[str]:
            resp = polite_get(client, url)
            if not resp or resp.status_code != 200:
                status = resp.status_code if resp else "no-response"
                log.warning("GET %s -> %s", url, status)
                return None
            return resp.text
    elif browser == "playwright":
        cookies = load_cookies_for(SOURCE)
        pw_cm = _playwright_fetcher(cookies=cookies)
        pw_fetch = pw_cm.__enter__()

        def fetch_html(url: str) -> Optional[str]:
            try:
                return pw_fetch(url)
            except Exception as e:  # noqa: BLE001
                log.warning("playwright fetch %s failed: %r", url, e)
                return None
    else:  # cffi
        cookies = load_cookies_for(SOURCE)
        cffi_cm = cffi_fetcher(cookies=cookies)
        cffi_fetch = cffi_cm.__enter__()

        def fetch_html(url: str) -> Optional[str]:
            return cffi_fetch(url)

    try:
        with db.connect() as conn:
            for url in discover_urls(fetch_html, max_pages=max_pages):
                stats["discovered"] += 1
                html = fetch_html(url)
                if not html:
                    stats["failed"] += 1
                    continue
                stats["fetched"] += 1

                try:
                    row = parse_detail(url, html)
                except Exception as e:  # noqa: BLE001
                    log.warning("parse %s failed: %r; skipping", url, e)
                    stats["failed"] += 1
                    continue

                if not category_matches(row.get("category"), cats):
                    stats["skipped_filter"] += 1
                    continue
                if not price_matches(row.get("asking_price_cents"), max_cents):
                    stats["skipped_filter"] += 1
                    continue

                try:
                    action = db.upsert_listing(conn, row)
                except Exception as e:  # noqa: BLE001
                    log.warning("upsert %s failed: %r; skipping", url, e)
                    stats["failed"] += 1
                    continue

                if action == "inserted":
                    stats["inserted"] += 1
                else:
                    stats["updated"] += 1

                if limit is not None and (stats["inserted"] + stats["updated"]) >= limit:
                    log.info("hit --limit=%d; stopping", limit)
                    break
    finally:
        if client is not None:
            client.close()
        if pw_cm is not None:
            pw_cm.__exit__(None, None, None)
        if cffi_cm is not None:
            cffi_cm.__exit__(None, None, None)

    return stats
