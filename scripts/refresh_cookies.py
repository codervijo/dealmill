#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "browser-cookie3>=0.20",
# ]
# ///
"""Host-side cookie refresh. Auto-detects browser cookie databases on disk
(Chrome, Chromium, snap-Chromium, Brave, Edge, Firefox) and writes one JSON
file per source under `cookies/`, in Playwright's `add_cookies()` shape.

Must run on the HOST (not inside dmill1). Requires:
- `uv` on the host (https://docs.astral.sh/uv/getting-started/installation/)
- A browser logged into the target sites
- OS keyring unlocked, OR cookies stored with the v10 "peanuts" fallback (the
  case for most snap-packaged Chromium installs)

Usage:
    ./scripts/refresh_cookies.py                # all configured sources
    ./scripts/refresh_cookies.py bizbuysell     # one source
    make refresh-cookies                        # all (via Makefile)
    make refresh-cookies ARGS="bizbuysell"      # one (via Makefile)
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

import browser_cookie3


# Sources we extract for; value is the cookie-domain suffix.
SOURCES: dict[str, str] = {
    "bizbuysell": ".bizbuysell.com",
    "loopnet":    ".loopnet.com",
    "facebook":   ".facebook.com",
    "crexi":      ".crexi.com",
}

# Per-source URL to open when no cookies are found. Each is the site homepage —
# every one prompts a sign-in when the user isn't authenticated.
LOGIN_URLS: dict[str, str] = {
    "bizbuysell": "https://www.bizbuysell.com/",
    "loopnet":    "https://www.loopnet.com/",
    "facebook":   "https://www.facebook.com/",
    "crexi":      "https://www.crexi.com/",
}

PROJECT_ROOT = Path(__file__).resolve().parent.parent
COOKIES_DIR = PROJECT_ROOT / "cookies"


# Each entry: (browser_cookie3 function name, list of glob patterns).
# Globs catch arbitrary profile dirs (Default, Profile 1, Person 2, etc.).
# Newer Chromium variants use the Network/ subdir.
_BROWSER_DB_GLOBS: list[tuple[str, list[str]]] = [
    ("chrome", [
        "~/.config/google-chrome/*/Cookies",
        "~/.config/google-chrome/*/Network/Cookies",
        "~/snap/chrome/common/chrome/*/Cookies",
        "~/snap/chrome/common/chrome/*/Network/Cookies",
    ]),
    ("chromium", [
        "~/.config/chromium/*/Cookies",
        "~/.config/chromium/*/Network/Cookies",
        "~/snap/chromium/common/chromium/*/Cookies",
        "~/snap/chromium/common/chromium/*/Network/Cookies",
    ]),
    ("brave", [
        "~/.config/BraveSoftware/Brave-Browser/*/Cookies",
        "~/.config/BraveSoftware/Brave-Browser/*/Network/Cookies",
    ]),
    ("edge", [
        "~/.config/microsoft-edge/*/Cookies",
        "~/.config/microsoft-edge/*/Network/Cookies",
    ]),
]

# Chromium creates these alongside real profiles; nothing useful in them.
_PSEUDO_PROFILES = {"System Profile", "Guest Profile", "Crash Reports"}


def _profile_name(cookies_path: Path) -> str:
    """Cookies path -> profile name. Handles both Default/Cookies and
    Default/Network/Cookies layouts."""
    p = cookies_path.parent
    if p.name == "Network":
        p = p.parent
    return p.name


def _detect_dbs() -> list[tuple[str, Path]]:
    """Find every existing cookie DB on disk (all profiles)."""
    found: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for fn_name, patterns in _BROWSER_DB_GLOBS:
        for pat in patterns:
            for raw in glob.glob(os.path.expanduser(pat)):
                p = Path(raw)
                if p in seen:
                    continue
                if _profile_name(p) in _PSEUDO_PROFILES:
                    continue
                seen.add(p)
                found.append((fn_name, p))
    # Firefox lives in three common spots on Linux:
    #   apt/deb:  ~/.mozilla/firefox/
    #   snap:     ~/snap/firefox/common/.mozilla/firefox/
    #   flatpak:  ~/.var/app/org.mozilla.firefox/.mozilla/firefox/
    ff_bases = [
        Path(os.path.expanduser("~/.mozilla/firefox")),
        Path(os.path.expanduser("~/snap/firefox/common/.mozilla/firefox")),
        Path(os.path.expanduser("~/.var/app/org.mozilla.firefox/.mozilla/firefox")),
    ]
    for ff_base in ff_bases:
        if ff_base.exists():
            for sqlite_file in ff_base.glob("*/cookies.sqlite"):
                found.append(("firefox", sqlite_file))
    return found


def _copy_sqlite_with_wal(src: Path, dst_dir: Path) -> Path:
    """Copy a SQLite DB plus its WAL / SHM / journal siblings. Returns the
    path to the copied main DB. SQLite reads the WAL automatically when it
    finds it alongside, so this captures uncommitted writes from a running
    browser (Firefox uses WAL, Chromium since recent versions too)."""
    for suffix in ("", "-wal", "-shm", "-journal"):
        src_file = Path(str(src) + suffix)
        if src_file.exists():
            shutil.copy2(src_file, dst_dir / src_file.name)
    return dst_dir / src.name


def _list_hosts(db_path: Path) -> list[str]:
    """Return distinct host values in a cookies DB. Handles both chromium-style
    (`cookies` table, `host_key` column) and firefox-style (`moz_cookies`,
    `host`) layouts. Copies WAL/SHM so recently-set cookies are visible."""
    is_firefox = db_path.name == "cookies.sqlite"
    table = "moz_cookies" if is_firefox else "cookies"
    col = "host" if is_firefox else "host_key"
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            copied = _copy_sqlite_with_wal(db_path, Path(tmpdir))
            conn = sqlite3.connect(str(copied))
            try:
                cur = conn.execute(f"SELECT DISTINCT {col} FROM {table} ORDER BY {col}")
                return [row[0] for row in cur.fetchall()]
            finally:
                conn.close()
    except Exception:
        return []


def _read_firefox_cookies(db_path: Path, domain_suffix: str) -> list[dict]:
    """Read Firefox cookies for a domain directly via SQLite. Firefox cookies
    are unencrypted; no browser_cookie3 / OS-keyring dance needed. Handles
    WAL by copying main+wal+shm together."""
    with tempfile.TemporaryDirectory() as tmpdir:
        copied = _copy_sqlite_with_wal(db_path, Path(tmpdir))
        if not copied.exists():
            return []
        conn = sqlite3.connect(str(copied))
        try:
            sql = (
                "SELECT host, name, value, path, expiry, isSecure, isHttpOnly, sameSite "
                "FROM moz_cookies WHERE host LIKE ?"
            )
            rows = conn.execute(sql, (f"%{domain_suffix}%",)).fetchall()
        finally:
            conn.close()
    samesite_map = {0: "None", 1: "Lax", 2: "Strict"}
    out: list[dict] = []
    for host, name, value, path, expiry, is_secure, is_http_only, same_site in rows:
        out.append({
            "name":     name,
            "value":    value,
            "domain":   host,
            "path":     path or "/",
            "expires":  float(expiry) if expiry else -1,
            "secure":   bool(is_secure),
            "httpOnly": bool(is_http_only),
            "sameSite": samesite_map.get(same_site, "Lax"),
        })
    return out


def _short(p: Path) -> str:
    home = os.path.expanduser("~")
    s = str(p)
    return s.replace(home, "~", 1) if s.startswith(home) else s


def _samesite(cookie) -> str:
    rest = getattr(cookie, "_rest", None) or {}
    val = str(rest.get("SameSite") or rest.get("sameSite") or "").strip().lower()
    if val == "strict":
        return "Strict"
    if val == "none":
        return "None"
    return "Lax"


def _http_only(cookie) -> bool:
    rest = getattr(cookie, "_rest", None) or {}
    return "HttpOnly" in rest or "httponly" in rest


def _to_dict(c) -> dict:
    return {
        "name":     c.name,
        "value":    c.value,
        "domain":   c.domain,
        "path":     c.path or "/",
        "expires":  float(c.expires) if c.expires else -1,
        "secure":   bool(c.secure),
        "httpOnly": _http_only(c),
        "sameSite": _samesite(c),
    }


def extract(domain_suffix: str) -> tuple[list[dict], list[str]]:
    """Try every detected browser cookie DB. Returns (cookies, errors).

    Errors from any single DB are caught and reported; we continue to the
    next DB. Keyring decryption failures show up here.
    """
    cookies: list[dict] = []
    errors: list[str] = []

    dbs = _detect_dbs()
    if not dbs:
        return cookies, ["no browser cookie DBs detected under ~/.config/ or ~/snap/ or ~/.mozilla/"]

    seen_names: set[tuple[str, str]] = set()  # dedupe (name, domain) across browsers
    for fn_name, cookie_file in dbs:
        try:
            if fn_name == "firefox":
                # Custom path: copy WAL+SHM, read SQLite directly. Firefox cookies
                # are unencrypted, so browser_cookie3 isn't needed here — and
                # bypassing it lets us actually see WAL contents from a live
                # browser session.
                new_cookies = _read_firefox_cookies(cookie_file, domain_suffix)
                for cd in new_cookies:
                    key = (cd["name"], cd["domain"])
                    if key in seen_names:
                        continue
                    seen_names.add(key)
                    cookies.append(cd)
                continue

            fn = getattr(browser_cookie3, fn_name, None)
            if not fn:
                continue
            jar = fn(domain_name=domain_suffix, cookie_file=str(cookie_file))
            for c in jar:
                key = (c.name, c.domain)
                if key in seen_names:
                    continue
                seen_names.add(key)
                cookies.append(_to_dict(c))
        except Exception as e:
            errors.append(f"     {fn_name} @ {_short(cookie_file)}: {type(e).__name__}: {e}")

    return cookies, errors


def _open_in_browser(url: str) -> bool:
    """Try a few common launchers; return True if one succeeded."""
    for cmd in (["firefox", url], ["xdg-open", url]):
        try:
            subprocess.Popen(
                cmd, start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return True
        except FileNotFoundError:
            continue
    return False


def _try_write(src: str) -> bool:
    """Try to extract + write cookies for one source. Returns True on success."""
    domain = SOURCES[src]
    cookies, errors = extract(domain)
    for err in errors:
        print(err)
    if not cookies:
        print(f"  !  {src}: no cookies found for {domain}")
        return False
    out_path = COOKIES_DIR / f"{src}.json"
    out_path.write_text(json.dumps(cookies, indent=2))
    rel = out_path.relative_to(PROJECT_ROOT)
    print(f"  +  {src}: {len(cookies):3d} cookies -> {rel}")
    return True


def refresh(names: Iterable[str], *, launch: bool = True) -> int:
    COOKIES_DIR.mkdir(parents=True, exist_ok=True)
    detected = _detect_dbs()
    if detected:
        print(f"  browsers detected ({len(detected)} profile{'' if len(detected)==1 else 's'}):")
        for fn_name, p in detected:
            print(f"     - {fn_name} [{_profile_name(p)}]: {_short(p)}")
    else:
        print("  ! no browser cookie DBs found anywhere; nothing to do")
        return 1

    valid_names = []
    for src in names:
        if src not in SOURCES:
            print(f"  ?  unknown source '{src}', skipping (valid: {', '.join(SOURCES)})")
            continue
        valid_names.append(src)

    # Pass 1
    missing = [src for src in valid_names if not _try_write(src)]

    # Optional interactive re-attempt: open browser for each missing source,
    # wait for the user to log in, then retry.
    if missing and launch and sys.stdin.isatty():
        urls = [LOGIN_URLS[s] for s in missing if s in LOGIN_URLS]
        if urls:
            print(f"\n  opening browser to log in to: {', '.join(missing)}")
            for url in urls:
                ok = _open_in_browser(url)
                print(f"     {'opened' if ok else 'failed to open'} {url}")
            try:
                input("\n  press Enter once you've logged in to each (Ctrl-C to abort) ... ")
                # Pass 2
                missing = [src for src in missing if not _try_write(src)]
            except (EOFError, KeyboardInterrupt):
                print("\n  aborted")

    if missing:
        print(f"\n  still no cookies for: {', '.join(missing)}")
        print(f"  hint: run `./scripts/refresh_cookies.py --diagnose` to see which DBs have what")
    return len(missing)


def diagnose() -> int:
    """List the domains present in each detected cookie DB. Helps figure out
    which browser/profile has the target site's cookies."""
    detected = _detect_dbs()
    if not detected:
        print("  ! no browser cookie DBs detected")
        return 1
    target_suffixes = list(SOURCES.values())
    for fn_name, p in detected:
        hosts = _list_hosts(p)
        print(f"\n  [{fn_name}] {_short(p)}  ({len(hosts)} distinct hosts)")
        matches = [h for h in hosts if any(s in h for s in target_suffixes)]
        if matches:
            for h in matches:
                print(f"     + matches a tracked source: {h}")
        else:
            print(f"     - no tracked-source domains in this DB")
            # show a sample so user can sanity-check
            for h in hosts[:8]:
                print(f"       e.g. {h}")
            if len(hosts) > 8:
                print(f"       ... and {len(hosts) - 8} more")
    return 0


def main() -> int:
    argv = list(sys.argv[1:])
    if "--diagnose" in argv:
        return diagnose()
    launch = "--no-launch" not in argv
    positional = [a for a in argv if not a.startswith("--")]
    names = positional if positional else list(SOURCES.keys())
    failures = refresh(names, launch=launch)
    return 1 if failures and not positional else 0


if __name__ == "__main__":
    sys.exit(main())
