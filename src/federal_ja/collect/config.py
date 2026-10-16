"""Constants and settings for collection. Everything tunable lives here."""

from __future__ import annotations

import datetime as dt
import os

PROJECT_URL = "https://github.com/abigailhaddad/federal-ja-documents"
USER_AGENT = f"federal-ja-documents/0.1 (+{PROJECT_URL})"

HF_REPO_ID = os.environ.get("JA_HF_REPO", "abigailhaddad/federal-ja-documents")

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
TRANSCRIBE_PAGES_PER_CHUNK = 10
