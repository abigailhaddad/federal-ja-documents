"""Field extraction through the Message Batches API (llm.batch_call,
extract.extract_documents_batch, extract --batch). Fake client, no network."""

import json
from types import SimpleNamespace

import anthropic
import httpx2
import pandas as pd
import pytest

from federal_ja.analysis import cli, data, extract, llm
from tests.test_field_extraction import NID, answer, tables

USAGE = SimpleNamespace(input_tokens=5000, output_tokens=8000,
                        cache_creation_input_tokens=0, cache_read_input_tokens=7900)


def message(tool_input, stop_reason="tool_use"):
    return SimpleNamespace(stop_reason=stop_reason, model="claude-haiku-5-5", usage=USAGE,
                           content=[SimpleNamespace(type="tool_use", name=extract.TOOL_NAME, input=tool_input)])


def succeeded(cid, tool_input, **kw):
    return SimpleNamespace(custom_id=cid, result=SimpleNamespace(type="succeeded", message=message(tool_input, **kw)))


class FakeBatches:
    """client.messages.batches: answers each request with ``answer_for(cid)``
    (a result object), after ``polls`` in-progress retrievals."""

    def __init__(self, answer_for, polls=1, create_error=None):
        self.answer_for, self.polls, self.create_error = answer_for, polls, create_error
        self.created, self.canceled, self.retrieved = [], [], 0

    def create(self, requests):
        if self.create_error:
            raise self.create_error
        self.created.append(requests)
        return SimpleNamespace(id=f"msgbatch_{len(self.created)}", processing_status="in_progress")

    def retrieve(self, bid):
        self.retrieved += 1
        done = self.retrieved >= self.polls or bool(self.canceled)
        return SimpleNamespace(id=bid, processing_status="ended" if done else "in_progress")

    def cancel(self, bid):
        self.canceled.append(bid)

    def results(self, bid):
        reqs = self.created[int(bid.split("_")[1]) - 1]
        return [r for r in (self.answer_for(q["custom_id"]) for q in reqs) if r is not None]


def client(batches):
    return SimpleNamespace(messages=SimpleNamespace(batches=batches))


def credit_error():
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages/batches")
    resp = httpx2.Response(400, request=req, json={"type": "error", "error": {
        "type": "invalid_request_error", "message": "Your credit balance is too low"}})
    return anthropic.BadRequestError("Your credit balance is too low", response=resp, body=None)


@pytest.fixture
def doc():
    return data.build_documents(*tables())[NID]


# ── llm.batch_call ─────────────────────────────────────────────────────────

def test_batch_request_is_the_direct_request(doc):
    parts = extract.build_prompt(doc, extract.model_text(doc)[0])
    b = FakeBatches(lambda cid: succeeded(cid, answer()), polls=3)
    out = llm.batch_call({NID: parts}, schema=extract.SCHEMA, tool_name=extract.TOOL_NAME,
                         model="claude-haiku-5-5", max_tokens=extract.MAX_OUTPUT_TOKENS, effort="high",
                         client=client(b), sleep=lambda s: None)
    ((req,),) = b.created
    assert req["custom_id"] == NID
    assert req["params"] == llm.anthropic_params(parts, schema=extract.SCHEMA, tool_name=extract.TOOL_NAME,
                                                 model="claude-haiku-5-5", max_tokens=extract.MAX_OUTPUT_TOKENS,
                                                 effort="high")
    assert req["params"]["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert b.retrieved == 3 and not b.canceled
    parsed, usage = out[NID]
    assert parsed == answer()
    assert usage["price_mult"] == 0.5
    full = llm.cost_usd("claude-haiku-5-5", 12900, 8000, cache_read_tokens=7900)
    assert usage["cost_usd"] == pytest.approx(full / 2, abs=1e-6)


def test_batch_half_price():
    assert llm.cost_usd("claude-haiku-5-5", 10_000, 8_000, price_mult=0.5) == pytest.approx(
        llm.cost_usd("claude-haiku-5-5", 10_000, 8_000) / 2)


def test_batch_errors_per_request():
    def answer_for(cid):
        if cid == "ok":
            return succeeded(cid, {"x": 1})
        if cid == "cut":
            return succeeded(cid, {"x": 1}, stop_reason="max_tokens")
        if cid == "err":
            err = SimpleNamespace(type="error", error=SimpleNamespace(type="overloaded_error", message="busy"))
            return SimpleNamespace(custom_id=cid, result=SimpleNamespace(type="errored", error=err))
        if cid == "exp":
            return SimpleNamespace(custom_id=cid, result=SimpleNamespace(type="expired"))
        return None     # "gone": no result line at all
    out = llm.batch_call({c: "p" for c in ("ok", "cut", "err", "exp", "gone")}, schema={}, tool_name=extract.TOOL_NAME,
                         model="claude-haiku-5-5", client=client(FakeBatches(answer_for)), sleep=lambda s: None)
    assert out["ok"][0] == {"x": 1}
    assert "max_tokens" in str(out["cut"]) and "overloaded_error" in str(out["err"])
    assert "expired" in str(out["exp"]) and "no result" in str(out["gone"])
    assert all(isinstance(out[c], llm.ModelError) for c in ("cut", "err", "exp", "gone"))


def test_batch_canceled_after_max_wait():
    t = [0.0]
    b = FakeBatches(lambda cid: succeeded(cid, {"x": 1}) if cid == "a" else None, polls=10**6)

    def sleep(s):
        t[0] += s
    out = llm.batch_call({"a": "p", "b": "p"}, schema={}, tool_name=extract.TOOL_NAME, model="claude-haiku-5-5",
                         max_wait_s=300, poll_s=60, client=client(b), sleep=sleep, clock=lambda: t[0])
    assert b.canceled == ["msgbatch_1"]
    assert out["a"][0] == {"x": 1} and isinstance(out["b"], llm.ModelError)


def test_batch_not_created_is_a_model_error():
    b = FakeBatches(lambda cid: None, create_error=credit_error())
    with pytest.raises(llm.ModelError, match="credit balance"):
        llm.batch_call({"a": "p"}, schema={}, tool_name="t", model="claude-haiku-5-5", client=client(b))


# ── extract.extract_documents_batch ────────────────────────────────────────

def fake_batch(*rounds):
    """batch_fn answering round i with rounds[i] (a dict nid -> answer, or an
    exception to raise / to return for every request)."""
    sent = []

    def batch_fn(prompts, *, schema, tool_name, model, max_tokens, max_wait_s, **kw):
        sent.append(sorted(prompts))
        r = rounds[min(len(sent) - 1, len(rounds) - 1)]
        if isinstance(r, Exception):
            raise r
        u = {"input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.0001, "price_mult": 0.5}
        return {nid: (r[nid] if isinstance(r[nid], Exception) else (r[nid], u)) for nid in prompts if nid in r}
    batch_fn.sent = sent
    return batch_fn


def test_batch_same_record_as_direct(doc):
    from tests.test_field_extraction import fake
    direct = extract.extract_document(doc, call_fn=fake(answer()), effort="high")
    out = extract.extract_documents_batch([doc], batch_fn=fake_batch({NID: answer()}), effort="high")
    rec = out[NID]
    for k in ("fields", "dropped", "raw", "attempt_kept", "chars_sent", "truncated", "extract_version", "effort"):
        assert rec[k] == direct[k], k
    assert rec["usage"]["calls"] == 1 and rec["usage"]["price_mult"] == 0.5


def test_batch_retry_round_only_for_dropped_values(doc):
    other = data.build_documents(*tables())[NID]
    other.notice_id = "c" * 32
    bad = answer(estimated_value_usd=2_000_000)
    fn = fake_batch({NID: bad, other.notice_id: answer()}, {NID: answer()})
    out = extract.extract_documents_batch([doc, other], batch_fn=fn, retries=1)
    assert fn.sent == [sorted([NID, other.notice_id]), [NID]]
    assert out[NID]["attempt_kept"] == 2 and out[NID]["usage"]["calls"] == 2
    assert out[NID]["fields"]["estimated_value_usd"] == 1_250_000
    assert out[other.notice_id]["attempt_kept"] == 1


def test_batch_failed_retry_keeps_first_answer(doc):
    bad = answer(estimated_value_usd=2_000_000)
    fn = fake_batch({NID: bad}, {NID: llm.ModelError("expired")})
    rec = extract.extract_documents_batch([doc], batch_fn=fn, retries=1)[NID]
    assert rec["attempt_kept"] == 1 and rec["fields"]["estimated_value_usd"] is None


def test_batch_first_round_failures(doc):
    out = extract.extract_documents_batch([doc], batch_fn=fake_batch({NID: llm.ModelError("errored")}))
    assert isinstance(out[NID], llm.ModelError)
    out = extract.extract_documents_batch([doc], batch_fn=fake_batch({NID: ["not", "an", "object"]}))
    assert isinstance(out[NID], llm.ModelError)
    with pytest.raises(llm.ModelError):
        extract.extract_documents_batch([doc], batch_fn=fake_batch(llm.ModelError("no credit")))


def test_batch_retry_skipped_near_the_deadline(doc):
    t = [0.0]
    fn = fake_batch({NID: answer(estimated_value_usd=2_000_000)}, {NID: answer()})

    def slow(prompts, **kw):
        t[0] += 3000
        return fn(prompts, **kw)
    rec = extract.extract_documents_batch([doc], batch_fn=slow, retries=1, max_wait_s=3300,
                                          clock=lambda: t[0])[NID]
    assert len(fn.sent) == 1 and rec["attempt_kept"] == 1


def test_estimate_is_conservative_for_a_typical_notice(doc):
    # validation median was about $0.0045 a notice at full price
    assert 0.002 < extract.estimate_batch_cost(doc) < 0.01


# ── extract --batch ────────────────────────────────────────────────────────

def write_tables(tmp_path, n_notices):
    n, a, p = tables()
    ids = [f"{i:032x}" for i in range(n_notices)]
    n = pd.concat([n.assign(notice_id=i) for i in ids], ignore_index=True)
    a = pd.concat([a.assign(notice_id=i) for i in ids], ignore_index=True)
    p = pd.concat([p.assign(notice_id=i) for i in ids], ignore_index=True)
    dd = tmp_path / "tables"
    dd.mkdir()
    for name, df in zip(data.TABLES, (n, a, p)):
        df.to_parquet(dd / f"{name}.parquet")
    (dd / "revision.json").write_text(json.dumps({"sha": "abc"}))
    return dd, ids


def test_cli_batch(tmp_path, monkeypatch):
    dd, ids = write_tables(tmp_path, 5)
    sent = []

    def batch_call(prompts, **kw):
        sent.append(sorted(prompts))
        u = {"input_tokens": 1000, "output_tokens": 200, "cost_usd": 0.0001, "price_mult": 0.5}
        return {nid: (llm.ModelError("expired") if nid == ids[0] else (answer(), u)) for nid in prompts}
    monkeypatch.setattr(llm, "batch_call", batch_call)
    one = extract.estimate_batch_cost(data.build_documents(*tables())[NID])
    out = tmp_path / "out"
    args = ["extract", "--batch", "--out", str(out), "--data-dir", str(dd), "--max-usd", str(one * 3.5)]
    assert cli.main(args) == 1                       # 3 of 5 fit under the cap; one of them failed
    assert len(sent) == 1 and len(sent[0]) == 3
    recs = [json.loads(ln) for ln in (out / "extractions.jsonl").read_text().splitlines()]
    assert len(recs) == 2 and all(r["dataset_revision"] == "abc" for r in recs)
    assert (out / "failures.jsonl").read_text().count("expired") == 1
    assert cli.main(args) == 1                       # next run: the failed one and the two not sent
    assert len(sent[1]) == 3 and ids[0] in sent[1]
    assert not set(sent[1]) & {r["notice_id"] for r in recs}


def test_cli_batch_not_created_exits_2(tmp_path, monkeypatch):
    dd, ids = write_tables(tmp_path, 2)

    def batch_call(prompts, **kw):
        raise llm.ModelError("batch not created (400): Your credit balance is too low")
    monkeypatch.setattr(llm, "batch_call", batch_call)
    out = tmp_path / "out"
    assert cli.main(["extract", "--batch", "--out", str(out), "--data-dir", str(dd), "--max-usd", "5"]) == 2
    assert (out / "failures.jsonl").read_text().count("credit balance") == 2
    assert not (out / "extractions.jsonl").exists() or not (out / "extractions.jsonl").read_text()


def test_cli_batch_zero_cap_sends_nothing(tmp_path, monkeypatch):
    dd, ids = write_tables(tmp_path, 2)

    def batch_call(prompts, **kw):
        raise AssertionError("no batch should be sent")
    monkeypatch.setattr(llm, "batch_call", batch_call)
    out = tmp_path / "out"
    assert cli.main(["extract", "--batch", "--out", str(out), "--data-dir", str(dd), "--max-usd", "0"]) == 0
