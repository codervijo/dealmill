"""Craigslist scraper (v1.B).

Walks each metro in `TARGET_METROS` across two categories:
- `bfs` — business for sale
- `cre` — commercial real estate for sale

URL shape: `https://<subdomain>.craigslist.org/search/<cat>` then pagination
via the `?s=N` query (CL paginates by offset, 120 results per page).

Craigslist is httpx-friendly (no Akamai). The Playwright path exists for
parity but is not the default.

Listing detail pages don't always include LD+JSON, so the parser leans on
CL's stable DOM: `#titletextonly` (title), `.price` (asking price),
`#postingbody` (description), `.postinginfos time.timeago` (listing date),
`.attrgroup` (mapped fields).
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
from dealmill.env import (
    category_matches,
    max_asking_price_cents,
    price_matches,
    target_categories,
)
from dealmill.polite_http import build_client, jitter_sleep, polite_get, random_ua

log = logging.getLogger(__name__)

SOURCE = "craigslist"
CATEGORIES = ("bfs", "cre")  # business-for-sale, commercial-RE-for-sale

# Map TARGET_METROS values to Craigslist subdomains. Unknown metros fall back
# to lowercase-and-strip-spaces (e.g. "Denver" -> "denver"); if that's wrong
# CL serves a generic page and we log + skip.
_METRO_SUBDOMAIN = {
    "salinas":      "monterey",
    "monterey":     "monterey",
    "san jose":     "sfbay",
    "sf bay":       "sfbay",
    "san francisco": "sfbay",
    "fresno":       "fresno",
    "sacramento":   "sacramento",
}

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEBUG_DIR = _PROJECT_ROOT / "data" / "debug"

# CL listing detail URL: https://<sub>.craigslist.org/<area>/<cat>/d/<slug>/<id>.html
_DETAIL_HREF_RE = re.compile(r"https?://[a-z0-9\-]+\.craigslist\.org/[^/]+/(?:bfs|cre)/d/[^/]+/\d+\.html$")


# ---------------------------------------------------------------------------
# Metro / URL helpers
# ---------------------------------------------------------------------------

def metros_from_env() -> list[str]:
    """Read TARGET_METROS from env into a list of raw metro names."""
    import os
    raw = os.environ.get("TARGET_METROS", "")
    return [m.strip() for m in raw.split(",") if m.strip()]


def subdomain_for(metro: str) -> str:
    return _METRO_SUBDOMAIN.get(metro.lower(), metro.lower().replace(" ", ""))


def search_url(subdomain: str, category: str, offset: int = 0) -> str:
    base = f"https://{subdomain}.craigslist.org/search/{category}"
    return base if offset == 0 else f"{base}?s={offset}"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover_urls(fetch_html, max_pages: int = 5) -> Iterator[str]:
    """Yield unique detail URLs across all metros × categories × pages."""
    seen: set[str] = set()
    subs = {subdomain_for(m) for m in metros_from_env()}
    if not subs:
        log.warning("TARGET_METROS is empty; falling back to ['sfbay']")
        subs = {"sfbay"}

    for sub in sorted(subs):
        for cat in CATEGORIES:
            for page in range(max_pages):
                url = search_url(sub, cat, offset=page * 120)
                html = fetch_html(url)
                if not html:
                    log.warning("CL search %s/%s offset=%d returned no html; skipping rest of this combo", sub, cat, page * 120)
                    break
                page_urls = list(_extract_detail_urls(html))
                if not page_urls:
                    if page == 0:
                        _debug_dump_search(sub, cat, html)
                    break  # no more results in this category
                yielded_this_page = 0
                for href in page_urls:
                    if href in seen:
                        continue
                    seen.add(href)
                    yielded_this_page += 1
                    yield href
                if yielded_this_page == 0:
                    break  # all dups, advance category


def _extract_detail_urls(html: str) -> Iterator[str]:
    soup = BeautifulSoup(html, "lxml")
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if _DETAIL_HREF_RE.match(href):
            yield href


def _debug_dump_search(subdomain: str, category: str, html: str) -> None:
    _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    p = _DEBUG_DIR / f"craigslist_{subdomain}_{category}_{int(time.time())}.html"
    p.write_text(html, encoding="utf-8")
    log.info("dumped CL search html (%d bytes) -> %s", len(html), p)


# ---------------------------------------------------------------------------
# Detail parsing
# ---------------------------------------------------------------------------

def parse_detail(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    row: dict = {
        "source": SOURCE,
        "url": url,
        "raw_html": html.encode("utf-8") if html else None,
        "category": _category_from_url(url),
        "is_broker": False,  # CL is overwhelmingly direct-seller
    }

    # Title
    t = soup.find(id="titletextonly")
    if t:
        row["title"] = t.get_text(strip=True)
    else:
        h1 = soup.find("h1")
        if h1:
            row["title"] = h1.get_text(strip=True)

    # Price
    price_el = soup.find(class_="price")
    if price_el:
        cents = _first_dollar_amount_to_cents(price_el.get_text(strip=True))
        if cents is not None:
            row["asking_price_cents"] = cents

    # Description
    body = soup.find(id="postingbody")
    if body:
        # Strip the QR-code notice CL injects
        for noise in body.find_all(class_=re.compile(r"print-qrcode|notices")):
            noise.decompose()
        row["description"] = body.get_text(" ", strip=True)

    # Listing date — `time.timeago[datetime=...]`
    time_el = soup.find("time", {"class": "timeago"})
    if time_el and time_el.get("datetime"):
        row["listing_date"] = time_el["datetime"][:10]

    # Location — precedence (cleanest first):
    #   1) LD+JSON PostalAddress (structured city/state fields)
    #   2) <meta name="geo.placename"> + <meta name="geo.region"> as fallback,
    #      with cleanup because CL sometimes packs full address into placename
    #      (e.g. "Hayward,CA 94545" instead of just "Hayward").
    for script in soup.find_all("script", {"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        addr = _find_postal_address(data)
        if addr:
            if addr.get("addressLocality"):
                row["location_city"] = str(addr["addressLocality"]).strip()
            if addr.get("addressRegion"):
                row["location_state"] = str(addr["addressRegion"]).strip().upper()
            break

    if not row.get("location_city"):
        geo_place = soup.find("meta", {"name": "geo.placename"})
        if geo_place and geo_place.get("content"):
            # Strip everything from the first comma or trailing digit-block.
            raw = geo_place["content"].strip()
            city = re.split(r",|\s+\d", raw, maxsplit=1)[0].strip()
            if city:
                row["location_city"] = city

    if not row.get("location_state"):
        geo_region = soup.find("meta", {"name": "geo.region"})
        if geo_region and geo_region.get("content"):
            content = geo_region["content"].strip()
            row["location_state"] = content.split("-")[-1].strip().upper() if "-" in content else content.upper()

    if row.get("location_city") and row.get("location_state"):
        row["location_raw"] = f"{row['location_city']}, {row['location_state']}"
    elif row.get("location_city"):
        row["location_raw"] = row["location_city"]

    return row


def _find_postal_address(obj) -> Optional[dict]:
    """Recursive walk to find any `{"@type": "PostalAddress", ...}` in a JSON tree."""
    if isinstance(obj, dict):
        if obj.get("@type") == "PostalAddress":
            return obj
        for v in obj.values():
            found = _find_postal_address(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _find_postal_address(v)
            if found:
                return found
    return None


def _category_from_url(url: str) -> Optional[str]:
    if "/bfs/d/" in url:
        return "business for sale"
    if "/cre/d/" in url:
        return "commercial real estate"
    return None


_DOLLAR_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")


def _first_dollar_amount_to_cents(text: str) -> Optional[int]:
    m = _DOLLAR_RE.search(text)
    if not m:
        return None
    try:
        return int(round(float(m.group(1).replace(",", "")) * 100))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Playwright fallback (rarely needed for CL, but keep parity with bizbuysell)
# ---------------------------------------------------------------------------

@contextmanager
def _playwright_fetcher():
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

    def fetch(url: str) -> str:
        jitter_sleep()
        page = context.new_page()
        try:
            stealth_sync(page)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
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
    browser: str = "httpx",
) -> dict:
    if browser not in ("httpx", "playwright"):
        raise ValueError(f"browser must be 'httpx' or 'playwright', got {browser!r}")

    cats = target_categories()
    max_cents = max_asking_price_cents()

    stats = dict(discovered=0, fetched=0, inserted=0, updated=0, skipped_filter=0, failed=0)

    client: Optional[httpx.Client] = None
    pw_cm = None
    pw_fetch = None

    if browser == "httpx":
        client = build_client()

        def fetch_html(url: str) -> Optional[str]:
            resp = polite_get(client, url)
            if not resp or resp.status_code != 200:
                status = resp.status_code if resp else "no-response"
                log.warning("GET %s -> %s", url, status)
                return None
            return resp.text
    else:
        pw_cm = _playwright_fetcher()
        pw_fetch = pw_cm.__enter__()

        def fetch_html(url: str) -> Optional[str]:
            try:
                return pw_fetch(url)
            except Exception as e:  # noqa: BLE001
                log.warning("playwright fetch %s failed: %r", url, e)
                return None

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

    return stats
