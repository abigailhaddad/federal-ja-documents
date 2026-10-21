"""The email: a subject line and a Markdown body (Buttondown renders Markdown)."""

from __future__ import annotations

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


def body(picks: list[dict], cands: dict[str, Candidate], n_reviewed: int, window: dict) -> str:
    if not picks:
        return (f"Nothing whoa-worthy today ({n_reviewed} new notice{'s' if n_reviewed != 1 else ''} "
                f"reviewed).\n\n" + footer(n_reviewed, window) + "\n")
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
            out.append(f"> “{_md_escape_line(q['text'])}”  \n> — {q['where']}\n")
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
    return "\n".join(out) + "\n" + footer(n_reviewed, window) + "\n"
