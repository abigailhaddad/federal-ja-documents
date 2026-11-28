"""Guards that stop a bad run from publishing: shrinking tables, a near-empty SAM CSV,
and the retry around Hugging Face commits. Nothing here touches the network."""
import datetime as dt
import io
import json
import os

import pandas as pd
import pytest

from federal_ja import hf_retry
from federal_ja.collect import cli, config, hf_sync
from federal_ja.collect.sources import collect_ja_notices


def _parquet(path, n):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pd.DataFrame({"notice_id": [str(i) for i in range(n)]}).to_parquet(path, index=False)
    return str(path)


def test_publish_refused_when_a_table_lost_rows(tmp_path, monkeypatch):
    monkeypatch.delenv("ALLOW_SHRINK", raising=False)
    (tmp_path / "data").mkdir()
    (tmp_path / hf_sync.PULLED_ROWS).write_text(json.dumps({"data/notices.parquet": 10,
                                                            "data/attachments.parquet": 20}))
    tables = {"data/notices.parquet": _parquet(tmp_path / "pub" / "n.parquet", 9),
              "data/attachments.parquet": _parquet(tmp_path / "pub" / "a.parquet", 25)}
    with pytest.raises(hf_sync.ShrinkError, match="notices"):
        hf_sync.check_not_shrinking(str(tmp_path), tables)
    monkeypatch.setenv("ALLOW_SHRINK", "1")
    hf_sync.check_not_shrinking(str(tmp_path), tables)


def test_publish_allowed_when_tables_grew_or_nothing_was_pulled(tmp_path, monkeypatch):
    monkeypatch.delenv("ALLOW_SHRINK", raising=False)
    tables = {"data/notices.parquet": _parquet(tmp_path / "pub" / "n.parquet", 10),
              "data/attachments.parquet": _parquet(tmp_path / "pub" / "a.parquet", 20)}
    hf_sync.check_not_shrinking(str(tmp_path), tables)      # no pulled_rows.json (first publish)
    (tmp_path / "data").mkdir()
    (tmp_path / hf_sync.PULLED_ROWS).write_text(json.dumps({"data/notices.parquet": 10,
                                                            "data/attachments.parquet": 20}))
    hf_sync.check_not_shrinking(str(tmp_path), tables)      # same counts


def _scan(text):
    return collect_ja_notices([(config.ACTIVE_LABEL, io.StringIO(text))], dt.date(2025, 1, 20))


def test_active_csv_that_is_an_html_page_stops_the_scan(monkeypatch):
    monkeypatch.delenv("ALLOW_SMALL_CSV", raising=False)
    res = _scan("<html><body>Request blocked</body></html>\n")
    with pytest.raises(SystemExit, match="rows parsed"):
        cli.check_active_csv(res.stats)
    monkeypatch.setenv("ALLOW_SMALL_CSV", "1")
    cli.check_active_csv(res.stats)


def test_active_csv_of_normal_size_passes(monkeypatch):
    monkeypatch.delenv("ALLOW_SMALL_CSV", raising=False)
    rows = "NoticeId,Type,Title,PostedDate\n" + "".join(
        f"n{i},Solicitation,t,2026-01-01\n" for i in range(cli.MIN_ACTIVE_ROWS))
    cli.check_active_csv(_scan(rows).stats)


class _Resp:
    def __init__(self, status):
        self.status_code = status


class _HTTPErr(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.response = _Resp(status)


def test_retry_on_503_then_success(monkeypatch):
    monkeypatch.setattr(hf_retry.time, "sleep", lambda s: None)
    calls = []

    def f():
        calls.append(1)
        if len(calls) < 3:
            raise _HTTPErr(503)
        return "ok"
    assert hf_retry.with_retry(f) == "ok" and len(calls) == 3


def test_no_retry_on_401_and_raise_after_last_try(monkeypatch):
    monkeypatch.setattr(hf_retry.time, "sleep", lambda s: None)
    calls = []

    def unauthorized():
        calls.append(1)
        raise _HTTPErr(401)
    with pytest.raises(_HTTPErr):
        hf_retry.with_retry(unauthorized)
    assert len(calls) == 1

    calls.clear()

    def always_429():
        calls.append(1)
        raise _HTTPErr(429)
    with pytest.raises(_HTTPErr):
        hf_retry.with_retry(always_429, tries=4)
    assert len(calls) == 4

    calls.clear()

    def dropped():
        calls.append(1)
        raise ConnectionError("reset")
    with pytest.raises(ConnectionError):
        hf_retry.with_retry(dropped, tries=2)
    assert len(calls) == 2
