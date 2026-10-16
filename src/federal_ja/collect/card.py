"""Render the Hugging Face dataset card (README.md) from the current tables."""

from __future__ import annotations

from . import config
from .report import summarize
from .store import Store

CARD_TEMPLATE = """---
license: other
license_name: us-government-public-records
license_link: {project_url}
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
- config_name: attachments
  data_files: data/attachments.parquet
  default: true
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
| `data/notices.parquet` | one row per J&A notice ({n_notices:,} rows) |
| `data/attachments.parquet` | one row per attachment listed on a notice ({n_attachments:,} rows), including extracted text |
| `data/exclusions.parquet` | J&A rows found in the source CSVs that are not in `notices` and why ({n_exclusions:,} rows) |
| `data/collection_state.json` | source-file ETags and run history |
| `files/<notice_id>/<resource_id>_<filename>` | the original files as downloaded from SAM.gov ({n_files:,} files, {gb:.2f} GB) |

### Coverage at build time

* Notices by type: {by_type}
* Attachment-list status: {by_res}
* Attachments by download status: {by_dl}
* Downloaded files by kind: {by_kind}
* Text source of downloaded files: {by_text}
* PDFs with at least one image-only page: {n_scanned:,} of {n_pdfs:,} ({n_pages_scanned:,} such pages)

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

## Scope and what is not here

* Only notices with `PostedDate` on or after {start_date}.
* Attachments that SAM marks deleted (`download_status = deleted`), not
  public (`not_public`), or that are links to other websites (`link`; the URL
  is kept in `link_url`, the target is not fetched) are listed but have no
  file.
* A notice whose attachment list could not be fetched has a
  `resources_status` starting with `error`; it is listed, with no attachments.
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
page: a page with fewer than 100 letters/digits in its text layer is listed in
`textless_pages`, and only those pages are transcribed (`transcribed_pages`).
Many J&As are typed documents in which one or more pages (often the redacted
or signed ones) were flattened to images, so these pages would otherwise have
no text. A PDF whose text layer came from OCR before it was posted counts as
typed, and its OCR text is kept as is, errors included.

### Machine transcriptions: read with care

Scanned PDFs are transcribed by an AI model (`transcription_model`, currently
`{model}`; prompt version in `transcription_prompt_version`). The model is
told to transcribe verbatim, mark redacted content `[REDACTED]`, mark
unreadable text `[illegible]`, and never guess redacted content.

**Transcriptions are machine-made and may contain errors**: misread numbers,
names and dates, dropped or duplicated lines, and, especially around
redactions, text that is not on the page or redactions that are not marked.
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
"""


def render_card(store: Store, start_date: str, built_at: str) -> str:
    s = summarize(store)
    src = store.state.get("sources", {})
    source_lines = "\n".join(
        f"  * `{k}`: {v.get('last_modified', '?')}" for k, v in sorted(src.items())) or "  * (none recorded)"
    return CARD_TEMPLATE.format(
        project_url=config.PROJECT_URL, built_at=built_at, start_date=start_date,
        posted_max=(s["posted_date_max"] or "")[:10], source_lines=source_lines,
        n_notices=s["notices"], n_attachments=s["attachments"],
        n_exclusions=len(store.exclusions),
        n_files=s["attachments_by_download_status"].get("ok", 0),
        gb=s["downloaded_bytes"] / 1e9, by_type=s["notices_by_type"],
        by_res=s["notices_by_resources_status"], by_dl=s["attachments_by_download_status"],
        by_kind=s["downloaded_by_file_kind"], by_text=s["text_source"],
        n_scanned=s["pdfs_needing_transcription"], n_pdfs=s["pdfs"],
        n_pages_scanned=s["textless_pages"],
        model=config.TRANSCRIBE_MODEL,
    )
