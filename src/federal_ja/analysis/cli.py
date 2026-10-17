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
from pathlib import Path

from . import data, llm, prompts, tagger

EXPERIMENTAL_BANNER = (
    "EXPERIMENTAL. Tags are possible issues for a person to check against the document, never findings. "
    "Measured precision and recall, with intervals, are in docs/analysis_validation.md."
)


def _status_lines() -> str:
    out = []
    for c, cat in tagger.CATEGORIES.items():
        out.append(f"  {c:20s} {tagger.STATUS.get(c, '?'):12s} {cat.kind}; {tagger.STATUS_NOTE.get(c, '')}")
    for c, why in tagger.NOT_SHIPPED.items():
        out.append(f"  {c:20s} {why}")
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
    return ap


def _load_jsonl(p: Path) -> list[dict]:
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


def cmd_tag(a) -> int:
    print(EXPERIMENTAL_BANNER, file=sys.stderr)
    cats = a.category or tagger.shipped()
    for c in cats:
        if c in tagger.NOT_SHIPPED:
            raise SystemExit(f"{c}: {tagger.NOT_SHIPPED[c]}")
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
    done = {(r["notice_id"], c) for r in _load_jsonl(reviews_p)
            if r["model"] == a.model and r["prompt_version"] == prompts.PROMPT_VERSION for c in r["categories"]}
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
               "model": a.model, "prompt_version": prompts.PROMPT_VERSION,
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
    for doc, need in todo:
        if spent >= a.max_usd:
            print(f"spend cap ${a.max_usd} reached; stopping", file=sys.stderr)
            break
        try:
            review = tagger.tag_document(doc, need, model=a.model)
        except llm.ModelError as exc:
            failed += 1
            print(f"{doc.notice_id}: {exc}", file=sys.stderr)
            continue
        review["dataset_revision"] = rev.get("sha")
        spent += review["usage"]["cost_usd"] or 0
        with open(reviews_p, "a") as f:
            f.write(json.dumps(review, ensure_ascii=False) + "\n")
        with open(tags_p, "a") as f:
            for row in tagger.output_rows(review, doc, rev.get("sha")):
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(json.dumps({"spent_usd": round(spent, 4), "failed": failed}), file=sys.stderr)
    return 1 if failed else 0


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    if a.cmd == "tag":
        return cmd_tag(a)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
