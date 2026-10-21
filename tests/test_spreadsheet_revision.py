from federal_ja.analysis.spreadsheet import data_revision


def test_one_commit_uses_that_commit():
    assert data_revision({"11a221ab5ae9"}, None) == "11a221ab5ae9"


def test_mixed_commits_use_the_ai_tables_own_commit():
    # after the first daily extraction publish: new extraction rows at newer
    # commits, theme rows still at the 2026-10-18 snapshot commit
    assert data_revision({"11a221ab5ae9", "e93882dcc18c"}, None) == "main"
    assert data_revision({"11a221ab5ae9", "e93882dcc18c"}, "abc123") == "abc123"
