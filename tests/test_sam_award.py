"""SAM award block per notice (sam_award.py). No network: a fake client."""

import datetime as dt
import json

import pandas as pd

from federal_ja.collect import pipeline, sam_award
from federal_ja.collect.sam_api import SamClient, SamError
from federal_ja.collect.sources import CollectResult, CollectStats
from federal_ja.collect.store import NOTICE_COLUMNS, Store

from .test_sam_api import FakeResp, FakeSession

# Shapes copied from real v2 records read 2026-10-23 (values shortened).
TASK_ORDER_JA = {"data2": {"organizationId": "500030974", "award": {
    "date": "2026-09-11", "number": "NNG15SC71B", "deliveryOrderNumber": "70CTD026FR0000064",
    "justificationAuthority": {"authority": "brand"}}}}
WITH_AWARDEE = {"data2": {"organizationId": "100", "award": {
    "date": "2025-04-18", "amount": "1536472.17000000", "number": "VA11816D1006",
    "awardee": {"name": "FAVOR TECHCONSULTING LLC", "ueiSAM": "R4WTAS3E7BA4",
                "location": {"zip": "22182-2739"}},
    "lineItemNumber": "", "deliveryOrderNumber": " 36C10B20N10060037_P00020 ",
    "justificationAuthority": {"authority": "5", "modificationNumber": "P00020"}}}}
NO_ORDER = {"data2": {"organizationId": "100188028", "award": {
    "date": "2025-02-01", "number": "75D30125P20445",
    "justificationAuthority": {"authority": "far13"}}}}
SPECIAL_EMPTY = {"data2": {"organizationId": "100167100", "award": {}}}
SPECIAL_NULL = {"data2": {"organizationId": "100076487", "award": None}}


def test_parse_task_order_ja():
    r = sam_award.parse_award(TASK_ORDER_JA)
    assert r["sam_award_status"] == "ok"
    assert r["sam_award_number"] == "NNG15SC71B"
    assert r["sam_award_order_number"] == "70CTD026FR0000064"
    assert r["sam_award_date"] == "2026-09-11"
    assert r["sam_justification_authority"] == "brand"
    assert r["sam_organization_id"] == "500030974"
    assert r["sam_awardee_name"] is None and r["sam_awardee_uei"] is None


def test_parse_awardee_and_verbatim_order_number():
    r = sam_award.parse_award(WITH_AWARDEE)
    assert r["sam_awardee_name"] == "FAVOR TECHCONSULTING LLC"
    assert r["sam_awardee_uei"] == "R4WTAS3E7BA4"
    assert r["sam_award_amount"] == "1536472.17000000"
    # copied as typed (only stripped): the modification suffix stays
    assert r["sam_award_order_number"] == "36C10B20N10060037_P00020"
    assert r["sam_award_modification_number"] == "P00020"


def test_parse_no_order_number_is_none_not_missing_status():
    r = sam_award.parse_award(NO_ORDER)
    assert r["sam_award_status"] == "ok"
    assert r["sam_award_order_number"] is None
    assert r["sam_award_number"] == "75D30125P20445"


def test_parse_empty_or_null_award_block():
    for rec in (SPECIAL_EMPTY, SPECIAL_NULL, {}, None, {"data2": None}):
        r = sam_award.parse_award(rec)
        assert r["sam_award_status"] == "no_award"
        assert all(r[c] is None for c in sam_award.FIELD_COLUMNS if c != "sam_organization_id")
    assert sam_award.parse_award(SPECIAL_EMPTY)["sam_organization_id"] == "100167100"
    # an award block whose every value is blank is no award either
    assert sam_award.parse_award({"data2": {"award": {"number": "", "lineItemNumber": ""}}})[
        "sam_award_status"] == "no_award"


def _client(responses):
    return SamClient(requests_per_second=0, session=FakeSession(responses), sleep=lambda s: None,
                     max_retries=1)


def test_fetch_award_statuses():
    assert sam_award.fetch_award(_client([FakeResp(200, TASK_ORDER_JA)]), "n")["sam_award_status"] == "ok"
    # 404 with an empty body (what SAM sends): not_found, not retried
    c = _client([FakeResp(404)])
    assert sam_award.fetch_award(c, "n")["sam_award_status"] == "not_found"
    assert len(c.session.calls) == 1
    assert c.session.calls[0] == "https://sam.gov/api/prod/opps/v2/opportunities/n"
    # 503 twice (one retry): an error status, nothing raised
    st = sam_award.fetch_award(_client([FakeResp(503), FakeResp(503)]), "n")["sam_award_status"]
    assert st.startswith("error: ") and "503" in st

    class NotJson(FakeResp):
        def json(self):
            raise ValueError("no json")
    st = sam_award.fetch_award(_client([NotJson(200)]), "n")["sam_award_status"]
    assert st.startswith("error: ") and "not JSON" in st


class FakeClient:
    """get_notice by notice id; records calls."""

    def __init__(self, records):
        self.records = records
        self.calls = []

    def get_notice(self, nid):
        self.calls.append(nid)
        r = self.records.get(nid)
        if isinstance(r, Exception):
            raise r
        if r is None:
            raise SamError("notice HTTP 404", 404)
        return r


def _store(tmp_path, statuses):
    s = Store(str(tmp_path))
    for nid, st in statuses.items():
        s.notices[nid] = {"notice_id": nid, "posted_date": "2025-03-01", "sam_award_status": st}
    return s


def test_select_default_pending_only(tmp_path):
    s = _store(tmp_path, {"new": "pending", "old": None, "ok": "ok", "bad": "error: x", "gone": "not_found"})
    assert sam_award.select(s) == ["new"]
    assert sam_award.select(s, backfill=True) == ["new", "old"]
    assert sam_award.select(s, retry_errors=True) == ["bad", "gone", "new"]
    assert sam_award.select(s, notice_ids=["ok", "missing"]) == ["ok"]


def test_fetch_fills_columns_and_counts(tmp_path):
    s = _store(tmp_path, {"a": "pending", "b": "pending", "c": "pending", "d": None})
    fc = FakeClient({"a": TASK_ORDER_JA, "b": SPECIAL_NULL, "c": None})
    out = sam_award.fetch_sam_awards(s, fc, now=lambda: "T1")
    assert sorted(fc.calls) == ["a", "b", "c"]               # d (never fetched) waits for --backfill
    assert out["by_status"] == {"ok": 1, "no_award": 1, "not_found": 1}
    assert s.notices["a"]["sam_award_order_number"] == "70CTD026FR0000064"
    assert s.notices["a"]["sam_award_checked_at"] == "T1"
    assert s.notices["d"]["sam_award_status"] is None
    assert sam_award.coverage(s) == {"no_award": 1, "not_found": 1, "ok": 1, "unfetched": 1}


def test_backfill_resumes_from_cache_after_tables_replaced(tmp_path):
    s = _store(tmp_path, {"a": None, "b": None, "c": None})
    fc = FakeClient({"a": TASK_ORDER_JA, "b": NO_ORDER, "c": RuntimeError("boom")})
    out = sam_award.fetch_sam_awards(s, fc, backfill=True, limit=2, workers=1)
    assert out["fetched"] == 2 and out["remaining_unfetched"] == 1
    first = set(fc.calls)
    cache = [json.loads(line) for line in open(tmp_path / "cache" / "sam_award.jsonl")]
    assert {r["notice_id"] for r in cache} == first

    # `pull` replaced the tables: statuses are null again. The cached answers
    # are applied without asking SAM; only the rest is fetched.
    s2 = _store(tmp_path, {"a": None, "b": None, "c": None})
    fc2 = FakeClient({"a": TASK_ORDER_JA, "b": NO_ORDER, "c": RuntimeError("boom")})
    out2 = sam_award.fetch_sam_awards(s2, fc2, backfill=True)
    assert set(fc2.calls) == {"a", "b", "c"} - first
    assert out2["from_cache"] == 2
    assert s2.notices["a"]["sam_award_order_number"] == "70CTD026FR0000064"
    # an exception becomes an error status, cached but not treated as settled
    assert s2.notices["c"]["sam_award_status"].startswith("error: RuntimeError")
    s3 = _store(tmp_path, {"a": None, "b": None, "c": None})
    fc3 = FakeClient({"c": NO_ORDER})
    sam_award.fetch_sam_awards(s3, fc3, backfill=True)
    assert fc3.calls == ["c"]


def test_pending_ignores_cache(tmp_path):
    """An edited notice is re-asked even if an older answer is cached."""
    s = _store(tmp_path, {"a": None})
    sam_award.fetch_sam_awards(s, FakeClient({"a": NO_ORDER}), backfill=True)
    s.notices["a"]["sam_award_status"] = "pending"
    fc = FakeClient({"a": TASK_ORDER_JA})
    sam_award.fetch_sam_awards(s, fc)
    assert fc.calls == ["a"]
    assert s.notices["a"]["sam_award_order_number"] == "70CTD026FR0000064"


def test_truncated_cache_line_is_skipped(tmp_path):
    p = tmp_path / "c.jsonl"
    p.write_text(json.dumps({"notice_id": "a", "sam_award_status": "ok"}) + "\n{\"notice_id\": \"b\", ")
    assert list(sam_award.load_cache(str(p))) == ["a"]


def _result(*recs):
    return CollectResult(notices={r["notice_id"]: r for r in recs}, exclusions=[],
                         stats=CollectStats(start_date=dt.date(2025, 1, 20)))


def _notice(nid, fp="f1", posted="2025-03-01 00:00:00"):
    return {"notice_id": nid, "title": "t", "posted_date": posted, "type": "Justification",
            "row_fingerprint": fp, "source_file": "active.csv", "source_files": ["active.csv"]}


def test_merge_notices_sets_pending_and_keeps_answers(tmp_path):
    s = Store(str(tmp_path))
    pipeline.merge_notices(s, _result(_notice("a"), _notice("b")))
    assert s.notices["a"]["sam_award_status"] == "pending"
    for nid in ("a", "b"):
        sam_award.apply(s, nid, {**sam_award.parse_award(TASK_ORDER_JA), "sam_award_checked_at": "T"})
    # a pre-existing notice from before the columns: stays null (backfill's job)
    s.notices["z"] = {"notice_id": "z", "posted_date": "2025-03-01", "row_fingerprint": "f1",
                      "source_files": ["active.csv"]}
    pipeline.merge_notices(s, _result(_notice("a"), _notice("b", fp="f2"), _notice("z")))
    assert s.notices["a"]["sam_award_status"] == "ok"                       # unchanged row keeps it
    assert s.notices["a"]["sam_award_order_number"] == "70CTD026FR0000064"
    assert s.notices["b"]["sam_award_status"] == "pending"                  # edited -> re-ask
    assert s.notices["b"]["sam_award_order_number"] == "70CTD026FR0000064"  # old answer kept meanwhile
    assert s.notices["z"].get("sam_award_status") is None


def test_columns_round_trip_through_parquet(tmp_path):
    assert all(c in NOTICE_COLUMNS for c in sam_award.COLUMNS)
    s = _store(tmp_path, {"a": None, "b": None})
    s.notices["a"]["row_fingerprint"] = "f"
    sam_award.apply(s, "a", {**sam_award.parse_award(WITH_AWARDEE), "sam_award_checked_at": "T"})
    s.save()
    df = pd.read_parquet(tmp_path / "data" / "notices.parquet").set_index("notice_id")
    assert df.loc["a", "sam_award_order_number"] == "36C10B20N10060037_P00020"
    assert df.loc["a", "sam_awardee_uei"] == "R4WTAS3E7BA4"
    assert pd.isna(df.loc["b", "sam_award_status"])          # missing stays missing, not "None"
    s2 = Store(str(tmp_path)).load()
    assert s2.notices["b"]["sam_award_status"] is None
    assert s2.notices["a"]["sam_award_status"] == "ok"
