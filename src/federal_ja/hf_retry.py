"""Retry a Hugging Face write on rate limits, server errors and dropped connections.

huggingface_hub retries its LFS/xet uploads, but not the final commit request, so a
429 or 5xx there used to fail the run on the first try.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

RETRY_STATUS = {429, 500, 502, 503, 504}


def retryable(exc: Exception) -> bool:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is not None:
        return status in RETRY_STATUS
    # no HTTP answer at all: a connection or timeout error from requests/httpx/the socket
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    mod = type(exc).__module__ or ""
    return mod.split(".")[0] in ("httpx", "httpx2", "httpcore", "requests", "urllib3")


def with_retry(fn, *args, tries: int = 4, base_seconds: float = 10, **kwargs):
    """fn(*args, **kwargs), retried with exponential backoff (10s, 20s, 40s) on
    retryable errors; anything else, or the last failure, is raised."""
    for attempt in range(tries):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - re-raised unless retryable
            if attempt + 1 >= tries or not retryable(exc):
                raise
            wait = base_seconds * 2 ** attempt
            log.warning("Hugging Face call failed (%s: %s); retry %d/%d in %.0fs",
                        type(exc).__name__, exc, attempt + 1, tries - 1, wait)
            time.sleep(wait)
