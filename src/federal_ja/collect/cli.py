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
                "local_path": p, "content_length": st.st_size,
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
    out = pipeline.transcribe_pending(store, _transcriber(args.max_usd), cands, checkpoint=store.save)
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
    sub.add_parser("reextract", help="re-run native extraction on downloaded files")
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
