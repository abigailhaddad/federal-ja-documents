"""Document text for tagging, and the verbatim-quote check.

EXPERIMENTAL. See ``federal_ja.analysis`` for what that means.

A J&A's text is built from the published ``data/pages.parquet`` table: every
page of every attachment, native text layer or machine transcription,
in attachment order and then page order. ``.docx`` attachments have no page
rows (they have no pages), so their whole-document text from
``data/attachments.parquet`` is used as one page. Spreadsheets (``xlsx``,
usually price sheets) are left out.

Every tag must carry a quote that is found in that text. Matching is exact,
or exact after the normalisation below (applied to both sides). The quote is
then located in the page(s) it came from, so a reader knows which page to
open and whether that page was machine-transcribed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Normalisation (applied identically to quotes and to the document)
# ---------------------------------------------------------------------------

# U+01AF / U+01B1 are what pypdf returns for the "ff" / "fi" ligature glyphs
# in some agency fonts ("suƯicient"); the presentation-form ligatures
# (U+FB00-FB04) are mapped for the same reason. List-bullet glyphs are
# deleted: models copy bulleted lists without them. U+F0B7 / U+F0A7 are the
# private-use bullets Word's Symbol font leaves in extracted text.
_LIGATURES = {"Ư": "ff", "Ʊ": "fi",
              "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"}
_BULLETS = {"•": "", "▪": "", "●": "", "◦": "", "": "", "": ""}
_CHAR_MAP = str.maketrans({
    "’": "'", "‘": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", " ": " ", "­": "",
    **_LIGATURES, **_BULLETS,
})
_WS = re.compile(r"\s+")
_WRAP_HYPHEN = re.compile(r"-\s+")
_ELLIPSIS = re.compile("\\.\\.\\.|…")

# The prompts ask for 8-60 word quotes. A 3-word fragment can't show a reader
# what the document says, so quotes under this length are rejected.
MIN_QUOTE_WORDS = 5


def normalize(s: str | None) -> str:
    """Case-sensitive normalisation shared by quote and document: curly
    quotes and dashes straightened, ligature glyphs expanded, bullets
    deleted, whitespace runs collapsed to one space, and a hyphen followed by
    whitespace (a line-wrap hyphen, "reduc-\\ntion") joined to a bare
    hyphen."""
    s = (s or "").translate(_CHAR_MAP)
    s = _WS.sub(" ", s).strip()
    return _WRAP_HYPHEN.sub("-", s)


def quote_ok(quote: str | None, normalized_doc: str) -> bool:
    """True only for a quote of at least MIN_QUOTE_WORDS words that is a
    substring of the normalised document. Any ellipsis means the quote was
    stitched from separate passages, which the prompts forbid, so it fails
    even when the pieces exist."""
    if not quote or _ELLIPSIS.search(quote):
        return False
    q = normalize(quote)
    if len(q.split()) < MIN_QUOTE_WORDS:
        return False
    return q in normalized_doc


# ---------------------------------------------------------------------------
# Document assembly
# ---------------------------------------------------------------------------

# Validated length range. Longer documents were never in a validation set,
# and cost grows with length, so they are skipped ("too_long").
MAX_DOC_CHARS = 60_000
MIN_DOC_CHARS = 200


@dataclass
class Page:
    resource_id: str
    filename: str
    page_number: int | None      # None for a .docx (no pages)
    text_source: str             # native | transcription | none
    text: str


@dataclass
class Document:
    notice_id: str
    meta: dict
    pages: list[Page] = field(default_factory=list)

    @property
    def has_transcribed_page(self) -> bool:
        return any(p.text_source == "transcription" and p.text.strip() for p in self.pages)

    def attachments(self) -> list[tuple[str, list[Page]]]:
        """(filename, pages) per attachment, in order."""
        out: list[tuple[str, list[Page]]] = []
        current = None
        for p in self.pages:
            if p.resource_id != current:
                current = p.resource_id
                out.append((p.filename or "document", []))
            out[-1][1].append(p)
        return out

    def model_text(self) -> str:
        """What the model reads: the validated layout, one
        '=== ATTACHMENT: <filename> ===' header per attachment followed by
        that attachment's text (its pages joined by blank lines)."""
        return "\n\n".join(f"=== ATTACHMENT: {fn} ===\n" + "\n\n".join(p.text for p in pages)
                           for fn, pages in self.attachments())

    def verification_text(self) -> str:
        """What quotes are checked against: the page texts WITHOUT headers,
        so a quote of a header can't verify."""
        return "\n\n".join(p.text for p in self.pages)

    def normalized(self) -> str:
        return normalize(self.verification_text())

    def skip_reason(self) -> str | None:
        n = len(self.model_text())
        if not any(p.text.strip() for p in self.pages) or n < MIN_DOC_CHARS:
            return "no_text"
        if n > MAX_DOC_CHARS:
            return "too_long"
        return None


@dataclass
class QuoteMatch:
    verified: bool
    match: str | None                   # "exact" | "normalized" | None
    pages: list[dict]                   # [{resource_id, page_number, text_source}]

    @property
    def transcribed(self) -> bool | None:
        if not self.pages:
            return None
        return any(p["text_source"] == "transcription" for p in self.pages)


def locate_quote(quote: str | None, doc: Document, normalized_doc: str | None = None) -> QuoteMatch:
    """Verify a quote against the document and find the page(s) it is on.
    A quote that runs across a page break is located on the shortest run of
    consecutive pages that contains it."""
    nd = normalized_doc if normalized_doc is not None else doc.normalized()
    if not quote_ok(quote, nd):
        return QuoteMatch(False, None, [])
    raw = _WS.sub(" ", quote).strip()
    exact = raw in _WS.sub(" ", doc.verification_text())
    q = normalize(quote)
    texts = [normalize(p.text) for p in doc.pages]
    found: list[int] = [i for i, t in enumerate(texts) if q in t]
    if found:
        idx = [found[0]]
    else:
        idx = []
        n = len(doc.pages)
        for span in range(2, n + 1):
            for i in range(0, n - span + 1):
                if q in normalize("\n\n".join(p.text for p in doc.pages[i:i + span])):
                    idx = list(range(i, i + span))
                    break
            if idx:
                break
    pages = [{"resource_id": doc.pages[i].resource_id, "page_number": doc.pages[i].page_number,
              "text_source": doc.pages[i].text_source} for i in idx]
    return QuoteMatch(True, "exact" if exact else "normalized", pages)


def clean_quote(quote: str) -> str:
    return _WS.sub(" ", quote).strip()


def display_quote(quote: str | None) -> str | None:
    """A verified quote with pypdf ligature glyphs mapped back to letters, for
    reading. It still verifies, since normalize() applies the same map."""
    return quote.translate(str.maketrans(_LIGATURES)) if quote else quote
