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
EXTRAS_CHECKS_VERSION = "2026-11-10.xchecks2"
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
    r"has (been )?perform|since (19|20)\d\d|extension|extend|bridge|follow-on|modif|increase|option|"
    # a past award of the contract: "was awarded to", "Award went to", "Award date:", "contract N39430-23-D-4050 ... awarded to"
    r"(was|were) awarded|award (went|was made)|award date|[A-Z0-9]{4,}-\d{2}-[A-Z]-\d{4}[^.]{0,60}awarded to|"
    r"renewal|continu", re.I)
_INTEREST_CUE = re.compile(
    r"interest|respon|capabilit|sources.sought|\bRFI\b|request for information|quot(e|ation)|inquir|"
    r"submitt|proposal|offer", re.I)
_CAGE_ROW = re.compile(r"\b(?=[0-9A-Z]*[0-9])[0-9A-Z]{5}\s*\|")
_CHECKLIST_LINE = re.compile(r"\bAMS?C\s+[1-5A-Z]\.")
_DATA_CUE = re.compile(r"\bdata\b|drawing|specification|proprietary|rights|technical", re.I)
_COST_CUE = re.compile(r"reverse|re-?engineer|duplicat|qualif|test|develop|transition|migrat|data|rights|"
                       r"new (source|vendor|contractor)|another (source|vendor|contractor)|recompet|compet", re.I)
_APPROVAL_CUE = re.compile(r"source approval|\bSAR\b|L04|M06|alternate offer|approved source|qualif|"
                           r"submit|new sources?", re.I)


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


def verify(raw: dict, doc: Document) -> tuple[dict, list[dict]]:
    """(fields, dropped) from one model answer. ``fields`` has every key of
    the table row, None or an empty list where nothing was kept."""
    nd = doc.normalized()
    fields: dict = {k: None for k in AMOUNT_KINDS + TEXT_KINDS}
    for k in AMOUNT_KINDS + TEXT_KINDS:
        fields[k + "_quote"] = None
        fields[k + "_quote_info"] = None
    for col in FIRM_LIST_FIELD.values():
        fields[col] = []
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
            if not _INCUMBENT_CUE.search(quote):
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
            if kind == "interested_source" and not (_INTEREST_CUE.search(quote) or _CAGE_ROW.search(quote)):
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


def rule_fields(doc: Document) -> dict:
    """What is read from the text by rule, with no model call.

    gao_protests       GAO protest file numbers ("B-424920", "B-424107.1")
    cofc_cases         Court of Federal Claims case numbers ("25-279C"), only
                       in a document that names the court
    nsns               National Stock Numbers as 4-2-3-4 digits, from text that
                       writes them "2840-01-666-1765" or "2915-004499118"
    uses_52_217_8      the text cites FAR 52.217-8 (option to extend services)
    form               dla_form.read for the DLA form, else None
    """
    text = "\n".join(p.text for p in doc.pages)
    gao = sorted(set(_GAO.findall(text)))
    cofc = sorted({re.sub(r"\s", "", x) for x in _COFC.findall(text)}) if _COFC_NEAR.search(text) else []
    nsns = sorted({"-".join(_NSN.match(x).groups()) for x in _NSN_WRITTEN.findall(text) if _NSN.match(x)})
    return {"gao_protests": gao[:MAX_LISTED], "cofc_cases": cofc[:MAX_LISTED], "nsns": nsns[:MAX_LISTED],
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
                           effort: str | None = EXTRAS_EFFORT, max_wait_s: float = 12_000) -> dict:
    """{notice_id: record or llm.ModelError} through the Message Batches API
    (half price). Raises llm.ModelError if the batch can't be created."""
    batch_fn = batch_fn or llm.batch_call
    texts = {d.notice_id: model_text(d) for d in docs}
    by_id = {d.notice_id: d for d in docs}
    kw = {"effort": effort} if effort else {}
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


def flat_row(rec: dict, meta: dict) -> dict:
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
    for col in FIRM_LIST_FIELD.values():
        row[col] = _firms(f.get(col) or [])
        row[col + "_count"] = len(f.get(col) or [])
        row[col + "_match_form"] = match.get(col)
    row["barrier_action_quote"] = f.get("barrier_action_quote")
    row["source_approval_path_quote"] = f.get("source_approval_path_quote")
    row["data_rights_basis"] = f.get("data_rights_basis")
    row["data_rights_basis_quote"] = f.get("data_rights_basis_quote")
    row["delay_to_compete"] = f.get("delay_to_compete")
    row["delay_to_compete_quote"] = f.get("delay_to_compete_quote")
    r = rec.get("rules") or {}
    form = r.get("form") or {}
    row["gao_protests"] = "; ".join(r.get("gao_protests") or []) or None
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


__all__ = ["EXTRAS_VERSION", "KINDS", "SCHEMA", "TOOL_NAME", "build_prompt", "estimate_cost",
           "extras_document", "extras_documents_batch", "flat_row", "parse_amount", "reverify",
           "split_firm", "verify"]
