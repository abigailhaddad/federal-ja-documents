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
- **Per-row detail** is in the proof file `handcheck_proof.csv`, published
  with the answer key and the pilot outputs on Hugging Face under
  `analysis/validation/field_extraction/`. Each row has our value, our quote and
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

## People and six more fields (prompt `2026-10-20.extract3`)

**Status: validated on a sample; not yet run on the dataset.** The published
`analysis/extractions.parquet` is still the `extract2` run described above.
This prompt version adds seven fields to the same single call. The new
instructions sit in the cached part of the prompt, before the per-notice
section.

### Fields

- **`people`**: one entry per person the document names, in any attachment
  (signers, approvers, certifiers, reviewers, digital-signature stamps,
  "Prepared by" lines, points of contact, vendor or other-firm staff, anyone
  else named), plus one entry per signature, certification, approval or
  review block whose name is visibly redacted. Each entry has:
  - `name`, exactly as written, and `name_quote`, a passage containing it;
  - `title` and `organization`, as printed;
  - `role_in_document`, one of `signer_or_approver`, `contracting_officer`
    (including contract specialists and buyers), `competition_advocate`,
    `requiring_or_technical_official`, `legal_reviewer`, `vendor_employee`,
    `other`;
  - `description`, one model-written sentence;
  - `redacted`, true when the name is withheld (`[REDACTED]`, "(b)(6)", the
    word "redacted"); the name is then null.
  - **Not entries:** a blank, unsigned block (a title with an empty name
    line); a redacted point of contact outside a signature block; a name that
    is only part of a place or organisation name, or only inside an email
    address.
  - **Digital-signature stamps** are kept as written. The typed name is used
    when there is one. When the stamp is the only form,
    `"Digitally signed by SMITH.JOHN.A.1234567890"` gives the name
    `SMITH.JOHN.A.1234567890`, number included and not reordered;
    `name_is_signature_stamp` is then true. A visible name whose stamp
    number or phone number is redacted is a named entry, not a redacted one.
  - **Names broken across lines** are joined with one space (or with
    nothing, without the hyphen, when one word was split).
- **`need_by_date`**: a hard deadline the document states the requirement
  must be met by, with its quote. Not a period-of-performance start date
  given only as the start, a signature date or a response due date.
- **`stated_pop_limit_days`**: a cap on this action's period of
  performance stated as a limit ("not to exceed six months" gives 180), with
  its quote.
- **`covered_piids`**: contract, order or modification numbers the
  document says this action awards, modifies, extends or is placed under,
  as written.
- **`incumbent_piid`**: the current or predecessor contract number, with
  its quote.
- **`market_research_methods`**: the methods the document says were used,
  each with a quote. The codes are `sources_sought`, `rfi`,
  `notice_of_intent`, `industry_day`, `sam_search`,
  `gsa_elibrary_or_advantage`, `sba_dsbs`, `other_database_search`,
  `vendor_contact`, `internet_search`, `procurement_history` and `other`.
- **`redaction_significant`** (`yes` / `no` / `unclear`): whether the
  redactions remove substantive content (value, prices, rationale, vendor,
  market-research results) rather than only names, signatures and contact
  details. It is the model's judgement. `yes` needs a verified quote;
  without one the field becomes `unclear`. The existing
  `redaction_detected` is unchanged.

### Checks (`checks_version` `2026-10-20.checks4`)

- **Named people.** A named person is kept only if `name_quote` is found in
  the document and the name is in that quote. The comparison uses letters
  and digits only, so line breaks and a stamp's dots still match. **A name
  that is not in the document can't be kept.**
- **Redacted people.** A redacted entry must have a null name and a quote
  that shows a redaction marker. An entry that gives a name and also says it
  is redacted is dropped.
- **Titles and organisations** that are not in the document are cleared.
  A second entry with the same name is dropped.
- **Need-by date.** The date must parse out of its quote.
- **Period-of-performance cap.** The duration must be in its quote:
  months count as 30, 30.44 or 31 days, and years as 365 or 366, with 3%
  or 2 days of tolerance.
- **Contract numbers.** An incumbent number must be in its quote. A covered
  number must be in the document.
- **Market-research methods.** Each method's quote must verify. Nothing
  checks that the quote shows that particular method.
- **Placeholders.** The model sometimes writes an empty value as a literal
  `""` or `null`. These are treated as empty, which happened on 41 of 78
  `incumbent_piid` answers in the validation runs.
- **No retries.** None of the new fields triggers the retry, so they can't
  change the other fields or double the cost. Every drop is listed in
  `values_dropped_by_checks`.
- **New columns.** `people_named`, `people_signature_block`,
  `people_redacted_blocks`, `people_json`, `need_by_date`, `need_by_quote`,
  `stated_pop_limit_days`, `stated_pop_limit_quote`, `covered_piids`,
  `incumbent_piid`, `incumbent_piid_quote`, `market_research_methods`,
  `redaction_significant`, `redaction_significant_quote`.

### Validation (2026-10-20)

- **Data:** the Hugging Face dataset at commit `8b7d8208fef6` (last modified
  2026-10-20 12:26 UTC).
- **Samples.**
  - **Random:** 24 notices drawn at random, seeded, from the 10,033 in-scope
    notices with usable text. The earlier pilot and hand-check notices were
    excluded.
  - **Edge cases:** 16 notices chosen on purpose:
    - signature blocks redacted with "(b)(6)" or `[REDACTED]`, including
      one mixed with named signers;
    - a redacted `NAME:` field;
    - unsigned blocks with a printed title and a blank name;
    - names and stamps broken across lines;
    - vendor staff named as points of contact;
    - several people sharing a title;
    - digital-signature stamps;
    - a Special Notice.
- **Answer key.** Built blind by separate Claude instances. Each read the
  full text against a written guide, without seeing any model output. Two
  sets of instances did it: one for people, one for the six fields. **No
  person judged the key.** I decided every disagreement against the
  document text.
- **Runs.** Two independent runs (A and B) of the shipped settings:
  `claude-haiku-5-5`, effort high, retries 1.
  - 39 of the 40 notices were extracted in each run.
  - **One failure, in every run:** a notice with a glyph-encoded text layer
    hit the output limit. It failed at 16,000 output tokens in four runs and
    at 32,000 in one more. The `extract2` prompt had extracted it.
- **Files.** The answer key, both runs, the proof file
  `handcheck_proof.csv` and `image_check.csv` are in
  [`analysis/validation/field_extraction_extract3/`](../analysis/validation/field_extraction_extract3/).
  - The proof file has one row per value: our value, our quote and its
    page, the key's value and passage, and the verdict.
  - `score.py` there recomputes the strict scores below.

People, named entries. A prediction counts as correct only if its name
matches one of the forms the key lists, character for character after
collapsing whitespace ("name exact"). Wilson 95% intervals:

| Scope | Set | Precision, run A | Recall, run A | Precision, run B | Recall, run B |
|---|---|---|---|---|---|
| Every named person | random (24) | 41/43 = 95% (85-99%) | 41/44 = 93% (82-98%) | 41/44 = 93% (82-98%) | 41/44 = 93% (82-98%) |
| Every named person | random + edge (39) | 71/73 = 97% (91-99%) | 71/74 = 96% (89-99%) | 71/74 = 96% (89-99%) | 71/74 = 96% (89-99%) |
| Signature-block roles only | random (24) | 30/31 = 97% (84-99%) | 30/32 = 94% (80-98%) | 30/32 = 94% (80-98%) | 30/32 = 94% (80-98%) |
| Signature-block roles only | random + edge (39) | 49/50 = 98% (90-100%) | 49/51 = 96% (87-99%) | 49/52 = 94% (84-98%) | 49/51 = 96% (87-99%) |

- **Signature-block scope** keeps the roles `signer_or_approver`,
  `contracting_officer`, `competition_advocate` and `legal_reviewer`. It is
  applied to the key's roles and to the model's roles separately.
- **Every miss and extra, run A.**
  - "Carlos Sosa Garcia SOSA" against the key's "Carlos Sosa Garcia". The
    page image shows "SOSA" printed on the next line, so this is the right
    person in a different name form.
  - "Wargo, Alicia" was reordered by the model to "Alicia Wargo". The
    check dropped it because that is not how the name is written.
  - "Thomas Dimarcantonio" against the text's "ThomasDimarcantonio". This
    is the only loose-only match.
- **Run B** also wrote one name with its title attached ("Dan Contracting
  Officer").
- **Role accuracy** on matched people: 71/72 in run A and 70/72 in run B.
  The differences are judgement calls, such as a contractor employee who
  signs the technical certification.
- **Redacted blocks.**
  - **Invented names: 0.** In both runs no entry gave a name for a redacted
    block, and no kept name was absent from the document.
  - **Titled blocks.** The key has 49 redacted blocks with a visible title.
    Run A recorded all 49 and run B recorded 47. Run B's two misses are
    page-6 certifications that may repeat page-1 blocks.
  - **Untitled blocks.** Six blocks have no visible title, such as four
    fully blacked-out coordination rows, checked on the page image. Neither
    run recorded those, apart from one in run B.
  - **One key error.** The key had a redacted banner "POC" entry, which the
    guide excludes.
- **Quote check.** Every kept quote was found in the page text: 122/122
  people entries in each run, and every quote of the new fields.
  - A second code path re-checked all 462 kept quotes from both runs
    against the whole-attachment text column instead of the page table.
    All passed.
  - Before the checks, run A had 123 people entries; 1 was dropped (the
    reordered name).
- **Page images.** 10 people entries were checked against images of the
  original PDF pages: 5 redacted blocks and 5 named people, a seeded
  sample. All 10 match. Each redacted name is blacked out on the image, and
  each title is printed as transcribed.

The six fields, strict scoring against the key (run A / run B):

| Field | Precision | Recall | Notes |
|---|---|---|---|
| `need_by_date` | 4/4 / 3/3 | 4/6 / 3/6 | Misses: two deadlines worded as "must be awarded before the current contract expires" and a bridged contract's expiry. Too few positives to rely on. |
| `stated_pop_limit_days` | 2/2 / 2/2 | 2/2 / 2/2 | Only 2 positives in 40 notices. Not measurable. |
| `incumbent_piid` | 12/12 / 13/13 | 12/13 / 13/13 | Run A's miss: a paraphrased quote, dropped by the check. |
| `covered_piids` (per number) | 11/11 / 12/12 | 11/12 / 12/12 | |
| `market_research_methods` (per method) | 51/66 = 77% / 53/63 = 84% | 51/59 = 86% / 53/59 = 90% | |
| `redaction_significant` | accuracy 38/39 / 37/39 | | Run A's one disagreement is a key error: "$xx" is a template placeholder, not a redaction. |

- **What the market-research disagreements are.** Of run A's 15 extra
  methods:
  - 6 are clear model errors: price data or past performance coded as
    market research, or research for the follow-on and not this action.
  - 1 is a key error.
  - The other 8 are boundary cases: a combined "Sources Sought / RFI"
    notice, or SAM.gov coded as `sam_search` against
    `other_database_search`.
- **Stability.** On the ten existing value fields, run A and run B agree on
  361/390 cells. Each new run agrees with the published `extract2` record on
  345-347/380.
  - Most differences are `period_of_performance` wording and the list of
    authorities.
  - As in the prompt-caching A/B, this is within run-to-run variation.

These are samples. With 39 notices, a field with 0 errors could still be
wrong about 9% of the time (the 95% upper bound for 0 of 39).

### Cost

- **Validation.** The runs cost $0.627 at list prices, recomputed from the
  raw token counts. That covers one 7-notice prompt-development run, one
  two-run pass of an earlier draft on the 40 notices, and the two final
  runs.
- **Not costed:** the calls that hit the output limit (five, about $0.05)
  and a first attempt rejected with a 400, which is not billed.
- **Per notice.** About $0.0038-0.0043 on random notices. That is 1.25-1.41
  times the `extract2` cost on the same notices, mostly from the longer
  answer.
- **Full re-extraction (projected, not run):** the 10,033 in-scope notices
  with usable text (9,870 Justifications and 163 Special Notices) would cost
  about **$38-46**, or $3.8-4.6 per 1,000 notices. Failed calls come on top.
- **New notices only:** 277 in-scope notices have no extraction row today
  (114 Justifications and 163 Special Notices), about $1.10-1.30. About 31
  notices a day were posted over the last 30 days, about $0.13 a day.

### Known failure modes (new fields)

- **Untitled redaction blocks.** A redacted block with no visible title,
  such as a fully blacked-out coordination row, is usually not recorded.
- **Black-box redactions in typed PDFs** leave no text, so those people
  are simply absent. This is the same limit as `redaction_detected`.
- **Name forms.**
  - A stray second line can be attached to a name ("Carlos Sosa Garcia
    SOSA").
  - A title can be glued onto a name ("Dan Contracting Officer").
  - A reordered "Last, First" name is dropped by the check, not kept.
- **Garbled text layers.** One glyph-encoded notice made every extract3 call
  hit the output limit, at both 16,000 and 32,000 tokens. Such a notice
  stays in `failures.jsonl`.
- **`market_research_methods`** follows category boundaries that are
  fuzzy. Treat the codes as a screen, not a count.
- **Too rare to validate.** `need_by_date` and `stated_pop_limit_days` have
  2-6 positives in the sample.

## Daily incremental extraction (`.github/workflows/extract-daily.yml`)

**Off until enabled.** The automatic run starts only after a successful
daily collection run, and only when the repository variable
`EXTRACT_DAILY_ENABLED` is `"true"`. GitHub fires `workflow_run` triggers
only for the workflow file on the default branch, so nothing runs before
this file is merged.

A manual run (`workflow_dispatch`) publishes only when "publish" is
ticked.

### What a run does

1. **Seed.** `extract --seed-from-hf` fills a work directory from the
   published `analysis/extractions.parquet`. Each row's `record_json` is the
   full stored record.
2. **Extract.** The run extracts every in-scope notice that has no row
   matching the current model, `extract_version` and effort. The newest
   posted go first. It stops at `--max-usd`, which defaults to $2.
   - A notice whose model call fails is retried on the next run.
3. **Build and publish.** `extract-table --publish` rebuilds the table:
   - one row per notice;
   - the latest record, whatever its prompt version (each row says its
     `extract_version` and `dataset_revision`);
   - the skipped notices.

   It then uploads that one file and nothing else.
   - **Shrink guard.** A table with fewer rows than the published one is
     refused, so a failed seed can't replace it.

### Checked locally, at dataset revision `8b7d8208fef6`

- **Round trip.** Seeding from the published table and rebuilding it
  reproduced all 9,766 published rows exactly, on every one of the
  table's columns. It added the 14 new columns and 3 notices newly skipped
  for having no text.
- **Dry run.** A dry run with prompt `extract3` would extract all 10,033
  in-scope notices with usable text: none has an `extract3` row yet.

### Cost

- **Backfill after enabling.** The first runs re-extract the whole table at
  about $2 a day. That is about 20 days and about $40 in all.
- **After the backfill.** Only new notices are extracted: about 31 a day
  were posted in the 30 days to 2026-10-19, about $0.13 a day. That count
  includes Special Notices, which have no extraction rows today: 163 of
  them have usable text, about $0.70.
- **Every later prompt-version bump** starts another backfill.

## Trial: five more fields in a second call (prompt `2026-10-20.extract4`)

**Status: trial.** The trial is one commit, so dropping it means reverting that commit. Without it, the prompt version is `extract3`. The main call's prompt is byte-for-byte the `extract3` prompt.

### Why a second call

The extract3 strict schema is at the size limit for a compiled grammar. Three layouts that added these fields to the main call were all rejected with a 400 ("The compiled grammar is too large"):

1. a property per field, with enums;
2. the same without enums;
3. a single generic list.

The five fields therefore come from a **second call**. It has its own short prompt and a schema with one list. Each entry has `kind`, `value`, `detail` and `quote`, and the code checks the kinds and codes. **Any future field will need the same treatment.**

### Fields (each quote-checked like the others)

- **`bridge_or_extension`** (yes/no/unclear): "yes" needs a verified quote.
- **`prior_extension_count`**: only when the document states the number ("fourth bridge" gives 3). The quote must contain a count.
- **`contract_types`**: as stated; each must be in its quote.
- **`is_letter_contract`** (yes/no): "yes" needs a quote saying "letter contract", "undefinitized" or "UCA".
- **`funding_sources`**: the named appropriation or fund; the name must be in its quote.
- **`policy_citations`**: each with `cited_for`, one of `reason_for_action`, `regulatory_numbering_or_procedure` or `other`.
- **`urgency_trigger`**: only when urgency is cited. It is one of six codes.

### Trial sample (18 notices)

- **4 known cases:**
  - DCSA/KPMG `1db3f3a6`;
  - ICE Camp East Montana `53d80bf1` and `8febc2f3`;
  - VA CCN `085682f1`.
- **2 bridges that describe themselves as repeats:** a "fourth bridge" and a "third bridge".
- **2 other bridges** and **2 urgency J&As**.
- **8 notices** drawn at random, seeded, from the extract3 random set.

The answer key was built blind by separate Claude instances; no person judged it. Data: dataset `8b7d8208fef6`. Files: [`analysis/validation/trial_fields_extract4/`](../analysis/validation/trial_fields_extract4/).

### Results

Strict scores (run A / run B):

| Field | Accuracy | Precision | Recall | Call |
|---|---|---|---|---|
| `bridge_or_extension` | 17/18 / 18/18 | 11/12 / 10/10 ("yes") | 9/9 / 9/9 | **worth it** |
| `prior_extension_count` | 18/18 / 18/18 | 3/3 / 3/3 | 3/3 / 3/3 | **worth it** (only 3 positives) |
| `is_letter_contract` | 17/18 / 18/18 | 4/4 / 4/4 | 4/4 / 4/4 | **worth it** |
| `contract_types` (per type) | | 14/14 / 13/14 | 14/18 / 13/18 | **worth it** |
| `funding_sources` (per source) | | 6/7 / 6/6 | 6/6 / 6/6 | **worth it** (small n) |
| `urgency_trigger` | 17/18 / 17/18 | 8/9 / 8/9 | 5/5 / 5/5 | **worth it, as a screen** |
| `policy_citations` (per citation) | | 5/5 / 6/6 | 5/12 / 6/12 | **not worth it yet** |

- **`bridge_or_extension`.** Run A's one disagreement is a judgement call: a new letter contract after the incumbent's contract was terminated.
- **`is_letter_contract`.** Run A's one disagreement is a notice where the model left out the letter-contract answer, so the field is null.
- **`contract_types`.** The misses are a "cost reimbursable (CR)" line item and an "Indefinite Quantity Contract (IQC)".
- **`funding_sources`.** The one extra is a fund named for a different action in the same document.
- **`urgency_trigger`.** Each run labels one non-urgency notice, a different notice each time.
- **`policy_citations`.**
  - **What works.** EO 14275 cited only for FAR renumbering was classed `regulatory_numbering_or_procedure` on both ICE notices, in both runs.
  - **Most misses don't matter.** 7 of the 12 key entries are bare "RFO" or "Revolutionary FAR Overhaul" mentions, which the key builder itself thought arguable.
  - **The misses that matter.** The citations the field exists for, the `reason_for_action` ones (the Deferred Resignation Program, a hiring freeze, a reorganisation), were found 1 of 4 times in run A and 2 of 4 in run B.
  - **One `cited_for` error:** HSPD-12 was called a reason.
- **The known cases.** On the four known cases both runs got these right:
  - letter contract: all four;
  - the funding source: OBBBA on both ICE notices, DCSA Working Capital Funds on the DCSA notice;
  - EO 14275 as numbering-only: both ICE notices;
  - the urgency trigger: the House Oversight deadline on DCSA, contractor termination on ICE, agency planning delay on VA CCN;
  - the VA CCN extension.
- **Quote check.** All kept quotes verified against the page text. A second code path against the attachment text matched 105 of 106; the one miss is "non-negotiable" split across a line break as "non-\nnegotiable", which that path does not rejoin.

### Cost

- **The second call** costs about **$0.0019 per notice**, about 45% on top of extract3's roughly $0.0042.
- **A full re-extraction** with it would be about $57-65 instead of $38-46 for the 10,033 notices.
- **Trial spend:** $0.34 (the extract3 baseline run $0.083, run A $0.124, run B $0.131). The 400s were not billed.

These are 18 notices: the positives per field are 3 to 9 (12 for policy citations), so every figure here is a small-sample estimate.
