"""Render the Hugging Face dataset card (README.md) from the current tables."""

from __future__ import annotations

from . import config
from .report import summarize
from .sources import JA_TITLE_PATTERN
from .store import Store

CARD_TEMPLATE = """---
license: cc0-1.0
pretty_name: Federal Justification & Approval (J&A) Documents
language:
- en
tags:
- government
- procurement
- federal-contracting
- sam-gov
configs:
- config_name: notices
  data_files: data/notices.parquet
  default: true
- config_name: attachments
  data_files: data/attachments.parquet
- config_name: pages
  data_files: data/pages.parquet
- config_name: experimental_extractions
  data_files: analysis/extractions.parquet
- config_name: experimental_theme_tags
  data_files: analysis/theme_tags.parquet
- config_name: experimental_theme_status
  data_files: analysis/theme_status.parquet
---

# Federal Justification & Approval (J&A) Documents

Justification & Approval documents (and similar justifications for other than
full and open competition) that federal agencies posted to
[SAM.gov](https://sam.gov) Contract Opportunities, with the original files,
extracted text, and notice metadata. Most notices were posted as
"Justification" notices; some are Special Notices whose title suggests a
justification (`collection_reason`, see [Sources](#sources)).

Code and issue tracker: {project_url}

## As of

* Tables last built: **{built_at}** (UTC)
* Notices posted from **{start_date}** through **{posted_max}** (PostedDate)
* Source files used, with the `Last-Modified` time SAM reported:
{source_lines}

## Contents

| Path | What |
|---|---|
| `data/notices.parquet` | one row per notice ({n_notices:,} rows), with `collection_reason` saying why it is here: agency, sub-tier, office, dates, award number, SAM's award block with the order number (`sam_award_*` columns, below), and the matching USAspending award (`usaspending_*` columns, below) |
| `data/attachments.parquet` | one row per attachment listed on a notice ({n_attachments:,} rows), including whole-document text |
| `data/pages.parquet` | one row per PDF page or image attachment ({n_pages:,} rows): `page_number`, `text_layer_chars`, `needs_transcription`, `text`, `text_source` (`native`, `transcription`, `xfa_form`, `none`), transcription model; join to attachments on `notice_id` + `resource_id` (`sha256` identifies the exact file) |
| `data/exclusions.parquet` | rows the collection rules picked from the source CSVs that are not in `notices`, and why ({n_exclusions:,} rows; `collection_reason` as in `notices`) |
| `data/collection_state.json` | source-file ETags and run history |
| `files/<notice_id>/<resource_id>_<filename>` | the original files as downloaded from SAM.gov ({n_files:,} files, {gb:.2f} GB) |
| `analysis/` | **experimental, AI-generated** tables (field extraction, refreshed daily; theme tags, a one-off snapshot); see [Experimental: AI-generated tables](#experimental-ai-generated-tables) |

`attachments` and `pages` repeat the main fields of their notice right after
`notice_id`, so every table shows which J&A a row belongs to: `agency`,
`sub_tier`, `office`, `notice_title` (notices `title`), `posted_date`,
`notice_type` (notices `type`), `award_number`, `sam_link`. They are copies
made when the tables are published; `notices` is the source.

### Coverage at build time

* Notices by `collection_reason`: {by_reason}
* Notices by type: {by_type}
* Attachment-list status: {by_res}
* Attachments by download status: {by_dl}
* Downloaded files by kind: {by_kind}
* Text source of downloaded files: {by_text}
* PDFs with at least one image-only page: {n_scanned:,} of {n_pdfs:,} ({n_pages_scanned:,} such pages)

{sam_award_section}

{usaspending_section}

{experimental_section}

## Sources

1. **Which J&As exist, and their metadata.** SAM.gov publishes bulk CSV
   extracts of Contract Opportunities:
   * the daily file of currently active notices,
     `ContractOpportunitiesFullCSV.csv`, and
   * yearly archives of inactive notices,
     `FY<YYYY>_archived_opportunities.csv`, refreshed weekly.

   A row is collected for one of two reasons, recorded in `collection_reason`:

   * `justification_type`: its `Type` starts with "Justification". Two
     spellings occur: `Justification` and `Justification and Approval (J&A)`.
   * `special_notice_title_match`: its `Type` is exactly `Special Notice` and
     its `Title` (the title only, not the description) matches this regular
     expression, ignoring case, anywhere in the title:

     ```
     {title_pattern}
     ```

     Agencies sometimes post a J&A as a Special Notice, for example a
     "Notice of Intent to Sole Source" with the justification attached. These
     rows are **candidates**: the title only says the notice may carry a
     justification. Many are notices of intent with no J&A attached, and a
     few are unrelated (e.g. "Fair Opportunity" industry updates).
     `analysis/extractions.parquet` keeps a Special Notice only when one of
     its attachments is titled as a justification (a text rule, no model
     call). On 2026-10-26 that kept 2,317 of the 5,423 Special Notices with a
     downloaded document. Rows left out of that table (these, and
     notices whose document the model says is not a J&A) are in
     `analysis/extractions_excluded.parquet`, with the reason.

   `type` is kept as SAM gives it. When a notice appears in more than one
   file, the row with the latest `PostedDate` is kept (ties go to the active
   file, then the newest archive), whichever rule picked it;
   `collection_reason` follows the kept row. `source_file` is the file the
   kept row came from and `source_files` lists every file the notice was
   seen in.
2. **Attachment lists** come from the sam.gov endpoint the SAM.gov website
   uses to list a notice's files,
   `https://sam.gov/api/prod/opps/v3/opportunities/<notice_id>/resources`.
3. **Files** are downloaded from
   `https://sam.gov/api/prod/opps/v3/opportunities/resources/files/<resource_id>/download`,
   which redirects to SAM's file storage. `sha256` and `downloaded_size` are
   computed on the bytes received.

## Documents hosted outside SAM.gov

Some notices attach a LINK instead of a file. Two kinds of link lead
straight to the J&A document and are fetched:

* `www.neco.navy.mil/synopsis_file/...` PDFs (Navy and DLA offices). That
  server does not send its intermediate TLS certificate, so the collector
  adds that one DigiCert intermediate (pinned by SHA-256) to the standard
  certificate bundle; certificate verification stays on.
* `piee.eb.mil` J&A notice pages (`noticeType=JustificationAndApproval`),
  whose attachments are public and downloaded the way the page's own
  "view attachment" button does.

These rows have `resource_type = link`, the original URL in `link_url`, and
the host in `source_host` (`sam.gov` for SAM files). One PIEE page can hold
several documents; extra ones get rows with `resource_id`
`<link resource_id>-<n>` and `parent_resource_id`. Other links (agency home
pages, PIEE vendor guides on acq.osd.mil, training sites) are not fetched.

{link_lines}

## Scope and what is not here

* Only notices with `PostedDate` on or after {start_date}.
* Every attachment SAM lists has a row in `attachments`, but not every one
  has a file. Attachments without a file, by `download_status`:
{not_downloaded_lines}
* {n_res_error:,} notices return HTTP 404 from SAM's attachment-list endpoint
  (and from its notice endpoint), so their attachments are unknown; they are
  listed in `notices` with `resources_status` starting with `error`.
  {n_no_att:,} notices list no attachments at all.
* Each SAM notice is one row. A J&A that was re-posted or amended under a new
  notice ID appears as separate rows; they are not merged.
  {n_multi_groups:,} agency + solicitation-number pairs have more than one
  notice ({n_multi_notices:,} notices).
* A J&A that was posted and removed between two daily snapshots, and never
  reached an archive file, is not in the source CSVs and cannot be here.
* SAM's `Type` is set by the posting office. J&As posted as a Special Notice
  are included only when the title matches the pattern above; J&As posted
  under any other type (for example as an Award Notice or Presolicitation),
  or as a Special Notice with a title that does not match, are not included.
* The CSV metadata is SAM's as of the build date; notices edited later are
  re-checked when their CSV row changes.

## How the text was produced

`attachments.text_source` says where each text came from:

| text_source | method |
|---|---|
| `pdf_text` | the PDF's own text layer, via `pypdf` |
| `pdf_xfa` | an XFA PDF form whose page is only a "Please wait..." placeholder: the text is the form's own XML, its wording with the filled-in values (one page, `pages.text_source` `xfa_form`; never transcribed) |
| `docx` | `python-docx` (paragraphs, then table rows) |
| `xlsx` | `openpyxl`, every sheet, tab-separated |
| `text` | plain-text attachment |
| `transcription` | machine transcription of page images; every page of the PDF was image-only (see below) |
| `pdf_text+transcription` | text-layer pages keep their native text; image-only pages are machine-transcribed |
| `none` | no text: unsupported format, unreadable file, or not yet transcribed |

PDF text has `--- page N ---` markers. The scanned/typed decision is made per
page: a page whose text layer has fewer than 300 characters (whitespace
stripped; `text_layer_chars` in `pages`), or whose text layer is
font-encoding garbage (mostly glyph codes such as `/0 /i255 /3` or control
characters), is listed in `textless_pages`, and only those pages are
transcribed (`transcribed_pages`). For a transcribed
page, `pages.text` is the transcription and `pages.text_layer_text` keeps the
thin native layer. PNG/JPEG attachments are treated as one image-only page.
Many J&As are typed documents in which one or more pages (often the redacted
or signed ones) were flattened to images, so these pages would otherwise have
no text. A PDF whose text layer came from OCR before it was posted counts as
typed, and its OCR text is kept as is, errors included.

### Machine transcriptions: read with care

Only image-only pages are transcribed; pages with a text layer keep their
native text. Transcriptions were made by Anthropic's Claude
(`transcription_model`: `{model}`; prompt version in
`transcription_prompt_version`). The model is told to transcribe verbatim,
mark redacted content `[REDACTED]`, mark unreadable text `[illegible]`, and
never guess redacted content. Pages transcribed: {n_tx_pages:,}.

**Transcriptions are machine-made and may contain errors.** A hand check of
52 pages against the page images (a 25-file pilot plus a spot check of the
full run) found no invented text inside redactions and no black redaction
box left unmarked, but did find these error types:

* adjacent redaction boxes merged into one `[REDACTED]` marker, or
  occasionally an extra marker, so marker counts are approximate;
* a thick blank fill-in line marked as `[REDACTED]`;
* `[signature]` inserted where only a redaction box or an empty cell was;
* digital-signature text collapsed to `[signature]`;
* omitted letterheads, page footers and handwritten notes;
* words cut off at the edge of a cropped image completed by the model;
* white-out redactions (values covered by white boxes) marked `[illegible]`
  instead of `[REDACTED]`;
* occasional silent correction of a misspelling in the source.

Pages are sent one per request. An earlier run that sent up to ten pages per
request sometimes skipped a page and shifted the following pages' text by
one; all such pages were re-transcribed one per request before publication.
Transcription failed for {n_tx_failed_pages:,} image-only pages in
{n_tx_failed_docs:,} files (the model declined or its output was blocked);
those pages have `text_source = none` or their thin native text.

Check the original file in `files/` before relying on any transcribed detail.
Token counts and cost are recorded per document.

## Caveats

* The SAM.gov endpoints used for attachments are public but undocumented and
  may change.
* `size` is SAM's declared size; `downloaded_size` is what was received.
* SAM's CSVs are latin-1 encoded; some characters in titles and descriptions
  may be mis-encoded at the source.
* These are U.S. government records obtained from a public website. Some
  attachments may contain contractor-supplied material.

## License

The dataset is released under [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/):
use it for any purpose, including commercially, without asking.

* The J&A documents and the USAspending figures are U.S. government records,
  which are generally not protected by copyright in the United States
  (17 U.S.C. 105). Material a contractor supplied to the government may be an
  exception; CC0 covers only rights this project holds.
* The transcriptions, metadata tables and match columns are dedicated to the
  public domain under CC0.
* The code that builds the dataset is Apache-2.0: {project_url}
"""


# Static text. analysis/extractions.parquet is refreshed by
# .github/workflows/extract-daily.yml; the theme tables are a one-off
# snapshot, uploaded by hand. Every figure here is copied from
# docs/field_extraction.md and docs/analysis_validation.md; change them only
# together with those documents, a prompt-version bump or a theme re-upload.
# Inserted with str.format as a value, so braces here are literal.
EXPERIMENTAL_SECTION = """## Experimental: AI-generated tables

The tables under `analysis/` were filled in by a language model (Claude Haiku
5.5, `claude-haiku-5-5`). They are not 100% right: check anything you rely on
against the document.

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
  dataset revision `11a221ab5ae9583a95ae645883de75237a46d44d`, and notices collected after it have no theme
  status.

The model name and every prompt are in [prompts.yaml](https://github.com/abigailhaddad/federal-ja-documents/blob/main/src/federal_ja/analysis/prompts.yaml).
"""


def render_card(store: Store, start_date: str, built_at: str,
                us_cols: dict | None = None) -> str:
    s = summarize(store)
    from collections import Counter
    dl = Counter(a.get("download_status") for a in store.attachments.values())
    labels = {"link": "link to another website that is not fetched (URL kept in `link_url`)",
              "not_public": "not public on SAM",
              "deleted": "marked deleted by SAM",
              "deleted_at_download": "SAM answered 'The resource has been deleted' when downloaded",
              "link_error": "supported link whose document could not be fetched (see `download_error`)",
              "too_large": "over the size limit", "error": "download failed",
              "pending": "not yet downloaded", "unsupported_type": "unsupported resource type"}
    not_downloaded_lines = "\n".join(f"  * `{k}`: {v:,} ({labels.get(k, k)})"
                                      for k, v in sorted(dl.items()) if k != "ok")
    groups = Counter((n.get("agency"), n.get("solicitation_number")) for n in store.notices.values()
                     if n.get("solicitation_number"))
    multi = [v for v in groups.values() if v > 1]
    res_status = Counter((n.get("resources_status") or "").split(":")[0] for n in store.notices.values())
    n_tx_pages = sum(1 for v in store.pages.values() for r in v if r["text_source"] == "transcription")
    from .links import link_kind, source_host
    lk = Counter((source_host(a.get("link_url") or ""), a.get("download_status"))
                 for a in store.attachments.values()
                 if a.get("resource_type") == "link" and link_kind(a.get("link_url") or ""))
    link_lines = "Fetch results by host and status:\n\n" + ("\n".join(
        f"* `{h}` `{st}`: {n:,}" for (h, st), n in sorted(lk.items())) or "* none yet")
    failed = [(k, r) for k, v in store.pages.items() for r in v
              if r["needs_transcription"] and r["text_source"] != "transcription"]
    src = store.state.get("sources", {})
    source_lines = "\n".join(
        f"  * `{k}`: {v.get('last_modified', '?')}" for k, v in sorted(src.items())) or "  * (none recorded)"
    return CARD_TEMPLATE.format(
        project_url=config.PROJECT_URL, built_at=built_at, start_date=start_date,
        posted_max=(s["posted_date_max"] or "")[:10], source_lines=source_lines,
        n_notices=s["notices"], n_attachments=s["attachments"],
        n_exclusions=len(store.exclusions),
        n_pages=sum(len(v) for v in store.pages.values()),
        n_files=s["attachments_by_download_status"].get("ok", 0),
        gb=s["downloaded_bytes"] / 1e9, by_type=s["notices_by_type"],
        by_reason=s["notices_by_collection_reason"], title_pattern=JA_TITLE_PATTERN.pattern,
        by_res=s["notices_by_resources_status"], by_dl=s["attachments_by_download_status"],
        by_kind=s["downloaded_by_file_kind"], by_text=s["text_source"],
        n_scanned=s["pdfs_needing_transcription"], n_pdfs=s["pdfs"],
        n_pages_scanned=s["textless_pages"],
        model=config.TRANSCRIBE_MODEL, not_downloaded_lines=not_downloaded_lines,
        n_res_error=res_status.get("error", 0), n_no_att=res_status.get("no_attachments", 0),
        n_multi_groups=len(multi), n_multi_notices=sum(multi), n_tx_pages=n_tx_pages,
        sam_award_section=sam_award_section(store),
        usaspending_section=usaspending_section(store, us_cols),
        experimental_section=EXPERIMENTAL_SECTION,
        n_tx_failed_pages=len(failed), link_lines=link_lines, n_tx_failed_docs=len({k for k, _ in failed}),
    )


SAM_AWARD_TEMPLATE = """## Award and order number from the SAM notice record (`sam_award_*` columns in `notices`)

The CSV extracts give one award number (`award_number`). SAM's notice form
has a second box, the delivery or task **order** number, which the CSVs do
not carry. For a J&A that justifies one order, the award-number box usually
holds the vehicle the order is placed under (a GSA schedule, NASA SEWP, an
agency IDIQ), and only the order number says which order the J&A is for.
Joining J&As to contracts on `award_number` alone attaches such a J&A to
every order under that vehicle, including other agencies' orders.

These columns are read from the notice record the SAM.gov notice page itself
loads, `https://sam.gov/api/prod/opps/v2/opportunities/<notice_id>`
(`data2.award`), one request per notice, copied as the posting office typed
them (surrounding whitespace removed, nothing else changed):

| Column | SAM field |
|---|---|
| `sam_award_status` | `ok` (award block read), `no_award` (record read, no award block; usual for Special Notices), `not_found` (SAM answered 404), `error: ...`, `pending` (new or edited, not yet fetched); empty: not fetched yet |
| `sam_award_checked_at` | when the record was read (UTC) |
| `sam_award_number` | `award.number` (normally the same as the CSV's `award_number`) |
| `sam_award_order_number` | `award.deliveryOrderNumber`: the order the J&A is for, when it is for an order |
| `sam_award_date`, `sam_award_amount` | `award.date`, `award.amount` |
| `sam_awardee_name`, `sam_awardee_uei` | `award.awardee.name`, `award.awardee.ueiSAM` (often empty on J&As) |
| `sam_award_modification_number` | `award.justificationAuthority.modificationNumber` |
| `sam_justification_authority` | `award.justificationAuthority.authority`, SAM's code for the authority selected on the form, as stored (seen: `brand`, `far1`, `far13`, `FAR13`, `FAR5`, `2`, `3`, `5`) |
| `sam_organization_id` | `data2.organizationId`, the posting office's SAM Federal Hierarchy key; `agency`, `sub_tier`, `office` and `aac_code` are the same office's names and code from the CSV |

Caveats:

* `sam_award_order_number` is free text. Seen on 2026-10-23: the award
  number repeated, a solicitation number (e.g. a `Q` in the 9th character),
  and a number with a modification suffix (`..._P00020`). It is not
  validated or normalised here.
* Edits made on SAM after a notice was read are picked up only when the
  notice's CSV row changes (the same signal that re-checks the attachment
  list).
* Notices collected before these columns existed are filled by a one-time
  backfill (`ja-collect sam-awards --backfill`); until it has run, their
  `sam_award_status` is empty, which means unknown, not "no order".

Coverage at build time, notices by `sam_award_status`: {coverage}
"""


def sam_award_section(store: Store) -> str:
    from .sam_award import coverage
    return SAM_AWARD_TEMPLATE.format(coverage=coverage(store))


USASPENDING_TEMPLATE = """## Vendor and amount from USAspending (`usaspending_*` columns in `notices`)

SAM's J&A notices rarely say who got the contract or for how much. Each
notice's `award_number` is looked up in USAspending's prime-contract
transactions, read from the public Hugging Face dataset
[`{hf_dataset}`](https://huggingface.co/datasets/{hf_dataset}) (a Parquet
mirror of USAspending's Award Data Archive; only its `serve/contracts/<FY>.parquet`
files, FY{fy_first}-FY{fy_last}, which hold each transaction once).

**As of:** the USAspending data runs through **{as_of}** (latest
`last_modified_date` in the files read; column `usaspending_as_of`). The join
was rebuilt on {joined_at} (UTC) and is rebuilt on every publish. Per-file
dates are in `data/collection_state.json` under `usaspending`.

**Match rate:** {matched:,} of {n:,} notices ({rate:.1%}) matched one
USAspending award.

| `usaspending_match_method` | notices | meaning |
|---|---|---|
{method_lines}

Notices posted more than 90 days before {as_of}: {old_matched:,} of {old_n:,}
matched ({old_rate:.1%}); posted in the last 90 days: {new_matched:,} of {new_n:,}
({new_rate:.1%}), mostly because of the reporting lag described below.

By agency (agencies with at least 50 notices). Most GSA J&As without a match
are Public Buildings Service real-property leases (`award_number` like
`LGA48151` or `GS-09P-LCA02608`), which are not reported as contracts to
USAspending; legislative-branch agencies (e.g. Library of Congress) do not
report to it either.

| agency | notices | matched |
|---|---|---|
{agency_lines}

**Read the amounts carefully.**

* `usaspending_total_obligated` is the sum of `federal_action_obligation`
  over every transaction of the matched award in FY{fy_first}-FY{fy_last}:
  money **obligated so far**. It is **not** the J&A's estimated value, which
  is often the ceiling over all option years. Obligations can be negative
  (de-obligations). Awards with transactions before FY{fy_first} are undercounted.
* For an IDV (an indefinite-delivery contract, BPA, or GWAC) the IDV's own
  transactions usually obligate nothing; money is obligated on the orders
  under it. `usaspending_idv_orders` and `usaspending_idv_orders_obligated`
  count the orders whose parent is that IDV and sum their obligations, for
  all ordering agencies (a GSA schedule or NASA SEWP contract can have orders
  from many agencies, so this is not the amount of the order the J&A is
  about). Only direct orders count; calls under a BPA that was set up under
  the IDV do not.
* `usaspending_current_total_value` and `usaspending_potential_total_value`
  are USAspending's award-level values (base and exercised options; base and
  all options) from the latest transaction.
* **Reporting lag.** USAspending publishes Department of Defense contract
  data about 90 days after the action, and an award is in USAspending only
  once its first action is reported. Recent J&As, DoD ones especially, may
  not match yet, and many J&As are posted before the award. A missing match
  is not evidence that no contract was signed.
* A match is by award number only. A notice's `award_number` is typed by the
  posting office and can be wrong, can be the IDV when the J&A is for one
  order, or can list several awards.

Columns:

| column | meaning |
|---|---|
| `usaspending_matched` | true if exactly one award was chosen |
| `usaspending_match_method` | how (table above) |
| `usaspending_match_note` | for `ambiguous`: the candidate award keys; for `piid_not_found`: whether the number looks like a solicitation number; for a match at another agency: says so |
| `usaspending_candidates` | number of USAspending awards with this PIID |
| `usaspending_award_key` | USAspending `contract_award_unique_key` |
| `usaspending_permalink` | the award's page on usaspending.gov |
| `usaspending_award_or_idv` | `AWARD` or `IDV` |
| `usaspending_award_type` | e.g. DEFINITIVE CONTRACT, DELIVERY ORDER, IDC, BPA |
| `usaspending_piid` | the matched `award_id_piid` as USAspending writes it |
| `usaspending_parent_award_piid` | for an order: the PIID of the IDV it was placed under |
| `usaspending_recipient_name`, `usaspending_recipient_uei` | the vendor (from the latest transaction) |
| `usaspending_recipient_parent_name` | the vendor's parent company |
| `usaspending_awarding_agency`, `usaspending_awarding_sub_agency`, `usaspending_awarding_office` | USAspending's awarding agency, sub-agency and office |
| `usaspending_same_agency` | USAspending's awarding agency is the notice's agency, by agency code (Army, Navy and Air Force count as Defense) or name; false is common and expected when an agency orders under another agency's contract (GSA schedules, NASA SEWP); empty when USAspending gives no agency |
| `usaspending_total_obligated` | sum of `federal_action_obligation`, US dollars (see above) |
| `usaspending_transactions` | number of transactions summed |
| `usaspending_current_total_value` | award-level base and exercised options value |
| `usaspending_potential_total_value` | award-level base and all options value |
| `usaspending_first_action_date`, `usaspending_latest_action_date` | earliest and latest `action_date` |
| `usaspending_extent_competed` | as reported on the latest transaction |
| `usaspending_other_than_full_and_open` | the exception cited on the latest transaction |
| `usaspending_idv_orders`, `usaspending_idv_orders_obligated` | IDVs only: orders under it and their summed obligations |
| `usaspending_as_of` | latest `last_modified_date` in the USAspending files read |
"""

METHOD_MEANING = {
    "exact": "the award number equals one award's PIID (ignoring case, spaces and punctuation)",
    "exact+agency": "several awards share the PIID; exactly one is at the notice's agency",
    "without_modification_number": "matched after dropping a trailing modification number (e.g. `_P00003`)",
    "without_modification_number+agency": "as above, then the notice's agency picked one of several",
    "ambiguous": "several awards share the PIID and the agency does not pick one; none chosen",
    "piid_not_found": "no USAspending award has this PIID (yet)",
    "no_piid": "`award_number` is empty or a placeholder (TBD, N/A, See J&A, XXXX, too short)",
    "multiple_award_numbers": "`award_number` lists several awards; not matched",
}


def usaspending_section(store: Store, us_cols: dict | None) -> str:
    from collections import Counter

    from . import usaspending

    info = store.state.get("usaspending")
    if us_cols is None or not info:
        return ""
    n = len(us_cols)
    matched = sum(1 for c in us_cols.values() if c["usaspending_matched"])
    methods = Counter(c["usaspending_match_method"] for c in us_cols.values())
    order = list(METHOD_MEANING)
    method_lines = "\n".join(
        f"| `{m}` | {k:,} | {METHOD_MEANING.get(m, '')} |"
        for m, k in sorted(methods.items(), key=lambda x: (order.index(x[0]) if x[0] in order else 99)))
    by_agency = Counter()
    by_agency_m = Counter()
    for nid, c in us_cols.items():
        a = (store.notices.get(nid) or {}).get("agency") or "(none)"
        by_agency[a] += 1
        by_agency_m[a] += bool(c["usaspending_matched"])
    agency_lines = "\n".join(
        f"| {a} | {k:,} | {by_agency_m[a]:,} ({by_agency_m[a] / k:.0%}) |"
        for a, k in by_agency.most_common() if k >= 50)
    import datetime as dt
    cutoff = (dt.date.fromisoformat(info["as_of"][:10]) - dt.timedelta(days=90)).isoformat()
    recent = {nid: (store.notices.get(nid) or {}).get("posted_date", "")[:10] >= cutoff for nid in us_cols}
    old_n = sum(1 for nid in us_cols if not recent[nid])
    old_m = sum(1 for nid, c in us_cols.items() if not recent[nid] and c["usaspending_matched"])
    new_n, new_m = n - old_n, matched - old_m
    years = [y["fiscal_year"] for y in info.get("fiscal_years", [])]
    return USASPENDING_TEMPLATE.format(
        hf_dataset=usaspending.HF_DATASET, fy_first=min(years), fy_last=max(years),
        as_of=info.get("as_of"), joined_at=(info.get("joined_at") or "")[:19],
        matched=matched, n=n, rate=matched / n if n else 0,
        method_lines=method_lines, agency_lines=agency_lines,
        old_n=old_n, old_matched=old_m, old_rate=old_m / old_n if old_n else 0,
        new_n=new_n, new_matched=new_m, new_rate=new_m / new_n if new_n else 0)
