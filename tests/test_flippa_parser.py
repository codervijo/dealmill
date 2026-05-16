"""dealmill.scrapers.flippa.parse_detail — against committed HTML fixtures.

Three fixtures saved 2026-05-15 from real Flippa /search detail pages:
ecommerce_design, website_education, ecommerce_hobbies. Each is ~450KB
because Flippa is a JS SPA and Playwright captures the full hydrated DOM.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dealmill.scrapers.flippa import parse_detail

FIXTURES = Path(__file__).parent / "fixtures" / "flippa"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Per-fixture spot checks
# ---------------------------------------------------------------------------

def test_ecommerce_design_has_title_and_price():
    row = parse_detail("https://flippa.com/12668272", load("ecommerce_design.html"))
    assert row.get("title")
    assert "Ecommerce Store" in row["title"]  # confirmed via DB query
    assert row.get("asking_price_cents") == 3_000_000 * 100  # $3,000,000


def test_website_education_price():
    row = parse_detail("https://flippa.com/12525555", load("website_education.html"))
    assert row.get("asking_price_cents") == 348_800 * 100  # $348,800


# ---------------------------------------------------------------------------
# Shared invariants
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fixture_name", [
    "ecommerce_design.html", "website_education.html", "ecommerce_hobbies.html",
])
def test_source_is_flippa(fixture_name):
    row = parse_detail("https://flippa.com/12345", load(fixture_name))
    assert row["source"] == "flippa"


@pytest.mark.parametrize("fixture_name", [
    "ecommerce_design.html", "website_education.html", "ecommerce_hobbies.html",
])
def test_category_is_online_business(fixture_name):
    row = parse_detail("https://flippa.com/12345", load(fixture_name))
    assert row["category"].startswith("online business")


@pytest.mark.parametrize("fixture_name", [
    "ecommerce_design.html", "website_education.html", "ecommerce_hobbies.html",
])
def test_is_broker_false_on_flippa(fixture_name):
    row = parse_detail("https://flippa.com/12345", load(fixture_name))
    assert row["is_broker"] is False


@pytest.mark.parametrize("fixture_name", [
    "ecommerce_design.html", "website_education.html", "ecommerce_hobbies.html",
])
def test_raw_html_preserved(fixture_name):
    html = load(fixture_name)
    row = parse_detail("https://flippa.com/12345", html)
    assert row["raw_html"] == html.encode("utf-8")


@pytest.mark.parametrize("fixture_name", [
    "ecommerce_design.html", "website_education.html", "ecommerce_hobbies.html",
])
def test_url_round_trips(fixture_name):
    url = "https://flippa.com/12345678"
    row = parse_detail(url, load(fixture_name))
    assert row["url"] == url


@pytest.mark.parametrize("fixture_name", [
    "ecommerce_design.html", "website_education.html", "ecommerce_hobbies.html",
])
def test_price_is_parsed(fixture_name):
    """Every fixture should yield a non-NULL asking price."""
    row = parse_detail("https://flippa.com/12345", load(fixture_name))
    assert row.get("asking_price_cents") is not None
    assert row["asking_price_cents"] > 0


@pytest.mark.parametrize("fixture_name", [
    "ecommerce_design.html", "website_education.html", "ecommerce_hobbies.html",
])
def test_no_physical_location(fixture_name):
    """Online businesses have no physical location — parser should leave fields NULL."""
    row = parse_detail("https://flippa.com/12345", load(fixture_name))
    assert row.get("location_city") in (None, "")
    assert row.get("location_state") in (None, "")
