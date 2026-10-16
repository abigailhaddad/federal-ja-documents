from types import SimpleNamespace

import pytest

from federal_ja.collect import transcribe as tr
from tests.test_extract import blank_pdf


def test_cost_tiers():
    assert tr.cost_usd(100_000, 0) == pytest.approx(0.01)
    assert tr.cost_usd(0, 1_000_000) == pytest.approx(0.50)
    assert tr.cost_usd(100_001, 0) == pytest.approx(100_001 / 1e6 * 0.50)


def test_budget():
    b = tr.BudgetTracker(1.0)
    b.add(0.6)
    b.check()
    b.add(0.5)
    with pytest.raises(tr.BudgetExceeded):
        b.check()


def test_prompt_rules_present():
    for s in ("[REDACTED]", "[illegible]", "Never guess", "--- page N ---"):
        assert s in tr.SYSTEM_PROMPT
    assert "pages 11 through 20" in tr.page_prompt(11, 20, 25)


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def stream(self, **kw):
        self.calls.append(kw)
        stop, text = self.script.pop(0)
        return FakeStream(SimpleNamespace(
            stop_reason=stop, content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=100,
                                  cache_creation_input_tokens=0, cache_read_input_tokens=0)))


def test_chunks_and_accounting(tmp_path):
    p = tmp_path / "s.pdf"
    p.write_bytes(blank_pdf(5))
    msgs = FakeMessages([("end_turn", "--- page 1 ---\nA"), ("end_turn", "--- page 3 ---\nB"),
                         ("end_turn", "--- page 5 ---\nC")])
    t = tr.Transcriber(client=SimpleNamespace(messages=msgs), pages_per_chunk=2)
    res = t.transcribe_pdf(str(p), 5)
    assert res.requests == 3 and res.input_tokens == 3000 and res.output_tokens == 300
    assert res.cost_usd == pytest.approx(3 * tr.cost_usd(1000, 100))
    assert res.text.index("A") < res.text.index("B") < res.text.index("C")
    kw = msgs.calls[0]
    assert kw["model"] == "claude-haiku-5-5"
    assert "temperature" not in kw
    assert kw["messages"][0]["content"][0]["type"] == "document"
    assert "pages 1 through 2" in kw["messages"][0]["content"][1]["text"]
    assert "pages 5 through 5" in msgs.calls[2]["messages"][0]["content"][1]["text"]


def test_max_tokens_splits_chunk(tmp_path):
    p = tmp_path / "s.pdf"
    p.write_bytes(blank_pdf(2))
    msgs = FakeMessages([("max_tokens", "partial"), ("end_turn", "p1"), ("end_turn", "p2")])
    t = tr.Transcriber(client=SimpleNamespace(messages=msgs), pages_per_chunk=2)
    res = t.transcribe_pdf(str(p), 2)
    assert res.text == "p1\n\np2" and res.requests == 3


def test_budget_stops_before_call(tmp_path):
    p = tmp_path / "s.pdf"
    p.write_bytes(blank_pdf(1))
    msgs = FakeMessages([])
    t = tr.Transcriber(client=SimpleNamespace(messages=msgs), budget=tr.BudgetTracker(0.0))
    with pytest.raises(tr.BudgetExceeded):
        t.transcribe_pdf(str(p), 1)
    assert msgs.calls == []
