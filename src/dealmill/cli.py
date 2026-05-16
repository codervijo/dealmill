"""Dealmill CLI entry — typer command tree. See docs/prd.md for the phased plan."""

import logging

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

app = typer.Typer(
    name="dealmill",
    help="Private deal sourcing tool. See docs/prd.md for the phased plan.",
    no_args_is_help=True,
)

# Per-domain subcommand groups. Today only `biz` is populated. `auctions`
# (v1.G) and `franchises` (v6) will get sibling subapps when those phases land.
biz_app = typer.Typer(
    name="biz",
    help="Business-for-sale listings (CL, Flippa, BBS, LoopNet, Crexi, FB).",
    no_args_is_help=True,
)
app.add_typer(biz_app, name="biz")

console = Console()


@app.callback()
def _root():
    """Top-level callback. Each domain (biz / auctions / franchises) is its
    own subcommand tree to keep DBs and workflows independent."""


@biz_app.callback()
def _biz_root():
    """Biz subcommand tree."""


@biz_app.command()
def scrape(
    source: str = typer.Argument(..., help="Source: bizbuysell | craigslist | loopnet | flippa | crexi | facebook."),
    limit: int = typer.Option(None, "--limit", help="Cap on inserts+updates this run."),
    max_pages: int = typer.Option(5, "--pages", help="Index pages to walk."),
    browser: str = typer.Option(
        None, "--browser",
        help="'httpx' | 'playwright' | 'cffi'. Default chosen per source (cffi for Akamai sites, playwright for SPAs, httpx for plain HTML).",
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Log per-listing progress."),
):
    """Run a scraper end-to-end (discover -> fetch -> parse -> upsert)."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, show_time=False)],
    )
    # httpx/httpcore are extremely chatty at DEBUG; we don't need them.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    # Per-source browser defaults; explicit --browser overrides.
    src = source.lower().strip()

    def call(mod, default: str):
        b = browser if browser else default
        return mod.run(limit=limit, max_pages=max_pages, browser=b)

    if src == "bizbuysell":
        from dealmill.scrapers import bizbuysell
        stats = call(bizbuysell, "cffi")
    elif src == "craigslist":
        from dealmill.scrapers import craigslist
        stats = call(craigslist, "httpx")
    elif src == "loopnet":
        from dealmill.scrapers import loopnet
        stats = call(loopnet, "cffi")
    elif src == "flippa":
        from dealmill.scrapers import flippa
        stats = call(flippa, "httpx")  # flippa auto-promotes to playwright internally
    elif src == "crexi":
        from dealmill.scrapers import crexi
        stats = call(crexi, "playwright")  # SPA — auto-promotion inside crexi.run() handles cffi→playwright
    elif src == "facebook":
        from dealmill.scrapers import facebook

        try:
            stats = facebook.run(limit=limit, max_pages=max_pages, browser=browser or "playwright")
        except (RuntimeError, NotImplementedError) as e:
            console.print(f"[yellow]{e}[/yellow]")
            raise typer.Exit(code=2)
    else:
        console.print(f"[red]unknown source[/red]: {source!r}")
        raise typer.Exit(code=2)

    table = Table(title=f"{src} scrape stats", show_header=False)
    for k in ("discovered", "fetched", "inserted", "updated", "skipped_filter", "failed"):
        table.add_row(k, str(stats.get(k, 0)))
    console.print(table)


@biz_app.command()
def reparse(
    source: str = typer.Argument(..., help="Source name (e.g. 'crexi', 'craigslist')."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Re-run parse_detail() on stored raw_html for every row of a source.

    Use this after a parser refinement to refresh existing rows without
    re-scraping (raw_html is preserved in the DB by v1.A.2's contract).
    db.upsert_listing uses COALESCE so NULL fields from the new parse don't
    clobber existing data; non-NULL fields replace.
    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, show_time=False)],
    )

    from importlib import import_module
    from dealmill import db as _db

    try:
        mod = import_module(f"dealmill.scrapers.{source.lower().strip()}")
    except ImportError:
        console.print(f"[red]unknown source: {source!r}[/red]")
        raise typer.Exit(code=2)
    parse_detail = getattr(mod, "parse_detail", None)
    if parse_detail is None:
        console.print(f"[red]source '{source}' has no parse_detail()[/red]")
        raise typer.Exit(code=2)

    updated = 0
    failed = 0
    with _db.connect() as conn:
        rows = conn.execute(
            "SELECT id, url, raw_html FROM listings WHERE source = ? AND raw_html IS NOT NULL",
            (source.lower().strip(),),
        ).fetchall()
        for r in rows:
            try:
                raw = r["raw_html"]
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                new = parse_detail(r["url"], raw)
                new.pop("raw_html", None)  # don't re-write the stored BLOB
                _db.upsert_listing(conn, new)
                updated += 1
            except Exception as e:  # noqa: BLE001
                console.print(f"[yellow]row {r['id']}: {type(e).__name__}: {e}[/yellow]")
                failed += 1

    console.print(
        f"reparsed [cyan]{source}[/cyan]: [green]{updated}[/green] updated · "
        f"[red]{failed}[/red] failed"
    )


@biz_app.command(name="db-status")
def db_status():
    """Open the SQLite DB (creating it + applying schema if needed) and print
    its path and row count. Smoke confirms v1.A.2 schema applies cleanly."""
    from dealmill.db import connect, db_path

    p = db_path()
    with connect(p) as conn:
        count = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    console.print(
        f"db: [cyan]{p}[/cyan]\nlistings rows: [green]{count}[/green]"
    )


@biz_app.command()
def score(
    limit: int = typer.Option(20, "--limit", help="Max listings to score this run."),
    force: bool = typer.Option(
        False, "--force", help="Re-score listings that already have a score."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
):
    """Score unscored listings via Claude API (v2.A)."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False, show_time=False)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)

    from dealmill.scorer import run as run_scorer

    try:
        stats = run_scorer(limit=limit, force=force)
    except RuntimeError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=2)

    table = Table(title="scorer stats", show_header=False)
    for k in ("considered", "scored", "failed"):
        table.add_row(k, str(stats.get(k, 0)))
    console.print(table)


@biz_app.command()
def dashboard(
    source: str = typer.Option(None, "--source", help="Filter by source (bizbuysell|craigslist|loopnet|flippa|facebook)."),
    min_score: int = typer.Option(None, "--min-score", help="Hide listings scored below this."),
    max_price: int = typer.Option(None, "--max-price", help="Hide listings priced above this (USD, no comma)."),
    status: str = typer.Option(None, "--status", help="Filter by status (new|reviewed|contacted|archived)."),
    location: str = typer.Option(None, "--location", help="Substring match on city/state/raw."),
    limit: int = typer.Option(50, "--limit", help="Max rows in the table."),
):
    """Terminal dashboard — Rich-rendered listings (v3.A).

    Requires a terminal at least 200 columns wide. Errors out otherwise.
    """
    import shutil

    cols = shutil.get_terminal_size((80, 24)).columns
    if cols < 200:
        console.print(
            f"[red]dashboard requires a terminal at least 200 columns wide; "
            f"yours is {cols}.[/red]"
        )
        console.print(
            "[dim]resize the terminal window and retry. "
            "(if you really need narrow output, pipe through `less -S`.)[/dim]"
        )
        raise typer.Exit(code=2)

    from dealmill.dashboard import render

    render(
        console=console,
        source=source,
        min_score=min_score,
        max_price=max_price,
        status=status,
        location=location,
        limit=limit,
    )
