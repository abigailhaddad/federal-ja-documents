"""Extract fields from one J&A document. EXPERIMENTAL.

One model call per notice reads the whole document (every attachment, every
page, native text or machine transcription) and returns one record: whether
it is really a J&A, the authority cited, a plain-English summary, the
estimated value, the proposed vendor, the period of performance, the latest
signature date, market research, whether it is redacted, and notes.

Every factual field comes with a verbatim quote. The code, not the model,
decides whether a value is kept:

* the quote must be found in the document text (exactly, or after the
  normalisation in text.py; ALL-CAPS headings may differ in case only), and
* the value must be visible in its own quote: the dollar amount parses out of
  the value quote, the vendor name is in the vendor quote, the signature date
  parses out of the signature quote, the authority's section number is in the
  authority quote.

A value that fails either check is set to null and listed in ``dropped`` with
the reason, so a reader can see what the model claimed and why it was not
kept. The approval-tier guard drops a dollar figure that is only an approval
threshold ("exceeding $900,000 but not exceeding $20 million").

The summary and the market-research summary are the model's own words
(labelled as summaries). Firms named in market research are kept only if the
name appears in the document text.

Long documents are sent whole up to MAX_EXTRACT_CHARS; beyond that the text is
cut at an attachment or page boundary and the record says so (``truncated``,
``chars_sent``, ``chars_total``). Nothing is cut silently.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

from . import llm
from .text import Document, display_quote, locate_quote, normalize

EXTRACT_VERSION = "2026-10-17.extract2"
TOOL_NAME = "record_ja_fields"

# ~100K tokens. Haiku 5.5's price rises 5x above 100K prompt tokens and the
# fields sought are in the J&A itself, which comes first; the few longer
# notices (5 of 9,766 on 2026-10-17) are mostly price lists and appendices.
MAX_EXTRACT_CHARS = 380_000
MIN_FIELD_QUOTE_WORDS = 2

AUTHORITY_PLAIN = [
    "only one responsible source",
    "unusual and compelling urgency",
    "industrial mobilization / engineering, developmental or research capability / expert services",
    "international agreement",
    "authorized or required by statute",
    "national security",
    "public interest",
    "brand name",
    "exception to fair opportunity: only one source capable",
    "exception to fair opportunity: urgency",
    "exception to fair opportunity: logical follow-on",
    "exception to fair opportunity: minimum guarantee",
    "exception to fair opportunity: statute",
    "simplified acquisition: only one source reasonably available",
    "8(a) or small business statute",
    "other",
]

DOC_KINDS = [
    "justification_and_approval",          # FAR 6.303 J&A (other than full and open competition)
    "limited_sources_justification",        # FAR 8.405-6 (GSA schedules)
    "exception_to_fair_opportunity",        # FAR 16.505(b)(2) (orders under multiple-award IDVs)
    "brand_name_justification",
    "simplified_acquisition_sole_source",   # FAR 13.106-1(b) / 13.501 single-source justification
    "other_sole_source_justification",
    "determination_and_findings",
    "notice_or_synopsis",
    "contract_award_or_modification",
    "other",
]

VALUE_BASIS = [
    "total_including_options",
    "base_only",
    "total_options_not_stated",
    "modification_or_increase_amount",
    "ceiling_or_maximum",
    "annual_or_per_period",
    "other",
]

_Q = {"type": ["string", "null"]}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["is_ja", "doc_kind", "doc_kind_quote", "authorities", "summary",
                 "summary_quote", "estimated_value_usd", "value_quote", "value_basis",
                 "value_redacted", "vendor", "vendor_quote", "period_of_performance",
                 "period_quote", "signature_date", "signature_quote", "market_research_summary",
                 "market_research_quote", "other_firms_named", "redacted", "redaction_quote", "notes",
                 "covers_multiple_actions", "additional_vendors"],
    "properties": {
        "is_ja": {"type": "string", "enum": ["yes", "no", "unclear"]},
        "doc_kind": {"type": "string", "enum": DOC_KINDS},
        "doc_kind_quote": _Q,
        "authorities": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["cited", "plain", "quote"],
            "properties": {"cited": {"type": "string"},
                           "plain": {"type": "string", "enum": AUTHORITY_PLAIN},
                           "quote": {"type": "string"}}}},
        "summary": {"type": "string"},
        "summary_quote": _Q,
        "estimated_value_usd": {"type": ["number", "null"]},
        "value_quote": _Q,
        "value_basis": {"type": "string", "enum": VALUE_BASIS + ["no_value"]},
        "value_redacted": {"type": "boolean"},
        "vendor": _Q,
        "vendor_quote": _Q,
        "period_of_performance": _Q,
        "period_quote": _Q,
        "signature_date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "signature_quote": _Q,
        "market_research_summary": _Q,
        "market_research_quote": _Q,
        "other_firms_named": {"type": "array", "items": {"type": "string"}},
        "redacted": {"type": "string", "enum": ["yes", "no"]},
        "redaction_quote": _Q,
        "notes": _Q,
        "covers_multiple_actions": {"type": "boolean"},
        "additional_vendors": {"type": "array", "items": {"type": "string"}},
    },
}

PROMPT = """You are extracting facts from ONE document posted on SAM.gov as a "Justification" notice. Most are federal Justifications & Approvals (J&As) for other than full and open competition, limited-sources justifications (FAR 8.405-6), or exceptions to fair opportunity (FAR 16.505(b)(2)), but some postings are other documents. The text below is every attachment of the notice; scanned pages were machine-transcribed and mark redactions as [REDACTED] and unreadable text as [illegible]. Text may contain OCR errors.

Call the tool """ + TOOL_NAME + """ exactly once with these fields. Use null (or an empty list) whenever the document does not state something. Never guess, never use outside knowledge, never fill in what a redaction hides.

QUOTES. Every *_quote and every authorities[].quote must be copied character-for-character from the document text: one contiguous passage of about 5-40 words that CONTAINS the value you report. Copy OCR errors and odd spellings exactly. You may collapse line breaks to single spaces. Never use "..." and never join separate passages. Never quote the "=== ATTACHMENT" headers or the notice metadata block. If you cannot quote the passage that shows a value, report the value as null.

FIELDS
- is_ja: "yes" if the document is a justification for not using full and open competition or fair opportunity (J&A, limited-sources justification, exception to fair opportunity, brand-name justification, sole-source justification under simplified acquisition, or similar); "no" if it is some other document (an award notice, a contract, a modification without a justification, a D&F only, a synopsis, a price list...); "unclear" if you cannot tell. If the notice has a justification plus other attachments (an order, a statement of work), is_ja is "yes".
- doc_kind: the closest kind, by the authority the justification uses:
  * justification_and_approval: other than full and open competition under FAR 6.302 / RFO 6.103, 10 U.S.C. 3204, 41 U.S.C. 3304 (including "J&A" forms for these).
  * limited_sources_justification: GSA Schedule orders or BPAs under FAR 8.405-6 or GSAR 538.7104 (RFO numbering).
  * exception_to_fair_opportunity: orders under multiple-award contracts under FAR 16.505(b)(2), RFO 16.507-6 / 16.507-7, 10 U.S.C. 3406(c), 41 U.S.C. 4106(c).
  * simplified_acquisition_sole_source: FAR 13.106-1(b), FAR 13.501, 41 U.S.C. 1901 (simplified or commercial procedures).
  * brand_name_justification: a justification whose sole basis is a brand-name requirement.
  * other_sole_source_justification: a justification that fits none of the above (e.g. 8(a), other statutes).
  * the rest for documents that are not justifications.
  doc_kind_quote: the title or heading that shows what the document is.
- authorities: each statutory or regulatory authority the document cites AS THE BASIS for not competing (e.g. "FAR 6.302-1(a)(2)", "10 U.S.C. 3204(a)(1)", "41 U.S.C. 3304(a)(2)", "FAR 16.505(b)(2)(i)(B)", "FAR 8.405-6(a)(1)(i)(B)", "FAR 13.106-1(b)(1)"). cited = the citation as written; plain = the closest category from the list; quote = the passage containing the citation. Do not list authorities that are only mentioned in passing (e.g. FAR 6.303 as the format requirement, approval-level rules in FAR 6.304, pricing rules in FAR 15.4). Most documents have one; list at most three.
- summary: one or two plain-English sentences, written by you, saying what is being bought and why the agency says competition is not being used. summary_quote: the one passage that best states that reason.
- estimated_value_usd: the estimated dollar value of THIS action as stated IN THE JUSTIFICATION, as a number (e.g. 1250000). Do not take it from an attached order, award notice, price list or the SAM metadata. value_quote must contain that figure. value_basis: what the figure covers (total including options; base period only; total where the document does not say whether options are included; the amount of a modification or increase; a ceiling or maximum; an annual or per-period amount; other), or "no_value" when estimated_value_usd is null.
  * Approval thresholds are NOT the value. Approval blocks often say who may approve at a dollar level ("exceeding $900,000 but not exceeding $20 million", "$15 million or less", "over $100 million"); checkboxes such as ">SAT and <= $750K" are also thresholds. If the only dollar figures are thresholds, estimated_value_usd is null.
  * REDACTED VALUES. In scanned pages a redaction is marked [REDACTED]. In typed PDFs a redacted figure usually just DISAPPEARS from the text: you see "is $ ." or "an estimated value of ." or "The IGCE amount is , which includes" with the figure missing, often in files named "..._Redacted.pdf". If the justification's value is redacted or blank in either way, estimated_value_usd is null and value_redacted is true. Otherwise value_redacted is false.
  * If several figures appear, prefer the total estimated value of the action including options; explain in notes if it is ambiguous.
- vendor: the name of the proposed contractor (the firm the agency intends to award to), exactly as written. vendor_quote must contain the name exactly as you give it. null if not named or redacted. If the justification covers several contractors, put the first one named in vendor and the others in additional_vendors (names exactly as written), and set covers_multiple_actions to true.
- covers_multiple_actions: true if the notice's attachments are justifications for different actions (different items, contracts or vendors), or one class justification covering several contracts; then describe the fields' choice in notes. false otherwise (a justification plus its own supporting documents is one action).
- period_of_performance: the period of performance as stated (dates or duration, including options if stated), short. period_quote must contain it.
- signature_date: the LATEST date on which anyone signed or approved the justification (signature blocks, "Digitally signed by ... Date: 2025.03.04", "Date: 3/4/2025"), as YYYY-MM-DD. signature_quote must contain that date copied exactly as the document writes it. Not the date of an attached order or award. null if no signature date is legible.
- market_research_summary: one short sentence, in your words, saying what market research the document describes and what it found (e.g. "A sources-sought notice drew two responses; neither could meet the requirement."). market_research_quote: the passage that best states the result. other_firms_named: names of firms OTHER than the proposed vendor(s) that the document names in connection with market research, responses, quotes or capability (exactly as written); empty list if none.
- redacted: "yes" if the document shows that portions were redacted: [REDACTED] markers, FOIA exemption markers such as "(b)(4)" or "(b)(6)" standing in for withheld text, statements that information was withheld, or blanks where a figure or name was clearly removed (see REDACTED VALUES). Note that "(b)(4)" can also be part of a citation such as "FAR 6.302-1(b)(4)"; that is not a redaction. Else "no". redaction_quote: a passage showing a redaction or a blank.
- notes: anything a reader needs to know to use these fields (several attachments that are different actions, several values, an amended J&A, a value given only per year, illegible pages...). null if nothing.

NOTICE METADATA (from SAM.gov, not part of the document; do not quote it)
<<METADATA>>

DOCUMENT
<<DOCUMENT>>"""


# ---------------------------------------------------------------------------
# Document text for the model
# ---------------------------------------------------------------------------

def model_text(doc: Document, max_chars: int | None = None) -> tuple[str, bool]:
    """The tagger's layout (one '=== ATTACHMENT' header per attachment), cut
    at a page boundary if longer than max_chars (default MAX_EXTRACT_CHARS).
    Returns (text, truncated)."""
    max_chars = MAX_EXTRACT_CHARS if max_chars is None else max_chars
    full = doc.model_text()
    if len(full) <= max_chars:
        return full, False
    parts: list[str] = []
    used = 0
    for fn, pages in doc.attachments():
        header = f"=== ATTACHMENT: {fn} ===\n"
        if used + len(header) > max_chars:
            break
        body: list[str] = []
        used += len(header) + 2
        for p in pages:
            if used + len(p.text) + 2 > max_chars:
                break
            body.append(p.text)
            used += len(p.text) + 2
        parts.append(header + "\n\n".join(body))
        if len(body) < len(pages):
            break
    return "\n\n".join(parts) + "\n\n=== [TEXT CUT HERE: document longer than the extraction limit] ===", True


def _metadata(doc: Document) -> str:
    m = doc.meta
    return "\n".join(f"{k}: {m.get(k)}" for k in ("title", "agency", "sub_tier", "office", "posted_date")
                     if m.get(k))


def build_prompt(doc: Document, text: str) -> str:
    return PROMPT.replace("<<METADATA>>", _metadata(doc)).replace("<<DOCUMENT>>", text)


# ---------------------------------------------------------------------------
# Value-in-quote checks
# ---------------------------------------------------------------------------

_MULT = {"thousand": 1e3, "k": 1e3, "million": 1e6, "mil": 1e6, "m": 1e6, "mm": 1e6,
         "billion": 1e9, "b": 1e9, "bn": 1e9}
_AMOUNT = re.compile(
    r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(thousand|million|billion|mil|bn|mm|k|m|b)?\b"
    r"|\b([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+\.[0-9]+|[0-9]+)\s*(thousand|million|billion)\b"
    r"|\bUSD\s*([0-9][0-9,]*(?:\.[0-9]+)?)",
    re.I)


def amounts_in(text: str) -> list[float]:
    """Dollar amounts written in text: "$1,234,567.89", "$1.2M", "$ 3.5 million",
    "2.4 million", "USD 50,000". OCR'd spaces inside numbers are not repaired."""
    out = []
    for m in _AMOUNT.finditer(normalize(text)):
        num, mult = (m.group(1), m.group(2)) if m.group(1) else (
            (m.group(3), m.group(4)) if m.group(3) else (m.group(5), None))
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        if mult:
            v *= _MULT[mult.lower()]
        out.append(v)
    return out


def value_in_quote(value: float, quote: str) -> bool:
    return any(abs(a - value) <= max(1.0, 0.0005 * value) for a in amounts_in(quote))


# Standard approval thresholds (FAR 6.304 and agency supplements, with the
# 2025 inflation adjustments and earlier values), in dollars.
TIER_AMOUNTS = frozenset({
    700_000, 750_000, 800_000, 900_000, 1_000_000,
    13_500_000, 15_000_000, 20_000_000,
    68_000_000, 75_000_000, 85_500_000, 90_000_000, 93_000_000, 100_000_000, 150_000_000,
})
_TIER_LANG = re.compile(
    r"(not\s+(to\s+)?exceed(ing)?|exceed(s|ing)?|or\s+less|or\s+more|greater\s+than|less\s+than|"
    r"more\s+than|above|below|\bover\b|\bunder\b|up\s+to|threshold|approv)", re.I)


def looks_like_tier(value: float, quote: str) -> bool:
    """A figure that is a standard approval threshold, quoted in threshold
    language, is treated as an approval tier, not the action's value."""
    return any(abs(value - t) < 1 for t in TIER_AMOUNTS) and bool(_TIER_LANG.search(quote))


_alnum = re.compile(r"[^0-9a-z]+")


def _squash(s: str) -> str:
    return _alnum.sub("", normalize(s).casefold())


def name_in(name: str, text: str) -> bool:
    n = _squash(name)
    return len(n) >= 2 and n in _squash(text)


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_D_PATTERNS = [
    (re.compile(r"\b(20\d\d)[./-](\d{1,2})[./-](\d{1,2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](20\d\d|\d\d)\b"), "mdy"),
    (re.compile(r"\b([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(20\d\d)\b"), "Mdy"),
    (re.compile(r"\b(\d{1,2})\s*-?\s*([A-Za-z]{3,9})\.?\s*-?,?\s*(20\d\d|\d\d)\b"), "dMy"),
    (re.compile(r"\b(20\d\d)(\d\d)(\d\d)\b"), "ymd"),
]


def dates_in(text: str) -> set[date]:
    """Calendar dates written in text, in the formats signature blocks use."""
    out: set[date] = set()
    t = normalize(text)
    for rx, kind in _D_PATTERNS:
        for m in rx.finditer(t):
            a, b, c = m.groups()
            try:
                if kind == "ymd":
                    y, mo, d = int(a), int(b), int(c)
                elif kind == "mdy":
                    mo, d, y = int(a), int(b), int(c)
                elif kind == "Mdy":
                    mo, d, y = _MONTHS.get(a[:3].lower()), int(b), int(c)
                else:
                    d, mo, y = int(a), _MONTHS.get(b[:3].lower()), int(c)
                if mo is None:
                    continue
                if y < 100:
                    y += 2000
                out.add(date(y, mo, d))
            except (ValueError, TypeError):
                continue
    return out


_SECTION = re.compile(r"\d+(?:\.\d+)+(?:-\d+)?|\b\d{3,5}\b|8\s*\(\s*a\s*\)")


def authority_in_quote(cited: str, quote: str) -> bool:
    """The citation's section number(s) appear in the quote ("6.302-1",
    "3204", "16.505", "8(a)"). A citation with no number (e.g. "brand name")
    passes on the quote check alone."""
    secs = [_squash(s) for s in _SECTION.findall(normalize(cited))]
    if not secs:
        return True
    q = _squash(quote)
    return any(s in q for s in secs)


# ---------------------------------------------------------------------------
# One document
# ---------------------------------------------------------------------------

def _check_quote(quote, doc, nd):
    return locate_quote(quote, doc, nd, min_words=MIN_FIELD_QUOTE_WORDS, ignore_case=True)


def _qinfo(m) -> dict:
    return {"quote_match": m.match, "quote_pages": m.pages, "quote_on_transcribed_page": m.transcribed}


def verify(raw: dict, doc: Document) -> tuple[dict, list[dict]]:
    """Apply the quote and value-in-quote checks to one model answer.
    Returns (fields, dropped)."""
    nd = doc.normalized()
    dropped: list[dict] = []
    f: dict = {}

    def keep(field, value, quote, extra_ok=True, why_not="value not in its quote"):
        """Return quote info if value+quote pass, else record the drop and return None."""
        if value in (None, "", []):
            return None
        m = _check_quote(quote, doc, nd)
        if not m.verified:
            dropped.append({"field": field, "value": value, "quote": quote,
                            "reason": "quote not found in document" if quote else "no quote"})
            return None
        if not extra_ok:
            dropped.append({"field": field, "value": value, "quote": quote, "reason": why_not})
            return None
        return _qinfo(m)

    f["is_ja"] = raw.get("is_ja")
    f["doc_kind"] = raw.get("doc_kind")
    m = _check_quote(raw.get("doc_kind_quote"), doc, nd)
    f["doc_kind_quote"] = display_quote(raw.get("doc_kind_quote")) if m.verified else None
    f["doc_kind_quote_info"] = _qinfo(m) if m.verified else None

    auths = []
    for a in raw.get("authorities") or []:
        info = keep("authority", a.get("cited"), a.get("quote"),
                    authority_in_quote(a.get("cited") or "", a.get("quote") or ""),
                    "citation's section number not in its quote")
        if info:
            auths.append({"cited": a["cited"], "plain": a.get("plain"),
                          "quote": display_quote(a["quote"]), **info})
    f["authorities"] = auths

    f["summary"] = (raw.get("summary") or "").strip() or None
    m = _check_quote(raw.get("summary_quote"), doc, nd)
    f["summary_quote"] = display_quote(raw.get("summary_quote")) if m.verified else None
    f["summary_quote_info"] = _qinfo(m) if m.verified else None

    v = raw.get("estimated_value_usd")
    vq = raw.get("value_quote")
    f["value_redacted"] = bool(raw.get("value_redacted"))
    f["estimated_value_usd"] = f["value_quote"] = f["value_basis"] = f["value_quote_info"] = None
    if v is not None:
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = None
    if v is not None:
        if v <= 0:
            dropped.append({"field": "estimated_value_usd", "value": v, "quote": vq, "reason": "not positive"})
        elif vq and looks_like_tier(v, vq):
            dropped.append({"field": "estimated_value_usd", "value": v, "quote": vq,
                            "reason": "approval-tier guard: a standard approval threshold quoted in threshold language"})
        else:
            info = keep("estimated_value_usd", v, vq, value_in_quote(v, vq or ""))
            if info:
                f.update(estimated_value_usd=v, value_quote=display_quote(vq),
                         value_basis=None if raw.get("value_basis") == "no_value" else raw.get("value_basis"),
                         value_quote_info=info)
    if f["value_redacted"] and f["estimated_value_usd"] is not None:
        f["value_redacted"] = False   # a stated value wins; the model's flag was inconsistent

    vendor = (raw.get("vendor") or "").strip() or None
    info = keep("vendor", vendor, raw.get("vendor_quote"), name_in(vendor or "", raw.get("vendor_quote") or ""),
                "vendor name not in its quote")
    f["vendor"] = vendor if info else None
    f["vendor_quote"] = display_quote(raw.get("vendor_quote")) if info else None
    f["vendor_quote_info"] = info

    vt = doc.verification_text()
    extra = []
    for name in raw.get("additional_vendors") or []:
        name = (name or "").strip()
        if not name:
            continue
        if name_in(name, vt):
            extra.append(name)
        else:
            dropped.append({"field": "additional_vendors", "value": name, "quote": None,
                            "reason": "vendor name not found in document"})
    f["additional_vendors"] = extra
    f["covers_multiple_actions"] = bool(raw.get("covers_multiple_actions"))

    pop = (raw.get("period_of_performance") or "").strip() or None
    info = keep("period_of_performance", pop, raw.get("period_quote"))
    f["period_of_performance"] = pop if info else None
    f["period_quote"] = display_quote(raw.get("period_quote")) if info else None
    f["period_quote_info"] = info

    sd = raw.get("signature_date")
    sdate = None
    if sd:
        try:
            sdate = date.fromisoformat(sd)
        except ValueError:
            dropped.append({"field": "signature_date", "value": sd, "quote": raw.get("signature_quote"),
                            "reason": "not a YYYY-MM-DD date"})
            sd = None
    info = keep("signature_date", sd, raw.get("signature_quote"),
                sdate is not None and sdate in dates_in(raw.get("signature_quote") or ""),
                "date not in its quote")
    f["signature_date"] = sd if info else None
    f["signature_quote"] = display_quote(raw.get("signature_quote")) if info else None
    f["signature_quote_info"] = info

    f["market_research_summary"] = (raw.get("market_research_summary") or "").strip() or None
    m = _check_quote(raw.get("market_research_quote"), doc, nd)
    f["market_research_quote"] = display_quote(raw.get("market_research_quote")) if m.verified else None
    f["market_research_quote_info"] = _qinfo(m) if m.verified else None
    firms = []
    for name in raw.get("other_firms_named") or []:
        name = (name or "").strip()
        if not name:
            continue
        if name_in(name, vt):
            firms.append(name)
        else:
            dropped.append({"field": "other_firms_named", "value": name, "quote": None,
                            "reason": "firm name not found in document"})
    f["other_firms_named"] = firms

    f["redacted"] = raw.get("redacted")
    m = _check_quote(raw.get("redaction_quote"), doc, nd)
    f["redaction_quote"] = display_quote(raw.get("redaction_quote")) if m.verified else None
    f["redaction_quote_info"] = _qinfo(m) if m.verified else None
    f["notes"] = (raw.get("notes") or "").strip() or None
    return f, dropped


def _n_dropped_factual(dropped: list[dict]) -> int:
    return sum(1 for d in dropped if d["field"] not in ("other_firms_named", "additional_vendors"))


_REDACTED_NAME = re.compile(r"redact", re.I)


def extract_document(doc: Document, *, model: str = llm.DEFAULT_MODEL, call_fn=None,
                     retries: int = 1, effort: str | None = None) -> dict:
    """Extract one record. Raises llm.ModelError if the model call fails, so
    the caller stores nothing and the notice is retried on the next run.

    If any factual value is dropped by the checks, the call is repeated up to
    ``retries`` times and the attempt with the fewest dropped values is kept
    (output varies between identical calls); every attempt's cost is counted."""
    call_fn = call_fn or llm.call
    text, truncated = model_text(doc)
    prompt = build_prompt(doc, text)
    usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0}
    best = None
    for attempt in range(1 + max(0, retries)):
        kw = {"effort": effort} if effort else {}
        raw, u = call_fn(prompt, schema=SCHEMA, tool_name=TOOL_NAME, model=model, max_tokens=16000, **kw)
        if not isinstance(raw, dict):
            raise llm.ModelError(f"model returned {type(raw).__name__}, not an object")
        usage["calls"] += 1
        usage["input_tokens"] += int(u.get("input_tokens") or 0)
        usage["output_tokens"] += int(u.get("output_tokens") or 0)
        usage["cost_usd"] += float(u.get("cost_usd") or 0)
        fields, dropped = verify(raw, doc)
        if best is None or _n_dropped_factual(dropped) < _n_dropped_factual(best[2]):
            best = (raw, fields, dropped, attempt + 1)
        if not _n_dropped_factual(dropped):
            break
    raw, fields, dropped, used = best
    usage["cost_usd"] = round(usage["cost_usd"], 6)
    full_len = len(doc.model_text())
    return {
        "experimental": True,
        "notice_id": doc.notice_id,
        "model": model,
        "extract_version": EXTRACT_VERSION,
        "effort": effort,
        "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "attempt_kept": used,
        "attachments": len(doc.attachments()),
        "pages": len(doc.pages),
        "transcribed_pages": sum(1 for p in doc.pages if p.text_source == "transcription"),
        "chars_total": full_len,
        "chars_sent": len(text),
        "truncated": truncated,
        "redaction_marker_in_text": "[REDACTED" in doc.verification_text(),
        "filename_says_redacted": any(_REDACTED_NAME.search(fn or "") for fn, _ in doc.attachments()),
        "fields": fields,
        "dropped": dropped,
        "raw": raw,
        "usage": usage,
    }


# ---------------------------------------------------------------------------
# Flat rows for a spreadsheet
# ---------------------------------------------------------------------------

def _pages(info: dict | None) -> str | None:
    if not info or not info.get("quote_pages"):
        return None
    return "; ".join(f"{p.get('resource_id')}#p{p.get('page_number') if p.get('page_number') is not None else 'docx'}"
                     + (" (transcribed)" if p.get("text_source") == "transcription" else "")
                     for p in info["quote_pages"])


def _redaction_evidence(rec: dict) -> str | None:
    ev = []
    if rec.get("redaction_marker_in_text"):
        ev.append("[REDACTED] marker in transcribed text")
    if rec.get("filename_says_redacted"):
        ev.append("attachment filename says redacted")
    if rec["fields"].get("redacted") == "yes" and rec["fields"].get("redaction_quote"):
        ev.append("model found a redaction (quote verified)")
    elif rec["fields"].get("redacted") == "yes":
        ev.append("model says redacted (no verified quote)")
    if rec["fields"].get("value_redacted"):
        ev.append("value redacted or blank")
    return "; ".join(ev) or None


def _redaction_detected(rec: dict) -> str:
    """"yes" when any evidence is found. "not detected" is NOT "no": a typed
    PDF redacted with black boxes loses the hidden text without a trace, and
    nothing in the text may show it."""
    return "yes" if _redaction_evidence(rec) else "not detected"


def flat_row(rec: dict, meta: dict) -> dict:
    f = rec["fields"]
    a = f["authorities"]
    return {
        "notice_id": rec["notice_id"],
        "sam_link": meta.get("sam_link") or f"https://sam.gov/opp/{rec['notice_id']}/view",
        "title": meta.get("title"),
        "agency": meta.get("agency"),
        "sub_tier": meta.get("sub_tier"),
        "posted_date": meta.get("posted_date"),
        "is_ja": f["is_ja"],
        "doc_kind": f["doc_kind"],
        "authority_cited": "; ".join(x["cited"] for x in a) or None,
        "authority_plain": "; ".join(dict.fromkeys(x["plain"] for x in a)) or None,
        "authority_quote": " | ".join(x["quote"] for x in a) or None,
        "summary_model_written": f["summary"],
        "reason_quote": f["summary_quote"],
        "estimated_value_usd": f["estimated_value_usd"],
        "value_basis": f["value_basis"],
        "value_redacted": f["value_redacted"],
        "value_quote": f["value_quote"],
        "value_quote_pages": _pages(f["value_quote_info"]),
        "vendor": f["vendor"],
        "vendor_quote": f["vendor_quote"],
        "additional_vendors": "; ".join(f.get("additional_vendors") or []) or None,
        "covers_multiple_actions": f.get("covers_multiple_actions"),
        "period_of_performance": f["period_of_performance"],
        "period_quote": f["period_quote"],
        "signature_date": f["signature_date"],
        "signature_quote": f["signature_quote"],
        "signature_quote_pages": _pages(f["signature_quote_info"]),
        "market_research_summary_model_written": f["market_research_summary"],
        "market_research_quote": f["market_research_quote"],
        "other_firms_named": "; ".join(f["other_firms_named"]) or None,
        "redaction_detected": _redaction_detected(rec),
        "redaction_evidence": _redaction_evidence(rec),
        "redaction_quote": f["redaction_quote"],
        "notes_model_written": f["notes"],
        "values_dropped_by_checks": "; ".join(f"{d['field']}={d['value']!r} ({d['reason']})"
                                              for d in rec["dropped"]) or None,
        "attachments": rec["attachments"],
        "pages": rec["pages"],
        "transcribed_pages": rec["transcribed_pages"],
        "truncated": rec["truncated"],
        "chars_sent": rec["chars_sent"],
        "chars_total": rec["chars_total"],
        "model": rec["model"],
        "extract_version": rec["extract_version"],
        "dataset_revision": rec.get("dataset_revision"),
    }
