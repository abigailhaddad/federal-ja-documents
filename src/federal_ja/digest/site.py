"""``python -m federal_ja.analysis digest-site --out site/dist``: the public web
archive + RSS feed of sent digests, https://woah-jas.abigailhaddad.com
(Cloudflare Pages project ``woah-jas``; 2026-10-21).

Input: the archive entries (archive.py), read from the dataset's
``analysis/digest_archive/`` (or ``--archive-dir`` for a local preview).
Output, and NOTHING else (the dist/ directory is wiped and rebuilt every time,
so it is an ALLOW-LIST by construction, the rule the owner's other Pages sites
adopted after two of them published their whole repo root through a
deny-list deploy):

    index.html           every issue, newest first
    issues/<name>.html   one page per issue: the picks with their quotes and
                         links, then the "All new notices" list
    about.html           what this is (the email footer's wording)
    feed.xml             RSS 2.0, the last FEED_ITEMS issues, full content
    style.css            light/dark via prefers-color-scheme
    404.html             Cloudflare Pages serves index.html with HTTP 200 for
                         unknown paths unless a 404.html exists

No JavaScript, no trackers, no external fonts or CDNs. Pages are rendered
from the structured entry (picks, notice rows), not by parsing the Markdown
body, and every piece of text is HTML-escaped: headlines, quotes, titles and
gists come from documents and a model, so they are untrusted. Links are kept
only when they are http(s) URLs. tests/test_site_dist.py scans the built tree
(forbidden files, Cloudflare's 25 MiB / 20,000-file limits, dangling links)
before every deploy.
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import sys
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

from . import archive as ar
from .render import DATASET_URL, ET, FIELDS_NOTE, FIGURE_SOURCE_NOTE, GISTS_NOTE, PROJECT_URL, SITE_URL

TITLE = "WOAH! J&As"
SUBSCRIBE_URL = "https://buttondown.com/abigailhaddad"
TAGLINE = "A daily pick of new federal sole-source justifications (J&As) from SAM.gov"
FEED_ITEMS = 30
# the only files the builder writes (issue pages aside); test_site_dist.py checks the tree independently
FIXED_FILES = ("index.html", "about.html", "feed.xml", "style.css", "404.html")


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", type=Path, required=True, help="output directory, wiped first (must be named dist)")
    p.add_argument("--archive-dir", type=Path, default=None,
                   help=f"read archive entries from this local directory instead of the dataset's {ar.ARCHIVE_DIR}/")


def e(s) -> str:
    return html.escape("" if s is None else str(s), quote=True)


def safe_url(u) -> str | None:
    u = str(u or "").strip()
    return u if re.match(r"^https?://[^\s<>\"']+$", u) else None


def link(u, text) -> str:
    u = safe_url(u)
    return f'<a href="{e(u)}">{e(text)}</a>' if u else e(text)


def issue_slug(name: str) -> str:
    return name[:-5] if name.endswith(".json") else name


def _dt(iso: str | None) -> datetime | None:
    if not iso:
        return None
    d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _date_label(entry: dict) -> str:
    d = datetime.fromisoformat(entry["date"])
    return f"{d:%B} {d.day}, {d.year}"


def _sent_label(entry: dict) -> str:
    d = _dt(entry.get("sent_at"))
    if not d:
        return ""
    d = d.astimezone(ET)
    return f"{d:%b} {d.day}, {d.year}, {d:%H:%M} ET"


def page(title: str, body: str, *, root: str = "", description: str = "") -> str:
    """``root``: the relative path back to the site root ('' or '../')."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(description or TAGLINE)}">
<meta name="referrer" content="no-referrer">
<link rel="stylesheet" href="{root}style.css">
<link rel="alternate" type="application/rss+xml" title="{e(TITLE)}" href="{root}feed.xml">
</head>
<body>
<header class="site"><a class="brand" href="{root}index.html">{e(TITLE)}</a>
<nav><a href="{root}index.html">All issues</a> <a href="{root}about.html">About</a> <a href="{root}feed.xml">RSS</a> <a href="{SUBSCRIBE_URL}">Subscribe by email</a></nav>
</header>
<main>
{body}
</main>
<footer class="site"><p>Chosen and written up by Claude. Quotes are checked word for word against the documents,
but check the source before relying on anything. <a href="{root}about.html">About</a> ·
<a href="{root}index.html">All issues</a> · <a href="{root}feed.xml">RSS</a></p></footer>
</body>
</html>
"""


def _pick_html(p: dict) -> str:
    line = " · ".join(x for x in (p.get("org"), p.get("vendor"), p.get("value"), p.get("authority")) if x)
    bits = [f"Posted {e(p.get('posted_date'))}"]
    if p.get("award_number"):
        bits.append(f"award {e(p['award_number'])}")
    bits.append(f"“{e(p.get('title'))}”")
    out = [f'<article class="pick"><h2><span class="rank">{e(p.get("rank"))}.</span> {e(p.get("headline"))}</h2>',
           f'<p class="facts">{e(line)}</p>' if line else "",
           f'<p class="meta">{", ".join(bits)}</p>',
           f'<p>{e(p.get("why"))}</p>']
    for q in p.get("quotes") or []:
        src = e(FIGURE_SOURCE_NOTE) if q.get("added_by_code") else ""
        out.append(f'<blockquote><p>“{e(q.get("text"))}”</p><footer>— {e(q.get("where"))}{src}</footer></blockquote>')
    links = [link(p.get("sam_link"), "SAM.gov notice")]
    links += [link(d.get("url"), d.get("filename") or "document") for d in p.get("documents") or []]
    if p.get("more_documents"):
        links.append(f"+{e(p['more_documents'])} more on SAM.gov")
    out.append(f'<p class="links">{" · ".join(links)}</p></article>')
    return "\n".join(x for x in out if x)


def _notices_html(rows: list[dict]) -> str:
    items = []
    for r in rows:
        bits = [r.get("org"), r.get("title") or "(no title)", r.get("vendor"), r.get("value"), r.get("authority")]
        star = '<span class="star" title="picked">★</span> ' if r.get("pick_rank") else ""
        gist = f'<br><span class="gist">{e(r["gist"])}</span>' if r.get("gist") else ""
        items.append(f"<li>{star}" + " · ".join(e(b) for b in bits if b)
                     + f" · {link(r.get('sam_link'), 'SAM.gov')}{gist}</li>")
    # issues sent before the one-line summaries existed (2026-10-21's) have no gists
    note = e(FIELDS_NOTE + (" " + GISTS_NOTE if any(r.get("gist") for r in rows) else ""))
    return (f'<section class="notices"><h2>All new notices ({len(rows)})</h2>\n<ul>\n' + "\n".join(items)
            + f'\n</ul>\n<p class="note">{note}</p></section>')


def issue_content(entry: dict) -> str:
    """The issue's body (shared by its page and its RSS item)."""
    picks = entry.get("picks") or []
    n = entry.get("notices_reviewed") or 0
    w = entry.get("window") or {}
    parts = []
    if not picks:
        parts.append(f"<p>Nothing WOAH-worthy today ({e(n)} new notice{'s' if n != 1 else ''} reviewed).</p>")
    parts += [_pick_html(p) for p in picks]
    if entry.get("notices"):
        parts.append(_notices_html(entry["notices"]))
    old = w.get("left_out_old")
    parts.append(f'<p class="note">{e(n)} new notice{"s" if n != 1 else ""} reviewed (first collected '
                 f'{e(w.get("from_label", "?"))} to {e(w.get("to_label", "?"))}'
                 + (f"; {e(old)} older notice{'s' if old != 1 else ''} skipped" if old else "") + ").</p>")
    return "\n".join(parts)


def issue_page(name: str, entry: dict, prev_name: str | None, next_name: str | None) -> str:
    nav = []
    if prev_name:
        nav.append(f'<a href="{e(issue_slug(prev_name))}.html">← Older</a>')
    if next_name:
        nav.append(f'<a href="{e(issue_slug(next_name))}.html">Newer →</a>')
    sent = _sent_label(entry)
    head = (f'<p class="dateline">{e(_date_label(entry))}' + (f" · sent {e(sent)}" if sent else "") + "</p>"
            f"<h1>{e(entry.get('subject'))}</h1>")
    body = head + "\n" + issue_content(entry) + (f'\n<p class="pager">{" · ".join(nav)}</p>' if nav else "")
    return page(entry.get("subject") or TITLE, body, root="../")


def index_page(items: list[tuple[str, dict]]) -> str:
    rows = []
    for name, entry in reversed(items):        # newest first
        n = len(entry.get("picks") or [])
        rows.append(f'<li><span class="date">{e(_date_label(entry))}</span> '
                    f'<a href="issues/{e(issue_slug(name))}.html">{e(entry.get("subject"))}</a> '
                    f'<span class="count">{n} pick{"s" if n != 1 else ""}</span></li>')
    lst = "<ul class=\"issues\">\n" + "\n".join(rows) + "\n</ul>" if rows else "<p>No issues yet.</p>"
    body = (f"<h1>{e(TITLE)}</h1>\n<p class=\"lede\">{e(TAGLINE)}, chosen and written up by Claude. "
            f'<a href="{SUBSCRIBE_URL}">Subscribe by email</a> · <a href="about.html">About</a> · <a href="feed.xml">RSS feed</a></p>\n{lst}')
    return page(TITLE, body)


def about_page() -> str:
    body = f"""<h1>About {e(TITLE)}</h1>
<p>{e(TAGLINE)}, via the public <a href="{e(DATASET_URL)}">federal-ja-documents dataset</a>.
Each issue has the few most remarkable new J&amp;A notices the collector picked up since the previous one
(at most five, sometimes none), each with verbatim quotes from the documents and links to SAM.gov,
then every new notice it reviewed, one line each.</p>
<p><strong>Chosen and written up by Claude.</strong> Quotes are checked word for word against the documents,
and dollar figures come from those quotes or from quote-checked extraction fields,
but AI can still misread a document: check the source before relying on anything.</p>
<p>This site shows the issues exactly as they were sent by email. Code, rules and checks:
<a href="{e(PROJECT_URL)}">github.com/abigailhaddad/federal-ja-documents</a>
(<a href="{e(PROJECT_URL)}/blob/main/docs/digest.md">how the digest works</a>).
Data: <a href="{e(DATASET_URL)}">huggingface.co/datasets/abigailhaddad/federal-ja-documents</a>.</p>
<p>Subscribe with any feed reader: <a href="feed.xml">RSS feed</a>.
No JavaScript, no cookies, no trackers.</p>"""
    return page(f"About · {TITLE}", body)


def not_found_page() -> str:
    # served at any depth, so root-absolute links
    return page(f"Not found · {TITLE}", '<h1>Not found</h1><p><a href="/index.html">All issues</a></p>',
                root="/")


def feed_xml(items: list[tuple[str, dict]], now: datetime | None = None) -> str:
    x = lambda s: html.escape("" if s is None else str(s), quote=False)  # noqa: E731  XML text escaping
    newest = list(reversed(items))[:FEED_ITEMS]
    built = now or max((_dt(en.get("sent_at")) for _, en in newest if en.get("sent_at")),
                       default=datetime(2026, 10, 21, tzinfo=timezone.utc))
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">', "<channel>",
           f"<title>{x(TITLE)}</title>", f"<link>{SITE_URL}/</link>",
           f"<description>{x(TAGLINE)}. Chosen and written up by Claude; check the source.</description>",
           "<language>en-us</language>",
           f'<atom:link href="{SITE_URL}/feed.xml" rel="self" type="application/rss+xml"/>',
           f"<lastBuildDate>{format_datetime(built.astimezone(timezone.utc), usegmt=True)}</lastBuildDate>"]
    for name, en in newest:
        # Cloudflare Pages serves /issues/X.html as a 308 to /issues/X (its
        # "pretty URLs"); the feed uses the final URL so links and guids are
        # canonical (checked on woah-jas.pages.dev, 2026-10-21). The pages'
        # own links keep .html so a local preview works from disk.
        url = f"{SITE_URL}/issues/{issue_slug(name)}"
        d = _dt(en.get("sent_at")) or datetime.fromisoformat(en["date"]).replace(tzinfo=timezone.utc)
        out += ["<item>", f"<title>{x(en.get('subject'))}</title>", f"<link>{url}</link>",
                f'<guid isPermaLink="true">{url}</guid>',
                f"<pubDate>{format_datetime(d.astimezone(timezone.utc), usegmt=True)}</pubDate>",
                # the HTML is escaped once more as XML text: no CDATA, so a "]]>" in a quote can't break it
                f"<description>{x(issue_content(en))}</description>", "</item>"]
    out += ["</channel>", "</rss>", ""]
    return "\n".join(out)


CSS = """:root{--bg:#fdfcf9;--fg:#1d1d1f;--muted:#5f6368;--line:#e3e1da;--accent:#9a3412;--quote:#f4f1ea}
@media (prefers-color-scheme: dark){:root{--bg:#141414;--fg:#e8e6e1;--muted:#a3a19c;--line:#2e2d2a;--accent:#fb923c;--quote:#1f1e1c}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:17px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}
header.site,main,footer.site{max-width:46rem;margin:0 auto;padding:0 16px}
header.site{display:flex;flex-wrap:wrap;gap:.5rem 1.5rem;align-items:baseline;justify-content:space-between;padding-top:1rem;padding-bottom:.75rem;border-bottom:1px solid var(--line)}
.brand{font-weight:800;font-size:1.2rem;text-decoration:none;color:var(--fg)}
nav a{margin-right:1rem;color:var(--muted)}
a{color:var(--accent);overflow-wrap:anywhere}
h1{font-size:1.6rem;line-height:1.25;margin:1.5rem 0 .75rem}
h2{font-size:1.2rem;line-height:1.3;margin:2rem 0 .5rem}
.rank{color:var(--muted)}
.facts{font-weight:600;margin:.25rem 0}
.meta,.dateline,.note,.count,.date,.gist{color:var(--muted)}
.meta{font-style:italic;margin:.25rem 0 .75rem}
.dateline{margin:1.5rem 0 0;font-size:.95rem}
blockquote{margin:.75rem 0;padding:.5rem 1rem;background:var(--quote);border-left:3px solid var(--accent)}
blockquote p{margin:0}
blockquote footer{color:var(--muted);font-size:.9rem;margin-top:.25rem;overflow-wrap:anywhere}
.links{font-size:.95rem}
.pick{border-bottom:1px solid var(--line);padding-bottom:1rem}
.notices ul,.issues{padding-left:1.1rem}
.notices li{margin:.4rem 0;font-size:.95rem}
.issues li{margin:.6rem 0}
.date{display:block;font-size:.9rem}
.count{font-size:.9rem;white-space:nowrap}
.note{font-size:.9rem}
.star{color:var(--accent)}
.pager{margin:2rem 0}
footer.site{margin-top:3rem;padding-top:1rem;padding-bottom:2rem;border-top:1px solid var(--line);color:var(--muted);font-size:.9rem}
"""


def build(items: list[tuple[str, dict]], out: Path, now: datetime | None = None) -> list[str]:
    """Write the site to ``out`` (wiped first). ``items``: (archive file name,
    entry), any order. Returns the files written, relative to ``out``."""
    out = Path(out)
    if out.name != "dist":
        raise SystemExit(f"refusing to wipe {out}: the output directory must be named dist")
    items = sorted(items, key=lambda t: ar.sort_key(t[0]))
    if out.exists():
        shutil.rmtree(out)
    (out / "issues").mkdir(parents=True)
    files = {"index.html": index_page(items), "about.html": about_page(), "feed.xml": feed_xml(items, now),
             "style.css": CSS, "404.html": not_found_page()}
    for i, (name, entry) in enumerate(items):
        prev_name = items[i - 1][0] if i > 0 else None
        next_name = items[i + 1][0] if i + 1 < len(items) else None
        files[f"issues/{issue_slug(name)}.html"] = issue_page(name, entry, prev_name, next_name)
    for rel, text in files.items():
        (out / rel).write_text(text, encoding="utf-8")
    return sorted(files)


def run(a) -> int:
    store = ar.LocalArchive(a.archive_dir) if a.archive_dir else ar.HFArchive()
    items = store.load_all()
    files = build(items, a.out)
    print(f"digest-site: {len(items)} issue(s), {len(files)} files -> {a.out}", file=sys.stderr)
    return 0
