"""SQLite persistence for Dealmill.

One `listings` table; URL is the dedupe key. Schema lives in `db/schema.sql` at
the project root and is applied idempotently on every `connect()`.

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
DEFAULT_DB_PATH = _PROJECT_ROOT / "data" / "dealmill.sqlite"
SCHEMA_PATH = _PROJECT_ROOT / "db" / "schema.sql"


def db_path() -> Path:
    """Resolve the SQLite file path. `DEALMILL_DB` env var overrides default."""
    override = os.environ.get("DEALMILL_DB")
    return Path(override) if override else DEFAULT_DB_PATH


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def apply_schema(conn: sqlite3.Connection) -> None:
    """Apply db/schema.sql. Idempotent (CREATE TABLE IF NOT EXISTS)."""
    conn.executescript(SCHEMA_PATH.read_text())


@contextmanager
def connect(path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    """Open a connection. Creates data/ if missing, enables WAL, applies schema."""
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        apply_schema(conn)
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

    Update semantics:
      - `last_seen` bumped to now.
      - `first_seen` left untouched.
      - `days_on_market` re-derived (caller's listing_date is still authoritative).
      - If `asking_price_cents` differs from the stored value, the OLD price is
        appended to `price_history` with the prior `last_seen` timestamp.
      - Every other scraper-owned field is COALESCE'd: a NULL in `row` keeps the
        previously stored value.
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
