"""The daily digest (federal_ja.digest) on synthetic data. No network: the
dataset reader, the model and Buttondown are all fakes."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from federal_ja.analysis import cli
from federal_ja.analysis.text import Document, Page
from federal_ja.digest import buttondown, model, render, run as digest_run, state as st, verify
from federal_ja.digest.candidates import (Candidate, build_candidates, select_candidates, value_label,
                                         vendor_label)

UTC = timezone.utc
T = lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC)  # noqa: E731

JA_TEXT = ("JUSTIFICATION FOR OTHER THAN FULL AND OPEN COMPETITION. The Government lost all of its "
           "contracting staff to the \u201cDeferred Resig-\nnation Program\u201d and cannot compete this requirement "
           "before the current contract expires. The estimated value of this action is $28,950,000,000 "
           "including all options. Optum Public Sector Solutions, Inc. is the only source.")
PAGE2 = ("Market research found no other source. The agency has extended this contract four times since "
         "2024 because of manpower reductions in the program office.")


def notice(nid, first_seen, posted, *, reason="justification_type", typ="Justification", title="J&A for services"):
    return {"notice_id": nid, "first_seen_at": first_seen, "posted_date": posted, "collection_reason": reason,
            "type": typ, "title": title, "agency": "VETERANS AFFAIRS, DEPARTMENT OF",
            "sub_tier": "VETERANS AFFAIRS, DEPARTMENT OF", "office": "SAC FREDERICK", "award_number": "36C10X26C0001",
            "solicitation_number": None, "sam_link": f"https://sam.gov/opp/{nid}/view", "description": None}


def candidate(nid="n1", ext=None, desc=None):
    meta = notice(nid, "2026-10-21T11:00:00+00:00", "2026-10-20 10:00:00")
    meta["description"] = desc
    doc = Document(nid, dict(meta), [Page("r1", "JA_Redacted.pdf", 1, "native", JA_TEXT),
                                     Page("r1", "JA_Redacted.pdf", 2, "transcription", PAGE2)])
    links = {nid: [{"filename": "JA_Redacted.pdf", "resource_id": "r1",
                    "url": "https://huggingface.co/datasets/x/resolve/abc/files/n1/r1_JA%20Redacted.pdf"}]}
    return build_candidates([meta], {nid: doc}, links, {nid: ext} if ext else {})[0]


# ── candidate selection ────────────────────────────────────────────────────

def test_select_by_first_seen_watermark_not_posted_date():
    ns = [notice("old", "2026-10-20T11:00:00+00:00", "2026-10-19 09:00:00"),
          notice("new", "2026-10-21T11:00:00+00:00", "2026-10-13 09:00:00"),   # posted 8 days ago, first seen today
          notice("edge", "2026-10-20T11:42:11+00:00", "2026-10-20 09:00:00")]  # == watermark: already covered
    rows, info = select_candidates(ns, after=T("2026-10-20T11:42:11"), as_of=T("2026-10-21T20:00:00"))
    assert [r["notice_id"] for r in rows] == ["new"]
    assert info["in_window"] == 1 and info["max_first_seen_at"] == T("2026-10-21T11:00:00")


def test_select_leaves_out_old_postings_and_non_intent_special_notices_and_counts_them():
    ns = [notice("a", "2026-10-21T11:00:00+00:00", "2026-09-01 09:00:00"),
          notice("b", "2026-10-21T11:00:00+00:00", "2026-10-20 09:00:00", reason="special_notice_title_match",
                 typ="Special Notice", title="ACC-APG Division D Competitive/Fair Opportunity Schedule"),
          notice("c", "2026-10-21T11:00:00+00:00", "2026-10-20 09:00:00", reason="special_notice_title_match",
                 typ="Special Notice", title="Notice of Intent to Sole Source"),
          notice("d", "2026-10-21T11:00:00+00:00", "2026-10-20 08:00:00")]
    rows, info = select_candidates(ns, after=None, as_of=T("2026-10-21T20:00:00"))
    assert [r["notice_id"] for r in rows] == ["c", "d"]      # newest posting first
    assert info["excluded"] == {"posted_more_than_14_days_before": 1, "special_notice_not_intent": 1}
    assert info["max_first_seen_at"] == T("2026-10-21T11:00:00")   # the left-out ones still move the watermark


def test_description_becomes_a_verifiable_page():
    c = candidate(desc="<p>This is a Notice of Intent to award a sole-source contract to Acme&nbsp;Corp.</p>")
    assert c.doc.pages[-1].filename == "SAM.gov notice description"
    raw = {"picks": [{"notice_id": "n1", "headline": "A sole-source notice", "why": "It says so.",
                      "quotes": [{"text": "This is a Notice of Intent to award a sole-source contract", "page": ""}],
                      "vendor": "", "value": "", "authority": ""}]}
    picks, _ = verify.verify_picks(raw, {"n1": c})
    assert picks[0]["quotes"][0]["where"] == "SAM.gov notice description"


# ── verification ───────────────────────────────────────────────────────────

def pick(**kw):
    p = {"notice_id": "n1", "headline": "VA loses all its contracting staff to deferred resignation",
         "why": "The agency says it lost all of its contracting staff. The action is worth $29 billion.",
         "quotes": [{"text": "The Government lost all of its contracting staff to the \"Deferred Resignation Program\"",
                     "page": "JA_Redacted.pdf, page 1"}],
         "vendor": "Optum Public Sector Solutions, Inc.", "value": "$28,950,000,000", "authority": ""}
    p.update(kw)
    return p


def test_quote_checks_tolerate_formatting_and_drop_invented_quotes():
    c = candidate()
    q2 = [{"text": "The Government  lost all of its contracting\nstaff to the \"Deferred Resignation Program\"",
           "page": ""},
          {"text": "The agency fired everyone and hid the paperwork from Congress", "page": ""}]
    picks, dropped = verify.verify_picks({"picks": [pick(quotes=q2, value="",
                                                         why="The agency lost all of its contracting staff.")]},
                                         {"n1": c})
    assert len(picks) == 1 and len(picks[0]["quotes"]) == 1
    assert picks[0]["quotes"][0]["where"] == "JA_Redacted.pdf, p. 1"
    # curly quotes and doubled spaces normalised; "Resig-/nation" rejoined across the line break
    assert picks[0]["quotes"][0]["match"] == "dehyphenated"
    assert any(d["what"] == "quote" and "fired everyone" in d["value"] for d in dropped)


def test_pick_with_no_verified_quote_is_dropped():
    c = candidate()
    picks, dropped = verify.verify_picks({"picks": [pick(quotes=[{"text": "Words that are not in the document at all", "page": ""}])]},
                                         {"n1": c})
    assert picks == [] and dropped[-1]["reason"] == "no quote verified"


def test_dollar_figures_must_come_from_quotes_or_extraction():
    c = candidate()
    value_q = {"text": "The estimated value of this action is $28,950,000,000 including all options", "page": ""}
    staff_q = pick()["quotes"][0]
    # rounding of a quoted amount passes
    picks, _ = verify.verify_picks({"picks": [pick(quotes=[staff_q, value_q])]}, {"n1": c})
    assert picks and "$29 billion" in picks[0]["why"]
    # the figure is unquoted but in the document: the code quotes its source sentence
    picks, dropped = verify.verify_picks({"picks": [pick(quotes=[staff_q])]}, {"n1": c})
    assert "$29 billion" in picks[0]["why"]
    added = picks[0]["quotes"][-1]
    assert added["added_by_code"] and "$28,950,000,000" in added["text"]
    # a figure that is nowhere in the document: the sentence goes
    picks, dropped = verify.verify_picks({"picks": [pick(quotes=[staff_q], value="", why="The agency lost all of its "
                                                         "contracting staff. The action is worth $31 billion.")]},
                                         {"n1": c})
    assert picks and "$31 billion" not in picks[0]["why"] and len(picks[0]["quotes"]) == 1
    assert any(d["what"] == "why_sentence" and "unsupported dollar figure" in d["reason"] for d in dropped)
    # truncation passes like rounding; a figure off at the precision written does not
    assert verify.amount_supported(1.2e9, 0.1e9, [1_229_179_038.02])
    assert verify.amount_supported(170e6, 1e6, [170_829_743.0])
    assert not verify.amount_supported(1.3e9, 0.1e9, [1_229_179_038.02])
    assert not verify.amount_supported(28.8e9, 0.1e9, [28_950_000_000.0])
    # an extraction value backs it
    picks, _ = verify.verify_picks({"picks": [pick(quotes=[staff_q])]},
                                   {"n1": candidate(ext={"estimated_value_usd": 28_950_000_000.0,
                                                         "value_basis": "total_including_options"})})
    assert "$29 billion" in picks[0]["why"] and picks[0]["value"] == "$28,950,000,000 (total incl. options)"
    # arithmetic (a total the document never states) fails, in the headline it drops the pick
    picks, dropped = verify.verify_picks({"picks": [pick(quotes=[staff_q, value_q], headline="A $57.9 billion deal")]},
                                         {"n1": c})
    assert picks == [] and "unsupported dollar figure" in dropped[-1]["reason"]


def test_numbers_must_appear_in_the_notice():
    c = candidate()
    picks, dropped = verify.verify_picks({"picks": [pick(why="It was extended four times since 2024. "
                                                          "It was extended 7 times.")]}, {"n1": c})
    assert picks[0]["why"] == "It was extended four times since 2024."
    assert any("'7'" in d["reason"] for d in dropped)


def test_vendor_and_value_from_the_model_need_the_text():
    c = candidate()
    picks, dropped = verify.verify_picks({"picks": [pick(vendor="Booz Allen Hamilton", value="$5,000,000",
                                                         why="The agency lost all of its contracting staff.")]},
                                         {"n1": c})
    assert picks[0]["vendor"] is None and picks[0]["value"] is None
    assert {d["what"] for d in dropped} >= {"vendor", "value"}


def test_at_most_five_picks_and_unknown_ids_dropped():
    cands = {f"n{i}": candidate(f"n{i}") for i in range(7)}
    raw = {"picks": [pick(notice_id=f"n{i}", why="The agency lost all of its contracting staff.") for i in range(7)]
           + [pick(notice_id="zzz")]}
    picks, dropped = verify.verify_picks(raw, cands)
    assert len(picks) == 5 and [p["rank"] for p in picks] == [1, 2, 3, 4, 5]
    assert sum(1 for d in dropped if d["what"] == "pick") == 3


# ── rendering ──────────────────────────────────────────────────────────────

def test_render_pick():
    c = candidate(ext={"estimated_value_usd": 28_950_000_000.0, "value_basis": "total_including_options",
                       "authority_cited": "FAR 6.302-2"})
    picks, _ = verify.verify_picks({"picks": [pick()]}, {"n1": c})
    when = T("2026-10-22T14:00:00")
    s = render.subject(picks, when)
    assert s == "WOAH! J&As — Oct 22: VA loses all its contracting staff to deferred resignation"
    b = render.body(picks, {"n1": c}, 12, {"from_label": "Oct 21", "to_label": "Oct 22", "left_out_old": 3,
                                          "max_age_days": 14})
    assert "## 1. VA loses all its contracting staff" in b
    assert "VETERANS AFFAIRS, DEPARTMENT OF · Optum Public Sector Solutions, Inc. · $28,950,000,000 " \
           "(total incl. options) · FAR 6.302-2" in b
    assert "> “The Government lost all of its contracting staff" in b and "> — JA_Redacted.pdf, p. 1" in b
    assert "[SAM.gov notice](https://sam.gov/opp/n1/view)" in b and "JA%20Redacted.pdf" in b
    assert "12 new notices reviewed" in b and "3 older notices skipped" in b
    assert "Chosen and written up by Claude" in b and not b.startswith("---")


def test_render_zero_picks():
    when = T("2026-10-22T03:00:00")   # 23:00 on Oct 21 in Eastern time
    assert render.subject([], when) == "WOAH! J&As — Oct 21: nothing WOAH-worthy"
    b = render.body([], {}, 7, {"from_label": "a", "to_label": "b"})
    assert b.startswith("Nothing WOAH-worthy today (7 new notices reviewed).")


def test_transcribed_page_is_labelled():
    c = candidate()
    q = {"text": "The agency has extended this contract four times since 2024 because of manpower reductions",
         "page": ""}
    picks, _ = verify.verify_picks({"picks": [pick(quotes=[q], why="It cites manpower reductions.")]}, {"n1": c})
    assert picks[0]["quotes"][0]["where"] == "JA_Redacted.pdf, p. 2 (machine-transcribed page)"


# ── model stage plumbing ───────────────────────────────────────────────────

def test_excerpt_puts_the_justification_first_and_marks_pages():
    nid = "n9"
    meta = notice(nid, "2026-10-21T11:00:00+00:00", "2026-10-20 10:00:00")
    doc = Document(nid, dict(meta), [Page("r0", "Statement_of_Work.pdf", 1, "native", "SOW text " * 50),
                                     Page("r1", "Sole_Source_Justification.pdf", 1, "native", JA_TEXT)])
    c = build_candidates([meta], {nid: doc}, {}, {})[0]
    text, used, total = model.excerpt(c, 800)
    assert text.startswith("[Sole_Source_Justification.pdf, page 1]")
    assert "[document text cut:" in text and used < total


def test_cap_stops_the_ranking_call():
    c = candidate()
    with pytest.raises(model.CapError):
        model._call("rank", [model.card(c, 5000)], model.RANK_SCHEMA, "record_picks",
                    {"max_usd": 0.001, "spent": 0.0, "calls": []}, lambda *a, **k: pytest.fail("called"))


# ── the run: state only after a successful send ────────────────────────────

class FakeReader:
    NOTICES = [notice("n1", "2026-10-21T11:00:00+00:00", "2026-10-20 10:00:00")]

    def __init__(self, rev):
        self.rev = rev

    def notices(self, since):
        return list(self.NOTICES)

    def documents(self, rows):
        c = candidate()
        return {"n1": Document("n1", dict(rows[0]), [p for p in c.doc.pages if p.resource_id == "r1"])}, \
               {"n1": c.links}

    def extractions(self, ids):
        return {}


def fake_model(picks):
    def call(prompt, *, schema, tool_name, model, max_tokens, effort, timeout):
        return {"picks": picks}, {"cost_usd": 0.01, "input_tokens": 100, "output_tokens": 50}
    return call


def args(tmp_path, *extra):
    return cli.build_parser().parse_args(["digest", *extra, "--out", str(tmp_path / "out"),
                                          "--state-file", str(tmp_path / "state.json")])


NOW = T("2026-10-21T15:00:00")


def run(tmp_path, a, *, send_fn, picks=None, recent=()):
    return digest_run.run(a, reader_factory=FakeReader, revision_fn=lambda: "rev1",
                          call_fn=fake_model([pick()] if picks is None else picks),
                          send_fn=send_fn, recent_fn=lambda: list(recent), now=NOW)


def test_dry_run_writes_files_and_never_sends_or_saves(tmp_path):
    def boom(*a, **k):
        raise AssertionError("sent")
    assert run(tmp_path, args(tmp_path, "--dry-run"), send_fn=boom) == 0
    assert not (tmp_path / "state.json").exists()
    rep = json.loads((tmp_path / "out" / "picks.json").read_text())
    assert rep["mode"] == "dry_run" and rep["candidates"] == 1 and len(rep["picks"]) == 1
    assert (tmp_path / "out" / "digest.md").read_text().startswith("# WOAH! J&As — Oct 21: VA loses")


def test_failed_send_leaves_state_unchanged(tmp_path):
    def fail(*a, **k):
        raise buttondown.SendError("Buttondown refused the email: HTTP 400")
    with pytest.raises(buttondown.SendError):
        run(tmp_path, args(tmp_path, "--send"), send_fn=fail)
    assert not (tmp_path / "state.json").exists()
    assert "SendError" in json.loads((tmp_path / "out" / "picks.json").read_text())["error"]


def test_successful_send_saves_watermark_and_rerun_sends_nothing_new(tmp_path):
    sent = []

    def ok(subject, body, *, metadata, status):
        sent.append((subject, metadata, status))
        return {"id": "em_1", "status": "about_to_send"}
    assert run(tmp_path, args(tmp_path, "--send"), send_fn=ok) == 0
    s = json.loads((tmp_path / "state.json").read_text())
    assert s["watermark_first_seen_at"] == "2026-10-21T11:00:00+00:00" and s["last_sent_date_et"] == "2026-10-21"
    assert sent[0][1]["to_watermark"] == "2026-10-21T11:00:00+00:00" and sent[0][2] == "about_to_send"
    # same day, nothing new: skipped, nothing sent
    assert run(tmp_path, args(tmp_path, "--send"), send_fn=ok) == 0
    assert len(sent) == 1


def test_never_a_second_email_the_same_day_even_with_new_notices(tmp_path, monkeypatch):
    sent = []

    def ok(subject, body, *, metadata, status):
        sent.append(subject)
        return {"id": f"em_{len(sent)}", "status": "about_to_send"}
    assert run(tmp_path, args(tmp_path, "--send"), send_fn=ok) == 0
    before = json.loads((tmp_path / "state.json").read_text())
    # a notice first seen after the morning email, the same day
    monkeypatch.setattr(FakeReader, "NOTICES", FakeReader.NOTICES + [
        notice("n2", "2026-10-21T13:00:00+00:00", "2026-10-21 09:00:00")])
    assert run(tmp_path, args(tmp_path, "--send"), send_fn=ok) == 0
    assert len(sent) == 1
    rep = json.loads((tmp_path / "out" / "picks.json").read_text())
    assert rep["skipped"].startswith("a digest was already sent today; 1 new notice")
    # state untouched, so n2 is still after the watermark for tomorrow
    assert json.loads((tmp_path / "state.json").read_text()) == before
    assert before["watermark_first_seen_at"] < "2026-10-21T13:00:00+00:00"


def test_zero_picks_email_and_skip_option(tmp_path):
    sent = []

    def ok(subject, body, *, metadata, status):
        sent.append((subject, body))
        return {"id": "em_2", "status": "about_to_send"}
    run(tmp_path, args(tmp_path, "--send", "--zero-picks", "skip"), send_fn=ok, picks=[])
    assert sent == [] and not (tmp_path / "state.json").exists()
    run(tmp_path, args(tmp_path, "--send"), send_fn=ok, picks=[])
    assert sent[0][0] == "WOAH! J&As — Oct 21: nothing WOAH-worthy"
    assert sent[0][1].startswith("Nothing WOAH-worthy today (1 new notice reviewed).")


def _sender():
    sent = []

    def ok(subject, body, *, metadata, status):
        sent.append((subject, metadata))
        return {"id": f"em_{len(sent)}", "status": "about_to_send"}
    return sent, ok


def _seed(tmp_path, last_sent_date_et):
    """A state whose last digest went out on that Eastern date, before FakeReader's 1 notice."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    st = {"version": 1, "watermark_first_seen_at": "2026-10-20T00:00:00+00:00",
          "last_sent_at": f"{last_sent_date_et}T11:00:00+00:00", "last_sent_date_et": last_sent_date_et, "runs": []}
    (tmp_path / "state.json").write_text(json.dumps(st))
    return st


def _at(tmp_path, ok, day, *, picks, extra=()):
    """One --send run at 9 a.m. ET on ``day``."""
    return digest_run.run(args(tmp_path, "--send", *extra), reader_factory=FakeReader, revision_fn=lambda: "rev1",
                          call_fn=fake_model(picks), send_fn=ok, recent_fn=lambda: [], now=T(f"{day}T13:00:00"))


def _skipped(tmp_path):
    return json.loads((tmp_path / "out" / "picks.json").read_text()).get("skipped")


def test_quiet_days_hold_then_send_at_the_gap_limit(tmp_path):
    sent, ok = _sender()
    before = _seed(tmp_path, "2026-10-23")                    # last digest Friday
    for day in ("2026-10-24", "2026-10-25"):                  # Saturday, Sunday: 1 notice, no pick: held
        assert _at(tmp_path, ok, day, picks=[]) == 0
        assert sent == []
        assert _skipped(tmp_path) == ("quiet day: no picks and 1 notice(s), under 5; "
                                      "held for the next digest (last sent 2026-10-23)")
        assert json.loads((tmp_path / "state.json").read_text()) == before
        assert not json.loads((tmp_path / "out" / "picks.json").read_text()).get("sent")
    _at(tmp_path, ok, "2026-10-26", picks=[])                 # Monday: 3 days since the last, sends
    assert len(sent) == 1 and sent[0][0] == "WOAH! J&As — Oct 26: nothing WOAH-worthy"
    assert sent[0][1]["notices_reviewed"] == 1
    assert json.loads((tmp_path / "state.json").read_text())["last_sent_date_et"] == "2026-10-26"


def test_quiet_rule_applies_on_weekdays_too(tmp_path):
    sent, ok = _sender()
    _seed(tmp_path, "2026-10-26")                             # Monday
    _at(tmp_path, ok, "2026-10-27", picks=[])                 # Tuesday
    assert sent == [] and _skipped(tmp_path).startswith("quiet day")


def test_enough_notices_send_even_with_no_picks(tmp_path):
    sent, ok = _sender()
    _seed(tmp_path, "2026-10-23")
    _at(tmp_path, ok, "2026-10-24", picks=[], extra=("--hold-under", "1"))   # 1 notice is not under 1
    assert len(sent) == 1


def test_quiet_day_with_a_pick_sends(tmp_path):
    sent, ok = _sender()
    _seed(tmp_path, "2026-10-23")
    _at(tmp_path, ok, "2026-10-24", picks=[pick()])
    assert len(sent) == 1


def test_no_hold_without_a_previous_digest_or_when_turned_off(tmp_path):
    sent, ok = _sender()
    _at(tmp_path, ok, "2026-10-21", picks=[])                 # no state yet: sends
    assert len(sent) == 1
    sent2, ok2 = _sender()
    _seed(tmp_path / "b", "2026-10-23")
    _at(tmp_path / "b", ok2, "2026-10-24", picks=[], extra=("--hold-under", "0"))
    assert len(sent2) == 1


def test_draft_does_not_move_the_state(tmp_path):
    def ok(subject, body, *, metadata, status):
        assert status == "draft"
        return {"id": "em_3", "status": "draft"}
    run(tmp_path, args(tmp_path, "--send", "--buttondown-status", "draft"), send_fn=ok)
    assert not (tmp_path / "state.json").exists()


def test_recovers_watermark_from_buttondown_when_state_save_failed(tmp_path):
    recent = [{"id": "em_9", "subject": "WOAH! J&As — Oct 21: x", "creation_date": "2026-10-21T12:00:00Z",
               "metadata": {"kind": buttondown.METADATA_KIND, "to_watermark": "2026-10-21T11:00:00+00:00"}}]

    def boom(*a, **k):
        raise AssertionError("re-sent a digest Buttondown already has")
    assert run(tmp_path, args(tmp_path, "--send"), send_fn=boom, recent=recent) == 0
    s = json.loads((tmp_path / "state.json").read_text())
    assert s["watermark_first_seen_at"] == "2026-10-21T11:00:00+00:00"
    assert s["runs"][-1]["recovered_from_buttondown"] == "em_9"


def test_backtest_window_refuses_send(tmp_path):
    with pytest.raises(SystemExit):
        run(tmp_path, args(tmp_path, "--send", "--first-seen-from", "2026-10-20T00:00:00+00:00"),
            send_fn=lambda *a, **k: None)


# ── Buttondown request shape ───────────────────────────────────────────────

def test_buttondown_request(monkeypatch):
    seen = {}

    def post(url, json, headers, timeout):
        seen.update(url=url, json=json, headers=headers)
        return SimpleNamespace(status_code=201, json=lambda: {"id": "em_x", "status": "about_to_send"}, headers={})
    monkeypatch.setenv("BUTTONDOWN_API_KEY", "k-secret")
    monkeypatch.setattr(buttondown.requests, "post", post)
    out = buttondown.send_email("S", "B", metadata={"to_watermark": "t"})
    assert out["id"] == "em_x" and seen["url"] == "https://api.buttondown.com/v1/emails"
    assert seen["headers"]["Authorization"] == "Token k-secret"
    assert seen["headers"]["X-Buttondown-Live-Dangerously"] == "true"
    assert seen["headers"]["X-API-Version"] == "2026-04-01"
    assert seen["json"]["status"] == "about_to_send" and seen["json"]["metadata"]["kind"] == buttondown.METADATA_KIND


def test_buttondown_error_is_loud_and_hides_the_key(monkeypatch):
    def post(url, json, headers, timeout):
        return SimpleNamespace(status_code=400, headers={},
                               json=lambda: {"code": "sending_requires_confirmation", "detail": "confirm first"})
    monkeypatch.setenv("BUTTONDOWN_API_KEY", "k-secret")
    monkeypatch.setattr(buttondown.requests, "post", post)
    with pytest.raises(buttondown.SendError) as e:
        buttondown.send_email("S", "B", metadata={})
    assert "sending_requires_confirmation" in str(e.value) and "k-secret" not in str(e.value)


def test_state_after_send_keeps_the_later_watermark():
    s = st.after_send({**st.empty_state(), "watermark_first_seen_at": "2026-10-21T11:00:00+00:00"},
                      new_watermark=None, sent_at=NOW, date_et="2026-10-21", run={})
    assert s["watermark_first_seen_at"] == "2026-10-21T11:00:00+00:00"


# ── the "All new notices" list ─────────────────────────────────────────────

def test_all_notices_list_every_candidate_with_checked_fields_only():
    big = candidate("big", ext={"estimated_value_usd": 5_000_000.0, "vendor": "Acme Corp",
                                "value_basis": "total_including_options", "authority_cited": "FAR 6.302-1"})
    small = candidate("small", ext={"estimated_value_usd": 10_000.0, "value_basis": "annual_or_per_period"})
    red = candidate("red", ext={"value_redacted": True, "redaction_evidence": "attachment filename says redacted"})
    none = candidate("none")                      # no extraction row: nothing guessed
    star = candidate("star")
    for c, sub, title in ((big, "DEPT OF THE ARMY", "Big buy"), (small, "VETERANS AFFAIRS, DEPARTMENT OF", "Small"),
                          (red, "U.S. CUSTOMS AND BORDER PROTECTION", "Redacted one"), (none, "GENERAL SERVICES ADMINISTRATION", "x" * 200),
                          (star, "DEPT OF THE NAVY", "The pick")):
        c.meta.update(sub_tier=sub, title=title)
    out = render.all_notices([none, small, red, big, star], [{"notice_id": "star", "rank": 1}])
    lines = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert out.startswith("## All new notices (5)")
    assert len(lines) == 5
    assert lines[0].startswith("- ★ Navy · The pick · [SAM.gov](https://sam.gov/opp/star/view)")
    assert lines[1] == ("- Army · Big buy · Acme Corp · $5,000,000 (total incl. options) · FAR 6.302-1 · "
                        "[SAM.gov](https://sam.gov/opp/big/view)")
    assert lines[2].startswith("- Veterans Affairs · Small · $10,000 (per year/period) ·")
    # unknown values after known ones, then by agency
    assert lines[3].startswith("- General Services Administration · xxx") and "…" in lines[3]
    assert "$" not in lines[3] and "Optum" not in lines[3]
    assert lines[4].startswith("- U.S. Customs and Border Protection · Redacted one · value redacted")


def test_short_org_keeps_acronyms():
    assert render.short_org({"sub_tier": "DEFENSE CONTRACT MANAGEMENT AGENCY (DCMA)"}) == \
        "Defense Contract Management Agency (DCMA)"
    assert render.short_org({"agency": "DEPT OF DEFENSE"}) == "Defense"
    assert render.short_org({"sub_tier": "DEPT OF THE AIR FORCE"}) == "Air Force"
    assert render.short_org({"sub_tier": "US IMMIGRATION AND CUSTOMS ENFORCEMENT"}) == "US Immigration and Customs Enforcement"


def test_zero_picks_email_still_lists_all_notices():
    c = candidate()
    b = render.body([], {}, 1, {"from_label": "a", "to_label": "b"}, all_cands=[c])
    assert b.startswith("Nothing WOAH-worthy today (1 new notice reviewed).")
    assert "## All new notices (1)" in b and "[SAM.gov](https://sam.gov/opp/n1/view)" in b
    assert b.index("All new notices") < b.index("*WOAH! J&As:*")


def test_excerpt_shares_the_budget_between_versions_of_the_justification():
    nid = "n8"
    meta = notice(nid, "2026-10-21T11:00:00+00:00", "2026-10-20 10:00:00")
    v1 = [Page("r1", "Other_Than_Full_and_Open_Competition.pdf", i, "native", f"OLD{i} " + "a " * 1500) for i in (1, 2)]
    v2 = [Page("r2", "Other Than Full and Open Competition_10.19.26.pdf", i, "native", f"NEW{i} " + "b " * 1500)
          for i in (1, 2)]
    doc = Document(nid, dict(meta), [Page("r0", "PWS.pdf", 1, "native", "pws " * 100)] + v1 + v2)
    c = build_candidates([meta], {nid: doc}, {}, {})[0]
    text, used, total = model.excerpt(c, 7000)
    assert "OLD1" in text and "NEW1" in text and used <= 10500
    assert text.index("OLD1") < text.index("NEW1")
    # with room left over, the other attachments follow
    text, _, _ = model.excerpt(c, 20000)
    assert text.index("NEW2") < text.index("[PWS.pdf")


def test_all_notices_shows_gist_under_each_line():
    a, b = candidate("a"), candidate("b")
    a.meta.update(sub_tier="DEPT OF THE ARMY", title="Bridge for pharmacy services")
    b.meta.update(sub_tier="DEPT OF THE NAVY", title="Spare parts")
    out = render.all_notices([a, b], [], gists={"a": "Bridge while a protest of the follow-on is resolved.", "b": ""})
    la = next(l for l in out.splitlines() if "Bridge for pharmacy" in l)
    i = out.splitlines().index(la)
    assert out.splitlines()[i + 1].strip() == "Bridge while a protest of the follow-on is resolved."
    lb = out.splitlines().index(next(l for l in out.splitlines() if "Spare parts" in l))
    assert not out.splitlines()[lb + 1].strip().startswith("Bridge")
    assert "not quote-checked" in out


def test_scheduled_publish_date_7am_eastern():
    from datetime import datetime, timezone
    from federal_ja.digest.run import scheduled_publish_date
    early = datetime(2026, 10, 22, 7, 0, tzinfo=timezone.utc)      # 03:00 EDT
    assert scheduled_publish_date(early, "07:00") == "2026-10-22T11:00:00Z"
    late = datetime(2026, 10, 22, 22, 30, tzinfo=timezone.utc)     # 18:30 EDT: send at once
    assert scheduled_publish_date(late, "07:00") is None
    assert scheduled_publish_date(early, "now") is None
    winter = datetime(2026, 12, 2, 7, 30, tzinfo=timezone.utc)     # 02:30 EST
    assert scheduled_publish_date(winter, "07:00") == "2026-12-02T12:00:00Z"


def test_send_email_schedules_when_publish_date_given(monkeypatch):
    from federal_ja.digest import buttondown
    sent = {}

    class R:
        status_code = 201
        def json(self):
            return {"id": "em_1", "status": "scheduled"}

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.update(json)
        return R()

    monkeypatch.setattr(buttondown.requests, "post", fake_post)
    monkeypatch.setattr(buttondown, "_headers", lambda live=False: {})
    buttondown.send_email("s", "b", metadata={}, publish_date="2026-10-22T11:00:00Z")
    assert sent["status"] == "scheduled" and sent["publish_date"] == "2026-10-22T11:00:00Z"


# ── the vendor slot: every named firm, not only the first ──────────────────

def test_vendor_label_shows_every_named_firm():
    # 2026-11-10: SPE4A726R0767's DLA form lists three approved sources and
    # the email showed only the first, as if it were a sole source.
    e = {"vendor": "SKURKA AEROSPACE INC",
         "additional_vendors": "HYDRO-AIRE AEROSPACE CORP; NEW PHOENIX AEROSPACE INC",
         "vendor_quote": "95270 | SKURKA AEROSPACE INC 81982 | HYDRO-AIRE AEROSPACE CORP 3XMD9 | NEW PHOENIX AEROSPACE INC"}
    assert vendor_label(e) == ("SKURKA AEROSPACE INC; HYDRO-AIRE AEROSPACE CORP; NEW PHOENIX AEROSPACE INC "
                               "(3 firms named)")


def test_vendor_label_one_firm_is_unchanged_and_none_stays_none():
    assert vendor_label({"vendor": "Acme Corp"}) == "Acme Corp"
    assert vendor_label({"vendor": "Acme Corp", "additional_vendors": None,
                         "vendor_quote": "CAGE | COMPANY NAME 70628 | ACME CORP"}) == "Acme Corp"
    assert vendor_label({"vendor": "Acme Corp", "additional_vendors": float("nan")}) == "Acme Corp"
    assert vendor_label({}) is None and vendor_label({"additional_vendors": "B Corp"}) is None


def test_vendor_label_long_lists_are_counted_not_cut_silently():
    e = {"vendor": "A Co", "additional_vendors": "B Co; C Co; D Co; E Co"}
    assert vendor_label(e) == "A Co; B Co; C Co and 2 more (5 firms named)"
    assert vendor_label({"vendor": "A Co", "additional_vendors": ["B Co", "A Co", ""]}) == "A Co; B Co (2 firms named)"


def test_vendor_label_flags_a_cage_table_whose_other_rows_were_not_captured():
    e = {"vendor": "U D T, INC.", "vendor_quote": "57547 | U D T, INC. 82918 | THE BOEING COMPANY"}
    assert vendor_label(e) == "U D T, INC. (one of several firms listed)"
    # one firm under three CAGE codes is still one firm
    same = {"vendor": "PARKER HANNIFIN",
            "vendor_quote": "CAGE | COMPANY NAME 98441 | PARKER HANNIFIN 59211 | PARKER HANNIFIN 82271 | PARKER HANNIFIN"}
    assert vendor_label(same) == "PARKER HANNIFIN"
    # a single row followed by other text, and tables in other layouts, are not flagged
    for vendor, quote in (("CORTRON, INC.", "CAGE | COMPANY NAME 6Z833 | CORTRON, INC. The use of authority 10 U.S.C. 3204(a)(1) is based on:"),
                          ("L3 TECHNOLOGIES, INC.", "0B107 | L3 TECHNOLOGIES, INC. P/N LFG-094-A3"),
                          ("PEERLESS MFG CO", "4730013209517 | TRAP,MOISTURE | PEERLESS MFG CO | 46219 | D251-10000-1"),
                          ("Metrex Valve Corp.", "0001 | Metrex Valve Corp. | 21013 | EAOM-513-NF2-3W")):
        assert vendor_label({"vendor": vendor, "vendor_quote": quote}) == vendor


def test_all_notices_and_picks_use_the_full_vendor_label():
    ext = {"vendor": "SKURKA AEROSPACE INC", "authority_cited": "10 U.S.C. 3204(a)(1)", "value_redacted": True,
           "additional_vendors": "HYDRO-AIRE AEROSPACE CORP; NEW PHOENIX AEROSPACE INC"}
    c = candidate("multi", ext=ext)
    assert c.extraction["additional_vendors"] == ext["additional_vendors"]   # the field reaches the candidate
    row = render.notice_rows([c], [])[0]
    assert row["vendor"] == ("SKURKA AEROSPACE INC; HYDRO-AIRE AEROSPACE CORP; NEW PHOENIX AEROSPACE INC "
                             "(3 firms named)")
    line = [ln for ln in render.all_notices([c], []).splitlines() if ln.startswith("- ")][0]
    assert "HYDRO-AIRE AEROSPACE CORP" in line and "(3 firms named)" in line


# ── the value slot: always labelled; nothing from a garbled text layer ─────

def test_value_label_says_what_the_figure_is_or_shows_nothing():
    assert value_label({"estimated_value_usd": 5_000_000.0, "value_basis": "modification_or_increase_amount"}) == \
        "$5,000,000 (modification/increase)"
    assert value_label({"estimated_value_usd": 146_629.98, "value_basis": "annual_or_per_period"}) == \
        "$146,630 (per year/period)"
    # a figure with no true label is not shown: a unit price, one of two line items
    assert value_label({"estimated_value_usd": 19_166.0, "value_basis": "other"}) is None
    assert value_label({"estimated_value_usd": 19_166.0}) is None
    assert value_label({}) is None


def test_redacted_is_said_only_where_something_says_so():
    marker = "[REDACTED] marker in transcribed text; model found a redaction (quote verified); value redacted or blank"
    named = "attachment filename says redacted; value redacted or blank"
    blank = "model found a redaction (quote verified); value redacted or blank"
    assert value_label({"value_redacted": True, "redaction_evidence": marker}) == "value redacted"
    assert value_label({"value_redacted": True, "redaction_evidence": named}) == "value redacted"
    # only a blank where the figure belongs: a black box and an empty field read the same
    assert value_label({"value_redacted": True, "redaction_evidence": blank}) == "value blank or redacted"
    assert value_label({"value_redacted": True}) == "value blank or redacted"


def test_nothing_read_from_a_garbled_text_layer_is_shown():
    # 2026-11-06: six VA notices said "value redacted" and two gave this vendor;
    # the documents state the value and name AGFA Healthcare Corporation
    ext = {"vendor": "$*)$ +HDOWKFDUH &RUSRUDWLRQ", "value_redacted": True, "garbled_text_layer": True,
           "authority_cited": "41 U.S.C. 4106(c)(2)"}
    assert value_label(ext) is None and vendor_label(ext) is None
    row = render.notice_rows([candidate("g", ext=ext)], [])[0]
    assert (row["vendor"], row["value"], row["authority"]) == (None, None, None)
    line = [ln for ln in render.all_notices([candidate("g", ext=ext)], []).splitlines() if ln.startswith("- ")][0]
    assert "redacted" not in line and "HDOWKFDUH" not in line


def test_a_pick_from_a_garbled_document_or_an_unlabelled_figure_shows_no_value(monkeypatch):
    garbled = candidate(ext={"vendor": "$*)$ +HDOWKFDUH", "value_redacted": True, "garbled_text_layer": True,
                             "authority_cited": "FAR 6.302-1"})
    picks, _ = verify.verify_picks({"picks": [pick(vendor="Optum", value="$5,000,000")]}, {"n1": garbled})
    assert picks and (picks[0]["vendor"], picks[0]["value"], picks[0]["authority"]) == (None, None, None)
    other = candidate(ext={"estimated_value_usd": 19_166.0, "value_basis": "other"})
    picks, _ = verify.verify_picks({"picks": [pick(value="$19,166")]}, {"n1": other})
    assert picks and picks[0]["value"] is None        # the model's figure does not fill the slot either


def test_c1_control_characters_in_titles_become_dashes():
    c = candidate("t")
    c.meta["title"] = "J065\x96\x96PACS \x97 Denver"
    assert render.notice_rows([c], [])[0]["title"] == "J065\u2013\u2013PACS \u2014 Denver"


def test_checked_facts_come_from_the_published_columns_and_reach_the_card():
    """2026-11-11: the second call's published columns become a "Checked facts" line."""
    from federal_ja.digest.candidates import checked_facts
    row = {"gao_protest_numbers": "B-424920", "contractor_named": "KBR", "ceiling_prior_usd": 79467840.0,
           "ceiling_increase_usd": 65532160.0, "ceiling_new_usd": 145000000.0,
           "ceiling_increase_quote": "by $65,532,160.00 from $79,467,840.00 to $145,000,000.00",
           "dla_approved_sources": None, "new_source_approval_quote": float("nan")}
    f = checked_facts(row)
    assert f["GAO protests"] == "B-424920" and f["current or previous contractor named"] == "KBR"
    assert f["ceiling change"].startswith("$79,467,840 -> $145,000,000 (+$65,532,160)")
    assert "approved sources (DLA form)" not in f and "how a new source can be approved" not in f
    from federal_ja.digest import model
    line = model._ext_line({"vendor": "KBR", "checked_facts": f})
    assert "Checked facts (each read from this document and checked against it): GAO protests: B-424920" in line
    # a row with none of the columns (before 2026-11-11, or nothing found) adds nothing
    assert checked_facts({"ceiling_prior_usd": float("nan")}) == {}
    assert "CHECKED FACTS" in model.CONFIG["rank"]["prompt"] and model.VERSION.endswith("digest3")
