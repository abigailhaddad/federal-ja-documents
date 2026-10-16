import csv
import datetime as dt
import io

from federal_ja.collect.sources import (
    collect_ja_notices, is_ja_type, parse_posted_date, row_fingerprint,
)

HEADER = ["NoticeId", "Title", "Sol#", "Department/Ind.Agency", "Sub-Tier", "Office",
          "PostedDate", "Type", "BaseType", "ArchiveType", "ArchiveDate", "AwardNumber",
          "Link", "Description"]


def make_csv(rows: list[dict]) -> io.StringIO:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=HEADER)
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in HEADER})
    # round-trip through latin-1 bytes like the real files
    return io.TextIOWrapper(io.BytesIO(buf.getvalue().encode("latin-1")), encoding="latin-1", newline="")


def row(nid, posted, type_="Justification", **kw):
    return {"NoticeId": nid, "PostedDate": posted, "Type": type_, "Title": f"t{nid}", **kw}


START = dt.date(2025, 1, 20)


def test_is_ja_type_variants():
    assert is_ja_type("Justification")
    assert is_ja_type("Justification and Approval (J&A)")
    assert is_ja_type("  Justification ")
    assert not is_ja_type("Award Notice")
    assert not is_ja_type("Special Notice")
    assert not is_ja_type("")
    assert not is_ja_type(None)


def test_parse_posted_date_forms():
    assert parse_posted_date("2026-10-15 15:03:33") == dt.date(2026, 10, 15)
    assert parse_posted_date("2025-01-21T10:00:00-05:00") == dt.date(2025, 1, 21)
    assert parse_posted_date("01/21/2025") == dt.date(2025, 1, 21)
    assert parse_posted_date("") is None
    assert parse_posted_date("garbage") is None


def test_filters_type_and_scope():
    res = collect_ja_notices([("active.csv", make_csv([
        row("a", "2025-01-20 00:00:01"),           # boundary: in scope
        row("b", "2025-01-19 23:59:59"),           # before start
        row("c", "2025-03-01 10:00:00", "Award Notice"),
        row("d", "2025-03-01 10:00:00", "Justification and Approval (J&A)"),
    ]))], START)
    assert set(res.notices) == {"a", "d"}
    assert res.stats.before_start == 1
    assert res.stats.type_counts_in_scope == {"Justification": 1, "Justification and Approval (J&A)": 1}
    assert res.notices["d"]["type"] == "Justification and Approval (J&A)"


def test_dedupe_keeps_latest_and_records_sources():
    active = make_csv([row("x", "2025-06-01 00:00:00", Title="new")])
    fy26 = make_csv([row("x", "2025-05-01 00:00:00", Title="old"), row("y", "2025-05-01 00:00:00")])
    fy25 = make_csv([row("y", "2025-05-01 00:00:00", Title="fy25")])
    res = collect_ja_notices([("active.csv", active), ("FY2026.csv", fy26), ("FY2025.csv", fy25)], START)
    assert res.notices["x"]["title"] == "new"
    assert res.notices["x"]["source_file"] == "active.csv"
    assert res.notices["x"]["source_files"] == ["active.csv", "FY2026.csv"]
    # tie on date -> earlier (higher-priority) source wins
    assert res.notices["y"]["source_file"] == "FY2026.csv"
    assert res.stats.duplicate_rows == 2
    assert res.stats.unique_ja_ids == 2


def test_later_row_in_lower_priority_file_wins():
    active = make_csv([row("x", "2025-02-01 00:00:00", Title="older")])
    arch = make_csv([row("x", "2025-03-01 00:00:00", Title="newer")])
    res = collect_ja_notices([("active.csv", active), ("FY2025.csv", arch)], START)
    assert res.notices["x"]["title"] == "newer"
    assert res.notices["x"]["source_file"] == "FY2025.csv"


def test_scope_uses_kept_row_and_counts_straddlers():
    arch = make_csv([row("x", "2025-01-10 00:00:00")])
    active = make_csv([row("x", "2025-02-01 00:00:00")])
    res = collect_ja_notices([("active.csv", active), ("FY2025.csv", arch)], START)
    assert "x" in res.notices
    assert res.stats.straddling_start == 1


def test_bad_rows_become_explicit_exclusions():
    res = collect_ja_notices([("active.csv", make_csv([
        row("", "2025-03-01 00:00:00"),
        row("z", "not a date"),
    ]))], START)
    reasons = sorted(e["reason"] for e in res.exclusions)
    assert reasons == ["missing_notice_id", "unparseable_posted_date"]
    assert res.notices == {}


def test_latin1_and_huge_fields():
    big = "é" * 300_000  # larger than the csv module's default field limit
    res = collect_ja_notices([("active.csv", make_csv([
        row("a", "2025-03-01 00:00:00", Description=big, Title="Café")]))], START)
    assert res.notices["a"]["title"] == "Café"
    assert len(res.notices["a"]["description"]) == 300_000


def test_public_sam_link_and_fingerprint_changes():
    r1 = row("a", "2025-03-01 00:00:00", Link="https://sam.gov/workspace/contract/opp/a/view")
    res = collect_ja_notices([("active.csv", make_csv([r1]))], START)
    assert res.notices["a"]["sam_link"] == "https://sam.gov/opp/a/view"
    assert res.notices["a"]["csv_link"].startswith("https://sam.gov/workspace")
    assert row_fingerprint(r1) != row_fingerprint(dict(r1, Description="edited"))
    assert row_fingerprint(r1) == row_fingerprint(dict(r1, Link="other"))
