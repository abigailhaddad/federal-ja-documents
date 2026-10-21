"""The model calls: an optional cheap screen, then one ranking call.

Both go through ``federal_ja.analysis.llm.call`` (strict tool schema, the
instructions sent as a cached first block, usage priced per call), so the
model, pricing and retry rules are the analysis package's.

Size and cost. Each candidate is sent as a compact card: SAM.gov metadata,
the extraction's fields when it has a row, and an excerpt of its text with
page markers. The excerpt takes the attachment whose filename looks like the
justification first, then the others, page by page, up to a per-candidate
character cap; the SAM.gov description is added at the end (truncated).

* At most ``shortlist`` (default 20) candidates go to the ranking model at up
  to ``RANK_CHARS`` characters each. With more candidates than that, the
  screen (Haiku) scores every one from a short excerpt in batches and the
  top-scored ``shortlist`` go on (ties: newer posting first).
* Before each call the worst case (input estimate + max_tokens of output) is
  checked against what is left of ``--max-usd``; if the ranking call would
  not fit, the shortlist is cut until it does, and if even five candidates do
  not fit the run stops (CapError) and sends nothing.
"""

from __future__ import annotations

import re
from importlib.resources import files

import yaml

from ..analysis import llm
from .candidates import DESCRIPTION_PAGE, Candidate

CONFIG: dict = yaml.safe_load(files(__package__).joinpath("prompts.yaml").read_text(encoding="utf-8"))
VERSION = CONFIG["version"]

SCREEN_CHARS = 1_800
RANK_CHARS = 7_000
DESCRIPTION_CHARS = 800
SCREEN_BATCH = 40
CHARS_PER_TOKEN = 3.2      # conservative (fewer chars per token = higher estimate)

_JA_NAME = re.compile(r"justif|j\s*&\s*a|\bja\b|_ja\b|\bja_|sole.?source|single.?source|\blsj\b|jofoc|"
                      r"exception|limited.?source|brand.?name|\bnoi\b|intent", re.I)

SCREEN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["scores"],
    "properties": {"scores": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["notice_id", "score", "reason"],
        "properties": {"notice_id": {"type": "string"}, "score": {"type": "integer"},
                       "reason": {"type": "string"}}}}}}

RANK_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["picks"],
    "properties": {"picks": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["notice_id", "headline", "why", "quotes", "vendor", "value", "authority"],
        "properties": {
            "notice_id": {"type": "string"},
            "headline": {"type": "string"},
            "why": {"type": "string"},
            "quotes": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["text", "page"],
                "properties": {"text": {"type": "string"}, "page": {"type": "string"}}}},
            "vendor": {"type": "string"},
            "value": {"type": "string"},
            "authority": {"type": "string"}}}}}}


class CapError(RuntimeError):
    """The next call could exceed the spend cap."""


def excerpt(c: Candidate, max_chars: int) -> tuple[str, int, int]:
    """(text with page markers, chars of document text included, chars total)."""
    atts = [(fn, pages) for fn, pages in c.doc.attachments() if fn != DESCRIPTION_PAGE]
    order = sorted(range(len(atts)), key=lambda i: (0 if _JA_NAME.search(atts[i][0] or "") else 1, i))
    parts, used, total = [], 0, sum(len(p.text) for _, pages in atts for p in pages)
    for i in order:
        fn, pages = atts[i]
        for p in pages:
            t = p.text.strip()
            if not t:
                continue
            room = max_chars - used
            if room <= 200:
                break
            marker = f"[{fn}, page {p.page_number}]" if p.page_number is not None else f"[{fn}]"
            if p.text_source == "transcription":
                marker = marker[:-1] + ", machine-transcribed]"
            chunk = t if len(t) <= room else t[:room].rsplit(" ", 1)[0] + " [...cut]"
            parts.append(marker + "\n" + chunk)
            used += min(len(t), room)
        if max_chars - used <= 200:
            break
    if total > used:
        parts.append(f"[document text cut: {used:,} of {total:,} characters shown]")
    return "\n\n".join(parts), used, total


def _ext_line(e: dict) -> str:
    if not e:
        return ""
    bits = []
    for k, label in (("is_ja", "is a J&A"), ("vendor", "vendor"), ("authority_cited", "authority"),
                     ("urgency_trigger", "urgency trigger"), ("bridge_or_extension", "bridge/extension"),
                     ("prior_extension_count", "prior extensions"), ("is_letter_contract", "letter contract"),
                     ("funding_sources", "funding")):
        v = e.get(k)
        if v not in (None, "", []):
            bits.append(f"{label}: {v}")
    v = e.get("estimated_value_usd")
    if isinstance(v, (int, float)) and v == v:
        bits.append(f"value: ${v:,.0f} ({e.get('value_basis') or 'basis not stated'})")
    elif e.get("value_redacted") is True:
        bits.append("value: redacted")
    out = "AI-extracted fields (hints; check against the text): " + "; ".join(bits)
    if e.get("summary_model_written"):
        out += f"\nAI summary: {e['summary_model_written']}"
    return out


def card(c: Candidate, max_chars: int) -> str:
    m = c.meta
    org = " / ".join(x for x in (m.get("agency"), m.get("sub_tier"), m.get("office")) if x)
    lines = [f"### CANDIDATE notice_id={c.notice_id}",
             f"Title: {m.get('title')}", f"Agency: {org}",
             f"Notice type: {m.get('type')}; posted {m.get('posted_date')}"]
    if m.get("award_number"):
        lines.append(f"Award number: {m['award_number']}")
    e = _ext_line(c.extraction)
    if e:
        lines.append(e)
    text, used, _ = excerpt(c, max_chars)
    desc = next((p.text for p in c.doc.pages if p.filename == DESCRIPTION_PAGE), "")
    desc = " ".join(desc.split())
    # the description gets the whole budget when there is no document text
    # (many Special Notices), else a short slice
    cap = DESCRIPTION_CHARS if used else max_chars
    if desc:
        d = desc if len(desc) <= cap else desc[:cap].rsplit(" ", 1)[0] + " [...cut]"
        lines.append(f"[{DESCRIPTION_PAGE}]\n{d}")
    lines.append(text if text else "(no document text)")
    return "\n".join(lines)


def est_cost(model: str, prompt_chars: int, max_tokens: int) -> float:
    inp = int(prompt_chars / CHARS_PER_TOKEN)
    return llm.cost_usd(model, inp, max_tokens) or 0.0


def _call(stage: str, cards: list[str], schema: dict, tool: str, budget: dict, call_fn) -> tuple[dict, dict]:
    cfg = CONFIG[stage]
    instructions = cfg["prompt"] + "\n\n"
    rest = "\n\n".join(cards)
    worst = est_cost(cfg["model"], len(instructions) + len(rest), cfg["max_tokens"])
    if budget["spent"] + worst > budget["max_usd"]:
        raise CapError(f"{stage}: worst case ${worst:.3f} would pass the cap "
                       f"(${budget['spent']:.3f} of ${budget['max_usd']:.2f} spent)")
    raw, usage = call_fn((instructions, rest), schema=schema, tool_name=tool, model=cfg["model"],
                         max_tokens=cfg["max_tokens"], effort=cfg.get("effort"), timeout=600)
    budget["spent"] += float(usage.get("cost_usd") or 0)
    budget["calls"].append({"stage": stage, **usage, "worst_case_usd": round(worst, 4)})
    return raw, usage


def screen(cands: list[Candidate], budget: dict, call_fn=None, batch: int = SCREEN_BATCH) -> dict[str, dict]:
    """{notice_id: {score, reason}} for every candidate (score -1 if the
    model left one out)."""
    call_fn = call_fn or llm.call
    out: dict[str, dict] = {}
    for i in range(0, len(cands), batch):
        chunk = cands[i:i + batch]
        raw, _ = _call("screen", [card(c, SCREEN_CHARS) for c in chunk], SCREEN_SCHEMA, "record_scores",
                       budget, call_fn)
        ids = {c.notice_id for c in chunk}
        for s in (raw or {}).get("scores") or []:
            if s.get("notice_id") in ids:
                out[s["notice_id"]] = {"score": int(s.get("score") or 0), "reason": s.get("reason") or ""}
        for c in chunk:
            out.setdefault(c.notice_id, {"score": -1, "reason": "not scored by the model"})
    return out


def shortlist(cands: list[Candidate], scores: dict[str, dict], n: int) -> list[Candidate]:
    """Top n by score; candidates arrive newest posting first, and the sort
    is stable, so ties keep that order. A candidate the screen did not score
    (-1) sorts last but is not dropped before the cut."""
    return sorted(cands, key=lambda c: -scores.get(c.notice_id, {"score": -1})["score"])[:n]


def rank(cands: list[Candidate], budget: dict, call_fn=None, min_keep: int = 5) -> tuple[dict, list[Candidate]]:
    """One ranking call. Returns (raw answer, the candidates actually sent)."""
    call_fn = call_fn or llm.call
    cfg = CONFIG["rank"]
    sent = list(cands)
    while True:
        cards = [card(c, RANK_CHARS) for c in sent]
        worst = est_cost(cfg["model"], len(cfg["prompt"]) + 2 + sum(len(x) + 2 for x in cards), cfg["max_tokens"])
        if budget["spent"] + worst <= budget["max_usd"] or len(sent) <= min_keep:
            break
        sent = sent[:-1]
    raw, _ = _call("rank", cards, RANK_SCHEMA, "record_picks", budget, call_fn)
    return raw, sent
