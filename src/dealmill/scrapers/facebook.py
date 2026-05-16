"""Facebook Marketplace scraper scaffold (v1.E — login-walled).

Unlike BBS / LoopNet (publicly browsable but Akamai-blocked) or Craigslist /
Flippa (open), Facebook Marketplace requires an authenticated session even to
browse listings. There is no public surface to scrape — without cookies, the
marketplace endpoints redirect to a login wall.

Unblock path:

1. Log into facebook.com in your real browser.
2. Run `make refresh-cookies facebook` on the host. Writes
   `cookies/facebook.json`.
3. Ensure `.env` has `FB_COOKIES_FILE=cookies/facebook.json` (in `.env.example`).
4. Refine this module: discover_urls() + parse_detail() against real
   logged-in marketplace HTML.

Until that real-selector pass lands, `run()` checks for cookies via
`load_cookies_for("facebook")`. If empty, raises with a clear instruction.
If present, raises NotImplementedError (selectors not written yet — that's
the next focused task).
"""

from __future__ import annotations

from typing import Optional

from dealmill.cookies import env_var_for, load_cookies_for

SOURCE = "facebook"


def run(
    *,
    limit: Optional[int] = None,
    max_pages: int = 5,
    browser: str = "playwright",
) -> dict:
    cookies = load_cookies_for(SOURCE)
    if not cookies:
        raise RuntimeError(
            "Facebook Marketplace (v1.E) is login-walled and requires a real "
            "FB session cookie. On the host, log into facebook.com in Chrome, "
            "then run `make refresh-cookies facebook` to populate "
            f"cookies/facebook.json (or set {env_var_for(SOURCE)} to a different path)."
        )
    raise NotImplementedError(
        f"{len(cookies)} cookies loaded for facebook, but the cookie-aware "
        "Playwright discover/parse path isn't written yet. Next step: implement "
        "discover_urls() and parse_detail() against real logged-in "
        "marketplace.facebook.com HTML, mirroring scrapers/craigslist.py."
    )
