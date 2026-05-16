# Prompt History

<!-- Append new prompts at the bottom, newest last. Format:
## YYYY-MM-DD
> <prompt text>
-->

## 2026-05-13
> Build a private deal sourcing tool called Dealmill — scrape (BizBuySell, Craigslist, LoopNet, Flippa), score with Claude API for seller motivation, surface in a local Flask dashboard, daily email digest of new deals scoring 7+. Local-only, single-user. Start with Phase 1 BizBuySell scraper and confirm before moving on. (Full spec landed in `docs/prd.md`.)

## 2026-05-13
> Init project structure using the project-init skill and the AI_AGENTS.md template pattern from `droyfun/vcops/AI_AGENTS.md`.

## 2026-05-13
> Modify the AI_AGENTS.md "Building info" and "Deployment info" sections to match the `sites/portfolio` Python + uv pattern (self-contained Makefile, not the central builder).

## 2026-05-13
> Add a Raison d'être section in the vcops/portfolio style.

## 2026-05-13
> Show feature table.

## 2026-05-13
> Refine the build strategy: Python runs in Docker only, use the central builder at `~/work/projects/builder/`, `make run` must run inside Docker. Then plan more before coding. JS only as a last resort — prefer Python or Go. Builder reference style: follow what `~/work/projects/sites/` consumer Makefiles do. Container name: `dmill1`. Networking: host. Update AI_AGENTS.md Building info + docs/prd.md Phase 1 to reflect the v1.A.1–v1.A.5 sub-phase breakdown.

## 2026-05-13
> Cross-check a claude.ai-generated `AI Agents Architecture` doc against the current PRD; decide what to adopt now vs defer. Decisions: keep TARGET_METROS = Salinas/Monterey/San Jose/Fresno/Sacramento; MAX_ASKING_PRICE default in `.env.example` (use 500000); add Facebook Marketplace as v1.E (planned only); plan Twilio SMS as v4.B.

## 2026-05-13
> Second claude.ai doc: a flatter "Phase 1 Features" checklist (DB, Scrapers, Enrichment, Scorer, Dashboard, Outreach Generator, Alerts, Pipeline Orchestrator). Mostly overlaps the first doc; adds operational details (random UA, 2–5s delays, `--test` / `--force` / `--dry-run` flags, port `localhost:5050`, status state machine, `new → reviewed → contacted → archived`) and introduces a Pipeline Orchestrator + manual Outreach Generator. Decide adoption + how this reconciles with the current 4-phase v1–v4 PRD structure.

## 2026-05-13
> Decisions on the second doc: keep the 4-phase v1–v4 PRD structure (stop-and-confirm between phases); adopt the operational concreteness into Phase 2 and Phase 3; promote Outreach Generator to v5.A; add Pipeline Orchestrator + cron as v5.B.

## 2026-05-14
> v1.A.1 / v1.A.2 / v1.A.3 implementation. Iterations: Dockerfile tag fix (v1.46→v1.59-noble); host-mode Make guard; httpx http2 dep miss; typer single-command callback fix; the BBS Akamai fight — httpx 403, Playwright + inline stealth 314-byte access-denied, Playwright + `tf-playwright-stealth` (import: `playwright_stealth`) same access-denied. Conclusion: BBS blocks at network/TLS/IP layer; stealth doesn't help. Pivoting to v1.B Craigslist; BBS waits for cookie seeding or proxy. v1.A.4 / v1.A.5 marked blocked in PRD; project memory `bbs-blocked-by-akamai` written so future sessions don't re-grind retries.

## 2026-05-14
> Start v1.B Craigslist scraper.

## 2026-05-14
> v1.B Craigslist landed, fully verified live (3 rows, dedupe, location parser refined via stored raw_html). v1.C LoopNet scaffold written but live blocked by Akamai (same as BBS). v1.D Flippa landed live via Playwright auto-promotion (Flippa is a JS SPA — httpx returns mustache templates). v1.E Facebook Marketplace scaffold written with cookie bail-out — login-walled, real selectors land when owner exports FB_COOKIES.

## 2026-05-15
> Build v3.A (dashboard) then v2.A (scorer); after that, plan while owner is AFK.

## 2026-05-15
> Reframe Phase 4: defer the daily email digest until the dashboard surfaces deals worth alerting on. Instead, build an "Interesting Deals" sectioned view on the dashboard (`/interesting`). Email becomes v4.C, deferred.

## 2026-05-15
> Revert the Flask web dashboard; rebuild v3.A as a terminal Rich-table dashboard like `lamill fleet seo`. `make run ARGS="dashboard"` shows sectioned interesting view by default; `--all` for the flat view with filters. Owner's example showed status dots (🟢🟠🔴), per-row context columns, summary footer.

## 2026-05-15
> Strip the sectioned "interesting" view — overkill for current data volumes. Make the flat view the only view; remove `--all`. "Interesting deals" becomes a filter flag combo (`dashboard --min-score 7`). v4.A merged into v3.A.

## 2026-05-15
> Pause Phase 4 (alerts) and Phase 5 (outreach + orchestrator). Focus stays on Phase 1–3. Reopen when scored-deal flow is reliable enough that alerting/automating is worth doing.

## 2026-05-15
> Wire cookie integration for BBS/LoopNet/FB. Use uv everywhere (host script too). Host-side `scripts/refresh_cookies.py` is a self-contained PEP 723 uv script with `browser-cookie3` as the only dep — reads Chrome's cookie DB, decrypts via OS keyring, writes Playwright-shaped JSON to `cookies/<source>.json`. `make refresh-cookies` invokes it. In-container `cookies.load_cookies_for(source)` reads the JSON from `*_COOKIES_FILE` env vars. Scrapers' `_playwright_fetcher()` now takes `cookies=...` and calls `context.add_cookies()` before navigating. Nothing else from `$HOME` is shared with the container — just the project-dir `cookies/` files.

## 2026-05-15
> Build test suite while owner is AFK (Plan A). Extracted 6 raw_html fixtures from current DB rows (3 CL + 3 Flippa). Wrote tests/test_db.py (15 cases), test_cookies.py (12 cases), test_craigslist_parser.py (24 cases via parametrize), test_flippa_parser.py (23 cases via parametrize). 74/74 passing on host via `PYTHONPATH=src uv run --no-project --with pytest --with bs4 --with lxml --with httpx ... pytest tests/`. Caught one assertion arithmetic bug (`300_000_00` ≠ $3M, fixed to `3_000_000 * 100`). All real bugs in production code: zero — current code is correct against the captured fixtures. v1.A.4 status updated from blocked → done (CL + Flippa portions); BBS-specific tests still pending real BBS HTML.

## 2026-05-16
> Cookie integration milestone: refresh-cookies works end-to-end. Fixed WAL handling for Firefox (copy main+wal+shm to tmpdir), expires normalization (negative timestamps → -1), and resilient bulk-then-one-by-one add_cookies. BBS scrape with 31 cookies loaded still returned Akamai 314-byte Access Denied — confirmed Akamai blocks at the TLS handshake layer, before cookies are inspected.

## 2026-05-16
> Add curl_cffi for real-Chrome TLS impersonation (defeats JA3/JA4). New browser mode `cffi` becomes the default for Akamai-protected sources (BBS, LoopNet) and the new v1.F Crexi scraper. CLI's `--browser` default flipped to None; per-source defaults applied. Crexi added as v1.F: same shape as LoopNet, cookie support, cffi default. Awaiting live test.
