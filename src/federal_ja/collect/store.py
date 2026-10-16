"""Local working copy of the dataset: tables plus stored files.

Layout (mirrors the Hugging Face repo):
  data/notices.parquet        one row per J&A notice
  data/attachments.parquet    one row per (notice_id, resource_id)
  data/exclusions.parquet     J&A rows/ids from the CSVs that are not notices
  data/collection_state.json  source ETags, layout version, run history
  files/<notice_id>/<resource_id>_<safe filename>
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import Iterable

import pandas as pd

LAYOUT_VERSION = "federal-ja-documents/v1"

NOTICE_COLUMNS = [
    "notice_id", "sam_link", "title", "solicitation_number", "agency", "cgac",
    "sub_tier", "fpds_code", "office", "aac_code", "posted_date", "type",
    "base_type", "archive_type", "archive_date", "naics_code",
    "classification_code", "active", "award_number", "award_date",
    "award_amount", "awardee", "description", "csv_link", "source_file",
    "source_files", "row_fingerprint", "first_seen_at", "resources_status",
    "resources_checked_at", "attachment_count",
]

ATTACHMENT_COLUMNS = [
    "notice_id", "resource_id", "attachment_id", "attachment_order",
    "resource_type", "filename", "link_url", "mime_type", "size",
    "access_level", "access_status", "export_controlled", "file_exists",
    "deleted_flag", "deleted_date", "effective_deleted_date", "sam_posted_date", "in_latest_listing",
    "download_status", "download_error", "downloaded_at", "downloaded_size",
    "sha256", "file_path", "file_kind", "page_count", "pages_with_text", "textless_pages",
    "native_text_chars", "needs_transcription", "text", "text_source",
    "extract_error", "transcription_model", "transcription_prompt_version",
    "transcription_input_tokens", "transcription_output_tokens",
    "transcription_cost_usd", "transcribed_pages", "transcribed_at", "transcription_error",
    "uploaded_at", "uploaded_commit", "local_copy",
]

EXCLUSION_COLUMNS = ["notice_id", "source_file", "posted_date", "type", "title", "reason"]

INT_COLS = {"size", "downloaded_size", "page_count", "pages_with_text",
            "native_text_chars", "transcription_input_tokens",
            "transcription_output_tokens", "attachment_order", "attachment_count"}


def safe_filename(name: str, max_len: int = 120) -> str:
    """ASCII-only, path-safe filename that keeps the extension."""
    name = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    name = name.replace("/", "_").replace("\\", "_")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    if not name:
        name = "file"
    if len(name) > max_len:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            name = stem[: max_len - len(ext) - 1] + "." + ext
        else:
            name = name[:max_len]
    return name


def file_rel_path(notice_id: str, resource_id: str, filename: str) -> str:
    return f"files/{notice_id}/{resource_id}_{safe_filename(filename)}"


class Store:
    def __init__(self, workdir: str):
        self.workdir = workdir
        self.notices: dict[str, dict] = {}
        self.attachments: dict[tuple[str, str], dict] = {}
        self.exclusions: list[dict] = []
        self.state: dict = {"layout": LAYOUT_VERSION, "sources": {}, "runs": []}

    # paths
    def path(self, *parts: str) -> str:
        return os.path.join(self.workdir, *parts)

    @property
    def data_dir(self) -> str:
        return self.path("data")

    # io
    def load(self) -> "Store":
        p = self.path("data", "notices.parquet")
        if os.path.exists(p):
            for rec in _read_records(p):
                self.notices[rec["notice_id"]] = rec
        p = self.path("data", "attachments.parquet")
        if os.path.exists(p):
            for rec in _read_records(p):
                self.attachments[(rec["notice_id"], rec["resource_id"])] = rec
        p = self.path("data", "exclusions.parquet")
        if os.path.exists(p):
            self.exclusions = _read_records(p)
        p = self.path("data", "collection_state.json")
        if os.path.exists(p):
            with open(p) as f:
                self.state = json.load(f)
        return self

    def save(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        _write(self.path("data", "notices.parquet"),
               sorted(self.notices.values(), key=lambda r: r["notice_id"]), NOTICE_COLUMNS)
        _write(self.path("data", "attachments.parquet"),
               sorted(self.attachments.values(),
                      key=lambda r: (r["notice_id"], r.get("attachment_order") or 0, r["resource_id"])),
               ATTACHMENT_COLUMNS)
        _write(self.path("data", "exclusions.parquet"), self.exclusions, EXCLUSION_COLUMNS)
        tmp = self.path("data", "collection_state.json.tmp")
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2, sort_keys=True, default=str)
        os.replace(tmp, self.path("data", "collection_state.json"))

    def attachments_for(self, notice_id: str) -> list[dict]:
        return [a for (n, _r), a in self.attachments.items() if n == notice_id]


def _clean(v):
    # pandas -> python: NaN/None to None, numpy scalars to python
    if v is None:
        return None
    if hasattr(v, "tolist") and not isinstance(v, (str, bytes)):
        return v.tolist()
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def _read_records(path: str) -> list[dict]:
    df = pd.read_parquet(path)
    return [{k: _clean(v) for k, v in r.items()} for r in df.to_dict("records")]


def _write(path: str, records: Iterable[dict], columns: list[str]) -> None:
    rows = [{c: r.get(c) for c in columns} for r in records]
    df = pd.DataFrame(rows, columns=columns)
    for c in columns:
        if c in INT_COLS:
            df[c] = pd.array(df[c], dtype="Int64")
        elif c in ("deleted_flag", "in_latest_listing", "needs_transcription", "local_copy"):
            df[c] = pd.array(df[c], dtype="boolean")
        elif c == "transcription_cost_usd":
            df[c] = pd.array(df[c], dtype="Float64")
        elif c == "source_files":
            df[c] = df[c].map(lambda v: [str(x) for x in v] if v is not None else [])
        elif c in ("textless_pages", "transcribed_pages"):
            df[c] = df[c].map(lambda v: [int(x) for x in v] if v is not None else None)
        else:
            df[c] = df[c].map(lambda v: None if v is None else str(v)).astype("string")
    tmp = path + ".tmp"
    df.to_parquet(tmp, index=False)
    os.replace(tmp, path)
