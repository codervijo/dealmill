"""SQLite persistence for Dealmill.

Three independent domains, each with its own SQLite file + schema:
  - `biz`        — business-for-sale listings (CL, Flippa, BBS, LoopNet, Crexi, FB)
  - `auctions`   — future v1.G (`data/auctions.sqlite`)
  - `franchises` — future v6 (`data/franchises.sqlite`)

Today only the biz DB exists; the others materialize when their phases land.

URL is the dedupe key within a domain. Schema lives in `db/<domain>_schema.sql`
at the project root and is applied idempotently on every `connect()`.

Contract: scraper-owned columns are written here. Columns reserved for v2.A
(enrichment / score / score_reasoning / top_signals / red_flags /
remote_operable) and v3.A (status) are intentionally NOT touched by
`upsert_listing` — those phases own their own writes.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional


# Project root is two levels above this file: src/dealmill/db.py -> dealmill/
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DATA_DIR = _PROJECT_ROOT / "data"
_SCHEMA_DIR = _PROJECT_ROOT / "db"

DEFAULT_DOMAIN = "biz"

# Per-domain file + schema. Future domains slot in by adding an entry.
_DOMAIN_DB_FILE = {
    "biz":        "biz.sqlite",
    "auctions":   "auctions.sqlite",
    "franchises": "franchises.sqlite",
}

_DOMAIN_SCHEMA_FILE = {
    "biz":        "biz_schema.sql",
    "auctions":   "auctions_schema.sql",
    "franchises": "franchises_schema.sql",
}


def db_path(domain: str = DEFAULT_DOMAIN) -> Path:
    """Resolve a domain's SQLite file path.

    Env var precedence:
      `DEALMILL_DB_<DOMAIN>` (e.g. DEALMILL_DB_BIZ) overrides the default path.
      `DEALMILL_DB` is honored for back-compat with the biz domain only.
    """
    if domain not in _DOMAIN_DB_FILE:
        raise ValueError(f"unknown domain {domain!r}; known: {sorted(_DOMAIN_DB_FILE)}")
    env = os.environ.get(f"DEALMILL_DB_{domain.upper()}")
    if not env and domain == "biz":
        env = os.environ.get("DEALMILL_DB")  # back-compat
    return Path(env) if env else (_DATA_DIR / _DOMAIN_DB_FILE[domain])


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def apply_schema(conn: sqlite3.Connection, domain: str = DEFAULT_DOMAIN) -> None:
    """Apply db/<domain>_schema.sql. Idempotent (CREATE TABLE IF NOT EXISTS)."""
    if domain not in _DOMAIN_SCHEMA_FILE:
        raise ValueError(f"unknown domain {domain!r}; known: {sorted(_DOMAIN_SCHEMA_FILE)}")
    schema_path = _SCHEMA_DIR / _DOMAIN_SCHEMA_FILE[domain]
    if not schema_path.exists():
        raise FileNotFoundError(
            f"schema file missing for domain {domain!r}: {schema_path}. "
            f"(Domains other than 'biz' aren't built yet — their schemas land "
            f"when their respective phases ship.)"
        )
    conn.executescript(schema_path.read_text())


@contextmanager
def connect(
    path: Optional[Path] = None,
    *,
    domain: str = DEFAULT_DOMAIN,
) -> Iterator[sqlite3.Connection]:
    """Open a connection to a domain's DB.

    `path` overrides the resolved DB path (used by tests with tmp_path).
    `domain` selects which schema to apply.

    Creates `data/` if missing, enables WAL, applies the domain schema.
    """
    p = path or db_path(domain)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        apply_schema(conn, domain)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _derive_days_on_market(
    listing_date: Optional[str],
    provided: Optional[int],
) -> Optional[int]:
    """Prefer scraper-supplied days_on_market; else derive from listing_date."""
    if provided is not None:
        return provided
    if not listing_date:
        return None
    try:
        ld = date.fromisoformat(listing_date[:10])
    except ValueError:
        return None
    return max((date.today() - ld).days, 0)


def _bool_or_none(v: Any) -> Optional[int]:
    if v is None:
        return None
    return 1 if v else 0


def upsert_listing(conn: sqlite3.Connection, row: dict) -> str:
    """Insert (by URL) or update an existing row.

    Returns 'inserted' or 'updated'.

    Currently only the biz schema has this `listings` table. When auctions /
    franchises land with different schemas, they'll get their own upsert_*
    functions — keeping each domain's contract explicit.

    Update semantics (biz):
      - `last_seen` bumped to now.
      - `first_seen` left untouched.
      - `days_on_market` re-derived (caller's listing_date is still authoritative).
      - If `asking_price_cents` differs from stored, the OLD price is appended
        to `price_history` with the prior `last_seen` timestamp.
      - Every other scraper-owned field is COALESCE'd: a NULL in `row` keeps
        the previously stored value.
      - Scorer / dashboard columns (score, enrichment, status, ...) are NEVER
        touched here.
    """
    url = row.get("url")
    if not url:
        raise ValueError("upsert_listing: row missing required 'url'")

    now = now_iso()
    new_price = row.get("asking_price_cents")
    new_dom = _derive_days_on_market(row.get("listing_date"), row.get("days_on_market"))

    existing = conn.execute(
        "SELECT id, asking_price_cents, price_history, last_seen "
        "FROM listings WHERE url = ?",
        (url,),
    ).fetchone()

    if existing is None:
        conn.execute(
            """
            INSERT INTO listings (
                source, url, title, description, asking_price_cents,
                location_city, location_state, location_raw,
                category, days_on_market, listing_date,
                is_broker, broker_name, raw_html,
                first_seen, last_seen
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row.get("source"),
                url,
                row.get("title"),
                row.get("description"),
                new_price,
                row.get("location_city"),
                row.get("location_state"),
                row.get("location_raw"),
                row.get("category"),
                new_dom,
                row.get("listing_date"),
                _bool_or_none(row.get("is_broker")),
                row.get("broker_name"),
                row.get("raw_html"),
                now,
                now,
            ),
        )
        return "inserted"

    listing_id = existing["id"]
    old_price = existing["asking_price_cents"]
    old_history = json.loads(existing["price_history"] or "[]")
    old_last_seen = existing["last_seen"]

    if new_price is not None and old_price is not None and new_price != old_price:
        old_history.append({"price_cents": old_price, "observed_at": old_last_seen})
        new_history_json: Optional[str] = json.dumps(old_history)
    elif old_history:
        new_history_json = json.dumps(old_history)
    else:
        new_history_json = None

    conn.execute(
        """
        UPDATE listings SET
            title              = COALESCE(?, title),
            description        = COALESCE(?, description),
            asking_price_cents = COALESCE(?, asking_price_cents),
            location_city      = COALESCE(?, location_city),
            location_state     = COALESCE(?, location_state),
            location_raw       = COALESCE(?, location_raw),
            category           = COALESCE(?, category),
            days_on_market     = COALESCE(?, days_on_market),
            listing_date       = COALESCE(?, listing_date),
            is_broker          = COALESCE(?, is_broker),
            broker_name        = COALESCE(?, broker_name),
            raw_html           = COALESCE(?, raw_html),
            last_seen          = ?,
            price_history      = ?
        WHERE id = ?
        """,
        (
            row.get("title"),
            row.get("description"),
            new_price,
            row.get("location_city"),
            row.get("location_state"),
            row.get("location_raw"),
            row.get("category"),
            new_dom,
            row.get("listing_date"),
            _bool_or_none(row.get("is_broker")),
            row.get("broker_name"),
            row.get("raw_html"),
            now,
            new_history_json,
            listing_id,
        ),
    )
    return "updated"
