"""Tests for EXPERIMENTAL field extraction (federal_ja.analysis.extract).
Synthetic fixtures only: no real documents, no network."""

import json
from datetime import date

import pandas as pd
import pytest

from federal_ja.analysis import cli, data, extract, llm

NID = "b" * 32

PAGE1 = ("JUSTIFICATION AND APPROVAL FOR OTHER THAN FULL AND OPEN COMPETITION\n"
         "1. Contracting activity: Example Command.\n"
         "2. The proposed contractor is Acme Widget Corporation, the original equipment manufacturer.\n"
         "3. The total estimated value of this action is $1,250,000.00 including all options.\n"
         "4. Authority: 10 U.S.C. 3204(a)(1), as implemented by FAR 6.302-1, only one responsible source.\n"
         "5. Period of performance: 1 March 2026 through 28 February 2027.")
PAGE2 = ("Market research: a sources sought notice was posted; Beta Systems LLC responded but could not "
         "provide the proprietary data.\n"
         "Approval: this justification exceeds $900,000 but does not exceed $20 million and is approved by "
         "the competition advocate.\n"
         "Digitally signed by DOE.JANE.1234 Date: 2026.02.10 09:15:02 -05'00'\n"
         "Contracting Officer [REDACTED] Date: 02/12/2026")


def tables(pages=None, atts=None):
    notices = pd.DataFrame([{"notice_id": NID, "sam_link": f"https://sam.gov/opp/{NID}/view",
                             "title": "Widget support", "agency": "DEPT OF EXAMPLES", "sub_tier": None,
                             "office": None, "posted_date": "2026-03-01 10:00:00",
                             "solicitation_number": None, "award_number": None}])
    attachments = pd.DataFrame(atts or [
        {"notice_id": NID, "resource_id": "r1", "attachment_order": 1, "filename": "ja.pdf",
         "download_status": "ok", "file_kind": "pdf", "text": None}])
    pages = pd.DataFrame(pages or [
        {"notice_id": NID, "resource_id": "r1", "page_number": 1, "text": PAGE1, "text_source": "native"},
        {"notice_id": NID, "resource_id": "r1", "page_number": 2, "text": PAGE2, "text_source": "transcription"},
    ])
    return notices, attachments, pages


@pytest.fixture
def doc():
    return data.build_documents(*tables())[NID]


def answer(**over):
    base = {
        "is_ja": "yes", "doc_kind": "justification_and_approval",
        "doc_kind_quote": "JUSTIFICATION AND APPROVAL FOR OTHER THAN FULL AND OPEN COMPETITION",
        "authorities": [{"cited": "10 U.S.C. 3204(a)(1)", "plain": "only one responsible source",
                         "quote": "Authority: 10 U.S.C. 3204(a)(1), as implemented by FAR 6.302-1"}],
        "summary": "Widgets from the OEM; only it has the data.",
        "summary_quote": "Acme Widget Corporation, the original equipment manufacturer",
        "estimated_value_usd": 1250000, "value_quote": "total estimated value of this action is $1,250,000.00",
        "value_basis": "total_including_options", "value_redacted": False,
        "vendor": "Acme Widget Corporation", "vendor_quote": "The proposed contractor is Acme Widget Corporation",
        "period_of_performance": "1 March 2026 through 28 February 2027",
        "period_quote": "Period of performance: 1 March 2026 through 28 February 2027.",
        "signature_date": "2026-02-12", "signature_quote": "Contracting Officer [REDACTED] Date: 02/12/2026",
        "market_research_summary": "One firm responded and could not meet the need.",
        "market_research_quote": "Beta Systems LLC responded but could not provide the proprietary data",
        "other_firms_named": ["Beta Systems LLC"],
        "redacted": "yes", "redaction_quote": "Contracting Officer [REDACTED] Date",
        "notes": None,
    }
    base.update(over)
    return base


def fake(*answers):
    seq = list(answers)
    calls = []

    def call(prompt, *, schema, tool_name, model, max_tokens, **kw):
        if tool_name == extract.FACTS_TOOL:   # TRIAL second call: the facts the answer carries
            call.facts_calls.append(prompt)
            last = seq[min(len(calls) - 1, len(seq) - 1)] if calls else seq[0]
            return {"facts": last.get("facts", [])}, {"input_tokens": 500, "output_tokens": 100, "cost_usd": 0.0001}
        calls.append(prompt)
        a = seq[min(len(calls) - 1, len(seq) - 1)]
        return a, {"input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.0002}
    call.facts_calls = []
    call.calls = calls
    return call


# ── value-in-quote helpers ─────────────────────────────────────────────────

@pytest.mark.parametrize("text,value", [
    ("is $1,250,000.00 including", 1_250_000), ("about $1.2M total", 1_200_000),
    ("$ 3.5 million", 3_500_000), ("2.4 million dollars", 2_400_000), ("USD 50,000", 50_000),
    ("$750K", 750_000), ("$12,345", 12_345), ("Estimated Value | 1,950,000", 1_950_000),
    ("is $ 11, 640 including", 11_640), ("$5 ,180.00.", 5_180), ("$1 70,584.05", 170_584.05),
    ("USD 13, 000.00", 13_000),
])
def test_amounts_in(text, value):
    assert extract.value_in_quote(value, text)


def test_amount_not_in_quote():
    assert not extract.value_in_quote(1_000_000, "the value is $100,000")
    assert not extract.value_in_quote(400_000, "The estimated value of this action is $400,00.00.")
    assert not extract.value_in_quote(2026, "FY 2026 funds")


@pytest.mark.parametrize("text", [
    "Date: 2026.02.12 09:15", "Date: 02/12/2026", "Date: 2/12/26", "February 12, 2026",
    "12 Feb 2026", "12-FEB-2026", "12FEB2026", "2026-02-12",
    "Date: _02/12/2026___", "2/ 12/2026 Name", "_____2/ 12/ 2026______", "2026 Feb 12", "Oate: 2026 .02.12 13:~",
    "12 February, 2026____ SIGNATURE DATE", "Date:2026.02.1213:35:44-05'00'", "2026-02-12T12:51:44",
    "Feb 12,2026", "February 12th, 2026", "February12,2026", "Date: 2026 02 12 13:24", "2/12/2026Christopher",
    "Date: 2026FEB12",
])
def test_dates_in(text):
    assert date(2026, 2, 12) in extract.dates_in(text)


def test_tier_guard():
    assert extract.looks_like_tier(20_000_000, "exceeds $900,000 but does not exceed $20 million")
    assert extract.looks_like_tier(15_000_000, "$15 million or less: approved by the Competition Advocate")
    # real values written with "not to exceed", "over", "below" (all seen in the full run)
    assert not extract.looks_like_tier(750_000, "a not to exceed (NTE) value of $750,000.00")
    assert not extract.looks_like_tier(75_000_000, "This contract, valued at $75,000,000 over a 36-month period")
    assert not extract.looks_like_tier(15_000_000, "The estimated total value is $15M as set forth in the table below.")
    assert not extract.looks_like_tier(1_250_000, "exceeds $900,000 but does not exceed $1,250,000")
    assert not extract.looks_like_tier(20_000_000, "The estimated value is $20,000,000.")


def test_authority_in_quote():
    assert extract.authority_in_quote("FAR 6.302-1(a)(2)", "pursuant to FAR 6.302-1(a)(2)(iii)")
    assert extract.authority_in_quote("10 U.S.C. 3204(a)(1)", "10 USC 3204 (a)(1)")
    assert not extract.authority_in_quote("FAR 6.302-2", "FAR 6.302-1 only one source")


# ── one document ───────────────────────────────────────────────────────────

def test_good_answer_kept_with_pages(doc):
    rec = extract.extract_document(doc, call_fn=fake(answer()))
    f = rec["fields"]
    assert rec["dropped"] == [] and rec["usage"]["calls"] == 1
    assert f["estimated_value_usd"] == 1_250_000 and f["vendor"] == "Acme Widget Corporation"
    assert f["signature_date"] == "2026-02-12"
    assert f["signature_quote_info"]["quote_on_transcribed_page"] is True
    assert f["value_quote_info"]["quote_pages"] == [{"resource_id": "r1", "page_number": 1, "text_source": "native"}]
    assert f["other_firms_named"] == ["Beta Systems LLC"]
    assert rec["redaction_marker_in_text"] is True and rec["truncated"] is False


def test_unverified_values_dropped_and_retried(doc):
    bad = answer(estimated_value_usd=2_000_000, vendor="Gamma Inc", vendor_quote="The proposed contractor is Gamma Inc",
                 signature_date="2026-02-13", other_firms_named=["Beta Systems LLC", "Nowhere Corp"])
    call = fake(bad, bad)
    rec = extract.extract_document(doc, call_fn=call, retries=1)
    f = rec["fields"]
    assert len(call.calls) == 2 and rec["usage"]["calls"] == 2
    assert f["estimated_value_usd"] is None and f["vendor"] is None and f["signature_date"] is None
    reasons = {d["field"]: d["reason"] for d in rec["dropped"]}
    assert reasons["estimated_value_usd"] == "value not in its quote"
    assert reasons["vendor"] == "quote not found in document"
    assert reasons["signature_date"] == "date not in its quote"
    assert reasons["other_firms_named"] == "firm name not found in document"
    assert f["other_firms_named"] == ["Beta Systems LLC"]


def test_retry_keeps_better_attempt(doc):
    bad = answer(estimated_value_usd=2_000_000)
    call = fake(bad, answer())
    rec = extract.extract_document(doc, call_fn=call, retries=1)
    assert rec["attempt_kept"] == 2 and rec["fields"]["estimated_value_usd"] == 1_250_000


def test_approval_tier_is_not_the_value(doc):
    a = answer(estimated_value_usd=20_000_000,
               value_quote="this justification exceeds $900,000 but does not exceed $20 million")
    rec = extract.extract_document(doc, call_fn=fake(a), retries=0)
    assert rec["fields"]["estimated_value_usd"] is None
    assert "approval-tier guard" in rec["dropped"][0]["reason"]


def test_case_insensitive_heading_and_ellipsis(doc):
    a = answer(doc_kind_quote="Justification and Approval for Other Than Full and Open Competition",
               value_quote="estimated value ... $1,250,000.00")
    rec = extract.extract_document(doc, call_fn=fake(a), retries=0)
    assert rec["fields"]["doc_kind_quote_info"]["quote_match"] == "case_insensitive"
    assert rec["fields"]["estimated_value_usd"] is None


def test_redacted_value(doc):
    a = answer(estimated_value_usd=None, value_quote=None, value_redacted=True, value_basis=None)
    rec = extract.extract_document(doc, call_fn=fake(a))
    assert rec["fields"]["value_redacted"] is True and rec["dropped"] == []


def test_long_document_truncated_at_page_boundary(monkeypatch):
    pages = [{"notice_id": NID, "resource_id": "r1", "page_number": i, "text": f"page {i} " + "x" * 990,
              "text_source": "native"} for i in range(1, 11)]
    d = data.build_documents(*tables(pages=pages))[NID]
    text, cut = extract.model_text(d, max_chars=3500)
    assert cut and "page 3 " in text and "page 4 " not in text and "TEXT CUT HERE" in text
    monkeypatch.setattr(extract, "MAX_EXTRACT_CHARS", 3500)
    rec = extract.extract_document(d, call_fn=fake(answer()), retries=0)
    assert rec["truncated"] is True and rec["chars_sent"] < rec["chars_total"]


def test_model_error_propagates(doc):
    def boom(*a, **k):
        raise llm.ModelError("down")
    with pytest.raises(llm.ModelError):
        extract.extract_document(doc, call_fn=boom)


def test_schema_is_strict_compatible():
    def walk(s):
        if s.get("type") == "object":
            assert s["additionalProperties"] is False
            assert set(s["required"]) == set(s["properties"])
            for v in s["properties"].values():
                walk(v)
        if s.get("type") == "array":
            walk(s["items"])
    walk(extract.SCHEMA)


def test_haiku_long_prompt_price():
    assert llm.cost_usd("claude-haiku-5-5", 1_000_000, 0) == 0.5
    assert llm.cost_usd("claude-haiku-5-5", 100_000, 0) == 0.01


# ── CLI: scope, resumability, failures, csv ────────────────────────────────

def test_cli_extract(tmp_path, monkeypatch):
    other = "c" * 32
    n, a, p = tables()
    n = pd.concat([n, n.assign(notice_id=other)], ignore_index=True)
    # the second notice's only attachment was not downloaded: out of scope
    a = pd.concat([a, a.assign(notice_id=other, download_status="not_public")], ignore_index=True)
    dd = tmp_path / "tables"
    dd.mkdir()
    for name, df in zip(data.TABLES, (n, a, p)):
        df.to_parquet(dd / f"{name}.parquet")
    (dd / "revision.json").write_text(json.dumps({"sha": "abc"}))
    calls = []
    real = extract.extract_document

    def fake_extract(doc, *, model, retries, effort):
        calls.append(doc.notice_id)
        if len(calls) == 1:
            raise llm.ModelError("overloaded")
        return real(doc, model=model, retries=retries, effort=effort, call_fn=fake(answer()))
    monkeypatch.setattr(extract, "extract_document", fake_extract)
    out = tmp_path / "out"
    args = ["extract", "--out", str(out), "--data-dir", str(dd), "--max-usd", "1"]
    assert cli.main(args) == 1                       # failure recorded, nothing stored
    assert (out / "failures.jsonl").read_text().count("overloaded") == 1
    assert not (out / "extractions.jsonl").exists() or not (out / "extractions.jsonl").read_text()
    assert cli.main(args) == 0                       # retried
    assert cli.main(args) == 0                       # resumable: nothing left
    assert calls == [NID, NID]
    rows = pd.read_csv(out / "extractions.csv", dtype=str)
    assert rows.notice_id.tolist() == [NID]
    assert rows.vendor[0] == "Acme Widget Corporation" and rows.estimated_value_usd[0] == "1250000.0"
    assert "https://sam.gov/opp/" in rows.sam_link[0]


def test_reverify_reapplies_checks(doc):
    rec = extract.extract_document(doc, call_fn=fake(answer()), retries=0)
    rec["fields"]["signature_date"] = None          # as an older check version might have left it
    rec["checks_version"] = "old"
    again = extract.reverify(rec, doc)
    assert again["checks_version"] == extract.CHECKS_VERSION and again["raw"] == rec["raw"]
    assert again["fields"]["signature_date"] == "2026-02-12" and again["usage"] == rec["usage"]


def test_garbled_text_layer_flag():
    shifted = "7KH FRQWUDFW ZLOO EH DZDUGHG DQG WKH YHQGRU LV WKH RQO\\ VRXUFH ZLWK WKH GDWD."
    pages = [{"notice_id": NID, "resource_id": "r1", "page_number": 1, "text": PAGE1 + "\n" + shifted,
              "text_source": "native"}]
    d = data.build_documents(*tables(pages=pages))[NID]
    assert extract.garbled_text_layer(d)
    clean = data.build_documents(*tables())[NID]
    assert not extract.garbled_text_layer(clean)


def test_malformed_answer_is_a_model_error(doc):
    with pytest.raises(llm.ModelError):
        extract.extract_document(doc, call_fn=fake(answer(authorities='[{"cited": "FAR 6.302-1"}]')))
    with pytest.raises(llm.ModelError):
        extract.extract_document(doc, call_fn=fake(answer(authorities=["FAR 6.302-1"])))


def test_truncation_puts_the_justification_first():
    atts = [
        {"notice_id": NID, "resource_id": "m", "attachment_order": 1, "filename": "technical_manual.pdf",
         "download_status": "ok", "file_kind": "pdf", "text": None},
        {"notice_id": NID, "resource_id": "j", "attachment_order": 2, "filename": "SOLE SOURCE-Signed_Redacted.pdf",
         "download_status": "ok", "file_kind": "pdf", "text": None}]
    pages = [{"notice_id": NID, "resource_id": "m", "page_number": i, "text": "manual " + "x" * 990,
              "text_source": "native"} for i in range(1, 11)]
    pages.append({"notice_id": NID, "resource_id": "j", "page_number": 1, "text": PAGE1, "text_source": "native"})
    d = data.build_documents(*tables(pages=pages, atts=atts))[NID]
    text, cut = extract.model_text(d, max_chars=3000)
    assert cut and text.startswith("=== ATTACHMENT: SOLE SOURCE-Signed_Redacted.pdf") and "Acme Widget" in text
    whole, cut2 = extract.model_text(d, max_chars=10**6)
    assert not cut2 and whole.startswith("=== ATTACHMENT: technical_manual.pdf")   # unchanged under the limit
