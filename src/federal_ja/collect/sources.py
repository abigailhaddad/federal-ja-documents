"""Read SAM.gov Contract Opportunities CSV extracts and pick out J&A notices.

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


def is_ja_type(type_value: str | None) -> bool:
    return (type_value or "").strip().startswith("Justification")


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


def notice_from_row(row: dict, source_label: str) -> dict:
    rec = {dst: (row.get(src) or "").strip() for src, dst in NOTICE_COLUMNS.items()}
    # The CSV's Link column points at the logged-in workspace view; the public
    # page is /opp/<id>/view.
    rec["sam_link"] = f"https://sam.gov/opp/{rec['notice_id']}/view" if rec["notice_id"] else ""
    rec["source_file"] = source_label
    rec["row_fingerprint"] = row_fingerprint(row)
    return rec


@dataclass
class FileStats:
    label: str
    rows: int = 0
    ja_rows: int = 0
    ja_types: Counter = field(default_factory=Counter)


@dataclass
class CollectStats:
    start_date: dt.date
    files: list[FileStats] = field(default_factory=list)
    ja_rows_total: int = 0
    unique_ja_ids: int = 0
    duplicate_rows: int = 0
    in_scope: int = 0
    before_start: int = 0
    # in scope by kept row but at least one other row was before start date
    straddling_start: int = 0
    type_counts_in_scope: Counter = field(default_factory=Counter)
    kept_source_counts: Counter = field(default_factory=Counter)

    def as_dict(self) -> dict:
        return {
            "start_date": self.start_date.isoformat(),
            "files": [
                {"label": f.label, "rows": f.rows, "ja_rows": f.ja_rows,
                 "ja_types": dict(f.ja_types)} for f in self.files
            ],
            "ja_rows_total": self.ja_rows_total,
            "unique_ja_ids": self.unique_ja_ids,
            "duplicate_rows": self.duplicate_rows,
            "in_scope": self.in_scope,
            "before_start": self.before_start,
            "straddling_start": self.straddling_start,
            "type_counts_in_scope": dict(self.type_counts_in_scope),
            "kept_source_counts": dict(self.kept_source_counts),
        }


@dataclass
class CollectResult:
    notices: dict[str, dict]          # notice_id -> record (in scope)
    exclusions: list[dict]            # J&A rows/ids not in notices, with reason
    stats: CollectStats


def iter_csv_rows(fh: IO[str]) -> Iterator[dict]:
    yield from csv.DictReader(fh)


def collect_ja_notices(
    sources: Iterable[tuple[str, IO[str]]],
    start_date: dt.date = config.DEFAULT_START_DATE,
) -> CollectResult:
    """Scan CSV sources (label, text stream) in priority order."""
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
            if not is_ja_type(row.get("Type")):
                continue
            fs.ja_rows += 1
            fs.ja_types[(row.get("Type") or "").strip()] += 1
            stats.ja_rows_total += 1
            nid = (row.get("NoticeId") or "").strip()
            if not nid:
                exclusions.append({
                    "notice_id": "", "source_file": label,
                    "posted_date": row.get("PostedDate", ""),
                    "type": row.get("Type", ""), "title": row.get("Title", ""),
                    "reason": "missing_notice_id",
                })
                continue
            d = parse_posted_date(row.get("PostedDate"))
            seen_in.setdefault(nid, [])
            if label not in seen_in[nid]:
                seen_in[nid].append(label)
            dates_seen.setdefault(nid, []).append(d)
            rec = notice_from_row(row, label)
            cur = best.get(nid)
            if cur is None:
                best[nid] = (d, priority, rec)
                continue
            stats.duplicate_rows += 1
            cur_d, cur_p, _ = cur
            # later date wins; equal date -> earlier priority (already kept)
            if d is not None and (cur_d is None or d > cur_d):
                best[nid] = (d, priority, rec)

    stats.unique_ja_ids = len(best)
    notices: dict[str, dict] = {}
    for nid, (d, _p, rec) in best.items():
        rec["source_files"] = seen_in[nid]
        if d is None:
            exclusions.append({
                "notice_id": nid, "source_file": rec["source_file"],
                "posted_date": rec["posted_date"], "type": rec["type"],
                "title": rec["title"], "reason": "unparseable_posted_date",
            })
            continue
        if d < start_date:
            stats.before_start += 1
            continue
        if any(x is not None and x < start_date for x in dates_seen[nid]):
            stats.straddling_start += 1
        stats.in_scope += 1
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
    return io.TextIOWrapper(r.raw, encoding="latin-1", newline="")


def head_etag(url: str) -> dict:
    r = requests.head(url, timeout=60, headers={"User-Agent": config.USER_AGENT})
    r.raise_for_status()
    return {
        "etag": r.headers.get("ETag", ""),
        "last_modified": r.headers.get("Last-Modified", ""),
        "content_length": int(r.headers.get("Content-Length", "0") or 0),
    }
