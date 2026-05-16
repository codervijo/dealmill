"""Terminal dashboard (v3.A) — Rich tables rendered in the dmill1 shell.

Single flat view with filter flags:
- `make run ARGS="dashboard"`                      — all listings
- `make run ARGS="dashboard --min-score 7"`        — only high-scoring
- `make run ARGS="dashboard --source craigslist"`  — single source
- `make run ARGS="dashboard --status reviewed"`    — by status
- `make run ARGS="dashboard --location Cupertino"` — substring on location
- `make run ARGS="dashboard --max-price 100000"`   — price ceiling

CLI enforces a minimum 200-col terminal.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich.text import Text

from dealmill import db


_SOURCE_LABELS = {
    "bizbuysell": "bbs",
    "craigslist": "cl",
    "loopnet":    "loopn",
    "flippa":     "flippa",
    "facebook":   "fb",
}

_STATUS_COLOR = {
    "new":       "blue",
    "reviewed":  "yellow",
    "contacted": "green",
    "archived":  "dim",
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def render(
    *,
    console: Console,
    source: Optional[str] = None,
    min_score: Optional[int] = None,
    max_price: Optional[int] = None,
    status: Optional[str] = None,
    location: Optional[str] = None,
    limit: int = 50,
) -> None:
    sql, params = _build_query(
        source=source,
        min_score=min_score,
        max_price=max_price,
        status=status,
        location=location,
        limit=limit,
    )
    with db.connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    _print_header(console, total=total)

    filt_parts = []
    if source:                filt_parts.append(f"source={source}")
    if min_score is not None: filt_parts.append(f"min_score≥{min_score}")
    if max_price is not None: filt_parts.append(f"max_price≤${max_price:,}")
    if status:                filt_parts.append(f"status={status}")
    if location:              filt_parts.append(f"location~{location!r}")
    filt_str = " · ".join(filt_parts) if filt_parts else "no filters"
    console.print(
        f"[dim]sort: score↓ days↓ · {filt_str} · showing {len(rows)} of {total}[/dim]"
    )

    if not rows:
        console.print()
        console.print("[yellow]No rows matched. Try widening filters.[/yellow]")
        _print_footer(console)
        return

    console.print()
    console.print(_build_row_table(rows))
    _print_footer(console)


# ---------------------------------------------------------------------------
# Header / footer
# ---------------------------------------------------------------------------

def _print_header(console: Console, *, total: int) -> None:
    p = db.db_path()
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(p)).isoformat(timespec="seconds")
    except OSError:
        mtime = "n/a"
    console.print(
        f"[bold]dealmill dashboard[/bold]  ·  "
        f"{total} listing{'' if total == 1 else 's'}  ·  "
        f"db updated [cyan]{mtime}[/cyan]"
    )


def _print_footer(console: Console) -> None:
    with db.connect() as conn:
        green    = conn.execute("SELECT COUNT(*) FROM listings WHERE score >= 8").fetchone()[0]
        yellow   = conn.execute("SELECT COUNT(*) FROM listings WHERE score BETWEEN 5 AND 7").fetchone()[0]
        orange   = conn.execute("SELECT COUNT(*) FROM listings WHERE score BETWEEN 1 AND 4").fetchone()[0]
        unscored = conn.execute("SELECT COUNT(*) FROM listings WHERE score IS NULL").fetchone()[0]
        archived = conn.execute("SELECT COUNT(*) FROM listings WHERE status='archived'").fetchone()[0]
    console.print()
    console.print(
        f"🟢 [green]{green}[/green] hot (8+)  ·  "
        f"🟡 [yellow]{yellow}[/yellow] medium (5-7)  ·  "
        f"🟠 [#d97706]{orange}[/#d97706] low (1-4)  ·  "
        f"⚪ [dim]{unscored}[/dim] unscored  ·  "
        f"📦 [dim]{archived}[/dim] archived"
    )


# ---------------------------------------------------------------------------
# Table
# ---------------------------------------------------------------------------

def _build_row_table(rows: list[dict]) -> Table:
    # Column widths tuned for the 200-col-min terminal the CLI enforces.
    # min_width keeps columns from collapsing when the table has many cols.
    table = Table(show_lines=False, show_header=True, header_style="bold", expand=True)
    table.add_column("",         width=3,    no_wrap=True)             # score dot
    table.add_column("Score",    justify="right", width=5, no_wrap=True)
    table.add_column("Title",    ratio=4, overflow="fold", min_width=50)
    table.add_column("Src",      width=6,   no_wrap=True)
    table.add_column("Cat",      ratio=2, overflow="fold", min_width=18)
    table.add_column("Location", ratio=2, overflow="fold", min_width=18)
    table.add_column("Price",    justify="right", width=11, no_wrap=True)
    table.add_column("Days",     justify="right", width=5,  no_wrap=True)
    table.add_column("Status",   width=10, no_wrap=True)

    for r in rows:
        score = r.get("score")
        dot = _score_dot(score)
        score_str = str(score) if score is not None else "—"
        title_str = (r.get("title") or "(no title)").strip()
        url = r.get("url") or ""
        if url:
            # OSC 8 hyperlink — clickable in modern terminals; the ↗ arrow
            # makes the link visible/discoverable everywhere.
            title_cell: object = Text(f"{title_str} ↗", style=f"link {url}")
        else:
            title_cell = title_str
        src = _SOURCE_LABELS.get(r.get("source", ""), r.get("source", ""))
        cat = (r.get("category") or "—").strip()
        loc_parts = [p for p in (r.get("location_city"), r.get("location_state")) if p]
        location = ", ".join(loc_parts) if loc_parts else "—"
        price_cents = r.get("asking_price_cents")
        price = f"${price_cents / 100:,.0f}" if price_cents else "—"
        dom = r.get("days_on_market")
        dom_str = str(dom) if dom is not None else "—"
        status_text = _status_text(r.get("status", "new"))

        table.add_row(dot, score_str, title_cell, src, cat, location, price, dom_str, status_text)

    return table


def _score_dot(score) -> str:
    if score is None:        return "[dim]⚪[/dim]"
    if score >= 8:           return "[green]🟢[/green]"
    if score >= 5:           return "[yellow]🟡[/yellow]"
    return "[#d97706]🟠[/#d97706]"


def _status_text(status: str) -> Text:
    color = _STATUS_COLOR.get(status, "white")
    return Text(status, style=color)


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

def _build_query(
    *,
    source: Optional[str],
    min_score: Optional[int],
    max_price: Optional[int],
    status: Optional[str],
    location: Optional[str],
    limit: int,
) -> tuple[str, list]:
    parts = ["SELECT * FROM listings WHERE 1=1"]
    params: list = []
    if source:
        parts.append("AND source = ?")
        params.append(source.lower().strip())
    if min_score is not None:
        parts.append("AND score >= ?")
        params.append(min_score)
    if max_price is not None:
        parts.append("AND asking_price_cents <= ?")
        params.append(max_price * 100)
    if status:
        parts.append("AND status = ?")
        params.append(status.lower().strip())
    if location:
        parts.append("AND (location_city LIKE ? OR location_state LIKE ? OR location_raw LIKE ?)")
        pat = f"%{location}%"
        params.extend([pat, pat, pat])
    parts.append("ORDER BY (score IS NULL), score DESC, days_on_market DESC LIMIT ?")
    params.append(limit)
    return " ".join(parts), params
