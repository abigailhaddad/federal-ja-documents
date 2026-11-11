"""The published extraction table: seeding a work directory from it,
rebuilding it, the shrink guard, and the incremental CLI path. No network."""

import json

import pandas as pd
import pytest

from federal_ja.analysis import cli, data, extract, extras, table
from tests.test_field_extraction import NID, answer, fake, tables


def _record(doc, **over):
    rec = extract.extract_document(doc, call_fn=fake(answer(**over)), retries=0, effort="high")
    rec["dataset_revision"] = "rev1"
    return rec


def _published(recs, skipped=()):
    rows = [{"notice_id": r["notice_id"], "status": "extracted", "record_json": json.dumps(r)} for r in recs]
    rows += [{"notice_id": s["notice_id"], "status": "skipped", "record_json": json.dumps(s)} for s in skipped]
    return pd.DataFrame(rows)


def test_seed_then_build_round_trip(tmp_path):
    doc = data.build_documents(*tables())[NID]
    rec = _record(doc)
    skip = {"notice_id": "c" * 32, "reason": "no_usable_text", "dataset_revision": "rev1"}
    info = table.seed_from_hf(tmp_path, table=_published([rec], [skip]))
    assert info == {"seeded": True, "extracted_rows": 1, "skipped_rows": 1, "second_call_rows": 0}
    # a second seed never overwrites work in progress
    assert table.seed_from_hf(tmp_path, table=_published([]))["seeded"] is False
    df = table.build_table(tmp_path, {NID: doc.meta})
    assert list(df.status) == ["extracted", "skipped"]
    row = df.iloc[0]
    assert row.vendor == "Acme Widget Corporation" and row.extract_version == extract.EXTRACT_VERSION
    assert json.loads(row.record_json) == rec
    assert df.iloc[1].skip_reason == "no_usable_text" and df.iloc[1].sam_link.endswith("/view")


def test_latest_record_wins_and_extraction_beats_skip(tmp_path):
    doc = data.build_documents(*tables())[NID]
    old = {**_record(doc), "extract_version": "2026-10-17.extract2"}
    new = _record(doc)
    (tmp_path / "extractions.jsonl").write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n")
    (tmp_path / "skipped.jsonl").write_text(json.dumps({"notice_id": NID, "reason": "no_text"}) + "\n")
    df = table.build_table(tmp_path, {})
    assert len(df) == 1 and df.iloc[0].extract_version == extract.EXTRACT_VERSION


def test_shrink_guard():
    table.check_not_shrinking(10, 10)
    with pytest.raises(table.ShrinkError):
        table.check_not_shrinking(9, 10)


def test_incremental_cli_extracts_only_notices_without_a_current_row(tmp_path, monkeypatch):
    """Seeded with a current-version row for the fixture notice and an
    old-version row for a second notice: only the second is extracted."""
    nid2 = "d" * 32
    n, a, p = tables()
    n2 = pd.concat([n, n.assign(notice_id=nid2, posted_date="2026-01-01 10:00:00")])
    a2 = pd.concat([a, a.assign(notice_id=nid2)])
    p2 = pd.concat([p, p.assign(notice_id=nid2)])
    docs = data.build_documents(n2, a2, p2)
    current = _record(docs[NID])
    old = {**_record(docs[nid2]), "extract_version": "2026-10-17.extract2"}
    monkeypatch.setattr(data, "download_tables", lambda *a, **k: {"sha": "rev2"})
    monkeypatch.setattr(data, "load_tables", lambda *a, **k: (n2, a2, p2))
    monkeypatch.setattr(table, "_download_published", lambda revision=None, path=table.TABLE_PATH:
                        _published([current, old]) if path == table.TABLE_PATH else _published([]))
    call = fake(answer())
    monkeypatch.setattr(extract.llm, "call", call)
    out = tmp_path / "x"
    assert cli.main(["extract", "--seed-from-hf", "--out", str(out), "--max-usd", "1"]) == 0
    assert len(call.calls) == 1
    recs = [json.loads(l) for l in (out / "extractions.jsonl").read_text().splitlines()]
    assert [r["notice_id"] for r in recs] == [NID, nid2, nid2] and recs[-1]["dataset_revision"] == "rev2"
    assert cli.main(["extract-table", "--out", str(out), "--table", str(tmp_path / "t.parquet")]) == 0
    df = pd.read_parquet(tmp_path / "t.parquet")
    assert len(df) == 2 and set(df.extract_version) == {extract.EXTRACT_VERSION}


def _second_call(nid, **fields):
    base = {k: None for k in ("incumbent_vendor", "source_approval_path_quote", "ceiling_arithmetic_ok")}
    return {"notice_id": nid, "extras_version": "2026-11-10.extras2", "checks_version": "x5", "model": "m",
            "effort": "high", "fields": {**base, "approved_sources": [], **fields},
            "rules": {"gao_protests": ["B-424920"], "gao_cited": ["B-401472"], "nsns": [], "form": None}}


def test_second_call_records_round_trip_and_only_checked_fields_are_published(tmp_path):
    doc = data.build_documents(*tables())[NID]
    rec = _record(doc)
    x = _second_call(NID, incumbent_vendor="Acme Widget Corporation", incumbent_vendor_quote="the incumbent, Acme",
                     interested_sources=[{"cage": None, "name": "Other Co", "quote": "Other Co responded"}],
                     not_to_exceed=900000.0, data_rights_basis="proprietary_to_contractor")
    pub = _published([rec]).assign(extras_record_json=[json.dumps(x)])
    info = table.seed_from_hf(tmp_path, table=pub)
    assert info["second_call_rows"] == 1
    assert [json.loads(l) for l in (tmp_path / "extras.jsonl").read_text().splitlines()] == [x]
    df = table.build_table(tmp_path, {NID: doc.meta})
    row = df.iloc[0]
    assert row.contractor_named == "Acme Widget Corporation" and row.gao_protest_numbers == "B-424920"
    stored = json.loads(row.extras_record_json)
    assert stored == extras.public_record(x) and stored["model"] == "m"
    # the stored record keeps nothing that is not published
    for k in ("interested_sources", "not_to_exceed", "data_rights_basis", "gao_cited"):
        assert k not in json.dumps(stored)
    # nothing that failed its check is a column: no interested firms, NTE, data-rights reason, cited decisions
    for col in df.columns:
        assert not any(k in col for k in ("interested", "not_to_exceed", "data_rights", "gao_cited", "unit_price"))
    assert set(extras.PUBLISHED_COLUMNS) <= set(df.columns)


def test_ceilings_are_published_only_when_all_three_add_up():
    ok = _second_call(NID, ceiling_arithmetic_ok=True, prior_ceiling=10.0, increase_amount=2.0, new_ceiling=12.0,
                      prior_ceiling_quote="from $10", increase_amount_quote="by $2", new_ceiling_quote="to $12")
    c = extras.published_columns(ok)
    assert (c["ceiling_prior_usd"], c["ceiling_increase_usd"], c["ceiling_new_usd"]) == (10.0, 2.0, 12.0)
    bad = _second_call(NID, ceiling_arithmetic_ok=False, prior_ceiling=10.0, increase_amount=3.0, new_ceiling=12.0)
    assert extras.published_columns(bad)["ceiling_new_usd"] is None
    assert extras.published_columns(None) == dict.fromkeys(extras.PUBLISHED_COLUMNS)


def test_dla_approved_sources_come_from_the_form_table_unless_the_model_names_another_cage():
    form = {"approved": [("95270", "SKURKA AEROSPACE INC")], "interested": []}
    x = _second_call(NID, approved_sources=[{"cage": "95270", "name": "SKURKA"}])
    x["rules"]["form"] = form
    assert extras.published_columns(x)["dla_approved_sources"] == "95270 | SKURKA AEROSPACE INC"
    x["fields"]["approved_sources"].append({"cage": "70326", "name": "TACTAIR"})
    assert extras.published_columns(x)["dla_approved_sources"] is None


def test_second_call_records_are_kept_on_excluded_and_skipped_rows(tmp_path):
    doc = data.build_documents(*tables())[NID]
    skip_id = "c" * 32
    (tmp_path / "extractions.jsonl").write_text(json.dumps(_record(doc)) + "\n")
    (tmp_path / "skipped.jsonl").write_text(json.dumps({"notice_id": skip_id, "reason": "no_usable_text"}) + "\n")
    (tmp_path / "extras.jsonl").write_text(json.dumps(_second_call(NID)) + "\n" + json.dumps(_second_call(skip_id)) + "\n")
    ex = table.build_excluded(tmp_path, {}, {NID: table.REASON_MODEL_NO})
    assert json.loads(ex.iloc[0].extras_record_json)["notice_id"] == NID
    df = table.build_table(tmp_path, {}, exclude={NID})
    assert json.loads(df.iloc[0].extras_record_json)["notice_id"] == skip_id
