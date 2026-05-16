"""Env helpers — load `.env` once, expose typed accessors for the constants
declared in `.env.example`.

Scrape-time filter contract (v1.A.3): TARGET_CATEGORIES is a comma-separated
list of category keys; MAX_ASKING_PRICE is dollars. category_matches() is
deliberately permissive — a missing category on the listing does NOT filter
the row out (we'd rather over-keep at v1.A and let v2.A/v3.A refine).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


_LOADED = False


def load_env() -> None:
    """Idempotently load `.env` from the project root."""
    global _LOADED
    if _LOADED:
        return
    root = Path(__file__).resolve().parents[2]
    load_dotenv(root / ".env", override=False)
    _LOADED = True


def target_categories() -> set[str]:
    load_env()
    raw = os.environ.get("TARGET_CATEGORIES", "")
    return {c.strip().lower() for c in raw.split(",") if c.strip()}


def max_asking_price_cents() -> Optional[int]:
    load_env()
    raw = os.environ.get("MAX_ASKING_PRICE", "").strip()
    if not raw:
        return None
    try:
        return int(float(raw) * 100)
    except ValueError:
        return None


# Maps owner-facing category keys -> substrings to match against the BBS
# category string. 'smb' is intentionally an empty list because it's a
# catch-all (it should match anything).
_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "laundromat":    ["laundromat", "laundry", "coin laundry"],
    "car_wash":      ["car wash"],
    "self_storage":  ["storage"],
    "smb":           [],
    "commercial_re": ["real estate", "commercial real"],
}


def category_matches(scraped_category: Optional[str], targets: set[str]) -> bool:
    """Permissive filter: returns True when in doubt."""
    if not targets or "smb" in targets:
        return True
    if not scraped_category:
        return True  # unknown -> keep, let downstream phases refine
    cat = scraped_category.lower()
    for tgt in targets:
        for kw in _CATEGORY_KEYWORDS.get(tgt, []):
            if kw in cat:
                return True
    return False


def price_matches(asking_price_cents: Optional[int], max_cents: Optional[int]) -> bool:
    """Permissive filter: returns True when price is unknown."""
    if max_cents is None or asking_price_cents is None:
        return True
    return asking_price_cents <= max_cents
