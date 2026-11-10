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
    # 5 x the annual figure: the product is in no quote, so it can't be kept
    f, dropped = extras.verify({"facts": [fact("stated_total", "3913252.65",
                                               "Total estimated dollar value and performance period. $782,650.53")]}, doc())
    assert f["stated_total"] is None
    assert dropped[0]["reason"] == "amount not in its quote"


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
    assert f["stated_total"] is None           # 38,332 is nowhere in the record


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
