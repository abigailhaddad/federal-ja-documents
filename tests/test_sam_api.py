import os

import requests

from federal_ja.collect import sam_api
from federal_ja.collect.sam_api import RateLimiter, SamClient, parse_resources

PAYLOAD = {
    "_embedded": {"opportunityAttachmentList": [{"opportunityId": "n1", "attachments": [
        {"attachmentId": "a1", "resourceId": "res1", "attachmentOrder": 1, "fileExists": "1",
         "name": "J&A Redacted.pdf", "type": "file", "postedDate": "2026-10-15T19:03:33Z",
         "accessLevel": "public", "mimeType": ".pdf", "size": 557939, "deletedFlag": "0",
         "accessStatus": "public"},
        {"attachmentId": "a2", "resourceId": "res2", "attachmentOrder": 2, "type": "link",
         "uri": "https://www.example.mil/ja.pdf", "description": "ja.pdf", "size": 0,
         "deletedFlag": "0", "accessLevel": "public"},
        {"attachmentId": "a3", "resourceId": "res3", "type": "file", "name": "old.pdf",
         "size": 10, "deletedFlag": "1", "accessLevel": "public"},
        {"attachmentId": "a4", "resourceId": "res4", "type": "file", "name": "p.pdf",
         "size": 10, "deletedFlag": "0", "accessLevel": "private"},
    ]}]},
}


def test_parse_resources():
    out = parse_resources(PAYLOAD)
    assert [a["resource_id"] for a in out] == ["res1", "res2", "res3", "res4"]
    f, link, deleted, private = out
    assert f["resource_type"] == "file" and f["filename"] == "J&A Redacted.pdf" and f["size"] == 557939
    assert f["link_url"] == "" and f["deleted_flag"] is False
    assert link["resource_type"] == "link" and link["link_url"] == "https://www.example.mil/ja.pdf"
    assert link["filename"] == "ja.pdf"
    assert deleted["deleted_flag"] is True
    assert private["access_level"] == "private"


def test_parse_resources_empty():
    assert parse_resources({"_links": {}}) == []
    assert parse_resources({}) == []


class FakeResp:
    def __init__(self, status, json_data=None, headers=None, body=b""):
        self.status_code = status
        self._json = json_data
        self.headers = headers or {}
        self.content = body
        self._body = body

    def json(self):
        return self._json

    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_retries_on_429_and_connection_error():
    sleeps = []
    s = FakeSession([FakeResp(429, headers={"Retry-After": "7"}),
                     requests.ConnectionError("boom"),
                     FakeResp(503),
                     FakeResp(200, PAYLOAD)])
    c = SamClient(requests_per_second=0, session=s, sleep=sleeps.append)
    out = c.list_resources("n1")
    assert len(out) == 4
    assert c.retry_count == 3
    assert sleeps[0] == 7.0  # honours Retry-After
    assert s.headers["User-Agent"].startswith("federal-ja-documents/")


def test_404_is_error_not_retried():
    s = FakeSession([FakeResp(404)])
    c = SamClient(requests_per_second=0, session=s, sleep=lambda x: None)
    try:
        c.list_resources("n1")
        assert False
    except sam_api.SamError as e:
        assert e.status == 404
    assert len(s.calls) == 1


def test_download_follows_redirect(tmp_path, monkeypatch):
    s = FakeSession([FakeResp(303, headers={"Location": "https://s3/x", "Content-Disposition": "a"})])
    body = b"%PDF-1.4 hello"
    monkeypatch.setattr(sam_api.requests, "get", lambda url, **kw: FakeResp(200, body=body))
    c = SamClient(requests_per_second=0, session=s, sleep=lambda x: None)
    dest = tmp_path / "files" / "n" / "r_x.pdf"
    res = c.download("r", str(dest))
    assert dest.read_bytes() == body
    assert res.bytes_written == len(body)
    import hashlib
    assert res.sha256 == hashlib.sha256(body).hexdigest()
    assert not os.path.exists(str(dest) + ".part")


def test_download_too_large(tmp_path, monkeypatch):
    s = FakeSession([FakeResp(303, headers={"Location": "https://s3/x"})])
    monkeypatch.setattr(sam_api.requests, "get", lambda url, **kw: FakeResp(200, body=b"x" * 100))
    c = SamClient(requests_per_second=0, session=s, sleep=lambda x: None)
    dest = tmp_path / "f.pdf"
    try:
        c.download("r", str(dest), max_bytes=10)
        assert False
    except sam_api.FileTooLarge:
        pass
    assert not dest.exists() and not os.path.exists(str(dest) + ".part")


def test_rate_limiter_spacing():
    t = [0.0]
    slept = []

    def sleep(d):
        slept.append(d)
        t[0] += d
    rl = RateLimiter(4, clock=lambda: t[0], sleep=sleep)
    for _ in range(3):
        rl.wait()
    assert slept == [0.25, 0.25]
