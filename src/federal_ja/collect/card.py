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
  default: true
- config_name: attachments
  data_files: data/attachments.parquet
- config_name: pages
  data_files: data/pages.parquet
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
| `data/attachments.parquet` | one row per attachment listed on a notice ({n_attachments:,} rows), including whole-document text |
| `data/pages.parquet` | one row per PDF page or image attachment ({n_pages:,} rows): `page_number`, `text_layer_chars`, `needs_transcription`, `text`, `text_source` (`native`, `transcription`, `none`), transcription model; join to attachments on `notice_id` + `resource_id` (`sha256` identifies the exact file) |
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
"""


def render_card(store: Store, start_date: str, built_at: str) -> str:
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
        n_tx_failed_pages=len(failed), link_lines=link_lines, n_tx_failed_docs=len({k for k, _ in failed}),
    )
