"""XFA PDF forms (collect/xfa.py): a PDF whose page is only a "Please wait..."
placeholder, with the form in XML inside. Synthetic PDFs, no network."""

import argparse
import json

import pytest
from pypdf import PdfWriter
from pypdf.generic import (ArrayObject, DecodedStreamObject, DictionaryObject, NameObject,
                           TextStringObject)

from federal_ja.collect import cli as collect_cli
from federal_ja.collect import extract as ex
from federal_ja.collect import pipeline, xfa
from federal_ja.collect.store import Store

PLACEHOLDER = ("Please wait... If this message is not eventually replaced by the proper contents of the "
               "document, your PDF viewer may not be able to display this type of document.")

TEMPLATE = b"""<template xmlns="http://www.xfa.org/schema/xfa-template/3.3/">
 <subform name="form1">
  <draw><value><text>Single Source Justification (Simplified Acquisition)</text></value></draw>
  <subform name="SectionA">
   <draw><value><text>Section A: General Contract Information</text></value></draw>
   <field name="Contract_Activity"><caption><value><text>Contracting Activity:</text></value></caption></field>
   <field name="Project_Name"><caption><value><text>Project Name</text></value></caption></field>
   <field name="Estimated_Cost"><caption><value><text>Estimated Cost:</text></value></caption></field>
  </subform>
  <subform name="SectionC">
   <draw><value><text>Section C: Justification</text></value></draw>
   <field name="Text"/>
  </subform>
  <subform name="Signer">
   <field name="Name"><caption><value><text>Name:</text></value></caption></field>
  </subform>
 </subform>
</template>"""

DATASETS = b"""<xfa:datasets xmlns:xfa="http://www.xfa.org/schema/xfa-data/1.0/"><xfa:data><form1>
 <SectionA><Contract_Activity>99 CONS/PKB</Contract_Activity><Project_Name>BAK-14 Assembly</Project_Name>
  <Estimated_Cost/></SectionA>
 <SectionC><Text><body xmlns="http://www.w3.org/1999/xhtml"><p>Only the OEM, ESCO, can supply the parts.</p>
  <p>Market research found no other source.</p></body></Text></SectionC>
 <Signer><Name>Jane Doe</Name></Signer><Signer><Name>John Roe</Name></Signer>
</form1></xfa:data></xfa:datasets>"""


def make_pdf(path, page_text=PLACEHOLDER, with_xfa=True):
    w = PdfWriter()
    page = w.add_blank_page(612, 792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)})})
    content = DecodedStreamObject()
    content.set_data(f"BT /F1 9 Tf 20 700 Td ({page_text}) Tj ET".encode())
    page[NameObject("/Contents")] = w._add_object(content)
    if with_xfa:
        parts = ArrayObject()
        for name, data in (("template", TEMPLATE), ("datasets", DATASETS)):
            s = DecodedStreamObject()
            s.set_data(data)
            parts += [TextStringObject(name), w._add_object(s)]
        w._root_object[NameObject("/AcroForm")] = w._add_object(
            DictionaryObject({NameObject("/Fields"): ArrayObject(), NameObject("/XFA"): parts}))
    with open(path, "wb") as f:
        w.write(f)
    return str(path)


def test_render_puts_values_after_their_captions():
    t = xfa.render(TEMPLATE, DATASETS)
    assert t.splitlines() == [
        "Single Source Justification (Simplified Acquisition)",
        "Section A: General Contract Information",
        "Contracting Activity: 99 CONS/PKB",                 # no doubled colon
        "Project Name: BAK-14 Assembly",
        "Section C: Justification",
        "Only the OEM, ESCO, can supply the parts.",
        "Market research found no other source.",
        "Name: Jane Doe",                                    # a repeated subform: every instance
        "Name: John Roe",
    ]


def test_extract_pdf_reads_the_form_not_the_placeholder(tmp_path):
    r = ex.extract_file(make_pdf(tmp_path / "f.pdf"), "SSJ.pdf")
    assert r.xfa_form and r.text_source == "pdf_xfa" and r.page_count == 1
    assert "Only the OEM, ESCO, can supply the parts." in r.page_texts[0]
    assert "Please wait" not in r.page_texts[0]
    assert r.needs_transcription is False and r.textless_pages == []


def test_placeholder_without_xfa_is_left_alone(tmp_path):
    r = ex.extract_file(make_pdf(tmp_path / "f.pdf", with_xfa=False), "x.pdf")
    assert not r.xfa_form and xfa.is_placeholder(r.page_texts[0])


def test_ordinary_pdf_is_not_touched(tmp_path):
    r = ex.extract_file(make_pdf(tmp_path / "f.pdf", page_text="JUSTIFICATION FOR SOLE SOURCE " * 20), "x.pdf")
    assert not r.xfa_form and r.text_source == "pdf_text"


def test_short_form_is_never_sent_to_transcription(tmp_path):
    store = Store(str(tmp_path))
    a = {"notice_id": "n", "resource_id": "r", "download_status": "ok", "file_kind": "pdf", "file_path": "x"}
    r = ex.ExtractResult(file_kind="pdf", page_texts=["Name: Jane Doe"], page_count=1, xfa_form=True)
    pipeline.apply_extraction(a, r, store)
    (row,) = store.pages[("n", "r")]
    assert row["text_source"] == "xfa_form" and row["needs_transcription"] is False
    assert a["text_source"] == "pdf_xfa"
    assert pipeline.pages_to_transcribe(store, a) == []
    assert pipeline.recheck_page_rule(store) == {"pages": 0, "docs": 0}


def test_xfa_forms_command_reads_stored_placeholders(tmp_path):
    store = Store(str(tmp_path))
    rel = "files/n/r.pdf"
    import os
    make_pdf_path = store.path(rel)
    os.makedirs(os.path.dirname(make_pdf_path), exist_ok=True)
    make_pdf(make_pdf_path)
    a = {"notice_id": "n", "resource_id": "r", "download_status": "ok", "file_kind": "pdf", "file_path": rel,
         "filename": "SSJ.pdf"}
    store.attachments[("n", "r")] = a
    # as stored before xfa.py: the placeholder as page 1's native text
    pipeline.set_pdf_pages(store, a, [PLACEHOLDER])
    assert pipeline.xfa_placeholders(store) == [a]
    out = collect_cli.cmd_xfa_forms(argparse.Namespace(), store)
    assert out == {"placeholders": 1, "read": 1, "still_placeholder": 0, "fetch_failed": []}
    assert "Market research found no other source." in store.pages[("n", "r")][0]["text"]
    assert pipeline.xfa_placeholders(store) == []                 # the daily call is a no-op from now on
    assert os.path.exists(make_pdf_path)                           # a local file is not deleted


def test_changed_text_is_extracted_again(tmp_path, monkeypatch):
    """A record whose document text changed (an XFA form read after the
    placeholder was extracted) no longer counts as done."""
    import pandas as pd
    from federal_ja.analysis import cli, data, extract
    from tests.test_field_extraction import NID, answer, fake, tables
    n, a, p = tables()
    dd = tmp_path / "tables"
    dd.mkdir()
    for name, df in zip(data.TABLES, (n, a, p)):
        df.to_parquet(dd / f"{name}.parquet")
    (dd / "revision.json").write_text(json.dumps({"sha": "abc"}))
    doc = data.build_documents(n, a, p)[NID]
    rec = extract.extract_document(doc, call_fn=fake(answer()), effort=extract.CONFIG["extraction"].get("effort"))
    out = tmp_path / "out"
    out.mkdir()
    sent = []
    real = extract.extract_document

    def fake_extract(d, *, model, retries, effort):
        sent.append(d.notice_id)
        return real(d, model=model, retries=retries, effort=effort, call_fn=fake(answer()))
    monkeypatch.setattr(extract, "extract_document", fake_extract)
    args = ["extract", "--out", str(out), "--data-dir", str(dd), "--max-usd", "1"]
    (out / "extractions.jsonl").write_text(json.dumps(rec) + "\n")
    assert cli.main(args) == 0 and sent == []                       # same text: done
    stale = dict(rec, chars_total=rec["chars_total"] - 500)
    (out / "extractions.jsonl").write_text(json.dumps(stale) + "\n")
    assert cli.main(args) == 0 and sent == [NID]                    # text changed: redone


def test_values_the_walk_cannot_place_are_kept():
    data = DATASETS.replace(b"</form1>", b"<Table1><Row1><Cell>Acme Corp responded but lacks the data rights."
                                         b"</Cell></Row1></Table1></form1>")
    t = xfa.render(TEMPLATE, data)
    assert t.splitlines()[-2:] == ["Other form entries:", "Acme Corp responded but lacks the data rights."]
    assert "Other form entries" not in xfa.render(TEMPLATE, DATASETS)
