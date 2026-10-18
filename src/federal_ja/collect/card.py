"""Render the Hugging Face dataset card (README.md) from the current tables."""

from __future__ import annotations

from . import config
from .report import summarize
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
extracted text, and notice metadata.

Code and issue tracker: {project_url}

## As of

* Tables last built: **{built_at}** (UTC)
* Notices posted from **{start_date}** through **{posted_max}** (PostedDate)
* Source files used, with the `Last-Modified` time SAM reported:
{source_lines}

## Contents

| Path | What |
|---|---|
| `data/notices.parquet` | one row per J&A notice ({n_notices:,} rows): agency, sub-tier, office, dates, award number, and the matching USAspending award (`usaspending_*` columns, below) |
| `data/attachments.parquet` | one row per attachment listed on a notice ({n_attachments:,} rows), including whole-document text |
| `data/pages.parquet` | one row per PDF page or image attachment ({n_pages:,} rows): `page_number`, `text_layer_chars`, `needs_transcription`, `text`, `text_source` (`native`, `transcription`, `none`), transcription model; join to attachments on `notice_id` + `resource_id` (`sha256` identifies the exact file) |
| `data/exclusions.parquet` | J&A rows found in the source CSVs that are not in `notices` and why ({n_exclusions:,} rows) |
| `data/collection_state.json` | source-file ETags and run history |
| `files/<notice_id>/<resource_id>_<filename>` | the original files as downloaded from SAM.gov ({n_files:,} files, {gb:.2f} GB) |
| `analysis/` | **experimental, AI-generated** tables (field extraction and theme tags), a static snapshot; see [Experimental: AI-generated tables](#experimental-ai-generated-tables) |

`attachments` and `pages` repeat the main fields of their notice right after
`notice_id`, so every table shows which J&A a row belongs to: `agency`,
`sub_tier`, `office`, `notice_title` (notices `title`), `posted_date`,
`notice_type` (notices `type`), `award_number`, `sam_link`. They are copies
made when the tables are published; `notices` is the source.

### Coverage at build time

* Notices by type: {by_type}
* Attachment-list status: {by_res}
* Attachments by download status: {by_dl}
* Downloaded files by kind: {by_kind}
* Text source of downloaded files: {by_text}
* PDFs with at least one image-only page: {n_scanned:,} of {n_pdfs:,} ({n_pages_scanned:,} such pages)

{usaspending_section}

{experimental_section}

## Sources

1. **Which J&As exist, and their metadata.** SAM.gov publishes bulk CSV
   extracts of Contract Opportunities:
   * the daily file of currently active notices,
     `ContractOpportunitiesFullCSV.csv`, and
   * yearly archives of inactive notices,
     `FY<YYYY>_archived_opportunities.csv`, refreshed weekly.

   A row is a J&A if its `Type` starts with "Justification". Two spellings
   occur: `Justification` and `Justification and Approval (J&A)`; the value
   is kept in `type`. When a notice appears in more than one file, the row
   with the latest `PostedDate` is kept (ties go to the active file, then the
   newest archive); `source_file` is the file the kept row came from and
   `source_files` lists every file the notice was seen in.
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
* SAM's `Type` is set by the posting office. J&As posted under another type
  (for example as a Special Notice or Award Notice) are not included.
* The CSV metadata is SAM's as of the build date; notices edited later are
  re-checked when their CSV row changes.

## How the text was produced

`attachments.text_source` says where each text came from:

| text_source | method |
|---|---|
| `pdf_text` | the PDF's own text layer, via `pypdf` |
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


# Static text: the analysis/ tables are a one-off snapshot, uploaded by hand,
# and are NOT rebuilt by the daily job. Every figure here is copied from
# docs/field_extraction.md and docs/analysis_validation.md; change them only
# together with those documents and with a re-upload of analysis/.
# Inserted with str.format as a value, so braces here are literal.
EXPERIMENTAL_SECTION = """## Experimental: AI-generated tables

The tables under `analysis/` were filled in by a language model (Claude Haiku
5.5, `claude-haiku-5-5`). They are not 100% right: check anything you rely on
against the document. Snapshot built 2026-10-18 from dataset revision
`11a221ab5ae9583a95ae645883de75237a46d44d`; not refreshed daily.

**Extracted from each J&A** (`analysis/extractions.parquet`, one row per notice with a document):

| Field | What it is |
|---|---|
| `is_ja`, `doc_kind` | whether the document is actually a J&A, and what kind |
| `authority_cited`, `authority_plain` | the legal authority cited for not competing, and a plain-language category |
| `summary_model_written` | why the agency is not competing it (the model's words) |
| `estimated_value_usd`, `value_basis`, `value_redacted` | the value stated in the J&A, what it covers, and whether it is redacted |
| `vendor`, `additional_vendors` | the proposed contractor(s) |
| `period_of_performance`, `signature_date` | how long the work runs; the latest signature date |
| `market_research_summary_model_written`, `other_firms_named` | the market research described, and other firms named |
| `redaction_detected` | whether redactions were detected |

Each factual field has a `*_quote` column with the verbatim text it came from.

**Themes** (`analysis/theme_tags.parquet`: one row per tagged passage, with its quote and page; `analysis/theme_status.parquet`: each notice's status per theme):

| Theme | The J&A says... |
|---|---|
| `workforce_cause` | a shutdown, staff losses, DOGE or a reorganisation caused the need or a delay |
| `agency_delay_cause` | the agency's own planning, reviews, approvals or staffing delayed competing it |
| `sources_rejected` | other firms responded but were found not capable |

The model name and every prompt are in [prompts.yaml](https://github.com/abigailhaddad/federal-ja-documents/blob/main/src/federal_ja/analysis/prompts.yaml); themes are only asked about passages that pass a [keyword screen](https://github.com/abigailhaddad/federal-ja-documents/blob/main/src/federal_ja/analysis/patterns.py).
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
        by_res=s["notices_by_resources_status"], by_dl=s["attachments_by_download_status"],
        by_kind=s["downloaded_by_file_kind"], by_text=s["text_source"],
        n_scanned=s["pdfs_needing_transcription"], n_pdfs=s["pdfs"],
        n_pages_scanned=s["textless_pages"],
        model=config.TRANSCRIBE_MODEL, not_downloaded_lines=not_downloaded_lines,
        n_res_error=res_status.get("error", 0), n_no_att=res_status.get("no_attachments", 0),
        n_multi_groups=len(multi), n_multi_notices=sum(multi), n_tx_pages=n_tx_pages,
        usaspending_section=usaspending_section(store, us_cols),
        experimental_section=EXPERIMENTAL_SECTION,
        n_tx_failed_pages=len(failed), link_lines=link_lines, n_tx_failed_docs=len({k for k, _ in failed}),
    )


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
