"""Command line: `uv run ja-collect <command> --workdir DIR ...`

Backfill (local):
  ja-collect scan --workdir W --csv active.csv --csv FY2026.csv --csv FY2025.csv
  ja-collect resources --workdir W
  ja-collect download --workdir W
  ja-collect transcribe --workdir W --max-usd 5
  ja-collect report --workdir W
  ja-collect publish --workdir W --mode full --delete-stale

Daily (GitHub Actions):
  ja-collect daily --workdir W --max-usd 2 --publish
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys

from . import config, pipeline, report
from .sam_api import SamClient
from .sources import collect_ja_notices, head_etag, open_csv_path, open_csv_url
from .store import Store

log = logging.getLogger("federal_ja.collect")


def _source_order(label: str) -> tuple:
    """Active file first, then archives newest fiscal year first."""
    m = re.search(r"FY(\d{4})", label)
    return (1, -int(m.group(1))) if m else (0, 0)


def _record_run(store: Store, kind: str, info: dict) -> None:
    store.state.setdefault("runs", []).append({"at": pipeline.now_iso(), "kind": kind, **info})
    store.state["runs"] = store.state["runs"][-200:]


def _merge_scan(store: Store, sources: list, start: dt.date) -> dict:
    res = collect_ja_notices(sources, start)
    diff = pipeline.merge_notices(store, res)
    in_scope = set(res.notices)
    rec = report.reconcile(store, in_scope)
    info = {"scan": res.stats.as_dict(), "new": len(diff["new"]),
            "changed": len(diff["changed"]),
            "reconcile": {k: v for k, v in rec.items() if k != "unaccounted"},
            "unaccounted": rec["unaccounted"][:50]}
    return info


def cmd_scan(args, store: Store) -> dict:
    start = dt.date.fromisoformat(args.start_date)
    if args.csv:
        paths = sorted(args.csv, key=lambda p: _source_order(os.path.basename(p)))
        sources = [(os.path.basename(p), open_csv_path(p)) for p in paths]
        for p in paths:
            st = os.stat(p)
            store.state["sources"][os.path.basename(p)] = {
                "content_length": st.st_size,
                "local_mtime": dt.datetime.fromtimestamp(st.st_mtime, dt.timezone.utc).isoformat(),
            }
    else:
        sources = _remote_sources(args, store, start)
    info = _merge_scan(store, sources, start)
    _record_run(store, "scan", info)
    store.save()
    return info


def _remote_sources(args, store: Store, start: dt.date) -> list:
    items = [(config.ACTIVE_LABEL, config.ACTIVE_CSV_URL)]
    for fy in sorted(config.archive_fiscal_years(start), reverse=True):
        items.append((config.archive_label(fy), config.archive_url(fy)))
    sources = []
    for label, url in items:
        try:
            meta = head_etag(url)
        except Exception as e:  # noqa: BLE001
            if label == config.ACTIVE_LABEL:
                raise
            log.warning("archive %s not available: %s", label, e)
            continue
        prev = store.state["sources"].get(label, {})
        is_archive = label != config.ACTIVE_LABEL
        if is_archive and args.archives == "never":
            continue
        if is_archive and args.archives == "auto" and prev.get("etag") == meta["etag"]:
            log.info("archive %s unchanged (etag), skipping", label)
            continue
        store.state["sources"][label] = {"url": url, **meta}
        sources.append((label, open_csv_url(url)))
    return sources


def cmd_resources(args, store: Store) -> dict:
    client = SamClient(requests_per_second=args.rps)
    out = pipeline.fetch_resources(store, client, workers=args.workers,
                                   retry_errors=args.retry_errors, checkpoint=store.save)
    out["sam_retries"] = client.retry_count
    out["sam_status_counts"] = client.status_counts
    _record_run(store, "resources", out)
    store.save()
    return out


def cmd_download(args, store: Store) -> dict:
    client = SamClient(requests_per_second=args.rps)
    out = pipeline.download_pending(store, client, workers=args.workers,
                                    retry_errors=args.retry_errors, limit=args.limit,
                                    checkpoint=store.save)
    out["sam_retries"] = client.retry_count
    out["sam_status_counts"] = client.status_counts
    _record_run(store, "download", out)
    store.save()
    return out


def _batches(rows: list[dict], max_files: int, max_bytes: int, size_key: str):
    """Group rows (kept together by notice) into batches under both limits."""
    batch, nbytes, last_notice = [], 0, None
    for r in sorted(rows, key=lambda r: (r["notice_id"], r["resource_id"])):
        full = len(batch) >= max_files or nbytes >= max_bytes
        if batch and full and r["notice_id"] != last_notice:
            yield batch
            batch, nbytes = [], 0
        batch.append(r)
        nbytes += r.get(size_key) or 0
        last_notice = r["notice_id"]
    if batch:
        yield batch


def _local_bytes(store: Store) -> int:
    root = store.path("files")
    total = 0
    for d, _dirs, files in os.walk(root):
        for f in files:
            total += os.path.getsize(os.path.join(d, f))
    return total


def cmd_stream(args, store: Store) -> dict:
    """Backfill without holding files locally: per batch, download + extract,
    upload the originals to HF, verify each file there, delete the local copy.
    Resumable: rows with uploaded_at set are done; a restart skips them."""
    from . import hf_sync

    guard = int(args.min_free_gb * 1e9)
    keep = set()
    if args.keep_local:
        keep = {line.strip() for line in open(args.keep_local) if line.strip()}
    out = {"batches": [], "peak_local_file_bytes": 0, "status": "done"}

    def track():
        out["peak_local_file_bytes"] = max(out["peak_local_file_bytes"], _local_bytes(store))

    track()
    out["adopted"] = pipeline.adopt_existing_files(store)
    store.save()
    # local files from before the per-page rule: re-extract before they go
    out["reextracted"] = pipeline.reextract(store, only_old_rule=True)
    store.save()
    client = SamClient(requests_per_second=args.rps)
    max_bytes = int(args.batch_gb * 1e9)

    def upload(rows):
        res = hf_sync.upload_verify_delete(store, rows, pipeline.now_iso(), keep_local=keep)
        out["batches"].append(res)
        log.info("uploaded batch: %s", res)

    backlog = [a for a in store.attachments.values()
               if a.get("download_status") == "ok" and not a.get("uploaded_at")
               and a.get("file_path") and os.path.exists(store.path(a["file_path"]))]
    for batch in _batches(backlog, args.batch_files, max_bytes, "downloaded_size"):
        upload(batch)
    while True:
        if pipeline.free_bytes(store.workdir) < guard:
            out["status"] = f"paused: free disk below {args.min_free_gb} GB"
            break
        pending = [a for a in store.attachments.values() if a.get("download_status") == "pending"]
        if args.retry_errors:
            pending += [a for a in store.attachments.values() if a.get("download_status") == "error"]
        if not pending:
            break
        batch = next(_batches(pending, args.batch_files, max_bytes, "size"))
        try:
            pipeline.download_pending(store, client, workers=args.workers, attachments=batch,
                                      min_free_bytes=guard)
        except pipeline.LowDisk as e:
            out["status"] = f"paused: {e}"
        track()
        store.save()
        upload([a for a in batch if a.get("download_status") == "ok" and not a.get("uploaded_at")])
        if out["status"] != "done":
            break
        if args.retry_errors:
            args.retry_errors = False  # one retry pass only
    out["sam_retries"] = client.retry_count
    out["sam_status_counts"] = client.status_counts
    out["batches_uploaded"] = len(out["batches"])
    out["files_uploaded"] = sum(b.get("uploaded", 0) for b in out["batches"])
    _record_run(store, "stream", {k: v for k, v in out.items() if k != "batches"})
    store.save()
    return out


def cmd_pages(args, store: Store) -> dict:
    """Build data/pages.parquet from stored text (files already deleted)."""
    out = pipeline.pages_from_stored_text(store)
    store.save()
    return out


def cmd_reextract_remote(args, store: Store) -> dict:
    """Re-extract, from the HF copies, files whose extraction failed or that
    were extracted by an older rule (images/encrypted PDFs)."""
    from . import hf_sync
    rows = [a for a in store.attachments.values() if a.get("download_status") == "ok" and (
        (a.get("extract_error") or "") or a.get("file_kind") == "other")]
    n = pipeline.reextract_remote(store, rows, lambda a: hf_sync.fetch_file(store, a))
    store.save()
    return {"reextracted": n, "still_failing": sum(1 for a in rows if a.get("extract_error"))}


def cmd_reextract(args, store: Store) -> dict:
    n = pipeline.reextract(store)
    store.save()
    return {"reextracted": n}


def _transcriber(max_usd: float):
    from .transcribe import BudgetTracker, Transcriber
    return Transcriber(budget=BudgetTracker(max_usd))


def cmd_transcribe(args, store: Store) -> dict:
    cands = pipeline.transcription_candidates(store)
    if args.ids:
        want = {line.strip() for line in open(args.ids) if line.strip()}
        cands = [a for a in cands if a["resource_id"] in want]
    if args.limit:
        cands = cands[: args.limit]
    from . import hf_sync
    out = pipeline.transcribe_pending(store, _transcriber(args.max_usd), cands, checkpoint=store.save,
                                      fetch=lambda a: hf_sync.fetch_file(store, a))
    _record_run(store, "transcribe", out)
    store.save()
    return out


def cmd_report(args, store: Store) -> dict:
    return report.summarize(store)


def cmd_publish(args, store: Store) -> dict:
    from . import hf_sync
    from .card import render_card

    now = pipeline.now_iso()
    card = render_card(store, args.start_date, now)
    if args.mode == "full":
        return hf_sync.publish_full(store, card, now=now, delete_stale=args.delete_stale)
    return {"uploaded_files": hf_sync.publish_delta(store, card, now=now)}


def cmd_daily(args, store: Store) -> dict:
    from . import hf_sync

    pulled = hf_sync.pull_tables(args.workdir)
    if not pulled and not args.allow_empty:
        raise SystemExit("remote dataset is not in this project's layout yet; run the backfill "
                         "and `publish --mode full` first (or pass --allow-empty)")
    store = Store(args.workdir).load()
    out = {"scan": cmd_scan(args, store)}
    out["resources"] = cmd_resources(args, store)
    out["download"] = cmd_download(args, store)
    if args.max_usd > 0:
        out["transcribe"] = cmd_transcribe(args, store)
    if args.publish:
        out["publish"] = cmd_publish(args, store)
    return out


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="ja-collect")
    p.add_argument("--workdir", required=True)
    p.add_argument("--start-date", default=os.environ.get("JA_START_DATE",
                                                          config.DEFAULT_START_DATE.isoformat()))
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def net(sp):
        sp.add_argument("--rps", type=float, default=config.SAM_REQUESTS_PER_SECOND)
        sp.add_argument("--workers", type=int, default=4)
        sp.add_argument("--retry-errors", action="store_true")

    s = sub.add_parser("scan", help="read CSVs and update the notices table")
    s.add_argument("--csv", action="append", help="local CSV path (repeatable); default: remote")
    s.add_argument("--archives", choices=["auto", "always", "never"], default="auto")
    s = sub.add_parser("resources", help="fetch attachment lists for pending notices"); net(s)
    s = sub.add_parser("download", help="download pending files and extract text"); net(s)
    s.add_argument("--limit", type=int)
    s = sub.add_parser("stream", help="backfill in batches: download, upload to HF, verify, delete local"); net(s)
    s.add_argument("--batch-files", type=int, default=500)
    s.add_argument("--batch-gb", type=float, default=1.0)
    s.add_argument("--min-free-gb", type=float, default=3.0)
    s.add_argument("--keep-local", help="file of file_paths to keep on disk after upload")
    sub.add_parser("reextract", help="re-run native extraction on downloaded files")
    sub.add_parser("pages", help="rebuild per-page rows from stored text")
    sub.add_parser("reextract_remote", help="re-extract failed/unknown files from their HF copies")
    s = sub.add_parser("transcribe", help="transcribe scanned PDFs")
    s.add_argument("--max-usd", type=float, required=True)
    s.add_argument("--limit", type=int)
    s.add_argument("--ids", help="file of resource_ids to restrict to")
    sub.add_parser("report", help="print counts")
    s = sub.add_parser("publish", help="upload to Hugging Face")
    s.add_argument("--mode", choices=["delta", "full"], default="delta")
    s.add_argument("--delete-stale", action="store_true")
    s = sub.add_parser("daily", help="pull, scan, fetch, transcribe, publish"); net(s)
    s.add_argument("--csv", action="append", help=argparse.SUPPRESS)
    s.add_argument("--archives", choices=["auto", "always", "never"], default="auto")
    s.add_argument("--max-usd", type=float, default=0.0)
    s.add_argument("--limit", type=int)
    s.add_argument("--ids", help=argparse.SUPPRESS)
    s.add_argument("--publish", action="store_true")
    s.add_argument("--allow-empty", action="store_true")
    s.add_argument("--mode", default="delta", help=argparse.SUPPRESS)
    s.add_argument("--delete-stale", action="store_true", help=argparse.SUPPRESS)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    os.makedirs(args.workdir, exist_ok=True)
    store = Store(args.workdir).load()
    fn = globals()[f"cmd_{args.cmd}"]
    out = fn(args, store)
    json.dump(out, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
