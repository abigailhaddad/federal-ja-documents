"""Offline tests for the data dictionary (data_docs.py) and the SQLite build (build_sqlite.py)."""
import sqlite3

import pandas as pd
import pytest

from federal_ja.collect import build_sqlite, data_docs

CFG = {
    "dataset": "someone/example",
    "tables": {
        "parent": {"file": "parent.parquet", "config": "default", "grain": "one parent", "primary_key": "id",
                   "columns": {"id": "row id", "n": "a number", "flag": "a flag"}},
        "child": {"file": "child.parquet", "config": "child", "grain": "one child", "primary_key": "cid",
                  "unconfirmed": ["note"], "columns": {"cid": "row id", "id": "parent id | pipe", "note": "a note"}},
    },
    "joins": [{"left": "parent.id", "right": "child.id", "kind": "||--o{", "label": "id", "note": "exact"}],
}


@pytest.fixture
def data(tmp_path):
    pd.DataFrame({"id": ["a", "b", "c"], "n": [1, None, 3], "flag": [True, False, True]}).astype({"n": "Int64"}) \
        .to_parquet(tmp_path / "parent.parquet", index=False)
    pd.DataFrame({"cid": ["x", "y"], "id": ["a", None], "note": ["", "hi"]}).to_parquet(tmp_path / "child.parquet", index=False)
    return tmp_path


def test_check_catches_drift(data):
    data_docs.check(str(data), CFG)
    bad = {**CFG, "tables": {**CFG["tables"], "parent": {**CFG["tables"]["parent"], "columns": {"id": "x", "n": "y"}}}}
    with pytest.raises(SystemExit, match="no description"):
        data_docs.check(str(data), bad)


def test_markdown(data):
    md = data_docs.markdown(str(data), CFG)
    assert "| `parent` | `parent.parquet` | `default` | 3 |" in md
    assert "parent ||--o{ child" in md and "string id FK" in md
    assert "| `n` | int64 | a number | 66.7% | 66.7% | `1` |" in md
    assert "| `note` | string | a note **(meaning not confirmed)** | 100.0% | 50.0% | `hi` |" in md
    assert "parent id \\| pipe" in md


def test_sqlite_matches_parquet(data, tmp_path):
    out = tmp_path / "x.sqlite"
    assert build_sqlite.build(str(data), str(out), CFG) == {"parent": 3, "child": 2}
    con = sqlite3.connect(out)
    assert con.execute("select id, n, flag from parent order by id").fetchall() == [("a", 1, 1), ("b", None, 0), ("c", 3, 1)]
    assert con.execute("select count(*) from child join parent using (id)").fetchone() == (1,)


def test_one_table_no_joins(data):
    cfg = {"dataset": "someone/example", "tables": {"parent": CFG["tables"]["parent"]}}
    md = data_docs.markdown(str(data), cfg)
    assert "How the tables join" not in md and "| `flag` | bool | a flag |" in md


def test_sqlite_dates_and_lists(tmp_path):
    import datetime
    pd.DataFrame({"id": ["a", "b"], "d": [datetime.date(2026, 1, 2), None], "tags": [["x", "y"], None]}) \
        .to_parquet(tmp_path / "t.parquet", index=False)
    cfg = {"dataset": "someone/example", "tables": {"t": {"file": "t.parquet", "config": "default", "grain": "g",
           "primary_key": "id", "columns": {"id": "i", "d": "a date", "tags": "a list"}}}}
    build_sqlite.build(str(tmp_path), str(tmp_path / "t.sqlite"), cfg)
    rows = sqlite3.connect(tmp_path / "t.sqlite").execute("select d, tags from t order by id").fetchall()
    assert rows == [("2026-01-02", '["x", "y"]'), (None, None)]


def test_update_card(data, tmp_path):
    card = tmp_path / "card.md"
    card.write_text(f"intro\n{data_docs.CARD_START}\nold\n{data_docs.CARD_END}\nend\n")
    data_docs.update_card(str(data), {**CFG, "card": str(card)})
    text = card.read_text()
    assert text.startswith("intro\n") and text.endswith(f"{data_docs.CARD_END}\nend\n")
    assert "old" not in text and "## Data dictionary" in text and "| `parent` |" in text


def test_flat_paths(data, tmp_path):
    cfg = {**CFG, "tables": {n: {**t, "file": "data/" + t["file"]} for n, t in CFG["tables"].items()}}
    with pytest.raises(FileNotFoundError):
        data_docs.check(str(data), cfg)
    data_docs.check(str(data), cfg, flat=True)
    assert "| `parent` | `data/parent.parquet` |" in data_docs.markdown(str(data), cfg, flat=True)


def test_composite_key(data, tmp_path):
    cfg = {**CFG, "tables": {**CFG["tables"], "child": {**CFG["tables"]["child"], "primary_key": ["cid", "id"]}}}
    assert "Part of the primary key (cid, id)." in data_docs.markdown(str(data), cfg)
    build_sqlite.build(str(data), str(tmp_path / "k.sqlite"), cfg)
    idx = sqlite3.connect(tmp_path / "k.sqlite").execute(
        "select sql from sqlite_master where name = 'child_cid_id'").fetchone()[0]
    assert "UNIQUE" in idx and '"cid", "id"' in idx


def test_repo_config_is_consistent():
    """docs/tables.toml parses, and every join and key names a described column."""
    cfg = data_docs.load_config()
    for t in cfg["tables"].values():
        assert all(k in t["columns"] for k in data_docs.key_columns(t))
        assert all(c in t["columns"] for c in t.get("unconfirmed", []))
    for j in cfg["joins"]:
        for side in (j["left"], j["right"]):
            tbl, col = side.split(".")
            assert col in cfg["tables"][tbl]["columns"]
