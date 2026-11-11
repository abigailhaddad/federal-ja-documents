"""The second call (analysis/extras.py): what the checks keep and drop. No model calls."""

import pytest

from federal_ja.analysis import extras, llm
from federal_ja.analysis.text import Document, Page

P1 = ("JUSTIFICATION FOR OTHER THAN FULL AND OPEN COMPETITION. "
      "This J&A will increase the contract ceiling by an estimated $5,000,000 from $31,223,718.74 to "
      "$36,223,718.74. The incumbent contractor, Paragon Systems, Inc., currently performs the work. "
      "Total estimated dollar value and performance period. $782,650.53 (NTE $900K) 12/29/2025 - 1/11/2026. "
      "The estimated value is $19,166.00 each, quantity 2. "
      "Approval is required for actions exceeding $900,000 but not exceeding $20 million.")
P2 = ("CAGE | COMPANY NAME 95270 | SKURKA AEROSPACE INC 81982 | HYDRO-AIRE AEROSPACE CORP "
      "3XMD9 | NEW PHOENIX AEROSPACE INC The use of authority 10 U.S.C. 3204(a)(1) is based on: "
      "12. A listing of sources, if any, indicating an interest in writing in the acquisition are: "
      "CAGE | COMPANY NAME 3XMD9 | NEW PHOENIX AEROSPACE 4KOV2 | IPT "
      "13. The solicitation will include Procurement Note L04, Offers for Part Number Items, and alternate "
      "offer Procurement Note M06, to allow potential new sources to submit their data for evaluation.")


def doc():
    meta = {"title": "J&A", "agency": "DEPT OF DEFENSE", "sub_tier": "DEFENSE LOGISTICS AGENCY",
            "office": "DLA AVIATION", "posted_date": "2026-11-09"}
    return Document("n1", meta, [Page("r1", "JA_Redacted.pdf", 1, "native", P1),
                                 Page("r1", "JA_Redacted.pdf", 2, "transcription", P2)])


def fact(kind, value, quote):
    return {"kind": kind, "value": value, "quote": quote}


CEILING_Q = "increase the contract ceiling by an estimated $5,000,000 from $31,223,718.74 to $36,223,718.74"


def test_ceiling_change_is_kept_with_quotes_and_the_arithmetic_is_checked():
    f, dropped = extras.verify({"facts": [fact("increase_amount", "5000000", CEILING_Q),
                                          fact("prior_ceiling", "31223718.74", CEILING_Q),
                                          fact("new_ceiling", "36223718.74", CEILING_Q)]}, doc())
    assert (f["increase_amount"], f["prior_ceiling"], f["new_ceiling"]) == (5_000_000.0, 31_223_718.74, 36_223_718.74)
    assert f["ceiling_arithmetic_ok"] is True and not dropped
    assert f["new_ceiling_quote_info"]["quote_pages"][0]["page_number"] == 1


def test_ceilings_that_do_not_add_up_are_kept_and_flagged():
    d = Document("n2", {}, [Page("r", "a.pdf", 1, "native",
                                 "This action raises the ceiling by $2,000,000 from $10,000,000 to $13,000,000 in total.")])
    q = "raises the ceiling by $2,000,000 from $10,000,000 to $13,000,000 in total"
    f, _ = extras.verify({"facts": [fact("increase_amount", "2000000", q), fact("prior_ceiling", "10000000", q),
                                    fact("new_ceiling", "13000000", q)]}, d)
    assert f["new_ceiling"] == 13_000_000.0 and f["ceiling_arithmetic_ok"] is False


def test_an_amount_the_document_does_not_print_is_dropped():
    # 5 x the stated figure: the product is in no quote, so it can't be kept
    f, dropped = extras.verify({"facts": [fact("not_to_exceed", "3913252.65",
                                               "Total estimated dollar value and performance period. $782,650.53")]}, doc())
    assert f["not_to_exceed"] is None
    assert dropped[0]["reason"] == "amount not in its quote"


def test_stated_total_is_no_longer_a_kind():
    # extras1 pilot: it picked up prior awards and figures that were really increases
    f, dropped = extras.verify({"facts": [fact("stated_total", "782650.53", "$782,650.53 (NTE $900K) 12/29/2025")]}, doc())
    assert "stated_total" not in f and dropped[0]["reason"] == "unknown fact kind"


def test_quote_must_be_in_the_document():
    f, dropped = extras.verify({"facts": [fact("not_to_exceed", "900000", "with a not to exceed amount of $900,000")]}, doc())
    assert f["not_to_exceed"] is None and dropped[0]["reason"] == "quote not found in the document"


def test_not_to_exceed_beside_an_estimate_is_kept_but_an_approval_threshold_is_not():
    f, dropped = extras.verify({"facts": [fact("not_to_exceed", "900000", "$782,650.53 (NTE $900K) 12/29/2025")]}, doc())
    assert f["not_to_exceed"] == 900_000.0 and not dropped
    f, dropped = extras.verify({"facts": [fact("not_to_exceed", "900000",
                                               "Approval is required for actions exceeding $900,000 but not exceeding $20 million")]}, doc())
    assert f["not_to_exceed"] is None and dropped[0]["reason"] == "an approval threshold, not a value"


def test_unit_price_and_quantity_are_kept_as_stated_and_never_multiplied():
    q = "The estimated value is $19,166.00 each, quantity 2."
    f, dropped = extras.verify({"facts": [fact("unit_price", "19166.00", q), fact("quantity", "2", q)]}, doc())
    assert (f["unit_price"], f["quantity"]) == (19_166.0, 2.0) and not dropped
    assert 38_332.0 not in [v for v in f.values() if isinstance(v, float)]   # never multiplied


def test_quantity_without_a_unit_price_is_dropped():
    q = "The estimated value is $19,166.00 each, quantity 2."
    f, dropped = extras.verify({"facts": [fact("quantity", "2", q)]}, doc())
    assert f["quantity"] is None and dropped[0]["reason"] == "no unit price kept"


def test_incumbent_name_must_be_in_its_quote():
    q = "The incumbent contractor, Paragon Systems, Inc., currently performs the work."
    f, _ = extras.verify({"facts": [fact("incumbent_vendor", "Paragon Systems, Inc.", q)]}, doc())
    assert f["incumbent_vendor"] == "Paragon Systems, Inc."
    f, dropped = extras.verify({"facts": [fact("incumbent_vendor", "Halvik Corp.", q)]}, doc())
    assert f["incumbent_vendor"] is None and dropped[0]["reason"] == "name not in its quote"


def test_incumbent_needs_a_quote_that_says_incumbent():
    d = Document("n3", {}, [Page("r", "a.pdf", 1, "native",
                                 "(b) The name and address of the contractor: Workwear Outfitters LLC, Nashville. "
                                 "Noblis is the only contractor who can perform the work, the market study concluded; "
                                 "the current contractor, Noblis, holds the order.")])
    f, dropped = extras.verify({"facts": [fact("incumbent_vendor", "Workwear Outfitters LLC",
                                               "The name and address of the contractor: Workwear Outfitters LLC")]}, d)
    assert f["incumbent_vendor"] is None
    assert dropped[0]["reason"] == "quote does not show the firm is the incumbent"
    f, _ = extras.verify({"facts": [fact("incumbent_vendor", "Noblis", "the current contractor, Noblis, holds the order")]}, d)
    assert f["incumbent_vendor"] == "Noblis"
    # a past award of the contract counts; "will be awarded to" does not
    d2 = Document("n3b", {}, [Page("r", "a.pdf", 1, "native",
                                   "The original five (5) year contract for this region, SPE8E3-15-D-0024, was awarded to "
                                   "SupplyCore, Inc. in 2015. The bridge will be awarded to Acme Logistics LLC next month.")])
    f, _ = extras.verify({"facts": [fact("incumbent_vendor", "SupplyCore, Inc.",
                                         "contract for this region, SPE8E3-15-D-0024, was awarded to SupplyCore, Inc.")]}, d2)
    assert f["incumbent_vendor"] == "SupplyCore, Inc."
    f, dropped = extras.verify({"facts": [fact("incumbent_vendor", "Acme Logistics LLC",
                                               "will be awarded to Acme Logistics LLC next month")]}, d2)
    assert f["incumbent_vendor"] is None and "incumbent" in dropped[0]["reason"]


def test_interested_firm_needs_a_quote_that_says_so_or_a_cage_table_row():
    d = Document("n4", {}, [Page("r", "a.pdf", 1, "native",
                                 "The American Dental Association (ADA CERP) and the American Psychology Association (APA) "
                                 "are the only organizations that accredit. Five capability packages were received from the "
                                 "following sources: Boeing (St. Louis, MO).")])
    raw = {"facts": [fact("interested_source", "American Psychology Association (APA)",
                          "the American Psychology Association (APA) are the only organizations that accredit"),
                     fact("interested_source", "Boeing", "Five capability packages were received from the following sources: Boeing")]}
    f, dropped = extras.verify(raw, d)
    assert [x["name"] for x in f["interested_sources"]] == ["Boeing"]
    assert dropped[0]["reason"] == "quote does not show the firm expressed interest"


def test_data_rights_cost_and_delay_to_compete():
    d = Document("n5", {}, [Page("r", "a.pdf", 1, "native",
        "Based upon the technical certification on file, technical data is not available and cannot be developed "
        "to permit full and open competition. [ ] AMSC P. The rights to use the data needed to purchase this part "
        "are not owned by the Government. The estimated cost to reverse engineer the Circuit Card Assembly is $6.5M. "
        "The Enterprise will recognize a total cost savings of $7,518,865.60. Installing a new commercial provider "
        "would face 6 to 12 months of permitting and approval delay. Any non approved sources will be sent SAR instructions.")])
    raw = {"facts": [
        fact("data_rights_basis", "data_not_available", "technical data is not available and cannot be developed to permit full and open competition"),
        fact("cost_to_compete", "6500000", "The estimated cost to reverse engineer the Circuit Card Assembly is $6.5M"),
        fact("delay_to_compete", "6 to 12 months", "would face 6 to 12 months of permitting and approval delay"),
        fact("source_approval_path", "stated", "Any non approved sources will be sent SAR instructions")]}
    f, dropped = extras.verify(raw, d)
    assert f["data_rights_basis"] == "data_not_available" and f["cost_to_compete"] == 6_500_000.0
    assert f["delay_to_compete"] == "6 to 12 months" and f["source_approval_path"].startswith("Any non approved") and not dropped
    bad = {"facts": [
        # a line of the form's printed code list is not something the document says about this part
        fact("data_rights_basis", "government_lacks_rights", "AMSC P. The rights to use the data needed to purchase this part are not owned by the Government"),
        fact("data_rights_basis", "they just can't", "technical data is not available and cannot be developed"),
        fact("cost_to_compete", "7518865.60", "The Enterprise will recognize a total cost savings of $7,518,865.60"),
        fact("delay_to_compete", "24 months", "would face 6 to 12 months of permitting and approval delay")]}
    f, dropped = extras.verify(bad, d)
    assert f["data_rights_basis"] is None and f["cost_to_compete"] is None and f["delay_to_compete"] is None
    assert [x["reason"] for x in dropped] == ["quote is a line of a printed checklist", "not one of the listed bases",
                                              "quote does not say what the cost is for", "duration not in its quote"]


def test_rule_fields_read_protest_numbers_and_nsns_without_a_model():
    d = Document("n6", {}, [Page("r", "a.pdf", 1, "native",
        "The protested task-order award is stayed in connection with GAO Protest B-424920 and B-424107.1. "
        "This award has been protested to the Court of Federal Claims, Case Number 25-279C. "
        "BLADE,TURBINE ROTOR | 2840-01-666-1765 | 482. Stator Assembly, Pump | 2915-004499118 | EAQ: 10. "
        "Phone 703-555-0100-2222. FAR Clause 52.217-8 Option to Extend Services may be exercised.")])
    r = extras.rule_fields(d)
    assert r["gao_protests"] == ["B-424107.1", "B-424920"] and r["cofc_cases"] == ["25-279C"]
    assert r["nsns"] == ["2840-01-666-1765", "2915-00-449-9118"] and r["uses_52_217_8"] is True and r["form"] is None
    assert extras.rule_fields(doc())["gao_protests"] == [] and extras.rule_fields(doc())["cofc_cases"] == []


def test_source_lists_keep_every_firm_with_its_cage_code():
    aq = "95270 | SKURKA AEROSPACE INC 81982 | HYDRO-AIRE AEROSPACE CORP 3XMD9 | NEW PHOENIX AEROSPACE INC"
    iq = "CAGE | COMPANY NAME 3XMD9 | NEW PHOENIX AEROSPACE 4KOV2 | IPT"
    raw = {"facts": [fact("approved_source", "95270 | SKURKA AEROSPACE INC", aq),
                     fact("approved_source", "81982 | HYDRO-AIRE AEROSPACE CORP", aq),
                     fact("approved_source", "3XMD9 | NEW PHOENIX AEROSPACE INC", aq),
                     fact("approved_source", "3XMD9 | NEW PHOENIX AEROSPACE INC", aq),      # given twice
                     fact("interested_source", "3XMD9 | NEW PHOENIX AEROSPACE", iq),
                     fact("interested_source", "4KOV2 | IPT", iq),
                     fact("interested_source", "99999 | IPT", iq),                           # CAGE not in the quote
                     fact("interested_source", "S3 AERODEFENSE", iq)]}                       # name not in the quote
    f, dropped = extras.verify(raw, doc())
    assert [(x["cage"], x["name"]) for x in f["approved_sources"]] == [
        ("95270", "SKURKA AEROSPACE INC"), ("81982", "HYDRO-AIRE AEROSPACE CORP"), ("3XMD9", "NEW PHOENIX AEROSPACE INC")]
    assert [(x["cage"], x["name"]) for x in f["interested_sources"]] == [("3XMD9", "NEW PHOENIX AEROSPACE"), ("4KOV2", "IPT")]
    assert f["approved_sources"][0]["quote_on_transcribed_page"] is True
    assert {d["reason"] for d in dropped} == {"CAGE code not in its quote", "name not in its quote"}


def test_barrier_action_is_the_quoted_passage_itself():
    q = "The solicitation will include Procurement Note L04, Offers for Part Number Items"
    f, _ = extras.verify({"facts": [fact("barrier_action", "stated", q)]}, doc())
    assert f["barrier_action"] == q


def test_unknown_kinds_and_second_entries_are_dropped_and_listed():
    raw = {"facts": [fact("vendor", "Acme", CEILING_Q), fact("new_ceiling", "36223718.74", CEILING_Q),
                     fact("new_ceiling", "31223718.74", CEILING_Q), fact("prior_ceiling", "5M", CEILING_Q)]}
    f, dropped = extras.verify(raw, doc())
    assert f["new_ceiling"] == 36_223_718.74
    assert [d["reason"] for d in dropped] == ["unknown fact kind", "a second entry for a single-valued fact; the first is kept",
                                              "value is not a plain dollar amount"]


def test_an_empty_answer_is_a_record_with_nothing_in_it():
    f, dropped = extras.verify({"facts": []}, doc())
    assert f["approved_sources"] == [] and f["interested_sources"] == [] and f["new_ceiling"] is None
    assert f["ceiling_arithmetic_ok"] is None and not dropped


def test_parse_amount_and_split_firm():
    assert extras.parse_amount("782650.53") == 782_650.53 and extras.parse_amount("$5,000,000") == 5_000_000.0
    assert extras.parse_amount("5M") is None and extras.parse_amount("about 5000000") is None
    assert extras.parse_amount("0") is None
    assert extras.split_firm("95270 | SKURKA AEROSPACE INC") == ("95270", "SKURKA AEROSPACE INC")
    assert extras.split_firm("CAGE | COMPANY NAME") == (None, "CAGE | COMPANY NAME")
    assert extras.split_firm("IPT") == (None, "IPT")


def test_one_call_makes_a_record_and_a_table_row_and_a_bad_shape_stores_nothing():
    seen = {}

    def call_fn(prompt, **kw):
        seen.update(kw, prompt=prompt)
        return ({"facts": [fact("new_ceiling", "36223718.74", CEILING_Q)]},
                {"input_tokens": 3000, "output_tokens": 900, "cost_usd": 0.00075, "cache_read_input_tokens": 1300})

    rec = extras.extras_document(doc(), call_fn=call_fn)
    assert seen["tool_name"] == "record_ja_extras" and seen["schema"] is extras.SCHEMA and seen["effort"] == "high"
    cached, per_doc = seen["prompt"]
    assert "NOTICE METADATA" not in cached and "SKURKA AEROSPACE INC" in per_doc and "<<" not in per_doc
    assert rec["extras_version"] == extras.EXTRAS_VERSION and rec["usage"]["calls"] == 1
    assert rec["chars_total"] == rec["chars_sent"] and rec["truncated"] is False
    row = extras.flat_row(rec, {"title": "J&A", "posted_date": "2026-11-09 10:00:00"})
    assert row["new_ceiling_usd"] == 36_223_718.74 and row["new_ceiling_quote_pages"] == "1"
    assert row["approved_sources"] is None and row["approved_sources_count"] == 0 and row["posted_date"] == "2026-11-09"
    with pytest.raises(llm.ModelError):
        extras.extras_document(doc(), call_fn=lambda p, **kw: ({"facts": [{"kind": "x"}]}, {}))


def test_batch_returns_a_record_or_the_error_for_each_notice():
    d1, d2 = doc(), Document("n2", {}, [Page("r", "a.pdf", 1, "native", P1)])

    def batch_fn(prompts, **kw):
        assert set(prompts) == {"n1", "n2"} and kw["tool_name"] == "record_ja_extras"
        return {"n1": ({"facts": []}, {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.0001, "price_mult": 0.5}),
                "n2": llm.ModelError("expired")}

    out = extras.extras_documents_batch([d1, d2], batch_fn=batch_fn)
    assert out["n1"]["usage"]["price_mult"] == 0.5 and isinstance(out["n2"], llm.ModelError)


def test_reverify_reapplies_the_checks_to_the_stored_answer():
    rec = extras.extras_document(doc(), call_fn=lambda p, **kw: (
        {"facts": [fact("new_ceiling", "36223718.74", CEILING_Q)]}, {}))
    rec["fields"] = {}
    assert extras.reverify(rec, doc())["fields"]["new_ceiling"] == 36_223_718.74


def test_the_extraction_prompt_and_version_are_untouched():
    # the point of the second call: adding to it must not make extraction records out of date
    from federal_ja.analysis import extract
    assert extract.EXTRACT_VERSION == "2026-10-21.extract6"
    assert "record_ja_extras" not in extract.PROMPT and extras.EXTRAS_VERSION != extract.EXTRACT_VERSION


# ── the command ────────────────────────────────────────────────────────────

def test_cli_extras_caps_on_the_estimate_stores_records_and_leaves_extractions_alone(tmp_path, monkeypatch):
    import json

    from federal_ja.analysis import cli, data
    from tests.test_batch_extraction import write_tables
    from tests.test_field_extraction import NID, tables

    dd, ids = write_tables(tmp_path, 5)
    sent = []

    def batch_call(prompts, **kw):
        sent.append(sorted(prompts))
        u = {"input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.0001, "price_mult": 0.5}
        return {nid: (llm.ModelError("expired") if nid == ids[0] else ({"facts": []}, u)) for nid in prompts}
    monkeypatch.setattr(llm, "batch_call", batch_call)
    monkeypatch.setattr(llm, "call", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no direct call with --batch")))
    one = extras.estimate_cost(data.build_documents(*tables())[NID])
    out = tmp_path / "out"
    args = ["extras", "--batch", "--out", str(out), "--data-dir", str(dd), "--max-usd", str(one * 3.5)]
    assert cli.main(args + ["--dry-run"]) == 0 and not sent and not (out / "extras.jsonl").exists()
    assert cli.main(args) == 1                       # 3 of 5 fit under the cap; one of them failed
    assert len(sent) == 1 and len(sent[0]) == 3
    recs = [json.loads(ln) for ln in (out / "extras.jsonl").read_text().splitlines()]
    assert len(recs) == 2 and all(r["dataset_revision"] == "abc" and r["extras_version"] == extras.EXTRAS_VERSION
                                  for r in recs)
    assert (out / "extras_failures.jsonl").read_text().count("expired") == 1
    assert (out / "extras.csv").read_text().count("\n") == 3          # header + 2 rows
    assert not (out / "extractions.jsonl").exists()                   # the extraction records are not touched
    assert cli.main(args) == 1                       # next run: the failed one and the two not sent
    assert len(sent[1]) == 3 and ids[0] in sent[1] and not set(sent[1]) & {r["notice_id"] for r in recs}


def test_cli_extras_direct_mode_and_notice_file(tmp_path, monkeypatch):
    import json

    from federal_ja.analysis import cli
    from tests.test_batch_extraction import write_tables

    dd, ids = write_tables(tmp_path, 4)
    calls = []

    def call(prompt, **kw):
        calls.append(kw["tool_name"])
        return {"facts": []}, {"input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.0002}
    monkeypatch.setattr(llm, "call", call)
    want = tmp_path / "ids.txt"
    want.write_text(ids[1] + "\n" + ids[3] + "\n")
    out = tmp_path / "out"
    assert cli.main(["extras", "--out", str(out), "--data-dir", str(dd), "--notice-file", str(want),
                     "--workers", "1", "--max-usd", "1"]) == 0
    recs = [json.loads(ln) for ln in (out / "extras.jsonl").read_text().splitlines()]
    assert sorted(r["notice_id"] for r in recs) == sorted([ids[1], ids[3]]) and calls == ["record_ja_extras"] * 2


def test_a_form_line_whose_box_is_empty_is_not_the_documents_statement():
    line = "Based upon the technical certification on file, technical data is not available and cannot be developed"
    ticked = Document("t1", {}, [Page("r", "a.pdf", 3, "transcription",
                                      f"11. Additional facts supporting other than full and open competition are: [X] {line} to permit full and open competition. [ ] Other: ____")])
    empty = Document("t2", {}, [Page("r", "a.pdf", 3, "transcription",
                                     f"11. Additional facts supporting other than full and open competition are: [ ] {line} to permit full and open competition. [X] Other: see below")])
    for q in (line, "technical data is not available and cannot be developed"):     # from the line's start, or a few words in
        raw = {"facts": [fact("data_rights_basis", "data_not_available", q)]}
        f, dropped = extras.verify(raw, ticked)
        assert f["data_rights_basis"] == "data_not_available" and not dropped
        f, dropped = extras.verify(raw, empty)
        assert f["data_rights_basis"] is None and dropped[0]["reason"] == "the form's box beside this line is not ticked"
    # ordinary prose with no box anywhere near is untouched
    assert extras.beside_an_empty_box("increase the contract ceiling by an estimated $5,000,000", doc()) is False


def test_an_increase_and_a_ceiling_need_a_quote_that_says_which_figure_is_which():
    d = Document("n7", {}, [Page("r", "a.pdf", 1, "native",
        "The government estimate is $1,801,453.52 for the period. Amount: $62,871.00. "
        "$534,421,295 $4,344,243 $4,570,539 $556,633,814 $562,158,447 $5,524,633. "
        "FormAlloy is the only source qualified to extend their marketing efforts to capture customers. "
        "This action will extend the contract with Acme Corp for six months.")])
    raw = {"facts": [fact("increase_amount", "1801453.52", "The government estimate is $1,801,453.52 for the period"),
                     fact("prior_ceiling", "562158447", "$534,421,295 $4,344,243 $4,570,539 $556,633,814 $562,158,447 $5,524,633"),
                     fact("incumbent_vendor", "FormAlloy", "FormAlloy is the only source qualified to extend their marketing efforts")]}
    f, dropped = extras.verify(raw, d)
    assert f["increase_amount"] is None and f["prior_ceiling"] is None and f["incumbent_vendor"] is None
    assert [x["reason"] for x in dropped] == ["quote does not show the figure is an increase",
                                              "quote does not show the figure is a ceiling or total",
                                              "quote does not show the firm is the incumbent"]
    f, _ = extras.verify({"facts": [fact("incumbent_vendor", "Acme Corp", "This action will extend the contract with Acme Corp for six months")]}, d)
    assert f["incumbent_vendor"] == "Acme Corp"
    # "from X to Y" with cents in the figures is still a ceiling change
    d2 = Document("n8", {}, [Page("r", "a.pdf", 1, "native", "Increase the ceiling by $1,000,000.00 from $49,000,000.00 to $50,000,000.00 for FY26.")])
    q = "by $1,000,000.00 from $49,000,000.00 to $50,000,000.00"
    f, dropped = extras.verify({"facts": [fact("increase_amount", "1000000", q), fact("prior_ceiling", "49000000", q),
                                          fact("new_ceiling", "50000000", q)]}, d2)
    assert (f["prior_ceiling"], f["new_ceiling"], f["ceiling_arithmetic_ok"]) == (49_000_000.0, 50_000_000.0, True) and not dropped


def test_an_extension_or_bridge_value_is_not_a_ceiling_increase():
    # xchecks5: 93 such figures were kept as increases on 2026-11-10
    d = Document("n11", {}, [Page("r", "a.pdf", 1, "native",
        "The total estimated value of this six-month extension is $450,000. "
        "This modification increases the ceiling by $450,000.")])
    f, dropped = extras.verify({"facts": [fact("increase_amount", "450000",
                                               "The total estimated value of this six-month extension is $450,000")]}, d)
    assert f["increase_amount"] is None and dropped[0]["reason"] == "quote does not show the figure is an increase"
    f, _ = extras.verify({"facts": [fact("increase_amount", "450000", "This modification increases the ceiling by $450,000")]}, d)
    assert f["increase_amount"] == 450_000.0


def test_a_unit_price_is_the_price_of_one_item_not_a_rate():
    d = Document("n12", {}, [Page("r", "a.pdf", 1, "native",
        "The estimated cost of this lease extension is $25.90 per rentable square feet per year. "
        "LUTATHERA 16 $42,496.79 $679,948.64. PSO Basic / TAS 81,693.75 $67.13 $5,484,101.44. "
        "Twelve (12) licenses at $151,133.50 per license.")])
    def up(v, q, qty=None):
        facts = [fact("unit_price", v, q)] + ([fact("quantity", qty, q)] if qty else [])
        return extras.verify({"facts": facts}, d)[0]["unit_price"]
    assert up("25.90", "this lease extension is $25.90 per rentable square feet per year") is None
    assert up("42496.79", "LUTATHERA 16 $42,496.79 $679,948.64", "16") == 42_496.79      # 16 x price = line total
    assert up("42496.79", "LUTATHERA 16 $42,496.79 $679,948.64") is None                  # no quantity: no way to tell
    assert up("67.13", "PSO Basic / TAS 81,693.75 $67.13 $5,484,101.44", "81693.75") is None   # hours, not items
    assert up("151133.50", "Twelve (12) licenses at $151,133.50 per license") == 151_133.5


def test_more_ways_a_quote_shows_a_past_contract_but_not_a_proposed_one():
    d = Document("n13", {}, [Page("r", "a.pdf", 1, "native",
        "The baseline task order was won by TekSynap via robust competition. "
        "Procurement history shows that International Television Corp. has been a past provider for this item. "
        "The contract will be awarded to Acme Logistics LLC, which has been identified as the only source.")])
    keep = lambda v, q: extras.verify({"facts": [fact("incumbent_vendor", v, q)]}, d)[0]["incumbent_vendor"]
    assert keep("TekSynap", "The baseline task order was won by TekSynap via robust competition") == "TekSynap"
    assert keep("International Television Corp.",
                "International Television Corp. has been a past provider for this item") == "International Television Corp."
    assert keep("Acme Logistics LLC",
                "The contract will be awarded to Acme Logistics LLC, which has been identified as the only source") is None


def test_an_interested_firm_can_be_a_row_under_an_interest_heading():
    d = Document("n14", {}, [Page("r", "a.pdf", 1, "native",
        "10. Interested Sources: Seven small businesses responded to the Sources Sought notice. "
        "a. Mary Helen Parkey Middlesboro, KY b. Premier Pump LLC Buford, GA. "
        "11. Market research: The following potential contractors were contacted: 1. Doculogix; 2. M8. "
        "12. A listing of the sources, if any, that expressed, in writing, an interest in the acquisition: "
        "Trane U.S. Inc is the only capable contractor. "
        "13. The following contractors are expected to express an interest: Medline Industries, LP.")])
    raw = {"facts": [fact("interested_source", "Premier Pump LLC", "b. Premier Pump LLC Buford, GA"),
                     fact("interested_source", "Doculogix", "1. Doculogix; 2. M8"),
                     fact("interested_source", "Trane U.S. Inc", "Trane U.S. Inc is the only capable contractor"),
                     fact("interested_source", "Medline Industries, LP",
                          "The following contractors are expected to express an interest: Medline Industries, LP")]}
    f, dropped = extras.verify(raw, d)
    assert [x["name"] for x in f["interested_sources"]] == ["Premier Pump LLC"]
    assert [x["value"] for x in dropped] == ["Doculogix", "Trane U.S. Inc", "Medline Industries, LP"]


def test_gao_numbers_are_split_into_protests_and_cited_decisions():
    d = Document("n15", {}, [Page("r", "a.pdf", 1, "native",
        "Performance is stayed while the agency completes corrective action in connection with GAO Protest B-424920. "
        "See Major Contracting Services, Inc., B-401472, Sep. 14, 2009, 2009 CPD ¶ 170 at 6. "
        "Standardization was supported under GAO ruling B-407223.2. Due to ongoing protests B-424143.2 of the award.")])
    r = extras.rule_fields(d)
    assert r["gao_protests"] == ["B-424143.2", "B-424920"] and r["gao_cited"] == ["B-401472", "B-407223.2"]
    assert r["gao_unclear"] == []


def test_same_firm_matches_suffixes_and_parents_but_not_generic_words():
    sf = extras.same_firm
    assert sf("BHPE LLC", ["BHPE"]) and sf("Raytheon Company", ["RAYTHEON CO."])
    assert sf("BAE Systems San Diego Ship Repair Inc.", ["BAE Systems"])
    assert sf("Boeing", ["Boeing Defense, Space, and Security"])
    assert sf("Johnson & Johnson", ["JOHNSON AND JOHNSON"])
    assert not sf("General Dynamics Information Technology", ["General Electric Company"])
    assert not sf("General", ["General Dynamics"])                 # a generic word never matches as a prefix
    assert not sf("Lockheed Martin", ["Northrop Grumman"]) and not sf("", ["Acme"])


def test_other_interested_firms_leave_out_the_awardee():
    d = Document("n16", {}, [Page("r", "a.pdf", 1, "native",
        "10. Interested Sources: The following firms responded to the sources sought: "
        "a. Midmark Corporation, Versailles, OH b. Acme Medical LLC, Dayton, OH.")])
    raw = {"facts": [fact("interested_source", "Midmark Corporation", "a. Midmark Corporation, Versailles, OH"),
                     fact("interested_source", "Acme Medical LLC", "b. Acme Medical LLC, Dayton, OH")]}
    rec = extras._record(d, raw, {}, "", False, "m", None)
    assert extras.flat_row(rec, {}, ["MIDMARK CORP"])["other_interested_sources"] == "Acme Medical LLC"
    assert extras.flat_row(rec, {}, ["Midmark", "Acme Medical"])["other_interested_sources"] == "none"
    assert extras.flat_row(rec, {})["other_interested_sources"] is None          # no awardee name: can't tell
    assert extras.flat_row(rec, {}, ["Midmark"])["interested_sources_confirmed"] == \
        "Midmark Corporation; Acme Medical LLC"



def test_same_firm_handles_parentheticals_and_acronyms():
    sf = extras.same_firm
    aw = ["Hindustan Aeronautics Ltd. (HAL)", "MTU Berlin-Brandenburg GmbH", "Tusas Engine Industries, Inc. (TEI)"]
    assert sf("HAL", aw) and sf("TEI", aw) and sf("MTU", aw)                       # fc54adb6
    assert sf("California Waste Recovery, LLC (Cal-Waste)", ["California Waste Recovery Systems, LLC"])  # 489a5d08
    assert sf("GDIT", ["General Dynamics Information Technology, Inc."])
    assert not sf("We Haul It All LLC", ["California Waste Recovery Systems, LLC"])
    assert not sf("Rolls-Royce", aw) and not sf("General", ["General Dynamics"]) and not sf("GE", ["General Electric"])


@pytest.mark.parametrize("quote,want", [
    ("The Government does not own the data or the rights to the data needed to purchase this part",
     ["government_does_not_own_data", "government_lacks_rights"]),
    ("The rights to use the data needed to purchase/repair this part from additional source(s) are not owned by the "
     "Government and cannot be purchased", ["government_lacks_rights"]),
    ("The data is owned by Rockwell Collins Inc and the Government will not initiate", []),   # no proprietary reason
    ("Astronics owns all technical data rights for this component.", []),
    ("Since the Navy does not possess sufficient rights to data such as drawings", ["government_lacks_rights"]),
    ("technical data is not available and cannot be developed to permit full and open competition", ["data_not_available"]),
    ("NGSC presently maintains sole authority over the hardware technical data package (TDP)", []),
    ("L3 is unwilling to sell a production level Technical Data Package", []),   # the owner's position, not rights
    ("The Government does not have the pertinent Technical Data Package (TDP) or specifications", ["data_not_available"]),
    ("No other vendor has current data access as this data cannot be archived for secondary use", []),
])
def test_technical_data_reasons_come_from_the_quotes_own_words(quote, want):
    assert sorted(extras.data_rights_reasons(quote)) == sorted(want)


def test_a_not_to_exceed_quoted_from_mid_sentence_is_read_with_the_start_of_its_sentence():
    # the model quoted "will not exceed ..." but the sentence caps only the special projects
    text = ("Five task orders are covered. The sum of all special projects under these orders "
            "will not exceed $6,615,821.77 in total for all actions executed under this justification.")
    d = Document("n1", {"title": "J&A"}, [Page("r1", "JA.pdf", 1, "native", text)])
    f, dropped = extras.verify({"facts": [fact("not_to_exceed", "6615821.77",
                                               "will not exceed $6,615,821.77 in total for all actions executed under this justification.")]}, d)
    assert f["not_to_exceed"] is None and "part of the action" in dropped[0]["reason"]
    whole = text.replace("The sum of all special projects under these orders", "The total value")
    d = Document("n1", {"title": "J&A"}, [Page("r1", "JA.pdf", 1, "native", whole)])
    f, _ = extras.verify({"facts": [fact("not_to_exceed", "6615821.77", "will not exceed $6,615,821.77 in total")]}, d)
    assert f["not_to_exceed"] == 6615821.77


@pytest.mark.parametrize("quote, want", [
    # round-4 check (2026-11-11): not owning data *rights* is lacking rights
    ("The Government does not own the necessary intellectual property data rights to the pertinent technical data",
     ["government_lacks_rights"]),
    ("The Government does not own the data or the rights to the data needed", ["government_does_not_own_data", "government_lacks_rights"]),
    ("The data is proprietary, and the contractor is unwilling to sell it", []),
])
def test_round_four_data_rights_phrasings(quote, want):
    assert extras.data_rights_reasons(quote) == want


def test_stored_record_gives_the_same_published_columns():
    # The daily rebuild reads the stored (trimmed) record, which keeps the delay and
    # cost quotes but not the model's own values. Until 2026-11-11 the rebuild blanked
    # every delay and cost quote because it also required those values.
    info = {"quote_match": "exact", "quote_pages": [{"resource_id": "r1", "page_number": 3, "text_source": "native"}],
            "quote_on_transcribed_page": False}
    full = {"notice_id": "n1", "extras_version": "v", "checks_version": "c",
            "fields": {"delay_to_compete": "6 to 12 months", "delay_to_compete_quote": "This adds 6 to 12 months of delay.",
                       "delay_to_compete_quote_info": info,
                       "cost_to_compete": 250000, "cost_to_compete_quote": "Recompeting would cost $250,000.",
                       "cost_to_compete_quote_info": info},
            "rules": {}, "raw": {"facts": []}}
    stored = extras.public_record(full)
    assert "delay_to_compete" not in stored["fields"]
    a, b = extras.published_columns(full), extras.published_columns(stored)
    assert b["delay_to_compete_quote"] == "This adds 6 to 12 months of delay."
    assert b["cost_to_compete_quote"] == "Recompeting would cost $250,000."
    assert a == b


def test_interested_contractor_entries_exact_names_only():
    lst = "60240 | GENTEX CORP; 4GPC1 | S3 AeroDefense LLC; BAE Systems Technology Solutions & Services Inc."
    # same firm once case, punctuation and the legal suffix are removed
    assert extras.interested_contractor_entries(lst, ["Gentex Corporation"]) == "60240 | GENTEX CORP"
    # a parent or sibling name is not the contractor
    assert extras.interested_contractor_entries(lst, ["BAE Systems Inc."]) is None
    # any of several proposed contractors; blanks ignored
    assert extras.interested_contractor_entries(lst, ["", "S3 AERODEFENSE, L.L.C."]) == "4GPC1 | S3 AeroDefense LLC"
    assert extras.interested_contractor_entries(None, ["Gentex"]) is None
