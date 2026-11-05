"""Constants and settings for collection. Everything tunable lives here."""

from __future__ import annotations

import datetime as dt
import os

# Who owns what. A new owner sets these (as environment variables or by editing the
# defaults) and every link, dataset id and signup form below follows. HANDOFF.md
# lists the places that cannot read this file (workflows, the cron worker, README).
GITHUB_REPO = os.environ.get("JA_GITHUB_REPO", "abigailhaddad/federal-ja-documents")
PROJECT_URL = f"https://github.com/{GITHUB_REPO}"
USER_AGENT = f"federal-ja-documents/0.1 (+{PROJECT_URL})"

HF_REPO_ID = os.environ.get("JA_HF_REPO", "abigailhaddad/federal-ja-documents")
DATASET_URL = f"https://huggingface.co/datasets/{HF_REPO_ID}"

# A separate Hugging Face dataset (a mirror of USAspending's award archive) that
# the collector reads to match notices to awards. It is not part of this dataset.
USASPENDING_HF_DATASET = os.environ.get("JA_USASPENDING_DATASET", "abigailhaddad/usaspending-bulk-awards")

# The digest email's public web archive, its Buttondown account and the
# "report a problem" link.
SITE_URL = os.environ.get("JA_SITE_URL", "https://woah-jas.abigailhaddad.com")
BUTTONDOWN_USER = os.environ.get("JA_BUTTONDOWN_USER", "abigailhaddad")
SUBSCRIBE_URL = f"https://buttondown.com/{BUTTONDOWN_USER}"
SUBSCRIBE_FORM_URL = f"https://buttondown.com/api/emails/embed-subscribe/{BUTTONDOWN_USER}"
ISSUE_URL = f"{PROJECT_URL}/issues/new"

# Earliest PostedDate (inclusive) of J&A notices we collect.
DEFAULT_START_DATE = dt.date(2025, 1, 20)

_S3 = "https://s3.amazonaws.com/falextracts/Contract%20Opportunities"
ACTIVE_CSV_URL = f"{_S3}/datagov/ContractOpportunitiesFullCSV.csv"
ARCHIVE_CSV_URL_TEMPLATE = f"{_S3}/Archived%20Data/FY{{fy}}_archived_opportunities.csv"

ACTIVE_LABEL = "ContractOpportunitiesFullCSV.csv"


def archive_label(fy: int) -> str:
    return f"FY{fy}_archived_opportunities.csv"


def archive_url(fy: int) -> str:
    return ARCHIVE_CSV_URL_TEMPLATE.format(fy=fy)


def fiscal_year(d: dt.date) -> int:
    """US federal fiscal year: FY N runs Oct 1 (N-1) .. Sep 30 N."""
    return d.year + 1 if d.month >= 10 else d.year


def archive_fiscal_years(start: dt.date, today: dt.date | None = None) -> list[int]:
    today = today or dt.date.today()
    return list(range(fiscal_year(start), fiscal_year(today) + 1))


# sam.gov endpoints used by the sam.gov website itself (public, undocumented).
RESOURCES_URL = "https://sam.gov/api/prod/opps/v3/opportunities/{notice_id}/resources"
# The notice record the SAM.gov notice page reads; used for its award block
# (sam_award.py).
NOTICE_URL = "https://sam.gov/api/prod/opps/v2/opportunities/{notice_id}"
DOWNLOAD_URL = (
    "https://sam.gov/api/prod/opps/v3/opportunities/resources/files/{resource_id}"
    "/download?api_key=null&token="
)
SAM_REQUESTS_PER_SECOND = float(os.environ.get("JA_SAM_RPS", "3"))

# Hugging Face rejects single files above 50 GB; we refuse anything far smaller
# so a runaway file can't blow the runner disk.
MAX_FILE_BYTES = int(os.environ.get("JA_MAX_FILE_BYTES", str(1_000_000_000)))

# Transcription
TRANSCRIBE_MODEL = "claude-haiku-5-5"
# USD per million tokens, Anthropic list price for claude-haiku-5-5:
# $0.10 in / $0.50 out for prompts up to 100K tokens, $0.50 / $2.50 beyond.
PRICE_TIER_THRESHOLD = 100_000
PRICE_INPUT_PER_M = 0.10
PRICE_OUTPUT_PER_M = 0.50
PRICE_INPUT_PER_M_LONG = 0.50
PRICE_OUTPUT_PER_M_LONG = 2.50
# One page per request. With multi-page chunks the model sometimes skipped a
# page and shifted the rest up while still emitting the requested page
# markers, so a marker check cannot catch it (found in the full run's spot
# check, reproduced on 2026-10-16). Per-page requests cost about the same.
TRANSCRIBE_PAGES_PER_CHUNK = 1
