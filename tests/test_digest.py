"""The daily digest (federal_ja.digest) on synthetic data. No network: the
dataset reader, the model and Buttondown are all fakes."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from federal_ja.analysis import cli
from federal_ja.analysis.text import Document, Page
from federal_ja.digest import buttondown, model, render, run as digest_run, state as st, verify
from federal_ja.digest.candidates import Candidate, build_candidates, select_candidates

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
    assert s == "Whoa J&As — Oct 22: VA loses all its contracting staff to deferred resignation"
    b = render.body(picks, {"n1": c}, 12, {"from_label": "Oct 21", "to_label": "Oct 22", "left_out_old": 3,
                                          "max_age_days": 14})
    assert "## 1. VA loses all its contracting staff" in b
    assert "VETERANS AFFAIRS, DEPARTMENT OF · Optum Public Sector Solutions, Inc. · $28,950,000,000 " \
           "(total incl. options) · FAR 6.302-2" in b
    assert "> “The Government lost all of its contracting staff" in b and "> — JA_Redacted.pdf, p. 1" in b
    assert "[SAM.gov notice](https://sam.gov/opp/n1/view)" in b and "JA%20Redacted.pdf" in b
    assert "12 new notices reviewed" in b and "3 notices first collected then but posted more than 14 days" in b
    assert "AI-selected" in b and not b.startswith("---")


def test_render_zero_picks():
    when = T("2026-10-22T03:00:00")   # 23:00 on Oct 21 in Eastern time
    assert render.subject([], when) == "Whoa J&As — Oct 21: nothing whoa-worthy"
    b = render.body([], {}, 7, {"from_label": "a", "to_label": "b"})
    assert b.startswith("Nothing whoa-worthy today (7 new notices reviewed).")


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
    assert (tmp_path / "out" / "digest.md").read_text().startswith("# Whoa J&As — Oct 21: VA loses")


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


def test_zero_picks_email_and_skip_option(tmp_path):
    sent = []

    def ok(subject, body, *, metadata, status):
        sent.append((subject, body))
        return {"id": "em_2", "status": "about_to_send"}
    run(tmp_path, args(tmp_path, "--send", "--zero-picks", "skip"), send_fn=ok, picks=[])
    assert sent == [] and not (tmp_path / "state.json").exists()
    run(tmp_path, args(tmp_path, "--send"), send_fn=ok, picks=[])
    assert sent[0][0] == "Whoa J&As — Oct 21: nothing whoa-worthy"
    assert sent[0][1].startswith("Nothing whoa-worthy today (1 new notice reviewed).")


def test_draft_does_not_move_the_state(tmp_path):
    def ok(subject, body, *, metadata, status):
        assert status == "draft"
        return {"id": "em_3", "status": "draft"}
    run(tmp_path, args(tmp_path, "--send", "--buttondown-status", "draft"), send_fn=ok)
    assert not (tmp_path / "state.json").exists()


def test_recovers_watermark_from_buttondown_when_state_save_failed(tmp_path):
    recent = [{"id": "em_9", "subject": "Whoa J&As — Oct 21: x", "creation_date": "2026-10-21T12:00:00Z",
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
                                "authority_cited": "FAR 6.302-1"})
    small = candidate("small", ext={"estimated_value_usd": 10_000.0})
    red = candidate("red", ext={"value_redacted": True})
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
    assert lines[1] == ("- Army · Big buy · Acme Corp · $5,000,000 · FAR 6.302-1 · "
                        "[SAM.gov](https://sam.gov/opp/big/view)")
    assert lines[2].startswith("- Veterans Affairs · Small · $10,000 ·")
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
    assert b.startswith("Nothing whoa-worthy today (1 new notice reviewed).")
    assert "## All new notices (1)" in b and "[SAM.gov](https://sam.gov/opp/n1/view)" in b
    assert b.index("All new notices") < b.index("*What this is:*")


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
