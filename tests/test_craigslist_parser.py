"""dealmill.scrapers.craigslist.parse_detail — against committed HTML fixtures.

Fixtures are real BBS-free CL detail pages saved 2026-05-15 (3 listings:
Cupertino garment bag, San Leandro cake machine, San Leandro concrete hammer).
Regenerated from `data/dealmill.sqlite` raw_html column when the parser changes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dealmill.scrapers.craigslist import parse_detail

FIXTURES = Path(__file__).parent / "fixtures" / "craigslist"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# garment_bag.html — owner-listed, 97 days on market
# ---------------------------------------------------------------------------

def test_garment_bag_title():
    row = parse_detail(
        "https://sfbay.craigslist.org/sby/bfs/d/cupertino-garment-bag-town-floral/7913393308.html",
        load("garment_bag.html"),
    )
    assert row["title"] == "Garment Bag: TOWN- floral"


def test_garment_bag_price():
    row = parse_detail("https://x/y/bfs/d/z/1.html", load("garment_bag.html"))
    assert row["asking_price_cents"] == 2600


def test_garment_bag_location():
    row = parse_detail("https://x/y/bfs/d/z/1.html", load("garment_bag.html"))
    assert row["location_city"] == "Cupertino"
    assert row["location_state"] == "CA"


def test_garment_bag_listing_date():
    row = parse_detail("https://x/y/bfs/d/z/1.html", load("garment_bag.html"))
    assert row["listing_date"] == "2026-02-06"


# ---------------------------------------------------------------------------
# cake_machine.html — different city, recent listing
# ---------------------------------------------------------------------------

def test_cake_machine_title_extracted():
    row = parse_detail("https://x/y/bfs/d/z/1.html", load("cake_machine.html"))
    assert row.get("title")  # non-empty
    assert "Birthday" in row["title"]  # known unique substring


def test_cake_machine_location_san_leandro():
    """LD+JSON has San Leandro; meta tag has 'Hayward,CA 94545'. LD+JSON wins."""
    row = parse_detail("https://x/y/bfs/d/z/1.html", load("cake_machine.html"))
    assert row["location_city"] == "San Leandro"
    assert row["location_state"] == "CA"


# ---------------------------------------------------------------------------
# Shared invariants on all three fixtures
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fixture_name", [
    "garment_bag.html", "cake_machine.html", "concrete_hammer.html",
])
def test_source_is_craigslist(fixture_name):
    row = parse_detail("https://x/y/bfs/d/z/1.html", load(fixture_name))
    assert row["source"] == "craigslist"


@pytest.mark.parametrize("fixture_name", [
    "garment_bag.html", "cake_machine.html", "concrete_hammer.html",
])
def test_raw_html_preserved(fixture_name):
    html = load(fixture_name)
    row = parse_detail("https://x/y/bfs/d/z/1.html", html)
    assert row["raw_html"] == html.encode("utf-8")


@pytest.mark.parametrize("fixture_name", [
    "garment_bag.html", "cake_machine.html", "concrete_hammer.html",
])
def test_is_broker_false_on_cl(fixture_name):
    """CL is direct-seller by default convention in our parser."""
    row = parse_detail("https://x/y/bfs/d/z/1.html", load(fixture_name))
    assert row["is_broker"] is False


@pytest.mark.parametrize("fixture_name", [
    "garment_bag.html", "cake_machine.html", "concrete_hammer.html",
])
def test_url_round_trips(fixture_name):
    url = "https://sfbay.craigslist.org/eby/bfs/d/test-listing/1234567890.html"
    row = parse_detail(url, load(fixture_name))
    assert row["url"] == url


@pytest.mark.parametrize("fixture_name", [
    "garment_bag.html", "cake_machine.html", "concrete_hammer.html",
])
def test_price_is_parsed(fixture_name):
    row = parse_detail("https://x/y/bfs/d/z/1.html", load(fixture_name))
    assert row.get("asking_price_cents") is not None
    assert row["asking_price_cents"] > 0


@pytest.mark.parametrize("fixture_name", [
    "garment_bag.html", "cake_machine.html", "concrete_hammer.html",
])
def test_category_is_business_for_sale(fixture_name):
    row = parse_detail("https://sfbay.craigslist.org/sby/bfs/d/x/1.html", load(fixture_name))
    assert row["category"] == "business for sale"
