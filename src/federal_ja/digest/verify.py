"""Checks on the model's picks. The code, not the model, decides what is sent.

* QUOTES: every quote must be found in the notice's own text (its document
  pages, or its SAM.gov description) by the analysis package's
  ``locate_quote``: exact, or after ``normalize`` (curly quotes and dashes
  straightened, ligature glyphs expanded, whitespace runs collapsed, line-wrap
  hyphens joined), or differing only in case; failing that, against the text
  with words split across a line break rejoined ("reduc-/tion" ->
  "reduction"), which normalize() leaves as "reduc-tion". At least ``MIN_QUOTE_WORDS``
  (5) words and no ellipsis. A failed quote is dropped; a pick with no quote
  left is dropped. The page shown is where the CODE found the quote, not
  what the model said.
* DOLLAR FIGURES in the headline and the "why" must match an amount in one
  of the pick's verified quotes, or the extraction's quote-checked
  ``estimated_value_usd``. A shortened figure passes when it is a rounding of
  such an amount at the precision written: for $28,950,000,000, "$29
  billion" and "$28.95 billion" pass, "$30 billion" and "$28.8 billion" do
  not. Sums, conversions and other arithmetic never pass.
* OTHER NUMBERS (digits) in the headline and "why" must appear in the
  notice's text or metadata.
* A "why" sentence that fails a check is removed; a headline that fails, or a
  "why" with no sentence left, drops the pick.
* Vendor, value and authority for the summary line: the extraction's
  quote-checked fields first; the model's own only if found in the text (the
  value only if it is an amount in a verified quote).
"""

from __future__ import annotations

import re

from ..analysis.extract import amounts_in, name_in, value_in_quote
from ..analysis.text import MIN_QUOTE_WORDS, Document, Page, clean_quote, display_quote, locate_quote, normalize
from .candidates import DESCRIPTION_PAGE, Candidate

MAX_PICKS = 5
MAX_QUOTES = 2

_MULT = {"thousand": 1e3, "k": 1e3, "million": 1e6, "m": 1e6, "mm": 1e6, "mil": 1e6,
         "billion": 1e9, "b": 1e9, "bn": 1e9, "trillion": 1e12, "t": 1e12}
# "$28.95 billion", "$29B", "$1,234,567", "20 billion", "$750M"
_PROSE_AMOUNT = re.compile(
    r"\$\s?(?P<n1>\d[\d,]*(?:\.\d+)?)(?:\s*(?P<m1>thousand|million|billion|trillion|mil|bn|mm|k|m|b|t)\b)?"
    r"|\b(?P<n2>\d[\d,]*(?:\.\d+)?)\s*(?P<m2>thousand|million|billion|trillion)\b", re.I)
_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?![\w])")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\[])")


def prose_amounts(text: str) -> list[tuple[float, float, tuple[int, int]]]:
    """(value, rounding tolerance, span) for each dollar figure in prose."""
    out = []
    for m in _PROSE_AMOUNT.finditer(text or ""):
        num = m.group("n1") or m.group("n2")
        mult_s = (m.group("m1") or m.group("m2") or "").lower()
        if m.group("n2") and not mult_s:
            continue
        try:
            v = float(num.replace(",", ""))
        except ValueError:
            continue
        decimals = len(num.split(".")[1]) if "." in num else 0
        mult = _MULT.get(mult_s, 1.0)
        tol = 0.5 * 10 ** (-decimals) * mult if mult_s else max(0.5 * 10 ** (-decimals), 1.0)
        out.append((v * mult, tol, m.span()))
    return out


def amount_supported(value: float, tol: float, sources: list[float]) -> bool:
    return any(abs(s - value) <= tol + 1e-6 or value_in_quote(value, f"${s:,.2f}") for s in sources)


def _numbers_ok(text: str, spans: list[tuple[int, int]], haystack: str) -> list[str]:
    """Digit tokens outside dollar figures that are NOT in the notice's text."""
    bad = []
    for m in _NUMBER.finditer(text):
        if any(a <= m.start() < b for a, b in spans):
            continue
        tok = m.group().rstrip(".,")
        if tok and tok not in haystack and tok.replace(",", "") not in haystack.replace(",", ""):
            bad.append(tok)
    return bad


def check_text(text: str, amount_sources: list[float], haystack: str) -> list[str]:
    """Problems with one piece of model-written text (empty list = fine)."""
    problems = []
    found = prose_amounts(text)
    for v, tol, span in found:
        if not amount_supported(v, tol, amount_sources):
            problems.append(f"unsupported dollar figure {text[span[0]:span[1]]!r}")
    for tok in _numbers_ok(text, [s for *_, s in found], haystack):
        problems.append(f"number {tok!r} not in the notice")
    return problems


_LINE_WRAP_HYPHEN = re.compile(r"(?<=[a-z])-[ \t]*\r?\n\s*(?=[a-z])")


def _dehyphenated(doc: Document) -> Document:
    """The document with words split across a line break rejoined
    ("reduc-\ntion" -> "reduction"), for a second quote check: normalize()
    keeps such a hyphen ("reduc-tion"), so a model that copies the word whole
    would otherwise fail. Only lowercase-hyphen-newline-lowercase is joined, so
    real compounds at a line end ("cost-\nplus") can be lost; the first, strict
    check runs before this one."""
    return Document(doc.notice_id, doc.meta, [Page(p.resource_id, p.filename, p.page_number, p.text_source,
                                                   _LINE_WRAP_HYPHEN.sub("", p.text)) for p in doc.pages])


def _page_label(cand: Candidate, pages: list[dict]) -> str:
    if not pages:
        return ""
    names = {}
    for p in cand.doc.pages:
        names.setdefault(p.resource_id, p.filename)
    first, last = pages[0], pages[-1]
    fn = names.get(first["resource_id"], "document")
    if fn == DESCRIPTION_PAGE:
        return DESCRIPTION_PAGE
    if first.get("page_number") is None:
        label = fn
    elif first is last or first["page_number"] == last.get("page_number"):
        label = f"{fn}, p. {first['page_number']}"
    else:
        label = f"{fn}, pp. {first['page_number']}-{last['page_number']}"
    if any(p.get("text_source") == "transcription" for p in pages):
        label += " (machine-transcribed page)"
    return label


def _amount_sources(quotes: list[dict], ext: dict) -> list[float]:
    src = [a for q in quotes for a in amounts_in(q["text"])]
    v = ext.get("estimated_value_usd")
    if isinstance(v, (int, float)) and v == v:
        src.append(float(v))
    return src


def _fmt_usd(v: float) -> str:
    return f"${v:,.0f}" if v >= 1 else f"${v:,.2f}"


_BASIS = {"total_including_options": "total incl. options", "base_only": "base period only",
          "total_options_not_stated": "total", "modification_or_increase_amount": "modification/increase",
          "ceiling_or_maximum": "ceiling", "annual_or_per_period": "per year/period"}


def verify_picks(raw: dict, cands: dict[str, Candidate]) -> tuple[list[dict], list[dict]]:
    """(kept picks, dropped items with reasons)."""
    kept, dropped = [], []
    seen = set()
    for i, p in enumerate((raw or {}).get("picks") or []):
        nid = (p.get("notice_id") or "").strip()
        if len(kept) >= MAX_PICKS:
            dropped.append({"notice_id": nid, "what": "pick", "reason": f"more than {MAX_PICKS} picks"})
            continue
        if nid not in cands or nid in seen:
            dropped.append({"notice_id": nid, "what": "pick", "reason": "unknown or repeated notice_id"})
            continue
        c = cands[nid]
        nd = c.doc.normalized()
        dehyph = _dehyphenated(c.doc)
        quotes = []
        for q in (p.get("quotes") or [])[:MAX_QUOTES]:
            m = locate_quote(q.get("text"), c.doc, nd, min_words=MIN_QUOTE_WORDS, ignore_case=True)
            if not m.verified:
                m = locate_quote(q.get("text"), dehyph, dehyph.normalized(), min_words=MIN_QUOTE_WORDS,
                                 ignore_case=True)
                if m.verified:
                    m.match = "dehyphenated"
            if not m.verified:
                dropped.append({"notice_id": nid, "what": "quote", "value": q.get("text"),
                                "reason": "not found in the notice text"})
                continue
            quotes.append({"text": display_quote(clean_quote(q["text"])), "match": m.match,
                           "pages": m.pages, "where": _page_label(c, m.pages),
                           "model_page": q.get("page") or ""})
        for q in (p.get("quotes") or [])[MAX_QUOTES:]:
            dropped.append({"notice_id": nid, "what": "quote", "value": q.get("text"),
                            "reason": f"more than {MAX_QUOTES} quotes"})
        if not quotes:
            dropped.append({"notice_id": nid, "what": "pick", "reason": "no quote verified"})
            continue
        ext = c.extraction or {}
        sources = _amount_sources(quotes, ext)
        meta_text = " ".join(str(c.meta.get(k) or "") for k in ("title", "award_number", "solicitation_number",
                                                                "posted_date", "agency", "sub_tier", "office"))
        haystack = normalize(c.doc.verification_text() + " " + meta_text)
        headline = " ".join((p.get("headline") or "").split())
        hp = check_text(headline, sources, haystack)
        if not headline or hp:
            dropped.append({"notice_id": nid, "what": "pick", "value": headline,
                            "reason": "headline: " + ("; ".join(hp) or "empty")})
            continue
        why_kept = []
        for s in _SENTENCE.split(" ".join((p.get("why") or "").split())):
            sp = check_text(s, sources, haystack)
            if sp:
                dropped.append({"notice_id": nid, "what": "why_sentence", "value": s, "reason": "; ".join(sp)})
            elif s:
                why_kept.append(s)
        if not why_kept:
            dropped.append({"notice_id": nid, "what": "pick", "reason": "no 'why' sentence passed the checks"})
            continue
        vt = c.doc.verification_text()
        vendor = ext.get("vendor") or ((p.get("vendor") or "").strip() or None)
        vendor_src = "extraction" if ext.get("vendor") else "model"
        if vendor and vendor_src == "model" and not name_in(vendor, vt):
            dropped.append({"notice_id": nid, "what": "vendor", "value": vendor, "reason": "not in the text"})
            vendor = None
        value = None
        ev = ext.get("estimated_value_usd")
        if isinstance(ev, (int, float)) and ev == ev:
            basis = _BASIS.get(ext.get("value_basis") or "")
            value = _fmt_usd(float(ev)) + (f" ({basis})" if basis else "")
        elif ext.get("value_redacted") is True:
            value = "value redacted"
        elif (p.get("value") or "").strip():
            mv = p["value"].strip()
            qa = [a for q in quotes for a in amounts_in(q["text"])]
            if any(amount_supported(v, tol, qa) for v, tol, _ in prose_amounts(mv)):
                value = mv
            else:
                dropped.append({"notice_id": nid, "what": "value", "value": mv,
                                "reason": "not an amount in a verified quote"})
        authority = ext.get("authority_cited") or ((p.get("authority") or "").strip() or None)
        if authority and not ext.get("authority_cited"):
            if normalize(authority).casefold() not in normalize(vt).casefold():
                dropped.append({"notice_id": nid, "what": "authority", "value": authority,
                                "reason": "not in the text"})
                authority = None
        seen.add(nid)
        kept.append({"notice_id": nid, "rank": len(kept) + 1, "headline": headline, "why": " ".join(why_kept),
                     "quotes": quotes, "vendor": vendor, "value": value, "authority": authority,
                     "model_rank": i + 1})
    return kept, dropped
