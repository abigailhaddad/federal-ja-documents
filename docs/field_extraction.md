# Experimental field extraction from J&A documents

**Status: experimental.** `python -m federal_ja.analysis extract` reads each
notice's documents with a language model and returns one record per notice:
whether it is a J&A, the authority cited, a summary of the reason, the
estimated value, the proposed vendor, the period of performance, the latest
signature date, market research, redaction, and notes. The code checks every
factual value against the document text before keeping it.

## Method

- **Scope.** Every notice with at least one attachment downloaded
  (`download_status = "ok"`). Notices without a downloaded document are out of
  scope. Notices in scope whose documents have no usable text (fewer than 200
  characters, or only a spreadsheet) are written to `skipped.jsonl` with a
  reason.
- **Text.** The same text the tagger uses: every page of every attachment, in
  attachment order and then page order, native text layer or machine
  transcription of a scanned page; a `.docx` counts as one page; spreadsheets
  are left out. Documents up to 380,000 characters (about 100,000 tokens) are
  sent whole. A longer document is cut at a page boundary; the record says so
  (`truncated`, `chars_sent`, `chars_total`).
- **One call per notice.** The model returns all fields through one strict
  tool call (schema in `extract.py`). Model: `claude-haiku-5-5` with
  `--effort high` (the model may think before answering). See "Model choice".
- **Checks (in code, not by the model).** A value is kept only if
  1. its quote is found in the document text (exactly, or after
     normalising whitespace, quote marks, dashes and ligature glyphs; a
     difference only in letter case is accepted and recorded as
     `case_insensitive`), and
  2. the value is visible in its own quote: the dollar figure parses out of
     the value quote, the vendor name is in the vendor quote, the signature
     date parses out of the signature quote, the authority's section number
     is in the authority quote.

  A value that fails is set to null and listed in `values_dropped_by_checks`
  with the reason. If any value fails, the model is asked once more and the
  attempt with fewer failures is kept. Firms named in market research, and
  additional vendors, are kept only if the name appears in the document.
- **Approval-tier guard.** A figure that equals a standard approval threshold
  (e.g. $900,000, $20 million, $100 million) and is quoted in threshold
  language ("exceeding", "not exceeding", "or less") is never reported as the
  value.
- **Redaction.** `redaction_detected` is "yes" when any of these is found:
  a `[REDACTED]` marker in transcribed text, an attachment filename containing
  "redacted", a redaction the model quoted, or a redacted/blank value.
  **"not detected" does not mean "not redacted":** in a typed PDF a black-box
  redaction usually removes the text without leaving any trace in the text
  layer.
- **Garbled text layers.** Some PDFs' text layers are letter-shifted by three
  code points ("7KH" for "The"), a font-encoding fault. Such records have
  `garbled_text_layer = true`; their fields may be missing or wrong.
- **Re-checking without new calls.** `extract --reverify` re-applies the
  current checks to the stored model answers (`raw` in `extractions.jsonl`).

## Output

- `extractions.jsonl`: one line per notice: the checked `fields`, the
  `dropped` values with reasons, the model's `raw` answer, page provenance for
  each quote (resource id, page number, and whether the page was
  machine-transcribed), cost, and versions (`extract_version`,
  `checks_version`, `dataset_revision`).
- `extractions.csv`: one flat row per notice, with the SAM.gov link. No ID
  column is numeric, so a spreadsheet can't mangle one. Columns ending in `_model_written` are the model's own words,
  not quotes.
- `failures.jsonl`: notices whose model call failed (retried on the next
  run). `skipped.jsonl`: notices in scope with no usable text.

## Model choice

A pilot of 41 documents (18 random, plus scanned-only, long, very long,
multi-attachment, redacted-value, approval-tier and odd-title documents) was
run with `claude-haiku-5-5` and `claude-sonnet-5-5` and the disagreements were
checked against the text. After one prompt revision (redacted values that
vanish from typed PDFs, document kinds by authority, value from the
justification only, several vendors), Haiku with `--effort high` agreed with
Sonnet on value, vendor and signature date on nearly every pilot document and
was right on the one date where the two Haiku settings differed. Sonnet cost
about $0.03-0.05 per document (about $300-470 for the dataset); Haiku with
`--effort high` about $0.003. Haiku with `--effort high` was chosen.

## Full run (2026-10-18)

- **Data:** the Hugging Face dataset at commit `11a221ab5ae9` (last modified
  2026-10-17 22:17 UTC).
- **Scope:** 9,766 notices with at least one downloaded attachment.
  - 9,756 were extracted.
  - 10 were skipped for having no usable text:
    - 3 have only a spreadsheet;
    - 2 have an empty `.docx`;
    - 1 has a `.docx` that holds only dots;
    - 3 are PDF portfolios whose only text is "open this PDF portfolio in
      Acrobat";
    - 1 is an 8-page scanned PDF whose transcription was refused at
      collection.
  - 9,756 + 10 = 9,766, so every notice in scope is accounted for.
- **Model settings:**
  - 9,747 notices: `claude-haiku-5-5` with `--effort high`.
  - 9 notices: no effort setting (a plain forced tool call). Every attempt
    with effort high failed on these, by hitting the output limit or by
    refusal. Only 1 of the 9 is flagged as garbled.
- **Cost:** $31.77 recorded for the full run, plus $2.55 for the pilots.
  Failed calls (refusals, output-limit hits) are not costed; they add an
  estimated $0.20-0.50.
- **Values dropped by the checks:** 294 notices have at least one factual
  value dropped. Each is listed with its reason in
  `values_dropped_by_checks`.
- **Long documents:** 5 notices were longer than the limit and were cut.
  They were redone after the attachment-ordering fix described in
  `model_text`.
- **Garbled text layers:** 71 notices are flagged `garbled_text_layer`.

## Hand check

### Samples

- **Random:** 32 notices drawn at random, seeded, from the extracted
  notices. The pilot documents, which shaped the prompt, were excluded.
- **Edge cases:** 19 notices chosen on purpose:
  - scanned-only documents with a value or a date
  - truncated documents and long documents
  - multi-action documents
  - documents the model called "not a J&A" or "unclear"
  - a garbled text layer
  - a notice re-run without effort
  - a value blanked out of a typed PDF
  - documents with approval-tier language, both where a value was kept and
    where none was
  - a value dropped by the checks

### How the check was done

- **The key was built blind.** Separate Claude instances read each
  document's full text, without seeing our output, and wrote their own
  answer for each field with the source passage and page.
- **The key was not judged by a person.** It came from Claude instances, not
  from human reviewers.
- **Every disagreement was decided against the document text.**
- **Page images.** For three values and six signature dates whose quotes sit
  on machine-transcribed pages, the rendered page image was checked against
  the transcription. All nine matched.
- **Per-row detail** is in the proof file `handcheck_proof.csv`, kept with
  the run outputs and not published. Each row has our value, our quote and
  its page, the checker's value and source text, and the verdict.

### Random sample (32 notices)

In this table, "correct" counts both values that match the document and nulls where the document states nothing.

| Field | Correct | Wrong | Missed (null, but stated) | Judgment call |
|---|---|---|---|---|
| is_ja | 32 | 0 | 0 | 0 |
| authority | 30 | 0 | 1 (quote failed the check) | 1 (blank template) |
| estimated value | 31 (9 values, 22 nulls) | 1 (unit price reported as value) | 0 | 0 |
| value_redacted | 32 | 0 | 0 | 0 |
| vendor | 31 | 0 | 0 | 1 (several approved sources, no single vendor) |
| period of performance | 31 | 0 | 1 (quote failed the check) | 0 |
| signature date | 32 (15 dates, 17 nulls) | 0 | 0 | 0 |
| redaction detected | 32 | 0 | 0 | 0 |

- **Summaries.** All 32 model-written summaries and market-research
  summaries were read against the documents. None contradicted the
  document.
- **Other firms named** is not scored strictly. It differed from the key on
  3 of 32 notices:
  - one extra name that is a data service, not a firm;
  - one partial list;
  - one where the key and the model read the list differently.

### Edge cases (19 notices)

| Field | Correct | Wrong | Missed | Judgment |
|---|---|---|---|---|
| is_ja | 19 | 0 | 0 | 0 |
| authority | 17 | 0 | 1 | 1 |
| estimated value | 17 | 0 | 2 | 0 |
| value_redacted | 18 | 1 | 0 | 0 |
| vendor | 17 | 1 | 1 | 0 |
| period | 17 | 0 | 0 | 2 |
| signature date | 19 | 0 | 0 | 0 |
| redaction detected | 16 | 1 | 0 | 2 |

Most of the edge-case errors come from one notice whose text layer is
letter-shifted; it is flagged `garbled_text_layer`. The other misses and
errors:

- A value dropped because the document itself misprints it ("$41,566,275.00
  million").
- A vendor taken from an attached task order when the J&A names only a brand.
- **Fixed during the check:** one truncated notice put its J&A after a
  400-page manual, so the J&A was cut off. Long notices are now cut with
  J&A-named attachments first. All 5 truncated notices were redone, and that
  notice now matches the key.

These are samples. With 32 random notices, a field with 0 errors could still
be wrong about 10% of the time (the 95% upper bound for 0 of 32).

## Known failure modes

- **Unit or per-period prices.** A unit price or per-period price can be
  reported as the value. `value_basis` usually says so ("annual_or_per_period",
  "other"), but not always.
- **Values from attached documents.** A value or vendor can come from an
  attached order or award notice rather than the justification, though the
  prompt forbids it.
- **Garbled text layers** (71 notices flagged). Fields are often missing.
  Garbled text has also been taken for redaction.
- **Hex-escaped text layers.** At least one text layer is hex-escaped
  ("/X41/X4D...") and is not flagged.
- **Redactions in typed PDFs.** A black-box redaction leaves no trace in
  the text, so "not detected" does not mean "not redacted".
- **Filename-only redaction evidence.** Where the only evidence is a filename
  saying "redacted", `redaction_detected` is "yes" even if nothing visible
  was removed.
- **Values the checks drop.** A correct value is dropped when its quote can't
  be matched. Causes include OCR spacing inside numbers, misprints in the
  document, a date in a format the parser doesn't know, or a paraphrased
  quote. These are nulls with a reason in `values_dropped_by_checks`, not
  wrong values.
- **Summaries and notes are the model's own words.** `summary`,
  `market_research_summary` and `notes` are not quotes. Check them against
  the quote columns.
