import re
from federal_ja.digest import buttondown


def test_recent_digests_sends_a_plain_date(monkeypatch):
    seen = {}

    class R:
        status_code = 200
        def json(self):
            return {"results": [], "next": None}

    def fake(method, url, headers=None, params=None, **k):
        seen.update(params or {})
        return R()

    monkeypatch.setattr(buttondown, "_request", fake)
    monkeypatch.setattr(buttondown, "_headers", lambda: {})
    buttondown.recent_digests()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", seen["creation_date__start"])
