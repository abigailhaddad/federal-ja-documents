"""The email: a subject line and a Markdown body (Buttondown renders Markdown)."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .candidates import Candidate

ET = ZoneInfo("America/New_York")
PREFIX = "Whoa J&As"
DATASET_URL = "https://huggingface.co/datasets/abigailhaddad/federal-ja-documents"
PROJECT_URL = "https://github.com/abigailhaddad/federal-ja-documents"
MAX_DOC_LINKS = 3


def day_label(d: datetime) -> str:
    d = d.astimezone(ET)
    return f"{d:%b} {d.day}"


def subject(picks: list[dict], when: datetime) -> str:
    top = picks[0]["headline"].rstrip(".") if picks else "nothing whoa-worthy"
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
    return (
        "---\n\n"
        f"*What this is:* a daily pick of the most remarkable new federal sole-source justification (J&A) "
        f"notices posted on SAM.gov, from the public [federal-ja-documents dataset]({DATASET_URL}). "
        f"Today: {n_reviewed} new notice{'s' if n_reviewed != 1 else ''} reviewed "
        f"(first collected {window.get('from_label', '?')} to {window.get('to_label', '?')})"
        + (f"; {window['left_out_old']} notice{'s' if window['left_out_old'] != 1 else ''} first collected then "
           f"but posted more than {window.get('max_age_days')} days earlier were left out"
           if window.get("left_out_old") else "")
        + ".\n\n"
        "*How:* the picks and write-ups are AI-selected and AI-written (Claude). Every quote was checked "
        "word for word against the notice's document text, and dollar figures come from those quotes or "
        "from quote-checked extraction fields. AI can still misread a document: check the source before "
        f"relying on anything. [How it works]({PROJECT_URL}/blob/main/docs/digest.md)."
    )


_SMALL = frozenset({"and", "of", "the", "for", "on", "in", "to", "at"})
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
        elif len(w) <= 3 or "." in w or w.startswith("(") or not w.isupper():
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


def all_notices(cands: list[Candidate], picks: list[dict]) -> str:
    """'All new notices (N)': every candidate, one line each. Vendor, value
    and authority only from the extraction's quote-checked fields (left out
    when the notice has no extraction row or the field is empty); picks first
    (marked with a star, in pick order), then by value (largest first, unknown
    last), then agency and title."""
    rank = {p["notice_id"]: p["rank"] for p in picks}

    def key(c):
        v = c.extraction.get("estimated_value_usd")
        v = v if isinstance(v, (int, float)) and v == v else None
        return (rank.get(c.notice_id, 99), v is None, -(v or 0), short_org(c.meta).lower(),
                (c.meta.get("title") or "").lower())
    lines = [f"## All new notices ({len(cands)})\n"]
    for c in sorted(cands, key=key):
        e = c.extraction or {}
        link = c.meta.get("sam_link") or f"https://sam.gov/opp/{c.notice_id}/view"
        bits = [short_org(c.meta), _trim(c.meta.get("title") or "(no title)", TITLE_CHARS), e.get("vendor"),
                _ext_value(e), e.get("authority_cited")]
        star = "\u2605 " if c.notice_id in rank else ""
        lines.append(f"- {star}" + " \u00b7 ".join(_md_escape_line(str(b)) for b in bits if b)
                     + f" \u00b7 [SAM.gov]({link})")
    lines.append("")
    lines.append("*Vendor, value and authority are shown only where the AI extraction found them with a "
                 "quote checked against the document; blank means not extracted yet or not stated, not none.*")
    return "\n".join(lines) + "\n"


def body(picks: list[dict], cands: dict[str, Candidate], n_reviewed: int, window: dict,
         all_cands: list[Candidate] | None = None) -> str:
    listing = ("\n" + all_notices(all_cands, picks)) if all_cands else ""
    if not picks:
        return (f"Nothing whoa-worthy today ({n_reviewed} new notice{'s' if n_reviewed != 1 else ''} "
                f"reviewed).\n" + listing + "\n" + footer(n_reviewed, window) + "\n")
    out = []
    for p in picks:
        c = cands[p["notice_id"]]
        m = c.meta
        org = m.get("sub_tier") or m.get("agency") or ""
        if m.get("sub_tier") and m.get("agency") and m["agency"] != m["sub_tier"]:
            org = f"{m['sub_tier']} ({m['agency']})"
        line = " · ".join(x for x in (org, p.get("vendor"), p.get("value"), p.get("authority")) if x)
        out.append(f"## {p['rank']}. {_md_escape_line(p['headline'])}\n")
        out.append(f"**{_md_escape_line(line)}**  ")
        bits = [f"Posted {str(m.get('posted_date') or '')[:10]}"]
        if m.get("award_number"):
            bits.append(f"award {m['award_number']}")
        bits.append(f"“{_md_escape_line(m.get('title') or '')}”")
        out.append("*" + ", ".join(bits) + "*\n")
        out.append(p["why"] + "\n")
        for q in p["quotes"]:
            src = " (source of the figure above, quoted by the checks)" if q.get("added_by_code") else ""
            out.append(f"> “{_md_escape_line(q['text'])}”  \n> — {q['where']}{src}\n")
        links = [f"[SAM.gov notice]({m.get('sam_link') or f'https://sam.gov/opp/{c.notice_id}/view'})"]
        docs = c.links
        # the documents the quotes came from first, then the rest in order
        qrids = [pg["resource_id"] for q in p["quotes"] for pg in q["pages"]]
        docs = sorted(docs, key=lambda d: (0 if d["resource_id"] in qrids else 1))
        for d in docs[:MAX_DOC_LINKS]:
            links.append(f"[{_md_escape_line(d['filename'])}]({d['url']})")
        if len(docs) > MAX_DOC_LINKS:
            links.append(f"+{len(docs) - MAX_DOC_LINKS} more on SAM.gov")
        out.append(" · ".join(links) + "\n")
    return "\n".join(out) + listing + "\n" + footer(n_reviewed, window) + "\n"
