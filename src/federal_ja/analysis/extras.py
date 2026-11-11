"""A second, smaller model call per notice for facts the extraction does not
ask for. EXPERIMENTAL.

Why a second call and not more fields in extract.py (2026-11-10): adding to
the extraction prompt means a new EXTRACT_VERSION, and a new version makes
every stored record out of date, so the whole table is extracted again and
every published value is replaced by a fresh answer (in the extract6
validation, two runs of the same prompt on 49 notices agreed on every dollar
value but differed on the vendor's spelling in 4 and on the summary in all
49). This call has its own version (EXTRAS_VERSION), its own records
(extras.jsonl) and its own table, so nothing already published changes.

What it asks for (prompts.yaml, ``extras``), one generic ``facts`` list of
{kind, value, quote}:

* ceiling changes: increase_amount, prior_ceiling, new_ceiling;
* not_to_exceed, when the document gives a maximum beside a lower estimate;
* unit_price and quantity, when the action is priced per unit;
* incumbent_vendor;
* approved_source and interested_source, one entry per firm (the two tables
  on Defense Logistics Agency forms);
* barrier_action and source_approval_path, what the document says will be
  done to open competition and how a new source can apply;
* data_rights_basis, cost_to_compete and delay_to_compete: why technical
  data blocks competition, and the cost and time the document gives for
  bringing in another source.

Three more things are read by rule, with no model call (``rule_fields``):
GAO protest numbers, Court of Federal Claims case numbers and NSNs in the
text, and, for the DLA "AVN Form S53", the ticked AMC/AMSC codes and the
form's two source tables (dla_form.py). Where the form's tables were read,
the record says whether the model's lists match them (``sources_match_form``).

The rules these serve are in docs/extras.md: show only a figure the document
prints, never one we computed.

As in extract.py, the code decides what is kept. Every entry needs a quote
found in the document text, and the value must be visible in its own quote:
the dollar amount parses out of it, the firm's name and CAGE code are in it,
the quantity is in it. An entry that fails is dropped and listed in
``dropped`` with the reason. A model answer that states both ceilings and
the increase is also checked for arithmetic (new = prior + increase, to the
cent or 0.05%); when it does not add up all three are kept and the record is
flagged ``ceiling_arithmetic_ok: false`` so a reader of the table can leave
them out.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from . import llm
from . import dla_form
from .extract import (
    MAX_OUTPUT_TOKENS,
    MIN_FIELD_QUOTE_WORDS,
    _blank,
    _metadata,
    _qinfo,
    durations_in,
    looks_like_tier,
    model_text,
    name_in,
    value_in_quote,
)
from .promptfile import CONFIG
from .text import Document, display_quote, locate_quote

EXTRAS_VERSION = CONFIG["extras"]["version"]
EXTRAS_EFFORT = CONFIG["extras"].get("effort")
# The deterministic checks have their own version, as in extract.py: they can
# be re-applied to the stored model answers without a new call.
EXTRAS_CHECKS_VERSION = "2026-11-11.xchecks6"
TOOL_NAME = "record_ja_extras"
EXTRAS_TIMEOUT_S = 600

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["facts"],
    "properties": {
        "facts": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["kind", "value", "quote"],
            "properties": {"kind": {"type": "string"}, "value": {"type": "string"},
                           "quote": {"type": "string"}}}},
    },
}

AMOUNT_KINDS = ("increase_amount", "prior_ceiling", "new_ceiling", "not_to_exceed", "unit_price", "cost_to_compete")
TEXT_KINDS = ("quantity", "incumbent_vendor", "barrier_action", "source_approval_path", "data_rights_basis",
              "delay_to_compete")
FIRM_LIST_KINDS = ("approved_source", "interested_source")
KINDS = AMOUNT_KINDS + TEXT_KINDS + FIRM_LIST_KINDS
DATA_RIGHTS_BASES = ("government_does_not_own_data", "government_lacks_rights", "data_not_available",
                     "data_uneconomical_to_obtain", "proprietary_to_contractor")
# The table column each firm-list kind fills.
FIRM_LIST_FIELD = {"approved_source": "approved_sources", "interested_source": "interested_sources"}

PROMPT = CONFIG["extras"]["prompt"]
_SPLIT = PROMPT.rfind("NOTICE METADATA", 0, PROMPT.find("<<METADATA>>"))
assert _SPLIT > 0 and "<<" not in PROMPT[:_SPLIT], "extras prompt layout changed; fix the cache split"
PROMPT_CACHED, PROMPT_PER_DOC = PROMPT[:_SPLIT], PROMPT[_SPLIT:]


def build_prompt(doc: Document, text: str) -> tuple[str, str]:
    """(cached instructions, per-notice part), as extract.build_prompt."""
    return PROMPT_CACHED, PROMPT_PER_DOC.replace("<<METADATA>>", _metadata(doc)).replace("<<DOCUMENT>>", text)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

_NUMBER = re.compile(r"^\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)$")
# A CAGE code: five letters and digits with at least one digit (so not "CAGE |").
_CAGE = re.compile(r"^(?=[0-9A-Z]*[0-9])[0-9A-Z]{5}$")
_WORD_NUM = re.compile(r"[0-9][0-9,]*(?:\.[0-9]+)?")

# What a quote has to show for the kinds where a verbatim quote containing
# the value is not enough (2026-11-10: in the extras1 pilot, 2 of 12
# "incumbents" read were only the proposed contractor, and one notice's
# "interested sources" were accreditation bodies the document merely listed).
_INCUMBENT_CUE = re.compile(
    r"incumbent|current(ly)?\b|existing|previous(ly)?|prior\b|predecessor|has (been )?provid|have (been )?provid|"
    r"has (been )?perform|since (19|20)\d\d|extension|extend(ed|ing|s)?\b[^.]{0,60}(contract|order|agreement|period|"
    r"services?|performance|\bPoP\b)|bridge|follow-on|modif|increase|option|"
    # a past award of the contract: "was awarded to", "Award went to", "Award date:", "contract N39430-23-D-4050 ... awarded to"
    r"(was|were) awarded|award (went|was made)|award date|[A-Z0-9]{4,}-\d{2}-[A-Z]-\d{4}[^.]{0,60}awarded to|"
    r"renewal|continu", re.I)
# Further ways a quote shows a past or current contract with the firm
# (xchecks5). On all 12,295 stored answers the cue above dropped 793
# incumbents; in a sample of 70 most were real ("was won by", "has
# successfully performed IDM services since", "has been a past provider",
# "awarded contract W911QX24D0004 to"). Accepted only when the quote has
# none of _NOT_YET (a proposed or future award), which is what the cue was
# guarding against: in the extras1 pilot 2 of 12 "incumbents" were only the
# proposed contractor.
_INCUMBENT_PAST = re.compile(
    r"\bawarded\b|\bwon by\b|\bwas (originally|previously|competitively|issued)|\boriginally\b|"
    r"\b(has|have|had) (had|been|successfully|historically|paid|contracted|maintained|supported|serviced)\b|"
    r"\bhistorically\b|\bin the past\b|\bpast (provider|source|performance|supplier)|"
    r"\b(was|were|been) (procured|purchased|ordered|overhauled|repaired|provided|performed|issued|executed)\b|"
    r"\bprocured\b[^.]{0,80}\bfrom\b|\b(provided|performed|overhauled|maintained|supported) by\b|"
    r"\bis the contractor\b|\balready (utiliz|using|uses)", re.I)
_NOT_YET = re.compile(
    r"\bwill\b|\bshall\b|\bto be (awarded|issued|placed)|\bpropos(ed|es|ing)\b|\bintends?\b|\bplans? to\b|"
    r"\banticipated (awardee|contractor|source)|\bprospective\b|\bwould\b", re.I)
_INTEREST_CUE = re.compile(
    r"interest|respon|capabilit|sources.sought|\bRFI\b|request for information|quot(e|ation)|inquir|"
    r"submitt|proposal|offer", re.I)
# "are expected to express an interest" is a forecast, not a response
# (xchecks5: three firms were kept on one notice for this phrase alone).
_EXPECTED_INTEREST = re.compile(r"expected to (express|show|have)\b[^.]{0,20}interest", re.I)
# "appear(s) qualified and interested to fulfill this requirement: Firm 1: ..."
# is the agency's own market-research assessment (a VA template), not a
# response from the firm (xchecks6: group-1 check, 3daaec51; the J&A's own
# item 10 named only the incumbent). Dropped unless the quote also says the
# firm responded.
_APPEARS_QUALIFIED = re.compile(r"appears? (to be )?(qualified|capable)|(qualified|capable) and interested", re.I)
_RESPONDED = re.compile(r"respon|replied|submitt|received|expressed (an |their )?interest|provided (a |their )?(quote|capabilit)|\b(stated|said|indicated|confirmed|told)\b", re.I)
# An interested-firm list often has the cue only in its heading ("The
# following firms responded: 1. ... 2. ..."), so the quote of one row has none
# (xchecks5: 1,407 firms on 471 notices dropped). A firm is kept when the
# nearest list-type phrase before its quote is an interest heading, not one
# for another kind of list (firms identified in SAM, contacted, considered).
_INTEREST_HEADING = re.compile(
    r"(?P<other>did not (respond|reply|submit|express)|no (additional |other )?(responses?|replies|interest)|"
    r"not interested|identified|search(ed|ing)?|registered|listed|considered|reviewed|contacted|solicited|"
    r"invited|\bfound\b|located|holders?\b|approved sources?|expected to|requested|procurement history|"
    r"purchas)|"
    r"(?P<interest>express(ed|ing)?,? (in writing,? )?(an |their |its )?interest|interested (firms|vendors|sources|parties|"
    r"companies|contractors|offerors)|responded|respondents?|responses? (were |was )?received|"
    r"received (the following |\w+ )?(responses?|capabilit|quotes?|quotations?|submissions?|proposals?)|"
    r"submitted (a |their |its )?(response|capabilit|quote|quotation|proposal)|"
    r"capabilit(y|ies) (statements?|packages?|briefs?) (were |was )?(received|submitted)|replied)", re.I)
_HEADING_WINDOW = 400
# A prose sentence between the heading and the firm ("... responded with
# capability statements, Mistral Group and Precision Welding. The capability
# statements show ... the sensor from Meggitt") means the firm is not a row of
# the list. A numbered row ("1. MCS of Tampa") or a firm name after "Inc." does
# not match: the period has to follow a lowercase letter and start a sentence
# whose second word is lowercase.
_PROSE_BREAK = re.compile(r"[a-z]\.\s+[A-Z][a-z]+\s+[a-z]")
# An increase and the ceilings around it need a quote that says which figure
# is which (2026-11-10, random pilot: "The government estimate is
# $1,801,453.52" and "Amount: $62,871.00" came back as increases, and a row
# of nine bare figures as a prior ceiling).
# "extension", "extend", "bridge" and "option" are not cues (xchecks5): on
# all 12,295 stored answers they let through 93 figures that were the value
# of the extension or bridge itself, or a running total ("Extension + All
# Options $1,176,797.98"), not a ceiling increase. That value is the action's
# value, which the main extraction already reads.
_INCREASE_CUE = re.compile(r"increas|rais|additional|\badd(s|ed|ing)?\b|modif|"
                           r"ceiling|\bby\s+(an estimated\s+)?\$", re.I)
_CEILING_CUE = re.compile(r"ceiling|maximum|total (estimated )?(contract |task order |order )?(value|amount)|"
                          r"\bfrom\b.{0,80}?\bto\b|awarded.{0,60}?amount|"       # dots allowed: "$49,000,000.00"
                          r"current|existing|original|revised|new total|capacity", re.I)
_CAGE_ROW = re.compile(r"\b(?=[0-9A-Z]*[0-9])[0-9A-Z]{5}\s*\|")
_CHECKLIST_LINE = re.compile(r"\bAMS?C\s+[1-5A-Z]\.")
_DATA_CUE = re.compile(r"\bdata\b|drawing|specification|proprietary|rights|technical", re.I)
_COST_CUE = re.compile(r"reverse|re-?engineer|duplicat|qualif|test|develop|transition|migrat|data|rights|"
                       r"new (source|vendor|contractor)|another (source|vendor|contractor)|recompet|compet", re.I)
_APPROVAL_CUE = re.compile(r"source approval|\bSAR\b|L04|M06|alternate offer|approved source|qualif|"
                           r"submit|new sources?", re.I)


# not-to-exceed (xchecks6; group-2 check 25/30): "will range from $274 to
# approximate $959,799" is the top of an estimate, and the VA's "$5 million"
# is the VAAR 819.7008 / 38 U.S.C. 8127 limit for a sole source to an SDVOSB or
# VOSB, which VA J&As restate ("The total contract value will not exceed
# $5,000,000"), not the contract's own maximum.
# A cap on part of the action, or not a cap at all (round-3 check of all 46,
# 2026-11-11): "an initial Not-To-Exceed (NTE) value of $49M" for the first
# undefinitized action under a $124M class ceiling; "The sum of all special
# projects ... will not exceed"; "Variance up to $2,800,000 is requested".
_PARTIAL_CAP = re.compile(r"\binitial\b[^.]{0,30}?\b(?:not[- ]to[- ]exceed|nte)\b|\bspecial\s+projects?\b|\bvariance\b|"
                          r"\b(?:each|per)\s+(?:clin|line\s+item|order|task\s+order|call)\b", re.I)
_RANGE = re.compile(r"\brang(e|es|ing)\b|\bfrom\s+(US)?\$\s?[\d,.]+\s*(to|-|through)\s", re.I)
_VA_SOLE_SOURCE_LIMIT = re.compile(r"819\.70(07|08|09)|38 U\.?\s?S\.?\s?C\.?\s?(§\s?)?8127", re.I)

# technical-data reason (xchecks6; group-2 check 18/30): 4 of the 6 wrong were
# software licences read as technical data, and "does not possess" was read as
# "does not own". The published reasons come from the quote's own words by
# rule, not from the model's category; a quote can give more than one.
_SOFTWARE = re.compile(r"software|licen[cs]|subscription|source code|\bSaaS\b|operating system|firmware", re.I)
_TECH_DATA = re.compile(r"drawing|technical data|data package|\bTDP\b|specification|engineering data|data rights|"
                        r"manufacturing data|design data|technical information", re.I)
_NEG = r"(?:does|do|did)\s+not\s+(?:currently\s+|physically\s+)?(?:possess|have)(?:\s+or\s+have\s+access\s+to)?"
_DATA_WORD = r"(?:technical|drawings?|data\b|tdp|specifications?|engineering)"
_REASON_RULES = (
    # No "proprietary to the contractor" reason (2026-11-11): two rounds of narrowing still
    # left it right on only 11 of 20 in the round-4 check; most wrong ones were a
    # proprietary software product, data product or brand, not technical data. A quote
    # whose only reason was that is now not published.
    # the data (not rights to it) is not the Government's: "does not own the drawings"
    ("government_does_not_own_data", re.compile(r"\b(?:does|do|did)\s+not\s+own\s+(?!(?:(?!(?:or|and)\b)[\w-]+\s+){0,5}rights\b)|\bnot\s+owned\s+by\b", re.I)),
    # a negation near "rights", rights not owned or unobtainable, or a refusal to sell them
    ("government_lacks_rights", re.compile(
        r"\b(?:not|lack(?:s|ed)?|without|no|insufficient|limited|restricted)\b[^.]{0,120}?\brights\b|"
        r"\brights\b[^.]{0,160}?\b(?:not\s+owned|not\s+available|cannot\s+be\s+(?:purchased|obtained)|unsuccessful)|"
        # not "unwilling to sell": that is the owner's position, not the Government's rights
        r"\b(?:need|needs|required?)\s+to\s+(?:acquire|purchase|obtain)\s+(?:full\s+|the\s+)?(?:data\s+)?rights", re.I)),
    # the data itself is not available or not in hand
    ("data_not_available", re.compile(
        r"\bnot\s+(?:currently\s+|physically\s+)?available|\bunavailab|\bno\s+(?:technical\s+)?data\s+package|"
        r"\b(?:not|n't)\s+(?:currently\s+)?(?:have\s+)?in\s+(?:its|their|the\s+government'?s?)?\s*possession|"
        r"\bnot\s+in\s+(?:the\s+)?government\s+possession|"
        r"\b(?:lack(?:s|ing)?|limited|insufficient)\s+(?:of\s+)?(?:the\s+|complete\s+|adequate\s+|sufficient\s+|necessary\s+)*"
        + _DATA_WORD + r"|\bdata\b[^.]{0,30}?\bis\s+insufficient|"
        r"\b(?:cannot\s+be|not\s+(?:currently\s+)?being|have\s+not\s+been|has\s+not\s+been)\s+developed|"
        r"\b(?:data|tdp|technical\s+data)\b[^.]{0,20}?\bis\s+incomplete|\bincomplete\s+(?:technical\s+)?data|"
        + _NEG + r"\b(?:(?!rights)[^.]){0,40}?\b" + _DATA_WORD + r"(?!\s+rights)|"
        r"\b(?:technical|drawings?|data|tdp)\b[^.]{0,80}?\b(?:that|which)\s+the\s+government\s+" + _NEG, re.I)),
    ("data_uneconomical_to_obtain", re.compile(r"uneconomical|not\s+economically|cannot\s+be\s+economically|cost[- ]prohibitive|prohibitive|prohibitively\s+expensive", re.I)),
)
_RIGHTS_NOT_OWNED = re.compile(r"\brights\b[^.]{0,160}?\bnot\s+owned\b", re.I)


def data_rights_reasons(quote: str) -> list[str]:
    """The technical-data reasons the quote states in its own words, in a fixed
    order. "The rights to use the data ... are not owned by the Government" is
    lacking rights, not not owning the data (2f15fa14)."""
    q = quote or ""
    out = [name for name, rx in _REASON_RULES if rx.search(q)]
    if "government_does_not_own_data" in out and _RIGHTS_NOT_OWNED.search(q) \
            and not re.search(r"\bnot\s+own\b[^.]{0,30}\b(?:the\s+)?(?:data|drawings|tdp)\b(?!\s+rights)", q, re.I):
        out.remove("government_does_not_own_data")
        if "government_lacks_rights" not in out:
            out.append("government_lacks_rights")
    return out


def parse_amount(value) -> float | None:
    """The model's plain number of dollars ("5000000", "782650.53"; a stray
    "$" or commas are tolerated). None for anything else: words, ranges, a
    multiplier ("5M") the model was told not to use."""
    m = _NUMBER.match(str(value or "").strip())
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    return v if v > 0 else None


def split_firm(value: str) -> tuple[str | None, str]:
    """"95270 | SKURKA AEROSPACE INC" -> ("95270", "SKURKA AEROSPACE INC");
    a value with no CAGE code -> (None, name)."""
    v = " ".join(str(value or "").split())
    if "|" in v:
        head, tail = (x.strip() for x in v.split("|", 1))
        if _CAGE.match(head.upper()) and tail:
            return head.upper(), tail
    return None, v


def _quantity_in(value: str, quote: str) -> bool:
    try:
        want = float(str(value).replace(",", ""))
    except ValueError:
        return False
    for m in _WORD_NUM.finditer(quote):
        try:
            if float(m.group(0).replace(",", "")) == want:
                return True
        except ValueError:
            continue
    return False


# A unit price has to read as the price of one item (xchecks5). On all 12,295
# stored answers, 94 of 192 kept "unit prices" had no such cue, and most were
# rents per square foot, hourly or monthly rates, fuel per gallon, or a
# period's line in a cost table.
_PER_ITEM = re.compile(r"\bunit (price|cost)\b|\beach\b|\bEA\b|/\s*ea\b|\bper (unit|item|each|piece|kit|set|"
                       r"assembly|system|license|licence|device|case|dose|vial|exam|sample|study|student|seat|"
                       r"user|collar|cutter)\b", re.I)
_RATE = re.compile(r"(?:\bper|/)\s*(?:rentable |usable )?(?:hour|hr|day|week|month|mo|year|yr|annum|night|"
                   r"sq|square|RSF|SF|USG|gal|gallon|lbs?|pound|mile|member|space|room|PVPM|PMPM|sea day)\b|"
                   r"\b(?:monthly|annual|hourly|daily)\b|\bmo\.|\bmonths?\b|\b(?:USG|gallons?|hours?|hrs?)\b", re.I)


def unit_price_ok(amount: float, quantity, quote: str) -> bool:
    """The quote shows ``amount`` is the price of one item: it says so
    ("unit price", "each", "per license"), or it is a table row in which the
    quantity times the amount is a line total printed in the same quote. A
    rate by time, area, volume or weight is never a unit price here."""
    if _RATE.search(quote):
        return False
    if _PER_ITEM.search(quote):
        return True
    # a count of items is a whole number; 81,693.75 in a cost table is hours
    if quantity is None or quantity <= 1 or quantity != int(quantity):
        return False
    want = amount * quantity
    return any(abs(parse_amount(m.group(0)) - want) <= max(0.01, 0.0005 * want)
               for m in _WORD_NUM.finditer(quote) if parse_amount(m.group(0)))


def _windows_before(quote: str, doc: Document, n: int) -> list[str]:
    """The ``n`` characters of document text before each place the quote
    appears (whitespace-normalized, pages joined, case ignored)."""
    q = _WS.sub(" ", quote or "").strip().casefold()
    t = _WS.sub(" ", " ".join(p.text for p in doc.pages))
    tl = t.casefold()
    out, i = [], tl.find(q) if q else -1
    while i >= 0:
        out.append(t[max(0, i - n):i])
        i = tl.find(q, i + 1)
    return out


# Agencies often fill the FAR 6.303-2(b)(10) heading with the proposed
# contractor itself ("Trane U.S. Inc is the only capable contractor",
# "TransOx, Inc is the only source sought", "D Noe, LLC ... Incumbent
# Lessor"; xchecks5 sample). Under the heading alone such a row is not kept.
_SOLE_ROW = re.compile(r"\bonly\b|\bsole\b|\bincumbent\b|\bpropos|\bawardee\b|\bexclusive", re.I)


def under_interest_heading(quote: str, doc: Document, name: str = "") -> bool:
    """Every place the quote appears, the last list-type phrase before the
    firm's name (in the _HEADING_WINDOW characters before the quote and the
    quote up to the name) is an interest heading, with no prose sentence
    between, and the row does not say the firm is the only or proposed one."""
    wins = _windows_before(quote, doc, _HEADING_WINDOW)
    if not wins:
        return False
    q = _WS.sub(" ", quote).strip()
    at = q.casefold().find(_WS.sub(" ", name).strip().casefold()) if name else -1
    lead = q[:at] if at >= 0 else q
    if _SOLE_ROW.search(q):
        return False
    # "DOLWD and their teaming partner BCI were contacted": a phrase for
    # another kind of list anywhere in the row itself
    if any(m.group("other") for m in _INTEREST_HEADING.finditer(q)):
        return False
    for w in wins:
        text = w + " " + lead
        last = None
        for m in _INTEREST_HEADING.finditer(text):
            last = m
        if last is None or last.group("interest") is None or _PROSE_BREAK.search(text[last.end():]):
            return False
    return True


_EMPTY_BOX_BEFORE = re.compile(r"(?:\[\s*\]|☐|\[unchecked\])\s*$")
_WS = re.compile(r"\s+")


def beside_an_empty_box(quote: str, doc: Document) -> bool:
    """The quoted passage is a line of a form with an EMPTY tick box in front
    of it, everywhere it appears: the form prints the sentence, and the
    agency did not select it. On DLA's AVN Form S53, item 11's "Based upon
    the technical certification on file, technical data is not available and
    cannot be developed..." has an empty box on 278 of 1,316 forms
    (2026-11-10), and reads as the document's own statement when quoted.
    False when the passage is not found this way (nothing can be said)."""
    q = _WS.sub(" ", quote or "").strip()
    if len(q) < 12:
        return False
    found = empty = 0
    for p in doc.pages:
        t = _WS.sub(" ", p.text)
        i = t.find(q)
        while i >= 0:
            found += 1
            # the box is at the start of the form line; the quote may begin a
            # few words in, so take the nearest box in the 80 characters before
            lead = t[max(0, i - 80):i]
            box = re.search(r"(\[\s*[xX✓✔√]?\s*\]|☑|☒|☐|\[checked\]|\[unchecked\])[^\[\]☑☒☐]{0,60}$", lead)
            if box and _EMPTY_BOX_BEFORE.search(box.group(1)):
                empty += 1
            i = t.find(q, i + 1)
    return found > 0 and empty == found


def verify(raw: dict, doc: Document) -> tuple[dict, list[dict]]:
    """(fields, dropped) from one model answer. ``fields`` has every key of
    the table row, None or an empty list where nothing was kept."""
    nd = doc.normalized()
    _text_cache: list = []

    def doc_text() -> str:
        if not _text_cache:
            _text_cache.append(" ".join(p.text for p in doc.pages))
        return _text_cache[0]
    def sentence_before(quote: str) -> str:
        # the start of the quote's sentence when the model began the quote mid-sentence
        # ("The sum of all special projects ... will not exceed $6.6M" quoted from "will not")
        text = doc_text()
        q = " ".join(quote.split())[:60]
        i = " ".join(text.split()).find(q) if q else -1
        if i < 0:
            return ""
        before = " ".join(text.split())[max(0, i - 300):i]
        ends = list(_SENTENCE_END.finditer(before))
        return before[ends[-1].end() - 1:] if ends else before
    fields: dict = {k: None for k in AMOUNT_KINDS + TEXT_KINDS}
    for k in AMOUNT_KINDS + TEXT_KINDS:
        fields[k + "_quote"] = None
        fields[k + "_quote_info"] = None
    for col in FIRM_LIST_FIELD.values():
        fields[col] = []
    fields["data_rights_reasons"] = None
    dropped: list[dict] = []
    seen: set[str] = set()

    def drop(kind, value, quote, reason):
        dropped.append({"field": kind, "value": value, "quote": _blank(quote), "reason": reason})

    for x in raw.get("facts") or []:
        if not isinstance(x, dict):
            continue
        kind = (_blank(x.get("kind")) or "").lower()
        value, quote = _blank(x.get("value")), _blank(x.get("quote"))
        if kind not in KINDS:
            drop("facts", x.get("kind"), quote, "unknown fact kind")
            continue
        if kind not in FIRM_LIST_KINDS and kind in seen:
            drop(kind, value, quote, "a second entry for a single-valued fact; the first is kept")
            continue
        if value is None:
            drop(kind, value, quote, "no value")
            continue
        m = locate_quote(quote, doc, nd, min_words=MIN_FIELD_QUOTE_WORDS, ignore_case=True)
        if not m.verified:
            drop(kind, value, quote, "quote not found in the document")
            continue
        if kind not in FIRM_LIST_KINDS and _CHECKLIST_LINE.search(quote):
            drop(kind, value, quote, "quote is a line of a printed checklist")
            continue
        if kind not in FIRM_LIST_KINDS and beside_an_empty_box(quote, doc):
            drop(kind, value, quote, "the form's box beside this line is not ticked")
            continue
        if kind in AMOUNT_KINDS:
            amount = parse_amount(value)
            if amount is None:
                drop(kind, value, quote, "value is not a plain dollar amount")
                continue
            if not value_in_quote(amount, quote):
                drop(kind, value, quote, "amount not in its quote")
                continue
            if kind == "not_to_exceed" and looks_like_tier(amount, quote):
                drop(kind, value, quote, "an approval threshold, not a value")
                continue
            if kind == "not_to_exceed" and (_PARTIAL_CAP.search(quote) or _PARTIAL_CAP.search(sentence_before(quote))):
                drop(kind, value, quote, "a cap on part of the action, or a requested variance, not the action's maximum")
                continue
            if kind == "not_to_exceed" and _RANGE.search(quote):
                drop(kind, value, quote, "the top of an estimate range, not a stated maximum")
                continue
            if kind == "not_to_exceed" and amount == 5_000_000 and _VA_SOLE_SOURCE_LIMIT.search(doc_text()):
                drop(kind, value, quote, "VA's $5 million limit for an SDVOSB/VOSB sole source, not this action's maximum")
                continue
            if kind == "increase_amount" and not _INCREASE_CUE.search(quote):
                drop(kind, value, quote, "quote does not show the figure is an increase")
                continue
            if kind in ("prior_ceiling", "new_ceiling") and not _CEILING_CUE.search(quote):
                drop(kind, value, quote, "quote does not show the figure is a ceiling or total")
                continue
            if kind == "cost_to_compete" and not _COST_CUE.search(quote):
                drop(kind, value, quote, "quote does not say what the cost is for")
                continue
            kept = amount
        elif kind == "quantity":
            if not _quantity_in(value, quote):
                drop(kind, value, quote, "quantity not in its quote")
                continue
            kept = float(str(value).replace(",", ""))
        elif kind == "incumbent_vendor":
            if not name_in(value, quote):
                drop(kind, value, quote, "name not in its quote")
                continue
            if not (_INCUMBENT_CUE.search(quote)
                    or (_INCUMBENT_PAST.search(quote) and not _NOT_YET.search(quote))):
                drop(kind, value, quote, "quote does not show the firm is the incumbent")
                continue
            kept = value
        elif kind == "barrier_action":
            kept = display_quote(quote)
        elif kind == "source_approval_path":
            if not _APPROVAL_CUE.search(quote):
                drop(kind, value, quote, "quote does not describe a way for a new source to apply")
                continue
            kept = display_quote(quote)
        elif kind == "data_rights_basis":
            if value not in DATA_RIGHTS_BASES:
                drop(kind, value, quote, "not one of the listed bases")
                continue
            if not _DATA_CUE.search(quote):
                drop(kind, value, quote, "quote is not about technical data")
                continue
            if _SOFTWARE.search(quote) and not _TECH_DATA.search(quote):
                drop(kind, value, quote, "software licensing, not technical data")
                continue
            reasons = data_rights_reasons(quote)
            if not reasons:
                drop(kind, value, quote, "the quote names none of the technical-data reasons in its own words")
                continue
            fields["data_rights_reasons"] = "; ".join(reasons)
            kept = value
        elif kind == "delay_to_compete":
            if not durations_in(quote) or not name_in(value, quote):
                drop(kind, value, quote, "duration not in its quote")
                continue
            kept = value
        else:   # a firm list
            cage, name = split_firm(value)
            if not name_in(name, quote):
                drop(kind, value, quote, "name not in its quote")
                continue
            if cage and not name_in(cage, quote):
                drop(kind, value, quote, "CAGE code not in its quote")
                continue
            # an interested firm needs a quote that says so, or a row of a CAGE
            # table (which table it is, is checked against the form: sources_match_form)
            if kind == "interested_source" and _EXPECTED_INTEREST.search(quote):
                drop(kind, value, quote, "quote says the firm is expected to express interest, not that it did")
                continue
            if kind == "interested_source" and _APPEARS_QUALIFIED.search(quote) and not _RESPONDED.search(quote):
                drop(kind, value, quote, "the agency's assessment that the firm appears qualified, not a response from it")
                continue
            if kind == "interested_source" and not (_INTEREST_CUE.search(quote) or _CAGE_ROW.search(quote)
                                                    or under_interest_heading(quote, doc, name)):
                drop(kind, value, quote, "quote does not show the firm expressed interest")
                continue
            col = FIRM_LIST_FIELD[kind]
            if any(f["name"].casefold() == name.casefold() and f["cage"] == cage for f in fields[col]):
                continue   # the same row given twice
            fields[col].append({"cage": cage, "name": name, "quote": display_quote(quote), **_qinfo(m)})
            continue
        seen.add(kind)
        fields[kind] = kept
        fields[kind + "_quote"] = display_quote(quote)
        fields[kind + "_quote_info"] = _qinfo(m)

    # a unit price needs a quote that reads as the price of one item; the
    # quantity counts for a table row only when it is in the same quote
    if fields["unit_price"] is not None:
        q = fields["unit_price_quote"]
        qty = fields["quantity"] if (fields["quantity"] is not None
                                     and _quantity_in(str(fields["quantity"]), q)) else None
        if not unit_price_ok(fields["unit_price"], qty, q):
            drop("unit_price", fields["unit_price"], q, "quote does not show the figure is the price of one item")
            fields["unit_price"] = fields["unit_price_quote"] = fields["unit_price_quote_info"] = None
    # a quantity means nothing without the unit price it counts
    if fields["quantity"] is not None and fields["unit_price"] is None:
        drop("quantity", fields["quantity"], fields["quantity_quote"], "no unit price kept")
        fields["quantity"] = fields["quantity_quote"] = fields["quantity_quote_info"] = None
    fields["ceiling_arithmetic_ok"] = ceiling_arithmetic_ok(fields)
    return fields, dropped


def ceiling_arithmetic_ok(fields: dict) -> bool | None:
    """True/False when the record has both ceilings and the increase; None
    when it can't be checked. The document's own figures are kept either way
    (some documents do not add up); the flag is for readers of the table."""
    p, n, i = fields.get("prior_ceiling"), fields.get("new_ceiling"), fields.get("increase_amount")
    if p is None or n is None or i is None:
        return None
    return abs(n - p - i) <= max(0.01, 0.0005 * n)


_GAO = re.compile(r"\bB-4\d{5}(?:\.\d+)?\b")
_COFC = re.compile(r"\b(?:No\.?|Case (?:No\.?|Number))\s*:?\s*(\d{1,2}-\d{1,5}\s?C)\b")
_COFC_NEAR = re.compile(r"Court of Federal Claims|\bCOFC\b", re.I)
_NSN = re.compile(r"(?<![\d-])(\d{4})-?(\d{2})-?(\d{3})-?(\d{4})(?![\d-])")
_NSN_WRITTEN = re.compile(r"(?<![\d-])(?:\d{4}-\d{2}-\d{3}-\d{4}|\d{4}-\d{9})(?![\d-])")
MAX_LISTED = 25
# A GAO "B-" number is either a protest about this or a related procurement
# or a decision the document cites as precedent ("See Major Contracting
# Services, Inc., B-401472, Sep. 14, 2009, 2009 CPD ¶ 170"). In the
# 2026-11-10 review 24 of 70 numbers were precedent cites. Each mention is
# read in the words around it; a protest event wins over a citation style.
_GAO_EVENT = re.compile(
    r"\bfiled\b|\bfiling\b|submitted|received|receipt|\bstay(ed)?\b|stop[- ]work|corrective action|pending|"
    r"pendency|sustained|denied|dismissed|withdrawn|protest (number|file|no\b)|file number|docket|outcome|"
    r"resolve|delay|level protests?|post-?award protest|bid protest|protested|ongoing|denial|"
    r"protests?[\s(\[:]*(?:number\s*|no\.?\s*)?B-4\d{5}", re.I)
_GAO_CITE = re.compile(
    r"CPD|Comp\.? ?Gen|precedent|\bSee\b|Matter of|recogni[sz]|interpreted|\bruling\b|consistent with|"
    # a case name before the number: "Inc., B-401472" or "Major Contracting
    # (B-401472)"; the second only with a capitalized word, so "GAO decisions
    # (B-424689" (decisions on this procurement's own protests) is not one
    r"GAO (Decision|case)\b|[A-Za-z.)]\s*,\s*B-4\d{5}|(?-i:[A-Z][a-z][A-Za-z.]*)\s*\(B-4\d{5}", re.I)


_SENTENCE_END = re.compile(r"[a-z0-9)\]]\.\s+[A-Z]")


def gao_numbers(text: str) -> dict:
    """{"protests": [...], "cited": [...], "unclear": [...]}: each GAO number
    once. A protest when any mention has protest-event words within 140
    characters before or 60 after; otherwise cited when a mention reads as a
    citation; otherwise unclear."""
    t = _WS.sub(" ", text)
    kind: dict[str, set] = {}
    for m in _GAO.finditer(t):
        # the mention's own sentence, at most 140 characters before and 60
        # after: a protest in the sentence before must not make a citation
        # that follows it a protest ("Inc., B-401472, Sep. 14" is not a break)
        before, after = t[max(0, m.start() - 140):m.start()], t[m.end():m.end() + 60]
        cuts = list(_SENTENCE_END.finditer(before))
        if cuts:
            before = before[cuts[-1].end() - 1:]
        cut = re.search(r"\.\s+[A-Z]", after)
        if cut:
            after = after[:cut.start() + 1]
        near = before + m.group(0) + after
        k = "protests" if _GAO_EVENT.search(near) else "cited" if _GAO_CITE.search(near) else "unclear"
        kind.setdefault(m.group(0), set()).add(k)
    out = {"protests": [], "cited": [], "unclear": []}
    for num, ks in sorted(kind.items()):
        out["protests" if "protests" in ks else "cited" if "cited" in ks else "unclear"].append(num)
    return out


def rule_fields(doc: Document) -> dict:
    """What is read from the text by rule, with no model call.

    gao_protests       GAO protest file numbers ("B-424920", "B-424107.1"):
                       a mention with protest-event words around it
    gao_cited          GAO decisions the document cites as precedent
    gao_unclear        GAO numbers that read as neither
    cofc_cases         Court of Federal Claims case numbers ("25-279C"), only
                       in a document that names the court
    nsns               National Stock Numbers as 4-2-3-4 digits, from text that
                       writes them "2840-01-666-1765" or "2915-004499118"
    uses_52_217_8      the text cites FAR 52.217-8 (option to extend services)
    form               dla_form.read for the DLA form, else None
    """
    text = "\n".join(p.text for p in doc.pages)
    gao = gao_numbers(text)
    cofc = sorted({re.sub(r"\s", "", x) for x in _COFC.findall(text)}) if _COFC_NEAR.search(text) else []
    nsns = sorted({"-".join(_NSN.match(x).groups()) for x in _NSN_WRITTEN.findall(text) if _NSN.match(x)})
    return {"gao_protests": gao["protests"][:MAX_LISTED], "gao_cited": gao["cited"][:MAX_LISTED],
            "gao_unclear": gao["unclear"][:MAX_LISTED], "cofc_cases": cofc[:MAX_LISTED], "nsns": nsns[:MAX_LISTED],
            "nsn_count": len(nsns), "uses_52_217_8": bool(re.search(r"52\.217-8\b", text)),
            "form": next((f for f in (dla_form.read(p_text) for p_text in _attachment_texts(doc)) if f), None)}


def _attachment_texts(doc: Document) -> list[str]:
    return ["\n".join(p.text for p in pages) for _, pages in doc.attachments()]


def sources_match_form(fields: dict, form: dict | None) -> dict:
    """For each source list, whether the model's firms are the form's:
    True/False by CAGE code, or None when the form's table was not read (so
    there is nothing to compare with). A list that does not match should not
    be shown: the two readings are independent, and they disagree."""
    out = {}
    for col, part in (("approved_sources", "approved"), ("interested_sources", "interested")):
        rows = (form or {}).get(part)
        if rows is None:
            out[col] = None
            continue
        out[col] = sorted(c for c, _ in rows) == sorted(f["cage"] or "" for f in fields.get(col) or [])
    return out


def shape_errors(raw) -> list[str]:
    if not isinstance(raw, dict):
        return ["not an object"]
    if not isinstance(raw.get("facts"), list):
        return ["facts is not a list"]
    return [f"facts[{i}] is not a {{kind, value, quote}} object" for i, x in enumerate(raw["facts"])
            if not isinstance(x, dict) or not all(isinstance(x.get(k), str) for k in ("kind", "value", "quote"))]


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------

def _record(doc: Document, raw: dict, usage: dict, text: str, truncated: bool, model: str,
            effort: str | None) -> dict:
    fields, dropped = verify(raw, doc)
    rules = rule_fields(doc)
    fields["sources_match_form"] = sources_match_form(fields, rules["form"])
    return {
        "experimental": True,
        "notice_id": doc.notice_id,
        "model": model,
        "extras_version": EXTRAS_VERSION,
        "effort": effort,
        "checks_version": EXTRAS_CHECKS_VERSION,
        "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pages": len(doc.pages),
        "transcribed_pages": sum(1 for p in doc.pages if p.text_source == "transcription"),
        "chars_total": len(doc.model_text()),
        "chars_sent": len(text),
        "truncated": truncated,
        "fields": fields,
        "rules": rules,
        "dropped": dropped,
        "raw": raw,
        "usage": usage,
    }


def _usage(u: dict) -> dict:
    return {"calls": 1, "input_tokens": int(u.get("input_tokens") or 0),
            "output_tokens": int(u.get("output_tokens") or 0),
            "cache_creation_input_tokens": int(u.get("cache_creation_input_tokens") or 0),
            "cache_read_input_tokens": int(u.get("cache_read_input_tokens") or 0),
            "cost_usd": round(float(u.get("cost_usd") or 0), 6),
            **({"price_mult": u["price_mult"]} if u.get("price_mult") else {})}


def _checked(raw) -> dict:
    bad = shape_errors(raw)
    if bad:
        raise llm.ModelError(f"model answer does not match the schema: {bad[:3]}")
    return raw


def extras_document(doc: Document, *, model: str = llm.DEFAULT_MODEL, call_fn=None,
                    effort: str | None = EXTRAS_EFFORT) -> dict:
    """One record for one notice. Raises llm.ModelError if the call fails or
    the answer has the wrong shape, so nothing is stored and the notice is
    tried again on the next run. No retry on dropped values: a dropped entry
    here is usually a fact the document does not state."""
    call_fn = call_fn or llm.call
    text, truncated = model_text(doc)
    kw = {"effort": effort} if effort else {}
    raw, u = call_fn(build_prompt(doc, text), schema=SCHEMA, tool_name=TOOL_NAME, model=model,
                     max_tokens=MAX_OUTPUT_TOKENS, timeout=EXTRAS_TIMEOUT_S, **kw)
    return _record(doc, _checked(raw), _usage(u), text, truncated, model, effort)


def extras_documents_batch(docs: list[Document], *, model: str = llm.DEFAULT_MODEL, batch_fn=None,
                           effort: str | None = EXTRAS_EFFORT, max_wait_s: float = 12_000,
                           resume_id: str | None = None) -> dict:
    """{notice_id: record or llm.ModelError} through the Message Batches API
    (half price). Raises llm.ModelError if the batch can't be created."""
    batch_fn = batch_fn or llm.batch_call
    texts = {d.notice_id: model_text(d) for d in docs}
    by_id = {d.notice_id: d for d in docs}
    kw = {"effort": effort} if effort else {}
    if resume_id:
        kw["resume_id"] = resume_id
    res = batch_fn({nid: build_prompt(by_id[nid], texts[nid][0]) for nid in by_id}, schema=SCHEMA,
                   tool_name=TOOL_NAME, model=model, max_tokens=MAX_OUTPUT_TOKENS, max_wait_s=max_wait_s, **kw)
    out: dict = {}
    for nid, doc in by_id.items():
        r = res.get(nid) or llm.ModelError("no result in the batch")
        try:
            if isinstance(r, Exception):
                raise r
            raw, u = r
            out[nid] = _record(doc, _checked(raw), _usage(u), *texts[nid], model, effort)
        except llm.ModelError as exc:
            out[nid] = exc
    return out


# Spend estimate for one notice, for choosing how many fit under a run's cap
# before a batch is sent. From the extras1 pilot (160 notices, batch,
# 2026-11-10): output was 1,190 tokens at the median, 2,124 at the 90th
# percentile and 4,322 at most, and the run cost $0.0949 against an estimate
# of $0.2964. 4,500 keeps the estimate above the largest answer seen while
# extras2 asks for four more kinds.
EST_TOKENS_PER_CHAR = 0.45
EST_INSTRUCTION_TOKENS = 2_300
EST_OUTPUT_TOKENS = 4_500


def estimate_cost(doc: Document, model: str = llm.DEFAULT_MODEL, *, batch: bool = True) -> float:
    text, _ = model_text(doc)
    tokens_in = EST_INSTRUCTION_TOKENS + int(len(text) * EST_TOKENS_PER_CHAR)
    return llm.cost_usd(model, tokens_in, EST_OUTPUT_TOKENS,
                        price_mult=llm.BATCH_PRICE_MULT if batch else 1.0) or 0.0


def reverify(rec: dict, doc: Document) -> dict:
    """The stored model answer under the current checks; no model call."""
    fields, dropped = verify(rec["raw"], doc)
    rules = rule_fields(doc)
    fields["sources_match_form"] = sources_match_form(fields, rules["form"])
    return {**rec, "fields": fields, "rules": rules, "dropped": dropped, "checks_version": EXTRAS_CHECKS_VERSION}


# ---------------------------------------------------------------------------
# Table row
# ---------------------------------------------------------------------------

def _firms(rows: list[dict]) -> str | None:
    return "; ".join((f"{r['cage']} | " if r.get("cage") else "") + r["name"] for r in rows) or None


def _pages(info: dict | None) -> str | None:
    pages = (info or {}).get("quote_pages") or []
    return ", ".join(str(p["page_number"]) for p in pages) or None


_CORP_WORDS = frozenset(("inc", "incorporated", "llc", "l l c", "corp", "corporation", "co", "company",
                         "ltd", "limited", "lp", "llp", "plc", "pllc", "pc", "the", "dba"))


def firm_key(name: str) -> list[str]:
    """Words of a firm name for matching: lowercased, '&' as 'and', punctuation
    dropped, corporate suffixes (Inc, LLC, Corp...) and 'the' removed."""
    s = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower().replace("&", " and "))
    return [w for w in s.split() if w not in _CORP_WORDS]


def _name_variants(name: str) -> list[tuple[list[str], bool]]:
    """(words, is_acronym) for a firm name: the name without its
    parentheticals, the full name, and each parenthetical on its own
    ("Hindustan Aeronautics Ltd. (HAL)" -> hindustan aeronautics, ..., hal).
    is_acronym: written in capitals with no spaces ("HAL", "MTU", "Cal-Waste" no)."""
    raw = str(name or "")
    inner = re.findall(r"\(([^()]{1,40})\)", raw)
    base = re.sub(r"\([^()]*\)", " ", raw)
    out = []
    for v in [base, raw, *inner]:
        k = firm_key(v)
        if k:
            out.append((k, bool(re.fullmatch(r"[A-Z0-9&]{2,8}", v.strip()))))
    return out


def same_firm(name: str, others: list[str]) -> bool:
    """True if ``name`` is one of ``others`` once suffixes, punctuation and
    parentheticals are set aside, or one name's words begin with all of the
    other's ('BAE Systems' / 'BAE Systems San Diego Ship Repair Inc.').
    A parenthetical counts as a name of its own ("HAL" matches "Hindustan
    Aeronautics Ltd. (HAL)", xchecks6 interested recheck, fc54adb6). Errs
    toward calling a parent or sibling the same firm: here that only leaves a
    firm off the list of other interested firms, which is safer than listing
    the awardee as one. A one-word name matches as a prefix ('Boeing' /
    'Boeing Defense, Space, and Security'; 'MTU' / 'MTU Berlin-Brandenburg
    GmbH') only if it is not a generic word ('General', 'National') and is
    at least 4 letters or written as an acronym."""
    for a, a_acr in _name_variants(name):
        for o in others:
            for b, b_acr in _name_variants(o):
                short, long_ = (a, b) if len(a) <= len(b) else (b, a)
                short_acr = a_acr if short is a else b_acr
                if short == long_:
                    return True
                # an acronym of the other name's initials ("GDIT" / "General
                # Dynamics Information Technology")
                for acr, words, is_acr in ((a, b, a_acr), (b, a, b_acr)):
                    if is_acr and len(acr) == 1 and len(acr[0]) >= 3 and len(words) >= 3 \
                            and acr[0] == "".join(w[0] for w in words if w != "and"):
                        return True
                if long_[:len(short)] == short and (
                        len(short) >= 2 or (short[0] not in _GENERIC_FIRST
                                            and (len(short[0]) >= 4 or (short_acr and len(short[0]) >= 3)))):
                    return True
    return False


_GENERIC_FIRST = frozenset((
    "general", "national", "american", "united", "international", "global", "advanced", "applied",
    "integrated", "technical", "technology", "technologies", "systems", "federal", "government",
    "defense", "data", "digital", "medical", "health", "science", "sciences", "solutions", "services",
    "engineering", "research", "management", "consulting", "associates", "enterprises", "industries",
    "group", "partners", "north", "south", "east", "west", "pacific", "atlantic", "eastern", "western",
    "first", "premier", "strategic", "professional", "innovative", "precision", "quality", "allied",
    "universal", "standard", "summit", "pioneer", "eagle", "liberty", "patriot", "veterans", "native",
    "southern", "northern", "central", "capital", "metro", "city", "county", "state", "world",
))


def interested_list(rec: dict) -> list[tuple] | None:
    """(cage, name) of every firm the document says responded or expressed
    interest; [] when a DLA form's item 12 says none and its text names no
    responder; None when it can't be read. On a DLA form: the form's table
    plus each model firm whose quote is a sentence (not a table row) and whose
    CAGE, or first word without one, the table lacks (see flat_row)."""
    f = rec.get("fields") or {}
    form = (rec.get("rules") or {}).get("form") or {}
    if form:
        rows = form.get("interested")
        if rows is None:
            return None
        # a model firm without a CAGE code ("BHPE replied to the Sources
        # Sought") is added unless a table firm starts with the same word
        table = {c for c, _ in rows}
        first = {(n.split() or [""])[0].casefold() for _, n in rows}
        added = [(x.get("cage"), x["name"]) for x in f.get("interested_sources") or []
                 if not _CAGE_ROW.search(x.get("quote") or "")
                 and (x["cage"] not in table if x.get("cage")
                      else (x["name"].split() or [""])[0].casefold() not in first)]
        return [tuple(r) for r in rows] + added
    return [(x.get("cage"), x["name"]) for x in f.get("interested_sources") or []] or None


def interested_firms_named(rec: dict | None) -> str | None:
    """The published interested-firms value (xchecks6): every firm the
    document names as having responded to the agency or expressed interest in
    the work. It can include the firm that got the award: agencies often list
    the sole source itself as an interested source, and taking it out by name
    failed on 5 of 20 lists in the round-3 check (misspellings, a parent
    company, an acquired firm, the awardee's distributor), so it is not taken
    out and the label says so.
    - Never "none", not even on a DLA form whose item 12 says none: the
      round-4 check (2026-11-11) found 4 of 20 lists missing firms the
      document names (a list cut at a signature block, item 10 naming firms
      the model left out), so an empty list is "not found", not "none".
    - Not necessarily every firm that responded: "named", not "all"."""
    if not rec:
        return None
    lst = interested_list(rec)
    return "; ".join((f"{c} | " if c else "") + n for c, n in lst) if lst else None


def flat_row(rec: dict, meta: dict, awardee_names: list[str] | None = None) -> dict:
    """One row of extras.csv / the published table."""
    f = rec["fields"]
    row = {"notice_id": rec["notice_id"], "sam_link": meta.get("sam_link"), "title": meta.get("title"),
           "agency": meta.get("agency"), "sub_tier": meta.get("sub_tier"),
           "posted_date": str(meta.get("posted_date") or "")[:10] or None}
    for k in AMOUNT_KINDS:
        row[k + "_usd"] = f.get(k)
        row[k + "_quote"] = f.get(k + "_quote")
        row[k + "_quote_pages"] = _pages(f.get(k + "_quote_info"))
    row["ceiling_arithmetic_ok"] = f.get("ceiling_arithmetic_ok")
    row["quantity"] = f.get("quantity")
    row["quantity_quote"] = f.get("quantity_quote")
    row["incumbent_vendor"] = f.get("incumbent_vendor")
    row["incumbent_vendor_quote"] = f.get("incumbent_vendor_quote")
    match = f.get("sources_match_form") or {}
    on_form = bool((rec.get("rules") or {}).get("form"))
    for col in FIRM_LIST_FIELD.values():
        row[col] = _firms(f.get(col) or [])
        row[col + "_count"] = len(f.get(col) or [])
        row[col + "_match_form"] = match.get(col)
    # The lists to show (xchecks5). Off the form, interested firms are kept
    # (each has a quote or a heading saying it responded or expressed
    # interest), but "approved source" is not: there the phrase turned up as
    # "FDA approved" and "only approved reseller" (random pilot, 2026-11-10).
    #
    # On a DLA form, against all 1,314 forms on 2026-11-10:
    # - approved: the form's table (dla_form.read) is the record. The model
    #   agreed on 1,064 of the 1,197 tables read, listed nothing on 116, and
    #   named a CAGE code the table lacks on 2. One of those two is a real
    #   conflict (table 70236, the form's own text "Tactair ... Cage:70326"),
    #   so a list is not shown when the model names a CAGE the table lacks.
    # - interested: item 12's table can say NONE while item 6 or 10 of the
    #   same form says a firm responded ("Triman Industries (CAGE 0ZBE8)
    #   responded with interest"). So the list is the table plus each model
    #   firm with a CAGE the table lacks whose quote is a sentence, not a table
    #   row; "none" only when both are empty. A table not read: not shown.
    form_rows = (rec.get("rules") or {}).get("form") or {}
    if on_form:
        rows = form_rows.get("approved")
        table = {c for c, _ in rows or []}
        extra = {x["cage"] for x in f.get("approved_sources") or [] if x.get("cage")} - table
        row["approved_sources_confirmed"] = (None if rows is None or extra else "none" if not rows else
                                             "; ".join(f"{c} | {n}" for c, n in rows))
    else:
        row["approved_sources_confirmed"] = None
    interested = interested_list(rec)
    fmt = lambda rows: "; ".join((f"{c} | " if c else "") + n for c, n in rows)   # noqa: E731
    row["interested_sources_confirmed"] = (None if interested is None else
                                           "none" if not interested else fmt(interested))
    # Interested firms other than the awardee (2026-11-11): on about 902 of
    # 1,638 notices with an award name, the awardee itself was on the list
    # (agencies often put the proposed contractor under the "(10) sources that
    # expressed interest" heading). Shown as "interested firms" that reads as
    # competition that wasn't there. The awardee names come from the main
    # extraction's vendor and from SAM and USAspending, not from this call.
    # None when no awardee name is known: the list can't be told apart then.
    names = [n for n in (awardee_names or []) if n]
    if interested is None or not names:
        row["other_interested_sources"] = None
    else:
        others = [(c, n) for c, n in interested if not same_firm(n, names)]
        row["other_interested_sources"] = fmt(others) if others else "none"
    row["barrier_action_quote"] = f.get("barrier_action_quote")
    row["source_approval_path_quote"] = f.get("source_approval_path_quote")
    row["data_rights_basis"] = f.get("data_rights_basis")
    row["data_rights_basis_quote"] = f.get("data_rights_basis_quote")
    row["delay_to_compete"] = f.get("delay_to_compete")
    row["delay_to_compete_quote"] = f.get("delay_to_compete_quote")
    r = rec.get("rules") or {}
    form = r.get("form") or {}
    row["gao_protests"] = "; ".join(r.get("gao_protests") or []) or None
    row["gao_cited"] = "; ".join(r.get("gao_cited") or []) or None
    row["gao_unclear"] = "; ".join(r.get("gao_unclear") or []) or None
    row["cofc_cases"] = "; ".join(r.get("cofc_cases") or []) or None
    row["nsns"] = "; ".join(r.get("nsns") or []) or None
    row["nsn_count"] = r.get("nsn_count")
    row["uses_52_217_8"] = r.get("uses_52_217_8")
    row["dla_form"] = bool(r.get("form"))
    row["form_amc"] = form.get("amc")
    row["form_amsc"] = "".join(form.get("amsc") or []) or None if form.get("amc") else None
    row["form_other_ticked"] = form.get("other_ticked")
    row["form_approved_sources"] = "; ".join(f"{c} | {n}" for c, n in form.get("approved") or []) or None
    row["form_interested_sources"] = ("none" if form.get("interested") == [] else
                                      "; ".join(f"{c} | {n}" for c, n in form.get("interested") or []) or None)
    row["form_not_read"] = "; ".join(f"{k}: {v}" for k, v in (form.get("why_not") or {}).items()) or None
    row["values_dropped_by_checks"] = "; ".join(
        f"{d['field']}={d['value']} ({d['reason']})" for d in rec.get("dropped") or []) or None
    row.update(pages=rec.get("pages"), transcribed_pages=rec.get("transcribed_pages"),
               truncated=rec.get("truncated"), chars_sent=rec.get("chars_sent"),
               chars_total=rec.get("chars_total"), model=rec.get("model"),
               extras_version=rec.get("extras_version"), checks_version=rec.get("checks_version"),
               effort=rec.get("effort"), dataset_revision=rec.get("dataset_revision"),
               run_at=rec.get("run_at"), experimental=True)
    return row


# ---------------------------------------------------------------------------
# What is published from the second call (2026-11-11)
# ---------------------------------------------------------------------------
#
# Only fields that held up when 30 random notices were read against the
# document (proof files: ~/Documents/ja-extraction-comparison-2026-11-10/
# group1_check, group2_check; summary in docs/extras.md):
#   contractor named (29 of 30, 1 unclear), new-source approval path (30/30),
#   ceiling changes with all three figures adding up (30/30), GAO protest
#   numbers (all 120 read), DLA approved sources from the form's own table,
#   NSNs (by rule, 55/55).
# Not published: interested firms (26/30), delay (28/30) and cost (25/30) to
# compete, not-to-exceed (25/30), technical-data reason (18/30), unit price,
# quantity, GAO decisions cited, the 52.217-8 flag, barrier-removal quotes.
# A column added here needs an entry in digest/changelog.yaml.

PUBLISHED_COLUMNS = (
    "contractor_named", "contractor_named_quote", "contractor_named_quote_pages",
    "new_source_approval_quote", "new_source_approval_quote_pages",
    "ceiling_prior_usd", "ceiling_increase_usd", "ceiling_new_usd",
    "ceiling_prior_quote", "ceiling_increase_quote", "ceiling_new_quote",
    "gao_protest_numbers", "dla_approved_sources", "nsns", "interested_firms_named",
    "not_to_exceed_usd", "not_to_exceed_quote", "not_to_exceed_quote_pages",
    "delay_to_compete_quote", "delay_to_compete_quote_pages",
    "cost_to_compete_quote", "cost_to_compete_quote_pages",
    "technical_data_reasons", "technical_data_reasons_quote", "technical_data_reasons_quote_pages",
    "second_call_version",
)


# What the published record keeps: only what the published columns are built
# from, and what the daily run needs to tell that a notice is done. The other
# facts the call asks for did not pass their checks and are not published, not
# even inside the stored record ("we only publish what we can justify",
# 2026-11-11). The full records of the 2026-11-10 run are kept outside the
# dataset (~/Documents/ja-extraction-comparison-2026-11-10/extras_full_backup.jsonl).
_PUBLIC_KINDS = ("incumbent_vendor", "source_approval_path", "increase_amount", "prior_ceiling", "new_ceiling",
                 "approved_source", "interested_source")
# Fields kept in the stored record. For delay, cost and the data-rights reason only the
# document's own sentence was checked (and the reasons read from it by rule), so the
# model's own value or category for those is left out.
_PUBLIC_FIELDS = tuple(k + s for k in ("incumbent_vendor", "source_approval_path", "increase_amount",
                                       "prior_ceiling", "new_ceiling", "not_to_exceed")
                       for s in ("", "_quote", "_quote_info")) + tuple(
    k + s for k in ("delay_to_compete", "cost_to_compete", "data_rights_basis") for s in ("_quote", "_quote_info")) + (
    "ceiling_arithmetic_ok", "approved_sources", "interested_sources", "data_rights_reasons")
_BOOKKEEPING = ("experimental", "notice_id", "model", "extras_version", "effort", "checks_version", "run_at",
                "pages", "transcribed_pages", "chars_total", "chars_sent", "truncated", "usage", "dataset_revision")


def public_record(rec: dict | None) -> dict | None:
    """The stored record cut down to the published facts (see _PUBLIC_KINDS)."""
    if not rec:
        return None
    f, r = rec.get("fields") or {}, rec.get("rules") or {}
    form = r.get("form") or None
    # the model's approved sources only matter (and are only checked) on a DLA form
    kinds = _PUBLIC_KINDS if form else tuple(k for k in _PUBLIC_KINDS if k != "approved_source")
    fields = {k: f.get(k) for k in _PUBLIC_FIELDS}
    if not form:
        fields["approved_sources"] = []
    return {**{k: rec.get(k) for k in _BOOKKEEPING},
            "fields": fields,
            "rules": {"gao_protests": r.get("gao_protests") or [], "nsns": r.get("nsns") or [],
                      "nsn_count": r.get("nsn_count"),
                      "form": {"approved": form.get("approved"), "interested": form.get("interested")} if form else None},
            "raw": {"facts": [x for x in (rec.get("raw") or {}).get("facts") or []
                              if isinstance(x, dict) and x.get("kind") in kinds]},
            "published_subset": True}


def published_columns(rec: dict | None) -> dict:
    """The second call's published columns for one notice (all None without a
    record). Values are as the document states them, each with its quote."""
    out = dict.fromkeys(PUBLISHED_COLUMNS)
    if not rec:
        return out
    f, r = rec.get("fields") or {}, rec.get("rules") or {}
    if f.get("incumbent_vendor"):
        out.update(contractor_named=f["incumbent_vendor"], contractor_named_quote=f.get("incumbent_vendor_quote"),
                   contractor_named_quote_pages=_pages(f.get("incumbent_vendor_quote_info")))
    if f.get("source_approval_path_quote"):
        out.update(new_source_approval_quote=f["source_approval_path_quote"],
                   new_source_approval_quote_pages=_pages(f.get("source_approval_path_quote_info")))
    # all three stated, and new = prior + increase (186 notices on 2026-11-10)
    if f.get("ceiling_arithmetic_ok") is True:
        out.update(ceiling_prior_usd=f["prior_ceiling"], ceiling_increase_usd=f["increase_amount"],
                   ceiling_new_usd=f["new_ceiling"], ceiling_prior_quote=f.get("prior_ceiling_quote"),
                   ceiling_increase_quote=f.get("increase_amount_quote"), ceiling_new_quote=f.get("new_ceiling_quote"))
    out["gao_protest_numbers"] = "; ".join(r.get("gao_protests") or []) or None
    out["nsns"] = "; ".join(r.get("nsns") or []) or None
    form = r.get("form") or {}
    if form:
        # the form's own table; not shown when the model names a CAGE code the
        # table lacks (one real conflict in 1,197 tables, see flat_row)
        rows = form.get("approved")
        table_cages = {c for c, _ in rows or []}
        extra = {x["cage"] for x in f.get("approved_sources") or [] if x.get("cage")} - table_cages
        if rows and not extra:
            out["dla_approved_sources"] = "; ".join(f"{c} | {n}" for c, n in rows)
    out["interested_firms_named"] = interested_firms_named(rec)
    # round-3 checks (2026-11-11): not-to-exceed, all 43 remaining values read;
    # delay and cost published as the document's own sentence (30/30, 29/30);
    # the technical-data reasons from the quote's own words, by rule
    if f.get("not_to_exceed") is not None:
        out.update(not_to_exceed_usd=f["not_to_exceed"], not_to_exceed_quote=f.get("not_to_exceed_quote"),
                   not_to_exceed_quote_pages=_pages(f.get("not_to_exceed_quote_info")))
    for k in ("delay_to_compete", "cost_to_compete"):
        if f.get(k) is not None and f.get(k + "_quote"):
            out[k + "_quote"] = f[k + "_quote"]
            out[k + "_quote_pages"] = _pages(f.get(k + "_quote_info"))
    if f.get("data_rights_reasons"):
        out.update(technical_data_reasons=f["data_rights_reasons"], technical_data_reasons_quote=f.get("data_rights_basis_quote"),
                   technical_data_reasons_quote_pages=_pages(f.get("data_rights_basis_quote_info")))
    out["second_call_version"] = f"{rec.get('extras_version')} / checks {rec.get('checks_version')}"
    return out


__all__ = ["EXTRAS_VERSION", "KINDS", "SCHEMA", "TOOL_NAME", "build_prompt", "estimate_cost",
           "extras_document", "extras_documents_batch", "flat_row", "parse_amount", "reverify",
           "split_firm", "verify"]
