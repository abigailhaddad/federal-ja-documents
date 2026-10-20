"""TRIAL fields (prompt extract4trial): bridge_or_extension,
prior_extension_count, contract_types, is_letter_contract, funding_sources,
policy_citations, urgency_trigger. Synthetic fixtures; no network."""

import pytest

from federal_ja.analysis import data, extract
from tests.test_field_extraction import NID, PAGE1, PAGE2, answer, fake, tables

PAGE = ("This is the seventh bridge for these services. The Government will issue a letter contract\n"
        "(undefinitized contract action) on a firm-fixed-price basis. The action is funded with\n"
        "One Big Beautiful Bill Act funds. Citations reflect the Revolutionary FAR Overhaul under EO 14275.\n"
        "Urgency: the House Committee on Oversight requires delivery by 1 June 2026.")


@pytest.fixture
def doc():
    return data.build_documents(*tables(pages=[
        {"notice_id": NID, "resource_id": "r1", "page_number": 1, "text": PAGE1, "text_source": "native"},
        {"notice_id": NID, "resource_id": "r1", "page_number": 2, "text": PAGE2, "text_source": "transcription"},
        {"notice_id": NID, "resource_id": "r1", "page_number": 3, "text": PAGE, "text_source": "native"}]))[NID]


def run(doc, **over):
    rec = extract.extract_document(doc, call_fn=fake(answer(**over)), retries=0)
    return rec["fields"], {d["field"]: d["reason"] for d in rec["dropped"]}


def test_trial_fields_kept(doc):
    f, dropped = run(doc, bridge_or_extension="yes", bridge_or_extension_quote="This is the seventh bridge for these services.",
                     prior_extension_count=[{"count": 6, "quote": "This is the seventh bridge for these services."}],
                     contract_types=[{"stated": "firm-fixed-price", "quote": "on a firm-fixed-price basis"}],
                     is_letter_contract="yes", letter_contract_quote="will issue a letter contract (undefinitized contract action)",
                     funding_sources=[{"name": "One Big Beautiful Bill Act", "quote": "funded with One Big Beautiful Bill Act funds"}],
                     policy_citations=[{"citation": "EO 14275", "cited_for": "regulatory_numbering_or_procedure",
                                        "quote": "Citations reflect the Revolutionary FAR Overhaul under EO 14275."}],
                     urgency_trigger=[{"trigger": "congressional_or_oversight_deadline",
                                       "quote": "the House Committee on Oversight requires delivery by 1 June 2026"}])
    assert f["bridge_or_extension"] == "yes" and f["prior_extension_count"] == 6
    assert [c["stated"] for c in f["contract_types"]] == ["firm-fixed-price"]
    assert f["is_letter_contract"] == "yes" and f["funding_sources"][0]["name"] == "One Big Beautiful Bill Act"
    assert f["policy_citations"][0]["cited_for"] == "regulatory_numbering_or_procedure"
    assert f["urgency_trigger"] == "congressional_or_oversight_deadline"
    assert not [k for k in dropped if k in ("bridge_or_extension", "prior_extension_count", "contract_types",
                                            "is_letter_contract", "funding_sources", "policy_citations", "urgency_trigger")]
    row = extract.flat_row(extract.extract_document(doc, call_fn=fake(answer(
        policy_citations=[{"citation": "EO 14275", "cited_for": "other", "quote": "under EO 14275."}])), retries=0), {})
    assert row["policy_citations"] == "EO 14275 [other]"


def test_trial_fields_dropped(doc):
    f, dropped = run(doc, bridge_or_extension="yes", bridge_or_extension_quote="a bridge that is not in the text",
                     prior_extension_count=[{"count": 2, "quote": "on a firm-fixed-price basis"}],
                     contract_types=[{"stated": "cost-plus-fixed-fee", "quote": "on a firm-fixed-price basis"}],
                     is_letter_contract="yes", letter_contract_quote="on a firm-fixed-price basis",
                     funding_sources=[{"name": "Working Capital Fund", "quote": "Working Capital Fund"}],
                     urgency_trigger=[{"trigger": "bid_protest", "quote": "a protest was filed"}])
    assert f["bridge_or_extension"] is None and f["prior_extension_count"] is None
    assert f["contract_types"] == [] and f["is_letter_contract"] is None and f["funding_sources"] == []
    assert f["urgency_trigger"] is None
    assert dropped["bridge_or_extension"] == "quote not found in document"
    assert dropped["prior_extension_count"] == "no count in its quote"
    assert dropped["contract_types"] == "stated not in its quote"
    assert dropped["is_letter_contract"] == "quote does not say letter contract or undefinitized"
    assert dropped["funding_sources"] == "quote not found in document"
    assert dropped["urgency_trigger"] == "quote not found in document"


def test_trial_no_values(doc):
    f, _ = run(doc, bridge_or_extension="no", bridge_or_extension_quote='""', prior_extension_count=[],
               contract_types=[], is_letter_contract="no", letter_contract_quote="", funding_sources=[],
               policy_citations=[], urgency_trigger=[])
    assert (f["bridge_or_extension"], f["is_letter_contract"], f["prior_extension_count"], f["urgency_trigger"]) == \
        ("no", "no", None, None)
