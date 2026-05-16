"""In-container cookie loader. Reads JSON files produced by
`scripts/refresh_cookies.py` and returns Playwright-shaped cookie dicts.

Each source maps to an env var pointing at a JSON file path:

    bizbuysell -> BBS_COOKIES_FILE
    loopnet    -> LOOPNET_COOKIES_FILE
    facebook   -> FB_COOKIES_FILE

Returns [] (graceful no-op) when the env var is unset or the file is missing
or malformed — scrapers proceed cookieless rather than crashing.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


_ENV_VAR = {
    "bizbuysell": "BBS_COOKIES_FILE",
    "loopnet":    "LOOPNET_COOKIES_FILE",
    "facebook":   "FB_COOKIES_FILE",
    "crexi":      "CREXI_COOKIES_FILE",
}

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def env_var_for(source: str) -> Optional[str]:
    return _ENV_VAR.get(source.lower())


def load_cookies_for(source: str) -> list[dict]:
    """Return cookies for `source` ([] if not configured / file missing / invalid)."""
    env_name = env_var_for(source)
    if not env_name:
        return []
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return []

    path = Path(raw)
    if not path.is_absolute():
        path = _PROJECT_ROOT / path

    if not path.exists():
        log.warning("cookies file %s does not exist (env: %s)", path, env_name)
        return []

    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("failed to read cookies from %s: %r", path, e)
        return []

    if not isinstance(data, list):
        log.warning("cookies file %s is not a JSON array", path)
        return []

    valid = [
        c for c in data
        if isinstance(c, dict) and c.get("name") and c.get("value") is not None
    ]
    for c in valid:
        c["expires"] = _normalize_expires(c.get("expires"))
    if not valid:
        log.warning("no valid cookies in %s", path)
    else:
        log.info("loaded %d cookies for %s from %s", len(valid), source, path)
    return valid


def _normalize_expires(v) -> float:
    """Playwright requires `expires` = -1 (session) or a positive unix timestamp.
    Anything else (None, 0, negative, non-numeric, absurdly large) → -1."""
    if v is None:
        return -1
    try:
        f = float(v)
    except (TypeError, ValueError):
        return -1
    if f <= 0 or f > 9_999_999_999:  # ~year 2286
        return -1
    return f


def add_cookies_resilient(context, cookies: list[dict]) -> int:
    """Add cookies to a Playwright BrowserContext, tolerating per-cookie failures.

    Tries the bulk add first (fast). On any error, retries cookie-by-cookie so
    a single malformed cookie doesn't drop the entire session. Returns the
    number successfully added.
    """
    try:
        context.add_cookies(cookies)
        log.info("playwright: loaded %d cookies into context", len(cookies))
        return len(cookies)
    except Exception as e:  # noqa: BLE001
        log.warning("playwright: bulk add_cookies failed (%r); retrying one-by-one", e)
        added = 0
        rejected = 0
        for c in cookies:
            try:
                context.add_cookies([c])
                added += 1
            except Exception:  # noqa: BLE001
                rejected += 1
        log.info("playwright: loaded %d / rejected %d cookies into context", added, rejected)
        return added
