"""dealmill.cookies — load semantics, env-var mapping, graceful failure modes."""

from __future__ import annotations

import json

import pytest

from dealmill import cookies


# ---------------------------------------------------------------------------
# env_var_for
# ---------------------------------------------------------------------------

def test_env_var_known_sources():
    assert cookies.env_var_for("bizbuysell") == "BBS_COOKIES_FILE"
    assert cookies.env_var_for("loopnet") == "LOOPNET_COOKIES_FILE"
    assert cookies.env_var_for("facebook") == "FB_COOKIES_FILE"


def test_env_var_case_insensitive():
    assert cookies.env_var_for("BIZBUYSELL") == "BBS_COOKIES_FILE"


def test_env_var_unknown_source():
    assert cookies.env_var_for("nonsense") is None


# ---------------------------------------------------------------------------
# load_cookies_for — graceful failure modes
# ---------------------------------------------------------------------------

def test_load_returns_empty_when_env_unset(monkeypatch):
    monkeypatch.delenv("BBS_COOKIES_FILE", raising=False)
    assert cookies.load_cookies_for("bizbuysell") == []


def test_load_returns_empty_when_env_blank(monkeypatch):
    monkeypatch.setenv("BBS_COOKIES_FILE", "   ")
    assert cookies.load_cookies_for("bizbuysell") == []


def test_load_returns_empty_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("BBS_COOKIES_FILE", str(tmp_path / "noexist.json"))
    assert cookies.load_cookies_for("bizbuysell") == []


def test_load_returns_empty_on_malformed_json(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not valid json {{{")
    monkeypatch.setenv("BBS_COOKIES_FILE", str(bad))
    assert cookies.load_cookies_for("bizbuysell") == []


def test_load_returns_empty_when_root_not_array(monkeypatch, tmp_path):
    f = tmp_path / "obj.json"
    f.write_text('{"name": "x", "value": "y"}')  # object, not array
    monkeypatch.setenv("BBS_COOKIES_FILE", str(f))
    assert cookies.load_cookies_for("bizbuysell") == []


def test_load_returns_empty_for_unknown_source(monkeypatch):
    assert cookies.load_cookies_for("nonsense") == []


# ---------------------------------------------------------------------------
# load_cookies_for — happy path + validation
# ---------------------------------------------------------------------------

def test_load_valid_cookies(monkeypatch, tmp_path):
    data = [
        {"name": "a", "value": "1", "domain": ".x.com"},
        {"name": "b", "value": "2", "domain": ".x.com"},
    ]
    f = tmp_path / "cookies.json"
    f.write_text(json.dumps(data))
    monkeypatch.setenv("BBS_COOKIES_FILE", str(f))
    result = cookies.load_cookies_for("bizbuysell")
    assert len(result) == 2
    assert {c["name"] for c in result} == {"a", "b"}


def test_load_drops_invalid_entries(monkeypatch, tmp_path):
    """Entries missing name+value (or wrong type) get filtered, not crash."""
    data = [
        {"name": "good", "value": "1"},
        {"name": "", "value": "2"},          # invalid: empty name
        {"value": "no name"},                  # invalid: missing name
        "not a dict",                          # invalid: wrong type
        {"name": "good2", "value": "2"},
        {"name": "no value"},                  # invalid: missing value
    ]
    f = tmp_path / "cookies.json"
    f.write_text(json.dumps(data))
    monkeypatch.setenv("BBS_COOKIES_FILE", str(f))
    result = cookies.load_cookies_for("bizbuysell")
    assert {c["name"] for c in result} == {"good", "good2"}


def test_load_resolves_relative_paths(monkeypatch, tmp_path):
    """Relative paths in env var should resolve against project root."""
    # Write a cookies file inside the project root's cookies/ dir (using a
    # unique test name so we don't collide with real fixtures).
    from dealmill.cookies import _PROJECT_ROOT
    rel_path = "cookies/__test_cookies_loader__.json"
    target = _PROJECT_ROOT / rel_path
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps([{"name": "k", "value": "v"}]))
        monkeypatch.setenv("BBS_COOKIES_FILE", rel_path)
        result = cookies.load_cookies_for("bizbuysell")
        assert len(result) == 1
        assert result[0]["name"] == "k"
    finally:
        if target.exists():
            target.unlink()
