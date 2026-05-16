"""BizBuySell scraper (v1.A.3).

Discovery pass walks `https://www.bizbuysell.com/businesses-for-sale/?page=N`.
Detail pass extracts the v1.A.2 row contract, preferring `application/ld+json`
embedded blocks (Product/Offer/RealEstateListing) and falling back to `og:`
meta tags + h1/CSS heuristics.

Filters applied post-parse, pre-upsert: TARGET_CATEGORIES, MAX_ASKING_PRICE
(both permissive on unknown).

Per the v1.A.3 contract, per-listing failures are logged and skipped — the
pipeline never crashes on a single bad page. raw_html is always stored so a
broken parser can be fixed without re-hitting the network.
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

BASE = "https://www.bizbuysell.com"
INDEX = f"{BASE}/businesses-for-sale/"
SOURCE = "bizbuysell"

# Detail URL guess. If discovery yields zero hits, _debug_dump_index() saves
# the HTML and logs sample 'business' anchors so we can correct this regex.
_DETAIL_HREF_RE = re.compile(r"^/business-for-sale/[^/]+/\d+/?$")

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEBUG_DIR = _PROJECT_ROOT / "data" / "debug"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_urls(fetch_html, max_pages: int = 5) -> Iterator[str]:
    """Yield unique detail URLs across the first `max_pages` of the index.

    `fetch_html(url) -> str | None` — caller supplies the fetcher, so the
    discovery pass uses the same HTTP/browser path as the detail pass.
    """
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        url = INDEX if page == 1 else f"{INDEX}?page={page}"
        html = fetch_html(url)
        if not html:
            log.warning("index page %d returned no html; stopping discovery", page)
            return
        page_urls = list(_extract_detail_urls(html))
        if not page_urls:
            _debug_dump_index(page, url, html)
            log.info("index page %d yielded no detail URLs; stopping", page)
            return
        for href in page_urls:
            full = urljoin(BASE, href)
            if full in seen:
                continue
            seen.add(full)
            yield full


def _extract_detail_urls(html: str) -> Iterator[str]:
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if _DETAIL_HREF_RE.match(href):
            yield href


def _debug_dump_index(page: int, url: str, html: str) -> None:
    """When discovery yields zero URLs, save the HTML and log diagnostic info
    so we can see whether BBS served an interstitial or the real listings page
    has a different link structure than `_DETAIL_HREF_RE` expects."""
    _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    dump_path = _DEBUG_DIR / f"index_p{page}_{int(time.time())}.html"
    dump_path.write_text(html, encoding="utf-8")
    log.info("dumped index html (%d bytes) -> %s", len(html), dump_path)

    lowered = html.lower()
    for marker in (
        "captcha",
        "checking your browser",
        "verify you are human",
        "are you a robot",
        "access denied",
        "cf-challenge",
        "akam",
    ):
        if marker in lowered:
            log.warning("index html contains '%s' marker — likely anti-bot interstitial", marker)

    # Sample a handful of anchors so we can correct the detail-URL regex.
    soup = BeautifulSoup(html, "lxml")
    candidates = [
        a["href"] for a in soup.find_all("a", href=True)
        if "business" in a["href"].lower() or "listing" in a["href"].lower()
    ]
    log.info("sample anchors mentioning business/listing (%d found):", len(candidates))
    for href in candidates[:15]:
        log.info("  %s", href)


# ---------------------------------------------------------------------------
# Detail parsing
# ---------------------------------------------------------------------------

def parse_detail(url: str, html: str) -> dict:
    """Parse a BBS detail page into a row dict compatible with db.upsert_listing.

    Always returns a dict with at minimum `source`, `url`, and `raw_html`. Other
    fields populated best-effort; missing fields stay absent (db.upsert_listing
    COALESCEs them, so a re-parse with better selectors can refine later).
    """
    soup = BeautifulSoup(html, "lxml")
    row: dict = {
        "source": SOURCE,
        "url": url,
        "raw_html": html.encode("utf-8") if html else None,
    }

    _from_ld_json(soup, row)
    _from_og_meta(soup, row)
    _from_visible_html(soup, row)

    return row


def _from_ld_json(soup: BeautifulSoup, row: dict) -> None:
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        # Some pages embed an array of schemas in a single script tag.
        candidates = data if isinstance(data, list) else [data]
        for obj in candidates:
            if not isinstance(obj, dict):
                continue
            type_str = _type_of(obj)
            if not any(t in type_str for t in ("product", "offer", "businessforsale", "realestatelisting")):
                continue
            row.setdefault("title", obj.get("name"))
            row.setdefault("description", obj.get("description"))

            offers = obj.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if isinstance(offers, dict):
                price = offers.get("price") or offers.get("highPrice") or offers.get("lowPrice")
                cents = _to_cents(price)
                if cents is not None:
                    row.setdefault("asking_price_cents", cents)

            addr = obj.get("address")
            if isinstance(addr, list):
                addr = addr[0] if addr else None
            if isinstance(addr, dict):
                row.setdefault("location_city", addr.get("addressLocality"))
                row.setdefault("location_state", addr.get("addressRegion"))

            cat = obj.get("category") or obj.get("industry")
            if cat:
                row.setdefault("category", str(cat))
            return  # first matching schema wins


def _from_og_meta(soup: BeautifulSoup, row: dict) -> None:
    for prop, key in (
        ("og:title", "title"),
        ("og:description", "description"),
    ):
        if row.get(key):
            continue
        tag = soup.find("meta", {"property": prop})
        if tag and tag.get("content"):
            row[key] = tag["content"].strip()


def _from_visible_html(soup: BeautifulSoup, row: dict) -> None:
    # Title fallback: first h1
    if not row.get("title"):
        h1 = soup.find("h1")
        if h1:
            row["title"] = h1.get_text(strip=True)

    # Asking price fallback: any element whose class hints "price"
    if "asking_price_cents" not in row:
        for el in soup.find_all(class_=re.compile(r"price", re.I)):
            text = el.get_text(" ", strip=True)
            cents = _first_dollar_amount_to_cents(text)
            if cents is not None:
                row["asking_price_cents"] = cents
                break

    # Location fallback: look for "City, ST" patterns in obvious places.
    if not row.get("location_city") or not row.get("location_state"):
        body_text = soup.get_text(" ", strip=True)
        m = re.search(r"\b([A-Z][a-zA-Z\.\- ]{1,40}),\s*([A-Z]{2})\b", body_text)
        if m:
            row.setdefault("location_city", m.group(1).strip())
            row.setdefault("location_state", m.group(2).strip())
            row.setdefault("location_raw", m.group(0).strip())

    # Days on market: heuristic. BBS often shows "Days on Market: N" or "Listed: N days ago".
    if "days_on_market" not in row:
        text = soup.get_text(" ", strip=True)
        m = re.search(r"Days?\s+on\s+Market[:\s]+(\d+)", text, re.I)
        if not m:
            m = re.search(r"Listed[:\s]+(\d+)\s+days?\s+ago", text, re.I)
        if m:
            try:
                row["days_on_market"] = int(m.group(1))
            except ValueError:
                pass

    # Broker vs owner — heuristic.
    if "is_broker" not in row:
        text = soup.get_text(" ", strip=True).lower()
        if "listed by broker" in text or "broker:" in text or "listing broker" in text:
            row["is_broker"] = True
        elif "owner financing" in text or "by owner" in text or "for sale by owner" in text:
            row["is_broker"] = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _type_of(obj: dict) -> str:
    t = obj.get("@type", "")
    if isinstance(t, list):
        t = " ".join(str(x) for x in t)
    return str(t).lower()


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
# Playwright fallback (opt-in)
# ---------------------------------------------------------------------------

_STEALTH_INIT_SCRIPT = """
// Hide the WebDriver flag
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
// Fake a non-empty plugins array
Object.defineProperty(navigator, 'plugins', {
    get: () => [{name: 'Chrome PDF Plugin'}, {name: 'Chrome PDF Viewer'}, {name: 'Native Client'}],
});
// Realistic language list
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
// chrome.runtime is set on real Chrome
window.chrome = window.chrome || { runtime: {} };
// permissions.query for notifications usually trips bot detectors in headless
const _origQuery = window.navigator.permissions && window.navigator.permissions.query;
if (_origQuery) {
    window.navigator.permissions.query = (p) => (
        p && p.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : _origQuery.call(window.navigator.permissions, p)
    );
}
"""


@contextmanager
def _playwright_fetcher(*, cookies: Optional[list[dict]] = None):
    """Yields a single function `fetch(url) -> str` using a reused browser ctx.

    Layered anti-detection (inline, no extra dep): launches Chromium with
    AutomationControlled disabled, sets a realistic viewport/locale/timezone,
    overrides navigator.webdriver / plugins / languages / chrome.runtime via
    an init script, and sleeps 2-5s after each navigation so any JS challenge
    has time to resolve before we read the DOM.

    `cookies`: optional list of Playwright cookie dicts loaded by
    dealmill.cookies.load_cookies_for(). Added to the context BEFORE the
    first navigation so Akamai sees a valid session from request one.
    """
    from playwright.sync_api import sync_playwright

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
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Linux"',
        },
    )
    if cookies:
        add_cookies_resilient(context, cookies)
    context.add_init_script(_STEALTH_INIT_SCRIPT)

    # tf-playwright-stealth (PyPI dist name) installs as the module `playwright_stealth`.
    # Patches many leakage points our inline script can't. Applied per-page since
    # `stealth_sync` operates on a Page.
    from playwright_stealth import stealth_sync

    def fetch(url: str) -> str:
        jitter_sleep()  # 2-5s between fetches, just like httpx path
        page = context.new_page()
        try:
            stealth_sync(page)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2500)  # let post-load JS challenges resolve
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
    """Run the BizBuySell scraper end-to-end.

    Default `browser` is `cffi` (curl_cffi + real-Chrome TLS impersonation +
    seeded cookies) since BBS is fronted by Akamai Bot Manager — httpx gets
    a 403 instantly and headless Chromium gets a 314-byte access-denied.
    Override with `--browser=playwright` for JS-rendered pages if needed.

    Returns stats: {'discovered', 'inserted', 'updated', 'skipped_filter',
    'failed', 'fetched'}.
    """
    if browser not in ("httpx", "playwright", "cffi"):
        raise ValueError(f"browser must be 'httpx' | 'playwright' | 'cffi', got {browser!r}")

    cats = target_categories()
    max_cents = max_asking_price_cents()

    stats = dict(discovered=0, fetched=0, inserted=0, updated=0, skipped_filter=0, failed=0)

    # Build a unified fetcher used for BOTH discovery and detail.
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

                # Parse
                try:
                    row = parse_detail(url, html)
                except Exception as e:  # noqa: BLE001
                    log.warning("parse %s failed: %r; skipping", url, e)
                    stats["failed"] += 1
                    continue

                # Filter
                if not category_matches(row.get("category"), cats):
                    stats["skipped_filter"] += 1
                    continue
                if not price_matches(row.get("asking_price_cents"), max_cents):
                    stats["skipped_filter"] += 1
                    continue

                # Persist
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
