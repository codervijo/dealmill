"""Claude motivation scorer (v2.A).

One API call per listing returning a single JSON blob with both enrichment
fields AND the 1-10 motivation score. Per `docs/prd.md` v2.A:

    {
      "score": 1..10,
      "reasoning": "...",
      "top_signals": ["..."],
      "red_flags": ["..."],
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

Batch up to 20 listings per run. Skip already-scored listings unless
`--force`. Per-listing failures log + skip; pipeline never crashes on a
single bad scoring response.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from typing import Optional

from dealmill import db
from dealmill.env import load_env

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-20250514"

SYSTEM_PROMPT = (
    "You are a deal analyst specializing in motivated-seller acquisitions of "
    "small businesses, commercial real estate, laundromats, car washes, and "
    "self-storage facilities. You score listings 1-10 for motivated-seller "
    "signals: 10 = highly motivated, open to creative financing, likely to "
    "accept seller financing; 1 = retail listing, broker-represented, no urgency."
)

USER_PROMPT_TEMPLATE = """Score this listing 1-10 for motivated-seller signals and extract structured enrichment fields.

Signal weights (highest first):
- Motivated language: "retiring", "health", "must sell", "motivated", "flexible terms", "owner will carry"
- No broker mentioned
- Price reductions
- Days on market >= 45
- Remote-operable category
- Owner financing mentioned
- Asking price under $500k

Listing:
- source: {source}
- url: {url}
- title: {title}
- category: {category}
- location: {location}
- asking_price_usd: {price}
- days_on_market: {dom}
- broker_listed: {broker}
- description: {description}

Return ONLY valid JSON in this exact shape (no markdown fences, no preamble):
{{
  "score": <integer 1-10>,
  "reasoning": "<one short sentence>",
  "top_signals": ["...", "..."],
  "red_flags": ["...", "..."],
  "enrichment": {{
    "business_type": "<short string>",
    "stated_revenue": <number or null>,
    "stated_cashflow": <number or null>,
    "seller_reason_for_selling": "<string or null>",
    "employee_count": <integer or null>,
    "years_in_operation": <integer or null>,
    "remote_operable": <true or false>
  }}
}}
"""


# ---------------------------------------------------------------------------
# Prompt + parse
# ---------------------------------------------------------------------------

def _format_prompt(row: dict) -> str:
    price = row.get("asking_price_cents")
    price_str = f"${price / 100:,.0f}" if price else "unknown"
    loc_parts = [p for p in (row.get("location_city"), row.get("location_state")) if p]
    loc = ", ".join(loc_parts) if loc_parts else (row.get("location_raw") or "unknown")
    broker = row.get("is_broker")
    broker_str = "yes" if broker == 1 else "no" if broker == 0 else "unknown"
    desc = row.get("description") or ""
    if len(desc) > 4000:
        desc = desc[:4000] + " [truncated]"

    return USER_PROMPT_TEMPLATE.format(
        source=row.get("source", ""),
        url=row.get("url", ""),
        title=row.get("title") or "(no title)",
        category=row.get("category") or "unknown",
        location=loc,
        price=price_str,
        dom=row.get("days_on_market") if row.get("days_on_market") is not None else "unknown",
        broker=broker_str,
        description=desc,
    )


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _parse_response(text: str) -> Optional[dict]:
    """Best-effort JSON extraction. Tolerates ```json``` fences and leading prose."""
    if not text:
        return None
    # 1) Stripped fenced block.
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 2) Raw JSON (Claude follows "no fences" most of the time).
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    # 3) First {...} block.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


# ---------------------------------------------------------------------------
# Anthropic call
# ---------------------------------------------------------------------------

def _score_one(client, row: dict) -> Optional[dict]:
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _format_prompt(row)}],
    )
    # anthropic returns content as a list of TextBlocks
    text = "".join(getattr(b, "text", "") for b in resp.content)
    parsed = _parse_response(text)
    if parsed is None:
        log.warning("scorer: unparseable response for %s: %s", row["url"], text[:200])
    return parsed


# ---------------------------------------------------------------------------
# DB writeback
# ---------------------------------------------------------------------------

def _writeback(conn: sqlite3.Connection, listing_id: int, result: dict) -> None:
    enrich = result.get("enrichment") or {}
    remote = enrich.get("remote_operable")
    remote_i = 1 if remote is True else 0 if remote is False else None

    conn.execute(
        """
        UPDATE listings SET
            score           = ?,
            score_reasoning = ?,
            top_signals     = ?,
            red_flags       = ?,
            enrichment      = ?,
            remote_operable = ?
        WHERE id = ?
        """,
        (
            result.get("score"),
            result.get("reasoning"),
            json.dumps(result.get("top_signals") or []),
            json.dumps(result.get("red_flags") or []),
            json.dumps(enrich),
            remote_i,
            listing_id,
        ),
    )


# ---------------------------------------------------------------------------
# Top-level run
# ---------------------------------------------------------------------------

def run(*, limit: int = 20, force: bool = False) -> dict:
    """Score up to `limit` listings. By default only NULL-score rows. Returns
    stats: {'considered', 'scored', 'failed'}."""
    load_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Add it to .env (see .env.example)."
        )

    # Import lazily so missing-key failures hit the explicit check above.
    from anthropic import Anthropic

    client = Anthropic()

    where = "" if force else "WHERE score IS NULL"
    sql = f"SELECT * FROM listings {where} ORDER BY id LIMIT ?"

    stats = dict(considered=0, scored=0, failed=0)
    with db.connect() as conn:
        rows = conn.execute(sql, (limit,)).fetchall()
        for r in rows:
            stats["considered"] += 1
            row = dict(r)
            try:
                result = _score_one(client, row)
            except Exception as e:  # noqa: BLE001
                log.warning("scorer: API error on %s: %r", row["url"], e)
                stats["failed"] += 1
                continue
            if not result or "score" not in result:
                stats["failed"] += 1
                continue
            try:
                _writeback(conn, row["id"], result)
            except Exception as e:  # noqa: BLE001
                log.warning("scorer: writeback failed on %s: %r", row["url"], e)
                stats["failed"] += 1
                continue
            stats["scored"] += 1
    return stats
