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
- `extractions.csv`: one flat row per notice, all columns as text, with the
  SAM.gov link. Columns ending in `_model_written` are the model's own words,
  not quotes.
- `failures.jsonl`: notices whose model call failed (retried on the next
  run). `skipped.jsonl`: notices in scope with no usable text.

## Model choice

<!-- RESULTS -->
