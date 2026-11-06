"""Native text extraction and the "does this need transcription?" decision.

text_source values written to the dataset:
  pdf_text       text layer of a PDF, via pdftotext (poppler), or pypdf where
                 pdftotext is missing or fails; pages.text_engine says which
  docx           python-docx (paragraphs, then tables)
  xlsx           openpyxl (every sheet, tab-separated cells)
  text           plain-text attachment (.txt/.csv), decoded as UTF-8/latin-1
  pdf_xfa        an XFA PDF form: the page is only a "Please wait..."
                 placeholder, so the text is the form's own XML (xfa.py)
  transcription  machine transcription of page images (every page of the
                 PDF lacked a text layer)
  pdf_text+transcription
                 PDF with some text-layer pages and some image-only pages;
                 text-layer pages keep their native text and only the
                 image-only pages are transcribed
  none           no text (unsupported type, unreadable file, or image-only
                 PDF not yet transcribed)

Decision is per page: a page lacks text when its text layer has fewer than
MIN_CHARS_PER_TEXT_PAGE characters (stripped). Many J&As are typed documents with one
or more pages rasterized (often the pages that were redacted or signed), so a
whole-document rule would silently lose those pages. A PDF needs
transcription if ANY page lacks text; ``textless_pages`` lists them.
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field

from . import xfa

log = logging.getLogger(__name__)

# A page "has text" if its text layer, stripped of leading/trailing
# whitespace, has at least this many characters. A typical typed J&A page has
# 2,000-5,000; a scanned page with no OCR has 0; a page with only a header,
# stamp or page number has well under 300. 300 matches the threshold used by
# downstream consumers of the dataset, so "transcribed" means the same thing
# on both sides.
MIN_CHARS_PER_TEXT_PAGE = 300

_ALNUM = re.compile(r"[A-Za-z0-9]")


@dataclass
class ExtractResult:
    file_kind: str
    text: str = ""
    text_source: str = "none"
    page_count: int | None = None
    pages_with_text: int | None = None
    textless_pages: list[int] = field(default_factory=list)  # 1-based
    native_text_chars: int = 0
    needs_transcription: bool = False
    extract_error: str = ""
    page_texts: list[str] = field(default_factory=list, repr=False)
    text_engine: str = ""  # who read the text layer: "pdftotext" or "pypdf"
    xfa_form: bool = False      # page_texts is the one-"page" XFA form text


def detect_kind(data_head: bytes, filename: str = "") -> str:
    """Classify a file by magic bytes, falling back to its extension."""
    name = filename.lower()
    if data_head.startswith(b"%PDF") or b"%PDF-" in data_head[:1024]:
        return "pdf"
    if data_head.startswith(b"PK\x03\x04"):
        if name.endswith((".docx", ".docm")):
            return "docx"
        if name.endswith((".xlsx", ".xlsm")):
            return "xlsx"
        return "zip"  # resolved further in sniff_zip
    if data_head.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
        return "image"
    if data_head.startswith(b"\xd0\xcf\x11\xe0"):
        return "ole"  # legacy .doc/.xls
    if name.endswith((".txt", ".csv")):
        return "text"
    if name.endswith(".pdf"):
        return "pdf"  # damaged header; let pypdf try
    return "other"


def sniff_zip(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
    except zipfile.BadZipFile:
        return "other"
    if "word/document.xml" in names:
        return "docx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    return "zip"


_SURROGATE = re.compile("[\ud800-\udfff]")


def clean_text(s: str) -> str:
    """Replace lone UTF-16 surrogates (pypdf emits them for some CJK/odd
    font encodings) with U+FFFD; they cannot be written as UTF-8."""
    return _SURROGATE.sub("\ufffd", s or "")


def count_alnum(s: str) -> int:
    return len(_ALNUM.findall(s or ""))


def text_layer_chars(text: str) -> int:
    return len((text or "").strip())


_GLYPH_TOKEN = re.compile(r"(/[A-Za-z]{0,3}\d+)+")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def text_layer_garbled(text: str) -> bool:
    """True for text layers that are font-encoding garbage rather than words:
    mostly glyph-name tokens ('/0 /i255 /3', '/X20/X53') or many control
    characters. Seen on ~3% of pages that were otherwise counted as typed."""
    t = (text or "").strip()
    if not t:
        return False
    toks = t.split()
    glyph = sum(1 for x in toks if _GLYPH_TOKEN.fullmatch(x)) / len(toks)
    control = len(_CONTROL.findall(t)) / len(t)
    return glyph > 0.3 or control > 0.1


def page_has_text(text: str) -> bool:
    return text_layer_chars(text) >= MIN_CHARS_PER_TEXT_PAGE and not text_layer_garbled(text)


def textless_pages(page_texts: list[str]) -> list[int]:
    """1-based numbers of pages whose text layer is missing or too thin."""
    return [i for i, t in enumerate(page_texts, 1) if not page_has_text(t)]


def pdf_needs_transcription(page_texts: list[str]) -> bool:
    return not page_texts or bool(textless_pages(page_texts))


def join_pages(page_texts: list[str]) -> str:
    return "\n\n".join(f"--- page {i} ---\n{t.strip()}" for i, t in enumerate(page_texts, 1))


ENGINE_PDFTOTEXT = "pdftotext"
ENGINE_PYPDF = "pypdf"
_PDFTOTEXT_TIMEOUT_S = 180


def pdf_engine_preference() -> str:
    """"pdftotext" (the default) or "pypdf". Set JA_PDF_ENGINE=pypdf to go back
    to the old reader, e.g. to rebuild a page exactly as it was published
    before 2026-11-06."""
    return ENGINE_PYPDF if os.environ.get("JA_PDF_ENGINE", "").strip().lower() == ENGINE_PYPDF else ENGINE_PDFTOTEXT


def pdftotext_page_texts(path: str) -> list[str] | None:
    """Page texts from poppler's ``pdftotext`` in its default reading-order
    mode (not -layout: that pads columns with spaces; not -raw: that follows
    the content stream and scrambles tables). pdftotext ends every page with
    a form feed, so splitting on it gives one text per page. None when
    poppler is missing, times out, or refuses the file (for example a PDF
    that forbids copying text), so the caller can fall back to pypdf."""
    exe = shutil.which("pdftotext")
    if not exe:
        return None
    try:
        proc = subprocess.run([exe, "-enc", "UTF-8", path, "-"], capture_output=True,
                              timeout=_PDFTOTEXT_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("pdftotext failed on %s: %s", path, e)
        return None
    if proc.returncode != 0:
        log.warning("pdftotext exit %s on %s: %s", proc.returncode, path,
                    proc.stderr.decode("utf-8", "replace")[:120])
        return None
    pages = proc.stdout.decode("utf-8", "replace").split("\f")
    if pages and pages[-1] == "":
        pages.pop()  # the form feed after the last page
    return [clean_text(t) for t in pages]


def pypdf_page_texts(path: str, reader=None) -> list[str]:
    from pypdf import PdfReader

    reader = reader or PdfReader(path)
    if reader.is_encrypted:
        reader.decrypt("")
    out = []
    for page in reader.pages:
        try:
            out.append(clean_text(page.extract_text()))
        except Exception:  # noqa: BLE001
            out.append("")
    return out


def native_pdf_texts(path: str, expected_pages: int | None = None, reader=None) -> tuple[list[str], str]:
    """(page texts, engine). pdftotext first; pypdf when pdftotext is missing,
    fails, or returns a different number of pages than pypdf counts (page
    numbers must stay the same whichever engine read the file)."""
    if pdf_engine_preference() == ENGINE_PDFTOTEXT:
        pages = pdftotext_page_texts(path)
        if pages is not None and (expected_pages is None or len(pages) == expected_pages):
            return pages, ENGINE_PDFTOTEXT
        if pages is not None:
            log.warning("pdftotext found %d pages, pypdf %d, in %s: using pypdf",
                        len(pages), expected_pages, path)
    return pypdf_page_texts(path, reader), ENGINE_PYPDF


def pdf_page_texts_with_engine(path: str) -> tuple[list[str], str]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            reader.decrypt("")
        expected = len(reader.pages)
    except Exception:  # noqa: BLE001 - let pdftotext try a file pypdf cannot open
        reader, expected = None, None
    return native_pdf_texts(path, expected, reader)


def pdf_page_texts(path: str) -> list[str]:
    return pdf_page_texts_with_engine(path)[0]


def extract_pdf(path: str) -> ExtractResult:
    from pypdf import PdfReader

    res = ExtractResult(file_kind="pdf")
    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as e:  # noqa: BLE001
                res.extract_error = f"encrypted: {e}"
                return res
        texts, engine = native_pdf_texts(path, len(reader.pages), reader)
        if texts and xfa.is_placeholder(texts[0]):
            try:
                form = clean_text(xfa.form_text(reader))
            except Exception as e:  # noqa: BLE001 - keep the placeholder text
                log.warning("XFA form text failed in %s: %s", path, e)
                form = ""
            if form.strip():
                texts, res.xfa_form, engine = [form], True, ENGINE_PYPDF  # the form XML is read with pypdf
    except Exception as e:  # noqa: BLE001
        res.extract_error = f"pypdf: {type(e).__name__}: {e}"[:500]
        return res
    res.page_texts = texts
    res.text_engine = engine
    res.page_count = len(texts)
    # an XFA form is never transcribed: a picture of it is the placeholder
    res.textless_pages = [] if res.xfa_form else textless_pages(texts)
    res.pages_with_text = len(texts) - len(res.textless_pages)
    res.native_text_chars = sum(len(t) for t in texts)
    res.needs_transcription = not res.xfa_form and pdf_needs_transcription(texts)
    if res.pages_with_text:
        # Native text of every page (empty pages keep their marker) until a
        # transcription fills the textless pages in.
        res.text, res.text_source = join_pages(texts), ("pdf_xfa" if res.xfa_form else "pdf_text")
    else:
        res.text, res.text_source = "", "none"
    return res


def extract_docx(path: str) -> ExtractResult:
    import docx

    res = ExtractResult(file_kind="docx")
    try:
        d = docx.Document(path)
        parts = [p.text for p in d.paragraphs]
        for table in d.tables:
            for row in table.rows:
                parts.append("\t".join(c.text.strip() for c in row.cells))
        res.text = clean_text("\n".join(parts).strip())
    except Exception as e:  # noqa: BLE001
        res.extract_error = f"python-docx: {type(e).__name__}: {e}"[:500]
        return res
    res.native_text_chars = len(res.text)
    res.text_source = "docx" if res.text else "none"
    return res


def extract_xlsx(path: str) -> ExtractResult:
    import openpyxl

    res = ExtractResult(file_kind="xlsx")
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        parts = []
        for ws in wb.worksheets:
            parts.append(f"=== sheet: {ws.title} ===")
            for row in ws.iter_rows(values_only=True):
                cells = ["" if v is None else str(v) for v in row]
                if any(cells):
                    parts.append("\t".join(cells).rstrip())
        wb.close()
        res.text = "\n".join(parts).strip()
    except Exception as e:  # noqa: BLE001
        res.extract_error = f"openpyxl: {type(e).__name__}: {e}"[:500]
        return res
    res.native_text_chars = len(res.text)
    res.text_source = "xlsx" if res.text else "none"
    return res


def extract_text_file(path: str) -> ExtractResult:
    res = ExtractResult(file_kind="text")
    raw = open(path, "rb").read()
    try:
        res.text = raw.decode("utf-8")
    except UnicodeDecodeError:
        res.text = raw.decode("latin-1")
    res.native_text_chars = len(res.text)
    res.text_source = "text" if res.text.strip() else "none"
    return res


def extract_file(path: str, filename: str = "") -> ExtractResult:
    with open(path, "rb") as f:
        head = f.read(2048)
    kind = detect_kind(head, filename)
    if kind == "zip":
        kind = sniff_zip(path)
    if kind == "pdf":
        return extract_pdf(path)
    if kind == "docx":
        return extract_docx(path)
    if kind == "xlsx":
        return extract_xlsx(path)
    if kind == "text":
        return extract_text_file(path)
    if kind == "image":
        # A picture of a page (seen: scanned J&A pages, screenshots of
        # approval emails). One page, no text layer, to be transcribed.
        return ExtractResult(file_kind="image", page_count=1, pages_with_text=0,
                             textless_pages=[1], needs_transcription=True, page_texts=[""])
    return ExtractResult(file_kind=kind)
