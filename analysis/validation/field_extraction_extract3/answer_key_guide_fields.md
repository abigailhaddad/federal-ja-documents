# Answer-key guide: six more fields from a J&A document

You are building an answer key by reading ONE document's full text and
recording six facts. Read every page of every attachment. The text file has
`##### ATTACHMENT ... #####` and `----- page N [native|transcription] -----`
markers; they are NOT document text. "[transcription]" pages were transcribed
from a scan and mark redactions as `[REDACTED]`. Typed PDFs often lose a
redacted figure without any marker ("an estimated value of ." with the figure
missing). Never guess, never use outside knowledge. For every value give a
short `passage` (5-40 words) copied from the text that contains it, and its
`page` ("<resource id>#<page number>").

## Fields

1. `need_by_date` (YYYY-MM-DD or null): a hard deadline the document states
   the requirement must be met by: "must be in place by 30 September 2026",
   "required no later than 1 March 2026", "delivery is required by", "the
   current contract expires 31 Jan 2026 and services must continue without a
   lapse" (deadline = that date only when the text ties the need to it).
   NOT: a period-of-performance start date stated only as the start; the
   signature date; proposal or response due dates; the posting date; dates of
   past events; a contract expiration date mentioned without saying the need
   must be met by it. If several deadlines, the earliest one for this action.
   If the document gives a month/year only, null (note it).
2. `stated_pop_limit_days` (integer or null): a cap on THIS action's period of
   performance that the document states as a limit ("not to exceed six
   months", "shall not exceed 12 months", "limited to 180 days", "a maximum of
   one year"). Convert: 1 week = 7, 1 month = 30, 1 year = 365 days (so
   "six months" = 180). NOT the period of performance itself when it is not
   phrased as a cap or limit ("a 12-month base period" is not a cap). Null if
   none.
3. `covered_piids` (list): contract, order, call or modification numbers the
   document says THIS action awards, modifies, extends, bridges or is placed
   under (the parent IDV/schedule contract of an order counts), exactly as
   written. NOT: solicitation numbers, J&A control numbers, purchase request
   numbers, CAGE codes, unrelated past contracts mentioned as history. If none
   is stated, empty list. For each, give a passage.
4. `incumbent_piid` (string or null): the current or predecessor contract
   (or order) number for this requirement, exactly as written: the contract
   being extended or bridged, or the expiring contract this action follows.
   If several, the first one named. Null if none stated.
5. `market_research_methods` (list of codes): methods the document says WERE
   USED for this action (not planned ones), each with a passage. Codes:
   `sources_sought`, `rfi`, `notice_of_intent` (a posted notice of intent to
   sole source / synopsis), `industry_day`, `sam_search` (SAM.gov entity
   search), `gsa_elibrary_or_advantage` (GSA eLibrary, GSA Advantage, eBuy),
   `sba_dsbs` (SBA Dynamic Small Business Search), `other_database_search`
   (FPDS, DIBBS, ECAT, VA vendor or other databases/catalogs),
   `vendor_contact` (contacting firms, asking for quotes or capability
   statements), `internet_search`, `procurement_history` (review of past
   buys or earlier market research), `other`. Each code at most once.
6. `redaction_significant` ("yes" / "no" / "unclear"): do the document's
   redactions remove SUBSTANTIVE content (the dollar value or prices, the
   rationale, the vendor's identity, market-research results, technical
   substance) rather than only names, signatures, phone numbers, emails,
   headers or control numbers? "no" when nothing is redacted or only those
   minor items are. "unclear" when there are redactions but you cannot tell
   what they removed. For "yes" give a passage showing a substantive
   redaction (or the blank where a figure was removed).

## Output

One JSON object per document, written to the file you are told:

```json
{"notice_id": "...",
 "need_by_date": {"value": "2026-09-30", "passage": "...", "page": "...", "notes": null},
 "stated_pop_limit_days": {"value": 180, "passage": "...", "page": "...", "notes": null},
 "covered_piids": [{"value": "W91ZRS-21-C-0001", "passage": "...", "page": "..."}],
 "incumbent_piid": {"value": "W91ZRS-21-C-0001", "passage": "...", "page": "...", "notes": null},
 "market_research_methods": [{"value": "sources_sought", "passage": "...", "page": "..."}],
 "redaction_significant": {"value": "no", "passage": null, "page": null, "notes": "only signature blocks are redacted"},
 "doc_notes": null}
```

Use `{"value": null, "passage": null, "page": null, "notes": ...}` for a
scalar field with no value. Add `"acceptable": [...]` to a scalar field
when another answer would also be defensible (e.g. two deadlines of similar
standing), and say why in `notes`.
