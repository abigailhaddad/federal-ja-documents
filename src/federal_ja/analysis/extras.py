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
* stated_total, a whole-action total the document prints beside a per-period
  or per-unit figure;
* incumbent_vendor;
* approved_source and interested_source, one entry per firm (the two tables
  on Defense Logistics Agency forms);
* barrier_action, what the document says will be done to open competition.

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
from .extract import (
    MAX_OUTPUT_TOKENS,
    MIN_FIELD_QUOTE_WORDS,
    _blank,
    _metadata,
    _qinfo,
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
EXTRAS_CHECKS_VERSION = "2026-11-10.xchecks1"
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

AMOUNT_KINDS = ("increase_amount", "prior_ceiling", "new_ceiling", "not_to_exceed", "unit_price", "stated_total")
FIRM_LIST_KINDS = ("approved_source", "interested_source")
KINDS = AMOUNT_KINDS + ("quantity", "incumbent_vendor", "barrier_action") + FIRM_LIST_KINDS
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
    fields: dict = {k: None for k in AMOUNT_KINDS + ("quantity", "incumbent_vendor", "barrier_action")}
    for k in AMOUNT_KINDS + ("quantity", "incumbent_vendor", "barrier_action"):
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
            kept = value
        elif kind == "barrier_action":
            kept = display_quote(quote)
        else:   # a firm list
            cage, name = split_firm(value)
            if not name_in(name, quote):
                drop(kind, value, quote, "name not in its quote")
                continue
            if cage and not name_in(cage, quote):
                drop(kind, value, quote, "CAGE code not in its quote")
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
# before a batch is sent. Not yet measured: the instruction block is about
# 1,300 tokens, and 6,000 output tokens (thinking included) is a guess set
# above what the pricing estimate expected (2,500 to 4,000). Replace both
# with measured numbers after the pilot.
EST_TOKENS_PER_CHAR = 0.45
EST_INSTRUCTION_TOKENS = 1_400
EST_OUTPUT_TOKENS = 6_000


def estimate_cost(doc: Document, model: str = llm.DEFAULT_MODEL, *, batch: bool = True) -> float:
    text, _ = model_text(doc)
    tokens_in = EST_INSTRUCTION_TOKENS + int(len(text) * EST_TOKENS_PER_CHAR)
    return llm.cost_usd(model, tokens_in, EST_OUTPUT_TOKENS,
                        price_mult=llm.BATCH_PRICE_MULT if batch else 1.0) or 0.0


def reverify(rec: dict, doc: Document) -> dict:
    """The stored model answer under the current checks; no model call."""
    fields, dropped = verify(rec["raw"], doc)
    return {**rec, "fields": fields, "dropped": dropped, "checks_version": EXTRAS_CHECKS_VERSION}


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
    for col in FIRM_LIST_FIELD.values():
        row[col] = _firms(f.get(col) or [])
        row[col + "_count"] = len(f.get(col) or [])
    row["barrier_action_quote"] = f.get("barrier_action_quote")
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
