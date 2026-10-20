"""Tests for the EXPERIMENTAL analysis package. Synthetic fixtures only: no
real documents, no labels, no network."""

import json

import pandas as pd
import pytest

from federal_ja.analysis import cli, data, llm, patterns, prompts, tagger, text

NID = "a" * 32

PAGE1 = ("JUSTIFICATION FOR OTHER THAN FULL AND OPEN COMPETITION. The follow-on competitive "
         "procurement was delayed because the requirements package was not completed by the program office.")
PAGE2 = ("Due to the Deferred Resignation Program, the office lost three contracting officers and the "
         "recompetition could not be awarded before the current contract ends.")
DOCX = "Market research: two firms responded to the sources sought notice and were found not capable."


def tables():
    notices = pd.DataFrame([{"notice_id": NID, "sam_link": f"https://sam.gov/opp/{NID}/view",
                             "title": "Widget support", "agency": "DEPT OF EXAMPLES", "sub_tier": None,
                             "office": None, "posted_date": "2026-09-01 10:00:00",
                             "solicitation_number": None, "award_number": None}])
    attachments = pd.DataFrame([
        # deliberately out of order: attachment_order decides
        {"notice_id": NID, "resource_id": "r2", "attachment_order": 2, "filename": "memo.docx",
         "download_status": "ok", "file_kind": "docx", "text": DOCX},
        {"notice_id": NID, "resource_id": "r1", "attachment_order": 1, "filename": "ja.pdf",
         "download_status": "ok", "file_kind": "pdf", "text": "ignored for PDFs"},
        {"notice_id": NID, "resource_id": "r3", "attachment_order": 3, "filename": "prices.xlsx",
         "download_status": "ok", "file_kind": "xlsx", "text": "PRICE SHEET should not appear"},
        {"notice_id": NID, "resource_id": "r4", "attachment_order": 4, "filename": "gone.pdf",
         "download_status": "deleted", "file_kind": None, "text": None},
    ])
    pages = pd.DataFrame([
        {"notice_id": NID, "resource_id": "r1", "page_number": 2, "text": PAGE2, "text_source": "transcription"},
        {"notice_id": NID, "resource_id": "r1", "page_number": 1, "text": PAGE1, "text_source": "native"},
    ])
    return notices, attachments, pages


@pytest.fixture
def doc():
    return data.build_documents(*tables())[NID]


# ── text assembly ──────────────────────────────────────────────────────────

def test_pages_in_attachment_then_page_order(doc):
    assert [(p.resource_id, p.page_number, p.text_source) for p in doc.pages] == [
        ("r1", 1, "native"), ("r1", 2, "transcription"), ("r2", None, "native")]


def test_xlsx_and_failed_downloads_left_out(doc):
    assert "PRICE SHEET" not in doc.model_text()
    assert all(p.resource_id in ("r1", "r2") for p in doc.pages)


def test_model_text_layout(doc):
    assert doc.model_text() == (f"=== ATTACHMENT: ja.pdf ===\n{PAGE1}\n\n{PAGE2}\n\n"
                                f"=== ATTACHMENT: memo.docx ===\n{DOCX}")


def test_verification_text_has_no_headers(doc):
    assert "=== ATTACHMENT" not in doc.verification_text()
    assert doc.has_transcribed_page


def test_notice_without_text_is_counted_as_skipped():
    n, a, p = tables()
    n2 = pd.concat([n, n.assign(notice_id="b" * 32)])
    docs = data.build_documents(n2, a, p)
    assert docs["b" * 32].pages == [] and docs["b" * 32].skip_reason() == "no_text"


def test_too_long_is_skipped():
    n, a, p = tables()
    p = p.assign(text=lambda d: d.text + " word" * 20000)
    assert data.build_documents(n, a, p)[NID].skip_reason() == "too_long"


# ── quote verification ─────────────────────────────────────────────────────

def test_exact_quote_located_on_native_page(doc):
    m = text.locate_quote("the requirements package was not completed by the program office", doc)
    assert m.verified and m.match == "exact"
    assert m.pages == [{"resource_id": "r1", "page_number": 1, "text_source": "native"}]
    assert m.transcribed is False


def test_quote_on_transcribed_page(doc):
    m = text.locate_quote("the office lost three contracting officers", doc)
    assert m.verified and m.transcribed is True and m.pages[0]["page_number"] == 2


def test_whitespace_and_curly_quote_normalisation():
    d = text.Document("x", {}, [text.Page("r", "f.pdf", 1, "native",
                                          "the agency’s  re-\ncompetition was\ndelayed by staffing")])
    m = text.locate_quote("the agency's re-competition was delayed by staffing", d)
    assert m.verified and m.match == "normalized"


def test_quote_across_page_break():
    d = text.Document("x", {}, [text.Page("r", "f.pdf", 1, "native", "alpha beta gamma delta"),
                                text.Page("r", "f.pdf", 2, "transcription", "epsilon zeta eta theta")])
    m = text.locate_quote("gamma delta epsilon zeta eta", d)
    assert m.verified and [p["page_number"] for p in m.pages] == [1, 2] and m.transcribed is True


@pytest.mark.parametrize("q", [
    "requirements package was not completed ... program office",     # stitched
    "requirements package",                                          # under 5 words
    "the requirements package was completed on time by the office",  # not in the text
    "=== ATTACHMENT: ja.pdf === JUSTIFICATION FOR OTHER",            # header
])
def test_bad_quotes_fail(doc, q):
    assert not text.locate_quote(q, doc).verified


def test_ligature_glyphs_match():
    d = text.Document("x", {}, [text.Page("r", "f.pdf", 1, "native", "there is no su\u01aficient time to compete this")])
    assert text.locate_quote("there is no sufficient time to compete", d).verified


# ── tagging and output schema ──────────────────────────────────────────────

def fake_call(responses):
    calls = []

    def call(prompt, *, schema, tool_name, model, pass_no):
        prompt = llm.prompt_text(prompt)
        calls.append((tool_name, pass_no, prompt))
        for key, resp in responses.items():
            if key in prompt:
                return resp(pass_no) if callable(resp) else resp, {"input_tokens": 100, "output_tokens": 10,
                                                                     "cost_usd": 0.0001}
        return ({"answer": False, "quote": None, "quote2": None, "explanation": "", "n_firms": None},
                {"input_tokens": 100, "output_tokens": 10, "cost_usd": 0.0001})
    call.calls = calls
    return call


@pytest.fixture
def shipped_all(monkeypatch):
    monkeypatch.setattr(tagger, "STATUS", {c: "screening" for c in tagger.CATEGORIES})


def test_tag_document_rows(doc, shipped_all):
    responses = {
        "QUESTION: Does the document's own narrative say": {
            "answer": True, "quote": "the requirements package was not completed by the program office",
            "quote2": "this quote does not exist anywhere in it", "explanation": "agency delay"},
        "DEFINITION\nsources_rejected": {
            "answer": True, "quote": "two firms responded to the sources sought notice and were found not capable",
            "quote2": None, "explanation": "rejected", "n_firms": 2},
        "QUESTION: Does the document say that a FEDERAL workforce": lambda p: {
            "answer": True, "quote": "Due to the Deferred Resignation Program, the office lost three contracting officers",
            "quote2": None, "explanation": "tracking"} if p == 1 else
            {"answer": False, "quote": None, "quote2": None, "explanation": ""},
    }
    call = fake_call(responses)
    cats = ["workforce_cause", "agency_delay_cause", "sources_rejected"]
    review = tagger.tag_document(doc, cats, call_fn=call)
    assert review["experimental"] is True
    assert review["prompt_version"] == prompts.PROMPT_VERSION
    assert len(call.calls) == 6       # 3 categories with keyword hits x 2 passes
    rows = tagger.output_rows(review, doc, "deadbeef")
    by_cat = {r["category"]: r for r in rows}
    assert set(by_cat) == {"workforce_cause", "agency_delay_cause", "sources_rejected"}
    for r in rows:
        assert r["experimental"] is True
        assert "not a finding" in r["note"].lower()
        assert r["dataset_revision"] == "deadbeef"
        assert r["quote_match"] in ("exact", "normalized")
        assert r["quote_pages"]
    assert by_cat["workforce_cause"]["found_in_passes"] == [1]
    assert by_cat["workforce_cause"]["all_passes"] is False
    assert by_cat["workforce_cause"]["quote_on_transcribed_page"] is True
    assert by_cat["agency_delay_cause"]["quote2"] is None           # unverified quote2 removed
    assert by_cat["agency_delay_cause"]["found_in_passes"] == [1, 2]
    assert by_cat["sources_rejected"]["n_firms"] == 2
    assert by_cat["sources_rejected"]["quote_pages"][0]["page_number"] is None   # docx
    json.dumps(rows)


def test_tag_document_sends_cached_parts_and_sums_cache_usage(doc, shipped_all):
    seen = []

    def call(prompt, *, schema, tool_name, model, pass_no):
        seen.append(prompt)
        return ({"answer": False, "quote": None, "quote2": None, "explanation": "", "n_firms": None},
                {"input_tokens": 1000, "output_tokens": 10, "cache_creation_input_tokens": 600 if pass_no == 1 else 0,
                 "cache_read_input_tokens": 0 if pass_no == 1 else 600, "cost_usd": 0.0001})
    review = tagger.tag_document(doc, ["workforce_cause", "agency_delay_cause", "sources_rejected"], call_fn=call)
    assert len(seen) == 6
    for p in seen:      # (instructions, per-document part); the first is one of the frozen templates' heads
        assert isinstance(p, tuple) and len(p) == 2
        assert any(t.startswith(p[0]) and t[len(p[0]):].startswith(prompts.METADATA_TOKEN)
                   for t in prompts.CONFIRM_TEMPLATES.values())
    assert review["usage"]["cache_creation_input_tokens"] == 3 * 600
    assert review["usage"]["cache_read_input_tokens"] == 3 * 600
    assert review["usage"]["input_tokens"] == 6000


def test_model_error_propagates(doc, shipped_all):
    def boom(*a, **k):
        raise llm.ModelError("down")
    with pytest.raises(llm.ModelError):
        tagger.tag_document(doc, ["workforce_cause"], call_fn=boom)


def test_union_passes_merges_contained_quotes():
    a = {"category": "workforce_cause", "quote": "staff losses delayed the award"}
    b = {"category": "workforce_cause", "quote": "Because of staff losses delayed the award, we"}
    u = tagger.union_passes([[a], [b]])
    assert len(u) == 1 and u[0]["passes"] == [1, 2]


def test_patterns_and_excerpts(doc):
    hits = patterns.find_hits(doc.verification_text(), "agency_delay_cause")
    assert hits
    ex = patterns.excerpts(doc.normalized(), hits)
    assert "requirements package was not completed" in " ".join(ex)


def test_protest_delay_is_not_a_candidate():
    t = "The award was delayed because of a GAO protest filed by an offeror."
    assert patterns.find_hits(t, "agency_delay_cause") == []


def test_prompt_is_frozen():
    # Any edit to the frozen text must come with a new PROMPT_VERSION and a
    # new validation round. Update this hash only together with both.
    import hashlib
    h = hashlib.sha256("".join(prompts.CONFIRM_TEMPLATES[c]
                               for c in ("agency_delay_cause", "sources_rejected")).encode()).hexdigest()[:16]
    assert (prompts.PROMPT_VERSION, h) == ("2026-10-16.frozen1", FROZEN_HASH)
    # The round-4 workforce_cause method: prompt and gate patterns together.
    h4 = hashlib.sha256((prompts.CONFIRM_TEMPLATES["workforce_cause"]
                         + json.dumps(patterns.WORKFORCE_PATTERNS, sort_keys=True)).encode()).hexdigest()[:16]
    assert (prompts.WORKFORCE_CONFIRM_VERSION, h4) == ("2026-10-17.wfgate1", WORKFORCE_HASH)


FROZEN_HASH = "341859a1880247af"
WORKFORCE_HASH = "22199f4bf415584f"


# ── CLI ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cat", ["repeat_bridge", "political_direction", "expiring_funds"])
def test_cli_refuses_unknown_category(cat, tmp_path):
    with pytest.raises(SystemExit) as e:
        cli.main(["tag", "--category", cat, "--out", str(tmp_path)])
    assert "unknown category" in str(e.value)


def test_cli_help_says_experimental(capsys):
    with pytest.raises(SystemExit):
        cli.main(["tag", "--help"])
    assert "EXPERIMENTAL" in capsys.readouterr().out


def test_shipped_categories_match_validation():
    # Only categories with a "screening" or better status may run.
    assert sorted(tagger.shipped()) == ["agency_delay_cause", "sources_rejected", "workforce_cause"]


# ── workforce_cause gate (round 4) ──────────────────────────────────────────

@pytest.mark.parametrize("t", [
    "due to the Deferred-\nResignation Program",            # hyphen + line wrap
    "the agency lost three contracting officers",
    "reduc-\ntions in force at the regional office",          # word split across a line
    "the 43-day government shut-\ndown delayed the award",
    "a hiring  freeze is in effect",
    "RE-ORGANIZATION of the program office",
    "the Department\u2019s staffing shortages",              # curly apostrophe
    "per E.O. 14222 and the DOGE review",
    "the COR left the Government in March",
    "Defened Resignation",                                    # OCR misspelling
])
def test_workforce_gate_matches_messy_text(t):
    assert patterns.find_hits(t, "workforce_cause")


def test_workforce_gate_ignores_plain_text():
    assert patterns.find_hits("The contractor has unique expertise and proprietary software.",
                              "workforce_cause") == []


def test_workforce_quote_must_name_the_event(doc, shipped_all):
    # The model says yes but quotes only the reason, not the workforce event:
    # the tag is dropped by the deterministic check.
    responses = {"QUESTION: Does the document say that a FEDERAL workforce": {
        "answer": True, "quote": "the recompetition could not be awarded before the current contract ends",
        "quote2": None, "explanation": "x"}}
    review = tagger.tag_document(doc, ["workforce_cause"], call_fn=fake_call(responses))
    assert review["tags"] == [] and review["categories"]["workforce_cause"] == "not_tagged"
    assert review["prompt_versions"] == {"workforce_cause": prompts.WORKFORCE_CONFIRM_VERSION}
