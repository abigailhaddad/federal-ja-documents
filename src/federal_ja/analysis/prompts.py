"""Prompt text and response schemas. EXPERIMENTAL.

These strings are FROZEN: they are byte-for-byte the prompts that were
validated (docs/analysis_validation.md). Any edit, even whitespace, changes
the model's behaviour, so it needs a new PROMPT_VERSION and a new validation
round on documents the edit was not written from.

Two methods are used:

* ``full`` (workforce_cause, NOT SHIPPED after round 3): the model reads the
  whole document. The frozen prompt asks about three categories at once
  (workforce_cause plus two others); only workforce_cause output is kept.
  repeat_bridge is not shipped, and agency_delay_cause uses the
  regex-then-confirm method instead, because the whole-document prompt
  tagged too much boilerplate for it. The prompt stays here, unchanged, so
  the validated method can be re-run and re-measured.
* ``confirm`` (the other categories): keyword patterns pick candidate
  passages (patterns.py) and the model answers one yes/no question about
  those excerpts only, with a verbatim quote.

The prompts were written for native PDF text; they say "text extracted from
a PDF; may contain OCR errors". Transcribed pages are passed the same way.
"""

from __future__ import annotations

PROMPT_VERSION = "2026-10-16.frozen1"

METADATA_TOKEN = "<<METADATA>>"
EXCERPTS_TOKEN = "<<EXCERPTS>>"

WORKFORCE_PROMPT = """You are a careful federal-procurement reviewer reading ONE publicly posted sole-source Justification & Approval (J&A) / limited-sources / exception-to-fair-opportunity document (text extracted from a PDF; may contain OCR errors, redactions, and missing pages). Flag ONLY the 3 kinds of statements below, and ONLY when the document itself affirmatively says them. Every flag is a "possible issue" (or, for workforce_cause, simply a tracked fact) for a human to check; never say "illegal", "violation" or "unlawful".

QUOTE RULES (strict)
- "quote" must be an EXACT, contiguous excerpt copied character-for-character from the DOCUMENT, 8-60 words. Copy OCR errors and odd spellings exactly as they appear (e.g. if the text says "Defened Resignation", quote "Defened Resignation"; do not fix it). You may only collapse line breaks to single spaces.
- Never stitch separate passages together and never use "..." inside a quote. If you need a second passage, put it in "quote2" (same rules).
- If you cannot quote a passage that by itself shows the point, do not report it.
- Do not quote the SAM metadata line or "=== ATTACHMENT ===" headers.

WHAT TO FLAG (category values)

1. workforce_cause — tracking only, not a rule violation. The document says that a federal workforce or political/administrative event caused or contributed to the need, the timing, or the delay: DOGE, the Deferred Resignation Program (DRP), reductions in force (RIF), hiring freezes, retirements/resignations/staff losses or shortages, VERA/VSIP, executive orders on workforce or cost-efficiency, administration-directed contract terminations, agency reorganization/consolidation, government shutdown/lapse in appropriations/furlough. Do NOT flag ordinary statements that the agency lacks in-house expertise unless a loss or reduction of staff is described.

STRICT DEFINITION: repeat_bridge means at least TWO stop-gap actions for the same requirement: an earlier bridge/extension/stopgap that the document describes, plus this one (or planned 52.217-8 extensions totalling more than 6 months). One bridge, by itself, is never a repeat bridge.
3. repeat_bridge — rule: FAR 6.301(c)(1)/RFO 6.103(b); FAR 52.217-8 ("the total extension of performance hereunder shall not exceed 6 months"). Flag when the document says this action follows one or more PRIOR bridges, extensions, 52.217-8 extensions, stop-gap/purchase-card coverage, or non-competed follow-on orders for the same requirement (e.g. "third bridge", "previously extended", "a 12-month follow-on was signed"), OR when it plans 52.217-8 extensions totaling more than 6 months. A single first bridge is NOT a repeat bridge. Also NOT a repeat bridge — do not flag these: recurring renewals of software licenses, subscriptions or software maintenance/support (e.g. "the prior year user license and support ... will expire"); repeat purchases of the same spare parts, components or equipment, including maintenance or parts renewals and buys "for the second or subsequent time" from the manufacturer (AMC/AMSC codes); and recurring subscription, data-service or curation/storage renewals (e.g. a museum collection already curated by the same repository). Those are routine recurring buys of the same item, not stop-gap extensions of a requirement whose follow-on is late.

EXAMPLES for repeat_bridge
- NOT a repeat bridge: "This is a bridge action for 90 days." / "The final option period expires on 30 June; this bridge contract will extend services for 12 months." / "The existing contract needs to be extended to allow award of the recompete." / "We intend to award a sole-source bridge task order." These describe ONE bridge or extension, the current action itself.
- NOT a repeat bridge: a renewal of a license, subscription, cloud usage, spare parts, or maintenance from the same vendor.
- IS a repeat bridge: "The contract was extended for 6 months under FAR 52.217-8 in March; this action is a further 12-month bridge." / "This is the third bridge modification." / "Services have been maintained on the purchase card since the contract expired; this J&A covers a one-year sole source." / "A 12-month follow-on order was signed in 2025; this action extends it again." / "Two 6-month 52.217-8 extensions are planned (12 months total)."
- The test: does the document describe an EARLIER bridge, extension or stopgap for the same requirement BEFORE this action (or 52.217-8 extensions totalling more than 6 months)? If you can't quote that earlier action, do not flag.

STRICT DEFINITION: agency_delay_cause means the document itself attributes a delay in the competitive follow-on to the agency (its planning, staff, requirements, reviews, approvals or funding). Protests, vendors, disasters and hypothetical future delays never count.
2. agency_delay_cause — rule: FAR 6.301(c)(1) / RFO 6.103(b)(1) ("shall not be justified on the basis of a lack of [advance] planning by the requiring activity") and, for urgency actions, FAR 6.302-2 / RFO 6.103-2. Flag when the document's OWN narrative says the sole-source/limited-source action (or its urgency) arose because the agency itself did not complete, start, or finish planning or the competitive follow-on in time — e.g. "delays in the execution of the follow-on", "requirements package was not completed", "failed to award", "the contract expired" without a replacement, late requirement definition, internal staffing/reorganization delays. Do NOT flag: delays caused solely by bid protests, court orders, natural disasters, or vendor events; a bare disclaimer ("this is not due to lack of planning") with no admitted delay. If the document disclaims planning failure but elsewhere admits an agency delay, flag the admission. Note: GSAR 570.405 lease extensions explicitly allow "delays in planning"; for leases, say so in the explanation.

EXAMPLES for agency_delay_cause
- NOT agency delay: delays caused by a bid protest, GAO, Court of Federal Claims or corrective action; "using the incumbent avoids the delays of onboarding a new vendor"; "a new competition would take 12-18 months" (hypothetical); dates of an RFI or market research; the vendor's own delays.
- IS agency delay: "the follow-on award was delayed because the requirements package was not completed"; "the contracting office could not finalize the evaluation in time"; "the competitive procurement was not awarded as planned due to staffing shortages in the contracting office"; "the contract expired before a replacement was awarded".
- The test: does the document say the AGENCY's own planning, staffing, requirements, reviews, approvals or funding delayed the competitive action, so that this sole source was needed?

EVIDENCE RULE (strict)
- Every repeat_bridge and agency_delay_cause flag MUST include "evidence_quote": an exact excerpt (same rules as "quote") that by itself shows the key fact. For repeat_bridge: the passage naming the EARLIER bridge/extension/stopgap/follow-on for this requirement (or the 52.217-8 periods totalling more than 6 months). For agency_delay_cause: the passage where the document says the agency's own planning, staffing, requirements, reviews, approvals or funding caused the delay.
- If you cannot quote such evidence, do not report the flag. For workforce_cause, set evidence_quote to null.

OTHER RULES
- Redacted or blank values, missing signature pages, and missing pages are NOT issues; never infer anything from what is not in the text.
- explanation: 1-3 neutral sentences that name the rule (or "tracking" for workforce_cause) and say what the document says.
- Return {"none_found": true, "issues": []} if nothing qualifies — this will be the answer for many documents. At most one flag per category per document unless there are clearly distinct instances.

OUTPUT JSON: {"none_found": bool, "issues": [{"category", "quote", "quote2" (or null), "explanation", "evidence_quote" (or null)}]}"""

CONFIRM_TEMPLATES: dict[str, str] = {}

CONFIRM_TEMPLATES["agency_delay_cause"] = """You are a careful federal-procurement reviewer reading ONE publicly posted sole-source Justification & Approval (J&A) / limited-sources / exception-to-fair-opportunity document (text extracted from a PDF; may contain OCR errors, redactions, and missing pages). You are shown EXCERPTS from it (the passages around keyword matches), not the whole document.

QUESTION: Does the document's own narrative say that the agency's own planning, staffing, requirements, reviews, approvals or funding delayed the competitive follow-on, so that this sole source was needed?

DEFINITION
STRICT DEFINITION: agency_delay_cause means the document itself attributes a delay in the competitive follow-on to the agency (its planning, staff, requirements, reviews, approvals or funding). Protests, vendors, disasters and hypothetical future delays never count.
2. agency_delay_cause — rule: FAR 6.301(c)(1) / RFO 6.103(b)(1) ("shall not be justified on the basis of a lack of [advance] planning by the requiring activity") and, for urgency actions, FAR 6.302-2 / RFO 6.103-2. Flag when the document's OWN narrative says the sole-source/limited-source action (or its urgency) arose because the agency itself did not complete, start, or finish planning or the competitive follow-on in time — e.g. "delays in the execution of the follow-on", "requirements package was not completed", "failed to award", "the contract expired" without a replacement, late requirement definition, internal staffing/reorganization delays. Do NOT flag: delays caused solely by bid protests, court orders, natural disasters, or vendor events; a bare disclaimer ("this is not due to lack of planning") with no admitted delay. If the document disclaims planning failure but elsewhere admits an agency delay, flag the admission. Note: GSAR 570.405 lease extensions explicitly allow "delays in planning"; for leases, say so in the explanation.

EXAMPLES for agency_delay_cause
- NOT agency delay: delays caused by a bid protest, GAO, Court of Federal Claims or corrective action; "using the incumbent avoids the delays of onboarding a new vendor"; "a new competition would take 12-18 months" (hypothetical); dates of an RFI or market research; the vendor's own delays.
- IS agency delay: "the follow-on award was delayed because the requirements package was not completed"; "the contracting office could not finalize the evaluation in time"; "the competitive procurement was not awarded as planned due to staffing shortages in the contracting office"; "the contract expired before a replacement was awarded".
- The test: does the document say the AGENCY's own planning, staffing, requirements, reviews, approvals or funding delayed the competitive action, so that this sole source was needed?

QUOTE RULES (strict)
- "quote" must be an EXACT, contiguous excerpt copied character-for-character from the DOCUMENT, 8-60 words. Copy OCR errors and odd spellings exactly as they appear (e.g. if the text says "Defened Resignation", quote "Defened Resignation"; do not fix it). You may only collapse line breaks to single spaces.
- Never stitch separate passages together and never use "..." inside a quote. If you need a second passage, put it in "quote2" (same rules).
- If you cannot quote a passage that by itself shows the point, do not report it.
- Do not quote the SAM metadata line or "=== ATTACHMENT ===" headers.


Answer true ONLY if the excerpts themselves show it; then give the single best verbatim quote (and optionally quote2). Otherwise answer false with quote null. Redactions and missing text are never evidence.
OUTPUT JSON: {"answer": bool, "quote": string|null, "quote2": string|null, "explanation": string}

<<METADATA>>
<<<EXCERPTS START>>>
<<EXCERPTS>>
<<<EXCERPTS END>>>"""

CONFIRM_TEMPLATES["political_direction"] = """You are a careful federal-procurement reviewer reading ONE publicly posted sole-source Justification & Approval (J&A) / limited-sources / exception-to-fair-opportunity document (text extracted from a PDF; may contain OCR errors, redactions, and missing pages). You are shown EXCERPTS from it (the passages around keyword matches), not the whole document.

DEFINITION
political_direction — tracking. Flag when the document attributes the requirement, its timing, or the sole-source decision to direction from political leadership or the administration: a named Executive Order being implemented, DOGE / the Department of Government Efficiency, the White House or the President, the Secretary / Administrator / agency head personally directing it, or an "Administration priority". Do NOT flag: routine statutory or regulatory citations (FAR, U.S.C., agency regulations), generic mission language, or an Executive Order cited only as a general compliance clause unrelated to why this action is needed.
STRICT: The quote must name the political source (the EO, DOGE, the White House/President, or the named official) AND tie it to this requirement or decision.

QUOTE RULES (strict)
- "quote" must be an EXACT, contiguous excerpt copied character-for-character from the DOCUMENT, 8-60 words. Copy OCR errors and odd spellings exactly as they appear (e.g. if the text says "Defened Resignation", quote "Defened Resignation"; do not fix it). You may only collapse line breaks to single spaces.
- Never stitch separate passages together and never use "..." inside a quote. If you need a second passage, put it in "quote2" (same rules).
- If you cannot quote a passage that by itself shows the point, do not report it.
- Do not quote the SAM metadata line or "=== ATTACHMENT ===" headers.


Answer true ONLY if the excerpts themselves show it; then give the single best verbatim quote (and optionally quote2). Otherwise answer false with quote null. n_firms only for sources_rejected, else null.
OUTPUT JSON: {"answer": bool, "quote": string|null, "quote2": string|null, "n_firms": int|null, "explanation": string}

<<METADATA>>
<<<EXCERPTS START>>>
<<EXCERPTS>>
<<<EXCERPTS END>>>"""

CONFIRM_TEMPLATES["sources_rejected"] = """You are a careful federal-procurement reviewer reading ONE publicly posted sole-source Justification & Approval (J&A) / limited-sources / exception-to-fair-opportunity document (text extracted from a PDF; may contain OCR errors, redactions, and missing pages). You are shown EXCERPTS from it (the passages around keyword matches), not the whole document.

DEFINITION
sources_rejected — tracking, a factual pattern (not an issue). Flag when the document says one or more OTHER firms (not the awardee) responded to a sources-sought notice, RFI, synopsis or notice of intent, or otherwise expressed interest, and the agency found them not capable, not acceptable, or not meeting the requirement. Set n_firms to the number of such rejected firms when the document states or lists it, else null. Do NOT flag: no responses were received; only the proposed awardee responded; generic market research that found no other sources.
STRICT: The quote must show that another firm responded or expressed interest AND was found not capable/acceptable.

QUOTE RULES (strict)
- "quote" must be an EXACT, contiguous excerpt copied character-for-character from the DOCUMENT, 8-60 words. Copy OCR errors and odd spellings exactly as they appear (e.g. if the text says "Defened Resignation", quote "Defened Resignation"; do not fix it). You may only collapse line breaks to single spaces.
- Never stitch separate passages together and never use "..." inside a quote. If you need a second passage, put it in "quote2" (same rules).
- If you cannot quote a passage that by itself shows the point, do not report it.
- Do not quote the SAM metadata line or "=== ATTACHMENT ===" headers.


Answer true ONLY if the excerpts themselves show it; then give the single best verbatim quote (and optionally quote2). Otherwise answer false with quote null. n_firms only for sources_rejected, else null.
OUTPUT JSON: {"answer": bool, "quote": string|null, "quote2": string|null, "n_firms": int|null, "explanation": string}

<<METADATA>>
<<<EXCERPTS START>>>
<<EXCERPTS>>
<<<EXCERPTS END>>>"""

CONFIRM_TEMPLATES["expiring_funds"] = """You are a careful federal-procurement reviewer reading ONE publicly posted sole-source Justification & Approval (J&A) / limited-sources / exception-to-fair-opportunity document (text extracted from a PDF; may contain OCR errors, redactions, and missing pages). You are shown EXCERPTS from it (the passages around keyword matches), not the whole document.

DEFINITION
expiring_funds — rule: FAR 6.301(c)(2) / RFO 6.103(b)(2) (other than full and open competition "shall not be justified on the basis of ... concerns related to the amount of funds available (e.g., funds will expire)"). Flag when the document gives expiring funds, end-of-fiscal-year funds, a deadline to obligate funds, or the amount of funds available as a REASON for not competing or for the timing that prevents competition (e.g. "funds expire on September 30 and must be obligated", "to avoid losing FY26 funds"). Do NOT flag: a mention of the fiscal year, the funding source or appropriation, that funds are available or certified, or the standard certification that the request is NOT "a desire to expend funds while those funds are still available".
STRICT: The quote must show funds or fiscal-year timing being used as a reason. If funds are only mentioned, do not flag.

QUOTE RULES (strict)
- "quote" must be an EXACT, contiguous excerpt copied character-for-character from the DOCUMENT, 8-60 words. Copy OCR errors and odd spellings exactly as they appear (e.g. if the text says "Defened Resignation", quote "Defened Resignation"; do not fix it). You may only collapse line breaks to single spaces.
- Never stitch separate passages together and never use "..." inside a quote. If you need a second passage, put it in "quote2" (same rules).
- If you cannot quote a passage that by itself shows the point, do not report it.
- Do not quote the SAM metadata line or "=== ATTACHMENT ===" headers.


Answer true ONLY if the excerpts themselves show it; then give the single best verbatim quote (and optionally quote2). Otherwise answer false with quote null. n_firms only for sources_rejected, else null.
OUTPUT JSON: {"answer": bool, "quote": string|null, "quote2": string|null, "n_firms": int|null, "explanation": string}

<<METADATA>>
<<<EXCERPTS START>>>
<<EXCERPTS>>
<<<EXCERPTS END>>>"""

FULL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "none_found": {"type": "boolean"},
        "issues": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "enum": ["workforce_cause", "repeat_bridge", "agency_delay_cause"]},
                "quote": {"type": "string"},
                "quote2": {"type": ["string", "null"]},
                "explanation": {"type": "string"},
                "evidence_quote": {"type": ["string", "null"]},
            },
            "required": ["category", "quote", "quote2", "explanation", "evidence_quote"],
            "additionalProperties": False}},
    },
    "required": ["none_found", "issues"],
    "additionalProperties": False,
}

CONFIRM_SCHEMA: dict = {
    "type": "object",
    "properties": {"answer": {"type": "boolean"}, "quote": {"type": ["string", "null"]},
                   "quote2": {"type": ["string", "null"]}, "explanation": {"type": "string"}},
    "required": ["answer", "quote", "quote2", "explanation"],
    "additionalProperties": False,
}

CONFIRM_SCHEMA_N_FIRMS: dict = {
    "type": "object",
    "properties": {"answer": {"type": "boolean"}, "quote": {"type": ["string", "null"]},
                   "quote2": {"type": ["string", "null"]}, "n_firms": {"type": ["integer", "null"]},
                   "explanation": {"type": "string"}},
    "required": ["answer", "quote", "quote2", "n_firms", "explanation"],
    "additionalProperties": False,
}

# Tool names the frozen runs used (part of what the model sees).
FULL_TOOL = "ja_review"
CONFIRM_TOOL = {"agency_delay_cause": "ja_confirm", "political_direction": "ja_confirm2",
                "sources_rejected": "ja_confirm2", "expiring_funds": "ja_confirm2"}


def metadata_line(meta: dict) -> str:
    return ("SAM.gov notice metadata (not part of the document; do not quote it): "
            f"title={meta.get('title')!r}; department={meta.get('agency')}; "
            f"posted_date={meta.get('posted_date')}\n")


def full_prompt(meta: dict, model_text: str) -> str:
    return (WORKFORCE_PROMPT + "\n\n" + metadata_line(meta)
            + "\n<<<DOCUMENT START>>>\n" + model_text + "\n<<<DOCUMENT END>>>")


def confirm_prompt(category: str, meta: dict, excerpts: list[str]) -> str:
    return (CONFIRM_TEMPLATES[category]
            .replace(METADATA_TOKEN, metadata_line(meta))
            .replace(EXCERPTS_TOKEN, "\n\n[...]\n\n".join(excerpts)))


def confirm_schema(category: str) -> dict:
    return CONFIRM_SCHEMA if category == "agency_delay_cause" else CONFIRM_SCHEMA_N_FIRMS
