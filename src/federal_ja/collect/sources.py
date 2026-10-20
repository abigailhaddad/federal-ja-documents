"""Read SAM.gov Contract Opportunities CSV extracts and pick out J&A notices.

A row is collected for one of two reasons (``collection_reason``):

* ``justification_type``: its ``Type`` starts with "Justification";
* ``special_notice_title_match``: its ``Type`` is "Special Notice" and its
  ``Title`` (not the description) matches ``JA_TITLE_PATTERN``. Agencies
  sometimes post a J&A as a Special Notice (e.g. "Notice of Intent to Sole
  Source" with the justification attached). These rows are candidates: the
  title only says the notice may carry a justification.

Every count in ``CollectStats`` is kept per reason, so the two paths are
never mixed silently.

The extracts are latin-1 encoded and contain very large Description fields, so
we raise csv.field_size_limit and stream rows (the yearly archives are ~1 GB).

Deduplication rule: the same NoticeId can appear in the active file and in one
or more yearly archives. Among all J&A rows for a NoticeId we keep the row
with the latest PostedDate; ties go to the source listed first (callers pass
the active file first, then archives newest-first). Every source file a
NoticeId was seen in is recorded in ``source_files``.

Scope (PostedDate >= start date) is applied to the kept row, after dedup.
Nothing is dropped silently: rows we cannot use are returned as exclusions
with a reason, and every count is in ``CollectStats``.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import IO, Iterable, Iterator

import requests

from . import config

csv.field_size_limit(sys.maxsize)

# CSV column -> dataset column
NOTICE_COLUMNS: dict[str, str] = {
    "NoticeId": "notice_id",
    "Title": "title",
    "Sol#": "solicitation_number",
    "Department/Ind.Agency": "agency",
    "CGAC": "cgac",
    "Sub-Tier": "sub_tier",
    "FPDS Code": "fpds_code",
    "Office": "office",
    "AAC Code": "aac_code",
    "PostedDate": "posted_date",
    "Type": "type",
    "BaseType": "base_type",
    "ArchiveType": "archive_type",
    "ArchiveDate": "archive_date",
    "NaicsCode": "naics_code",
    "ClassificationCode": "classification_code",
    "Active": "active",
    "AwardNumber": "award_number",
    "AwardDate": "award_date",
    "Award$": "award_amount",
    "Awardee": "awardee",
    "Link": "csv_link",
    "Description": "description",
}

# Fields hashed into row_fingerprint; a change means the notice was edited on
# SAM and its attachment list should be re-checked.
FINGERPRINT_FIELDS = [
    "Title", "Sol#", "PostedDate", "Type", "BaseType", "ArchiveType",
    "ArchiveDate", "AwardNumber", "Description",
]


REASON_JUSTIFICATION_TYPE = "justification_type"
REASON_SPECIAL_NOTICE_TITLE = "special_notice_title_match"
COLLECTION_REASONS = (REASON_JUSTIFICATION_TYPE, REASON_SPECIAL_NOTICE_TITLE)

# SAM's Type value for special notices, as it appears in the CSVs (checked on
# the active file of 2026-10-19: 4,961 rows, no other spelling).
SPECIAL_NOTICE_TYPE = "Special Notice"

# Title keywords that make a Special Notice a J&A candidate. Matched anywhere
# in the title, ignoring case. The "." in "sole.source" etc. is any one
# character ("sole source", "Sole-Source", "sole/source"); "solesource" with
# no separator does not match.
JA_TITLE_PATTERN = re.compile(
    r"justification|j\s*&\s*a\b|sole.source|single.source|limited.source|far\s*6\.302"
    r"|fair opportunity|only one responsible source|unusual and compelling urgency",
    re.IGNORECASE,
)


def is_ja_type(type_value: str | None) -> bool:
    return (type_value or "").strip().startswith("Justification")


def is_special_notice_type(type_value: str | None) -> bool:
    return (type_value or "").strip() == SPECIAL_NOTICE_TYPE


def title_looks_like_ja(title: str | None) -> bool:
    return JA_TITLE_PATTERN.search(title or "") is not None


def collection_reason(type_value: str | None, title: str | None) -> str | None:
    """Why a row with this Type and Title is collected, or None if it is not."""
    if is_ja_type(type_value):
        return REASON_JUSTIFICATION_TYPE
    if is_special_notice_type(type_value) and title_looks_like_ja(title):
        return REASON_SPECIAL_NOTICE_TITLE
    return None


def parse_posted_date(value: str | None) -> dt.date | None:
    """Parse the date part of a SAM PostedDate.

    Seen forms: '2026-10-15 15:03:33', '2025-01-21T10:00:00-05:00',
    '2025-01-21 10:00:00.123-05'. Returns None if unparseable.
    """
    s = (value or "").strip()
    if len(s) >= 10:
        try:
            return dt.date.fromisoformat(s[:10])
        except ValueError:
            pass
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(s.split()[0], fmt).date()
        except (ValueError, IndexError):
            pass
    return None


def row_fingerprint(row: dict) -> str:
    h = hashlib.sha1()
    for f in FINGERPRINT_FIELDS:
        h.update((row.get(f) or "").encode("utf-8", "replace"))
        h.update(b"\x1f")
    return h.hexdigest()


def notice_from_row(row: dict, source_label: str, reason: str) -> dict:
    rec = {dst: (row.get(src) or "").strip() for src, dst in NOTICE_COLUMNS.items()}
    rec["collection_reason"] = reason
    # The CSV's Link column points at the logged-in workspace view; the public
    # page is /opp/<id>/view.
    rec["sam_link"] = f"https://sam.gov/opp/{rec['notice_id']}/view" if rec["notice_id"] else ""
    rec["source_file"] = source_label
    rec["row_fingerprint"] = row_fingerprint(row)
    return rec


def _by_reason() -> Counter:
    return Counter({r: 0 for r in COLLECTION_REASONS})


@dataclass
class FileStats:
    label: str
    rows: int = 0
    ja_rows: int = 0                     # Type starts with "Justification"
    ja_types: Counter = field(default_factory=Counter)
    special_notice_rows: int = 0         # every Type == "Special Notice" row
    special_notice_title_matches: int = 0  # of those, Title matches JA_TITLE_PATTERN


@dataclass
class CollectStats:
    start_date: dt.date
    files: list[FileStats] = field(default_factory=list)
    # ja_rows_total and unique_ja_ids count the justification_type path only;
    # the *_by_reason counters have both paths, and their sums are the totals.
    ja_rows_total: int = 0
    unique_ja_ids: int = 0
    rows_by_reason: Counter = field(default_factory=_by_reason)
    unique_ids_by_reason: Counter = field(default_factory=_by_reason)
    unique_ids_total: int = 0
    duplicate_rows: int = 0
    in_scope: int = 0
    in_scope_by_reason: Counter = field(default_factory=_by_reason)
    before_start: int = 0
    before_start_by_reason: Counter = field(default_factory=_by_reason)
    # in scope by kept row but at least one other row was before start date
    straddling_start: int = 0
    type_counts_in_scope: Counter = field(default_factory=Counter)
    kept_source_counts: Counter = field(default_factory=Counter)

    def as_dict(self) -> dict:
        return {
            "start_date": self.start_date.isoformat(),
            "files": [
                {"label": f.label, "rows": f.rows, "ja_rows": f.ja_rows,
                 "ja_types": dict(f.ja_types), "special_notice_rows": f.special_notice_rows,
                 "special_notice_title_matches": f.special_notice_title_matches}
                for f in self.files
            ],
            "ja_rows_total": self.ja_rows_total,
            "unique_ja_ids": self.unique_ja_ids,
            "rows_by_reason": dict(self.rows_by_reason),
            "unique_ids_by_reason": dict(self.unique_ids_by_reason),
            "unique_ids_total": self.unique_ids_total,
            "duplicate_rows": self.duplicate_rows,
            "in_scope": self.in_scope,
            "in_scope_by_reason": dict(self.in_scope_by_reason),
            "before_start": self.before_start,
            "before_start_by_reason": dict(self.before_start_by_reason),
            "straddling_start": self.straddling_start,
            "type_counts_in_scope": dict(self.type_counts_in_scope),
            "kept_source_counts": dict(self.kept_source_counts),
        }


@dataclass
class CollectResult:
    notices: dict[str, dict]          # notice_id -> record (in scope)
    exclusions: list[dict]            # collected rows/ids not in notices, with reason
    stats: CollectStats


def iter_csv_rows(fh: IO[str]) -> Iterator[dict]:
    yield from csv.DictReader(fh)


def collect_ja_notices(
    sources: Iterable[tuple[str, IO[str]]],
    start_date: dt.date = config.DEFAULT_START_DATE,
) -> CollectResult:
    """Scan CSV sources (label, text stream) in priority order.

    Both collection paths go through the same dedup, scope and exclusion
    rules; when a NoticeId has rows of both kinds, the kept row decides its
    ``collection_reason``."""
    stats = CollectStats(start_date=start_date)
    best: dict[str, tuple[dt.date | None, int, dict]] = {}
    seen_in: dict[str, list[str]] = {}
    dates_seen: dict[str, list[dt.date | None]] = {}
    exclusions: list[dict] = []

    for priority, (label, fh) in enumerate(sources):
        fs = FileStats(label=label)
        stats.files.append(fs)
        for row in iter_csv_rows(fh):
            fs.rows += 1
            if is_special_notice_type(row.get("Type")):
                fs.special_notice_rows += 1
            reason = collection_reason(row.get("Type"), row.get("Title"))
            if reason is None:
                continue
            if reason == REASON_JUSTIFICATION_TYPE:
                fs.ja_rows += 1
                fs.ja_types[(row.get("Type") or "").strip()] += 1
                stats.ja_rows_total += 1
            else:
                fs.special_notice_title_matches += 1
            stats.rows_by_reason[reason] += 1
            nid = (row.get("NoticeId") or "").strip()
            if not nid:
                exclusions.append({
                    "notice_id": "", "source_file": label,
                    "posted_date": row.get("PostedDate", ""),
                    "type": row.get("Type", ""), "title": row.get("Title", ""),
                    "collection_reason": reason, "reason": "missing_notice_id",
                })
                continue
            d = parse_posted_date(row.get("PostedDate"))
            seen_in.setdefault(nid, [])
            if label not in seen_in[nid]:
                seen_in[nid].append(label)
            dates_seen.setdefault(nid, []).append(d)
            rec = notice_from_row(row, label, reason)
            cur = best.get(nid)
            if cur is None:
                best[nid] = (d, priority, rec)
                continue
            stats.duplicate_rows += 1
            cur_d, cur_p, _ = cur
            # later date wins; equal date -> earlier priority (already kept)
            if d is not None and (cur_d is None or d > cur_d):
                best[nid] = (d, priority, rec)

    stats.unique_ids_total = len(best)
    for _d, _p, rec in best.values():
        stats.unique_ids_by_reason[rec["collection_reason"]] += 1
    stats.unique_ja_ids = stats.unique_ids_by_reason[REASON_JUSTIFICATION_TYPE]
    notices: dict[str, dict] = {}
    for nid, (d, _p, rec) in best.items():
        rec["source_files"] = seen_in[nid]
        reason = rec["collection_reason"]
        if d is None:
            exclusions.append({
                "notice_id": nid, "source_file": rec["source_file"],
                "posted_date": rec["posted_date"], "type": rec["type"],
                "title": rec["title"], "collection_reason": reason,
                "reason": "unparseable_posted_date",
            })
            continue
        if d < start_date:
            stats.before_start += 1
            stats.before_start_by_reason[reason] += 1
            continue
        if any(x is not None and x < start_date for x in dates_seen[nid]):
            stats.straddling_start += 1
        stats.in_scope += 1
        stats.in_scope_by_reason[reason] += 1
        stats.type_counts_in_scope[rec["type"]] += 1
        stats.kept_source_counts[rec["source_file"]] += 1
        notices[nid] = rec
    return CollectResult(notices=notices, exclusions=exclusions, stats=stats)


def open_csv_path(path: str) -> IO[str]:
    return open(path, encoding="latin-1", newline="")


def open_csv_url(url: str, session: requests.Session | None = None) -> IO[str]:
    """Stream a remote CSV as text without saving it to disk."""
    s = session or requests.Session()
    r = s.get(url, stream=True, timeout=(30, 300),
              headers={"User-Agent": config.USER_AGENT})
    r.raise_for_status()
    r.raw.decode_content = True
    # urllib3 closes the response as soon as it is fully read, and a
    # TextIOWrapper then raises "I/O operation on closed file" instead of
    # returning EOF.
    r.raw.auto_close = False
    fh = io.TextIOWrapper(r.raw, encoding="latin-1", newline="")
    # Keep the response and session alive for as long as the stream is read;
    # otherwise they are garbage-collected and the socket is closed.
    fh._response = r  # type: ignore[attr-defined]
    fh._session = s  # a collected Session closes its connection pool
    return fh


def head_etag(url: str) -> dict:
    r = requests.head(url, timeout=60, headers={"User-Agent": config.USER_AGENT})
    r.raise_for_status()
    return {
        "etag": r.headers.get("ETag", ""),
        "last_modified": r.headers.get("Last-Modified", ""),
        "content_length": int(r.headers.get("Content-Length", "0") or 0),
    }
