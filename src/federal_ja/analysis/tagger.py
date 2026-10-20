"""Tag possible issues in one J&A document. EXPERIMENTAL.

Every tag is a POSSIBLE ISSUE FOR A PERSON TO CHECK against the document. A
tag is never a finding that anything is wrong, improper or illegal, and two of
the categories are tracked facts rather than issues at all.

Each tag must carry a verbatim quote that is found in the document's page
text (exactly, or after whitespace/quote-mark normalisation); a tag whose
quote can't be found is dropped. The quote is located on its page(s), and the
tag records whether that page was machine-transcribed.

Every document is reviewed in two passes and the union of verified tags is
kept, because model output varies between identical calls. Each tag records
which passes found it.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from . import llm, patterns, prompts
from .text import Document, clean_quote, display_quote, locate_quote, normalize, quote_ok

PASSES = 2
NOTE = "Possible issue for a person to check against the document. Not a finding."


@dataclass(frozen=True)
class Category:
    name: str
    method: str          # "full" | "confirm"
    kind: str            # "possible issue" | "tracked fact"
    description: str


# Statuses come from docs/analysis_validation.md. Only categories in SHIPPED
# can be run; see STATUS for each one's measured numbers.
CATEGORIES: dict[str, Category] = {c.name: c for c in (
    Category("workforce_cause", "confirm", "tracked fact",
             "The J&A says a federal workforce or administrative event (DOGE, the Deferred Resignation "
             "Program, a RIF, a hiring freeze, staff losses, a reorganisation, a shutdown) caused or "
             "contributed to the need, the timing or a delay."),
    Category("agency_delay_cause", "confirm", "possible issue",
             "The J&A's own narrative says the agency's own planning, staffing, requirements, reviews, "
             "approvals or funding delayed the competitive action (FAR 6.301(c)(1))."),
    Category("sources_rejected", "confirm", "tracked fact",
             "Other firms responded or expressed interest and the agency found them not capable or not "
             "acceptable."),
)}

# Statuses from the round-3 validation (2026-10-17, docs/analysis_validation.md),
# applied mechanically by rules fixed before scoring: "ready" needs a 95%
# Wilson lower bound on random-sample precision >= 80% and run-to-run
# stability >= 90%; "screening" needs the lower bound >= 50%; anything else is
# not shipped. No category reached "ready". workforce_cause was re-tested in
# round 4 with a new method (keyword gate, then confirm) on fresh documents.
STATUS: dict[str, str] = {
    "agency_delay_cause": "screening",
    "sources_rejected": "screening",
    "workforce_cause": "screening",
}
# Random-sample precision (95% Wilson interval) behind each status, for help
# text and refusals. The full table is in docs/analysis_validation.md.
STATUS_NOTE: dict[str, str] = {
    "agency_delay_cause": "precision 12/15 = 80% (55-93%), recall 34% (21-51%) on 400 random J&As",
    "sources_rejected": "precision 24/34 = 71% (54-83%), recall 71% (54-83%) on 400 random J&As",
    "workforce_cause": ("keyword gate + confirm (round 4): precision 11/12 = 92% (65-99%), recall 11/16 = 69% "
                        "(44-86%) on 400 fresh random J&As"),
}
SHIPPED_STATUSES = ("ready", "screening")



def shipped() -> list[str]:
    return [c for c in CATEGORIES if STATUS.get(c) in SHIPPED_STATUSES]


CallFn = Callable[..., tuple[dict, dict]]


def _call(call_fn: CallFn, prompt: llm.Prompt, schema: dict, tool: str, model: str, pass_no: int):
    return call_fn(prompt, schema=schema, tool_name=tool, model=model, pass_no=pass_no)


def _verify(cat: str, item: dict, nd: str) -> dict | None:
    if not quote_ok(item.get("quote"), nd):
        return None
    q2 = item.get("quote2") or None
    if q2 and not quote_ok(q2, nd):
        q2 = None
    # workforce_cause: the quoted text itself must name the workforce or
    # administrative event (a gate-pattern hit in quote or quote2), so a tag
    # can't rest on a passage that only states the reason for the action.
    if cat == "workforce_cause" and not (patterns.find_hits(item["quote"], cat)
                                         or (q2 and patterns.find_hits(q2, cat))):
        return None
    return {"category": cat, "quote": clean_quote(item["quote"]),
            "quote2": clean_quote(q2) if q2 else None,
            "explanation": (item.get("explanation") or "").strip(),
            "n_firms": item.get("n_firms") if cat == "sources_rejected" else None}


def union_passes(runs: list[list[dict]]) -> list[dict]:
    """Union of verified tags across passes. Two tags are the same when they
    share a category and one normalised quote contains the other; the first
    seen is kept, with the list of passes (1-based) that found it."""
    out: list[dict] = []
    keys: list[tuple[str, str]] = []
    for i, run in enumerate(runs, 1):
        for f in run:
            k = (f["category"], normalize(f["quote"]))
            j = next((j for j, (c, q) in enumerate(keys) if c == k[0] and (k[1] in q or q in k[1])), None)
            if j is None:
                keys.append(k)
                out.append({**f, "passes": [i]})
            elif i not in out[j]["passes"]:
                out[j]["passes"].append(i)
    return out


def tag_document(doc: Document, categories: list[str], *, model: str = llm.DEFAULT_MODEL,
                 passes: int = PASSES, call_fn: CallFn | None = None, first_pass: int = 1) -> dict:
    """Review one document for the given categories. Returns a review dict
    (see output_rows). Raises llm.ModelError if any call fails, so the caller
    stores nothing and the document is retried later.

    first_pass numbers the passes (first_pass .. first_pass+passes-1); a
    caching wrapper can use it to keep repeated runs separate."""
    call_fn = call_fn or (lambda p, **kw: llm.call(p, **{k: v for k, v in kw.items() if k != "pass_no"}))
    for c in categories:
        if c not in CATEGORIES:
            raise ValueError(f"unknown or unshipped category {c!r}")
    nd = doc.normalized()
    pass_nos = list(range(first_pass, first_pass + passes))
    jobs: list[tuple[str, llm.Prompt, dict, str, int]] = []   # (category, prompt, schema, tool, pass)
    candidates: dict[str, int] = {}
    vt = doc.verification_text()
    for c in categories:
        if CATEGORIES[c].method != "confirm":
            continue
        hits = patterns.find_hits(vt, c)
        candidates[c] = len(hits)
        if not hits:
            continue
        pr = prompts.confirm_prompt(c, doc.meta, patterns.excerpts(nd, hits))
        jobs += [(c, pr, prompts.confirm_schema(c), prompts.CONFIRM_TOOL[c], p) for p in pass_nos]

    with ThreadPoolExecutor(max_workers=max(1, min(8, len(jobs)))) as ex:
        results = list(ex.map(lambda j: _call(call_fn, j[1], j[2], j[3], model, j[4]), jobs))

    per_pass: dict[str, dict[int, list[dict]]] = {c: {p: [] for p in pass_nos} for c in categories}
    usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 0, "cost_usd": 0.0, "calls": len(jobs)}
    for (cat, _pr, _s, _t, p), (parsed, u) in zip(jobs, results):
        if not isinstance(parsed, dict):
            raise llm.ModelError(f"model returned {type(parsed).__name__}, not an object")
        usage["input_tokens"] += int(u.get("input_tokens") or 0)
        usage["output_tokens"] += int(u.get("output_tokens") or 0)
        usage["cache_creation_input_tokens"] += int(u.get("cache_creation_input_tokens") or 0)
        usage["cache_read_input_tokens"] += int(u.get("cache_read_input_tokens") or 0)
        usage["cost_usd"] += float(u.get("cost_usd") or 0)
        items = [{**parsed, "category": cat}] if parsed.get("answer") else []
        for it in items:
            v = _verify(cat, it, nd)
            if v:
                per_pass[cat][p].append(v)
    usage["cost_usd"] = round(usage["cost_usd"], 6)

    tags = []
    status = {}
    for c in categories:
        u = union_passes([per_pass[c][p] for p in pass_nos])
        for t in u:
            t["passes"] = [pass_nos[i - 1] for i in t["passes"]]
            m = locate_quote(t["quote"], doc, nd)
            t["quote_match"] = m.match
            t["quote_pages"] = m.pages
            t["quote_on_transcribed_page"] = m.transcribed
            t["quote"] = display_quote(t["quote"])
            t["quote2"] = display_quote(t["quote2"])
            t["method"] = CATEGORIES[c].method
        tags += u
        if CATEGORIES[c].method == "confirm" and not candidates.get(c):
            status[c] = "no_candidate_passages"
        else:
            status[c] = "tagged" if u else "not_tagged"
    return {
        "experimental": True,
        "notice_id": doc.notice_id,
        "model": model,
        "prompt_version": prompts.PROMPT_VERSION,
        "prompt_versions": {c: prompts.prompt_version(c) for c in categories},
        "passes": pass_nos,
        "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "categories": status,
        "pages": len(doc.pages),
        "transcribed_pages": sum(1 for p in doc.pages if p.text_source == "transcription"),
        "tags": tags,
        "usage": usage,
    }


def output_rows(review: dict, doc: Document, dataset_revision: str | None) -> list[dict]:
    """One flat row per tag, each marked experimental."""
    rows = []
    for t in review["tags"]:
        rows.append({
            "experimental": True,
            "note": NOTE,
            "notice_id": doc.notice_id,
            "sam_link": doc.meta.get("sam_link") or f"https://sam.gov/opp/{doc.notice_id}/view",
            "title": doc.meta.get("title"),
            "agency": doc.meta.get("agency"),
            "posted_date": doc.meta.get("posted_date"),
            "category": t["category"],
            "category_kind": CATEGORIES[t["category"]].kind,
            "category_status": STATUS.get(t["category"]),
            "quote": t["quote"],
            "quote2": t["quote2"],
            "explanation": t["explanation"],
            "n_firms": t.get("n_firms"),
            "quote_match": t["quote_match"],
            "quote_pages": t["quote_pages"],
            "quote_on_transcribed_page": t["quote_on_transcribed_page"],
            "found_in_passes": t["passes"],
            "all_passes": len(t["passes"]) == len(review["passes"]),
            "method": t["method"],
            "model": review["model"],
            "prompt_version": prompts.prompt_version(t["category"]),
            "dataset_revision": dataset_revision,
            "run_at": review["run_at"],
        })
    return rows
