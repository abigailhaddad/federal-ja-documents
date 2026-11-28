import pytest

from federal_ja.collect import hf_sync
from federal_ja.collect.card import EXPERIMENTAL_SECTION, render_card
from federal_ja.collect.store import Store

ANALYSIS_TABLES = {
    "experimental_extractions": "analysis/extractions.parquet",
    "experimental_theme_tags": "analysis/theme_tags.parquet",
    "experimental_theme_status": "analysis/theme_status.parquet",
}


def _front_matter(card: str) -> dict:
    yaml = pytest.importorskip("yaml")
    assert card.startswith("---\n")
    return yaml.safe_load(card.split("---\n")[1])


def test_card_keeps_experimental_configs_and_section(tmp_path):
    card = render_card(Store(str(tmp_path)), "2025-01-20", "2026-10-18T00:00:00")
    configs = {c["config_name"]: c for c in _front_matter(card)["configs"]}
    for name, path in ANALYSIS_TABLES.items():
        assert configs[name]["data_files"] == path
        assert not configs[name].get("default")
    assert configs["notices"]["default"] is True     # notices stays the default viewer table
    assert EXPERIMENTAL_SECTION in card
    assert "## Experimental: AI-generated tables" in card
    assert "(#experimental-ai-generated-tables)" in card   # link from the Contents table


def test_experimental_section_labels():
    s = EXPERIMENTAL_SECTION
    for must in ["claude-haiku-5-5", "11a221ab5ae9583a95ae645883de75237a46d44d", "not refreshed",
                 "refreshed after each", "2026-10-21.extract6", "people_json", "`need_by_date` (19 of 30 right",
                 "field_extraction.md", "not 100% right", "workforce_cause", "agency_delay_cause", "sources_rejected",
                 "estimated_value_usd", "prompts.yaml", "patterns.py"]:
        assert must in s, must


def test_stale_paths_never_include_analysis():
    remote = ["README.md", "data/notices.parquet", "old/layout.parquet",
              "analysis/extractions.parquet", "analysis/validation/theme_tags/round3/run_A.jsonl"]
    keep = {"README.md", "data/notices.parquet"}
    assert hf_sync.stale_paths(remote, keep) == ["old/layout.parquet"]


def test_publish_tables_delete_stale_keeps_analysis(tmp_path, monkeypatch):
    s = Store(str(tmp_path))
    tables = {}
    for rel in hf_sync.TABLE_FILES:
        p = tmp_path / "pub" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        tables[rel] = str(p)
    monkeypatch.setattr(hf_sync.published, "write", lambda store, us_cols: tables)

    remote = hf_sync.TABLE_FILES + ["README.md", ".gitattributes", "old/snapshot.parquet",
                                    "analysis/extractions.parquet", "analysis/theme_tags.parquet",
                                    "analysis/validation/README.md"]
    commits = []

    class Api:
        def list_repo_files(self, repo_id, repo_type):
            return list(remote)

        def create_commit(self, repo_id, ops, repo_type, commit_message):
            commits.append(ops)

            class Info:
                oid = "c1"
            return Info()

    monkeypatch.setattr(hf_sync, "_api", lambda: Api())
    out = hf_sync.publish_tables(s, "card", delete_stale=True, us_cols=None)
    assert out["deleted_stale"] == ["old/snapshot.parquet"]
    deleted = [op.path_in_repo for op in commits[-1]]
    assert deleted == ["old/snapshot.parquet"]


def test_card_documents_special_notice_gate(tmp_path):
    from federal_ja.collect.sources import JA_TITLE_PATTERN
    s = Store(str(tmp_path))
    s.notices["a"] = {"notice_id": "a", "type": "Justification", "collection_reason": "justification_type",
                      "posted_date": "2025-03-01"}
    s.notices["b"] = {"notice_id": "b", "type": "Special Notice",
                      "collection_reason": "special_notice_title_match", "posted_date": "2025-03-02"}
    card = render_card(s, "2025-01-20", "2026-10-18T00:00:00")
    assert JA_TITLE_PATTERN.pattern in card            # the exact gate, not a paraphrase
    assert "`special_notice_title_match`" in card and "**candidates**" in card
    assert "{'justification_type': 1, 'special_notice_title_match': 1}" in card


def test_collection_notes_show_the_same_gate():
    # 2026-11-28: the gate moved with the collection notes from README.md to docs/collection.md (the README no
    # longer pastes patterns; the dataset card prints the live one)
    from pathlib import Path
    from federal_ja.collect.sources import JA_TITLE_PATTERN
    notes = (Path(__file__).resolve().parents[1] / "docs" / "collection.md").read_text()
    assert JA_TITLE_PATTERN.pattern in notes


def test_card_documents_sam_award_columns(tmp_path):
    from federal_ja.collect import sam_award
    s = Store(str(tmp_path))
    s.notices["a"] = {"notice_id": "a", "posted_date": "2025-03-01", "sam_award_status": "ok"}
    s.notices["b"] = {"notice_id": "b", "posted_date": "2025-03-01"}
    card = render_card(s, "2025-01-20", "2026-10-23T00:00:00")
    for c in sam_award.COLUMNS:
        assert f"`{c}`" in card, c
    assert "deliveryOrderNumber" in card
    assert "{'ok': 1, 'unfetched': 1}" in card


def test_card_has_no_dated_backfill_promise():
    # 2026-11-15 audit: the card still said the extract6 backfill was "expected
    # within a few days of 2026-10-21"; on HF 12,351 of 12,352 extracted rows
    # were extract6.
    assert "within a few days" not in EXPERIMENTAL_SECTION
    assert "@@" not in EXPERIMENTAL_SECTION


def test_card_lists_every_justification_authority_value(tmp_path):
    # 2026-11-15 audit: a fixed "seen:" list left out far2 (927 notices), 1 (505) and others.
    s = Store(str(tmp_path))
    for i, v in enumerate(["far1", "far1", "far2", "1", None]):
        s.notices[str(i)] = {"notice_id": str(i), "posted_date": "2025-03-01", "sam_justification_authority": v}
    card = render_card(s, "2025-01-20", "2026-11-15T00:00:00")
    assert "`far1` (2), `1` (1), `far2` (1)" in card


def test_card_gets_data_dictionary_of_published_tables(tmp_path):
    """render_card leaves a marker; add_dictionary fills it from the published data/ tables and stops
    if their columns no longer match docs/tables.toml."""
    import pandas as pd

    from federal_ja.collect import data_docs
    from federal_ja.collect.card import DICTIONARY_MARK, add_dictionary

    card = render_card(Store(str(tmp_path / "w")), "2025-01-20", "2026-11-16T00:00:00")
    assert DICTIONARY_MARK in card
    pub = tmp_path / "publish"
    (pub / "data").mkdir(parents=True)
    tables = {n: t for n, t in data_docs.load_config()["tables"].items() if t["file"].startswith("data/")}
    for t in tables.values():
        pd.DataFrame({c: pd.Series([], dtype="string") for c in t["columns"]}).to_parquet(pub / t["file"])
    out = add_dictionary(card, str(pub))
    assert DICTIONARY_MARK not in out and "## Data dictionary" in out
    assert "| `notices` | `data/notices.parquet` |" in out and "analysis/extractions.parquet" not in out.split("## Data dictionary")[1].split("## License")[0].split("described in")[0]
    first = next(iter(tables.values()))
    pd.DataFrame({"x": pd.Series([], dtype="string")}).to_parquet(pub / first["file"])
    with pytest.raises(SystemExit, match="SCHEMA DRIFT"):
        add_dictionary(card, str(pub))
