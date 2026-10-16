"""Counts and reconciliation over a Store."""

from __future__ import annotations

from collections import Counter

from .store import Store


def summarize(store: Store) -> dict:
    n = store.notices.values()
    a = list(store.attachments.values())
    files = [x for x in a if x.get("resource_type") == "file"]
    ok = [x for x in a if x.get("download_status") == "ok"]
    size_mismatch = [x for x in ok if (x.get("size") or 0) and x.get("downloaded_size") != x.get("size")]
    return {
        "notices": len(store.notices),
        "notices_by_type": dict(Counter(x.get("type") for x in n)),
        "notices_by_resources_status": dict(Counter(
            (x.get("resources_status") or "pending").split(":")[0] for x in n)),
        "notices_by_source_file": dict(Counter(x.get("source_file") for x in n)),
        "posted_date_min": min((x.get("posted_date") or "" for x in n), default=None),
        "posted_date_max": max((x.get("posted_date") or "" for x in n), default=None),
        "attachments": len(a),
        "attachments_by_resource_type": dict(Counter(x.get("resource_type") for x in a)),
        "file_attachments": len(files),
        "attachments_by_download_status": dict(Counter(x.get("download_status") for x in a)),
        "download_errors": dict(Counter((x.get("download_error") or "")[:80]
                                        for x in a if x.get("download_status") in ("error", "too_large", "deleted_at_download"))),
        "downloaded_bytes": sum(x.get("downloaded_size") or 0 for x in ok),
        "declared_size_mismatches": len(size_mismatch),
        "downloaded_by_file_kind": dict(Counter(x.get("file_kind") for x in ok)),
        "text_source": dict(Counter(x.get("text_source") for x in ok)),
        "pdfs": sum(1 for x in ok if x.get("file_kind") == "pdf"),
        "pdfs_needing_transcription": sum(1 for x in ok if x.get("file_kind") == "pdf" and x.get("needs_transcription")),
        "pdf_pages": sum(x.get("page_count") or 0 for x in ok if x.get("file_kind") == "pdf"),
        "textless_pages": sum(len(x.get("textless_pages") or []) for x in ok if x.get("file_kind") == "pdf"),
        "pdfs_all_pages_textless": sum(1 for x in ok if x.get("file_kind") == "pdf"
                                       and x.get("page_count") and x.get("pages_with_text") == 0),
        "pdf_extract_errors": sum(1 for x in ok if x.get("file_kind") == "pdf" and x.get("extract_error")),
        "not_in_latest_listing": sum(1 for x in a if x.get("in_latest_listing") is False),
        "notices_with_downloaded_text": len({x["notice_id"] for x in ok if x.get("text")}),
        "exclusions": dict(Counter(e["reason"] for e in store.exclusions)),
    }


def reconcile(store: Store, in_scope_ids: set[str]) -> dict:
    """Every in-scope J&A NoticeId from the CSVs must be a notice or an exclusion."""
    excluded = {e["notice_id"] for e in store.exclusions}
    missing = sorted(in_scope_ids - set(store.notices) - excluded)
    extra = sorted(set(store.notices) - in_scope_ids)
    return {"in_scope_ids": len(in_scope_ids), "in_notices": len(in_scope_ids & set(store.notices)),
            "in_exclusions": len(in_scope_ids & excluded), "unaccounted": missing,
            "notices_not_in_this_scan": len(extra)}
