"""Shared test setup."""
import pytest


@pytest.fixture(autouse=True)
def _no_real_topic_lookup(monkeypatch):
    # buttondown.send_email looks up the "J&A digest" tag id over the network; tests never do.
    from federal_ja.digest import buttondown
    monkeypatch.setattr(buttondown, "_topic_tag_id", lambda topic: "sub_tag_test_ja")
