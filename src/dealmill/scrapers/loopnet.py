"""LoopNet scraper scaffold (v1.C).

LoopNet is owned by CoStar and fronted by Akamai Bot Manager — same anti-bot
posture as BizBuySell. The architecture here is complete; live data
extraction is expected to be blocked until session cookies are seeded (see
project memory `bbs-blocked-by-akamai.md`). Parser selectors are best-effort
guesses against an inaccessible site and WILL need refinement when we can
inspect real HTML.

Discovery hits the national for-sale index; detail-page parser tries LD+JSON
`RealEstateListing` / `Place` / `Product` first, then CSS heuristics, then
meta tags.
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

BASE = "https://www.loopnet.com"
INDEX = f"{BASE}/search/commercial-real-estate/usa/for-sale/"
SOURCE = "loopnet"

# Best-effort guess at LoopNet's detail URL shape. Likely needs refinement
# once a real LoopNet page is observable.
_DETAIL_HREF_RE = re.compile(r"^https?://(?:www\.)?loopnet\.com/Listing/\d+/[^/]+/?$")

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
            log.warning("LoopNet index page %d returned no html; stopping", page)
            return
        page_urls = list(_extract_detail_urls(html))
        if not page_urls:
            _debug_dump_index(page, html)
            log.info("LoopNet index page %d yielded no detail URLs; stopping", page)
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
        if _DETAIL_HREF_RE.match(href if href.startswith("http") else urljoin(BASE, href)):
            yield href


def _debug_dump_index(page: int, html: str) -> None:
    _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    p = _DEBUG_DIR / f"loopnet_index_p{page}_{int(time.time())}.html"
    p.write_text(html, encoding="utf-8")
    log.info("dumped LoopNet index html (%d bytes) -> %s", len(html), p)
    lowered = html.lower()
    for marker in ("access denied", "akam", "captcha", "checking your browser"):
        if marker in lowered:
            log.warning("LoopNet html contains '%s' marker — likely anti-bot block", marker)


# ---------------------------------------------------------------------------
# Detail parsing
# ---------------------------------------------------------------------------

def parse_detail(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    row: dict = {
        "source": SOURCE,
        "url": url,
        "raw_html": html.encode("utf-8") if html else None,
        "category": "commercial real estate",
    }

    # LD+JSON (any of Product / Offer / RealEstateListing / Place)
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            type_str = str(obj.get("@type", "")).lower()
            if not any(t in type_str for t in ("product", "offer", "realestatelisting", "place")):
                continue
            if not row.get("title"):
                row["title"] = obj.get("name")
            if not row.get("description"):
                row["description"] = obj.get("description")

            offers = obj.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if isinstance(offers, dict):
                price = offers.get("price") or offers.get("highPrice") or offers.get("lowPrice")
                cents = _to_cents(price)
                if cents is not None and "asking_price_cents" not in row:
                    row["asking_price_cents"] = cents

            addr = _find_postal_address(obj)
            if addr:
                if not row.get("location_city") and addr.get("addressLocality"):
                    row["location_city"] = str(addr["addressLocality"]).strip()
                if not row.get("location_state") and addr.get("addressRegion"):
                    row["location_state"] = str(addr["addressRegion"]).strip().upper()

    # og: fallback
    if not row.get("title"):
        og = soup.find("meta", {"property": "og:title"})
        if og and og.get("content"):
            row["title"] = og["content"].strip()
    if not row.get("description"):
        og = soup.find("meta", {"property": "og:description"})
        if og and og.get("content"):
            row["description"] = og["content"].strip()

    # CSS title fallback
    if not row.get("title"):
        h1 = soup.find("h1")
        if h1:
            row["title"] = h1.get_text(strip=True)

    # Price CSS fallback (any element with class hinting price)
    if "asking_price_cents" not in row:
        for el in soup.find_all(class_=re.compile(r"price|asking", re.I)):
            cents = _first_dollar_amount_to_cents(el.get_text(" ", strip=True))
            if cents is not None:
                row["asking_price_cents"] = cents
                break

    if row.get("location_city") and row.get("location_state"):
        row["location_raw"] = f"{row['location_city']}, {row['location_state']}"

    # LoopNet is a broker-dominated marketplace; default unless we detect otherwise.
    if "is_broker" not in row:
        row["is_broker"] = True

    return row


def _find_postal_address(obj) -> Optional[dict]:
    """Recursive walk for any {"@type": "PostalAddress", ...} in a JSON tree."""
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
# Playwright fallback (likely needed for LoopNet given Akamai)
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
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)
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
    """Default `browser` is `cffi` since LoopNet (CoStar) is Akamai-protected,
    matching the BBS scraper's posture."""
    if browser not in ("httpx", "playwright", "cffi"):
        raise ValueError(f"browser must be 'httpx' | 'playwright' | 'cffi', got {browser!r}")

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
    else:  # browser == "cffi"
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
