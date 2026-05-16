# AI Agent Context — Dealmill

> **Spec, phases, and success criteria live in [`docs/prd.md`](docs/prd.md)** — not duplicated here. This file is for agents entering the project cold: what it is, how to run it, where to look.

## Raison d'être

`dealmill` is a **private deal-flow funnel** for motivated-seller acquisitions. It exists because the deals worth looking at — SMB acquisitions, commercial real estate, laundromats, car washes, self-storage — are scattered across multiple marketplaces (BizBuySell, Craigslist, LoopNet, Flippa) with no single view, and the strongest signals of motivation (distress language, days on market, no broker, price cuts) get lost in the noise unless something scores every new listing the moment it appears. dealmill is the single place to:

1. Scrape every new listing across the tracked sources into one local SQLite store, deduplicated by URL.
2. Score each listing 1–10 for seller motivation using the Claude API — signals: distress language ("retiring", "health", "must sell", "motivated", "flexible terms", "owner will carry"), no broker mentioned, price reductions, days on market ≥ 45, remote-operable category.
3. Surface high-score deals through a local Flask dashboard (filter by category / price / location / score; flag and archive) and a daily email digest of new deals scoring ≥ 7.

This is a **single-user, local-only** tool. No cloud, no auth, no external database. It runs on the owner's machine and exists to find motivated-seller deals before other buyers do.

For the full phased plan, hard constraints, and per-phase success criteria, read `docs/prd.md`.

## Stack
- Python ≥ 3.11
- SQLite (single local file — the system of record)
- BeautifulSoup + Playwright (scraping)
- Claude API — model `claude-sonnet-4-20250514` (motivation scoring; model ID as specified by the owner)
- Flask (local web dashboard)
- SendGrid or SMTP (daily digest transport)

## Project structure
- `docs/` — PRD, prompt history
- `CLAUDE.md` — project rules every task must follow
- (source layout TBD — proposed `src/dealmill/` with submodules per scraper / scorer / dashboard / alerts)
- (data layout TBD — proposed `data/dealmill.sqlite`)

## Building info

**Stack: Python ≥ 3.11 + Playwright, run exclusively inside Docker via the central builder at `~/work/projects/builder/`.** No host Python install. A project-local `./Dockerfile` based on Microsoft's `mcr.microsoft.com/playwright/python` image — with `uv` layered on top — gives us Python, Playwright, Chromium, and every system lib Chromium needs in one image (zero Node, no pnpm). The builder's central `Makefile` auto-detects `STACK=python` from `pyproject.toml` and supplies `deps` / `build` / `test` / `clean` via `Makefile.python`. Dealmill subcommands (`scrape`, `score`, `serve`, `digest`) live in `Makefile.local` as thin wrappers around `uv run python -m dealmill <cmd>`.

**Builder reference** — the project `Makefile` uses the Docker-aware fallback pattern (matches `sites/*/seo/` and `sites/iotnews.today/ingester/`):

```make
ifneq ($(wildcard /usr/src/builder),)
  BUILDER_PATH ?= /usr/src/builder
else ifneq ($(wildcard /usr/builder),)
  BUILDER_PATH ?= /usr/builder
else
  BUILDER_PATH ?= $(HOME)/work/projects/builder
endif
include $(BUILDER_PATH)/Makefile
```

This resolves to `/usr/src/builder` inside the `dmill1` dev container (the central `buildsh` recipe copies the builder repo in and symlinks `/usr/builder` → that), and falls back to `$(HOME)/work/projects/builder` on the host.

**Dev container config** (set in `Makefile.local` via `export`):

| Env var          | Value     | Why                                                  |
|------------------|-----------|------------------------------------------------------|
| `CONTAINER_NAME` | `dmill1`  | Project-named container; coexists with other projects |
| `HOST_PORT`      | _(empty)_ | Triggers `--network=host` in `dev_container.sh`      |
| `CONTAINER_PORT` | _(empty)_ | Same as above                                         |

Host networking is intentional — scrapers need unfettered outbound, and the Flask dashboard (when v3.A lands) is reachable on `localhost` from the host browser without port-map juggling.

**Normal flow:**

```bash
cd droyfun/dealmill
make buildsh                                   # build dmill1 image (first run) + enter shell

# inside the dmill1 container:
make deps                                      # uv sync
make run ARGS="scrape bizbuysell --limit 5"    # invoke CLI; pass args via ARGS=
make scrape                                    # alias: uv run python -m dealmill scrape $(ARGS)
make serve                                     # local Flask dashboard (v3.A)
make test                                      # uv run pytest
make clean                                     # remove .venv, dist, *.egg-info, __pycache__
```

`make run` from the host prints the builder's "Not running inside Docker" warning. That's intentional — there is no host Python; always work from inside `dmill1`.

There is no parent `Makefile` to invoke from `droyfun/`. Do **not** try to drive Dealmill through `sites/Makefile`'s Docker-orchestrated `make run proj=...` flow; that path is hardcoded for the pnpm/Vite sites and is not applicable here.

## Deployment info

- **Platform**: n/a — Dealmill is a local tool (CLI + localhost Flask dashboard), not a deployable web app.
- **Live URL**: none.
- **Last deployed commit**: n/a.
- **Deploy trigger**: n/a — runs locally on the owner's machine.
- **Notes**: Conformance `kind = local-tool` (Python `pyproject.toml`, no public deploy target). Requires `ANTHROPIC_API_KEY`, and either a SendGrid API key or SMTP credentials, supplied via env (`.env` is gitignored). All listing data and scores stay in the local SQLite file.

## How to run

Phase 1 only at start. Plan: implement and confirm the BizBuySell scraper end-to-end (rows landed in SQLite, deduplicated by URL) **before** moving on to additional sources, scoring, dashboard, or alerts. See `docs/prd.md` for the full phased plan.

## Key conventions
- Deduplicate listings by URL
- Score with Claude (`claude-sonnet-4-20250514`)
- All state lives in one local SQLite DB
- No external services beyond the Claude API, scraping targets, and email transport
- Follow `CLAUDE.md` rules on every task (caution > speed; ask rather than guess; surgical changes; no surprise commits)

## Out of scope / don't touch
- _(to be filled in by the owner as the project matures)_
