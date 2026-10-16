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


def page_prompt(first_page: int, last_page: int, total_pages: int) -> str:
    return (
        f"This document chunk contains pages {first_page} through {last_page} "
        f"of a {total_pages}-page document. Transcribe every page in order, "
        f"starting each with '--- page N ---' where N runs from {first_page} "
        f"to {last_page}."
    )


def cost_usd(input_tokens: int, output_tokens: int) -> float:
    """Cost of ONE request at claude-haiku-5-5 list prices (tier by prompt size)."""
    if input_tokens > config.PRICE_TIER_THRESHOLD:
        pin, pout = config.PRICE_INPUT_PER_M_LONG, config.PRICE_OUTPUT_PER_M_LONG
    else:
        pin, pout = config.PRICE_INPUT_PER_M, config.PRICE_OUTPUT_PER_M
    return input_tokens / 1e6 * pin + output_tokens / 1e6 * pout


class BudgetExceeded(Exception):
    pass


@dataclass
class BudgetTracker:
    max_usd: float
    spent_usd: float = 0.0

    def check(self) -> None:
        if self.spent_usd >= self.max_usd:
            raise BudgetExceeded(f"spent ${self.spent_usd:.4f} >= cap ${self.max_usd:.2f}")

    def add(self, usd: float) -> None:
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


def split_pdf(data: bytes, start: int, end: int) -> bytes:
    """Return a new PDF holding pages [start, end) (0-based)."""
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(io.BytesIO(data))
    w = PdfWriter()
    for i in range(start, end):
        w.add_page(reader.pages[i])
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


_PAGE_MARK = re.compile(r"^--- page (\d+) ---\s*$", re.M)


def pages_marked(text: str) -> list[int]:
    return [int(m) for m in _PAGE_MARK.findall(text)]


MAX_CHUNK_BYTES = 20_000_000  # request limit is 32 MB including base64 overhead


class Transcriber:
    def __init__(self, client=None, model: str = config.TRANSCRIBE_MODEL,
                 pages_per_chunk: int = config.TRANSCRIBE_PAGES_PER_CHUNK,
                 budget: BudgetTracker | None = None, max_tokens: int = 32000):
        if client is None:
            import anthropic
            client = anthropic.Anthropic(max_retries=6)
        self.client = client
        self.model = model
        self.pages_per_chunk = pages_per_chunk
        self.budget = budget
        self.max_tokens = max_tokens

    def _call(self, pdf_bytes: bytes, first: int, last: int, total: int):
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
                {"type": "text", "text": page_prompt(first, last, total)},
            ]}],
        )
        with self.client.messages.stream(**kwargs) as stream:
            return stream.get_final_message()

    def _transcribe_range(self, data: bytes, start: int, end: int, total: int,
                          res: TranscriptionResult) -> list[str]:
        if self.budget:
            self.budget.check()
        chunk = data if (start == 0 and end == total) else split_pdf(data, start, end)
        if len(chunk) > MAX_CHUNK_BYTES and end - start > 1:
            mid = (start + end) // 2
            return (self._transcribe_range(data, start, mid, total, res)
                    + self._transcribe_range(data, mid, end, total, res))
        msg = self._call(chunk, start + 1, end, total)
        u = msg.usage
        inp = (u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0) \
            + (getattr(u, "cache_read_input_tokens", 0) or 0)
        c = cost_usd(inp, u.output_tokens or 0)
        res.input_tokens += inp
        res.output_tokens += u.output_tokens or 0
        res.cost_usd += c
        res.requests += 1
        res.stop_reasons.append(msg.stop_reason or "")
        if self.budget:
            self.budget.add(c)
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        if msg.stop_reason == "max_tokens" and end - start > 1:
            mid = (start + end) // 2
            return (self._transcribe_range(data, start, mid, total, res)
                    + self._transcribe_range(data, mid, end, total, res))
        if msg.stop_reason == "max_tokens":
            text += "\n[transcription truncated: output limit reached]"
        if msg.stop_reason == "refusal":
            res.error = "refusal"
        return [text.strip()]

    def transcribe_pdf(self, path: str, page_count: int) -> TranscriptionResult:
        data = open(path, "rb").read()
        res = TranscriptionResult(text="", model=self.model,
                                  prompt_version=PROMPT_VERSION, pages=page_count)
        parts: list[str] = []
        for start in range(0, page_count, self.pages_per_chunk):
            end = min(start + self.pages_per_chunk, page_count)
            parts.extend(self._transcribe_range(data, start, end, page_count, res))
        res.text = "\n\n".join(p for p in parts if p)
        return res
