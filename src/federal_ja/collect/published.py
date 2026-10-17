"""Build the tables as published on Hugging Face from the working tables.

`notices` stays the source of truth. At publish time:

* `notices` gets the `usaspending_*` columns (see usaspending.py);
* `attachments` and `pages` get copies of the main notice fields
  (agency, sub-tier, office, title, posted date, notice type, award number,
  SAM link), placed right after `notice_id`, so whichever table a reader
  opens shows which J&A a row belongs to.

The working copies in `<workdir>/data/` are not changed; the published files
are written to `<workdir>/publish/data/`. When a later run pulls the published
tables back, Store drops the extra columns on save.
"""

from __future__ import annotations

import os
import shutil

import pandas as pd

from . import usaspending
from .store import Store

# (column in notices, column name in attachments/pages)
NOTICE_COPIES = [
    ("agency", "agency"), ("sub_tier", "sub_tier"), ("office", "office"),
    ("title", "notice_title"), ("posted_date", "posted_date"), ("type", "notice_type"),
    ("award_number", "award_number"), ("sam_link", "sam_link"),
]
COPIED_COLUMNS = [dst for _src, dst in NOTICE_COPIES]

PUBLISH_DIR = "publish"


def _denormalise(df: pd.DataFrame, notices: pd.DataFrame) -> pd.DataFrame:
    clash = [c for c in COPIED_COLUMNS if c in df.columns]
    if clash:
        raise ValueError(f"columns already present, would be overwritten: {clash}")
    copy = notices[["notice_id"] + [s for s, _d in NOTICE_COPIES]].rename(
        columns=dict(NOTICE_COPIES))
    out = df.merge(copy, on="notice_id", how="left", validate="many_to_one")
    if len(out) != len(df):
        raise AssertionError("row count changed while adding notice fields")
    cols = list(df.columns)
    i = cols.index("notice_id") + 1
    return out[cols[:i] + COPIED_COLUMNS + cols[i:]]


def _with_usaspending(notices: pd.DataFrame, us_cols: dict[str, dict]) -> pd.DataFrame:
    missing = set(notices["notice_id"]) - set(us_cols)
    if missing:
        raise AssertionError(f"{len(missing)} notices have no USAspending join result")
    rows = [us_cols[nid] for nid in notices["notice_id"]]
    us = pd.DataFrame(rows, columns=usaspending.COLUMNS)
    for c in usaspending.COLUMNS:
        if c in usaspending.BOOL_COLS:
            us[c] = pd.array(us[c], dtype="boolean")
        elif c in usaspending.INT_COLS:
            us[c] = pd.array(us[c], dtype="Int64")
        elif c in usaspending.FLOAT_COLS:
            us[c] = pd.array(us[c], dtype="Float64")
        else:
            us[c] = pd.array([None if v is None or (isinstance(v, float) and pd.isna(v)) else str(v)
                              for v in us[c]], dtype="string")
    return pd.concat([notices.reset_index(drop=True), us], axis=1)


def write(store: Store, us_cols: dict[str, dict] | None) -> dict[str, str]:
    """Write the published tables. Returns {path in repo: local path}.

    us_cols None means the tables are built without the USAspending columns
    (only for tests / explicit --no-usaspending runs)."""
    from .hf_sync import TABLE_FILES

    store.save()
    out_dir = store.path(PUBLISH_DIR, "data")
    os.makedirs(out_dir, exist_ok=True)
    notices = pd.read_parquet(store.path("data", "notices.parquet"))
    paths = {}
    for rel in TABLE_FILES:
        name = os.path.basename(rel)
        dst = os.path.join(out_dir, name)
        if name == "notices.parquet":
            df = _with_usaspending(notices, us_cols) if us_cols is not None else notices
            df.to_parquet(dst + ".tmp", index=False)
            os.replace(dst + ".tmp", dst)
        elif name in ("attachments.parquet", "pages.parquet"):
            df = _denormalise(pd.read_parquet(store.path(rel)), notices)
            df.to_parquet(dst + ".tmp", index=False)
            os.replace(dst + ".tmp", dst)
        else:
            shutil.copyfile(store.path(rel), dst)
        paths[rel] = dst
    return paths
