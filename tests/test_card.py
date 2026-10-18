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
    for must in ["claude-haiku-5-5", "11a221ab5ae9583a95ae645883de75237a46d44d", "not refreshed daily",
                 "not 100% right", "workforce_cause", "agency_delay_cause", "sources_rejected",
                 "estimated_value_usd", "prompts.py", "extract.py"]:
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
