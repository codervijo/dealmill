# PRD — Dealmill

A private, local, single-user tool that scrapes motivated-seller listings across SMB acquisitions, commercial real estate, laundromats, car washes, and self-storage; scores each listing 1–10 for seller motivation via the Claude API; and surfaces high-score deals through a local web dashboard and a daily email digest.

## Hard constraints
- **Local-only.** No cloud, no auth, no external database. The SQLite file on disk is the system of record.
- **Single-user.** Runs on the owner's machine; not deployed.
- **Stop-and-confirm between phases.** Phase N must be observed working by the owner before Phase N+1 starts.

## Phase 1 — Scrapers

Per-listing extraction contract, shared across all sources: `title`, `description`, `asking_price`, `location`, `category`, `days_on_market`, `is_broker` + `broker_name`, `url`, `first_seen` / `last_seen`. Persistence: single local SQLite DB. Dedupe key: `url`.

### v1.A — BizBuySell scraper (foundation; status: architecture done, live data blocked)

> **Status note (2026-05-14):** v1.A.1, v1.A.2, and v1.A.3 (scraper code) are done and exercised end-to-end against the live site. v1.A.4 and v1.A.5 are **blocked**: BizBuySell sits behind Akamai Bot Manager and serves a hard 314-byte "access denied" to our session — both the httpx path and Playwright + `tf-playwright-stealth`. JS-level stealth doesn't beat their detection. Unblock path is browser-cookie seeding or a residential proxy; tracked separately. The pipeline mechanics are validated; this isn't a code defect, it's a sourcing constraint. We pivot to v1.B (Craigslist) to unblock the rest of Phase 1 while BBS waits.


- [ ] **v1.A.1** — Dev container + builder integration. Project-local `Dockerfile` (`mcr.microsoft.com/playwright/python` + `uv`), `Makefile` with Docker-aware `BUILDER_PATH` fallback, `Makefile.local` (`CONTAINER_NAME=dmill1`, empty `HOST_PORT`/`CONTAINER_PORT` for host networking, subcommand wrappers), `pyproject.toml` (uv-managed deps: `httpx`, `beautifulsoup4`, `lxml`, `playwright`, `typer`, `rich`, `flask`, `python-dotenv`, `anthropic`, `pytest`), and `.env.example` with the owner-tunable constants:

  ```
  ANTHROPIC_API_KEY=
  TARGET_METROS=Salinas,Monterey,San Jose,Fresno,Sacramento
  TARGET_CATEGORIES=laundromat,car_wash,self_storage,smb,commercial_re
  MAX_ASKING_PRICE=500000
  MIN_SCORE_ALERT=7
  MIN_SCORE_INSTANT=8
  ```

  **Success:** `make buildsh` enters `dmill1`; `make deps` succeeds inside.
- [ ] **v1.A.2** — SQLite schema + `db.py`. Idempotent schema apply, `upsert_listing(row)` keyed on `url`, `first_seen` set on insert, `last_seen` updated on every observation, `days_on_market` recomputed from listing date when present. Raw HTML stored in a sibling column so re-parsing never needs a re-scrape. **Schema reserves columns for downstream phases (nullable now, populated later — so no migrations later):** `price_history TEXT` (JSON array, written by v1.A.3 on re-scrape when price changes), `enrichment TEXT` (JSON, v2.A), `score INTEGER`, `score_reasoning TEXT`, `top_signals TEXT`, `red_flags TEXT`, `remote_operable INTEGER` (all v2.A), `status TEXT DEFAULT 'new'` (v3.A; state machine `new → reviewed → contacted → archived`).
- [ ] **v1.A.3** — BizBuySell scraper. Default path: `httpx` + BeautifulSoup. Index pass walks pagination on the search results page; detail pass extracts the contract fields (prefer embedded `application/ld+json` blocks where present, fall back to CSS selectors). Playwright path opt-in via `--browser=playwright`, used only when httpx hits Cloudflare/403. **Polite-scrape contract:** randomized realistic User-Agent per request, 2–5s jittered delay between requests, exponential backoff on 429/503, per-listing failures logged and skipped (the pipeline never crashes on a single bad page). Reads `TARGET_CATEGORIES` and `MAX_ASKING_PRICE` from env to filter at scrape time. On re-scrape, if the price differs from the most recent stored price, the previous price is appended to `price_history`.
- [x] **v1.A.4 (CL + Flippa parsers, db, cookies) ✅ done (2026-05-15).** 74 tests passing. Fixtures: 6 real raw_html files captured in `tests/fixtures/{craigslist,flippa}/`. Coverage: `db.upsert_listing` semantics (first_seen / last_seen / price_history append on price change / days_on_market derivation / no-clobber on v2/v3 columns), `cookies.load_cookies_for` (env unset / file missing / malformed JSON / invalid entries filtered / relative paths resolve), `craigslist.parse_detail` (3 fixtures × shared invariants + per-fixture spot checks), `flippa.parse_detail` (3 fixtures × shared invariants). Run via `make test` inside `dmill1`.
- [ ] **BBS-specific parser tests** still pending — captured when v1.A.5 (cookies) produces first real BBS HTML.
- [ ] **v1.A.5** — _Parked 2026-05-16 (IP-reputation blocked)._ Tested end-to-end with: (1) httpx → 403, (2) Playwright headless + tf-playwright-stealth + cookies → 314-byte Access Denied, (3) curl_cffi (real Chrome TLS) + 31 valid session cookies → 403 *instantly*. The fact that real-Chrome-TLS + valid session got 403 with no JS challenge means Akamai has the originating IP flagged as automation, not our fingerprint or session. Only remaining technical path is a residential proxy (~$20-100/mo) — disproportionate to the private-tool budget. **Park.** Revisit if/when owner wants to invest in proxy infrastructure or scrapes from a fresh IP after a cooldown.

### v1.B — Craigslist scraper ✅ done (2026-05-14)
- [x] Discovery walks `<sub>.craigslist.org/search/{bfs,cre}` across `TARGET_METROS` (5-metro → subdomain map for our defaults; falls back to lowercase-strip-spaces for unknowns).
- [x] Parser: LD+JSON `PostalAddress` first (structured), `meta geo.placename` + `geo.region` fallback with cleanup. Title / asking price / listing date / days_on_market / is_broker / description / raw_html all populated correctly.
- [x] End-to-end smoke: `make run ARGS="scrape craigslist --limit 3 --pages 1"` lands 3 deduped rows; re-run shows `inserted=0, updated=3`; `last_seen` bumps.
- [x] Data quality observation (sourcing issue, **not** a parser defect): CL's `/bfs/` category is dominated by used equipment, not whole businesses. v2.A motivation scorer will rank these low; v3.A filters can hide them. May want to scope to `/cre/` only or raise a price floor later.

### v1.C — LoopNet scraper (parked 2026-05-16, same as BBS)
- [x] Architecture + cffi + cookie wiring all complete.
- [ ] **Parked.** Same IP-reputation block as BBS (v1.A.5). Same unblock path (residential proxy). Won't pursue further without proxy budget.

### v1.D — Flippa scraper ✅ done (2026-05-14)
- [x] Discovery walks `/search` via Playwright (Flippa is a JS SPA; httpx sees template placeholders only, so `run()` force-promotes browser to playwright with a warning).
- [x] Parser: LD+JSON Product/Offer first for title/price/category; og:title fallback. Location fields stay NULL because online businesses have no physical location.
- [x] End-to-end smoke: 3 rows inserted with title/asking_price/category/raw_html populated. Owner observation: og:title is boilerplate ("X | Type — Type listed on Flippa"); richer titles available in rendered DOM. Parser refinement deferred — raw_html stored, no re-scrape needed when we tighten selectors.

### v1.F — Crexi scraper (2026-05-16 · added)
- [x] `scrapers/crexi.py` — same shape as loopnet (LD+JSON-first parser, cookie loading, cffi default).
- [x] Cookie integration via `make refresh-cookies crexi` after logging into www.crexi.com.
- [ ] Live test pending. Crexi is less anti-bot than LoopNet historically; cffi + cookies should sail through.

### v1.E — Facebook Marketplace scaffold (2026-05-15 · waiting on cookies)
- [x] Scaffold + CLI dispatch in place (`scrapers/facebook.py`). `run()` raises with a clear instruction if no cookies are loaded; raises `NotImplementedError` when cookies are present so the real discover/parse work has a clear placeholder.
- [x] Cookie wiring matches BBS/LoopNet: `make refresh-cookies facebook` populates `cookies/facebook.json`; `FB_COOKIES_FILE` env points at it.
- [ ] **Waiting on owner action:** log into facebook.com in host Chrome, run `make refresh-cookies facebook`.
- [ ] Once cookies are present, land `discover_urls()` + `parse_detail()` against real logged-in marketplace HTML.

## Phase 2 — Scorer

### v2.A — Motivation scorer (Claude API)
- [ ] One Claude API call per listing (`claude-sonnet-4-20250514`) returning **a single JSON blob with both enrichment fields and the motivation score** — avoids the 2× API cost of running a separate enrichment pass.
- [ ] Output schema:

  ```json
  {
    "score": 7,
    "reasoning": "...",
    "top_signals": ["retiring", "no broker", "days on market 92"],
    "red_flags": ["financials not disclosed"],
    "enrichment": {
      "business_type": "...",
      "stated_revenue": null,
      "stated_cashflow": null,
      "seller_reason_for_selling": "...",
      "employee_count": null,
      "years_in_operation": null,
      "remote_operable": false
    }
  }
  ```

- [ ] Signal weights (highest first): motivated language (`retiring`, `health`, `must sell`, `motivated`, `flexible terms`, `owner will carry`); no broker mentioned; price reduction detected; days on market ≥ 45; remote-operable category; owner financing mentioned; asking price ≤ `MAX_ASKING_PRICE`.
- [ ] Batch up to 20 listings per run. Skip already-scored listings unless `--force` passed.
- [ ] On parse failure, store `score = NULL`, log a warning, continue — the pipeline never crashes on a single bad scoring response.

## Phase 3 — Dashboard

> Reframed 2026-05-15: terminal dashboard with Rich tables, NOT a web app. Matches the visual shape of the owner's `lamill fleet seo` command — emoji status dots, single-screen scannable table, summary footer.

### v3.A — Terminal dashboard ✅ done (2026-05-15)
- [x] `make run ARGS="dashboard"` — single flat Rich-rendered table of all listings, sorted score↓ days_on_market↓. (No sectioned view: removed 2026-05-15 — overkill for current data volumes; revisit if/when rows exceed a comfortable single-table scan.)
- [x] Filter flags: `--source`, `--min-score`, `--max-price`, `--status`, `--location`, `--limit`. The "interesting deals" workflow is now `dashboard --min-score 7` (post-v2.A) or other filter combos — no separate view.
- [x] Columns: status dot (🟢/🟡/🟠/⚪) · Score · Title (OSC 8 hyperlink + ↗) · Src · Cat · Location · Price · Days · Status.
- [x] Header: total count + DB mtime. Footer: roll-up counts (hot / medium / low / unscored / archived).
- [x] Hard requirement: terminal ≥ 200 columns wide. CLI errors out otherwise.
- [x] Status state machine (`new → reviewed → contacted → archived`) lives in the schema; v3.A renders the column read-only. Status mutation moves to a separate CLI command if/when needed (potential v3.B).

## Phase 4 — Alerts ⏸ paused (2026-05-15)

> Status: paused. Don't build automated alerting until Phase 1–3 work has surfaced enough genuine deals that alerting on them is worth the noise budget. Reopen when `dashboard --min-score 7` is producing rows worth waking up to.

> Decision (2026-05-15): don't automate email until the dashboard is reliably surfacing deals worth being notified about. `/interesting` becomes the primary deal-discovery surface; email digest is deferred to v4.C until the manual browse loop produces good leads.

### v4.A — Interesting Deals view _(merged into v3.A, 2026-05-15)_
The sectioned view was over-engineered for current data volumes. Surfacing interesting deals is now just `dashboard --min-score 7` (post-v2.A) or other filter combinations on v3.A. If/when row counts grow large enough that scanning a flat table no longer works, revisit as a follow-on phase.

### v4.B — Twilio SMS instant alerts (planned)
- [ ] Twilio SMS for any new deal scoring ≥ `MIN_SCORE_INSTANT` (default 8). Adds a paid vendor + new secrets (`TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `ALERT_PHONE`).
- [ ] Weekly summary email — top 10 deals still active, sorted by score.
- [ ] Price-drop alerts on owner-flagged deals.

### v4.C — Daily email digest (deferred)
- [ ] Was the original v4.A. Deferred per 2026-05-15: don't automate alerts until `/interesting` is producing real leads worth alerting on.
- [ ] Format/transport contract from the original spec stands: plain text, SendGrid OR SMTP selected by env, `--dry-run` previews, `MIN_SCORE_ALERT` (default 7).
- [ ] Trigger to flip on: owner determines manual `/interesting` browsing has surfaced enough good deals that automation is worth the noise budget.

## Phase 5 — Productionization ⏸ paused (2026-05-15)

> Status: paused alongside Phase 4. Outreach drafts and pipeline orchestration are downstream of having a steady stream of scored deals worth contacting. Reopen when the alert-worthiness signal is reliable.

### v5.A — Outreach Generator (manual-trigger)
- [ ] Triggered manually from the deal detail page; **never automated**.
- [ ] One Claude API call with listing context + `top_signals`. Returns two variants:
  - **Email** — under 100 words, warm/direct, no jargon, no price mention.
  - **Short message** — SMS/Facebook-friendly, under 50 words.
- [ ] Copy-to-clipboard on each variant.
- [ ] Generated outreach persisted to DB (sibling column or table) for audit.

### v5.B — Pipeline orchestrator + scheduler
- [ ] Single command (`dealmill run`) executes the full pipeline: `scrape → score → digest` across all enabled sources.
- [ ] Flags: `--scrape-only`, `--score-only`, `--alert-only`, `--dry-run`, `--force`.
- [ ] Run-stats logged each invocation: start/end time, new listings discovered, listings scored, alerts sent.
- [ ] Cron entry every 6h on the owner's host crontab (not an in-container scheduler) invoking `make run ARGS="run"` against the persistent `dmill1` container.

## Tech stack
Python ≥ 3.11 · SQLite · BeautifulSoup + Playwright (scraping) · Claude API `claude-sonnet-4-20250514` (scoring) · Flask (dashboard) · SendGrid / SMTP (email) · Twilio (SMS, v4.B) · host cron (scheduler, v5.B).

## Success criteria
- **Phase 1 done** when the BizBuySell scraper persists deduplicated rows to SQLite on a real run and the owner confirms the data looks right.
- **Each later phase done** only after the owner confirms the previous phase's output.
- A phase is **not done** if any step was skipped or any test was skipped — surface uncertainty (CLAUDE.md Rule 12: Fail loud).
