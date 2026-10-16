"""Collection steps. Each step reads and updates a Store and is resumable:
re-running a step only touches rows still pending (or failed, on request).

  merge_notices      CSV scan result -> notices table (new / changed detection)
  fetch_resources    attachment lists for notices with resources_status pending
  download_pending   download + native text extraction for pending files
  transcribe_pending machine transcription for PDFs that need it
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable

from . import extract as ex
from . import transcribe as transcribe_mod
from .sam_api import FileTooLarge, SamClient, SamError
from .sources import CollectResult
from .store import Store, file_rel_path

log = logging.getLogger(__name__)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- notices

def merge_notices(store: Store, result: CollectResult, now: str | None = None) -> dict:
    """Merge a CSV scan into the store.

    New notice ids get resources_status='pending'. A known notice whose row
    fingerprint changed (edited on SAM) is also set back to 'pending' so its
    attachment list is re-checked. Returns {'new': [...], 'changed': [...]}.
    """
    now = now or now_iso()
    new, changed = [], []
    for nid, rec in result.notices.items():
        old = store.notices.get(nid)
        if old is None:
            rec = dict(rec, first_seen_at=now, resources_status="pending",
                       resources_checked_at=None, attachment_count=None)
            store.notices[nid] = rec
            new.append(nid)
            continue
        merged_sources = list(dict.fromkeys(list(old.get("source_files") or []) + list(rec["source_files"])))
        keep = {k: old.get(k) for k in ("first_seen_at", "resources_status",
                                        "resources_checked_at", "attachment_count")}
        if old.get("row_fingerprint") != rec["row_fingerprint"]:
            # Only adopt the new row if it is not older than what we have.
            if (rec.get("posted_date") or "") >= (old.get("posted_date") or ""):
                keep["resources_status"] = "pending"
                changed.append(nid)
                store.notices[nid] = dict(rec, **keep, source_files=merged_sources)
            else:
                store.notices[nid] = dict(old, source_files=merged_sources)
        else:
            store.notices[nid] = dict(rec, **keep, source_files=merged_sources)
    # Exclusions: union by (notice_id, source_file, reason)
    seen = {(e["notice_id"], e["source_file"], e["reason"]) for e in store.exclusions}
    for e in result.exclusions:
        k = (e["notice_id"], e["source_file"], e["reason"])
        if k not in seen:
            store.exclusions.append(e)
            seen.add(k)
    return {"new": new, "changed": changed}


# --------------------------------------------------------------------------- resources

def initial_download_status(a: dict) -> str:
    if a["resource_type"] == "link":
        return "link"
    if a["resource_type"] != "file":
        return "unsupported_type"
    if a["deleted_flag"]:
        return "deleted"
    if (a.get("access_level") or "").lower() != "public":
        return "not_public"
    return "pending"


def apply_resource_listing(store: Store, notice_id: str, listing: list[dict]) -> None:
    listed = set()
    for a in listing:
        rid = a["resource_id"]
        if not rid:
            continue
        listed.add(rid)
        key = (notice_id, rid)
        old = store.attachments.get(key)
        rec = dict(a, notice_id=notice_id, in_latest_listing=True)
        if old is None:
            rec["download_status"] = initial_download_status(a)
            rec["text_source"] = "none"
            rec["text"] = ""
            store.attachments[key] = rec
        else:
            merged = dict(old, **rec)
            # A file that was deleted/private before and is public now becomes downloadable.
            if old.get("download_status") in ("deleted", "not_public") and initial_download_status(a) == "pending":
                merged["download_status"] = "pending"
            store.attachments[key] = merged
    for (n, rid), rec in store.attachments.items():
        if n == notice_id and rid not in listed:
            rec["in_latest_listing"] = False


def fetch_resources(store: Store, client: SamClient, notice_ids: Iterable[str] | None = None,
                    workers: int = 4, retry_errors: bool = False,
                    checkpoint: Callable[[], None] | None = None,
                    checkpoint_every: int = 500) -> dict:
    if notice_ids is None:
        want = {"pending"} | ({"error"} if retry_errors else set())
        notice_ids = [nid for nid, n in store.notices.items()
                      if (n.get("resources_status") or "pending").split(":")[0] in want]
    notice_ids = list(notice_ids)
    counts = {"ok": 0, "no_attachments": 0, "error": 0}
    log.info("fetching attachment lists for %d notices", len(notice_ids))

    def job(nid):
        try:
            return nid, client.list_resources(nid), None
        except SamError as e:
            return nid, None, str(e)
        except Exception as e:  # noqa: BLE001 - recorded, never silently dropped
            return nid, None, f"{type(e).__name__}: {e}"

    done = 0
    with ThreadPoolExecutor(workers) as pool:
        for fut in as_completed([pool.submit(job, n) for n in notice_ids]):
            nid, listing, err = fut.result()
            n = store.notices[nid]
            n["resources_checked_at"] = now_iso()
            if err is not None:
                n["resources_status"] = f"error: {err}"[:300]
                counts["error"] += 1
            else:
                apply_resource_listing(store, nid, listing)
                n["attachment_count"] = len(listing)
                n["resources_status"] = "ok" if listing else "no_attachments"
                counts["ok" if listing else "no_attachments"] += 1
            done += 1
            if checkpoint and done % checkpoint_every == 0:
                log.info("resources: %d/%d", done, len(notice_ids))
                checkpoint()
    return counts


# --------------------------------------------------------------------------- download

TRANSCRIBED_SOURCES = ("transcription", "pdf_text+transcription")

def apply_extraction(a: dict, r: ex.ExtractResult) -> None:
    a.update(file_kind=r.file_kind, page_count=r.page_count,
             pages_with_text=r.pages_with_text, textless_pages=list(r.textless_pages),
             native_text_chars=r.native_text_chars,
             needs_transcription=r.needs_transcription, extract_error=r.extract_error)
    # never overwrite a transcription with an empty native result
    if a.get("text_source") not in TRANSCRIBED_SOURCES:
        a["text"], a["text_source"] = r.text, r.text_source


class LowDisk(Exception):
    pass


def free_bytes(path: str) -> int:
    return shutil.disk_usage(path).free


def download_pending(store: Store, client: SamClient, workers: int = 4,
                     retry_errors: bool = False, limit: int | None = None,
                     checkpoint: Callable[[], None] | None = None,
                     checkpoint_every: int = 200,
                     attachments: list[dict] | None = None,
                     min_free_bytes: int = 0) -> dict:
    """Download + extract. With min_free_bytes, stops (raising LowDisk after
    recording what finished) before free space drops below the guard; rows not
    attempted stay 'pending'."""
    if attachments is not None:
        todo = list(attachments)
    else:
        want = {"pending"} | ({"error"} if retry_errors else set())
        todo = [a for a in store.attachments.values() if a.get("download_status") in want]
    if limit:
        todo = todo[:limit]
    counts: dict[str, int] = {}
    log.info("downloading %d files", len(todo))
    os.makedirs(store.workdir, exist_ok=True)

    def job(a):
        if min_free_bytes and free_bytes(store.workdir) < min_free_bytes:
            return a, None, None, None, "LOWDISK"
        rel = file_rel_path(a["notice_id"], a["resource_id"], a.get("filename") or "")
        dest = store.path(rel)
        try:
            dl = client.download(a["resource_id"], dest)
        except FileTooLarge as e:
            return a, rel, None, None, f"too_large: {e}"
        except SamError as e:
            return a, rel, None, None, str(e)
        except Exception as e:  # noqa: BLE001
            return a, rel, None, None, f"{type(e).__name__}: {e}"
        try:
            r = ex.extract_file(dest, a.get("filename") or "")
        except Exception as e:  # noqa: BLE001
            r = ex.ExtractResult(file_kind="other", extract_error=f"{type(e).__name__}: {e}"[:500])
        return a, rel, dl, r, None

    done = 0
    low_disk = False
    pool = ThreadPoolExecutor(workers)
    try:
        for fut in as_completed([pool.submit(job, a) for a in todo]):
            a, rel, dl, r, err = fut.result()
            if err == "LOWDISK":
                low_disk = True
                continue  # stays pending
            if err is not None:
                if err.startswith("too_large"):
                    status = "too_large"
                elif "has been deleted" in err:
                    status = "deleted_at_download"
                else:
                    status = "error"
                a.update(download_status=status, download_error=err[:300])
            else:
                a.update(download_status="ok", download_error="", downloaded_at=now_iso(),
                         downloaded_size=dl.bytes_written, sha256=dl.sha256, file_path=rel,
                         uploaded_at=None)
                apply_extraction(a, r)
            counts[a["download_status"]] = counts.get(a["download_status"], 0) + 1
            done += 1
            if checkpoint and done % checkpoint_every == 0:
                log.info("downloads: %d/%d %s", done, len(todo), counts)
                checkpoint()
    except BaseException:
        # Don't let queued jobs keep downloading after the main loop died
        # (this happened once when a checkpoint hit a full disk).
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    if low_disk:
        raise LowDisk(f"free disk below {min_free_bytes / 1e9:.1f} GB; stopped with {counts}")
    return counts


def adopt_existing_files(store: Store) -> dict:
    """Mark not-yet-recorded attachments whose file is already on disk as
    downloaded (hash + extract), e.g. after a run was killed between
    checkpoints. Files are only renamed into place once complete, so a file at
    its final path is whole; its size is still checked against SAM's."""
    import hashlib

    counts = {"adopted": 0, "size_mismatch": 0}
    for a in store.attachments.values():
        if a.get("download_status") == "ok" or a.get("resource_type") != "file":
            continue
        rel = file_rel_path(a["notice_id"], a["resource_id"], a.get("filename") or "")
        p = store.path(rel)
        if not os.path.exists(p):
            continue
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        size = os.path.getsize(p)
        if a.get("size") and size != a["size"]:
            counts["size_mismatch"] += 1
        a.update(download_status="ok", download_error="", downloaded_at=now_iso(),
                 downloaded_size=size, sha256=h.hexdigest(), file_path=rel, uploaded_at=None)
        apply_extraction(a, ex.extract_file(p, a.get("filename") or ""))
        counts["adopted"] += 1
    return counts


def reextract(store: Store, only_old_rule: bool = False) -> int:
    """Re-run native extraction on downloaded files still on disk (after rule
    changes). only_old_rule: just PDFs extracted before textless_pages existed."""
    n = 0
    for a in store.attachments.values():
        if only_old_rule and not (a.get("file_kind") == "pdf" and a.get("textless_pages") is None):
            continue
        if a.get("download_status") == "ok" and a.get("file_path"):
            p = store.path(a["file_path"])
            if os.path.exists(p):
                apply_extraction(a, ex.extract_file(p, a.get("filename") or ""))
                n += 1
    return n


# --------------------------------------------------------------------------- transcription

def transcription_candidates(store: Store) -> list[dict]:
    return [a for a in store.attachments.values()
            if a.get("download_status") == "ok" and a.get("file_kind") == "pdf"
            and a.get("needs_transcription") and a.get("text_source") not in TRANSCRIBED_SOURCES
            and (a.get("page_count") or 0) > 0]


def apply_transcription(a: dict, t, native_page_texts: list[str]) -> None:
    """Merge a page-level transcription into an attachment row."""
    a.update(transcription_model=t.model, transcription_prompt_version=t.prompt_version,
             transcription_input_tokens=t.input_tokens,
             transcription_output_tokens=t.output_tokens,
             transcription_cost_usd=round(t.cost_usd, 6), transcribed_at=now_iso(),
             transcription_error=t.error, transcribed_pages=sorted(t.page_texts))
    if t.error or not t.page_texts:
        return
    a["text"] = transcribe_mod.merge_page_texts(native_page_texts, t.page_texts)
    all_pages = set(range(1, len(native_page_texts) + 1))
    a["text_source"] = "transcription" if set(t.page_texts) >= all_pages else "pdf_text+transcription"


def transcribe_pending(store: Store, transcriber, attachments: list[dict] | None = None,
                       checkpoint: Callable[[], None] | None = None) -> dict:
    from .transcribe import BudgetExceeded

    todo = attachments if attachments is not None else transcription_candidates(store)
    stats = {"done": 0, "errors": 0, "cost_usd": 0.0, "pages": 0, "stopped_on_budget": False}
    for a in todo:
        path = store.path(a["file_path"])
        try:
            native = ex.pdf_page_texts(path)
            pages = list(a.get("textless_pages") or ex.textless_pages(native))
            t = transcriber.transcribe_pdf(path, len(native), pages)
        except BudgetExceeded:
            stats["stopped_on_budget"] = True
            break
        except Exception as e:  # noqa: BLE001
            a["transcription_error"] = f"{type(e).__name__}: {e}"[:300]
            stats["errors"] += 1
            continue
        apply_transcription(a, t, native)
        stats["done"] += 1
        stats["pages"] += t.pages
        stats["cost_usd"] += t.cost_usd
        if checkpoint and stats["done"] % 25 == 0:
            checkpoint()
    return stats
