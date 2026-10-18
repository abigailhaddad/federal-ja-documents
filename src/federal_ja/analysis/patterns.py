"""High-recall keyword patterns that pick the passages a model then checks.

EXPERIMENTAL. A pattern hit is a CANDIDATE passage, never a tag: "this action
is not due to a lack of planning" matches the agency-delay patterns. For the
regex-then-confirm categories, only documents with at least one hit are sent
to the model, and the model sees only the passages around the hits.

Matching runs on normalize()d text and on a copy with in-word hyphens
removed ("reduc-tion" -> "reduction"), case-insensitive, and the hits are
unioned.

The patterns were written and tuned on development documents and then frozen
before the held-out checks in docs/analysis_validation.md. Changing one means
re-validating the category.
"""

from __future__ import annotations

import re

from .text import normalize

# ── agency_delay_cause (frozen 2026-10-16) ─────────────────────────────────

_W1 = r"(?:[\w'()/,.-]+\s+)"  # one "word" plus trailing space, for {0,n} gaps

AGENCY_DELAY_PATTERNS: dict[str, str] = {
    "delay_in_procurement": (r"\bdelay\w*\s+" + _W1 + r"{0,8}(?:follow-on|recompet\w*|re-compet\w*|competitive|"
                             r"procurement|acquisition|solicitation|award|requirements?\s+package|"
                             r"source\s+selection|contract\s+action)"),
    "procurement_delayed": (r"(?:follow-on|recompet\w*|re-compet\w*|procurement|acquisition|solicitation|award|"
                            r"package|requirement)\s+" + _W1 + r"{0,6}(?:was|were|has\s+been|have\s+been|is|are|been)\s+"
                            r"(?:delayed|not\s+(?:yet\s+)?(?:completed|awarded|finalized|ready))"),
    "not_as_planned": (r"not\s+(?:been\s+)?(?:completed|awarded|finalized|executed)\s+"
                       r"(?:in\s+time|as\s+(?:originally\s+)?(?:planned|scheduled|anticipated))"),
    "failed_to_award": r"fail\w*\s+to\s+(?:award|complete|finalize)|unable\s+to\s+(?:award|complete|finalize)",
    "lack_of_planning": r"lack\s+of\s+(?:advance\s+|advanced\s+)?(?:acquisition\s+)?planning",
    "funding_approval_delay": (r"(?:funding|approvals?|requirements?|review)\s+" + _W1
                               + r"{0,3}delay\w*|delay\w*\s+in\s+(?:funding|approv\w+|receiving|obtaining)"),
    "expired": r"(?:contract|order|agreement|period\s+of\s+performance)\s+(?:has\s+|had\s+)?expired",
    "insufficient_time": r"(?:insufficient|not\s+enough|inadequate)\s+time",
}
# A hit whose sentence blames a protest or a court is dropped: the category
# excludes protest-caused delay.
PROTEST = re.compile(r"protest|\bGAO\b|court\s+of\s+federal\s+claims|\bCOFC\b|corrective\s+action", re.I)

# ── sources_rejected (frozen 2026-10-16) ──

_W2 = r"(?:[\w'()/,.&$-]+\s+)"

PATTERNS2: dict[str, list[str]] = {
    "sources_rejected": [
        r"(?:responses?|responded|replies|replied)\s+" + _W2 + r"{0,6}(?:sources?[\s-]+sought|\bRFI\b|request\s+for\s+information|synopsis|notice|announcement|posting)",
        r"(?:sources?[\s-]+sought|\bRFI\b|request\s+for\s+information|synopsis|notice\s+of\s+intent|special\s+notice)\s+" + _W2 + r"{0,10}(?:responses?|responded|yielded|received|resulted\s+in)",
        r"capability\s+statements?|expressed\s+(?:an\s+)?interest|interested\s+(?:parties|vendors|sources|firms|companies|offerors)",
        r"(?:did\s+not|does\s+not|do\s+not|failed\s+to|unable\s+to)\s+(?:demonstrate|meet|possess|show|satisfy)",
        r"\bnot\s+(?:capable|qualified|acceptable|responsive|technically\s+acceptable|an\s+authorized)\b|\bincapable\b|\bunqualified\b|technically\s+unacceptable|non-?responsive",
        r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|several|multiple)\s+(?:\w+\s+){0,2}(?:vendors?|firms?|companies|sources|contractors|businesses|offerors|respondents)\s+(?:\w+\s+){0,3}(?:responded|submitted|expressed|replied|provided)",
    ],
}

# Sentences dropped for a category: boilerplate the category excludes.
EXCLUDE2 = {
    "sources_rejected": re.compile(r"\bno\s+(?:responses?|vendors?|firms?|other)\b|did\s+not\s+receive\s+any", re.I),
}

# ── workforce_cause gate (round 4, written 2026-10-17 on design data only) ──
# A federal workforce / administrative event: deferred resignation, RIF,
# hiring freeze, DOGE / workforce or cost-efficiency EOs, VERA/VSIP, staff
# losses, vacancies, attrition, retirements, reassignments, shutdown /
# lapse / furlough, reorganisation. Broad on purpose: the model then decides
# whether the document ties the event to THIS action.
_WN = r"(?:personnel|staff|staffing|employees|workforce|FTEs?|contracting\s+officers?|contract\s+specialists?|" \
      r"billets|positions|manning|manpower|team\s+members|CORs?|program\s+managers?|engineers|officers)"
WORKFORCE_PATTERNS: dict[str, str] = {
    "drp": r"\bdef\w{0,4}ed[\s-]*resig\w*|\bDRP\b|fork\s+in\s+the\s+road",
    "rif": r"reductions?[\s-]+in[\s-]+force|\bRIFs?\b|\bRIF'?d\b",
    "hiring_freeze": r"hiring[\s-]+(?:freeze|pause|restrictions?)",
    "doge": r"\bDOGE\b|department\s+of\s+government\s+efficiency",
    "workforce_eo": r"(?:executive\s+order|\bE\.?\s?O\.?)\s*(?:no\.?\s*)?1(?:4158|4170|4210|4222|4240)\b",
    "vera_vsip": r"\bVERA\b|\bVSIP\b|voluntary\s+(?:early\s+)?(?:retirement|separation)",
    "staff_loss": (r"(?:loss|losses|lost|departures?|attrition|reductions?|shortages?|decrease|turnover|vacanc\w+)\s+"
                   r"(?:of|in)\s+" + _W1 + r"{0,4}" + _WN),
    "staff_noun_loss": (r"(?:staff|staffing|personnel|workforce|manning|manpower)\s+"
                        r"(?:shortages?|reductions?|losses|cuts|constraints|challenges|deficits?|changes|turnover|levels)"),
    "lost_n": r"\blost\s+" + _W1 + r"{0,3}" + _WN,
    "vacancy": r"\bvacan(?:t|cy|cies)\b",
    "attrition": r"\battrition\b|\bturnover\b|short[\s-]*staffed|understaffed",
    "departure": (r"\b(?:departure|reassignment|retirement|resignation)\s+of\s+" + _W1 + r"{0,4}" + _WN +
                  r"|\b(?:departed|retired|resigned|reassigned)\b|left\s+(?:the\s+)?(?:agency|government|federal\s+service)"),
    "workforce_reduction": r"workforce\s+(?:reduction|optimization|realignment|restructur\w*|cuts?)",
    "backfill": r"(?:cannot|can\s*not|could\s+not|unable\s+to|not)\s+(?:be\s+)?backfill\w*",
    "shutdown": r"(?:government|federal)\s+(?:government\s+)?shutdowns?|\bshutdown\b|lapses?\s+in\s+(?:federal\s+)?appropriations?|furlough\w*",
    "reorg": r"\bre-?organiz\w+|\bre-?structur\w+|\bconsolidation\s+of\s+" + _W1 + r"{0,3}(?:offices?|operations|functions|agencies)",
    "retirements": r"\bretirements\b|\bresignations?\b",
}
_CW = {name: re.compile(p, re.I) for name, p in WORKFORCE_PATTERNS.items()}


_C1 = {name: re.compile(p, re.I) for name, p in AGENCY_DELAY_PATTERNS.items()}
_C2 = {c: [re.compile(p, re.I) for p in ps] for c, ps in PATTERNS2.items()}
_INWORD_HYPHEN = re.compile(r"(?<=[a-z])-(?=[a-z])")
_SENT_END = re.compile(r"(?<=[.;:!?])\s")

Hit = tuple[int, int, str]   # (start, end) in the normalised text, sentence


def _sentence_bounds(text: str, start: int, end: int, cap: int) -> tuple[int, int]:
    left = max(text.rfind(". ", 0, start), text.rfind("; ", 0, start))
    s = left + 2 if left >= 0 and start - left < cap else max(0, start - cap // 2)
    m = _SENT_END.search(text, end)
    e = m.start() if m and m.start() - end < cap else min(len(text), end + cap // 2)
    return s, e


def _agency_delay_hits(norm: str) -> list[Hit]:
    variants = [norm]
    dehyph = _INWORD_HYPHEN.sub("", norm)
    if dehyph != norm:
        variants.append(dehyph)
    hits: list[Hit] = []
    seen: set[tuple[str, str]] = set()
    for name, rx in _C1.items():
        for v in variants:
            for m in rx.finditer(v):
                s, e = _sentence_bounds(v, m.start(), m.end(), 400)
                sent = v[s:e].strip()
                if PROTEST.search(sent):
                    continue
                if (name, sent) in seen:
                    continue
                seen.add((name, sent))
                # A hit found only in the de-hyphenated copy is located
                # approximately in the normalised text.
                pos = norm.find(sent[:40]) if v is not norm else m.start()
                pos = max(pos, 0)
                hits.append((pos, pos + (m.end() - m.start()), sent))
    return hits


def _hits2(norm: str, cat: str) -> list[Hit]:
    out: list[Hit] = []
    seen: set[str] = set()
    for variant in (norm, _INWORD_HYPHEN.sub("", norm)):
        for rx in _C2[cat]:
            for m in rx.finditer(variant):
                a, b = _sentence_bounds(variant, m.start(), m.end(), 450)
                sent = variant[a:b]
                if EXCLUDE2[cat].search(sent) or sent in seen:
                    continue
                seen.add(sent)
                pos = m.start() if variant is norm else max(0, norm.find(sent[:40]))
                out.append((pos, pos + (m.end() - m.start()), sent))
    return out


def _workforce_hits(norm: str) -> list[Hit]:
    """Same mechanics as agency_delay_cause (normalised + de-hyphenated
    variants, sentence context, dedupe per pattern and sentence), without
    the protest exclusion."""
    variants = [norm]
    dehyph = _INWORD_HYPHEN.sub("", norm)
    if dehyph != norm:
        variants.append(dehyph)
    hits: list[Hit] = []
    seen: set[tuple[str, str]] = set()
    for name, rx in _CW.items():
        for v in variants:
            for m in rx.finditer(v):
                s, e = _sentence_bounds(v, m.start(), m.end(), 400)
                sent = v[s:e].strip()
                if (name, sent) in seen:
                    continue
                seen.add((name, sent))
                pos = norm.find(sent[:40]) if v is not norm else m.start()
                pos = max(pos, 0)
                hits.append((pos, pos + (m.end() - m.start()), sent))
    return hits


PATTERN_CATEGORIES = ("agency_delay_cause", "workforce_cause", *PATTERNS2)


def find_hits(raw_text: str, category: str) -> list[Hit]:
    """Candidate passages for one category in a document's verification
    text. Offsets are in normalize(raw_text)."""
    norm = normalize(raw_text)
    if category == "agency_delay_cause":
        return _agency_delay_hits(norm)
    if category == "workforce_cause":
        return _workforce_hits(norm)
    if category in PATTERNS2:
        return _hits2(norm, category)
    raise ValueError(f"no patterns for {category!r}")


def excerpts(normalized_doc: str, hits: list[Hit], width: int = 1500, max_chars: int = 12000) -> list[str]:
    """The passages the model sees: each hit plus `width` characters either
    side, overlapping windows merged, up to about max_chars in total."""
    spans = sorted((max(0, s - width), min(len(normalized_doc), e + width)) for s, e, _ in hits)
    merged: list[list[int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out, total = [], 0
    for s, e in merged:
        if total >= max_chars:
            break
        out.append(normalized_doc[s:e])
        total += e - s
    return out
