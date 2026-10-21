"""The published extraction table: seeding a work directory from it,
rebuilding it, the shrink guard, and the incremental CLI path. No network."""

import json

import pandas as pd
import pytest

from federal_ja.analysis import cli, data, extract, table
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
    assert info == {"seeded": True, "extracted_rows": 1, "skipped_rows": 1}
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
    monkeypatch.setattr(table, "_download_published", lambda revision=None: _published([current, old]))
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
