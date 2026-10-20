"""Tests for the extract3 fields: people, need_by, stated_pop_limit,
covered_piids, incumbent_piid, market_research_methods,
redaction_significant (since extract5 all but people and covered_piids come
from the facts list). Synthetic fixtures only: no real documents, no
network."""

import json

import pytest

from federal_ja.analysis import data, extract
from tests.test_field_extraction import NID, PAGE1, PAGE2, answer, fake, tables

PAGE = ("JUSTIFICATION FOR OTHER THAN FULL AND OPEN COMPETITION\n"
        "This bridge extends contract W912DY-21-C-0042 under modification P00012. The current contract\n"
        "expires and services must be in place by 30 September 2026 to avoid a lapse.\n"
        "The period of performance shall not exceed six (6) months.\n"
        "Market research: a sources sought notice was posted on SAM.gov and a search of the Dynamic Small\n"
        "Business Search was performed. The estimated value is [REDACTED].\n"
        "Company Point of Contact: Sandra Selzer, CEO, Acme Widget Corporation\n"
        "I certify this justification is accurate.\n"
        "JOHN Q.\nPUBLIC\nContracting Officer, Example Command\n"
        "Digitally signed by SMITH.JANE.A.1234567890 Date: 2026.02.10\n"
        "Competition Advocate\n"
        "[REDACTED (b)(6)]\n"
        "Associate Counsel\n"
        "[REDACTED]\n"
        "Program Manager\n"
        "Name: ________________ Title: Head of Contracting Activity")


@pytest.fixture
def doc():
    # the base fixture's two pages (so the other fields' quotes verify) plus this page
    return data.build_documents(*tables(pages=[
        {"notice_id": NID, "resource_id": "r1", "page_number": 1, "text": PAGE1, "text_source": "native"},
        {"notice_id": NID, "resource_id": "r1", "page_number": 2, "text": PAGE2, "text_source": "transcription"},
        {"notice_id": NID, "resource_id": "r1", "page_number": 3, "text": PAGE, "text_source": "transcription"}]))[NID]


def person(**kw):
    base = {"name": None, "name_quote": "", "title": "", "organization": "", "role_in_document": "other",
            "description": "", "redacted": False}
    base.update(kw)
    return base


def F(kind, value, quote=""):
    return {"kind": kind, "value": value, "quote": quote}


def run(doc, **over):
    rec = extract.extract_document(doc, call_fn=fake(answer(**over)), retries=0)
    return rec["fields"], rec["dropped"]


# ── people ─────────────────────────────────────────────────────────────────

def test_named_people_kept_with_pages(doc):
    f, dropped = run(doc, people=[
        person(name="JOHN Q. PUBLIC", name_quote="JOHN Q. PUBLIC Contracting Officer, Example Command",
               title="Contracting Officer", organization="Example Command", role_in_document="contracting_officer",
               description="Signs the CO certification."),
        person(name="Sandra Selzer", name_quote="Company Point of Contact: Sandra Selzer, CEO",
               title="CEO", organization="Acme Widget Corporation", role_in_document="vendor_employee"),
    ])
    names = [p["name"] for p in f["people"]]
    assert names == ["JOHN Q. PUBLIC", "Sandra Selzer"]
    jq = f["people"][0]
    # the name is split across a line break in the text; line breaks count as spaces
    assert jq["name_quote_info"]["quote_match"] == "exact"
    assert jq["name_quote_info"]["quote_pages"][0]["page_number"] == 3
    assert jq["name_quote_info"]["quote_on_transcribed_page"] is True
    assert jq["title"] == "Contracting Officer" and jq["organization"] == "Example Command"
    assert not [d for d in dropped if d["field"].startswith("people")]


def test_digital_signature_stamp_kept_as_written(doc):
    f, _ = run(doc, people=[person(name="SMITH.JANE.A.1234567890",
                                   name_quote="Digitally signed by SMITH.JANE.A.1234567890 Date: 2026.02.10",
                                   role_in_document="competition_advocate")])
    p = f["people"][0]
    assert p["name"] == "SMITH.JANE.A.1234567890" and p["name_is_signature_stamp"] is True


def test_invented_or_unquoted_names_dropped(doc):
    f, dropped = run(doc, people=[
        # a name that is not in its (real) quote
        person(name="Mary Major", name_quote="I certify this justification is accurate."),
        # a quote that is not in the document
        person(name="Mary Major", name_quote="Mary Major, Contracting Officer"),
        # no name and not redacted (an unsigned block)
        person(name=None, name_quote="Name: ________________ Title: Head of Contracting Activity", title="HCA"),
    ])
    assert f["people"] == []
    reasons = [d["reason"] for d in dropped if d["field"] == "people"]
    assert reasons == ["name not in its quote", "quote not found in document", "no name, and not marked redacted"]


def test_redacted_blocks(doc):
    f, dropped = run(doc, people=[
        person(redacted=True, name_quote="[REDACTED (b)(6)] Associate Counsel", title="Associate Counsel",
               role_in_document="legal_reviewer"),
        person(redacted=True, name_quote="[REDACTED] Program Manager", title="Program Manager",
               role_in_document="requiring_or_technical_official"),
        # a guessed name for a redacted block is never kept
        person(redacted=True, name="Pat Doe", name_quote="[REDACTED] Program Manager"),
        # "redacted" with a quote that shows no redaction marker
        person(redacted=True, name_quote="Competition Advocate [REDACTED (b)(6)]".split(" [")[0] + " Advocate"),
    ])
    assert [(p["name"], p["title"], p["redacted"]) for p in f["people"]] == [
        (None, "Associate Counsel", True), (None, "Program Manager", True)]
    reasons = [d["reason"] for d in dropped if d["field"] == "people"]
    assert "name given for an entry marked redacted" in reasons
    assert any(r in ("redacted entry's quote shows no redaction marker", "quote not found in document") for r in reasons)


def test_redaction_marker_patterns():
    rx = extract.REDACTION_MARK
    assert rx.search("[REDACTED (b)(6)]") and rx.search("( b ) ( 6 )") and rx.search("(b)(7)(C)")
    assert rx.search("name redacted") and rx.search("withheld")
    assert not rx.search("FAR 6.302-1(b)(4)") and not rx.search("Contracting Officer")


def test_title_not_in_document_cleared_duplicate_dropped(doc):
    f, dropped = run(doc, people=[
        person(name="Sandra Selzer", name_quote="Sandra Selzer, CEO", title="Chief Executive Officer"),
        person(name="sandra  selzer", name_quote="Sandra Selzer, CEO, Acme"),
    ])
    assert len(f["people"]) == 1 and f["people"][0]["title"] is None
    fields = {d["field"]: d["reason"] for d in dropped}
    assert fields["people.title"] == "title not found in document"
    assert fields["people"] == "duplicate of an earlier entry"


def test_people_drops_do_not_trigger_a_retry(doc):
    call = fake(answer(people=[person(name="Nobody Here", name_quote="Nobody Here signs")]))
    rec = extract.extract_document(doc, call_fn=call, retries=1)
    assert rec["usage"]["calls"] == 1


def test_people_flat_columns_and_redaction_evidence(doc):
    a = answer(people=[
        person(name="JOHN Q. PUBLIC", name_quote="JOHN Q. PUBLIC Contracting Officer", title="Contracting Officer",
               role_in_document="contracting_officer"),
        person(name="Sandra Selzer", name_quote="Sandra Selzer, CEO", role_in_document="vendor_employee"),
        person(redacted=True, name_quote="[REDACTED (b)(6)] Associate Counsel", title="Associate Counsel",
               role_in_document="legal_reviewer"),
    ], redacted="no", redaction_quote=None)
    rec = extract.extract_document(doc, call_fn=fake(a), retries=0)
    row = extract.flat_row(rec, {})
    assert row["people_named"] == "JOHN Q. PUBLIC (Contracting Officer); Sandra Selzer"
    assert row["people_signature_block"] == "JOHN Q. PUBLIC (Contracting Officer); [name redacted] (Associate Counsel)"
    assert row["people_redacted_blocks"] == 1
    assert [p["name"] for p in json.loads(row["people_json"])] == ["JOHN Q. PUBLIC", "Sandra Selzer", None]
    assert "a person's name redacted" in row["redaction_evidence"]


def test_people_shape_errors():
    assert extract.shape_errors({"people": "[]"}) == ["people is str"]
    assert extract.shape_errors({"people": ["x"]}) == ["people item is not an object"]


# ── need_by, stated_pop_limit, PIIDs, market research, redaction_significant ──

@pytest.mark.parametrize("text,days", [
    ("not to exceed six (6) months", 180), ("shall not exceed 12 months", 365), ("limited to 180 days", 180),
    ("a maximum of one year", 365), ("90 calendar days", 90), ("a 1-year bridge", 365), ("2 weeks", 14),
])
def test_days_in_quote(text, days):
    assert extract.days_in_quote(days, text)


def test_days_not_in_quote():
    assert not extract.days_in_quote(90, "not to exceed six (6) months")
    assert not extract.days_in_quote(180, "estimated value of $180")


def test_extract3_fields_kept(doc):
    f, dropped = run(doc, covered_piids=["W912DY-21-C-0042", "P00012", "W912DY-21-C-0042"], facts=[
        F("need_by", "2026-09-30", "services must be in place by 30 September 2026"),
        F("stated_pop_limit", "180", "The period of performance shall not exceed six (6) months."),
        F("incumbent_piid", "W912DY-21-C-0042", "This bridge extends contract W912DY-21-C-0042"),
        F("market_research_method", "sources_sought", "a sources sought notice was posted on SAM.gov"),
        F("market_research_method", "SBA_DSBS", "a search of the Dynamic Small Business Search was performed"),
        F("market_research_method", "sources_sought", "a sources sought notice was posted"),
        F("Redaction_Significant", "yes", "The estimated value is [REDACTED].")])
    assert f["need_by_date"] == "2026-09-30" and f["stated_pop_limit_days"] == 180
    assert f["covered_piids"] == ["W912DY-21-C-0042", "P00012"]
    assert f["incumbent_piid"] == "W912DY-21-C-0042"
    assert [m["method"] for m in f["market_research_methods"]] == ["sources_sought", "sba_dsbs"]
    assert f["redaction_significant"] == "yes" and f["redaction_significant_quote_info"]["quote_match"]
    assert not dropped
    row = extract.flat_row({**extract.extract_document(doc, call_fn=fake(answer(
        covered_piids=["W912DY-21-C-0042", "P00012"])), retries=0)}, {})
    assert row["covered_piids"] == "W912DY-21-C-0042; P00012"


def test_extract3_fields_dropped(doc):
    f, dropped = run(doc, covered_piids=["W912DY-99-C-9999"], facts=[
        F("need_by", "2026-10-30", "services must be in place by 30 September 2026"),
        F("stated_pop_limit", "365", "The period of performance shall not exceed six (6) months."),
        F("incumbent_piid", "W912DY-21-C-0043", "This bridge extends contract W912DY-21-C-0042"),
        F("market_research_method", "industry_day", "an industry day was held"),
        F("redaction_significant", "yes", "")])
    assert f["need_by_date"] is None and f["stated_pop_limit_days"] is None
    assert f["covered_piids"] == [] and f["incumbent_piid"] is None and f["market_research_methods"] == []
    assert f["redaction_significant"] == "unclear"
    reasons = {d["field"]: d["reason"] for d in dropped}
    assert reasons == {"need_by_date": "date not in its quote", "stated_pop_limit_days": "duration not in its quote",
                       "covered_piids": "contract number not found in document",
                       "incumbent_piid": "contract number not in its quote",
                       "market_research_methods": "quote not found in document",
                       "redaction_significant": "no quote"}


def test_extract3_empty_values(doc):
    f, dropped = run(doc, covered_piids=[], facts=[F("redaction_significant", "no"), F("incumbent_piid", "")])
    assert (f["need_by_date"], f["stated_pop_limit_days"], f["covered_piids"], f["incumbent_piid"],
            f["market_research_methods"], f["redaction_significant"]) == (None, None, [], None, [], "no")
    assert dropped == []


def test_old_records_still_flatten():
    """A stored extract2 record (no extract3 fields) still makes a flat row."""
    d = data.build_documents(*tables())[NID]
    rec = extract.extract_document(d, call_fn=fake(answer()), retries=0)
    for k in ("people", "need_by_date", "covered_piids", "market_research_methods", "redaction_significant"):
        rec["fields"].pop(k, None)
    row = extract.flat_row(rec, {})
    assert row["people_named"] is None and row["covered_piids"] is None and row["people_redacted_blocks"] == 0


def test_schema_union_budget():
    """Strict tool use rejected the schema at 17 union-typed parameters
    (live 400, 2026-10-20); 15 is accepted."""
    def unions(x):
        if isinstance(x, dict):
            return (isinstance(x.get("type"), list) or "anyOf" in x) + sum(unions(v) for v in x.values())
        if isinstance(x, list):
            return sum(unions(v) for v in x)
        return 0
    assert unions(extract.SCHEMA) <= 15


def test_new_instructions_are_in_the_cached_part():
    for k in ("- people:", "- covered_piids:", "- facts:", *(f'* kind "{x}"' for x in extract.FACT_KINDS)):
        assert k in extract.PROMPT_CACHED and k not in extract.PROMPT_PER_DOC


def test_fact_kinds_and_codes_are_checked_in_code(doc):
    """The schema leaves kind and value as plain strings (no enums, to keep
    the compiled grammar small): an unknown kind or code, a number that does
    not parse, or a second entry for a single-valued kind is dropped and
    listed, never kept or silently lost."""
    f, dropped = run(doc, facts=[
        F("deadline", "2026-09-30", "services must be in place by 30 September 2026"),
        F("market_research_method", "carrier_pigeon", "a sources sought notice was posted on SAM.gov"),
        F("stated_pop_limit", "six months", "The period of performance shall not exceed six (6) months."),
        F("redaction_significant", "maybe", "The estimated value is [REDACTED]."),
        F("incumbent_piid", "W912DY-21-C-0042", "This bridge extends contract W912DY-21-C-0042"),
        F("incumbent_piid", "P00012", "under modification P00012")])
    assert f["need_by_date"] is None and f["market_research_methods"] == [] and f["stated_pop_limit_days"] is None
    assert f["redaction_significant"] is None and f["incumbent_piid"] == "W912DY-21-C-0042"
    reasons = sorted((d["field"], d["reason"]) for d in dropped)
    assert reasons == [("facts", "unknown fact kind"),
                       ("incumbent_piid", "a second entry for a single-valued fact; the first is kept"),
                       ("market_research_methods", "not a known method code"),
                       ("redaction_significant", "not yes, no or unclear"),
                       ("stated_pop_limit_days", "not a positive whole number")]


def test_one_call_per_notice(doc):
    """extract5 asks everything in one call (the extract4 trial made two)."""
    call = fake(answer())
    rec = extract.extract_document(doc, call_fn=call, retries=0)
    assert len(call.calls) == 1 and rec["usage"]["calls"] == 1 and "facts" not in rec["usage"]


def test_placeholder_empty_strings_are_empty(doc):
    """'""' (a literal pair of quote marks) and similar placeholders mean
    "none", not a value whose quote failed."""
    f, dropped = run(doc, covered_piids=['""', "null"],
                     facts=[F("incumbent_piid", '""', '""'), F("redaction_significant", "no", '""')],
                     people=[person(name="Sandra Selzer", name_quote="Sandra Selzer, CEO", title='""',
                                    organization="N/A")])
    assert f["incumbent_piid"] is None and f["covered_piids"] == []
    assert f["people"][0]["title"] is None and f["people"][0]["organization"] is None
    assert not [d for d in dropped if d["field"] in ("incumbent_piid", "covered_piids") or d["field"].startswith("people")]
