"""Pull dataset state from, and publish to, the Hugging Face dataset repo.

Daily runs never download the whole repo: they fetch only the small table and
state files, collect the day's delta into a fresh workdir, and commit only the
new files plus the updated tables.
"""

from __future__ import annotations

import json
import logging
import os

from . import config
from .store import LAYOUT_VERSION, Store

log = logging.getLogger(__name__)

TABLE_FILES = ["data/notices.parquet", "data/attachments.parquet", "data/pages.parquet",
               "data/exclusions.parquet", "data/collection_state.json"]


def fetch_file(store: Store, row: dict, repo_id: str = config.HF_REPO_ID) -> str:
    """Download one stored original from the HF dataset into the workdir
    (same relative path) and check its sha256 against the table."""
    import hashlib

    from huggingface_hub import hf_hub_download

    p = hf_hub_download(repo_id, row["file_path"], repo_type="dataset", local_dir=store.workdir)
    h = hashlib.sha256(open(p, "rb").read()).hexdigest()
    if row.get("sha256") and h != row["sha256"]:
        os.remove(p)
        raise UploadVerifyError(f"sha256 mismatch fetching {row['file_path']}")
    return p


def _api():
    from huggingface_hub import HfApi
    return HfApi()


def remote_has_layout(repo_id: str = config.HF_REPO_ID) -> bool:
    from huggingface_hub import hf_hub_download
    from huggingface_hub.utils import EntryNotFoundError

    try:
        p = hf_hub_download(repo_id, "data/collection_state.json", repo_type="dataset")
    except EntryNotFoundError:
        return False
    with open(p) as f:
        return json.load(f).get("layout") == LAYOUT_VERSION


def pull_tables(workdir: str, repo_id: str = config.HF_REPO_ID) -> bool:
    """Download table/state files into workdir. False if the repo is not in our layout."""
    from huggingface_hub import hf_hub_download

    if not remote_has_layout(repo_id):
        return False
    for rel in TABLE_FILES:
        hf_hub_download(repo_id, rel, repo_type="dataset", local_dir=workdir)
    return True


def files_to_upload(store: Store) -> list[str]:
    return sorted(a["file_path"] for a in store.attachments.values()
                  if a.get("download_status") == "ok" and a.get("file_path")
                  and not a.get("uploaded_at") and os.path.exists(store.path(a["file_path"])))


def publish_delta(store: Store, card_text: str, repo_id: str = config.HF_REPO_ID,
                  batch_size: int = 500, now: str = "") -> int:
    """Commit new files in batches, then the tables and the dataset card."""
    from huggingface_hub import CommitOperationAdd

    api = _api()
    paths = files_to_upload(store)
    by_path = {a["file_path"]: a for a in store.attachments.values() if a.get("file_path")}
    for i in range(0, len(paths), batch_size):
        batch = paths[i:i + batch_size]
        ops = [CommitOperationAdd(path_in_repo=p, path_or_fileobj=store.path(p)) for p in batch]
        api.create_commit(repo_id, ops, repo_type="dataset",
                          commit_message=f"Add {len(batch)} attachment files")
        for p in batch:
            by_path[p]["uploaded_at"] = now
    store.save()
    with open(store.path("README.md"), "w") as f:
        f.write(card_text)
    ops = [CommitOperationAdd(path_in_repo=p, path_or_fileobj=store.path(p))
           for p in TABLE_FILES + ["README.md"]]
    api.create_commit(repo_id, ops, repo_type="dataset",
                      commit_message="Update notices, attachments and dataset card")
    return len(paths)


def publish_full(store: Store, card_text: str, repo_id: str = config.HF_REPO_ID,
                 now: str = "", delete_stale: bool = False) -> dict:
    """Initial upload of a complete local workdir (replaces an older layout).

    Uses upload_large_folder (resumable, multi-commit). With delete_stale,
    remote files that are not part of this layout are deleted afterwards.
    """
    from huggingface_hub import CommitOperationAdd, CommitOperationDelete

    api = _api()
    api.upload_large_folder(repo_id=repo_id, folder_path=store.workdir, repo_type="dataset",
                            allow_patterns=["files/**"])
    # Only after the files are up do we mark them uploaded and push the tables.
    for a in store.attachments.values():
        if a.get("download_status") == "ok" and a.get("file_path"):
            a["uploaded_at"] = now
    store.save()
    with open(store.path("README.md"), "w") as f:
        f.write(card_text)
    ops = [CommitOperationAdd(path_in_repo=p, path_or_fileobj=store.path(p))
           for p in TABLE_FILES + ["README.md"]]
    api.create_commit(repo_id, ops, repo_type="dataset",
                      commit_message="Notices, attachments and dataset card")
    deleted = 0
    if delete_stale:
        keep = set(TABLE_FILES) | {"README.md", ".gitattributes"} | set(
            a["file_path"] for a in store.attachments.values() if a.get("file_path"))
        stale = [p for p in api.list_repo_files(repo_id, repo_type="dataset") if p not in keep]
        for i in range(0, len(stale), 1000):
            ops = [CommitOperationDelete(path_in_repo=p) for p in stale[i:i + 1000]]
            api.create_commit(repo_id, ops, repo_type="dataset",
                              commit_message="Remove files from the previous snapshot layout")
        deleted = len(stale)
    return {"deleted_stale": deleted}


# --------------------------------------------------------------------------- streaming backfill

MANIFEST = "data/upload_manifest.jsonl"


class UploadVerifyError(Exception):
    pass


def verify_remote(api, repo_id: str, rows: list[dict]) -> list[str]:
    """Return problems for rows whose remote file is missing or differs.

    Checks size always, and sha256 whenever the hub reports one (LFS/xet
    files)."""
    problems = []
    paths = [r["file_path"] for r in rows]
    infos = {}
    for i in range(0, len(paths), 200):
        for info in api.get_paths_info(repo_id, paths[i:i + 200], repo_type="dataset"):
            infos[info.path] = info
    for r in rows:
        info = infos.get(r["file_path"])
        if info is None:
            problems.append(f"missing: {r['file_path']}")
            continue
        if getattr(info, "size", None) != r["downloaded_size"]:
            problems.append(f"size {getattr(info, 'size', None)} != {r['downloaded_size']}: {r['file_path']}")
        lfs = getattr(info, "lfs", None)
        sha = getattr(lfs, "sha256", None) if lfs else None
        if sha and sha != r["sha256"]:
            problems.append(f"sha256 mismatch: {r['file_path']}")
    return problems


def upload_verify_delete(store: Store, rows: list[dict], now: str,
                         repo_id: str = config.HF_REPO_ID, keep_local: set[str] = frozenset(),
                         api=None) -> dict:
    """Commit these rows' files in one commit, verify each on the hub, then
    delete the local copies (except keep_local) and append to the manifest."""
    from huggingface_hub import CommitOperationAdd

    api = api or _api()
    rows = [r for r in rows if r.get("file_path") and os.path.exists(store.path(r["file_path"]))]
    if not rows:
        return {"uploaded": 0}
    ops = [CommitOperationAdd(path_in_repo=r["file_path"], path_or_fileobj=store.path(r["file_path"]))
           for r in rows]
    nbytes = sum(r["downloaded_size"] or 0 for r in rows)
    info = api.create_commit(repo_id, ops, repo_type="dataset",
                             commit_message=f"Add {len(rows)} J&A attachment files")
    commit = getattr(info, "oid", "") or ""
    problems = verify_remote(api, repo_id, rows)
    if problems:
        raise UploadVerifyError(f"{len(problems)} problems after commit {commit}: {problems[:5]}")
    os.makedirs(store.data_dir, exist_ok=True)
    with open(store.path(MANIFEST), "a") as mf:
        for r in rows:
            r["uploaded_at"] = now
            r["uploaded_commit"] = commit
            mf.write(json.dumps({"file_path": r["file_path"], "notice_id": r["notice_id"],
                                 "resource_id": r["resource_id"], "size": r["downloaded_size"],
                                 "sha256": r["sha256"], "commit": commit, "verified_at": now}) + "\n")
            if r["file_path"] not in keep_local:
                os.remove(store.path(r["file_path"]))
                r["local_copy"] = False
                d = os.path.dirname(store.path(r["file_path"]))
                if os.path.isdir(d) and not os.listdir(d):
                    os.rmdir(d)
            else:
                r["local_copy"] = True
    store.save()
    return {"uploaded": len(rows), "bytes": nbytes, "commit": commit}
