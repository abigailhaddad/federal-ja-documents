# Answer-key guide: five trial fields from a J&A document

You are building an answer key by reading ONE document's full text. Read
every page of every attachment. The `##### ATTACHMENT` and `----- page N`
markers are not document text; "[transcription]" pages were transcribed from
a scan. Never guess or use outside knowledge. For every value give a short
`passage` (5-40 words) copied from the text, and its `page`
("<resource id>#<page number>").

## Fields

1. `bridge_or_extension` ("yes" / "no" / "unclear"): "yes" if THIS action
   is a bridge contract, a short-term extension of an existing contract or
   order (including an extension under FAR 52.217-8 or a sole-source
   extension of services), or a stop-gap until a competitive follow-on.
   "no" for a new requirement, a follow-on that is the long-term
   replacement, a brand-name buy, etc. "unclear" if you can't tell.
   `prior_extension_count` (integer or null): ONLY if the document states
   how many earlier bridges/extensions there were for this requirement
   ("this is the seventh bridge" gives 6; "a second bridge" gives 1; "two
   previous extensions" gives 2). Null if the document does not state a
   number (do not count them yourself from a history table unless the
   document says the number).
2. `contract_types` (list): the contract type(s) of THIS action as stated:
   e.g. "firm-fixed-price", "FFP", "time-and-materials", "labor-hour",
   "cost-plus-fixed-fee", "IDIQ", "BPA call", "task order", "delivery
   order", "purchase order". Copy each as written, with a passage. Empty
   list if none stated. `is_letter_contract` ("yes" / "no"): "yes" if the
   action is a letter contract, an undefinitized contract action (UCA), or
   is otherwise stated to be undefinitized; give the passage.
3. `funding_source_named` (list): a specific appropriation, act or fund the
   document names as the funding source for this action ("One Big Beautiful
   Bill Act", "DCSA Working Capital Funds", "FY26 O&M, Army", "American
   Rescue Plan"), as written, with a passage. Not generic "funds are
   available". Empty list if none.
4. `policy_citations` (list): each executive order, named administration
   initiative, DOGE, or similar policy the document cites, with `citation`
   (as written, e.g. "EO 14275", "Executive Order 14222", "DOGE"),
   `cited_for` and a passage. `cited_for` is one of:
   - `reason_for_action`: the policy is given as a reason for, or a driver
     of, this action or its timing;
   - `regulatory_numbering_or_procedure`: cited only because of FAR
     renumbering / the "Revolutionary FAR Overhaul" (EO 14275 footnotes such
     as "FAR citations reflect the RFO") or another procedural point;
   - `other`: anything else (background, mentioned in passing).
   FAR / DFARS / statute citations are NOT policy citations.
5. `urgency_trigger` (code or null): ONLY when the document cites urgency
   (unusual and compelling urgency, FAR 6.302-2 / RFO 6.103-2, 16.505(b)(2)
   urgency, or an urgency justification), the trigger it states, one of
   `congressional_or_oversight_deadline`, `bid_protest`,
   `contractor_default_or_termination`, `emergency_or_disaster`,
   `agency_planning_delay`, `other`; with a passage. Null when urgency is
   not cited as the authority or basis.

## Output

One JSON object per document, written to the file you are told:

```json
{"notice_id": "...",
 "bridge_or_extension": {"value": "yes", "passage": "...", "page": "...", "notes": null},
 "prior_extension_count": {"value": null, "passage": null, "page": null, "notes": null},
 "contract_types": [{"value": "firm-fixed-price", "passage": "...", "page": "..."}],
 "is_letter_contract": {"value": "no", "passage": null, "page": null, "notes": null},
 "funding_source_named": [{"value": "...", "passage": "...", "page": "..."}],
 "policy_citations": [{"value": "EO 14275", "cited_for": "regulatory_numbering_or_procedure", "passage": "...", "page": "..."}],
 "urgency_trigger": {"value": null, "passage": null, "page": null, "notes": null},
 "doc_notes": null}
```

Add `"acceptable": [...]` to a scalar field when another answer would also
be defensible, and explain in `notes`.
