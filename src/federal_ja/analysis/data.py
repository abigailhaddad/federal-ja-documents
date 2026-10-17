"""Read the published Hugging Face dataset and build one Document per notice.

EXPERIMENTAL. Reads only the public tables (``data/notices.parquet``,
``data/attachments.parquet``, ``data/pages.parquet``), downloaded once at a
pinned dataset commit so every result says which day's data it was built on.
Nothing here writes to the dataset.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

import requests

from .text import Document, Page

HF_DATASET = "abigailhaddad/federal-ja-documents"
HF_API_URL = f"https://huggingface.co/api/datasets/{HF_DATASET}"
HF_FILE_URL = f"https://huggingface.co/datasets/{HF_DATASET}/resolve/{{rev}}/{{path}}"
TABLES = ("notices", "attachments", "pages")

# Attachments left out of the text: spreadsheets (price sheets in practice;
# never validated).
SKIP_FILE_KINDS = frozenset({"xlsx"})


def _clean(v: Any) -> Any:
    if v is None:
        return None
    try:
        if isinstance(v, float) and math.isnan(v):
            return None
        import pandas as pd
        if v is pd.NA:
            return None
    except (TypeError, ValueError):
        pass
    return v


def dataset_revision() -> dict:
    r = requests.get(HF_API_URL, timeout=60)
    r.raise_for_status()
    j = r.json()
    return {"dataset": HF_DATASET, "sha": j.get("sha"), "last_modified": j.get("lastModified")}


def download_tables(cache_dir: Path, *, revision: str | None = None, refresh: bool = False) -> dict:
    """Download the three tables into cache_dir at one dataset commit (the
    latest unless ``revision`` is given). Skipped when already present,
    unless refresh. Returns the revision info, also written to
    cache_dir/revision.json."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    meta_path = cache_dir / "revision.json"
    have = all((cache_dir / f"{t}.parquet").exists() for t in TABLES) and meta_path.exists()
    if have and not refresh:
        info = json.loads(meta_path.read_text())
        if revision is None or info.get("sha") == revision:
            return info
    info = dataset_revision() if revision is None else {"dataset": HF_DATASET, "sha": revision,
                                                        "last_modified": None}
    for t in TABLES:
        url = HF_FILE_URL.format(rev=info["sha"], path=f"data/{t}.parquet")
        tmp = cache_dir / f"{t}.parquet.part"
        with requests.get(url, stream=True, timeout=600) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
        tmp.replace(cache_dir / f"{t}.parquet")
    meta_path.write_text(json.dumps(info, indent=1))
    return info


def load_tables(cache_dir: Path):
    import pandas as pd
    cache_dir = Path(cache_dir)
    return tuple(pd.read_parquet(cache_dir / f"{t}.parquet") for t in TABLES)


def _notice_meta(n: dict) -> dict:
    return {k: _clean(n.get(k)) for k in ("title", "agency", "sub_tier", "office", "posted_date",
                                          "solicitation_number", "award_number", "sam_link")}


def build_documents(notices, attachments, pages, notice_ids: Iterable[str] | None = None) -> dict[str, Document]:
    """One Document per notice (pandas DataFrames in, dict out).

    Per attachment, in ``attachment_order``: its rows in pages.parquet in
    page order (native or transcription; a page with text_source "none" is
    kept with empty text so it is counted, not lost). An attachment with no
    page rows (a .docx) contributes its whole-document text from
    attachments.parquet as one page, text_source "native". Spreadsheets are
    left out. A notice with no usable text still gets a Document (with no
    pages), so it is counted as skipped rather than silently dropped."""
    want = set(notice_ids) if notice_ids is not None else None
    if want is not None:
        notices = notices[notices.notice_id.isin(want)]
        attachments = attachments[attachments.notice_id.isin(want)]
        pages = pages[pages.notice_id.isin(want)]
    by_att: dict[tuple[str, str], list[dict]] = {}
    for r in pages.sort_values(["notice_id", "resource_id", "page_number"]).to_dict("records"):
        by_att.setdefault((r["notice_id"], r["resource_id"]), []).append(r)
    by_notice: dict[str, list[dict]] = {}
    att_sorted = attachments.assign(_o=attachments.attachment_order.fillna(10**6)).sort_values(
        ["notice_id", "_o", "resource_id"])
    for r in att_sorted.to_dict("records"):
        by_notice.setdefault(r["notice_id"], []).append(r)
    docs: dict[str, Document] = {}
    for n in notices.to_dict("records"):
        nid = n["notice_id"]
        doc = Document(nid, _notice_meta(n))
        for a in by_notice.get(nid, []):
            if _clean(a.get("download_status")) != "ok" or _clean(a.get("file_kind")) in SKIP_FILE_KINDS:
                continue
            rid = a["resource_id"]
            fn = _clean(a.get("filename")) or "document"
            rows = by_att.get((nid, rid))
            if rows:
                for p in rows:
                    doc.pages.append(Page(rid, fn, _clean(p.get("page_number")),
                                          _clean(p.get("text_source")) or "none", _clean(p.get("text")) or ""))
            elif _clean(a.get("text")):
                doc.pages.append(Page(rid, fn, None, "native", a["text"]))
        docs[nid] = doc
    return docs
