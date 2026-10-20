import csv
import datetime as dt
import io

import pytest

from federal_ja.collect.sources import (
    collect_ja_notices, collection_reason, is_ja_type, parse_posted_date, row_fingerprint,
    title_looks_like_ja,
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


def test_open_csv_url_keeps_session_alive(monkeypatch):
    import gc
    import weakref
    from federal_ja.collect import sources

    class Raw(io.BytesIO):
        decode_content = False
        auto_close = True

    class Resp:
        def __init__(self):
            self.raw = Raw("NoticeId,Type,PostedDate\na,Justification,2025-03-01\n".encode("latin-1"))
        def raise_for_status(self):
            pass

    class Sess:
        def get(self, *a, **k):
            return Resp()
    s = Sess()
    ref = weakref.ref(s)
    fh = sources.open_csv_url("https://x", session=s)
    del s
    gc.collect()
    assert ref() is not None          # held by the stream
    assert "Justification" in fh.read()


# Titles copied from Special Notices in SAM's active CSV (2026-10-19), plus
# spacing / punctuation variants of the same keywords.
GATE_YES = [
    "Notice of Intent to Sole Source: Operations, Maintenance, and Defense of Army Communications",
    "NOTICE OF INTENT TO SOLE SOURCE TO SONALYST",
    "Notice of Intent to Award Sole-Source Contract",
    "6640--Sole Source Post-Award Notice Individually Ventilated Cage Systems",
    "Notice of Intent to Single Source MH-139A Battery Maintenance Service",
    "Post Award Redacted J&A DIA APC Warranty",
    "USACE Basis of Design J & A for Interior Finishes - Notice of Intent",
    "Class j&a for controllers",
    "Redacted Limited Sources Justification for PRISM Hosting, Operations, and Maintenance Services",
    "Posting Limited- Sources Justification (LSJ) for Forescout Products",
    "R--Justification for Sexual Assault Hotline",
    "Award under FAR 6.302-1, only one responsible source",
    "Bridge contract - FAR6.302-2 Unusual and Compelling Urgency",
    "Exception to Fair Opportunity - Task Order 12",
]
GATE_NO = [
    "Notice of Intent DNA Synthesizer",
    "Notice of Intent to Award - Strategic Defense Industrial Base (SDIB) Software-as-a-Service",
    "Brand Name - Capital IQ Software",
    "NOTICE OF INTENT TO AWARD A NON-COMPETITIVE CONTRACT Operational Lease of Flexible Endoscopy",
    "Industry Day: J&As and You",       # "J&As": no word boundary after the A
    "Solesource requirement",          # no separator between the words
    "Hochiki - Fire Alarm Panel Brand name Class JA W912PL25S0007",
    "",
]


@pytest.mark.parametrize("title", GATE_YES)
def test_title_gate_matches(title):
    assert title_looks_like_ja(title)
    assert collection_reason("Special Notice", title) == "special_notice_title_match"


@pytest.mark.parametrize("title", GATE_NO)
def test_title_gate_rejects(title):
    assert not title_looks_like_ja(title)
    assert collection_reason("Special Notice", title) is None


def test_collection_reason_needs_special_notice_type():
    t = "Notice of Intent to Sole Source"
    assert collection_reason(" Special Notice ", t) == "special_notice_title_match"
    assert collection_reason("Presolicitation", t) is None
    assert collection_reason("Award Notice", t) is None
    assert collection_reason("Special Notices", t) is None
    assert collection_reason(None, t) is None
    # a Justification is collected whatever its title says
    assert collection_reason("Justification", "Anything") == "justification_type"
    assert collection_reason("Justification", t) == "justification_type"


def test_special_notices_collected_and_counted_separately():
    res = collect_ja_notices([("active.csv", make_csv([
        row("j", "2025-03-01 10:00:00"),
        row("s1", "2025-03-01 10:00:00", "Special Notice", Title="NOTICE OF INTENT TO SOLE SOURCE"),
        row("s2", "2025-03-01 10:00:00", "Special Notice", Title="Industry Day"),
        # description mentions a J&A but the title does not: not collected
        row("s3", "2025-03-01 10:00:00", "Special Notice", Title="Industry Day",
            Description="The sole source justification is attached."),
        row("s4", "2024-12-01 10:00:00", "Special Notice", Title="J & A posting"),   # before start
        row("p", "2025-03-01 10:00:00", "Presolicitation", Title="Sole-Source requirement"),
    ]))], START)
    assert set(res.notices) == {"j", "s1"}
    assert res.notices["j"]["collection_reason"] == "justification_type"
    assert res.notices["s1"]["collection_reason"] == "special_notice_title_match"
    assert res.notices["s1"]["type"] == "Special Notice"          # type kept as SAM gives it
    st = res.stats
    f = st.files[0]
    assert (f.rows, f.ja_rows, f.special_notice_rows, f.special_notice_title_matches) == (6, 1, 4, 2)
    assert f.ja_types == {"Justification": 1}
    assert st.ja_rows_total == 1 and st.unique_ja_ids == 1
    assert st.rows_by_reason == {"justification_type": 1, "special_notice_title_match": 2}
    assert st.unique_ids_by_reason == {"justification_type": 1, "special_notice_title_match": 2}
    assert st.unique_ids_total == 3
    assert st.in_scope == 2
    assert st.in_scope_by_reason == {"justification_type": 1, "special_notice_title_match": 1}
    assert st.before_start_by_reason == {"justification_type": 0, "special_notice_title_match": 1}
    assert st.type_counts_in_scope == {"Justification": 1, "Special Notice": 1}
    d = st.as_dict()
    assert d["files"][0]["special_notice_title_matches"] == 2
    assert d["in_scope_by_reason"]["special_notice_title_match"] == 1
    assert sum(d["in_scope_by_reason"].values()) == d["in_scope"]
    assert sum(d["unique_ids_by_reason"].values()) == d["unique_ids_total"]


def test_dedup_across_paths_kept_row_sets_reason():
    # The same NoticeId as a Special Notice in an archive and, later, as a
    # Justification in the active file: one notice, reason from the kept row.
    active = make_csv([row("x", "2025-06-01 00:00:00", "Justification")])
    arch = make_csv([row("x", "2025-05-01 00:00:00", "Special Notice", Title="Intent to sole source")])
    res = collect_ja_notices([("active.csv", active), ("FY2025.csv", arch)], START)
    assert res.notices["x"]["collection_reason"] == "justification_type"
    assert res.notices["x"]["source_files"] == ["active.csv", "FY2025.csv"]
    assert res.stats.duplicate_rows == 1
    assert res.stats.unique_ids_by_reason == {"justification_type": 1, "special_notice_title_match": 0}


def test_special_notice_exclusions_carry_reason():
    res = collect_ja_notices([("active.csv", make_csv([
        row("", "2025-03-01 00:00:00", "Special Notice", Title="Sole Source notice"),
        row("z", "not a date", "Special Notice", Title="Sole Source notice"),
    ]))], START)
    assert sorted((e["reason"], e["collection_reason"]) for e in res.exclusions) == [
        ("missing_notice_id", "special_notice_title_match"),
        ("unparseable_posted_date", "special_notice_title_match"),
    ]
