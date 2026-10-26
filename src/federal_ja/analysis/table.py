"""The published extraction table, ``analysis/extractions.parquet``. EXPERIMENTAL.

Used by the daily incremental run (``.github/workflows/extract-daily.yml``):

1. ``seed_from_hf`` writes the published table's stored records back into a
   work directory as ``extractions.jsonl`` and ``skipped.jsonl`` (each row's
   ``record_json`` is the full record), so ``extract`` skips every notice
   that already has a current-version row.
2. ``extract`` runs on the rest, under its ``--max-usd`` cap.
3. ``build_table`` turns the work directory back into the table: one row per
   notice, the latest record whatever its extraction version (each row says
   its ``extract_version`` and ``dataset_revision``), plus the skipped
   notices.
4. ``upload_table`` commits that one file to the dataset, and refuses to
   publish a table with fewer rows than the published one (a failed seed
   must never replace the table with a near-empty one), counting only the
   rows left out on purpose (Special Notices that don't look like a J&A,
   ja_check.py) as an allowed difference. Nothing else in the dataset is
   touched.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import extract
from .data import HF_DATASET

TABLE_PATH = "analysis/extractions.parquet"
_META = ("sam_link", "title", "agency", "sub_tier", "posted_date")


class ShrinkError(RuntimeError):
    """The new table has fewer rows than the published one."""


def _jsonl(p: Path) -> list[dict]:
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


def seed_rows(table) -> tuple[list[dict], list[dict]]:
    """(extraction records, skipped records) from a published table (a
    DataFrame with ``status`` and ``record_json``)."""
    recs, skipped = [], []
    for r in table.to_dict("records"):
        rec = json.loads(r["record_json"])
        (recs if r["status"] == "extracted" else skipped).append(rec)
    return recs, skipped


def _download_published(revision: str | None = None):
    import pandas as pd
    from huggingface_hub import hf_hub_download

    return pd.read_parquet(hf_hub_download(HF_DATASET, TABLE_PATH, repo_type="dataset", revision=revision))


def seed_from_hf(out: Path, *, revision: str | None = None, table=None) -> dict:
    """Write the published table's records into out/ unless out/ already has
    extractions. Returns counts. ``table`` (a DataFrame) skips the download
    (tests)."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    ext_p, skip_p = out / "extractions.jsonl", out / "skipped.jsonl"
    if ext_p.exists():
        return {"seeded": False, "reason": f"{ext_p} exists"}
    recs, skipped = seed_rows(table if table is not None else _download_published(revision))
    ext_p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs))
    skip_p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in skipped))
    return {"seeded": True, "extracted_rows": len(recs), "skipped_rows": len(skipped)}


def notice_ids(out: Path) -> set[str]:
    """Every notice with a record (extracted or skipped) in out/."""
    out = Path(out)
    return {r["notice_id"] for r in _jsonl(out / "extractions.jsonl") + _jsonl(out / "skipped.jsonl")}


def build_table(out: Path, docs_meta: dict[str, dict], *, exclude: set[str] = frozenset()):
    """One row per notice: the latest extraction record (any version), or
    the skip record for a notice with no extraction. A notice that is skipped
    and also has an extraction keeps the extraction. Notices in ``exclude``
    (Special Notices that don't look like a J&A, ja_check.py) get no row."""
    import pandas as pd

    out = Path(out)
    latest: dict[str, dict] = {}
    for r in _jsonl(out / "extractions.jsonl"):
        if r["notice_id"] not in exclude:
            latest[r["notice_id"]] = r
    rows = []
    for nid, r in latest.items():
        rows.append({"notice_id": nid, "status": "extracted", "skip_reason": None,
                     **extract.flat_row(r, docs_meta.get(nid) or {}), "effort": r.get("effort"),
                     "run_at": r.get("run_at"), "experimental": True,
                     "record_json": json.dumps(r, ensure_ascii=False)})
    seen = set(latest) | set(exclude)
    for s in _jsonl(out / "skipped.jsonl"):
        nid = s["notice_id"]
        if nid in seen:
            continue
        seen.add(nid)
        meta = docs_meta.get(nid) or {}
        rows.append({"notice_id": nid, "status": "skipped", "skip_reason": s.get("reason"),
                     **{k: meta.get(k) for k in _META},
                     "sam_link": meta.get("sam_link") or f"https://sam.gov/opp/{nid}/view",
                     "dataset_revision": s.get("dataset_revision"), "experimental": True,
                     "record_json": json.dumps(s, ensure_ascii=False)})
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["posted_date", "notice_id"], ascending=[False, True], na_position="last")
    return df.reset_index(drop=True)


def check_not_shrinking(new_rows: int, published_rows: int, excluded_rows: int = 0) -> None:
    """``excluded_rows``: rows left out on purpose (build_table's exclude);
    only those may account for a smaller table."""
    if new_rows + excluded_rows < published_rows:
        raise ShrinkError(f"new table has {new_rows} rows (+{excluded_rows} left out on purpose), "
                          f"the published one {published_rows}; not publishing")


def upload_table(path: Path, message: str, *, new_rows: int, excluded_rows: int = 0) -> str:
    """Commit the table to the dataset (needs HF_TOKEN). Returns the commit URL."""
    from huggingface_hub import HfApi

    check_not_shrinking(new_rows, len(_download_published()), excluded_rows)
    info = HfApi().upload_file(path_or_fileobj=str(path), path_in_repo=TABLE_PATH, repo_id=HF_DATASET,
                               repo_type="dataset", commit_message=message)
    return str(getattr(info, "commit_url", info))
