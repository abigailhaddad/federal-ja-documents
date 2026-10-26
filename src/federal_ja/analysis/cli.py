"""``python -m federal_ja.analysis tag ...`` -- EXPERIMENTAL issue tagging.

Writes, under --out:
  reviews.jsonl   one line per reviewed document (including documents with no
                  tags), keyed by notice_id + model + prompt_version
  tags.jsonl      one line per tag (every row has "experimental": true)
  skipped.jsonl   documents not reviewed, with the reason

Resumable: a (notice, category) already reviewed with the same model and
prompt version is skipped. A document whose model call fails records nothing
and is retried on the next run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import data, extract, ja_check, llm, prompts, spreadsheet, table, tagger
from .promptfile import CONFIG

EXPERIMENTAL_BANNER = (
    "EXPERIMENTAL. Tags are possible issues for a person to check against the document, never findings. "
    "Measured precision and recall, with intervals, are in docs/analysis_validation.md."
)


def _status_lines() -> str:
    out = []
    for c, cat in tagger.CATEGORIES.items():
        out.append(f"  {c:20s} {tagger.STATUS.get(c, '?'):12s} {cat.kind}; {tagger.STATUS_NOTE.get(c, '')}")
    return "\n".join(out)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m federal_ja.analysis",
        description=EXPERIMENTAL_BANNER,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Categories and their validation status:\n" + _status_lines())
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tag", help="EXPERIMENTAL: tag possible issues in J&A documents",
                       description=EXPERIMENTAL_BANNER,
                       formatter_class=argparse.RawDescriptionHelpFormatter,
                       epilog="Categories and their validation status:\n" + _status_lines())
    t.add_argument("--category", action="append", default=None,
                   help="category to tag (repeatable). Default: every shipped category.")
    t.add_argument("--limit", type=int, default=None, help="review at most N documents (newest first)")
    t.add_argument("--notice-id", action="append", default=None, help="review only these notices (repeatable)")
    t.add_argument("--since", default=None, help="only notices posted on or after YYYY-MM-DD")
    t.add_argument("--out", required=True, type=Path, help="output directory")
    t.add_argument("--data-dir", type=Path, default=Path("work/hf_tables"),
                   help="where the dataset tables are cached (default work/hf_tables)")
    t.add_argument("--revision", default=None, help="dataset commit to use (default: latest)")
    t.add_argument("--model", default=os.environ.get("JA_MODEL", llm.DEFAULT_MODEL),
                   help=f"model (default {llm.DEFAULT_MODEL}, the only validated one)")
    t.add_argument("--max-usd", type=float, default=5.0, help="stop once this much has been spent (default 5)")
    t.add_argument("--dry-run", action="store_true", help="count documents in scope; make no model calls")
    t.add_argument("--workers", type=int, default=1,
                   help="documents reviewed in parallel (default 1). The spend cap is checked as "
                        "each document finishes, so it can overshoot by up to one document per worker.")
    x = sub.add_parser("extract", help="EXPERIMENTAL: extract fields (authority, value, vendor, dates...) "
                                       "from every J&A with a downloaded document",
                       description=EXTRACT_BANNER)
    x.add_argument("--limit", type=int, default=None, help="extract at most N notices (newest first)")
    x.add_argument("--notice-id", action="append", default=None, help="only these notices (repeatable)")
    x.add_argument("--notice-file", type=Path, default=None, help="file of notice ids, one per line")
    x.add_argument("--since", default=None, help="only notices posted on or after YYYY-MM-DD")
    x.add_argument("--out", required=True, type=Path, help="output directory")
    x.add_argument("--data-dir", type=Path, default=Path("work/hf_tables"),
                   help="where the dataset tables are cached (default work/hf_tables)")
    x.add_argument("--revision", default=None, help="dataset commit to use (default: latest)")
    x.add_argument("--model", default=os.environ.get("JA_MODEL", llm.DEFAULT_MODEL),
                   help=f"model (default {llm.DEFAULT_MODEL})")
    x.add_argument("--retries", type=int, default=1,
                   help="re-ask up to N times when a value fails the quote checks (default 1)")
    x.add_argument("--effort", default=CONFIG["extraction"].get("effort"), choices=["low", "medium", "high"],
                   help="effort level; the model thinks before answering (default from prompts.yaml: "
                        f"{CONFIG['extraction'].get('effort')})")
    x.add_argument("--max-usd", type=float, default=5.0, help="stop once this much has been spent (default 5)")
    x.add_argument("--dry-run", action="store_true", help="count notices in scope; make no model calls")
    x.add_argument("--workers", type=int, default=1, help="notices extracted in parallel (default 1)")
    x.add_argument("--batch", action="store_true",
                   help="send the notices through the Message Batches API (half price; results within 24 "
                        "hours, usually one). The spend cap picks how many notices to send from an "
                        "estimate, since a batch can't be stopped part way; --workers is ignored")
    x.add_argument("--batch-max-wait-min", type=float, default=200,
                   help="with --batch: cancel the batch after this many minutes and keep what came "
                        "back (default 200, under the workflow's 240-minute job limit)")
    x.add_argument("--csv-only", action="store_true",
                   help="make no model calls; rebuild extractions.csv from extractions.jsonl")
    x.add_argument("--reverify", action="store_true",
                   help="make no model calls; re-apply the current quote/value checks to the stored model "
                        "answers in extractions.jsonl, keeping the latest record per notice (rewritten in "
                        "place, previous copy kept as .bak), then rebuild extractions.csv")
    x.add_argument("--seed-from-hf", action="store_true",
                   help="if --out has no extractions.jsonl, fill it (and skipped.jsonl) from the published "
                        "analysis/extractions.parquet first, so only notices without a current-version row "
                        "are extracted")
    t2 = sub.add_parser("extract-table", help="EXPERIMENTAL: build analysis/extractions.parquet from an "
                                              "extract --out directory (and optionally upload it)")
    t2.add_argument("--out", required=True, type=Path, help="the extract --out directory")
    t2.add_argument("--table", required=True, type=Path, help="parquet file to write")
    t2.add_argument("--data-dir", type=Path, default=Path("work/hf_tables"),
                    help="cache of the dataset tables (for titles, agencies, links)")
    t2.add_argument("--revision", default=None, help="dataset commit to use (default: latest)")
    t2.add_argument("--publish", action="store_true",
                    help="upload the table to the dataset as analysis/extractions.parquet (needs HF_TOKEN); "
                         "refused if it has fewer rows than the published table")
    w = sub.add_parser("spreadsheet", help="build a reader-friendly .xlsx from the published dataset "
                                           "(includes the EXPERIMENTAL AI columns)")
    w.add_argument("--out", required=True, type=Path, help="output .xlsx path")
    w.add_argument("--revision", default=None,
                   help="dataset commit to read the AI tables from (default: latest)")
    from ..digest import run as digest_run
    d = sub.add_parser("digest", help="EXPERIMENTAL: the daily 'WOAH! J&As' email of the most remarkable new "
                                      "notices (see docs/digest.md)")
    digest_run.add_arguments(d)
    from ..digest import site as digest_site
    ds = sub.add_parser("digest-site", help="build the static web archive + RSS feed of sent digests "
                                            "(https://woah-jas.abigailhaddad.com; see docs/digest.md)")
    digest_site.add_arguments(ds)
    da = sub.add_parser("digest-archive-add", help="upload one archive_entry.json (written by a sent digest) to "
                                                   "the dataset's analysis/digest_archive/; never overwrites")
    da.add_argument("entry", type=Path, help="archive_entry.json from a digest --send run's --out directory")
    da.add_argument("--archive-dir", type=Path, default=None, help="a local archive directory instead")
    return ap


EXTRACT_BANNER = (
    "EXPERIMENTAL. Fields are read from each document by a language model. Every factual value carries a "
    "verbatim quote that was found in the document text, and values whose quote or value check failed are "
    "nulled and listed in values_dropped_by_checks. Summaries are the model's own words."
)


def _load_jsonl(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


def cmd_tag(a) -> int:
    print(EXPERIMENTAL_BANNER, file=sys.stderr)
    cats = a.category or tagger.shipped()
    for c in cats:
        if c not in tagger.CATEGORIES:
            raise SystemExit(f"unknown category {c!r}; choose from {', '.join(tagger.CATEGORIES)}")
        if tagger.STATUS.get(c) not in tagger.SHIPPED_STATUSES:
            raise SystemExit(f"{c}: {tagger.STATUS_NOTE.get(c, 'not shipped')} "
                             "(see docs/analysis_validation.md)")
    if not cats:
        raise SystemExit("no shipped categories")
    if a.model not in llm.VALIDATED_MODELS:
        print(f"WARNING: {a.model} has not been validated; the published numbers are for "
              f"{', '.join(sorted(llm.VALIDATED_MODELS))}.", file=sys.stderr)

    rev = data.download_tables(a.data_dir, revision=a.revision)
    notices, attachments, pages = data.load_tables(a.data_dir)
    if a.notice_id:
        notices = notices[notices.notice_id.isin(a.notice_id)]
    if a.since:
        notices = notices[notices.posted_date.fillna("") >= a.since]
    notices = notices.sort_values("posted_date", ascending=False)
    docs = data.build_documents(notices, attachments, pages, notices.notice_id.tolist())

    a.out.mkdir(parents=True, exist_ok=True)
    reviews_p, tags_p, skipped_p = a.out / "reviews.jsonl", a.out / "tags.jsonl", a.out / "skipped.jsonl"
    done = {(r["notice_id"], c) for r in _load_jsonl(reviews_p) if r["model"] == a.model
            for c in r["categories"]
            if r.get("prompt_versions", {}).get(c, r.get("prompt_version")) == prompts.prompt_version(c)}
    skipped_ids = {r["notice_id"] for r in _load_jsonl(skipped_p)}

    todo, skipped = [], {}
    for nid in notices.notice_id.tolist():
        doc = docs[nid]
        why = doc.skip_reason()
        if why:
            skipped[nid] = why
            continue
        need = [c for c in cats if (nid, c) not in done]
        if need:
            todo.append((doc, need))
    if a.limit is not None:
        todo = todo[:a.limit]
    summary = {"experimental": True, "dataset_revision": rev.get("sha"), "categories": cats,
               "model": a.model, "prompt_versions": {c: prompts.prompt_version(c) for c in cats},
               "notices": len(notices), "skipped": {k: list(skipped.values()).count(k) for k in set(skipped.values())},
               "to_review": len(todo)}
    print(json.dumps(summary), file=sys.stderr)
    with open(skipped_p, "a") as f:
        for nid, why in skipped.items():
            if nid not in skipped_ids:
                f.write(json.dumps({"notice_id": nid, "reason": why, "dataset_revision": rev.get("sha")}) + "\n")
    if a.dry_run:
        return 0

    spent, failed = 0.0, 0
    lock = threading.Lock()
    stop = threading.Event()

    def review_one(item):
        nonlocal spent, failed
        doc, need = item
        if stop.is_set():
            return
        try:
            review = tagger.tag_document(doc, need, model=a.model)
        except llm.ModelError as exc:
            with lock:
                failed += 1
                print(f"{doc.notice_id}: {exc}", file=sys.stderr)
            return
        review["dataset_revision"] = rev.get("sha")
        with lock:
            spent += review["usage"]["cost_usd"] or 0
            with open(reviews_p, "a") as f:
                f.write(json.dumps(review, ensure_ascii=False) + "\n")
            with open(tags_p, "a") as f:
                for row in tagger.output_rows(review, doc, rev.get("sha")):
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            if spent >= a.max_usd and not stop.is_set():
                stop.set()
                print(f"spend cap ${a.max_usd} reached; stopping", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        list(ex.map(review_one, todo))
    print(json.dumps({"spent_usd": round(spent, 4), "failed": failed}), file=sys.stderr)
    return 1 if failed else 0


def _latest_by_notice(rows: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for r in rows:
        out[r["notice_id"]] = r
    return out


def write_extract_csv(out: Path, docs_meta: dict[str, dict], model: str) -> int:
    """extractions.csv: one row per notice (the latest record for this model
    and extraction version)."""
    import pandas as pd
    recs = [r for r in _latest_by_notice(_load_jsonl(out / "extractions.jsonl")).values()
            if r["model"] == model and r["extract_version"] == extract.EXTRACT_VERSION]
    rows = [extract.flat_row(r, docs_meta.get(r["notice_id"], {})) for r in recs]
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["posted_date", "notice_id"], ascending=[False, True])
    df.to_csv(out / "extractions.csv", index=False)
    return len(df)


def cmd_extract(a) -> int:
    print(EXTRACT_BANNER, file=sys.stderr)
    if a.model not in llm.PRICES_PER_M:
        raise SystemExit(f"{a.model}: no price on file, so the spend cap can't work")
    rev = data.download_tables(a.data_dir, revision=a.revision)
    notices, attachments, pages = data.load_tables(a.data_dir)
    # Scope: notices with at least one downloaded attachment. Notices with
    # none have no document to read and are not part of the output at all.
    with_doc = set(attachments.loc[attachments.download_status == "ok", "notice_id"])
    notices = notices[notices.notice_id.isin(with_doc)]
    want = set(a.notice_id or [])
    if a.notice_file:
        want |= {ln.strip() for ln in a.notice_file.read_text().splitlines() if ln.strip()}
    if want:
        notices = notices[notices.notice_id.isin(want)]
    if a.since:
        notices = notices[notices.posted_date.fillna("") >= a.since]
    notices = notices.sort_values(["posted_date", "notice_id"], ascending=[False, True])
    docs = data.build_documents(notices, attachments, pages, notices.notice_id.tolist())
    # Special Notices whose documents don't look like a J&A (mostly notices of
    # intent to sole source) are out of scope: never sent to the model, and
    # extract-table leaves them out of the table (ja_check.py, 2026-10-26).
    not_ja = ja_check.not_ja_special_notices(notices, docs)
    notices = notices[~notices.notice_id.isin(not_ja)]
    docs = {nid: d for nid, d in docs.items() if nid not in not_ja}
    meta = {nid: {**d.meta} for nid, d in docs.items()}

    a.out.mkdir(parents=True, exist_ok=True)
    ext_p, fail_p, skipped_p = a.out / "extractions.jsonl", a.out / "failures.jsonl", a.out / "skipped.jsonl"
    if getattr(a, "seed_from_hf", False):
        print(json.dumps(table.seed_from_hf(a.out)), file=sys.stderr)
    if a.reverify:
        # one record per notice: the latest (earlier attempts stay in the .bak copy)
        recs = list(_latest_by_notice(_load_jsonl(ext_p)).values())
        tmp = ext_p.with_suffix(".jsonl.tmp")
        with open(tmp, "w") as f:
            for r in recs:
                if r["notice_id"] in docs:
                    r = extract.reverify(r, docs[r["notice_id"]])
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        ext_p.replace(ext_p.with_suffix(".jsonl.bak"))
        tmp.replace(ext_p)
        print(json.dumps({"reverified": len(recs)}), file=sys.stderr)
    if a.csv_only or a.reverify:
        n = write_extract_csv(a.out, meta, a.model)
        print(json.dumps({"csv_rows": n}), file=sys.stderr)
        return 0
    done = {r["notice_id"] for r in _load_jsonl(ext_p)
            if r["model"] == a.model and r["extract_version"] == extract.EXTRACT_VERSION
            and r.get("effort") == a.effort
            # a cut document is redone when the cutting rule changes
            and (not r.get("truncated") or r.get("truncation_version") == extract.TRUNCATION_VERSION)
            # and a document whose text changed since (2026-10-26: XFA forms
            # first stored as a "Please wait..." placeholder, now read)
            and (r["notice_id"] not in docs or r.get("chars_total") in (None, len(docs[r["notice_id"]].model_text())))}
    skip_recs = _load_jsonl(skipped_p)
    skipped_ids = {r["notice_id"] for r in skip_recs}
    # a notice the model refused is not sent again until the model or the
    # prompt version changes (2026-10-26: two notices were refused every run)
    refused = {r["notice_id"] for r in skip_recs if r.get("reason") == REASON_REFUSED
               and r.get("model") == a.model and r.get("extract_version") == extract.EXTRACT_VERSION}
    todo, skipped = [], {}
    for nid in notices.notice_id.tolist():
        doc = docs[nid]
        if not any(p.text.strip() for p in doc.pages) or len(doc.model_text()) < extract_min_chars():
            skipped[nid] = "no_text" if not doc.pages else "no_usable_text"
            continue
        if nid not in done and nid not in refused:
            todo.append(doc)
    if a.limit is not None:
        todo = todo[:a.limit]
    summary = {"experimental": True, "dataset_revision": rev.get("sha"), "model": a.model,
               "extract_version": extract.EXTRACT_VERSION, "in_scope": len(notices),
               "already_done": len(done & set(notices.notice_id)),
               "special_notices_not_ja": len(not_ja),
               "refused_before": len(refused & set(notices.notice_id)),
               "skipped": {k: list(skipped.values()).count(k) for k in set(skipped.values())},
               "to_extract": len(todo)}
    if a.batch and a.dry_run:
        # what sending every notice left would cost at batch prices (estimate)
        summary["batch_estimated_usd_all"] = round(sum(extract.estimate_batch_cost(d, a.model) for d in todo), 2)
    print(json.dumps(summary), file=sys.stderr)
    with open(skipped_p, "a") as f:
        for nid, why in skipped.items():
            if nid not in skipped_ids:
                f.write(json.dumps({"notice_id": nid, "reason": why, "dataset_revision": rev.get("sha")}) + "\n")
    if a.dry_run:
        return 0
    if a.batch:
        return _extract_batch(a, todo, rev, ext_p, fail_p, meta, skipped_p)

    spent, failed, n_done = 0.0, 0, 0
    lock = threading.Lock()
    stop = threading.Event()

    def one(doc):
        nonlocal spent, failed, n_done
        if stop.is_set():
            return
        try:
            rec = extract.extract_document(doc, model=a.model, retries=a.retries, effort=a.effort)
        except Exception as exc:   # ModelError, or any bug on one document: record it, keep going
            with lock:
                failed += 1
                with open(fail_p, "a") as f:
                    f.write(json.dumps({"notice_id": doc.notice_id, "model": a.model, "error": f"{type(exc).__name__}: {exc}"[:500],
                                        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}) + "\n")
                if str(exc) == llm.REFUSED:
                    _record_refusal(skipped_p, doc.notice_id, a, rev)
                print(f"{doc.notice_id}: {exc}", file=sys.stderr)
            return
        rec["dataset_revision"] = rev.get("sha")
        with lock:
            spent += rec["usage"]["cost_usd"] or 0
            n_done += 1
            with open(ext_p, "a") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            if n_done % 100 == 0:
                print(json.dumps({"done": n_done, "of": len(todo), "spent_usd": round(spent, 4),
                                  "failed": failed}), file=sys.stderr, flush=True)
            if spent >= a.max_usd and not stop.is_set():
                stop.set()
                print(f"spend cap ${a.max_usd} reached; stopping", file=sys.stderr)

    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        list(ex.map(one, todo))
    n = write_extract_csv(a.out, meta, a.model)
    print(json.dumps({"spent_usd": round(spent, 4), "extracted": n_done, "failed": failed,
                      "csv_rows": n}), file=sys.stderr)
    return 1 if failed else 0


REASON_REFUSED = "model_refused"


def _record_refusal(skipped_p, nid, a, rev) -> None:
    """A skip record that keeps the notice from being sent again for this
    model and prompt version. It shows in the table as a skipped row unless
    the notice already has an extraction (an older version's is kept)."""
    with open(skipped_p, "a") as f:
        f.write(json.dumps({"notice_id": nid, "reason": REASON_REFUSED, "model": a.model,
                            "extract_version": extract.EXTRACT_VERSION, "dataset_revision": rev.get("sha"),
                            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}) + "\n")


def _extract_batch(a, todo, rev, ext_p, fail_p, meta, skipped_p) -> int:
    """cmd_extract through the Message Batches API. Takes notices in order
    while their estimated cost fits under --max-usd, sends them as one batch
    (plus a retry batch), and stores records and failures as the loop does.
    Returns 2 if the batch could not be created at all (no credit, bad key),
    so the workflow fails loudly instead of publishing an unchanged table."""
    picked, est = [], 0.0
    for doc in todo:
        c = extract.estimate_batch_cost(doc, a.model)
        if est + c > a.max_usd:      # so --max-usd 0 sends nothing (rebuild the table only)
            break
        picked.append(doc)
        est += c
    print(json.dumps({"batch": True, "sending": len(picked), "of": len(todo),
                      "estimated_usd": round(est, 4)}), file=sys.stderr, flush=True)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def fail(nid, err):
        with open(fail_p, "a") as f:
            f.write(json.dumps({"notice_id": nid, "model": a.model, "error": err[:500], "at": stamp}) + "\n")

    try:
        results = extract.extract_documents_batch(picked, model=a.model, retries=a.retries, effort=a.effort,
                                                  max_wait_s=a.batch_max_wait_min * 60)
    except llm.ModelError as exc:
        for doc in picked:
            fail(doc.notice_id, f"ModelError: {exc}")
        print(f"batch not sent: {exc}", file=sys.stderr)
        return 2
    spent, n_done, failed = 0.0, 0, 0
    with open(ext_p, "a") as f:
        for nid, rec in results.items():
            if isinstance(rec, Exception):
                failed += 1
                fail(nid, f"{type(rec).__name__}: {rec}")
                if str(rec) == llm.REFUSED:
                    _record_refusal(skipped_p, nid, a, rev)
                print(f"{nid}: {rec}", file=sys.stderr)
                continue
            rec["dataset_revision"] = rev.get("sha")
            spent += rec["usage"]["cost_usd"] or 0
            n_done += 1
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    n = write_extract_csv(a.out, meta, a.model)
    print(json.dumps({"spent_usd": round(spent, 4), "estimated_usd": round(est, 4), "extracted": n_done,
                      "failed": failed, "not_sent": len(todo) - len(picked), "csv_rows": n}), file=sys.stderr)
    return 1 if failed else 0


def cmd_extract_table(a) -> int:
    rev = data.download_tables(a.data_dir, revision=a.revision)
    notices, attachments, pages = data.load_tables(a.data_dir)
    meta = {n["notice_id"]: data._notice_meta(n) for n in notices.to_dict("records")}
    # Left out of the table (2026-10-26), including rows already published:
    # Special Notices that don't look like a J&A (ja_check.py), and notices
    # whose document the model says is not a J&A. They go to the excluded
    # table instead, so the next run doesn't extract them again.
    ids = table.notice_ids(a.out)
    sn = notices[notices.notice_id.isin(ids)]
    not_ja = ja_check.not_ja_special_notices(sn, data.build_documents(sn, attachments, pages, sn.notice_id.tolist()))
    # ...except where a J&A-named attachment couldn't be read: the model's "no"
    # then only describes the other attachments (2026-10-26, faa1cd84)
    model_no = table.model_says_not_ja(a.out)
    mn = notices[notices.notice_id.isin(model_no)]
    mn_docs = data.build_documents(mn, attachments, pages, mn.notice_id.tolist())
    unread = {nid for nid, d in mn_docs.items() if ja_check.unreadable_ja_attachment(d)}
    reasons = {nid: table.REASON_MODEL_NO for nid in model_no - unread}
    reasons.update({nid: table.REASON_SPECIAL_NOTICE for nid in not_ja})
    df = table.build_table(a.out, meta, exclude=set(reasons))
    ex = table.build_excluded(a.out, meta, reasons)
    excluded_path = a.table.with_name(a.table.stem + "_excluded.parquet")
    a.table.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(a.table, index=False)
    ex.to_parquet(excluded_path, index=False)
    summary = {"rows": len(df), "dataset_revision": rev.get("sha"),
               "by_status": df["status"].value_counts().to_dict() if len(df) else {},
               "by_extract_version": df["extract_version"].fillna("none").value_counts().to_dict()
               if len(df) and "extract_version" in df else {},
               "excluded_rows": len(ex),
               "model_no_kept_unreadable_ja": len(unread),
               "excluded_by_reason": ex["exclude_reason"].value_counts().to_dict() if len(ex) else {}}
    if a.publish:
        summary["commit"] = table.upload_table(
            a.table, f"analysis/extractions.parquet: {len(df)} rows (dataset {str(rev.get('sha'))[:12]})",
            new_rows=len(df), excluded_path=excluded_path, excluded_rows=len(ex))
    print(json.dumps(summary, default=str), file=sys.stderr)
    return 0


def extract_min_chars() -> int:
    from .text import MIN_DOC_CHARS
    return MIN_DOC_CHARS


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if a.cmd == "tag":
        return cmd_tag(a)
    if a.cmd == "extract":
        return cmd_extract(a)
    if a.cmd == "extract-table":
        return cmd_extract_table(a)
    if a.cmd == "digest":
        from ..digest import run as digest_run
        return digest_run.run(a)
    if a.cmd == "digest-site":
        from ..digest import site as digest_site
        return digest_site.run(a)
    if a.cmd == "digest-archive-add":
        from ..digest import archive as digest_archive
        entry = json.loads(a.entry.read_text())
        if not entry.get("sent_at") or not entry.get("buttondown_email_id"):
            raise SystemExit("this entry has no sent_at / buttondown_email_id: only a SENT digest is archived")
        store = digest_archive.LocalArchive(a.archive_dir) if a.archive_dir else digest_archive.HFArchive()
        print(store.save(entry) or "already archived (same Buttondown email id for that day)")
        return 0
    if a.cmd == "spreadsheet":
        print(json.dumps(spreadsheet.main(a.out, a.revision), default=str), file=sys.stderr)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
