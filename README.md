# federal-ja-documents

Collects federal **Justification & Approval (J&A)** documents posted on
[SAM.gov](https://sam.gov) and publishes them, with extracted text and notice
metadata, as the Hugging Face dataset
[`abigailhaddad/federal-ja-documents`](https://huggingface.co/datasets/abigailhaddad/federal-ja-documents).

A J&A is the document an agency must publish when it awards a contract
without full and open competition. Agencies post them on SAM.gov as
"Justification" notices, usually with a PDF attached, and sometimes as
Special Notices.

## Repository layout

```
src/federal_ja/
  collect/     builds the dataset from SAM.gov and publishes it (this README)
  analysis/    EXPERIMENTAL AI labels (themes, extracted details) and the reader spreadsheet (below)
tests/         tests (collection, and the analysis package on synthetic fixtures)
docs/analysis_validation.md   how the analysis categories were validated
docs/field_extraction.md      how field extraction works and how it was checked
.github/workflows/daily.yml   daily collection job
```

`analysis/` reads only the published dataset (the parquet tables on Hugging
Face), never collection internals, so the two can evolve separately.

## Sources

Everything comes from SAM.gov; no API key is needed.

| What | Where |
|---|---|
| Which J&As (and J&A candidates) exist + metadata | Daily active-notices CSV `ContractOpportunitiesFullCSV.csv` and yearly archive CSVs `FY<YYYY>_archived_opportunities.csv` (refreshed weekly) from `s3.amazonaws.com/falextracts/Contract Opportunities/` |
| Attachment list per notice | `https://sam.gov/api/prod/opps/v3/opportunities/<noticeId>/resources` |
| File download | `https://sam.gov/api/prod/opps/v3/opportunities/resources/files/<resourceId>/download?api_key=null&token=` (redirects to a signed S3 URL) |

The two `sam.gov/api/prod` endpoints are the ones the SAM.gov website itself
uses. They are public but undocumented, so the client is deliberately polite:
at most 3 requests/second to sam.gov (`JA_SAM_RPS`), retries with exponential
backoff on 429/5xx/connection errors (honouring `Retry-After`), and a
User-Agent that names this project.

The CSVs are read as latin-1 with the csv field-size limit raised.

## What gets collected

* **Which rows:** a CSV row is collected for one of two reasons, kept in the
  `collection_reason` column of `notices` (and `exclusions`); `type` stays
  as SAM gave it.
  * `justification_type`: `Type` starts with `Justification` (both
    `Justification` and `Justification and Approval (J&A)` occur).
  * `special_notice_title_match`: `Type` is exactly `Special Notice` and the
    `Title` (title only, not the description) matches, case-insensitively,
    anywhere in the title:

    ```
    justification|j\s*&\s*a\b|sole.source|single.source|limited.source|far\s*6\.302|fair opportunity|only one responsible source|unusual and compelling urgency
    ```

    (`JA_TITLE_PATTERN` in `collect/sources.py`.) Agencies sometimes post a
    real J&A as a Special Notice, e.g. a "Notice of Intent to Sole Source"
    with the justification attached, which the Type rule alone misses. These
    rows are **candidates**: some may be notices of intent with no J&A
    attached, and a few are unrelated (e.g. "Fair Opportunity" industry
    updates). The analysis `is_ja` label says whether a notice's documents
    actually are a J&A. In the active CSV of 2026-10-19 plus the FY2025-FY2027
    archives of 2026-10-18, 9,024 Special Notices posted on or after
    2025-01-20 match, next to 10,594 Justification notices.

  Scan statistics (`collection_state.json` run history) count the two paths
  separately: per file `ja_rows` / `ja_types` (Justification only) and
  `special_notice_rows` / `special_notice_title_matches`; overall
  `rows_by_reason`, `unique_ids_by_reason`, `in_scope_by_reason` and
  `before_start_by_reason`. `ja_rows_total` and `unique_ja_ids` keep their
  Justification-only meaning.
* **Scope:** notices with `PostedDate` on or after **2025-01-20**
  (`--start-date` or `JA_START_DATE`).
* **Dedup:** if a NoticeId appears in several CSVs, the row with the latest
  `PostedDate` is kept (ties: active file, then newest archive). The kept row's
  file is `source_file`; all files are in `source_files`.
* **No silent drops:** a collected row that can't be used (no NoticeId, unparseable
  date) goes to `data/exclusions.parquet` with a reason. A notice whose
  attachment list can't be fetched stays in `notices` with
  `resources_status = "error: ..."`. Every attachment SAM lists gets a row,
  with `download_status` one of `ok`, `link` (off-site URL, recorded, not
  fetched), `deleted`, `not_public`, `too_large`, `error`.
* **Text:** `text_source` is `pdf_text` (pypdf text layer), `docx`
  (python-docx), `xlsx` (openpyxl), `text`, `transcription` (every page of
  the PDF was image-only), `pdf_text+transcription` (native text for typed
  pages, transcription for image-only pages), or `none`.
* **Scanned pages:** the decision is per page. A PDF page whose text layer
  has fewer than 300 characters (after stripping whitespace), or is
  font-encoding garbage (glyph codes like `/0 /i255`), is listed in
  `textless_pages`, and only those pages are transcribed with the Anthropic API (`claude-haiku-5-5`),
  sent as PDF documents with instructions to transcribe verbatim, mark
  `[REDACTED]` and `[illegible]`, and never guess redacted content. This
  matters: many J&As are typed documents with one or more pages rasterized
  (often the redacted or signed pages), and a whole-document rule would lose
  them. PNG/JPEG attachments are transcribed as one page. Tokens and cost are
  recorded per document; a spend cap (`--max-usd`) stops a run.

## Dataset layout (Hugging Face)

```
data/notices.parquet          one row per notice (collection_reason says why), plus usaspending_* columns (vendor, amounts)
data/attachments.parquet      one row per attachment, with whole-document text
data/pages.parquet            one row per PDF page / image attachment: page_number,
                              text_layer_chars, text, text_source (native|transcription|none),
                              transcription model; join on notice_id + resource_id (+ sha256)
data/exclusions.parquet       collected CSV rows not in notices, with reason
data/collection_state.json    source ETags, run history
analysis/                     EXPERIMENTAL, AI-generated tables (see below); extractions refreshed daily, themes a snapshot
files/<notice_id>/<resource_id>_<safe filename>   original files
README.md                     dataset card (generated by collect/card.py)
```

The published `attachments` and `pages` tables also carry copies of the main
notice fields (`agency`, `sub_tier`, `office`, `notice_title`, `posted_date`,
`notice_type`, `award_number`, `sam_link`) right after `notice_id`. They are
added at publish time by `collect/published.py`; `notices` is the source and
the working tables in `<workdir>/data/` do not have them.

## USAspending join (vendor and amount)

At every publish, `collect/usaspending.py` looks up each notice's
`award_number` in USAspending prime-contract transactions and adds
`usaspending_*` columns to the published `notices` table (vendor, UEI, parent
company, awarding agency, obligations summed over all transactions, award
values, first and latest action dates, extent competed, and for IDVs the
orders under them). The source is the public Hugging Face dataset
[`abigailhaddad/usaspending-bulk-awards`](https://huggingface.co/datasets/abigailhaddad/usaspending-bulk-awards),
only its `serve/contracts/<FY>.parquet` files (one per fiscal year, each
transaction once), read remotely with DuckDB (selected columns only, temp
space capped at 2 GB). A full join reads FY2007 onward and takes about ten
minutes.

Matching is on a normalised key (uppercase, letters and digits only):
placeholders (`TBD`, `N/A`, ...) and fields listing several awards are not
matched; a trailing modification number (`_P00003`) is dropped if the full
key is not found; when several awards share a PIID, the one at the notice's
agency is chosen if it is the only one there, otherwise the notice is marked
`ambiguous` and nothing is chosen. The dataset card documents every column,
the match rate, and the caveats (obligations are not the J&A's estimated
value; DoD data reaches USAspending about 90 days late).

```sh
uv run ja-collect --workdir $W publish --dry-run   # builds <workdir>/publish/ without uploading
```

## Running it

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest

W=../federal-ja-documents-data/work
# 1. notice list from local copies of the CSVs (or omit --csv to stream them)
uv run ja-collect --workdir $W scan --csv ContractOpportunitiesFullCSV.csv \
    --csv FY2025_archived_opportunities.csv --csv FY2026_archived_opportunities.csv \
    --csv FY2027_archived_opportunities.csv
# 2. attachment lists, 3. files + native text
uv run ja-collect --workdir $W resources
uv run ja-collect --workdir $W download
# 4. transcription of scanned PDFs (needs ANTHROPIC_API_KEY)
uv run ja-collect --workdir $W transcribe --max-usd 5
uv run ja-collect --workdir $W report
# 5. first publish replaces whatever is in the HF repo (needs HF auth)
uv run ja-collect --workdir $W publish --mode full --delete-stale
```

Every step is resumable: it only works on rows still `pending` (add
`--retry-errors` to retry failures).

For a backfill on a machine with little disk, use `stream` instead of
`download`: it works in batches (default 500 files or 1 GB), commits each
batch's original files to the HF dataset, verifies every file there (size,
and sha256 where the hub reports one), deletes the local copies, and records
progress in `data/upload_manifest.jsonl`. It stops cleanly if free disk drops
below `--min-free-gb` (default 3). `transcribe` then fetches each PDF from
the HF dataset, transcribes it, and deletes it again.

```sh
uv run ja-collect --workdir $W stream
```

### Daily job

`.github/workflows/daily.yml` runs `ja-collect daily`, which:

1. downloads only the table files from Hugging Face (not the stored files);
2. streams today's active CSV, plus any yearly archive whose ETag changed
   since the last run (so archives are picked up once a week, after SAM
   refreshes them);
3. fetches attachment lists for new notices and for notices whose CSV row
   changed (a cheap signal that the notice was edited);
4. downloads new files, extracts text, transcribes new scanned PDFs within the
   day's spend cap;
5. commits the new files and updated tables to Hugging Face.

The schedule is gated by the repository variable `DAILY_ENABLED` (off unless
set to `true`). Secrets: `HF_TOKEN`, `ANTHROPIC_API_KEY`.

## AI-generated fields (experimental)

Claude Haiku 5.5 filled in the fields below for each J&A; they are published
under `analysis/` on Hugging Face. They are not 100% right: check anything you
rely on against the document.

**Extracted from each J&A** (`analysis/extractions.parquet`, one row per notice with a document):

| Field | What it is |
|---|---|
| `is_ja`, `doc_kind` | whether the document is actually a J&A, and what kind |
| `authority_cited`, `authority_plain` | the legal authority cited for not competing, and a plain-language category |
| `summary_model_written` | why the agency is not competing it (the model's words) |
| `estimated_value_usd`, `value_basis`, `value_redacted` | the value stated in the J&A, what it covers, and whether it is redacted |
| `vendor`, `additional_vendors`, `covers_multiple_actions` | the proposed contractor(s), and whether the J&A covers more than one action |
| `period_of_performance`, `signature_date` | how long the work runs; the latest signature date |
| `market_research_summary_model_written`, `other_firms_named` | the market research described, and other firms named |
| `market_research_methods` | the market-research methods the document says were used (codes such as `sources_sought`, `rfi`, `sam_search`, `vendor_contact`) |
| `redaction_detected`, `redaction_evidence` | whether redactions were detected, and what showed it ("not detected" is not "no") |
| `redaction_significant` | `yes` / `no` / `unclear`: the model's judgement whether the redactions remove substance (value, prices, rationale, vendor, market-research results) rather than only names and contact details |
| `people_named`, `people_signature_block`, `people_redacted_blocks`, `people_json` | every person the document names (signers, approvers, reviewers, points of contact, vendor staff, anyone else); the subset in signature, approval and review blocks; the number of those blocks whose name is redacted; and the full list, each with name, title, organization, role, a one-sentence description (model-written), a redacted flag and the quote |
| `need_by_date` | a hard deadline the document says the requirement must be met by |
| `stated_pop_limit_days` | a stated cap on the period of performance, in days ("not to exceed six months" gives 180) |
| `covered_piids`, `incumbent_piid` | contract, order or modification numbers this action awards, modifies, extends or is placed under; the current or predecessor contract |
| `bridge_or_extension`, `prior_extension_count` | whether this bridges or extends an existing contract; how many earlier extensions the document states ("fourth bridge" gives 3) |
| `contract_types`, `is_letter_contract` | the contract type(s) as stated; whether it is a letter contract or undefinitized action |
| `funding_sources` | the named appropriation(s) or fund(s) |
| `urgency_trigger` | only when urgency is cited, what caused it: `congressional_or_oversight_deadline`, `bid_protest`, `contractor_default_or_termination`, `emergency_or_disaster`, `agency_planning_delay` or `other` |

Each single-value field has a `*_quote` column with the verbatim text it came
from (`value_quote_pages` and `signature_quote_pages` give the page). The list
fields (`people`, `market_research_methods`, `contract_types`,
`funding_sources`) keep a quote per item in `people_json` or `record_json`
(the full stored record). Every row also says its `extract_version` (the
prompt), `checks_version`, `model` and `dataset_revision`, and lists in
`values_dropped_by_checks` anything the checks removed. Notices with no
usable text have `status` `skipped` and a `skip_reason`.

**Known weak spots** (validation numbers in [docs/field_extraction.md](https://github.com/abigailhaddad/federal-ja-documents/blob/main/docs/field_extraction.md)):

* `contract_types` misses about a third of the types a document states (11
  of 18 found in the validation sample).
* `market_research_methods` follows fuzzy category boundaries: treat it as a
  screen, not a count.
* `need_by_date` and `stated_pop_limit_days` had only 2-6 cases in the
  validation sample, too few to measure how often they are right.
* People: names are copied from the public documents as printed, so they can
  carry layout errors (a title glued onto a name, a stray extra word) and
  digital-signature stamps such as `SMITH.JOHN.A.1234567890`. A redacted name
  is a null `name` in `people_json` and `[name redacted]` in
  `people_signature_block`. Redaction blocks with no visible title, and
  black-box redactions in typed PDFs, are usually missed.

**Themes** (`analysis/theme_tags.parquet`: one row per tagged passage, with its quote and page; `analysis/theme_status.parquet`: each notice's status per theme):

| Theme | The J&A says... |
|---|---|
| `workforce_cause` | a shutdown, staff losses, DOGE or a reorganisation caused the need or a delay |
| `agency_delay_cause` | the agency's own planning, reviews, approvals or staffing delayed competing it |
| `sources_rejected` | other firms responded but were found not capable |

**How it works.**

* **Extraction:** the model reads each notice's documents in full (including
  the transcriptions of scanned pages) and returns the fields above, each with
  the verbatim sentence it came from. Code then checks that every quote is in
  the document and that the value appears in its quote; anything that fails is
  removed and listed in `values_dropped_by_checks`. An approval ceiling
  ("not exceeding $15 million") is never reported as the value.
* **Themes:** a [keyword screen](https://github.com/abigailhaddad/federal-ja-documents/blob/main/src/federal_ja/analysis/patterns.py) finds candidate passages
  (e.g. "shutdown", "staff losses", "delayed", "responded to the sources
  sought"). Documents with no hit are not sent to the model for that theme.
  The model sees only the passages around the hits and answers a yes/no
  question with a quote; this runs twice, and a tag is kept only if its quote
  is found in the document.

The model name and every prompt are in [prompts.yaml](https://github.com/abigailhaddad/federal-ja-documents/blob/main/src/federal_ja/analysis/prompts.yaml).

**How the tables are updated.**

* **Extractions** (`analysis/extractions.parquet`) are refreshed after each
  successful daily collection run: notices with no row at the current prompt
  version are extracted, newest posted first, until that run's spend cap,
  and the table is republished. Each row says its `extract_version` and the
  dataset commit it was read from (`dataset_revision`). When the prompt
  changes, older rows are re-extracted the same way, a cap's worth per run.
  The current prompt is `2026-10-21.extract6`; until its backfill finishes
  (expected within a few days of 2026-10-21), rows from the older
  `2026-10-17.extract2` prompt, which have none of the newer fields, sit
  next to `extract6` rows. Filter on `extract_version` to use one version.
* **Themes** (`analysis/theme_tags.parquet`, `analysis/theme_status.parquet`)
  are not refreshed: they are a one-off snapshot built 2026-10-18 from
  dataset revision `11a221ab5ae9`, and notices collected after it have no
  theme status.

The daily refresh is `.github/workflows/extract-daily.yml`, gated by the
repository variable `EXTRACT_DAILY_ENABLED`. It runs (needs
`ANTHROPIC_API_KEY`; `--publish` also needs `HF_TOKEN`):

```sh
uv run python -m federal_ja.analysis extract --seed-from-hf --out work/extract --max-usd 2 --workers 4
uv run python -m federal_ja.analysis extract-table --out work/extract --table work/extractions.parquet --publish
```

`extract-table --publish` uploads only `analysis/extractions.parquet`, and
refuses a table with fewer rows than the published one. The theme tables are
rebuilt only by hand (`uv run python -m federal_ja.analysis tag --out
work/tags --workers 8 --max-usd 20`, then an upload).
`publish --delete-stale` never deletes anything under `analysis/`. The card's
AI section is static text in `collect/card.py`; update it with any prompt
change or theme re-upload.

## Spreadsheet for readers (`federal_ja.analysis spreadsheet`)

```sh
uv run python -m federal_ja.analysis spreadsheet --out federal_JA_documents.xlsx
```

Builds an Excel workbook from the published dataset alone (about 20 seconds; no
API key, no local files). It reads the `analysis/` tables and the `data/` tables
at the dataset commit the AI tables were built from, so every row joins.

- **Rows:** notices with a downloaded document that the extraction read as an
  actual J&A, newest posting first. Everything left out is counted, with
  reasons, on the About sheet.
- **Sheets:** About (method, accuracy, caveats, as-of dates), J&As (SAM.gov,
  USAspending and AI columns; AI columns are marked), Theme tags (every quote
  with its page).
- Document links point at that dataset commit, so they keep working.
- **Mixed dataset commits.** Once the daily extraction has published, the AI
  rows record several `dataset_revision`s (each run reads the dataset as of
  that day; the theme snapshot is from `11a221ab5ae9`). The workbook then
  reads the notice, attachment and page tables at the same commit as the AI
  tables (`--revision`, default `main`); those tables only grow, so every
  notice an AI row was built from is there.

## License

- **Code:** [Apache-2.0](LICENSE). See [NOTICE](NOTICE) for attribution.
- **Dataset on Hugging Face:** [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/). The J&A documents and USAspending figures are U.S. government records and generally not copyrighted (17 U.S.C. 105). Material a contractor supplied may be an exception.
