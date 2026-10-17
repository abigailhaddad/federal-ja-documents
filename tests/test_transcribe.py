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
    assert "pages 11 through 20 of a 25-page" in tr.page_prompt(list(range(11, 21)), 25)
    assert "pages 2, 5 and 7 of a 9-page" in tr.page_prompt([2, 5, 7], 9)
    assert "page 4 of a 4-page" in tr.page_prompt([4], 4)


def test_split_and_merge():
    assert tr.split_by_markers("--- page 2 ---\nA\n--- page 5 ---\nB") == {2: "A", 5: "B"}
    assert tr.split_by_markers("junk\n--- page 1 ---\nA") == {0: "junk", 1: "A"}
    merged = tr.merge_page_texts(["n1", "", "n3"], {2: "T2"})
    assert merged == "--- page 1 ---\nn1\n\n--- page 2 ---\nT2\n\n--- page 3 ---\nn3"


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
    msgs = FakeMessages([("end_turn", "--- page 1 ---\nA\n--- page 2 ---\nA2"),
                         ("end_turn", "--- page 3 ---\nB\n--- page 4 ---\nB2"),
                         ("end_turn", "--- page 5 ---\nC")])
    t = tr.Transcriber(client=SimpleNamespace(messages=msgs), pages_per_chunk=2)
    res = t.transcribe_pdf(str(p), 5)
    assert res.requests == 3 and res.input_tokens == 3000 and res.output_tokens == 300
    assert res.cost_usd == pytest.approx(3 * tr.cost_usd(1000, 100))
    assert res.page_texts == {1: "A", 2: "A2", 3: "B", 4: "B2", 5: "C"}
    kw = msgs.calls[0]
    assert kw["model"] == "claude-haiku-5-5"
    assert "temperature" not in kw
    assert kw["messages"][0]["content"][0]["type"] == "document"
    assert "pages 1 through 2 of a 5-page" in kw["messages"][0]["content"][1]["text"]
    assert "page 5 of a 5-page" in msgs.calls[2]["messages"][0]["content"][1]["text"]


def test_only_requested_pages_are_sent(tmp_path):
    from pypdf import PdfReader
    import base64, io
    p = tmp_path / "s.pdf"
    p.write_bytes(blank_pdf(6))
    msgs = FakeMessages([("end_turn", "--- page 2 ---\nX\n--- page 5 ---\nY")])
    t = tr.Transcriber(client=SimpleNamespace(messages=msgs), pages_per_chunk=10)
    res = t.transcribe_pdf(str(p), 6, pages=[2, 5])
    assert res.page_texts == {2: "X", 5: "Y"} and res.pages == 2
    sent = base64.b64decode(msgs.calls[0]["messages"][0]["content"][0]["source"]["data"])
    assert len(PdfReader(io.BytesIO(sent)).pages) == 2


def test_marker_mismatch_falls_back_to_per_page(tmp_path):
    p = tmp_path / "s.pdf"
    p.write_bytes(blank_pdf(2))
    msgs = FakeMessages([("end_turn", "--- page 1 ---\nboth pages run together"),
                         ("end_turn", "--- page 1 ---\nfirst"), ("end_turn", "second, no marker")])
    t = tr.Transcriber(client=SimpleNamespace(messages=msgs), pages_per_chunk=10)
    res = t.transcribe_pdf(str(p), 2)
    assert res.page_texts == {1: "first", 2: "second, no marker"} and res.requests == 3


def test_max_tokens_splits_chunk(tmp_path):
    p = tmp_path / "s.pdf"
    p.write_bytes(blank_pdf(2))
    msgs = FakeMessages([("max_tokens", "partial"), ("end_turn", "p1"), ("end_turn", "p2")])
    t = tr.Transcriber(client=SimpleNamespace(messages=msgs), pages_per_chunk=2)
    res = t.transcribe_pdf(str(p), 2)
    assert res.page_texts == {1: "p1", 2: "p2"} and res.requests == 3


def test_refusal_records_error(tmp_path):
    p = tmp_path / "s.pdf"
    p.write_bytes(blank_pdf(1))
    msgs = FakeMessages([("refusal", "")])
    t = tr.Transcriber(client=SimpleNamespace(messages=msgs), pages_per_chunk=10)
    res = t.transcribe_pdf(str(p), 1)
    assert res.error == "refusal" and res.page_texts == {}


def test_budget_stops_before_call(tmp_path):
    p = tmp_path / "s.pdf"
    p.write_bytes(blank_pdf(1))
    msgs = FakeMessages([])
    t = tr.Transcriber(client=SimpleNamespace(messages=msgs), budget=tr.BudgetTracker(0.0))
    with pytest.raises(tr.BudgetExceeded):
        t.transcribe_pdf(str(p), 1)
    assert msgs.calls == []


def test_relative_page_numbers_are_mapped_back(tmp_path):
    p = tmp_path / "s.pdf"
    p.write_bytes(blank_pdf(5))
    msgs = FakeMessages([("end_turn", "--- page 1 ---\nX\n--- page 2 ---\nY")])
    t = tr.Transcriber(client=SimpleNamespace(messages=msgs), pages_per_chunk=10)
    res = t.transcribe_pdf(str(p), 5, pages=[4, 5])
    assert res.page_texts == {4: "X", 5: "Y"} and res.requests == 1


def test_zero_width_chars_removed(tmp_path):
    p = tmp_path / "s.pdf"
    p.write_bytes(blank_pdf(1))
    msgs = FakeMessages([("end_turn", "--- page 1 ---\nID 13966349​84")])
    t = tr.Transcriber(client=SimpleNamespace(messages=msgs), pages_per_chunk=10)
    assert t.transcribe_pdf(str(p), 1).page_texts == {1: "ID 1396634984"}
