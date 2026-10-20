"""Machine transcription of scanned / image-only PDFs with the Anthropic API.

The PDF is split into chunks of pages (pypdf), and each chunk is sent as a
base64 ``document`` block. The model is asked for a verbatim transcription
with explicit [REDACTED] / [illegible] markers and never to guess redacted
content. Token usage and cost are recorded per document; a BudgetTracker
stops the run before a spend cap is crossed.
"""

from __future__ import annotations

import base64
import io
import logging
import re
import time
from dataclasses import dataclass, field

from . import config

log = logging.getLogger(__name__)

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = """You transcribe scanned U.S. government contracting documents \
(Justification and Approval documents and their attachments) into plain text.

Rules:
- Transcribe verbatim: every word, number, date, dollar amount, citation, \
heading, form label, table cell, footnote, stamp and handwritten annotation \
that is legible. Keep original spelling, capitalization and punctuation, even \
if it looks wrong. Do not summarize, paraphrase, correct, or add commentary.
- Preserve layout loosely: keep paragraph breaks and list numbering; render \
tables as rows with cells separated by " | ".
- Redactions: wherever content is blacked out, whited out, boxed over, or \
otherwise removed, write [REDACTED]. If a redaction label is printed on the \
page (for example "(b)(4)" or "b6"), keep it next to the marker, e.g. \
"[REDACTED (b)(4)]". Never guess, infer, or reconstruct redacted content.
- Illegible text: write [illegible] for any word or passage you cannot read \
with confidence. Do not guess.
- Signatures: write [signature] for a handwritten signature; transcribe a \
printed name or digital-signature text block if legible.
- Blank pages: write [blank page].
- Begin each page with a line "--- page N ---" using the page numbers given \
in the request.
- Output only the transcription."""


def describe_pages(pages: list[int]) -> str:
    if len(pages) == 1:
        return f"page {pages[0]}"
    if pages == list(range(pages[0], pages[-1] + 1)):
        return f"pages {pages[0]} through {pages[-1]}"
    return "pages " + ", ".join(map(str, pages[:-1])) + f" and {pages[-1]}"


def page_prompt(pages: list[int], total_pages: int) -> str:
    """pages: the original 1-based page numbers contained in this chunk, in order."""
    return (
        f"This PDF contains {describe_pages(pages)} of a {total_pages}-page "
        f"document, in that order ({len(pages)} page{'s' if len(pages) > 1 else ''}). "
        f"Transcribe every page in order. Start each page with a line "
        f"'--- page N ---' where N is its original page number: "
        f"{', '.join(map(str, pages))}."
    )


def cost_usd(input_tokens: int, output_tokens: int, *,
             cache_creation_tokens: int = 0, cache_read_tokens: int = 0) -> float:
    """Cost of ONE request at claude-haiku-5-5 list prices (tier by prompt size).
    input_tokens includes any cache write/read tokens, which are priced at
    1.25x / 0.1x the input price. (Transcription requests set no cache
    breakpoint, so both are 0 in practice; this keeps the price right if the
    API ever reports them.)"""
    if input_tokens > config.PRICE_TIER_THRESHOLD:
        pin, pout = config.PRICE_INPUT_PER_M_LONG, config.PRICE_OUTPUT_PER_M_LONG
    else:
        pin, pout = config.PRICE_INPUT_PER_M, config.PRICE_OUTPUT_PER_M
    inp = (input_tokens - cache_creation_tokens - cache_read_tokens
           + 1.25 * cache_creation_tokens + 0.10 * cache_read_tokens)
    return inp / 1e6 * pin + output_tokens / 1e6 * pout


class BudgetExceeded(Exception):
    pass


@dataclass
class BudgetTracker:
    max_usd: float
    spent_usd: float = 0.0

    def __post_init__(self):
        import threading
        self._lock = threading.Lock()

    def check(self) -> None:
        if self.spent_usd >= self.max_usd:
            raise BudgetExceeded(f"spent ${self.spent_usd:.4f} >= cap ${self.max_usd:.2f}")

    def add(self, usd: float) -> None:
        with self._lock:
            self.spent_usd += usd


@dataclass
class TranscriptionResult:
    text: str
    model: str
    prompt_version: str
    pages: int
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    requests: int = 0
    stop_reasons: list[str] = field(default_factory=list)
    error: str = ""
    page_texts: dict[int, str] = field(default_factory=dict)  # 1-based page -> text


def split_pdf(data: bytes, pages: list[int]) -> bytes:
    """Return a new PDF holding the given 1-based pages, in order."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(data))
    w = PdfWriter()
    for p in pages:
        w.add_page(reader.pages[p - 1])
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


_PAGE_MARK = re.compile(r"^--- page (\d+) ---\s*$", re.M)


def pages_marked(text: str) -> list[int]:
    return [int(m) for m in _PAGE_MARK.findall(text)]


def split_by_markers(text: str) -> dict[int, str]:
    """'--- page N ---' sections -> {N: body}. Text before the first marker is dropped
    only if blank; otherwise it is kept under key 0 so the caller can notice."""
    out: dict[int, str] = {}
    marks = list(_PAGE_MARK.finditer(text))
    head = text[: marks[0].start()].strip() if marks else text.strip()
    if head:
        out[0] = head
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out[int(m.group(1))] = text[m.end():end].strip()
    return out


def merge_page_texts(native: list[str], transcribed: dict[int, str]) -> str:
    """Per page: the transcription if there is one, else the native text."""
    parts = []
    for i, t in enumerate(native, 1):
        body = transcribed.get(i, t)
        parts.append(f"--- page {i} ---\n{(body or '').strip()}")
    return "\n\n".join(parts)


_INVISIBLE = re.compile("[\u200b\u200c\u200d\u2060\ufeff]")


def _clean_output(text: str) -> str:
    """Drop zero-width characters (one appeared inside a transcribed ID in
    the pilot; they are never on the page)."""
    return _INVISIBLE.sub("", text).strip()


MAX_CHUNK_BYTES = 20_000_000  # request limit is 32 MB including base64 overhead


class Transcriber:
    def __init__(self, client=None, model: str = config.TRANSCRIBE_MODEL,
                 pages_per_chunk: int = config.TRANSCRIBE_PAGES_PER_CHUNK,
                 budget: BudgetTracker | None = None, max_tokens: int = 32000):
        if client is None:
            import os

            import anthropic
            # Keys that are not scoped to a workspace need this header.
            ws = os.environ.get("ANTHROPIC_WORKSPACE_ID")
            headers = {"anthropic-workspace-id": ws} if ws else None
            client = anthropic.Anthropic(max_retries=6, default_headers=headers)
        self.client = client
        self.model = model
        self.pages_per_chunk = pages_per_chunk
        self.budget = budget
        self.max_tokens = max_tokens

    def _call(self, pdf_bytes: bytes, pages: list[int], total: int):
        b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
        kwargs = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=SYSTEM_PROMPT,
            # No temperature: claude-haiku-5-5 rejects sampling parameters.
            # Thinking off: transcription needs no reasoning, and thinking
            # tokens are billed as output.
            thinking={"type": "disabled"},
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": [
                {"type": "document", "source": {
                    "type": "base64", "media_type": "application/pdf", "data": b64}},
                {"type": "text", "text": page_prompt(pages, total)},
            ]}],
        )
        with self.client.messages.stream(**kwargs) as stream:
            return stream.get_final_message()

    def _account(self, msg, res: TranscriptionResult) -> None:
        u = msg.usage
        write = getattr(u, "cache_creation_input_tokens", 0) or 0
        read = getattr(u, "cache_read_input_tokens", 0) or 0
        inp = (u.input_tokens or 0) + write + read
        c = cost_usd(inp, u.output_tokens or 0, cache_creation_tokens=write, cache_read_tokens=read)
        res.input_tokens += inp
        res.output_tokens += u.output_tokens or 0
        res.cost_usd += c
        res.requests += 1
        res.stop_reasons.append(msg.stop_reason or "")
        if self.budget:
            self.budget.add(c)

    def _halves(self, data, pages, total, res):
        mid = len(pages) // 2
        out = self._transcribe_pages(data, pages[:mid], total, res)
        out.update(self._transcribe_pages(data, pages[mid:], total, res))
        return out

    def _transcribe_pages(self, data: bytes, pages: list[int], total: int,
                          res: TranscriptionResult) -> dict[int, str]:
        if self.budget:
            self.budget.check()
        whole = pages == list(range(1, total + 1))
        chunk = data if whole else split_pdf(data, pages)
        if len(chunk) > MAX_CHUNK_BYTES and len(pages) > 1:
            return self._halves(data, pages, total, res)
        msg = self._call(chunk, pages, total)
        self._account(msg, res)
        text = _clean_output("".join(b.text for b in msg.content if getattr(b, "type", "") == "text"))
        if msg.stop_reason == "refusal":
            res.error = "refusal"
            return {}
        if msg.stop_reason == "max_tokens" and len(pages) > 1:
            return self._halves(data, pages, total, res)
        if msg.stop_reason == "max_tokens":
            text += "\n[transcription truncated: output limit reached]"
        parts = split_by_markers(text)
        if len(pages) == 1:
            # one page: everything belongs to it, marker or not
            body = parts.get(pages[0])
            if body is None or len(parts) > 1:
                body = _PAGE_MARK.sub("", text).strip()
            return {pages[0]: body}
        if sorted(k for k in parts if k) == sorted(pages) and 0 not in parts:
            return {p: parts[p] for p in pages}
        # Seen in the pilot: for a chunk starting mid-document the model
        # sometimes numbers pages 1..n within the chunk. Map those back when
        # the numbering is exactly 1..n and nothing precedes the first marker.
        if 0 not in parts and sorted(parts) == list(range(1, len(pages) + 1)):
            return {pages[i - 1]: parts[i] for i in range(1, len(pages) + 1)}
        # Markers don't line up with the pages sent: redo page by page rather
        # than guess which text belongs to which page.
        log.warning("page markers %s != requested %s; retrying per page",
                    sorted(parts), pages)
        out: dict[int, str] = {}
        for p in pages:
            out.update(self._transcribe_pages(data, [p], total, res))
        return out

    def transcribe_image(self, path: str) -> TranscriptionResult:
        """Transcribe an image attachment (PNG/JPEG) as page 1."""
        data = open(path, "rb").read()
        media = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
        res = TranscriptionResult(text="", model=self.model, prompt_version=PROMPT_VERSION, pages=1)
        if self.budget:
            self.budget.check()
        b64 = base64.standard_b64encode(data).decode("ascii")
        kwargs = dict(model=self.model, max_tokens=self.max_tokens, system=SYSTEM_PROMPT,
                      thinking={"type": "disabled"}, output_config={"effort": "low"},
                      messages=[{"role": "user", "content": [
                          {"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}},
                          {"type": "text", "text": "This image is page 1 of a 1-page document. "
                                                   "Transcribe it, starting with '--- page 1 ---'."}]}])
        with self.client.messages.stream(**kwargs) as stream:
            msg = stream.get_final_message()
        self._account(msg, res)
        if msg.stop_reason == "refusal":
            res.error = "refusal"
            return res
        text = _clean_output("".join(b.text for b in msg.content if getattr(b, "type", "") == "text"))
        res.page_texts = {1: _PAGE_MARK.sub("", text).strip()}
        res.text = f"--- page 1 ---\n{res.page_texts[1]}"
        return res

    def transcribe_pdf(self, path: str, page_count: int,
                       pages: list[int] | None = None) -> TranscriptionResult:
        """Transcribe the given 1-based pages (default: all) of a PDF."""
        data = open(path, "rb").read()
        pages = list(pages) if pages else list(range(1, page_count + 1))
        res = TranscriptionResult(text="", model=self.model,
                                  prompt_version=PROMPT_VERSION, pages=len(pages))
        for i in range(0, len(pages), self.pages_per_chunk):
            res.page_texts.update(
                self._transcribe_pages(data, pages[i:i + self.pages_per_chunk], page_count, res))
            if res.error:
                break
        res.text = "\n\n".join(f"--- page {p} ---\n{res.page_texts[p]}"
                                for p in sorted(res.page_texts))
        return res
