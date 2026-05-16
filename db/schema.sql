-- Dealmill schema. Applied idempotently by db.py:apply_schema() on every connect.
--
-- One table, URL-keyed dedupe. Scraper-owned columns are populated on every
-- upsert; v2.A/v3.A columns are reserved nullable here so we never need an
-- ALTER TABLE migration later.

CREATE TABLE IF NOT EXISTS listings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,

    -- Scraper-owned (v1.A.3 onward).
    source              TEXT    NOT NULL,            -- 'bizbuysell', 'craigslist', ...
    url                 TEXT    NOT NULL UNIQUE,     -- dedupe key
    title               TEXT,
    description         TEXT,
    asking_price_cents  INTEGER,                     -- integer cents avoids float rounding
    location_city       TEXT,
    location_state      TEXT,
    location_raw        TEXT,                        -- whatever the source gives, pre-parse
    category            TEXT,
    days_on_market      INTEGER,                     -- derived from listing_date when absent
    listing_date        TEXT,                        -- ISO 8601 date (source-claimed)
    is_broker           INTEGER,                     -- 0/1, NULL = unknown
    broker_name         TEXT,
    raw_html            BLOB,                        -- so re-parse never needs re-scrape

    first_seen          TEXT    NOT NULL,            -- ISO 8601 UTC, set once
    last_seen           TEXT    NOT NULL,            -- ISO 8601 UTC, bumped each upsert

    -- v1.A.3: appended JSON array of {price_cents, observed_at} when price changes.
    price_history       TEXT,

    -- v2.A: motivation scorer fields.
    enrichment          TEXT,                        -- JSON blob
    score               INTEGER,                     -- 1..10
    score_reasoning     TEXT,
    top_signals         TEXT,                        -- JSON array
    red_flags           TEXT,                        -- JSON array
    remote_operable     INTEGER,                     -- 0/1, NULL = unknown

    -- v3.A: dashboard state machine.
    status              TEXT    NOT NULL DEFAULT 'new'  -- new | reviewed | contacted | archived
);

CREATE INDEX IF NOT EXISTS idx_listings_score     ON listings(score);
CREATE INDEX IF NOT EXISTS idx_listings_status    ON listings(status);
CREATE INDEX IF NOT EXISTS idx_listings_source    ON listings(source);
CREATE INDEX IF NOT EXISTS idx_listings_last_seen ON listings(last_seen);
