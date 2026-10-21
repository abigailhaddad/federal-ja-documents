"""Which notices a digest looks at, and what it knows about each one.

Everything is read from the published Hugging Face dataset at ONE dataset
commit (recorded in picks.json), with DuckDB over HTTPS and only the columns
and rows needed, so nothing large is downloaded:

* ``data/notices.parquet``: every notice's metadata, including
  ``first_seen_at`` (when the collector first saw it: the digest's clock;
  ``posted_date`` is SAM's posting date and can be days earlier);
* ``data/attachments.parquet`` / ``data/pages.parquet``: the document text of
  the candidates only, assembled with the analysis package's
  ``build_documents`` (attachment order, then page order);
* ``analysis/extractions.parquet``: the AI-extracted fields, when the notice
  has a row (any prompt version). The digest never depends on them.

Candidate rules (``select_candidates``, a pure function, tested):

1. ``first_seen_at`` inside the window (after the last sent digest's
   watermark, see state.py);
2. a Justification notice, or a Special Notice whose title says it is a
   sole-source intent or justification (``SPECIAL_NOTICE_INTENT``). Special
   Notices collected only because the title says "fair opportunity" (in
   practice, competition-schedule bulletins) are left out;
3. posted within ``max_age_days`` of the digest date. When the collector
   picks up a yearly archive or adds a new collection path, thousands of OLD
   notices get a new first_seen_at on one day; they are not news. Every
   left-out notice is counted by reason in picks.json.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from ..analysis.data import HF_DATASET, _clean, build_documents
from ..analysis.text import Document, Page

HF_BASE = f"https://huggingface.co/datasets/{HF_DATASET}/resolve/"

# Matches 323 of the 341 Special Notices in the dataset on 2026-10-21; the 18
# misses were all "Fair Opportunity" schedule bulletins ("ACC-APG Division D
# Competitive/Fair Opportunity ...", "Fair Opportunity to Quote ...").
SPECIAL_NOTICE_INTENT = re.compile(
    r"sole.?source|single.?source|limited.?source|justification|j\s*&\s*a\b|intent|6\.302|"
    r"only one responsible source|urgen(t|cy)|brand.?name|other than full|\bjofoc\b|exception", re.I)

NOTICE_COLS = ("notice_id", "sam_link", "title", "agency", "sub_tier", "office", "posted_date", "type",
               "collection_reason", "award_number", "solicitation_number", "description", "first_seen_at")
# Extraction fields shown to the model (when present). Every factual one
# passed the extraction's own quote checks; the summary is model-written.
EXTRACTION_FIELDS = ("is_ja", "vendor", "estimated_value_usd", "value_basis", "value_redacted", "value_quote",
                     "authority_cited", "urgency_trigger", "bridge_or_extension", "prior_extension_count",
                     "is_letter_contract", "funding_sources", "summary_model_written", "extract_version")

DESCRIPTION_PAGE = "SAM.gov notice description"


def parse_ts(v) -> datetime | None:
    """first_seen_at ('2026-10-21T11:42:27+00:00') or posted_date
    ('2026-10-20 15:37:01', SAM's local time, treated as UTC) -> aware UTC."""
    v = _clean(v)
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@dataclass
class Candidate:
    notice_id: str
    meta: dict
    doc: Document
    extraction: dict = field(default_factory=dict)
    links: list[dict] = field(default_factory=list)   # [{filename, url}] in reading order

    @property
    def first_seen_at(self) -> str | None:
        return self.meta.get("first_seen_at")


def wanted_reason(n: dict) -> str | None:
    """None if the notice is a digest candidate by type, else why not."""
    reason = _clean(n.get("collection_reason"))
    if reason == "justification_type" or str(_clean(n.get("type")) or "").startswith("Justification"):
        return None
    if reason == "special_notice_title_match":
        return None if SPECIAL_NOTICE_INTENT.search(_clean(n.get("title")) or "") else "special_notice_not_intent"
    return "other_type"


def select_candidates(notices: list[dict], *, after: datetime | None, until: datetime | None = None,
                      as_of: datetime, max_age_days: int = 14,
                      posted_from: str | None = None, posted_to: str | None = None) -> tuple[list[dict], dict]:
    """(candidate notice rows, {"excluded": counts by reason, "in_window": n,
    "max_first_seen_at": latest first_seen_at in the window}).

    ``after``: first_seen_at must be strictly later (the watermark); None
    means no lower bound. ``until``: first_seen_at at or before it.
    ``posted_from`` / ``posted_to`` (YYYY-MM-DD, inclusive) are only for
    backtests of days the collector loaded in bulk."""
    keep, excluded = [], {}
    in_window = []
    for n in notices:
        fs = parse_ts(n.get("first_seen_at"))
        if fs is None:
            excluded["no_first_seen_at"] = excluded.get("no_first_seen_at", 0) + 1
            continue
        if (after is not None and fs <= after) or (until is not None and fs > until):
            continue
        in_window.append((n, fs))
    cutoff = as_of - timedelta(days=max_age_days)
    for n, fs in in_window:
        why = wanted_reason(n)
        posted = parse_ts(n.get("posted_date"))
        pday = (str(_clean(n.get("posted_date")) or ""))[:10]
        if why is None and (posted_from and pday < posted_from or posted_to and pday > posted_to):
            why = "outside_posted_window"
        if why is None and (posted is None or posted < cutoff):
            why = f"posted_more_than_{max_age_days}_days_before"
        if why:
            excluded[why] = excluded.get(why, 0) + 1
            continue
        keep.append(n)
    keep.sort(key=lambda n: (str(n.get("posted_date") or ""), n["notice_id"]), reverse=True)
    window = {"in_window": len(in_window),
              "max_first_seen_at": max((fs for _, fs in in_window), default=None)}
    return keep, {"excluded": excluded, **window}


# --------------------------------------------------------------------------- loading

def _sql_list(ids) -> str:
    return ",".join("'" + i.replace("'", "''") + "'" for i in ids)


class DatasetReader:
    """Reads the published tables at one commit with DuckDB."""

    def __init__(self, revision: str):
        import duckdb
        self.revision = revision
        self.base = HF_BASE + revision + "/"
        self.con = duckdb.connect()

    def notices(self, since_first_seen: datetime | None) -> list[dict]:
        where = f"where first_seen_at > '{since_first_seen.isoformat()}'" if since_first_seen else ""
        # first_seen_at strings are ISO UTC ('...+00:00'), so a string
        # comparison prefilters; select_candidates applies the exact rule.
        cols = ", ".join(NOTICE_COLS)
        return self.con.sql(f"select {cols} from '{self.base}data/notices.parquet' {where}").df().to_dict("records")

    def documents(self, notices: list[dict]):
        import pandas as pd
        ids = [n["notice_id"] for n in notices]
        if not ids:
            return {}, {}
        lst = _sql_list(ids)
        att = self.con.sql(
            f"""select notice_id, resource_id, attachment_order, download_status, file_kind, filename, file_path,
                       case when page_count is null then text end as text
                from '{self.base}data/attachments.parquet' where notice_id in ({lst})""").df()
        pages = self.con.sql(
            f"""select notice_id, resource_id, page_number, text_source, text
                from '{self.base}data/pages.parquet' where notice_id in ({lst})""").df()
        docs = build_documents(pd.DataFrame(notices), att, pages, ids)
        links: dict[str, list[dict]] = {}
        att = att.assign(_o=att.attachment_order.fillna(10**6)).sort_values(["notice_id", "_o", "resource_id"])
        for r in att.to_dict("records"):
            if _clean(r.get("download_status")) == "ok" and _clean(r.get("file_path")):
                links.setdefault(r["notice_id"], []).append(
                    {"filename": _clean(r.get("filename")) or "document", "resource_id": r["resource_id"],
                     "url": self.base + quote(r["file_path"], safe="/")})
        return docs, links

    def extractions(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        try:
            df = self.con.sql(f"select * from '{self.base}analysis/extractions.parquet' "
                              f"where notice_id in ({_sql_list(ids)}) and status = 'extracted'").df()
        except Exception as exc:   # no table at this commit: the digest works without it
            print(f"no extraction table read ({type(exc).__name__}: {str(exc)[:120]})")
            return {}
        out = {}
        for r in df.to_dict("records"):
            out[r["notice_id"]] = {k: _clean(r.get(k)) for k in EXTRACTION_FIELDS if k in r}
        return out


def build_candidates(rows: list[dict], docs: dict, links: dict, extractions: dict) -> list[Candidate]:
    """Attach documents, links and extraction rows. The SAM.gov description
    is added to the document as one extra page (``DESCRIPTION_PAGE``): it is
    the notice's own text on SAM.gov, and for many Special Notices the only
    text there is, so a quote from it verifies like any other."""
    out = []
    for n in rows:
        nid = n["notice_id"]
        meta = {k: _clean(n.get(k)) for k in NOTICE_COLS}
        doc = docs.get(nid) or Document(nid, meta)
        doc.meta.update({k: meta.get(k) for k in ("first_seen_at", "type", "description")})
        desc = _strip_html(meta.get("description") or "")
        if desc.strip():
            doc.pages.append(Page("sam_description", DESCRIPTION_PAGE, None, "native", desc))
        out.append(Candidate(nid, meta, doc, extractions.get(nid) or {}, links.get(nid) or []))
    return out


_TAG = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    import html
    return html.unescape(_TAG.sub(" ", s))
