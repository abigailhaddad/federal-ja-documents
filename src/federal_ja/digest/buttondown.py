"""Sending through Buttondown's API (checked against its OpenAPI schema,
https://api.buttondown.com/v1/openapi.json, and changelog on 2026-10-21).

* ``POST https://api.buttondown.com/v1/emails`` with
  ``Authorization: Token <key>``; body ``subject``, ``body`` (Markdown is
  detected), ``status``, ``metadata``. Success is ``201`` with the Email.
* ``status``: ``about_to_send`` sends now; ``draft`` only saves a draft. The
  request pins ``X-API-Version: 2026-04-01``, under which a missing status
  means ``draft`` (the older default was to send immediately), so nothing is
  sent by accident; and an explicit ``about_to_send`` needs the
  ``X-Buttondown-Live-Dangerously: true`` header the first time an API key
  sends (``sending_requires_confirmation`` otherwise). It is sent only with
  ``about_to_send``.
* ``GET /v1/emails?creation_date__start=...`` lists recent emails; run.py uses
  it to find a digest that was sent but whose state was never saved.

The key comes from ``BUTTONDOWN_API_KEY`` and is never printed: errors show
the status code and Buttondown's ``code``/``detail`` only.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone

import requests

API = "https://api.buttondown.com/v1"
API_VERSION = "2026-04-01"
METADATA_KIND = "federal-ja-digest"


class SendError(RuntimeError):
    pass


def _headers(send_now: bool = False) -> dict:
    key = os.environ.get("BUTTONDOWN_API_KEY")
    if not key:
        raise SendError("BUTTONDOWN_API_KEY is not set")
    h = {"Authorization": f"Token {key}", "X-API-Version": API_VERSION, "Content-Type": "application/json",
         "User-Agent": "federal-ja-documents digest"}
    if send_now:
        h["X-Buttondown-Live-Dangerously"] = "true"
    return h


def _err(r) -> str:
    try:
        j = r.json()
        detail = j.get("detail") if isinstance(j, dict) else j
        code = j.get("code") if isinstance(j, dict) else None
    except ValueError:
        detail, code = r.text[:300], None
    return f"HTTP {r.status_code}" + (f" {code}" if code else "") + f": {str(detail)[:500]}"


def _request(method: str, url: str, *, attempts: int = 4, **kw):
    last = None
    for i in range(attempts):
        try:
            r = requests.request(method, url, timeout=60, **kw)
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}"
            time.sleep(5 * (i + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = _err(r)
            time.sleep(min(int(r.headers.get("Retry-After") or 0) or 5 * (i + 1), 60))
            continue
        return r
    raise SendError(f"Buttondown {method} {url} failed after {attempts} attempts: {last}")


def send_email(subject: str, body: str, *, metadata: dict, status: str = "about_to_send") -> dict:
    """Create (and, with about_to_send, send) one email. Returns
    {id, status, absolute_url}. Raises SendError on any failure.

    Only a 429 (nothing created) is retried. A 5xx or a network error is not:
    the email may have been created, and a retry could send it twice."""
    if status not in ("about_to_send", "draft"):
        raise SendError(f"unsupported status {status!r}")
    if body.lstrip().startswith("---"):
        raise SendError("body starts with '---' (Buttondown reads that as frontmatter)")
    payload = {"subject": subject, "body": body, "status": status,
               "metadata": {"kind": METADATA_KIND, **metadata}}
    last = None
    for i in range(4):
        try:
            r = requests.post(f"{API}/emails", json=payload, headers=_headers(status == "about_to_send"),
                              timeout=60)
        except requests.RequestException as exc:
            # the request may have reached Buttondown and created the email:
            # never retry blindly. The next run looks for this digest's
            # metadata in Buttondown before sending again (run.py).
            raise SendError(f"Buttondown POST failed ({type(exc).__name__}); check the Buttondown dashboard "
                            "before rerunning") from None
        if r.status_code == 429:
            last = _err(r)
            time.sleep(min(int(r.headers.get("Retry-After") or 0) or 10, 60))
            continue
        if r.status_code != 201:
            raise SendError(f"Buttondown refused the email: {_err(r)}")
        j = r.json()
        return {"id": j.get("id"), "status": j.get("status"), "absolute_url": j.get("absolute_url")}
    raise SendError(f"Buttondown POST failed: {last}")


def recent_digests(days: int = 10) -> list[dict]:
    """This project's digest emails created in the last ``days`` days
    (metadata.kind == METADATA_KIND), any status but draft."""
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{API}/emails"
    params = {"creation_date__start": start, "-status": "draft", "ordering": "-creation_date"}
    out = []
    for _ in range(20):
        r = _request("GET", url, headers=_headers(), params=params)
        if r.status_code != 200:
            raise SendError(f"Buttondown email list failed: {_err(r)}")
        j = r.json()
        for e in j.get("results") or []:
            md = e.get("metadata") or {}
            if md.get("kind") == METADATA_KIND:
                out.append({"id": e.get("id"), "subject": e.get("subject"), "status": e.get("status"),
                            "creation_date": e.get("creation_date"), "metadata": md})
        if not j.get("next"):
            break
        url, params = j["next"], None
    return out
