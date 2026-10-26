"""Which Special Notices look like an actual J&A (analysis/ja_check.py), and
that extraction and the published table leave the others out. The cases are
shaped on notices read by hand on 2026-10-26. Synthetic text, no network."""

import json

import pandas as pd
import pytest

from federal_ja.analysis import cli, data, ja_check, table
from federal_ja.analysis.text import Document, Page
from tests.test_field_extraction import NID, PAGE1, PAGE2, answer, fake, tables

NOI = ("NOTICE OF INTENT TO AWARD A SOLE SOURCE CONTRACT THIS IS NOT A REQUEST FOR COMPETITIVE PROPOSALS. "
       "The agency intends to award a contract to Acme under the authority of FAR 13.106-1(b)(2) "
       "(only one source reasonably available). ") * 3


def doc(*atts):
    """atts: (filename, text) per attachment, one page each."""
    return Document("n", {}, [Page(f"r{i}", fn, 1, "native", text) for i, (fn, text) in enumerate(atts)])


@pytest.mark.parametrize("atts,why", [
    ([("Notice of Intent.pdf", NOI), ("A05_J-A for under SAT.pdf", "United States Department of Agriculture")],
     "filename"),
    ([("SSJ_CO Signed - Redacted.pdf", "")], "filename"),                       # "_" next to the acronym
    ([("A13 - JOFOC - Sole Source - Signed.pdf", "")], "filename"),
    ([("Justification and Approval- Post.pdf", "Please wait... If this message is not eventually replaced")],
     "filename"),                                                               # unreadable PDF form
    ([("DF Decade box.pdf", "CONTROLLED UNCLASSIFIED INFORMATION DETERMINATION & FINDINGS Authority to "
                            "Solicit from a Single Source")], "opening"),
    ([("Restricting Competition Document_Redacted.pdf", "Control No.: JA-26-Nat-0466")], "filename"),
    ([("memo.pdf", "Control No: 2025-0037 Memorandum for Record - Limiting Competition at or below the SAT")],
     "opening"),
    ([("Determination of sole source.pdf", "Determination that Only One Source is Reasonably Available "
                                           "(Including Brand Name) - FAR Part 13 Acquisitions")], "opening"),
    ([("STATEMENT OF NEED POSTING.pdf", "STATEMENT OF NEED Bow Thruster Overhaul Parts " * 20 +
       "END OF STATEMENT OF NEED SINGLE SOURCE DETERMINATION USING SIMPLIFIED ACQUISITION PROCEDURES")],
     "heading"),                                                                # stapled behind a notice
])
def test_looks_like_ja(atts, why):
    assert ja_check.ja_evidence(doc(*atts)) == why


@pytest.mark.parametrize("atts", [
    [("Notice of Intent to Sole Source.pdf", NOI)],
    [("NOTICE OF INTENT TO AWARD SOLE SOURCE-FY26 Auto Repair.pdf", NOI)],        # quotes "only one source ..."
    [("Fiber To Desk NOCA - 22 Oct 26.pdf", "Notice of Contract Action (NOCA) Title: Fiber To Desk "
                                           "Original Set Aside: OTHER THAN FULL AND OPEN COMPETITION")],
    [("Performance Work Statement.pdf", "PERFORMANCE WORK STATEMENT " + "x " * 250 + "submit written "
                                        "justification for replacement")],    # in passing, past the opening
    [("36C25926Q0724.docx", "GENERAL INFORMATION DESCRIPTION In accordance with FAR 13.106-1(b), this Notice "
                            "of Intent is to inform all parties")],
    [("NOI.pdf", "")],                                                          # no text, no J&A filename
])
def test_not_ja(atts):
    assert ja_check.ja_evidence(doc(*atts)) is None


def test_only_special_notices_are_checked():
    notices = pd.DataFrame([{"notice_id": "j", "type": "Justification"},
                            {"notice_id": "s", "type": "Special Notice"},
                            {"notice_id": "k", "type": " Special Notice "}])
    noi = doc(("Notice of Intent.pdf", NOI))
    ja = doc(("J&A.pdf", PAGE1))
    assert ja_check.not_ja_special_notices(notices, {"j": noi, "s": noi, "k": ja}) == {"s"}
    assert ja_check.not_ja_special_notices(notices.drop(columns="type"), {"s": noi}) == set()


# ── the table ──────────────────────────────────────────────────────────────

def record(nid):
    from federal_ja.analysis import extract
    d = data.build_documents(*tables())[NID]
    d.notice_id = nid
    return extract.extract_document(d, call_fn=fake(answer()))


def test_build_table_leaves_excluded_out(tmp_path):
    (tmp_path / "extractions.jsonl").write_text(
        "".join(json.dumps(record(n)) + "\n" for n in ("a", "b")))
    (tmp_path / "skipped.jsonl").write_text(json.dumps({"notice_id": "c", "reason": "no_text"}) + "\n")
    assert table.notice_ids(tmp_path) == {"a", "b", "c"}
    df = table.build_table(tmp_path, {}, exclude={"b", "c"})
    assert df.notice_id.tolist() == ["a"]


def test_shrink_guard_counts_both_tables_together():
    table.check_not_shrinking(90 + 10, 100)
    with pytest.raises(table.ShrinkError):
        table.check_not_shrinking(89 + 10, 100)


def test_model_no_rows_move_to_the_excluded_table(tmp_path):
    no = record("n")
    no["fields"]["is_ja"] = "no"
    unclear = record("u")
    unclear["fields"]["is_ja"] = "unclear"
    (tmp_path / "extractions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in (record("y"), no, unclear)))
    assert table.model_says_not_ja(tmp_path) == {"n"}
    reasons = {"n": table.REASON_MODEL_NO}
    assert sorted(table.build_table(tmp_path, {}, exclude=set(reasons)).notice_id) == ["u", "y"]
    ex = table.build_excluded(tmp_path, {}, reasons)
    assert ex.notice_id.tolist() == ["n"] and ex.exclude_reason[0] == "model_says_not_ja"
    assert ex.status[0] == "extracted" and json.loads(ex.record_json[0])["fields"]["is_ja"] == "no"


def test_excluded_rows_are_seeded_back_so_they_are_not_extracted_again(tmp_path, monkeypatch):
    no = record("n")
    no["fields"]["is_ja"] = "no"
    main = pd.DataFrame([{"notice_id": "y", "status": "extracted", "record_json": json.dumps(record("y"))}])
    excl = pd.DataFrame([{"notice_id": "n", "status": "extracted", "record_json": json.dumps(no)}])
    monkeypatch.setattr(table, "_download_published",
                        lambda revision=None, path=table.TABLE_PATH: main if path == table.TABLE_PATH else excl)
    assert table.seed_from_hf(tmp_path)["extracted_rows"] == 2
    assert table.notice_ids(tmp_path) == {"y", "n"}


# ── extract and extract-table ──────────────────────────────────────────────

def write_tables(tmp_path):
    ja, noi, jtype = "a" * 32, "b" * 32, "c" * 32
    notices = pd.DataFrame([
        {"notice_id": nid, "sam_link": f"https://sam.gov/opp/{nid}/view", "title": "Notice of Intent to Sole Source",
         "agency": "DEPT OF EXAMPLES", "sub_tier": None, "office": None, "posted_date": "2026-03-01 10:00:00",
         "solicitation_number": None, "award_number": None, "type": t}
        for nid, t in ((ja, "Special Notice"), (noi, "Special Notice"), (jtype, "Justification"))])
    atts = pd.DataFrame([
        {"notice_id": ja, "resource_id": "r1", "attachment_order": 1, "filename": "J&A signed.pdf",
         "download_status": "ok", "file_kind": "pdf", "text": None},
        {"notice_id": noi, "resource_id": "r2", "attachment_order": 1, "filename": "Notice of Intent.pdf",
         "download_status": "ok", "file_kind": "pdf", "text": None},
        {"notice_id": jtype, "resource_id": "r3", "attachment_order": 1, "filename": "Notice of Intent.pdf",
         "download_status": "ok", "file_kind": "pdf", "text": None}])
    pages = pd.DataFrame([
        {"notice_id": ja, "resource_id": "r1", "page_number": 1, "text": PAGE1, "text_source": "native"},
        {"notice_id": ja, "resource_id": "r1", "page_number": 2, "text": PAGE2, "text_source": "native"},
        {"notice_id": noi, "resource_id": "r2", "page_number": 1, "text": NOI, "text_source": "native"},
        {"notice_id": jtype, "resource_id": "r3", "page_number": 1, "text": NOI, "text_source": "native"}])
    dd = tmp_path / "tables"
    dd.mkdir()
    for name, df in zip(data.TABLES, (notices, atts, pages)):
        df.to_parquet(dd / f"{name}.parquet")
    (dd / "revision.json").write_text(json.dumps({"sha": "abc"}))
    return dd, ja, noi, jtype


def test_extract_skips_special_notices_that_are_not_ja(tmp_path, monkeypatch):
    dd, ja, noi, jtype = write_tables(tmp_path)
    from federal_ja.analysis import extract
    sent = []
    real = extract.extract_document

    def fake_extract(doc, *, model, retries, effort):
        sent.append(doc.notice_id)
        return real(doc, model=model, retries=retries, effort=effort, call_fn=fake(answer()))
    monkeypatch.setattr(extract, "extract_document", fake_extract)
    out = tmp_path / "out"
    assert cli.main(["extract", "--out", str(out), "--data-dir", str(dd), "--max-usd", "1"]) == 0
    # the Justification-type notice is extracted whatever its text says
    assert sorted(sent) == sorted([ja, jtype])


def test_extract_table_drops_published_non_ja_rows(tmp_path, monkeypatch):
    dd, ja, noi, jtype = write_tables(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    # as if seeded from a table published before the check existed
    (out / "extractions.jsonl").write_text("".join(json.dumps(record(n)) + "\n" for n in (ja, noi, jtype)))
    monkeypatch.setattr(data, "download_tables", lambda d, revision=None: {"sha": "abc"})
    t = tmp_path / "t.parquet"
    assert cli.main(["extract-table", "--out", str(out), "--data-dir", str(dd), "--table", str(t)]) == 0
    assert sorted(pd.read_parquet(t).notice_id) == sorted([ja, jtype])
    ex = pd.read_parquet(tmp_path / "t_excluded.parquet")
    assert ex.notice_id.tolist() == [noi] and ex.exclude_reason.tolist() == ["special_notice_not_ja"]


def test_unreadable_ja_attachment():
    assert ja_check.unreadable_ja_attachment(doc(("NOTICE OF INTENT.pdf", NOI), ("Redacted JA.pdf",
        "Please wait... \n  \nIf this message is not eventually replaced by the proper contents")))
    assert ja_check.unreadable_ja_attachment(doc(("SSJ_signed.pdf", "")))
    assert not ja_check.unreadable_ja_attachment(doc(("J&A.pdf", PAGE1)))
    assert not ja_check.unreadable_ja_attachment(doc(("form.pdf", "Please wait... If this message is not "
                                                                  "eventually replaced")))
