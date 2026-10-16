"""Polite client for the two keyless sam.gov endpoints the sam.gov website uses.

* GET /api/prod/opps/v3/opportunities/{noticeId}/resources -> attachment list
* GET /api/prod/opps/v3/opportunities/resources/files/{resourceId}/download
  -> 303 redirect to a signed S3 URL

They are public but undocumented, so we rate-limit every sam.gov request
(default 3/sec across threads), retry 429/5xx/connection errors with
exponential backoff (honouring Retry-After), and send a User-Agent naming the
project. The S3 leg of a download is not sam.gov and is not rate-limited.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from typing import Callable

import requests

from . import config

log = logging.getLogger(__name__)

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


class RateLimiter:
    def __init__(self, per_second: float, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.interval = 1.0 / per_second if per_second > 0 else 0.0
        self.clock, self.sleep = clock, sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self.clock()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            self.sleep(delay)


class SamError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class DownloadResult:
    bytes_written: int
    sha256: str
    content_disposition: str


def parse_resources(payload: dict) -> list[dict]:
    """Flatten the HAL response into one dict per attachment.

    Shape: {"_embedded": {"opportunityAttachmentList": [{"attachments": [...]}]}}
    A notice with no attachments returns no ``_embedded`` key.
    """
    out: list[dict] = []
    lists = (payload or {}).get("_embedded", {}).get("opportunityAttachmentList", []) or []
    for lst in lists:
        for a in lst.get("attachments", []) or []:
            rtype = (a.get("type") or "").strip().lower()
            out.append({
                "resource_id": a.get("resourceId") or "",
                "attachment_id": a.get("attachmentId") or "",
                "attachment_order": a.get("attachmentOrder"),
                "resource_type": rtype,
                "filename": a.get("name") or (a.get("description") if rtype == "link" else "") or "",
                "link_url": (a.get("uri") or "") if rtype == "link" else "",
                "mime_type": a.get("mimeType") or "",
                "size": int(a.get("size") or 0),
                "access_level": a.get("accessLevel") or "",
                "access_status": a.get("accessStatus") or "",
                "export_controlled": str(a.get("exportControlled") or ""),
                "file_exists": str(a.get("fileExists") or ""),
                "deleted_flag": str(a.get("deletedFlag") or "0") == "1",
                "deleted_date": a.get("deletedDate") or "",
                "sam_posted_date": a.get("postedDate") or "",
            })
    return out


class SamClient:
    def __init__(self, requests_per_second: float = config.SAM_REQUESTS_PER_SECOND,
                 max_retries: int = 6, backoff_base: float = 2.0,
                 session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": config.USER_AGENT})
        self.limiter = RateLimiter(requests_per_second, sleep=sleep)
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.sleep = sleep
        self.retry_count = 0          # observability: total retries performed
        self.status_counts: dict[int, int] = {}

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), 300.0)
            except ValueError:
                pass
        return min(self.backoff_base ** attempt + random.uniform(0, 1), 300.0)

    def _sam_get(self, url: str, *, allow_redirects: bool = True) -> requests.Response:
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self.limiter.wait()
            try:
                r = self.session.get(url, timeout=(20, 120), allow_redirects=allow_redirects)
            except (requests.ConnectionError, requests.Timeout) as e:
                last = e
                if attempt < self.max_retries:
                    self.retry_count += 1
                    self.sleep(self._backoff(attempt, None))
                    continue
                raise SamError(f"connection error: {e}") from e
            self.status_counts[r.status_code] = self.status_counts.get(r.status_code, 0) + 1
            if r.status_code in RETRY_STATUS and attempt < self.max_retries:
                self.retry_count += 1
                log.info("sam.gov %s -> %s, retrying", url, r.status_code)
                self.sleep(self._backoff(attempt, r.headers.get("Retry-After")))
                continue
            return r
        raise SamError(f"gave up: {last}")

    def list_resources(self, notice_id: str) -> list[dict]:
        r = self._sam_get(config.RESOURCES_URL.format(notice_id=notice_id))
        if r.status_code == 404:
            raise SamError("resources 404", 404)
        if r.status_code != 200:
            raise SamError(f"resources HTTP {r.status_code}", r.status_code)
        return parse_resources(r.json())

    def download(self, resource_id: str, dest: str,
                 max_bytes: int = config.MAX_FILE_BYTES) -> DownloadResult:
        """Download a file attachment to ``dest`` (atomic rename)."""
        url = config.DOWNLOAD_URL.format(resource_id=resource_id)
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            # sam.gov leg (rate-limited, retried inside _sam_get)
            r = self._sam_get(url, allow_redirects=False)
            cd = r.headers.get("Content-Disposition", "")
            if r.status_code == 200:
                return _write_bytes(r.content, dest, max_bytes, cd)
            if r.status_code not in (301, 302, 303, 307, 308):
                raise SamError(f"download HTTP {r.status_code}", r.status_code)
            target = r.headers.get("Location", "")
            # S3 leg (signed URL; a fresh one is minted on each retry)
            try:
                resp = requests.get(target, stream=True, timeout=(20, 300),
                                    headers={"User-Agent": config.USER_AGENT})
                if resp.status_code == 200:
                    return _stream_to_file(resp, dest, max_bytes, cd)
                if resp.status_code not in RETRY_STATUS:
                    raise SamError(f"s3 HTTP {resp.status_code}", resp.status_code)
                last_err = SamError(f"s3 HTTP {resp.status_code}", resp.status_code)
            except (requests.ConnectionError, requests.Timeout,
                    requests.exceptions.ChunkedEncodingError) as e:
                last_err = e
            if attempt < self.max_retries:
                self.retry_count += 1
                self.sleep(self._backoff(attempt, None))
        raise SamError(f"download failed after retries: {last_err}")


class FileTooLarge(SamError):
    pass


def _write_bytes(data: bytes, dest: str, max_bytes: int, cd: str) -> DownloadResult:
    if len(data) > max_bytes:
        raise FileTooLarge(f"too large: >{max_bytes} bytes")
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dest)
    return DownloadResult(len(data), hashlib.sha256(data).hexdigest(), cd)


def _stream_to_file(resp: requests.Response, dest: str, max_bytes: int,
                    content_disposition: str) -> DownloadResult:
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".part"
    h = hashlib.sha256()
    n = 0
    try:
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(1 << 16):
                if not chunk:
                    continue
                n += len(chunk)
                if n > max_bytes:
                    raise FileTooLarge(f"too large: >{max_bytes} bytes")
                h.update(chunk)
                f.write(chunk)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return DownloadResult(n, h.hexdigest(), content_disposition)
