"""Native PDF text: pdftotext first, pypdf as the fallback, and which one read each page.

No network. The PDFs are built by hand. These tests show the plumbing (engine
choice, fallback, page numbers, the stored `text_engine`); the quality
difference between the engines is measured on real documents in the pull request
that introduced pdftotext, not here.
"""
import shutil
import subprocess

import pytest

from federal_ja.collect import extract as ex
from federal_ja.collect import pipeline
from federal_ja.collect.store import Store

from .test_extract import LONG, blank_pdf, text_pdf
from .test_xfa import make_pdf as xfa_pdf

needs_poppler = pytest.mark.skipif(shutil.which("pdftotext") is None, reason="poppler is not installed")


@pytest.fixture(autouse=True)
def default_engine(monkeypatch):
    # a JA_PDF_ENGINE left in the environment must not change what these tests mean
    monkeypatch.delenv("JA_PDF_ENGINE", raising=False)


def write(tmp_path, data: bytes, name="f.pdf") -> str:
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


@needs_poppler
def test_pdftotext_is_the_default_and_pages_line_up(tmp_path):
    path = write(tmp_path, text_pdf(["First page text", "Second page text", "Third page text"]))
    r = ex.extract_pdf(path)
    assert r.text_engine == "pdftotext"
    assert r.page_count == 3 and len(r.page_texts) == 3
    assert [t.strip() for t in r.page_texts] == ["First page text", "Second page text", "Third page text"]
    # same page numbers as pypdf gives
    assert [t.strip() for t in ex.pypdf_page_texts(path)] == [t.strip() for t in r.page_texts]


@needs_poppler
def test_an_empty_last_page_keeps_its_place(tmp_path):
    # pdftotext ends every page with a form feed; the empty page after the
    # last one must not be dropped or a phantom page added
    r = ex.extract_pdf(write(tmp_path, text_pdf([LONG, ""])))
    assert r.text_engine == "pdftotext" and r.page_count == 2
    assert r.textless_pages == [2] and r.needs_transcription is True


@needs_poppler
def test_apostrophes_and_spacing_survive(tmp_path):
    path = write(tmp_path, text_pdf(["The Government's services were not competed."]))
    r = ex.extract_pdf(path)
    assert r.text_engine == "pdftotext"
    # Helvetica's standard encoding turns the typewriter apostrophe into a
    # curly one; both engines read it the same way
    assert r.page_texts[0].strip() == "The Government\u2019s services were not competed."
    assert ex.pypdf_page_texts(path)[0].strip() == r.page_texts[0].strip()


@needs_poppler
def test_typed_versus_scanned_decision_is_the_same_under_both_engines(tmp_path):
    path = write(tmp_path, text_pdf([LONG, "Page 2 of 3", LONG]))
    via_pdftotext = ex.extract_pdf(path)
    via_pypdf = ex.pypdf_page_texts(path)
    assert via_pdftotext.textless_pages == ex.textless_pages(via_pypdf) == [2]


def test_environment_variable_forces_pypdf(tmp_path, monkeypatch):
    monkeypatch.setenv("JA_PDF_ENGINE", "pypdf")
    r = ex.extract_pdf(write(tmp_path, text_pdf(["Some words here"])))
    assert r.text_engine == "pypdf" and r.page_texts[0].strip() == "Some words here"


def test_falls_back_to_pypdf_when_poppler_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(ex.shutil, "which", lambda name: None)
    path = write(tmp_path, text_pdf(["Some words here", "More words"]))
    r = ex.extract_pdf(path)
    assert r.text_engine == "pypdf" and r.page_count == 2
    assert [t.strip() for t in r.page_texts] == ["Some words here", "More words"]


def test_falls_back_to_pypdf_when_pdftotext_refuses_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ex.shutil, "which", lambda name: "/usr/bin/pdftotext")
    monkeypatch.setattr(ex.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 1, stdout=b"", stderr=b"Copying of text from this document is not allowed."))
    r = ex.extract_pdf(write(tmp_path, text_pdf(["Copy-protected words"])))
    assert r.text_engine == "pypdf" and r.page_texts[0].strip() == "Copy-protected words"


def test_falls_back_to_pypdf_on_a_timeout(tmp_path, monkeypatch):
    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a, 1)
    monkeypatch.setattr(ex.shutil, "which", lambda name: "/usr/bin/pdftotext")
    monkeypatch.setattr(ex.subprocess, "run", slow)
    r = ex.extract_pdf(write(tmp_path, text_pdf(["Slow words"])))
    assert r.text_engine == "pypdf"


def test_a_different_page_count_means_pypdf_is_used(tmp_path, monkeypatch):
    # page numbers must not move because of the engine: if pdftotext disagrees
    # with pypdf about how many pages there are, keep pypdf's reading
    monkeypatch.setattr(ex, "pdftotext_page_texts", lambda path: ["only one page"])
    r = ex.extract_pdf(write(tmp_path, text_pdf(["a b c", "d e f", "g h i"])))
    assert r.text_engine == "pypdf" and r.page_count == 3


def test_a_blank_pdf_still_needs_transcription(tmp_path):
    r = ex.extract_pdf(write(tmp_path, blank_pdf(2)))
    assert r.page_count == 2 and r.needs_transcription is True and r.textless_pages == [1, 2]


@needs_poppler
def test_xfa_forms_are_still_read_from_their_xml(tmp_path):
    # pdftotext sees only the "Please wait..." placeholder, as pypdf does; the
    # form text comes from the XML, which is read with pypdf
    r = ex.extract_file(xfa_pdf(tmp_path / "f.pdf"), "SSJ.pdf")
    assert r.xfa_form and r.text_source == "pdf_xfa" and r.text_engine == "pypdf"
    assert "Please wait" not in r.page_texts[0]


def test_the_engine_is_stored_on_each_page_row_and_survives_a_save(tmp_path):
    s = Store(str(tmp_path))
    a = {"notice_id": "n", "resource_id": "1", "sha256": "h"}
    pipeline.set_pdf_pages(s, a, [LONG, ""], engine="pdftotext")
    rows = s.pages[("n", "1")]
    assert [r["text_engine"] for r in rows] == ["pdftotext", "pdftotext"]
    s.save()
    t = Store(str(tmp_path)).load()
    assert [r["text_engine"] for r in t.pages[("n", "1")]] == ["pdftotext", "pdftotext"]


def test_old_rows_without_the_column_load_with_no_engine(tmp_path):
    s = Store(str(tmp_path))
    pipeline.set_pdf_pages(s, {"notice_id": "n", "resource_id": "1", "sha256": "h"}, [LONG])
    assert s.pages[("n", "1")][0]["text_engine"] is None


def test_apply_extraction_passes_the_engine_through(tmp_path, monkeypatch):
    s = Store(str(tmp_path))
    a = {"notice_id": "n", "resource_id": "1", "sha256": "h", "filename": "x.pdf"}
    r = ex.extract_pdf(write(tmp_path, text_pdf([LONG])))
    pipeline.apply_extraction(a, r, s)
    assert s.pages[("n", "1")][0]["text_engine"] == r.text_engine
    assert r.text_engine in ("pdftotext", "pypdf")


# --- the backfill: re-read pypdf-era pages with pdftotext --------------------

NEW = "Services were not competed because only one source could supply them. " * 8  # >300 chars


def pypdf_era_store(tmp_path, pdf_pages, stored_pages, transcribed=()):
    """A store with one PDF whose pages were stored with no text_engine, and a
    `fetch` that hands back a fresh copy of the PDF each time."""
    s = Store(str(tmp_path / "work"))
    a = {"notice_id": "n", "resource_id": "1", "sha256": "h", "filename": "x.pdf", "file_path": "files/x.pdf",
         "download_status": "ok", "file_kind": "pdf"}
    s.attachments[("n", "1")] = a
    pipeline.set_pdf_pages(s, a, stored_pages)  # engine None: how every row looked before the column existed
    for n in transcribed:
        s.pages[("n", "1")][n - 1].update(text="[transcribed]", text_source="transcription",
                                          text_layer_text="", transcription_model="m")
    src = write(tmp_path, text_pdf(pdf_pages), "src.pdf")
    count = {"n": 0}

    def fetch(row):
        count["n"] += 1
        dst = tmp_path / f"copy{count['n']}.pdf"
        shutil.copy(src, dst)
        return str(dst)
    return s, a, fetch


def test_the_backfill_picks_only_pypdf_era_pdfs(tmp_path):
    s, a, fetch = pypdf_era_store(tmp_path, [NEW], [NEW])
    assert pipeline.pypdf_era_pdfs(s) == [a]
    pipeline.set_pdf_pages(s, a, [NEW], engine="pdftotext")
    assert pipeline.pypdf_era_pdfs(s) == []


@needs_poppler
def test_the_backfill_replaces_the_text_and_records_the_engine(tmp_path):
    old = "Service s were not competed because only one source could supply them. " * 8
    s, a, fetch = pypdf_era_store(tmp_path, [NEW], [old])
    out = pipeline.reread_pdf_text(s, pipeline.pypdf_era_pdfs(s), fetch)
    assert (out["read"], out["changed_docs"], out["changed_pages"], out["skipped"]) == (1, 1, 1, [])
    row = s.pages[("n", "1")][0]
    assert row["text_engine"] == "pdftotext" and "Services were not competed" in row["text"]
    assert "Services were not competed" in a["text"] and "Service s were" not in a["text"]
    assert out["changes"][0]["pages_changed"] == 1
    assert pipeline.pypdf_era_pdfs(s) == []          # resumable: not picked again
    assert not list(tmp_path.glob("copy*.pdf"))      # the fetched copy is deleted


@needs_poppler
def test_the_backfill_counts_a_file_with_the_same_text_as_unchanged(tmp_path):
    same = ex.pdftotext_page_texts(write(tmp_path, text_pdf([NEW]), "probe.pdf"))[0]
    s, a, fetch = pypdf_era_store(tmp_path, [NEW], [same])
    out = pipeline.reread_pdf_text(s, pipeline.pypdf_era_pdfs(s), fetch)
    assert out["read"] == 1 and out["unchanged"] == 1 and out["changed_pages"] == 0
    assert s.pages[("n", "1")][0]["text_engine"] == "pdftotext"


@needs_poppler
def test_the_backfill_leaves_a_file_alone_when_the_page_count_changes(tmp_path):
    s, a, fetch = pypdf_era_store(tmp_path, [NEW, NEW], [NEW])
    before = [dict(r) for r in s.pages[("n", "1")]]
    out = pipeline.reread_pdf_text(s, pipeline.pypdf_era_pdfs(s), fetch)
    assert out["read"] == 0 and "page count 1 -> 2" in out["skipped"][0]["reason"]
    assert s.pages[("n", "1")] == before


@needs_poppler
def test_the_backfill_leaves_a_file_alone_when_the_typed_or_scanned_decision_changes(tmp_path):
    s, a, fetch = pypdf_era_store(tmp_path, [NEW, NEW], [NEW, ""])   # page 2 was stored as scanned
    out = pipeline.reread_pdf_text(s, pipeline.pypdf_era_pdfs(s), fetch)
    assert out["read"] == 0 and "textless pages [2] -> []" in out["skipped"][0]["reason"]
    assert s.pages[("n", "1")][1]["text_engine"] is None


def test_the_backfill_skips_when_pdftotext_did_not_read_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("JA_PDF_ENGINE", "pypdf")
    s, a, fetch = pypdf_era_store(tmp_path, [NEW], [NEW])
    out = pipeline.reread_pdf_text(s, pipeline.pypdf_era_pdfs(s), fetch)
    assert out["read"] == 0 and out["skipped"][0]["reason"] == "engine was pypdf"
    assert pipeline.pypdf_era_pdfs(s) == [a]         # still waiting for a run that has poppler


@needs_poppler
def test_the_backfill_never_touches_a_transcribed_page(tmp_path):
    s, a, fetch = pypdf_era_store(tmp_path, [NEW, ""], [NEW, ""], transcribed=(2,))
    out = pipeline.reread_pdf_text(s, pipeline.pypdf_era_pdfs(s), fetch)
    p2 = s.pages[("n", "1")][1]
    assert out["read"] == 1 and p2["text"] == "[transcribed]" and p2["text_source"] == "transcription"
    assert a["text_source"] == "pdf_text+transcription"


def test_a_missing_hf_copy_is_reported_not_fatal(tmp_path):
    s, a, _ = pypdf_era_store(tmp_path, [NEW], [NEW])

    def broken(row):
        raise FileNotFoundError("gone")
    out = pipeline.reread_pdf_text(s, [a], broken)
    assert out["read"] == 0 and out["fetch_failed"][0]["resource_id"] == "1"
