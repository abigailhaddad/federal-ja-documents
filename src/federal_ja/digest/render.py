"""The email: a subject line and a Markdown body (Buttondown renders Markdown)."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .candidates import Candidate

ET = ZoneInfo("America/New_York")
PREFIX = "WOAH! J&As"
DATASET_URL = "https://huggingface.co/datasets/abigailhaddad/federal-ja-documents"
PROJECT_URL = "https://github.com/abigailhaddad/federal-ja-documents"
# The public web archive + RSS feed of sent digests (site.py, Cloudflare Pages;
# 2026-10-21).
SITE_URL = "https://woah-jas.abigailhaddad.com"
SUBSCRIBE_URL = "https://buttondown.com/abigailhaddad"
MAX_DOC_LINKS = 3


def day_label(d: datetime) -> str:
    d = d.astimezone(ET)
    return f"{d:%b} {d.day}"


def subject(picks: list[dict], when: datetime) -> str:
    top = picks[0]["headline"].rstrip(".") if picks else "nothing WOAH-worthy"
    return f"{PREFIX} — {day_label(when)}: {top}"


def _md_escape_line(s: str) -> str:
    # Keep a stray "#" / ">" / "-" at a line start from turning into markup.
    return s.replace("\n", " ").strip()


def _fmt_et(iso: str | None) -> str:
    if not iso:
        return "?"
    d = datetime.fromisoformat(iso).astimezone(ET)
    return f"{d:%b} {d.day}, {d:%H:%M} ET"


def footer(n_reviewed: int, window: dict) -> str:
    old = window.get("left_out_old")
    return (
        "---\n\n"
        f"*WOAH! J&As:* a daily pick of new federal sole-source justifications (J&As) from SAM.gov, via the "
        f"[federal-ja-documents dataset]({DATASET_URL}). {n_reviewed} new notice{'s' if n_reviewed != 1 else ''} "
        f"reviewed today (first collected {window.get('from_label', '?')} to {window.get('to_label', '?')}"
        + (f"; {old} older notice{'s' if old != 1 else ''} skipped" if old else "")
        + ").\n\n"
        "Chosen and written up by AI (Claude). Quotes are checked word for word against the documents, "
        f"but check the source before relying on anything. [How it works]({PROJECT_URL}/blob/main/docs/digest.md).\n\n"
        f"Archive & RSS: [{SITE_URL}]({SITE_URL}) · Subscribe: [{SUBSCRIBE_URL}]({SUBSCRIBE_URL})"
    )


_SMALL = frozenset({"and", "of", "the", "for", "on", "in", "to", "at"})
# short all-caps words that are acronyms even though they have a vowel
_ACRONYMS = frozenset({"US", "USA", "ICE", "VA", "AID", "USAID", "IRS", "EPA", "FAA", "TSA", "SEC", "NASA", "NOAA",
                       "HUD", "HHS", "DOE", "DOJ", "DOT", "DEA", "ATF", "FBI", "USDA", "GSA", "OPM", "SBA"})
_DROP = re.compile(r",\s*DEPARTMENT OF( THE)?$|^DEPT\.? OF( THE)?\s+|^DEPARTMENT OF( THE)?\s+", re.I)
TITLE_CHARS = 90


def short_org(m: dict) -> str:
    """'DEPT OF THE ARMY' -> 'Army', 'VETERANS AFFAIRS, DEPARTMENT OF' ->
    'Veterans Affairs'; short all-caps words (VA, U.S., ICE) stay as they are."""
    name = (m.get("sub_tier") or m.get("agency") or "").strip()
    name = _DROP.sub("", name).strip() or name
    words = []
    for i, w in enumerate(name.split()):
        lw = w.lower()
        if i and lw in _SMALL:
            words.append(lw)
        elif ("." in w or w.startswith("(") or not w.isupper() or w in _ACRONYMS
              or (len(w) <= 4 and not set(w) & set("AEIOU"))):
            words.append(w)
        else:
            words.append(w.capitalize())
    return " ".join(words)


def _trim(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "\u2026"


def _ext_value(e: dict) -> str | None:
    v = e.get("estimated_value_usd")
    if isinstance(v, (int, float)) and v == v:
        return f"${v:,.0f}"
    return "value redacted" if e.get("value_redacted") is True else None


def _sam_link(c: Candidate) -> str:
    return c.meta.get("sam_link") or f"https://sam.gov/opp/{c.notice_id}/view"


def notice_rows(cands: list[Candidate], picks: list[dict], gists: dict | None = None) -> list[dict]:
    """The 'All new notices' list as data, in display order: every
    candidate. Vendor, value and authority only from the extraction's
    quote-checked fields (None when the notice has no extraction row or the
    field is empty); picks first (in pick order), then by value (largest
    first, unknown last), then agency and title.

    One function for the email (``all_notices``) and the web archive
    (archive.py stores these rows; site.py renders them), so the two can't
    drift apart (2026-10-21)."""
    rank = {p["notice_id"]: p["rank"] for p in picks}

    def key(c):
        v = c.extraction.get("estimated_value_usd")
        v = v if isinstance(v, (int, float)) and v == v else None
        return (rank.get(c.notice_id, 99), v is None, -(v or 0), short_org(c.meta).lower(),
                (c.meta.get("title") or "").lower())
    out = []
    for c in sorted(cands, key=key):
        e = c.extraction or {}
        out.append({"notice_id": c.notice_id, "pick_rank": rank.get(c.notice_id),
                    "org": short_org(c.meta), "title": c.meta.get("title") or "",
                    "vendor": e.get("vendor") or None, "value": _ext_value(e),
                    "authority": e.get("authority_cited") or None, "sam_link": _sam_link(c),
                    "gist": ((gists or {}).get(c.notice_id) or "").strip() or None})
    return out


def all_notices(cands: list[Candidate], picks: list[dict], gists: dict | None = None) -> str:
    """'All new notices (N)': every candidate, one line each (``notice_rows``),
    picks marked with a star."""
    lines = [f"## All new notices ({len(cands)})\n"]
    for r in notice_rows(cands, picks, gists):
        bits = [r["org"], _trim(r["title"] or "(no title)", TITLE_CHARS), r["vendor"], r["value"], r["authority"]]
        star = "\u2605 " if r["pick_rank"] else ""
        gist = _md_escape_line(r["gist"] or "")
        lines.append(f"- {star}" + " \u00b7 ".join(_md_escape_line(str(b)) for b in bits if b)
                     + f" \u00b7 [SAM.gov]({r['sam_link']})" + (f"  \n  {gist}" if gist else ""))
    lines.append("")
    lines.append(NOTICES_NOTE)
    return "\n".join(lines) + "\n"


FIELDS_NOTE = ("Vendor, value and authority are shown only where the AI extraction found them with a "
               "quote checked against the document; blank means not extracted yet or not stated, not none.")
GISTS_NOTE = "The one-line summaries are AI-written from each notice's text and are not quote-checked."
NOTICES_NOTE = f"*{FIELDS_NOTE} {GISTS_NOTE}*"


def pick_view(p: dict, c: Candidate) -> dict:
    """One verified pick (verify.verify_picks) as display data: what the
    email shows for it and what the web archive stores (same reason as
    notice_rows)."""
    m = c.meta
    org = m.get("sub_tier") or m.get("agency") or ""
    if m.get("sub_tier") and m.get("agency") and m["agency"] != m["sub_tier"]:
        org = f"{m['sub_tier']} ({m['agency']})"
    # the documents the quotes came from first, then the rest in order
    qrids = [pg["resource_id"] for q in p["quotes"] for pg in q.get("pages") or []]
    docs = sorted(c.links, key=lambda d: (0 if d["resource_id"] in qrids else 1))
    return {"rank": p["rank"], "notice_id": c.notice_id, "headline": p["headline"], "why": p["why"],
            "org": org, "vendor": p.get("vendor"), "value": p.get("value"), "authority": p.get("authority"),
            "posted_date": str(m.get("posted_date") or "")[:10], "award_number": m.get("award_number"),
            "title": m.get("title") or "", "sam_link": _sam_link(c),
            "quotes": [{"text": q["text"], "where": q["where"], "added_by_code": bool(q.get("added_by_code"))}
                       for q in p["quotes"]],
            "documents": [{"filename": d["filename"], "url": d["url"]} for d in docs[:MAX_DOC_LINKS]],
            "more_documents": max(0, len(docs) - MAX_DOC_LINKS)}


FIGURE_SOURCE_NOTE = " (source of the figure above, quoted by the checks)"


def body(picks: list[dict], cands: dict[str, Candidate], n_reviewed: int, window: dict,
         all_cands: list[Candidate] | None = None, gists: dict | None = None) -> str:
    listing = ("\n" + all_notices(all_cands, picks, gists)) if all_cands else ""
    if not picks:
        return (f"Nothing WOAH-worthy today ({n_reviewed} new notice{'s' if n_reviewed != 1 else ''} "
                f"reviewed).\n" + listing + "\n" + footer(n_reviewed, window) + "\n")
    out = []
    for p in picks:
        v = pick_view(p, cands[p["notice_id"]])
        line = " · ".join(x for x in (v["org"], v["vendor"], v["value"], v["authority"]) if x)
        out.append(f"## {v['rank']}. {_md_escape_line(v['headline'])}\n")
        out.append(f"**{_md_escape_line(line)}**  ")
        bits = [f"Posted {v['posted_date']}"]
        if v["award_number"]:
            bits.append(f"award {v['award_number']}")
        bits.append(f"“{_md_escape_line(v['title'])}”")
        out.append("*" + ", ".join(bits) + "*\n")
        out.append(v["why"] + "\n")
        for q in v["quotes"]:
            src = FIGURE_SOURCE_NOTE if q["added_by_code"] else ""
            out.append(f"> “{_md_escape_line(q['text'])}”  \n> — {q['where']}{src}\n")
        links = [f"[SAM.gov notice]({v['sam_link']})"]
        for d in v["documents"]:
            links.append(f"[{_md_escape_line(d['filename'])}]({d['url']})")
        if v["more_documents"]:
            links.append(f"+{v['more_documents']} more on SAM.gov")
        out.append(" · ".join(links) + "\n")
    return "\n".join(out) + listing + "\n" + footer(n_reviewed, window) + "\n"
