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

TABLE_FILES = ["data/notices.parquet", "data/attachments.parquet",
               "data/exclusions.parquet", "data/collection_state.json"]


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
