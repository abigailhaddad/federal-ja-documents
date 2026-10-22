"""``python -m federal_ja.analysis digest``: build (and optionally send) one digest.

Steps: read the state (state.py) -> choose the window -> read the candidates
at one dataset commit (candidates.py) -> screen if there are many, then rank
(model.py) -> check every quote and figure (verify.py) -> render (render.py)
-> write ``digest.md``, ``picks.json`` and ``archive_entry.json`` under --out
-> with --send, send through Buttondown and only then save the new state and
the public archive entry (archive.py; the web archive + RSS feed are built
from those entries).

--dry-run never sends, never writes the state or the archive and never calls
Buttondown.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import archive as ar, buttondown, model, render, state as st, verify
from .candidates import DatasetReader, build_candidates, select_candidates


def scheduled_publish_date(now: datetime, send_at: str) -> str | None:
    """ISO UTC time for a Buttondown scheduled send at ``send_at`` (HH:MM US
    Eastern) on ``now``'s Eastern date, or None to send at once ('now', or
    that time has already passed)."""
    if not send_at or send_at == "now":
        return None
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    h, m = (int(x) for x in send_at.split(":"))
    local = now.astimezone(et)
    target = local.replace(hour=h, minute=m, second=0, microsecond=0)
    if local >= target - timedelta(minutes=2):
        return None
    return target.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def add_arguments(p: argparse.ArgumentParser) -> None:
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="write digest.md + picks.json; no send, no state update")
    mode.add_argument("--send", action="store_true", help="send through Buttondown, then save the state")
    p.add_argument("--out", type=Path, default=Path("work/digest"), help="output directory (default work/digest)")
    p.add_argument("--max-usd", type=float, default=0.35, help="spend cap for this run's model calls (default 0.35)")
    p.add_argument("--revision", default=None, help="dataset commit to read (default: the latest)")
    p.add_argument("--shortlist", type=int, default=20,
                   help="candidates sent to the ranking call; more than this are screened first (default 20)")
    p.add_argument("--max-age-days", type=int, default=14,
                   help="leave out notices posted more than N days before the digest date (default 14)")
    p.add_argument("--initial-lookback-hours", type=float, default=36,
                   help="with no saved state, cover notices first seen in the last N hours (default 36)")
    p.add_argument("--zero-picks", choices=["send", "skip"], default="send",
                   help="when nothing is picked: send the one-line 'nothing WOAH-worthy' email (default) or skip")
    p.add_argument("--send-at", default="07:00",
                   help="deliver at this US Eastern time (HH:MM) via a Buttondown scheduled send when the run "
                        "finishes earlier that day; later runs send at once. 'now' always sends at once (default 07:00)")
    p.add_argument("--buttondown-status", choices=["about_to_send", "draft"], default="about_to_send",
                   help="about_to_send sends now (default); draft only saves a Buttondown draft (state unchanged)")
    p.add_argument("--state-file", type=Path, default=None,
                   help="keep the state in this local JSON file instead of the dataset's analysis/digest_state.json")
    p.add_argument("--archive-dir", type=Path, default=None,
                   help="keep the sent-digest archive in this local directory instead of the dataset's "
                        f"{ar.ARCHIVE_DIR}/ (default with --state-file: digest_archive/ next to it)")
    g = p.add_argument_group("backtests (replace the state window; not allowed with --send)")
    g.add_argument("--first-seen-from", default=None, help="first_seen_at after this ISO time")
    g.add_argument("--first-seen-to", default=None, help="first_seen_at at or before this ISO time")
    g.add_argument("--posted-from", default=None, help="posted on or after YYYY-MM-DD")
    g.add_argument("--posted-to", default=None, help="posted on or before YYYY-MM-DD")
    g.add_argument("--as-of", default=None, help="the digest's date/time (ISO; default now): subject date and age cutoff")


def _iso(s: str | None) -> datetime | None:
    if not s:
        return None
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _label(d: datetime | None) -> str:
    if d is None:
        return "?"
    e = d.astimezone(render.ET)
    return f"{e:%b} {e.day} {e:%H:%M} ET"


def recover_from_buttondown(state: dict, store, recent_fn) -> dict:
    """If Buttondown has a digest whose watermark is newer than the saved
    state (the email went out but saving the state failed), adopt it."""
    wm = st.watermark(state)
    best = None
    for e in recent_fn():
        to = e.get("metadata", {}).get("to_watermark")
        if to and (wm is None or datetime.fromisoformat(to) > wm):
            if best is None or datetime.fromisoformat(to) > datetime.fromisoformat(best["metadata"]["to_watermark"]):
                best = e
    if best is None:
        return state
    to = datetime.fromisoformat(best["metadata"]["to_watermark"])
    sent = _iso((best.get("creation_date") or "").replace("Z", "+00:00")) or datetime.now(timezone.utc)
    print(f"RECOVERY: Buttondown has digest {best['id']} ({best.get('subject')!r}) up to {to.isoformat()}, newer "
          f"than the saved state; adopting it so it is not sent again", file=sys.stderr)
    new = st.after_send(state, new_watermark=to, sent_at=sent,
                        date_et=sent.astimezone(render.ET).date().isoformat(),
                        run={"recovered_from_buttondown": best["id"], "subject": best.get("subject"),
                             "at": datetime.now(timezone.utc).isoformat()})
    store.save(new, f"digest state: recovered watermark {to.isoformat()} from Buttondown")
    return new


def run(a, *, store=None, archive=None, reader_factory=DatasetReader, revision_fn=None, call_fn=None,
        send_fn=buttondown.send_email, recent_fn=buttondown.recent_digests, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    manual = any([a.first_seen_from, a.first_seen_to, a.posted_from, a.posted_to])
    if a.send and (manual or a.as_of):
        raise SystemExit("--send cannot be combined with the backtest window options")
    as_of = _iso(a.as_of) or now
    today_et = as_of.astimezone(render.ET).date().isoformat()
    store = store or (st.LocalState(a.state_file) if a.state_file else st.HFState())
    if archive is None:
        adir = a.archive_dir or (a.state_file.parent / "digest_archive" if a.state_file else None)
        archive = ar.LocalArchive(adir) if adir else ar.HFArchive()
    a.out.mkdir(parents=True, exist_ok=True)
    report: dict = {"experimental": True, "digest_version": model.VERSION, "mode": "send" if a.send else "dry_run",
                    "run_at": now.isoformat(), "as_of": as_of.isoformat()}
    try:
        return _run(a, report, store, archive, reader_factory, revision_fn, call_fn, send_fn, recent_fn, now,
                    as_of, today_et, manual)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()[-3000:]
        raise
    finally:
        (a.out / "picks.json").write_text(json.dumps(report, indent=1, default=str, ensure_ascii=False))


def _run(a, report, store, archive, reader_factory, revision_fn, call_fn, send_fn, recent_fn, now, as_of,
         today_et, manual) -> int:
    if manual:
        state = st.empty_state()
        after, until = _iso(a.first_seen_from), _iso(a.first_seen_to)
        report["window"] = {"kind": "manual"}
    else:
        state = store.load()
        if a.send:
            state = recover_from_buttondown(state, store, recent_fn)
        wm = st.watermark(state)
        after = wm or (as_of - timedelta(hours=a.initial_lookback_hours))
        until = None
        report["window"] = {"kind": "state" if wm else "initial_lookback"}
    report["window"].update({"after": after.isoformat() if after else None,
                             "until": until.isoformat() if until else None})
    if revision_fn is None:
        from ..analysis.data import dataset_revision
        revision_fn = lambda: dataset_revision()["sha"]  # noqa: E731
    rev = a.revision or revision_fn()
    report["dataset_revision"] = rev
    reader = reader_factory(rev)
    notices = reader.notices(after)
    rows, info = select_candidates(notices, after=after, until=until, as_of=as_of, max_age_days=a.max_age_days,
                                   posted_from=a.posted_from, posted_to=a.posted_to)
    new_wm = info["max_first_seen_at"]
    report["window"].update({"notices_first_seen_in_window": info["in_window"],
                             "max_first_seen_at": new_wm.isoformat() if new_wm else None})
    report["candidates"] = len(rows)
    report["excluded"] = info["excluded"]
    print(json.dumps({"dataset_revision": rev, "window": report["window"], "candidates": len(rows),
                      "excluded": info["excluded"]}, default=str), file=sys.stderr)

    # One email a day, at most (2026-10-22): a later run the same Eastern day
    # (GitHub's late cron, the 22:30 UTC fallback, a rerun) sends nothing, even
    # with new notices. The state is not touched, so those notices stay after
    # the watermark and go in the next day's digest.
    if a.send and state.get("last_sent_date_et") == today_et:
        report["skipped"] = ("a digest was already sent today; "
                             + (f"{len(rows)} new notice(s) wait for tomorrow's" if rows else "no new notices"))
        (a.out / "digest.md").write_text(f"(skipped: {report['skipped']})\n")
        print(report["skipped"], file=sys.stderr)
        return 0

    docs, links = reader.documents(rows)
    ext = reader.extractions([r["notice_id"] for r in rows])
    cands = build_candidates(rows, docs, links, ext)
    report["with_extraction_row"] = sum(1 for c in cands if c.extraction)
    report["with_document_text"] = sum(1 for c in cands if any(p.text.strip() and p.resource_id != "sam_description"
                                                               for p in c.doc.pages))
    budget = {"max_usd": a.max_usd, "spent": 0.0, "calls": []}
    report["budget"] = budget
    short = cands
    scores = model.screen(cands, budget, call_fn) if cands else {}
    if len(cands) > a.shortlist:
        short = model.shortlist(cands, scores, a.shortlist)
    report["screen"] = [{"notice_id": c.notice_id, "title": c.meta.get("title"), **scores[c.notice_id]}
                        for c in sorted(cands, key=lambda c: -scores[c.notice_id]["score"])]
    raw, sent = ({"picks": []}, []) if not short else model.rank(short, budget, call_fn)
    report["ranked"] = [c.notice_id for c in sent]
    report["model_answer"] = raw
    by_id = {c.notice_id: c for c in sent}
    picks, dropped = verify.verify_picks(raw, by_id)
    report["picks"] = picks
    report["dropped_by_checks"] = dropped
    window = {"from_label": _label(after) if after else "the start", "to_label": _label(new_wm or until or as_of),
              "left_out_old": sum(v for k, v in info["excluded"].items() if k.startswith("posted_more_than")),
              "max_age_days": a.max_age_days}
    subject = render.subject(picks, as_of)
    body = render.body(picks, by_id, len(cands), window, all_cands=cands,
                       gists={k: v.get("gist") for k, v in scores.items()})
    report["subject"], report["body"] = subject, body
    report["spent_usd"] = round(budget["spent"], 4)
    (a.out / "digest.md").write_text(f"# {subject}\n\n{body}")
    entry = ar.make_entry(subject=subject, body=body, date_et=today_et, sent_at=None, email_id=None, email_url=None,
                          dataset_revision=rev, digest_version=model.VERSION, notices_reviewed=len(cands),
                          window=window, picks=[render.pick_view(p, by_id[p["notice_id"]]) for p in picks],
                          notices=render.notice_rows(cands, picks, {k: v.get("gist") for k, v in scores.items()}))
    # written on every run for previews; it reaches the public archive only
    # after a successful send (below)
    (a.out / "archive_entry.json").write_text(json.dumps(entry, indent=1, ensure_ascii=False))
    print(json.dumps({"subject": subject, "picks": len(picks), "dropped": len(dropped),
                      "spent_usd": report["spent_usd"]}), file=sys.stderr)

    if not a.send:
        return 0
    if not picks and a.zero_picks == "skip":
        report["skipped"] = "no picks and --zero-picks skip"
        return 0
    meta = {"from_watermark": st.watermark(state).isoformat() if st.watermark(state) else None,
            "to_watermark": new_wm.isoformat() if new_wm else None, "dataset_revision": rev,
            "notices_reviewed": len(cands), "picks": [p["notice_id"] for p in picks],
            "digest_version": model.VERSION}
    sched = scheduled_publish_date(now, a.send_at)
    kw = {"publish_date": sched} if sched else {}
    if sched:
        print(json.dumps({"scheduled_for": sched}), file=sys.stderr)
    result = send_fn(subject, body, metadata=meta, status=a.buttondown_status, **kw)
    report["buttondown"] = result
    print(json.dumps({"buttondown": result}), file=sys.stderr)
    if a.buttondown_status != "about_to_send":
        return 0
    new_state = st.after_send(state, new_watermark=new_wm, sent_at=now, date_et=today_et,
                              run={"at": now.isoformat(), "dataset_revision": rev, "subject": subject,
                                   "email_id": result.get("id"), "notices_reviewed": len(cands),
                                   "picks": [p["notice_id"] for p in picks],
                                   "to_watermark": meta["to_watermark"], "spent_usd": report["spent_usd"]})
    report["state_commit"] = store.save(new_state, f"digest state: sent {subject[:80]}")
    # The public archive entry, after the state (2026-10-21): if this write
    # fails, the state is already saved, so a rerun does not send again; the
    # run fails loudly and archive_entry.json (in the workflow's artifact) can
    # be uploaded with `digest-archive-add`.
    entry.update(sent_at=now.astimezone(timezone.utc).isoformat(), buttondown_email_id=result.get("id"),
                 buttondown_url=result.get("absolute_url"))
    (a.out / "archive_entry.json").write_text(json.dumps(entry, indent=1, ensure_ascii=False))
    try:
        report["archive_file"] = archive.save(entry) or "already archived"
    except Exception as exc:
        report["archive_error"] = f"{type(exc).__name__}: {exc}"
        raise
    print(json.dumps({"archive": report["archive_file"]}), file=sys.stderr)
    report["sent"] = True     # email, state and archive all saved: the workflow deploys the site only then
    return 0
