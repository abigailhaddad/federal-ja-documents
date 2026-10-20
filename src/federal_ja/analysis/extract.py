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

``people`` lists the people the document names (signers, approvers, points of
contact, vendor staff...), one entry each, plus signature blocks whose name is
visibly redacted. A named entry is kept only if its quote is found in the
document and the name is in its quote, so a name can't be invented. A redacted
entry must have a null name and a quote that shows a redaction marker; an entry
that gives a name while saying it is redacted is dropped. Titles and
organisations are kept only if they appear in the document text. See
``verify_people``.

The small optional facts (a need-by date, a stated period-of-performance cap,
the incumbent contract number, the market-research methods, whether the
redactions are substantive, bridge/extension, the prior-extension count,
contract types, letter contract, funding sources, the urgency trigger) come
back in ONE generic ``facts`` list of {kind, value, quote} strings, and the
code turns it into the same flat fields as before (``_verify_facts``). Why a
list: strict tool use compiles the schema into a grammar, and the extract3
schema was already at the compiled-grammar size limit; a property per field
was rejected (see FACT_KINDS).

Long documents are sent whole up to MAX_EXTRACT_CHARS; beyond that the text is
cut at an attachment or page boundary and the record says so (``truncated``,
``chars_sent``, ``chars_total``). Nothing is cut silently.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone

from . import llm
from .promptfile import CONFIG
from .text import Document, display_quote, locate_quote, normalize

EXTRACT_VERSION = CONFIG["extraction"]["version"]  # prompt (prompts.yaml); see TRUNCATION_VERSION for long-document layout
TRUNCATION_VERSION = "2026-10-18.trunc2"
# The deterministic checks (quote search, value-in-quote parsers, tier
# guard) have their own version: they can be re-applied to stored model
# answers without new model calls (``extract --reverify``).
CHECKS_VERSION = "2026-10-20.checks5"   # checks4: people; checks5: the facts list (prompt extract5)
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

PEOPLE_ROLES = [
    "signer_or_approver",
    "contracting_officer",
    "competition_advocate",
    "requiring_or_technical_official",
    "legal_reviewer",
    "vendor_employee",
    "other",
]
MR_METHODS = [
    "sources_sought", "rfi", "notice_of_intent", "industry_day", "sam_search",
    "gsa_elibrary_or_advantage", "sba_dsbs", "other_database_search", "vendor_contact",
    "internet_search", "procurement_history", "other",
]

URGENCY_TRIGGERS = ["congressional_or_oversight_deadline", "bid_protest", "contractor_default_or_termination",
                    "emergency_or_disaster", "agency_planning_delay", "other"]

# The roles of signature and approval blocks: the narrower scope
# ``people_signature_block`` in the flat row.
SIGNATURE_ROLES = frozenset({"signer_or_approver", "contracting_officer", "competition_advocate",
                             "legal_reviewer"})

_Q = {"type": ["string", "null"]}

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["is_ja", "doc_kind", "doc_kind_quote", "authorities", "summary",
                 "summary_quote", "estimated_value_usd", "value_quote", "value_basis",
                 "value_redacted", "vendor", "vendor_quote", "period_of_performance",
                 "period_quote", "signature_date", "signature_quote", "market_research_summary",
                 "market_research_quote", "other_firms_named", "redacted", "redaction_quote", "notes",
                 "covers_multiple_actions", "additional_vendors", "covered_piids", "facts", "people"],
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
        # No string|null unions below: strict tool use allows at most 16
        # union-typed parameters and the fields above use 15.
        "covered_piids": {"type": "array", "items": {"type": "string"}},
        # One generic list for the small optional facts (FACT_KINDS), plain
        # strings with no enums: the code checks kinds, codes and numbers.
        "facts": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["kind", "value", "quote"],
            "properties": {"kind": {"type": "string"}, "value": {"type": "string"},
                           "quote": {"type": "string"}}}},
        "people": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["name", "name_quote", "title", "organization", "role_in_document",
                         "description", "redacted"],
            "properties": {"name": _Q,
                           "name_quote": {"type": "string"},
                           # plain strings ("" = none printed), not string|null:
                           # strict tool use allows at most 16 union-typed
                           # parameters in a schema, and the other fields use 15.
                           "title": {"type": "string"},
                           "organization": {"type": "string"},
                           "role_in_document": {"type": "string", "enum": PEOPLE_ROLES},
                           "description": {"type": "string"},
                           "redacted": {"type": "boolean"}}}},
    },
}

# The kinds in the ``facts`` list, and the flat field each one fills. On
# 2026-10-20 the extract3 schema was at the size limit for a compiled grammar
# ("The compiled grammar is too large", 400): adding the trial fields to it as
# a property per field (with or without enums), or even as one extra generic
# list, was rejected, and the trial (extract4) asked them in a second call.
# extract5 makes room by moving the extract3 fields that were already small
# quote-paired items (need_by, stated_pop_limit, incumbent_piid,
# market_research_methods, redaction_significant) into the same generic list
# as the trial fields, so one call returns everything. The flat fields keep
# their extract3 / extract4 names and shapes.
FACT_FIELD = {
    "need_by": "need_by_date",
    "stated_pop_limit": "stated_pop_limit_days",
    "incumbent_piid": "incumbent_piid",
    "market_research_method": "market_research_methods",
    "redaction_significant": "redaction_significant",
    "bridge": "bridge_or_extension",
    "prior_extension_count": "prior_extension_count",
    "letter_contract": "is_letter_contract",
    "contract_type": "contract_types",
    "funding_source": "funding_sources",
    "urgency_trigger": "urgency_trigger",
}
FACT_KINDS = tuple(FACT_FIELD)
# Kinds that may appear more than once; for the others the first entry is
# kept and any later one is listed in ``dropped``.
_MULTI_KINDS = frozenset({"market_research_method", "contract_type", "funding_source"})

PROMPT = CONFIG["extraction"]["prompt"]  # text in prompts.yaml

# Prompt caching: the instructions (everything before the per-notice section)
# are identical on every call and are sent as a cached block; the split falls
# at the start of the "NOTICE METADATA" heading, so the two parts joined are
# byte-for-byte the prompt in prompts.yaml. The placeholders must all come
# after the split, or the cached part would differ by document.
_SPLIT = PROMPT.rfind("NOTICE METADATA", 0, PROMPT.find("<<METADATA>>"))
assert _SPLIT > 0 and "<<" not in PROMPT[:_SPLIT], "extraction prompt layout changed; fix the cache split"
PROMPT_CACHED, PROMPT_PER_DOC = PROMPT[:_SPLIT], PROMPT[_SPLIT:]


# ---------------------------------------------------------------------------
# Document text for the model
# ---------------------------------------------------------------------------

_JA_NAME = re.compile(r"justif|j\s*&\s*a|\bja\b|_ja\b|\bja_|sole.?source|single.?source|\blsj\b|jofoc|jotfoc|\befo\b|"
                      r"exception|limited.?source|brand.?name", re.I)


def model_text(doc: Document, max_chars: int | None = None) -> tuple[str, bool]:
    """The tagger's layout (one '=== ATTACHMENT' header per attachment).
    Returns (text, truncated).

    When the whole notice is longer than max_chars (default
    MAX_EXTRACT_CHARS), attachments are reordered before cutting: those whose
    filename looks like a justification first, then the rest from shortest
    to longest, and the text is cut at a page boundary. (In the first full
    run one notice's J&A came after a 400-page technical manual and was cut
    off entirely.) The layout of documents under the limit is unchanged."""
    max_chars = MAX_EXTRACT_CHARS if max_chars is None else max_chars
    full = doc.model_text()
    if len(full) <= max_chars:
        return full, False
    atts = doc.attachments()
    order = sorted(range(len(atts)), key=lambda i: (
        0 if _JA_NAME.search(atts[i][0] or "") else 1,
        sum(len(p.text) for p in atts[i][1]), i))
    parts: list[str] = []
    used = 0
    for i in order:
        fn, pages = atts[i]
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


def build_prompt(doc: Document, text: str) -> tuple[str, str]:
    """(cached instructions, per-notice part); llm.prompt_text joins them into
    the whole prompt."""
    return PROMPT_CACHED, PROMPT_PER_DOC.replace("<<METADATA>>", _metadata(doc)).replace("<<DOCUMENT>>", text)


# ---------------------------------------------------------------------------
# Value-in-quote checks
# ---------------------------------------------------------------------------

_MULT = {"thousand": 1e3, "k": 1e3, "million": 1e6, "mil": 1e6, "m": 1e6, "mm": 1e6,
         "billion": 1e9, "b": 1e9, "bn": 1e9}
_AMOUNT = re.compile(
    r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*(thousand|million|billion|mil|bn|mm|k|m|b)?\b"
    r"|\b([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+\.[0-9]+|[0-9]+)\s*(thousand|million|billion)\b"
    r"|\bUSD\s*([0-9][0-9,]*(?:\.[0-9]+)?)"
    # a bare comma-grouped figure ("Estimated Value | 1,950,000"), as tables write it
    r"|(?<![0-9.,$])([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{2})?)(?![0-9,])",
    re.I)


# OCR'd stray spaces around a thousands comma or inside the leading group
# ("$ 11, 640", "$5 ,180.00", "$1 70,584.05").
_NUM_SPACE = re.compile(r"(?<=\d)\s+(?=,\d)|(?<=\d,)\s+(?=\d{3}\b)|(?<=\$\d)\s+(?=\d{1,2},\d{3})")


def amounts_in(text: str) -> list[float]:
    """Dollar amounts written in text: "$1,234,567.89", "$1.2M", "$ 3.5 million",
    "2.4 million", "USD 50,000", with OCR'd spaces around commas repaired."""
    out = []
    for m in _AMOUNT.finditer(_NUM_SPACE.sub("", normalize(text))):
        if m.group(1):
            num, mult = m.group(1), m.group(2)
        elif m.group(3):
            num, mult = m.group(3), m.group(4)
        else:
            num, mult = m.group(5) or m.group(6), None
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
# Threshold language: an approval or review word, or a dollar RANGE ("or
# less", "exceeding $X but not exceeding $Y"). A bare "not to exceed" is NOT
# threshold language: "a not-to-exceed value of $750,000" is the action's
# value. (The first version of this guard also matched "over", "below" and
# "not to exceed"; on the full run 8 of its 9 drops were real values.)
_TIER_LANG = re.compile(
    r"approv|competition\s+advocate|head\s+of\s+(the\s+)?(contracting|agency)|senior\s+procurement|"
    r"\bHCA\b|\bSPE\b|threshold|\blevels?\b|or\s+less|or\s+more|less\s+than|greater\s+than|"
    r"exceed(s|ing)?\s+\$\s*[0-9.,]+\s*(k|m|million|billion)?,?\s+(but|and)\s+(does\s+)?not|"
    r"\bover\s+\$\s*[0-9.,]+\s*(k|m|million|billion)?,?\s+(but|and)\b", re.I)


def looks_like_tier(value: float, quote: str) -> bool:
    """A figure that is a standard approval threshold, quoted in approval
    or range language, is treated as an approval tier, not the value."""
    return any(abs(value - t) < 1 for t in TIER_AMOUNTS) and bool(_TIER_LANG.search(quote))


_alnum = re.compile(r"[^0-9a-z]+")


def _squash(s: str) -> str:
    return _alnum.sub("", normalize(s).casefold())


def name_in(name: str, text: str) -> bool:
    n = _squash(name)
    return len(n) >= 2 and n in _squash(text)


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
# Digit runs are delimited by (?<!\d) / (?!\d), not \b, because signature
# stamps glue dates to times and names ("2025.03.2513:35:44",
# "9/24/2026Christopher").
_D_PATTERNS = [
    (re.compile(r"(?<!\d)(20\d\d)[./ -](\d{1,2})[./ -](\d{1,2})(?=\d{2}:|T\d|[^0-9]|$)"), "ymd"),
    (re.compile(r"(?<![\d.])(\d{1,2})[./-](\d{1,2})[./-](20\d\d|\d\d)(?!\d)"), "mdy"),
    (re.compile(r"\b([A-Za-z]{3,9})\.?\s*(\d{1,2})(?:st|nd|rd|th)?,?\s*(20\d\d)(?!\d)"), "Mdy"),
    (re.compile(r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)?\s*-?\s*([A-Za-z]{3,9})\.?\s*-?,?\s*(20\d\d|\d\d)(?!\d)"), "dMy"),
    (re.compile(r"(?<!\d)(20\d\d)(\d\d)(\d\d)(?!\d)"), "ymd"),
    (re.compile(r"(?<!\d)(20\d\d)\s*([A-Za-z]{3,9})\.?\s*(\d{1,2})(?!\d)"), "yMd"),
]


_FILL = re.compile(r"_+")
_DIGIT_SEP = re.compile(r"(?<=\d)\s*([./-])\s*(?=\d)")


def dates_in(text: str) -> set[date]:
    """Calendar dates written in text, in the formats signature blocks use.
    Fill-in underscores ("_09/18/2026___") and stray spaces around date
    separators ("9/ 18/2026", "2026 .05.29") are removed first."""
    out: set[date] = set()
    t = _DIGIT_SEP.sub(r"\1", _FILL.sub(" ", normalize(text)))
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
                elif kind == "yMd":
                    y, mo, d = int(a), _MONTHS.get(b[:3].lower()), int(c)
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


_NUM_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split())}
_NUM_WORDS.update({"thirty": 30, "forty-five": 45, "sixty": 60, "ninety": 90, "twenty-four": 24,
                   "thirty-six": 36, "one hundred twenty": 120, "one hundred eighty": 180})
_UNIT_DAYS = {"day": (1,), "week": (7,), "month": (30, 30.44, 31), "year": (365, 366)}
_DURATION = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?|" + "|".join(sorted((re.escape(w) for w in _NUM_WORDS), key=len, reverse=True))
    + r")\s*(?:\(\s*\d+\s*\)\s*)?-?\s*(?:calendar\s+)?(day|week|month|year)s?\b", re.I)


def durations_in(text: str) -> list[float]:
    """Durations written in text, in days ("six (6) months" -> 180, 182.6
    and 186; "one year" -> 365, 366; "90 days" -> 90)."""
    out: list[float] = []
    for m in _DURATION.finditer(normalize(text)):
        n = m.group(1).lower()
        num = float(n) if n[0].isdigit() else _NUM_WORDS[n]
        out.extend(num * f for f in _UNIT_DAYS[m.group(2).lower()])
    return out


def days_in_quote(days: int, quote: str) -> bool:
    return any(abs(d - days) <= max(2.0, 0.03 * d) for d in durations_in(quote))


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


# A name visibly withheld: the transcription's [REDACTED] marker, a FOIA
# exemption for personal privacy or law-enforcement records ((b)(6),
# (b)(7)(C), (b)(3)), or the word itself. "(b)(4)" (trade secrets) is left out:
# it is not used for names, and FAR 6.302-1(b)(4) is a citation.
REDACTION_MARK = re.compile(r"\[\s*REDACTED|\(\s*b\s*\)\s*\(\s*[367]\s*\)|\bredact(ed|ion)\b|\bwithheld\b", re.I)
# A digital-signature stamp's name form, "LAST.FIRST.M.1234567890" (DoD CAC
# certificates). It is kept as written; the flag says it is a stamp form.
_STAMP_FORM = re.compile(r"^[A-Z][A-Z'-]*(\.[A-Z][A-Z'-]*)+\.?(\d{6,})?$")


def verify_people(raw_people, doc: Document, nd: str, vt: str) -> tuple[list[dict], list[dict]]:
    """Check the model's ``people`` entries. Returns (kept, dropped).

    A named entry is kept only if its name_quote is found in the document
    and the name is visible in that quote (letters and digits compared, so a
    name broken across a line, or a stamp's dots, still match). A redacted
    entry must have no name and a quote showing a redaction marker. The
    title and organisation are cleared (and recorded as dropped) if they do
    not appear in the document. A second entry with the same name as an
    earlier one is dropped as a duplicate."""
    kept: list[dict] = []
    dropped: list[dict] = []
    seen: set[str] = set()
    for p in raw_people or []:
        name = _blank(p.get("name"))
        quote = p.get("name_quote")
        redacted = bool(p.get("redacted"))

        def drop(reason, value=None):
            dropped.append({"field": "people", "value": value if value is not None else (name or p.get("title")),
                            "quote": quote, "reason": reason})

        m = _check_quote(quote, doc, nd)
        if not m.verified:
            drop("quote not found in document" if quote else "no quote")
            continue
        if redacted:
            if name:
                drop("name given for an entry marked redacted")
                continue
            if not REDACTION_MARK.search(normalize(quote or "")):
                drop("redacted entry's quote shows no redaction marker")
                continue
        else:
            if not name:
                drop("no name, and not marked redacted")
                continue
            if REDACTION_MARK.search(name):
                drop("name is a redaction marker")
                continue
            if not name_in(name, quote or ""):
                drop("name not in its quote")
                continue
            key = _squash(name)
            if key in seen:
                drop("duplicate of an earlier entry")
                continue
            seen.add(key)
        entry = {"name": name, "name_quote": display_quote(quote), "redacted": redacted,
                 "name_is_signature_stamp": bool(name and _STAMP_FORM.match(name))}
        for k in ("title", "organization"):
            v = _blank(p.get(k))
            if v and not name_in(v, vt):
                dropped.append({"field": f"people.{k}", "value": v, "quote": None,
                                "reason": f"{k} not found in document"})
                v = None
            entry[k] = v
        role = p.get("role_in_document")
        entry["role_in_document"] = role if role in PEOPLE_ROLES else "other"
        entry["description"] = (p.get("description") or "").strip() or None
        entry["name_quote_info"] = _qinfo(m)
        kept.append(entry)
    return kept, dropped


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

    piids, seen = [], set()
    for x in raw.get("covered_piids") or []:
        x = _blank(x)
        if not x or _squash(x) in seen:
            continue
        if len(_squash(x)) >= 4 and name_in(x, vt):
            piids.append(x)
            seen.add(_squash(x))
        else:
            dropped.append({"field": "covered_piids", "value": x, "quote": None,
                            "reason": "contract number not found in document"})
    f["covered_piids"] = piids

    _verify_facts(raw, doc, nd, f, dropped)

    people, pdrop = verify_people(raw.get("people"), doc, nd, vt)
    f["people"] = people
    dropped.extend(pdrop)
    return f, dropped


# The model sometimes writes the empty string the prompt asks for as a
# literal pair of quote marks ('""'), or as "null"/"none"/"N/A". Seen on 41 of
# 78 incumbent_piid answers in the 2026-10-20 validation runs.
_PLACEHOLDER = frozenset({'""', "''", "null", "none", "n/a", "na", "not stated", "not applicable"})


def _blank(v) -> str | None:
    """A string value, or None for empty, whitespace or a placeholder."""
    if not isinstance(v, str):
        return None
    v = v.strip()
    return None if not v or v.casefold() in _PLACEHOLDER else v


def _first(v):
    return v[0] if isinstance(v, list) and v and isinstance(v[0], dict) else {}


_LETTER = re.compile(r"letter\s+contract|undefinitized|\bUCAs?\b", re.I)
_COUNT_WORD = re.compile(r"\d|\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth|"
                         r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|"
                         r"once|twice|several|multiple)\b", re.I)


def _verify_facts(raw: dict, doc: Document, nd: str, f: dict, dropped: list[dict]) -> None:
    """Turn the ``facts`` list into the flat fields of FACT_FIELD, with the
    same checks each field had as a property of its own (extract3) or in the
    trial's second call (extract4). Every value the code does not keep (an
    unknown kind or code, a second entry for a single-valued kind, a number
    that does not parse, a failed quote) is listed in ``dropped``."""
    by: dict[str, list[dict]] = {}
    for x in raw.get("facts") or []:
        if not isinstance(x, dict):
            continue
        kind = (_blank(x.get("kind")) or "").lower()
        if kind not in FACT_FIELD:
            dropped.append({"field": "facts", "value": x.get("kind"), "quote": _blank(x.get("quote")),
                            "reason": "unknown fact kind"})
            continue
        if by.get(kind) and kind not in _MULTI_KINDS:
            dropped.append({"field": FACT_FIELD[kind], "value": x.get("value"), "quote": _blank(x.get("quote")),
                            "reason": "a second entry for a single-valued fact; the first is kept"})
            continue
        by.setdefault(kind, []).append(x)

    def one(kind):
        x = (by.get(kind) or [{}])[0]
        return _blank(x.get("value")), _blank(x.get("quote"))

    def bad(field, value, quote, reason):
        dropped.append({"field": field, "value": value, "quote": quote, "reason": reason})

    def check(field, value, quote, ok=True, why=""):
        """Quote info if the quote is found and ``ok``; else record the drop."""
        if value in (None, "", []):
            return None
        m = _check_quote(quote, doc, nd)
        if not m.verified:
            bad(field, value, quote, "quote not found in document" if quote else "no quote")
            return None
        if not ok:
            bad(field, value, quote, why)
            return None
        return _qinfo(m)

    # need_by_date: the date must parse out of its quote
    d, q = one("need_by")
    dd = None
    if d:
        try:
            dd = date.fromisoformat(d)
        except ValueError:
            bad("need_by_date", d, q, "not a YYYY-MM-DD date")
            d = None
    info = check("need_by_date", d, q, dd is not None and dd in dates_in(q or ""), "date not in its quote")
    f["need_by_date"] = d if info else None
    f["need_by_quote"] = display_quote(q) if info else None
    f["need_by_quote_info"] = info

    # stated_pop_limit_days: the duration must be in its quote
    v, q = one("stated_pop_limit")
    days = None
    if v is not None:
        days = int(v) if v.isdigit() else None
        if not days:
            bad("stated_pop_limit_days", v, q, "not a positive whole number")
            days = None
    info = check("stated_pop_limit_days", days, q, days is not None and days_in_quote(days, q or ""),
                 "duration not in its quote")
    f["stated_pop_limit_days"] = days if info else None
    f["stated_pop_limit_quote"] = display_quote(q) if info else None
    f["stated_pop_limit_quote_info"] = info

    ip, q = one("incumbent_piid")
    info = check("incumbent_piid", ip, q, name_in(ip or "", q or ""), "contract number not in its quote")
    f["incumbent_piid"] = ip if info else None
    f["incumbent_piid_quote"] = display_quote(q) if info else None
    f["incumbent_piid_quote_info"] = info

    # market_research_methods: a known code, once each; nothing checks that
    # the quote shows that particular method
    methods, seen_m = [], set()
    for x in by.get("market_research_method") or []:
        code, q = (_blank(x.get("value")) or "").lower(), _blank(x.get("quote"))
        if code not in MR_METHODS:
            bad("market_research_methods", x.get("value"), q, "not a known method code")
            continue
        if code in seen_m:
            continue
        info = check("market_research_methods", code, q)
        if info:
            methods.append({"method": code, "quote": display_quote(q), **info})
            seen_m.add(code)
    f["market_research_methods"] = methods

    # redaction_significant: "yes" needs a verified quote, else "unclear"
    rs, q = one("redaction_significant")
    rs = (rs or "").lower() or None
    if rs is not None and rs not in ("yes", "no", "unclear"):
        bad("redaction_significant", rs, q, "not yes, no or unclear")
        rs = None
    f["redaction_significant"] = rs
    f["redaction_significant_quote"] = f["redaction_significant_quote_info"] = None
    if rs == "yes":
        info = check("redaction_significant", rs, q)
        if info:
            f["redaction_significant_quote"] = display_quote(q)
            f["redaction_significant_quote_info"] = info
        else:
            f["redaction_significant"] = "unclear"   # the drop says why

    def flag(kind, allowed, yes_ok=lambda q: True, why=""):
        """A yes/no(/unclear) fact: "yes" needs a verified quote that passes yes_ok."""
        field = FACT_FIELD[kind]
        v, q = one(kind)
        v = (v or "").lower()
        if not v:
            return None, None
        if v not in allowed:
            bad(field, v, q, "not one of " + ", ".join(allowed))
            return None, None
        if v != "yes":
            return v, None
        if check(field, v, q, yes_ok(q or ""), why):
            return "yes", display_quote(q)
        return None, None

    f["bridge_or_extension"], f["bridge_or_extension_quote"] = flag("bridge", ("yes", "no", "unclear"))
    f["is_letter_contract"], f["letter_contract_quote"] = flag(
        "letter_contract", ("yes", "no"), lambda q: bool(_LETTER.search(normalize(q))),
        "quote does not say letter contract or undefinitized")

    # prior_extension_count: only a number the document states
    f["prior_extension_count"] = f["prior_extension_count_quote"] = None
    v, q = one("prior_extension_count")
    if v is not None:
        n = int(v) if v.isdigit() else None
        if n is None:
            bad("prior_extension_count", v, q, "not a whole number")
        elif check("prior_extension_count", n, q, bool(_COUNT_WORD.search(normalize(q or ""))),
                   "no count in its quote") is not None:
            f["prior_extension_count"], f["prior_extension_count_quote"] = n, display_quote(q)

    def items(kind, key):
        field = FACT_FIELD[kind]
        out, seen = [], set()
        for x in by.get(kind) or []:
            v, q = _blank(x.get("value")), _blank(x.get("quote"))
            if not v or _squash(v) in seen:
                continue
            if check(field, v, q, name_in(v, q or ""), f"{key} not in its quote"):
                seen.add(_squash(v))
                out.append({key: v, "quote": display_quote(q)})
        return out

    f["contract_types"] = items("contract_type", "stated")
    f["funding_sources"] = items("funding_source", "name")

    t, q = one("urgency_trigger")
    t = (t or "").lower()
    f["urgency_trigger"] = f["urgency_trigger_quote"] = None
    if t and t not in URGENCY_TRIGGERS:
        bad("urgency_trigger", t, q, "not a known trigger code")
    elif t and check("urgency_trigger", t, q):
        f["urgency_trigger"], f["urgency_trigger_quote"] = t, display_quote(q)


def shape_errors(raw: dict) -> list[str]:
    """Shape problems in a model answer. Strict tool use should prevent
    them, but a tool offered with tool_choice "auto" has returned, e.g., a
    list field as a JSON string; such an answer is treated as a failed call."""
    out = []
    for k in ("authorities", "other_firms_named", "additional_vendors", "people", "covered_piids", "facts"):
        v = raw.get(k)
        if v is not None and not isinstance(v, list):
            out.append(f"{k} is {type(v).__name__}")
    for a in raw.get("authorities") or []:
        if not isinstance(a, dict):
            out.append(f"authorities item is {type(a).__name__}")
            break
    for k in ("people", "facts"):
        if isinstance(raw.get(k), list) and any(not isinstance(x, dict) for x in raw[k]):
            out.append(f"{k} item is not an object")
    for k in ("other_firms_named", "additional_vendors"):
        if isinstance(raw.get(k), list) and any(not isinstance(x, (str, type(None))) for x in raw[k]):
            out.append(f"{k} item is not a string")
    v = raw.get("estimated_value_usd")
    if v is not None and not isinstance(v, (int, float)):
        out.append(f"estimated_value_usd is {type(v).__name__}")
    return out


# People are left out of the retry rule, like the name lists: a dropped
# person must not trigger a second call that could change the other fields
# (and double the cost).
_NOT_RETRIED = ("other_firms_named", "additional_vendors", "people", "people.title", "people.organization",
                # covered_piids and every field from the facts list, for the same reason
                "covered_piids", "facts", *FACT_FIELD.values())


def _n_dropped_factual(dropped: list[dict]) -> int:
    return sum(1 for d in dropped if d["field"] not in _NOT_RETRIED)


_REDACTED_NAME = re.compile(r"redact", re.I)

# Some PDFs' text layers come out letter-shifted by three code points
# ("7KH" for "The", "WKH" for "the"), a font-encoding fault the collection's
# garbage check does not catch. The model can't read those passages and the
# fields may be missing or wrong; the record flags it. Three hits of these
# shifted common words mark a document as partly or wholly garbled (checked
# 2026-10-18 against every document with 1-15 hits: 1-2 hits are sometimes
# noise; at 3+ every one inspected had shifted passages). "IRU" ("for") is
# left out: it is also an acronym (Indefeasible Right of Use).
_SHIFTED = re.compile(r"7KH|WKH|DQG|WKDW|ZLWK")
GARBLED_MIN_HITS = 3


def garbled_text_layer(doc: Document) -> bool:
    return len(_SHIFTED.findall(doc.verification_text())) >= GARBLED_MIN_HITS


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
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 0, "cost_usd": 0.0, "calls": 0}
    best = None
    for attempt in range(1 + max(0, retries)):
        kw = {"effort": effort} if effort else {}
        raw, u = call_fn(prompt, schema=SCHEMA, tool_name=TOOL_NAME, model=model, max_tokens=16000, **kw)
        if not isinstance(raw, dict):
            raise llm.ModelError(f"model returned {type(raw).__name__}, not an object")
        bad = shape_errors(raw)
        if bad:
            raise llm.ModelError(f"model answer does not match the schema: {bad}")
        usage["calls"] += 1
        usage["input_tokens"] += int(u.get("input_tokens") or 0)
        usage["output_tokens"] += int(u.get("output_tokens") or 0)
        usage["cache_creation_input_tokens"] += int(u.get("cache_creation_input_tokens") or 0)
        usage["cache_read_input_tokens"] += int(u.get("cache_read_input_tokens") or 0)
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
        "checks_version": CHECKS_VERSION,
        "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "attempt_kept": used,
        "attachments": len(doc.attachments()),
        "pages": len(doc.pages),
        "transcribed_pages": sum(1 for p in doc.pages if p.text_source == "transcription"),
        "chars_total": full_len,
        "chars_sent": len(text),
        "truncated": truncated,
        "truncation_version": TRUNCATION_VERSION if truncated else None,
        "redaction_marker_in_text": "[REDACTED" in doc.verification_text(),
        "filename_says_redacted": any(_REDACTED_NAME.search(fn or "") for fn, _ in doc.attachments()),
        "garbled_text_layer": garbled_text_layer(doc),
        "fields": fields,
        "dropped": dropped,
        "raw": raw,
        "usage": usage,
    }


def reverify(rec: dict, doc: Document) -> dict:
    """Re-apply the current checks to a stored record's model answer (no
    model call). Returns the updated record.

    The checks read the facts from ``raw["facts"]``. An extract3 answer
    (need_by, incumbent_piid... as properties of their own) re-verified with
    them would lose those fields; none is published (the published table is
    extract2, which has neither), so no converter is kept."""
    fields, dropped = verify(rec["raw"], doc)
    return {**rec, "fields": fields, "dropped": dropped, "checks_version": CHECKS_VERSION,
            "filename_says_redacted": any(_REDACTED_NAME.search(fn or "") for fn, _ in doc.attachments()),
            "garbled_text_layer": garbled_text_layer(doc)}


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
    if any(p.get("redacted") for p in rec["fields"].get("people") or []):
        ev.append("a person's name redacted (quote verified)")
    return "; ".join(ev) or None


def _redaction_detected(rec: dict) -> str:
    """"yes" when any evidence is found. "not detected" is NOT "no": a typed
    PDF redacted with black boxes loses the hidden text without a trace, and
    nothing in the text may show it."""
    return "yes" if _redaction_evidence(rec) else "not detected"


def _person_label(p: dict) -> str:
    who = p["name"] if p["name"] else "[name redacted]"
    extra = ", ".join(x for x in (p.get("title"), p.get("organization")) if x)
    return f"{who} ({extra})" if extra else who


def flat_row(rec: dict, meta: dict) -> dict:
    f = rec["fields"]
    a = f["authorities"]
    people = f.get("people") or []
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
        "need_by_date": f.get("need_by_date"),
        "need_by_quote": f.get("need_by_quote"),
        "stated_pop_limit_days": f.get("stated_pop_limit_days"),
        "stated_pop_limit_quote": f.get("stated_pop_limit_quote"),
        "covered_piids": "; ".join(f.get("covered_piids") or []) or None,
        "incumbent_piid": f.get("incumbent_piid"),
        "incumbent_piid_quote": f.get("incumbent_piid_quote"),
        "market_research_methods": "; ".join(m["method"] for m in f.get("market_research_methods") or []) or None,
        "redaction_significant": f.get("redaction_significant"),
        "redaction_significant_quote": f.get("redaction_significant_quote"),
        # from the facts list too (first asked in the extract4 trial)
        "bridge_or_extension": f.get("bridge_or_extension"),
        "bridge_or_extension_quote": f.get("bridge_or_extension_quote"),
        "prior_extension_count": f.get("prior_extension_count"),
        "prior_extension_count_quote": f.get("prior_extension_count_quote"),
        "contract_types": "; ".join(c["stated"] for c in f.get("contract_types") or []) or None,
        "is_letter_contract": f.get("is_letter_contract"),
        "letter_contract_quote": f.get("letter_contract_quote"),
        "funding_sources": "; ".join(c["name"] for c in f.get("funding_sources") or []) or None,
        "urgency_trigger": f.get("urgency_trigger"),
        "urgency_trigger_quote": f.get("urgency_trigger_quote"),
        "people_named": "; ".join(_person_label(p) for p in people if not p["redacted"]) or None,
        "people_signature_block": "; ".join(_person_label(p) for p in people
                                            if p["role_in_document"] in SIGNATURE_ROLES) or None,
        "people_redacted_blocks": sum(1 for p in people if p["redacted"]),
        "people_json": json.dumps([{k: p.get(k) for k in ("name", "title", "organization", "role_in_document",
                                                          "redacted", "name_is_signature_stamp", "name_quote",
                                                          "description")}
                                   for p in people], ensure_ascii=False) if people else None,
        "values_dropped_by_checks": "; ".join(f"{d['field']}={d['value']!r} ({d['reason']})"
                                              for d in rec["dropped"]) or None,
        "attachments": rec["attachments"],
        "pages": rec["pages"],
        "transcribed_pages": rec["transcribed_pages"],
        "truncated": rec["truncated"],
        "garbled_text_layer": rec.get("garbled_text_layer"),
        "chars_sent": rec["chars_sent"],
        "chars_total": rec["chars_total"],
        "model": rec["model"],
        "extract_version": rec["extract_version"],
        "checks_version": rec.get("checks_version"),
        "dataset_revision": rec.get("dataset_revision"),
    }
