import pandas as pd
import pytest

from federal_ja.collect import published, usaspending
from federal_ja.collect.store import Store


@pytest.mark.parametrize("raw,expected", [
    ("SPE4A726D0247", ("ok", "SPE4A726D0247")),
    ("SPRDL1-25-C-0057", ("ok", "SPRDL125C0057")),
    (" n00104 25 p jb47 ", ("ok", "N0010425PJB47")),
    ("SPRRA224D0011_P00003", ("ok", "SPRRA224D0011P00003")),
    ("47QRCA25DU287(SAIC)", ("ok", "47QRCA25DU287")),
    ("(SPE4A1-25-G-0011)", ("ok", "SPE4A125G0011")),
    ("19KU2026P0301-PR15881334", ("ok", "19KU2026P0301")),
    ("A09CLERK26-0005", ("ok", "A09CLERK260005")),
    ("TBD", ("no_piid", None)),
    ("(TBD)", ("no_piid", None)),
    ("See_JandA", ("no_piid", None)),
    ("P0314", ("no_piid", None)),
    ("", ("no_piid", None)),
    (None, ("no_piid", None)),
    ("N3220521D4105_4106", ("multiple_award_numbers", None)),
    ("70CDCR25D00000008_09_10", ("multiple_award_numbers", None)),
    ("47PK0121D0001-D0061", ("multiple_award_numbers", None)),
    ("SPE7LX26D0012-SPE7LX26D0013", ("multiple_award_numbers", None)),
    ("19TC1025D0008_and_19TC1025D0010", ("multiple_award_numbers", None)),
    ("HHSN316201200023W_75N98124F00001", ("multiple_award_numbers", None)),
])
def test_classify(raw, expected):
    assert usaspending.classify(raw) == expected


def tx(key, piid, agency="097", obl=0.0, date="2025-01-01", flag="AWARD", parent=None, mod="0",
       recipient="ACME", fy=2025, potential=None):
    return {"contract_award_unique_key": key, "award_id_piid": piid, "parent_award_id_piid": parent,
            "award_or_idv_flag": flag, "award_type": "DEFINITIVE CONTRACT" if flag == "AWARD" else None,
            "idv_type": "IDC" if flag == "IDV" else None, "awarding_agency_code": agency,
            "awarding_agency_name": "AG" + agency, "awarding_sub_agency_name": "SUB",
            "awarding_office_name": "OFF", "recipient_name": recipient, "recipient_uei": "UEI1",
            "recipient_parent_name": "PARENT", "federal_action_obligation": obl,
            "total_dollars_obligated": None, "current_total_value_of_award": 10.0,
            "potential_total_value_of_award": potential, "action_date": pd.Timestamp(date).date(),
            "modification_number": mod, "extent_competed": "NOT COMPETED",
            "other_than_full_and_open_competition": "ONLY ONE SOURCE",
            "usaspending_permalink": "https://www.usaspending.gov/award/" + key + "/",
            "last_modified_date": None, "k_piid": usaspending.normalize(piid), "fiscal_year_file": fy}


def run(notices, rows, orders=None):
    txdf = pd.DataFrame(rows)
    odf = pd.DataFrame(orders or [], columns=["k_parent", "parent_award_agency_id", "n_orders", "obligated"])
    return usaspending.match_notices(notices, txdf, odf, "2026-10-01")


def test_exact_match_sums_obligations_across_years():
    rows = [tx("CONT_AWD_W1_9700_-NONE-_-NONE-", "W912DQ25C0001", obl=100, date="2025-02-01", fy=2025),
            tx("CONT_AWD_W1_9700_-NONE-_-NONE-", "W912DQ25C0001", obl=-20, date="2026-01-05",
               fy=2026, mod="P00001", recipient="ACME LLC", potential=500.0)]
    out = run([{"notice_id": "n1", "award_number": "W912DQ-25-C-0001", "cgac": "021"}], rows)["n1"]
    assert out["usaspending_matched"] and out["usaspending_match_method"] == "exact"
    assert out["usaspending_total_obligated"] == 80
    assert out["usaspending_transactions"] == 2
    assert out["usaspending_recipient_name"] == "ACME LLC"          # latest transaction
    assert out["usaspending_first_action_date"] == "2025-02-01"
    assert out["usaspending_latest_action_date"] == "2026-01-05"
    assert out["usaspending_potential_total_value"] == 500.0
    assert out["usaspending_same_agency"] is True                    # Army 021 counts as DoD 097
    assert out["usaspending_as_of"] == "2026-10-01"


def test_multiple_candidates_agency_picks_or_ambiguous():
    rows = [tx("CONT_AWD_0001_7000_A_7000", "0001", agency="070", parent="A"),
            tx("CONT_AWD_0001_3600_B_3600", "0001", agency="036", parent="B"),
            tx("CONT_AWD_0001_3600_C_3600", "0001", agency="036", parent="C")]
    notices = [{"notice_id": "dhs", "award_number": "000001", "cgac": "070"}]
    rows = [dict(r, award_id_piid="000001", k_piid="000001") for r in rows]
    out = run(notices + [{"notice_id": "va", "award_number": "000001", "cgac": "036"},
                         {"notice_id": "gsa", "award_number": "000001", "cgac": "047"}], rows)
    assert out["dhs"]["usaspending_matched"] and out["dhs"]["usaspending_match_method"] == "exact+agency"
    assert out["dhs"]["usaspending_parent_award_piid"] == "A"
    for nid in ("va", "gsa"):
        assert not out[nid]["usaspending_matched"]
        assert out[nid]["usaspending_match_method"] == "ambiguous"
        assert out[nid]["usaspending_candidates"] == 3
        assert out[nid]["usaspending_recipient_name"] is None


def test_modification_suffix_and_failures():
    rows = [tx("CONT_AWD_X_9700_-NONE-_-NONE-", "SPRRA224D0011")]
    out = run([{"notice_id": "a", "award_number": "SPRRA224D0011_P00003", "cgac": "097"},
               {"notice_id": "b", "award_number": "SPRRA224D9999", "cgac": "097"},
               {"notice_id": "c", "award_number": "TBD", "cgac": "097"},
               {"notice_id": "d", "award_number": "SPRRA224D0011", "cgac": "036"}], rows)
    assert out["a"]["usaspending_match_method"] == "without_modification_number"
    assert out["b"]["usaspending_match_method"] == "piid_not_found" and not out["b"]["usaspending_matched"]
    assert out["c"]["usaspending_match_method"] == "no_piid"
    # a single candidate at another agency is matched but flagged
    assert out["d"]["usaspending_matched"] and out["d"]["usaspending_same_agency"] is False
    assert "differs" in out["d"]["usaspending_match_note"]


def test_idv_orders():
    rows = [tx("CONT_IDV_SPE4A726D0247_9700", "SPE4A726D0247", flag="IDV")]
    orders = [{"k_parent": "SPE4A726D0247", "parent_award_agency_id": "9700", "n_orders": 3, "obligated": 1234.5},
              {"k_parent": "SPE4A726D0247", "parent_award_agency_id": "4700", "n_orders": 9, "obligated": 9.0}]
    out = run([{"notice_id": "n", "award_number": "SPE4A726D0247", "cgac": "097"}], rows, orders)["n"]
    assert out["usaspending_award_or_idv"] == "IDV" and out["usaspending_award_type"] == "IDC"
    assert out["usaspending_idv_orders"] == 3 and out["usaspending_idv_orders_obligated"] == 1234.5


def test_published_tables_denormalise_and_keep_working_copy(tmp_path):
    s = Store(str(tmp_path))
    s.notices["n1"] = {"notice_id": "n1", "title": "Bridge", "agency": "DEPT OF DEFENSE",
                       "sub_tier": "DEPT OF THE ARMY", "office": "W6QK", "posted_date": "2025-03-01",
                       "type": "Justification", "award_number": "W1", "sam_link": "https://sam.gov/x"}
    s.notices["n2"] = {"notice_id": "n2", "title": "Other", "agency": "GSA", "type": "Justification"}
    s.attachments[("n1", "r1")] = {"notice_id": "n1", "resource_id": "r1", "filename": "a.pdf"}
    s.attachments[("n1", "r2")] = {"notice_id": "n1", "resource_id": "r2", "filename": "b.pdf"}
    s.pages[("n1", "r1")] = [{"notice_id": "n1", "resource_id": "r1", "page_number": 1, "text": "x"}]
    us = run([{"notice_id": "n1", "award_number": "W1", "cgac": "021"},
              {"notice_id": "n2", "award_number": "TBD", "cgac": "047"}], [])
    paths = published.write(s, us)
    att = pd.read_parquet(paths["data/attachments.parquet"])
    assert list(att.columns[:10]) == ["notice_id"] + published.COPIED_COLUMNS + ["resource_id"]
    assert (att.agency == "DEPT OF DEFENSE").all() and (att.notice_title == "Bridge").all()
    pg = pd.read_parquet(paths["data/pages.parquet"])
    assert pg.sub_tier.tolist() == ["DEPT OF THE ARMY"] and pg.notice_type.tolist() == ["Justification"]
    no = pd.read_parquet(paths["data/notices.parquet"])
    assert len(no) == 2 and set(usaspending.COLUMNS) <= set(no.columns)
    assert no.set_index("notice_id").loc["n2", "usaspending_match_method"] == "no_piid"
    assert str(no.usaspending_matched.dtype) == "boolean"
    # the working copy keeps its own schema, and reloading published tables drops the copies
    assert "agency" not in pd.read_parquet(tmp_path / "data" / "attachments.parquet").columns
    for rel, p in paths.items():
        (tmp_path / rel).write_bytes(open(p, "rb").read())
    t = Store(str(tmp_path)).load()
    t.save()
    assert "usaspending_matched" not in pd.read_parquet(tmp_path / "data" / "notices.parquet").columns


def test_published_requires_every_notice(tmp_path):
    s = Store(str(tmp_path))
    s.notices["n1"] = {"notice_id": "n1"}
    with pytest.raises(AssertionError):
        published.write(s, {})


def test_idv_orders_count_an_order_once_across_years():
    odf = pd.DataFrame([
        {"k_parent": "P", "parent_award_agency_id": "9700", "contract_award_unique_key": "CONT_AWD_o1", "obligated": 5.0},
        {"k_parent": "P", "parent_award_agency_id": "9700", "contract_award_unique_key": "CONT_AWD_o1", "obligated": 7.0},
        {"k_parent": "P", "parent_award_agency_id": "9700", "contract_award_unique_key": "CONT_AWD_o2", "obligated": 1.0}])
    agg = usaspending.idv_orders(odf)
    assert agg.n_orders.tolist() == [2] and agg.obligated.tolist() == [13.0]


def test_placeholders_and_agency_names():
    assert usaspending.classify("SPE1C1-25-D-TBD") == ("no_piid", None)
    assert usaspending.classify("FA8688-26-C-XXXX") == ("no_piid", None)
    lab = {"cgac": "016", "agency": "LABOR, DEPARTMENT OF"}
    assert usaspending.same_agency(lab, "1601", "Department of Labor") is True
    assert usaspending.same_agency(lab, "047", "General Services Administration") is False
    assert usaspending.same_agency({"cgac": "020", "agency": "TREASURY"}, "999", None) is None


def test_solicitation_note():
    out = run([{"notice_id": "s", "award_number": "N0018926RB001", "cgac": "017"}], [])["s"]
    assert out["usaspending_match_method"] == "piid_not_found"
    assert "solicitation" in out["usaspending_match_note"]


def test_idv_orders_skip_child_idvs():
    odf = pd.DataFrame([
        {"k_parent": "P", "parent_award_agency_id": "4732", "contract_award_unique_key": "CONT_AWD_o1", "obligated": 5.0},
        {"k_parent": "P", "parent_award_agency_id": "4732", "contract_award_unique_key": "CONT_IDV_bpa", "obligated": 0.0}])
    assert usaspending.idv_orders(odf).n_orders.tolist() == [1]
