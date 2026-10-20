"""Prompt caching on the analysis model calls: the two-part prompt, the request
shape sent to each provider, and cache-aware usage/cost. No network."""

import sys
from types import ModuleType, SimpleNamespace

import anthropic
import pytest

from federal_ja.analysis import data, extract, llm, prompts
from federal_ja.collect import transcribe as tr
from tests.test_field_extraction import NID, answer, fake, tables

META_A = {"title": "Widget support", "agency": "DEPT OF EXAMPLES", "posted_date": "2026-03-01"}
META_B = {"title": "Other <<EXCERPTS>> title", "agency": "DEPT OF OTHER", "posted_date": "2025-01-02"}


@pytest.fixture
def doc():
    return data.build_documents(*tables())[NID]


# ── the split is not a prompt change ───────────────────────────────────────

def test_extraction_parts_join_to_the_old_prompt(doc):
    text, _ = extract.model_text(doc)
    parts = extract.build_prompt(doc, text)
    assert isinstance(parts, tuple) and len(parts) == 2
    # the prompt exactly as built before caching
    old = extract.PROMPT.replace("<<METADATA>>", extract._metadata(doc)).replace("<<DOCUMENT>>", text)
    assert llm.prompt_text(parts) == old
    assert parts[0] + parts[1] == old
    # the split falls at the start of the per-notice section
    assert parts[1].startswith("NOTICE METADATA (from SAM.gov")
    assert parts[0].endswith("null if nothing.\n\n")
    assert "<<" not in parts[0] and "Acme Widget" not in parts[0]


def test_extraction_cached_part_is_the_same_for_every_document(doc):
    other = data.build_documents(*tables(pages=[
        {"notice_id": NID, "resource_id": "r1", "page_number": 1, "text": "Something else entirely.",
         "text_source": "native"}]))[NID]
    a = extract.build_prompt(doc, extract.model_text(doc)[0])
    b = extract.build_prompt(other, extract.model_text(other)[0])
    assert a[0] == b[0] == extract.PROMPT_CACHED
    assert a[1] != b[1]


@pytest.mark.parametrize("cat", list(prompts.CONFIRM_TEMPLATES))
def test_confirm_parts_join_to_the_old_prompt(cat):
    ex = ["first excerpt <<METADATA>> stays", "second excerpt"]
    for meta in (META_A, META_B):
        parts = prompts.confirm_prompt(cat, meta, ex)
        old = (prompts.CONFIRM_TEMPLATES[cat].replace(prompts.METADATA_TOKEN, prompts.metadata_line(meta))
               .replace(prompts.EXCERPTS_TOKEN, "\n\n[...]\n\n".join(ex)))
        assert llm.prompt_text(parts) == old
        assert parts[1].startswith("SAM.gov notice metadata")
    # the cached part does not depend on the document
    assert prompts.confirm_prompt(cat, META_A, ["x"])[0] == prompts.confirm_prompt(cat, META_B, ["y z"])[0]


# ── request shape ──────────────────────────────────────────────────────────

class FakeAnthropic:
    sent: list[dict] = []
    usage = SimpleNamespace(input_tokens=1200, output_tokens=300,
                            cache_creation_input_tokens=0, cache_read_input_tokens=1800)

    def __init__(self, **kw):
        self.messages = self

    def create(self, **kw):
        FakeAnthropic.sent.append(kw)
        return SimpleNamespace(stop_reason="tool_use", model=kw["model"], usage=FakeAnthropic.usage,
                               content=[SimpleNamespace(type="tool_use", name=kw["tools"][0]["name"],
                                                        input={"answer": True})])


@pytest.fixture
def fake_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    FakeAnthropic.sent = []
    return FakeAnthropic


def test_anthropic_request_has_two_blocks_with_cache_on_the_first(fake_anthropic, doc):
    parts = extract.build_prompt(doc, extract.model_text(doc)[0])
    parsed, usage = llm.call(parts, schema=extract.SCHEMA, tool_name=extract.TOOL_NAME,
                             model="claude-haiku-5-5", effort="high")
    kw = fake_anthropic.sent[0]
    (msg,) = kw["messages"]
    assert msg["role"] == "user"
    first, second = msg["content"]
    assert first == {"type": "text", "text": parts[0], "cache_control": {"type": "ephemeral"}}
    assert second == {"type": "text", "text": parts[1]}
    assert first["text"] + second["text"] == llm.prompt_text(parts)
    # everything else is as before
    assert kw["tools"] == [{"name": extract.TOOL_NAME, "description": "Record the result of reviewing this document.",
                            "input_schema": extract.SCHEMA, "strict": True}]
    assert kw["tool_choice"] == {"type": "auto"} and kw["output_config"] == {"effort": "high"}
    assert "system" not in kw and "temperature" not in kw


def test_anthropic_request_plain_string_unchanged(fake_anthropic):
    llm.call("just a string", schema=prompts.CONFIRM_SCHEMA, tool_name="t", model="claude-haiku-5-5")
    assert fake_anthropic.sent[0]["messages"] == [{"role": "user", "content": "just a string"}]
    assert fake_anthropic.sent[0]["tool_choice"] == {"type": "tool", "name": "t"}


def test_confirm_request_cached(fake_anthropic):
    parts = prompts.confirm_prompt("sources_rejected", META_A, ["two firms responded"])
    llm.call(parts, schema=prompts.CONFIRM_SCHEMA_N_FIRMS, tool_name="ja_confirm2", model="claude-haiku-5-5")
    content = fake_anthropic.sent[0]["messages"][0]["content"]
    assert content[0]["cache_control"] == {"type": "ephemeral"} and "cache_control" not in content[1]
    assert content[0]["text"] + content[1]["text"] == llm.prompt_text(parts)


def test_empty_part_rejected():
    with pytest.raises(ValueError):
        llm.anthropic_content(("", "rest"))
    with pytest.raises(ValueError):
        llm.anthropic_content(("cached", ""))


def test_openai_gets_the_joined_string(monkeypatch, doc):
    sent = []

    class Err(Exception):
        pass

    class Client:
        def __init__(self, **kw):
            self.chat = SimpleNamespace(completions=self)

        def create(self, **kw):
            sent.append(kw)
            return SimpleNamespace(model=kw["model"], usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
                                   choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(
                                       refusal=None, content='{"answer": true}'))])

    mod = ModuleType("openai")
    mod.OpenAI = Client
    mod.RateLimitError = mod.APIConnectionError = mod.InternalServerError = mod.APIError = Err
    monkeypatch.setitem(sys.modules, "openai", mod)
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    parts = extract.build_prompt(doc, extract.model_text(doc)[0])
    parsed, usage = llm.call(parts, schema=extract.SCHEMA, tool_name=extract.TOOL_NAME, model="gpt-x")
    assert parsed == {"answer": True}
    assert sent[0]["messages"] == [{"role": "user", "content": parts[0] + parts[1]}]
    assert usage == {"model": "gpt-x", "input_tokens": 10, "output_tokens": 5, "cost_usd": None}


# ── usage and cost ─────────────────────────────────────────────────────────

def test_usage_records_cache_tokens_separately(fake_anthropic):
    _, u = llm.call(("cached", "rest"), schema=prompts.CONFIRM_SCHEMA, tool_name="t", model="claude-haiku-5-5")
    assert u["input_tokens"] == 1200 + 1800          # total, as before caching
    assert u["cache_creation_input_tokens"] == 0 and u["cache_read_input_tokens"] == 1800
    assert u["cost_usd"] == pytest.approx((1200 + 1800 * 0.1) / 1e6 * 0.10 + 300 / 1e6 * 0.50, abs=1e-9)
    fake_anthropic.usage = SimpleNamespace(input_tokens=1200, output_tokens=300,
                                           cache_creation_input_tokens=1800, cache_read_input_tokens=0)
    try:
        _, u = llm.call(("cached", "rest"), schema=prompts.CONFIRM_SCHEMA, tool_name="t", model="claude-haiku-5-5")
    finally:
        fake_anthropic.usage = FakeAnthropic.usage
    assert u["input_tokens"] == 3000 and u["cache_creation_input_tokens"] == 1800
    assert u["cost_usd"] == pytest.approx((1200 + 1800 * 1.25) / 1e6 * 0.10 + 300 / 1e6 * 0.50, abs=1e-9)


def test_cost_usd_cache_prices():
    # no cache tokens: unchanged
    assert llm.cost_usd("claude-haiku-5-5", 100_000, 0) == 0.01
    assert llm.cost_usd("claude-haiku-5-5", 100_000, 0, cache_creation_tokens=100_000) == pytest.approx(0.0125)
    assert llm.cost_usd("claude-haiku-5-5", 100_000, 0, cache_read_tokens=100_000) == pytest.approx(0.001)
    assert llm.cost_usd("claude-haiku-5-5", 100_000, 0, cache_creation_tokens=2_000,
                        cache_read_tokens=8_000) == pytest.approx((90_000 + 2_500 + 800) / 1e6 * 0.10)
    # the long-prompt tier is chosen on the whole prompt, cache included
    assert llm.cost_usd("claude-haiku-5-5", 200_000, 0, cache_read_tokens=150_000) == pytest.approx(
        (50_000 + 15_000) / 1e6 * 0.50)
    assert llm.cost_usd("unknown-model", 10, 10, cache_read_tokens=5) is None


def test_extract_document_sums_cache_tokens(doc):
    call = fake(answer())

    def with_cache(prompt, **kw):
        a, u = call(prompt, **kw)
        return a, {**u, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 900}
    rec = extract.extract_document(doc, call_fn=with_cache, retries=0)
    assert rec["usage"]["cache_read_input_tokens"] == 900
    assert rec["usage"]["cache_creation_input_tokens"] == 0
    assert rec["usage"]["input_tokens"] == 1000


def test_transcribe_cost_cache_prices():
    assert tr.cost_usd(1000, 100) == pytest.approx(1000 / 1e6 * 0.10 + 100 / 1e6 * 0.50)
    assert tr.cost_usd(1000, 0, cache_read_tokens=1000) == pytest.approx(1000 * 0.1 / 1e6 * 0.10)
    assert tr.cost_usd(1000, 0, cache_creation_tokens=1000) == pytest.approx(1000 * 1.25 / 1e6 * 0.10)
