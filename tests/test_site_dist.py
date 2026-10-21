"""The deploy gate for the web archive (https://woah-jas.abigailhaddad.com).

Same rule as the owner's other Cloudflare Pages sites (2026-10-21): the deploy
publishes an ALLOW-LIST directory built from scratch, and this file scans the
built tree ON DISK before anything is uploaded. It deliberately does not
import the builder's file list or re-derive its expectations from site.py: a
builder bug (a stray copy, a wrong path) shows up as a real file here and
fails the scan. Two sibling sites once published their whole repo root
(scripts, CI config, CLAUDE.md) through a deny-list deploy.

    SITE_DIST=site/dist uv run pytest -q tests/test_site_dist.py   # the real build (workflows)
    uv run pytest -q tests/test_site_dist.py                       # a build from fixture entries

The workflows run it on the dist/ they are about to deploy; if it fails,
nothing is uploaded.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

import pytest

MAX_FILE_BYTES = 26_214_400     # Cloudflare Pages: 25 MiB per file
MAX_FILE_COUNT = 20_000         # Cloudflare Pages: files per deploy
ALLOWED_SUFFIXES = {".html", ".css", ".xml", ".txt", ".svg", ".ico"}
FORBIDDEN_NAME_GLOBS = ["*.py", "*.pyc", "*.pyo", "*.parquet", "*.csv", "*.json", "*.jsonl", "*.md", "*.yml",
                        "*.yaml", "*.toml", "*.lock", ".DS_Store", ".env", "*.env", ".gitignore", "*.sh"]
FORBIDDEN_DIR_NAMES = {"tests", "src", "data", "analysis", "work", "docs", "__pycache__", ".git", ".github",
                       ".venv", ".pytest_cache", "node_modules", ".wrangler"}
TOP_LEVEL = {"index.html", "about.html", "feed.xml", "style.css", "404.html", "issues"}
ISSUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d+)?\.html$")
REF_RE = re.compile(r"""\b(?:href|src)\s*=\s*(['"])(?P<url>[^'"]*)\1""", re.I)
SKIP = ("http://", "https://", "mailto:")


@pytest.fixture(scope="module")
def dist(tmp_path_factory) -> Path:
    env = os.environ.get("SITE_DIST")
    if env:
        d = Path(env)
        assert d.is_dir(), f"SITE_DIST={env} is not a directory"
        return d
    from federal_ja.digest import site
    from .test_digest_site import entry
    d = tmp_path_factory.mktemp("site") / "dist"
    site.build([("2026-10-20.json", entry("2026-10-20")), ("2026-10-21.json", entry("2026-10-21", evil=True))], d)
    return d


def files(d: Path) -> list[Path]:
    return [p for p in d.rglob("*") if p.is_file() or p.is_symlink()]


def test_only_the_site_files_are_there(dist):
    top = {p.name for p in dist.iterdir()}
    assert top <= TOP_LEVEL, f"unexpected top-level entries: {sorted(top - TOP_LEVEL)}"
    assert {"index.html", "about.html", "feed.xml", "style.css", "404.html"} <= top
    for p in files(dist):
        rel = p.relative_to(dist)
        if rel.parts[0] == "issues":
            assert len(rel.parts) == 2 and ISSUE_RE.match(rel.name), f"unexpected file {rel}"


def test_no_forbidden_files_or_directories(dist):
    bad = []
    for p in dist.rglob("*"):
        rel = p.relative_to(dist)
        if p.is_symlink():
            bad.append(f"{rel} (symlink)")
        if any(part in FORBIDDEN_DIR_NAMES for part in rel.parts[:-1] + ((rel.name,) if p.is_dir() else ())):
            bad.append(f"{rel} (forbidden directory)")
        if p.is_file():
            if p.suffix.lower() not in ALLOWED_SUFFIXES:
                bad.append(f"{rel} (type {p.suffix or 'none'} not allowed)")
            if any(fnmatch.fnmatch(p.name, g) for g in FORBIDDEN_NAME_GLOBS) or p.name.startswith("."):
                bad.append(f"{rel} (forbidden name)")
    assert not bad, "forbidden in dist/:\n  " + "\n  ".join(bad)


def test_cloudflare_limits(dist):
    fs = files(dist)
    assert 0 < len(fs) < MAX_FILE_COUNT
    big = [(str(p.relative_to(dist)), p.stat().st_size) for p in fs if p.stat().st_size > MAX_FILE_BYTES]
    assert not big, f"over 25 MiB: {big}"


def test_every_local_link_resolves_inside_dist(dist):
    broken = []
    for p in files(dist):
        if p.suffix != ".html":
            continue
        for m in REF_RE.finditer(p.read_text(encoding="utf-8")):
            u = m.group("url").split("#")[0].split("?")[0]
            if not u or u.startswith(SKIP):
                continue
            t = (dist / u.lstrip("/")) if u.startswith("/") else (p.parent / u)
            try:
                t.resolve().relative_to(dist.resolve())
            except ValueError:
                broken.append(f"{p.relative_to(dist)} -> {u} (escapes dist/)")
                continue
            if not t.is_file():
                broken.append(f"{p.relative_to(dist)} -> {u} (missing)")
    assert not broken, "\n".join(broken)


def test_no_scripts_trackers_or_external_assets(dist):
    for p in files(dist):
        if p.suffix not in (".html", ".css"):
            continue
        t = p.read_text(encoding="utf-8")
        assert not re.search(r"<script\b", t, re.I), f"{p.name}: <script>"
        assert not re.search(r"<(link|img|iframe)\b[^>]*\b(href|src)=[\"']https?://", t, re.I), \
            f"{p.name}: external asset"
        assert "@import" not in t and not re.search(r"url\(\s*['\"]?https?:", t), f"{p.name}: external CSS"


def test_every_page_links_home_and_to_the_feed(dist):
    for p in files(dist):
        if p.suffix == ".html":
            t = p.read_text(encoding="utf-8")
            assert re.search(r'href="(\.\./|/)?index\.html"', t) and re.search(r'href="(\.\./|/)?feed\.xml"', t), p
    assert all(ref.startswith("/") for ref in (m.group("url") for m in REF_RE.finditer(
        (dist / "404.html").read_text())) if not ref.startswith(SKIP)), "404.html needs root-absolute links"
