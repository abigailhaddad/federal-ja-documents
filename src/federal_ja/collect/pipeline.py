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
import time
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
    if a["deleted_flag"] or a.get("effective_deleted_date"):
        # effectiveDeletedDate with deletedFlag "0" was seen on all 48 files
        # whose download then failed with "The resource has been deleted."
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

def set_pdf_pages(store: Store, a: dict, page_texts: list[str]) -> None:
    """(Re)build the per-page rows of a PDF from its native page texts.
    Existing transcriptions of pages that still need one are kept."""
    key = (a["notice_id"], a["resource_id"])
    old = {r["page_number"]: r for r in store.pages.get(key, [])}
    rows = []
    for i, t in enumerate(page_texts, 1):
        t = (t or "").strip()
        needs = not ex.page_has_text(t)
        row = {"notice_id": a["notice_id"], "resource_id": a["resource_id"],
               "sha256": a.get("sha256"), "page_number": i, "page_count": len(page_texts),
               "text_layer_chars": len(t), "needs_transcription": needs,
               "text": t, "text_source": "native" if t else "none", "text_layer_text": None,
               "transcription_model": None, "transcription_prompt_version": None,
               "transcribed_at": None}
        prev = old.get(i)
        if needs and prev and prev.get("text_source") == "transcription":
            row.update({k: prev[k] for k in ("text", "text_source", "transcription_model",
                                              "transcription_prompt_version", "transcribed_at")})
            row["text_layer_text"] = t
        rows.append(row)
    store.pages[key] = rows


def doc_text_from_pages(rows: list[dict]) -> tuple[str, str]:
    """Attachment-level (text, text_source) from its page rows."""
    sources = {r["text_source"] for r in rows}
    if not rows or sources == {"none"}:
        return "", "none"
    text = "\n\n".join(f"--- page {r['page_number']} ---\n{r['text'] or ''}" for r in rows)
    if "transcription" in sources:
        return text, ("pdf_text+transcription" if "native" in sources else "transcription")
    return text, "pdf_text"


def apply_extraction(a: dict, r: ex.ExtractResult, store: Store | None = None) -> None:
    a.update(file_kind=r.file_kind, page_count=r.page_count,
             pages_with_text=r.pages_with_text, textless_pages=list(r.textless_pages),
             native_text_chars=r.native_text_chars,
             needs_transcription=r.needs_transcription, extract_error=r.extract_error)
    if store is not None and r.file_kind in ("pdf", "image") and r.page_count:
        set_pdf_pages(store, a, r.page_texts)
        a["text"], a["text_source"] = doc_text_from_pages(store.pages[(a["notice_id"], a["resource_id"])])
        return
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
                apply_extraction(a, r, store)
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
        apply_extraction(a, ex.extract_file(p, a.get("filename") or ""), store)
        counts["adopted"] += 1
    return counts


def fetch_links(store: Store, rows: list[dict] | None = None, retry_errors: bool = False,
                fetcher=None, pause: float = 1.0, min_free_bytes: int = 0) -> dict:
    """Fetch the J&A documents behind SAM LINK resources on supported hosts
    (see links.py). The first document stays on the link's own row; extra
    documents from one link (a PIEE page can list several) get rows with
    resource_id '<link resource_id>-<n>' and parent_resource_id set."""
    import hashlib
    from . import links as lk

    fetcher = fetcher or lk.fetch_link
    want = {"link"} | ({"link_error"} if retry_errors else set())
    if rows is None:
        rows = [a for a in store.attachments.values()
                if a.get("resource_type") == "link" and a.get("download_status") in want
                and lk.link_kind(a.get("link_url") or "")]
    counts: dict[str, int] = {}
    for a in list(rows):
        if min_free_bytes and free_bytes(store.workdir) < min_free_bytes:
            raise LowDisk("free disk below guard during link fetch")
        url = a["link_url"]
        a["source_host"] = lk.source_host(url)
        try:
            docs = fetcher(url)
        except Exception as e:  # noqa: BLE001 - recorded per row
            a.update(download_status="link_error", download_error=f"{type(e).__name__}: {e}"[:300])
            counts["link_error"] = counts.get("link_error", 0) + 1
            time.sleep(pause)
            continue
        for k, d in enumerate(docs):
            if k == 0:
                row = a
            else:
                rid = f"{a['resource_id']}-{k + 1}"
                row = dict(a, resource_id=rid, parent_resource_id=a["resource_id"],
                           attachment_id="", attachment_order=a.get("attachment_order"))
                store.attachments[(a["notice_id"], rid)] = row
            rel = file_rel_path(row["notice_id"], row["resource_id"], d.name)
            dest = store.path(rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(d.data)
            row.update(download_status="ok", download_error="", downloaded_at=now_iso(),
                       downloaded_size=len(d.data), sha256=hashlib.sha256(d.data).hexdigest(),
                       file_path=rel, filename=d.name, uploaded_at=None, source_host=a["source_host"],
                       link_documents=len(docs))
            apply_extraction(row, ex.extract_file(dest, d.name), store)
            counts["ok"] = counts.get("ok", 0) + 1
        time.sleep(pause)
    return counts


def reextract_remote(store: Store, rows: list[dict], fetch: Callable[[dict], str]) -> int:
    """Re-extract files that are no longer local: fetch, extract, delete."""
    n = 0
    for a in rows:
        p = fetch(a)
        try:
            apply_extraction(a, ex.extract_file(p, a.get("filename") or ""), store)
            n += 1
        finally:
            os.remove(p)
    return n


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
                apply_extraction(a, ex.extract_file(p, a.get("filename") or ""), store)
                n += 1
    return n


def pages_from_stored_text(store: Store) -> dict:
    """Build page rows for downloaded PDFs from the stored attachment text
    (which carries our own '--- page N ---' markers), for files no longer on
    disk. Also re-applies the current page rule to needs_transcription /
    textless_pages. A PDF stored with no text at all gets page_count empty
    pages (every page was below the old threshold; the exact thin-layer
    count is refreshed when the file is transcribed)."""
    counts = {"pdfs": 0, "pages": 0, "no_text_pdfs": 0, "marker_mismatch": 0}
    for a in store.attachments.values():
        if a.get("download_status") != "ok" or a.get("file_kind") != "pdf" or not a.get("page_count"):
            continue
        n = a["page_count"]
        parts = transcribe_mod.split_by_markers(a.get("text") or "")
        parts.pop(0, None)
        if not a.get("text"):
            counts["no_text_pdfs"] += 1
        elif sorted(parts) != list(range(1, n + 1)):
            counts["marker_mismatch"] += 1
        texts = [parts.get(i, "") for i in range(1, n + 1)]
        set_pdf_pages(store, a, texts)
        tl = ex.textless_pages(texts)
        a["textless_pages"], a["pages_with_text"] = tl, n - len(tl)
        a["needs_transcription"] = bool(tl)
        a["text"], a["text_source"] = doc_text_from_pages(store.pages[(a["notice_id"], a["resource_id"])])
        counts["pdfs"] += 1
        counts["pages"] += n
    return counts


# --------------------------------------------------------------------------- transcription

def pages_to_transcribe(store: Store, a: dict) -> list[int]:
    return [r["page_number"] for r in store.pages.get((a["notice_id"], a["resource_id"]), [])
            if r["needs_transcription"] and r["text_source"] != "transcription"]


def reset_multipage_transcriptions(store: Store) -> dict:
    """Undo page transcriptions of every PDF that had two or more pages
    transcribed (those were sent in multi-page chunks), so they are redone
    one page per request. The thin native layer is restored as the text."""
    docs = pages = 0
    for key, rows in store.pages.items():
        tx = [r for r in rows if r["text_source"] == "transcription"]
        if len(tx) < 2:
            continue
        docs += 1
        for r in tx:
            layer = r.get("text_layer_text") or ""
            r.update(text=layer, text_source="native" if layer else "none", text_layer_text=None,
                     transcription_model=None, transcription_prompt_version=None, transcribed_at=None)
            pages += 1
        a = store.attachments.get(key)
        if a is not None:
            a["transcribed_pages"] = sorted(r["page_number"] for r in rows if r["text_source"] == "transcription")
            a["text"], a["text_source"] = doc_text_from_pages(rows)
    return {"docs": docs, "pages": pages}


PERMANENT_TX_ERRORS = ("refusal", "content filtering")


def recheck_page_rule(store: Store) -> dict:
    """Re-apply the current page rule to stored native page text (no file
    access): pages whose text layer is now judged unusable become
    needs_transcription, and their files' textless_pages are updated."""
    pages = docs = 0
    for key, rows in store.pages.items():
        changed = False
        for r in rows:
            if r["text_source"] == "transcription" or r["needs_transcription"]:
                continue
            if not ex.page_has_text(r["text"] or ""):
                r["needs_transcription"] = True
                pages += 1
                changed = True
        if changed:
            docs += 1
            a = store.attachments.get(key)
            if a is not None:
                tl = [r["page_number"] for r in rows if r["needs_transcription"]]
                a.update(textless_pages=tl, pages_with_text=len(rows) - len(tl), needs_transcription=True)
    return {"pages": pages, "docs": docs}


def transcription_candidates(store: Store, retry_declined: bool = False) -> list[dict]:
    """Files with image-only pages still to transcribe. Files the model
    declined (refusal / output blocked by content filtering) are skipped
    unless retry_declined, so the daily run does not pay for them again."""
    out = []
    for a in store.attachments.values():
        if not (a.get("download_status") == "ok" and a.get("file_kind") in ("pdf", "image")
                and a.get("needs_transcription") and pages_to_transcribe(store, a)):
            continue
        err = a.get("transcription_error") or ""
        if not retry_declined and any(m in err for m in PERMANENT_TX_ERRORS):
            continue
        out.append(a)
    return out


def apply_transcription(store: Store, a: dict, t) -> None:
    """Write a page-level transcription into the page rows and the attachment."""
    key = (a["notice_id"], a["resource_id"])
    now = now_iso()
    prev_pages = list(a.get("transcribed_pages") or [])
    a.update(transcription_model=t.model, transcription_prompt_version=t.prompt_version,
             transcription_input_tokens=(a.get("transcription_input_tokens") or 0) + t.input_tokens,
             transcription_output_tokens=(a.get("transcription_output_tokens") or 0) + t.output_tokens,
             transcription_cost_usd=round((a.get("transcription_cost_usd") or 0) + t.cost_usd, 6),
             transcribed_at=now, transcription_error=t.error,
             transcribed_pages=sorted(set(prev_pages) | set(t.page_texts)))
    by_page = {r["page_number"]: r for r in store.pages.get(key, [])}
    for p, text in t.page_texts.items():
        r = by_page.get(p)
        if r is None:
            continue
        layer = r["text"] if r["text_source"] != "transcription" else r.get("text_layer_text")
        r.update(text_layer_text=layer, text=text, text_source="transcription", transcription_model=t.model,
                 transcription_prompt_version=t.prompt_version, transcribed_at=now)
    a["text"], a["text_source"] = doc_text_from_pages(store.pages.get(key, []))


def transcribe_pending(store: Store, transcriber, attachments: list[dict] | None = None,
                       checkpoint: Callable[[], None] | None = None,
                       fetch: Callable[[dict], str] | None = None,
                       keep_fetched: bool = False, workers: int = 1,
                       checkpoint_every: int = 25, progress: Callable[[dict], None] | None = None) -> dict:
    """Transcribe the image-only pages of each candidate PDF.

    The file is read from the workdir if present, else obtained with
    ``fetch(row) -> local path`` (e.g. from the HF dataset) and deleted
    afterwards unless keep_fetched. Native page texts are re-extracted from
    the file first, so page rows and counts are exact for every transcribed
    PDF. With workers > 1, files are fetched and transcribed in threads;
    all table updates happen in the calling thread."""
    from .transcribe import BudgetExceeded

    todo = list(attachments if attachments is not None else transcription_candidates(store))
    stats = {"done": 0, "errors": 0, "cost_usd": 0.0, "pages": 0, "stopped_on_budget": False,
             "no_pages_left": 0}

    def job(a):
        path = store.path(a["file_path"])
        fetched = False
        try:
            if not os.path.exists(path):
                if fetch is None:
                    return a, None, None, "file not available locally"
                path, fetched = fetch(a), True
            if a.get("file_kind") == "image":
                return a, None, transcriber.transcribe_image(path), None
            native = ex.pdf_page_texts(path)
            done_pages = {r["page_number"] for r in store.pages.get((a["notice_id"], a["resource_id"]), [])
                          if r["text_source"] == "transcription"}
            pages = [p for p in ex.textless_pages(native) if p not in done_pages]
            t = transcriber.transcribe_pdf(path, len(native), pages) if pages else None
            return a, native, t, None
        except BudgetExceeded:
            return a, None, None, "BUDGET"
        except Exception as e:  # noqa: BLE001
            return a, None, None, f"{type(e).__name__}: {e}"[:300]
        finally:
            if fetched and not keep_fetched and os.path.exists(path):
                os.remove(path)

    def apply(res) -> bool:
        a, native, t, err = res
        if err == "BUDGET":
            stats["stopped_on_budget"] = True
            return False
        if err is not None:
            a["transcription_error"] = err
            stats["errors"] += 1
            return True
        if native is not None:
            set_pdf_pages(store, a, native)
            tl = ex.textless_pages(native)
            a.update(textless_pages=tl, pages_with_text=len(native) - len(tl),
                     needs_transcription=bool(tl))
        if t is None:
            a["text"], a["text_source"] = doc_text_from_pages(store.pages[(a["notice_id"], a["resource_id"])])
            stats["no_pages_left"] += 1
            return True
        apply_transcription(store, a, t)
        if t.error:
            stats["errors"] += 1
        else:
            stats["done"] += 1
        stats["pages"] += len(t.page_texts)
        stats["cost_usd"] += t.cost_usd
        n = stats["done"] + stats["errors"]
        if checkpoint and n % checkpoint_every == 0:
            checkpoint()
        if progress and n % checkpoint_every == 0:
            progress(stats)
        return True

    if workers <= 1:
        for a in todo:
            if not apply(job(a)):
                break
        return stats

    from concurrent.futures import FIRST_COMPLETED, wait
    pool = ThreadPoolExecutor(workers)
    pending, it, stop = set(), iter(todo), False
    try:
        while True:
            while not stop and len(pending) < workers * 2:
                a = next(it, None)
                if a is None:
                    break
                pending.add(pool.submit(job, a))
            if not pending:
                break
            finished, pending = wait(pending, return_when=FIRST_COMPLETED)
            for f in finished:
                if not apply(f.result()):
                    stop = True
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return stats
