"""dealmill.db — upsert semantics, schema application, scraper-owned fields."""

from __future__ import annotations

import json
import time
from datetime import date, timedelta

import pytest

from dealmill import db


@pytest.fixture
def tmp_db(tmp_path):
    return tmp_path / "test.sqlite"


def _row(conn, url: str):
    return conn.execute("SELECT * FROM listings WHERE url = ?", (url,)).fetchone()


def test_schema_applies(tmp_db):
    with db.connect(tmp_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        assert count == 0


def test_schema_apply_is_idempotent(tmp_db):
    with db.connect(tmp_db) as conn:
        pass
    with db.connect(tmp_db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        assert count == 0


def test_schema_reserves_v2_v3_columns(tmp_db):
    with db.connect(tmp_db) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(listings)").fetchall()}
        for c in ("score", "score_reasoning", "top_signals", "red_flags",
                  "enrichment", "remote_operable", "status", "price_history"):
            assert c in cols, f"missing reserved column: {c}"


def test_insert_minimal_row(tmp_db):
    with db.connect(tmp_db) as conn:
        action = db.upsert_listing(conn, {
            "source": "test",
            "url": "https://example.com/1",
            "title": "Hello",
        })
        assert action == "inserted"
        row = _row(conn, "https://example.com/1")
        assert row["title"] == "Hello"
        assert row["first_seen"] == row["last_seen"]
        assert row["status"] == "new"  # schema default


def test_re_upsert_returns_updated(tmp_db):
    payload = {"source": "test", "url": "https://example.com/1", "title": "x"}
    with db.connect(tmp_db) as conn:
        assert db.upsert_listing(conn, payload) == "inserted"
        assert db.upsert_listing(conn, payload) == "updated"


def test_update_bumps_last_seen_keeps_first_seen(tmp_db):
    with db.connect(tmp_db) as conn:
        db.upsert_listing(conn, {"source": "t", "url": "https://example.com/1", "title": "x"})
        first = _row(conn, "https://example.com/1")
        time.sleep(1.1)  # iso seconds resolution
        db.upsert_listing(conn, {"source": "t", "url": "https://example.com/1", "title": "x"})
        second = _row(conn, "https://example.com/1")
        assert first["first_seen"] == second["first_seen"]
        assert second["last_seen"] > first["last_seen"]


def test_coalesce_preserves_existing_when_new_is_null(tmp_db):
    with db.connect(tmp_db) as conn:
        db.upsert_listing(conn, {
            "source": "t", "url": "https://example.com/1",
            "title": "Original", "category": "alpha",
        })
        db.upsert_listing(conn, {
            "source": "t", "url": "https://example.com/1",
            "title": "Updated",
            # category omitted (i.e. None)
        })
        row = _row(conn, "https://example.com/1")
        assert row["title"] == "Updated"
        assert row["category"] == "alpha"


def test_price_history_appended_on_change(tmp_db):
    with db.connect(tmp_db) as conn:
        db.upsert_listing(conn, {
            "source": "t", "url": "https://example.com/1", "asking_price_cents": 10000,
        })
        db.upsert_listing(conn, {
            "source": "t", "url": "https://example.com/1", "asking_price_cents": 8000,
        })
        row = _row(conn, "https://example.com/1")
        assert row["asking_price_cents"] == 8000
        history = json.loads(row["price_history"])
        assert len(history) == 1
        assert history[0]["price_cents"] == 10000
        assert "observed_at" in history[0]


def test_price_history_unchanged_when_price_unchanged(tmp_db):
    with db.connect(tmp_db) as conn:
        db.upsert_listing(conn, {
            "source": "t", "url": "https://example.com/1", "asking_price_cents": 10000,
        })
        db.upsert_listing(conn, {
            "source": "t", "url": "https://example.com/1", "asking_price_cents": 10000,
        })
        row = _row(conn, "https://example.com/1")
        # Either NULL or empty array — both indicate no entries appended.
        assert row["price_history"] in (None, "", "[]")


def test_days_on_market_derived_from_listing_date(tmp_db):
    listing_date = (date.today() - timedelta(days=10)).isoformat()
    with db.connect(tmp_db) as conn:
        db.upsert_listing(conn, {
            "source": "t", "url": "https://example.com/1", "listing_date": listing_date,
        })
        row = _row(conn, "https://example.com/1")
        assert row["days_on_market"] == 10


def test_days_on_market_explicit_overrides_derivation(tmp_db):
    listing_date = (date.today() - timedelta(days=10)).isoformat()
    with db.connect(tmp_db) as conn:
        db.upsert_listing(conn, {
            "source": "t", "url": "https://example.com/1",
            "listing_date": listing_date, "days_on_market": 50,
        })
        row = _row(conn, "https://example.com/1")
        assert row["days_on_market"] == 50


def test_missing_url_raises(tmp_db):
    with db.connect(tmp_db) as conn:
        with pytest.raises(ValueError):
            db.upsert_listing(conn, {"source": "t", "title": "no url"})


def test_upsert_never_clobbers_v2_columns(tmp_db):
    """Scraper-owned upsert MUST NOT overwrite score / enrichment / status."""
    with db.connect(tmp_db) as conn:
        db.upsert_listing(conn, {
            "source": "t", "url": "https://example.com/1", "title": "first",
        })
        # Simulate v2.A having scored this row
        conn.execute(
            "UPDATE listings SET score = 8, score_reasoning = 'r', "
            "enrichment = '{}', status = 'reviewed' WHERE url = ?",
            ("https://example.com/1",),
        )
        # Re-upsert (scraper sees the same URL again)
        db.upsert_listing(conn, {
            "source": "t", "url": "https://example.com/1", "title": "second",
        })
        row = _row(conn, "https://example.com/1")
        assert row["title"] == "second"        # scraper field updated
        assert row["score"] == 8               # scorer field preserved
        assert row["score_reasoning"] == "r"
        assert row["enrichment"] == "{}"
        assert row["status"] == "reviewed"     # dashboard field preserved


def test_url_is_unique(tmp_db):
    """Direct INSERT with duplicate URL must fail at the DB layer."""
    import sqlite3
    with db.connect(tmp_db) as conn:
        db.upsert_listing(conn, {"source": "t", "url": "https://example.com/1"})
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO listings (source, url, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?)",
                ("t", "https://example.com/1", "now", "now"),
            )


def test_raw_html_stored_as_blob(tmp_db):
    with db.connect(tmp_db) as conn:
        db.upsert_listing(conn, {
            "source": "t", "url": "https://example.com/1",
            "raw_html": b"<html>test</html>",
        })
        row = _row(conn, "https://example.com/1")
        assert row["raw_html"] == b"<html>test</html>"
