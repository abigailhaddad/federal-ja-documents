"""Model calls. EXPERIMENTAL.

One function, ``call``, sends one prompt and returns (parsed JSON, usage). It
is the only network call in the analysis package; tests patch it.

Providers:
* Anthropic (any ``claude-*`` model; the default and the only validated one,
  ``claude-haiku-5-5``). Structured output is a single forced tool call with
  ``strict: true`` whose input_schema is the response schema. No temperature
  is sent: current Claude models reject non-default sampling parameters.
  Reads ANTHROPIC_API_KEY, and ANTHROPIC_WORKSPACE_ID if set.
* OpenAI (any other model name), via the ``openai`` package if installed,
  with strict Structured Outputs at temperature 0. NOT validated in this
  repo. Reads OPENAI_API_KEY.

Output varies between calls even with the same prompt, which is why every
document is reviewed twice (see tagger.py).
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

DEFAULT_MODEL = "claude-haiku-5-5"
VALIDATED_MODELS = frozenset({"claude-haiku-5-5"})

# USD per 1M tokens (input, output), list prices. Anthropic: prompts up to
# 100K tokens (documents here are capped at 60,000 characters).
PRICES_PER_M: dict[str, tuple[float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50),
}


class ModelError(RuntimeError):
    """A model call failed. The caller records nothing for the document, so a
    rerun retries it; a partial (one-pass) result is never stored as done."""


def provider_for(model: str) -> str:
    return "anthropic" if model.startswith("claude-") else "openai"


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    pr = PRICES_PER_M.get(model)
    return round(input_tokens / 1e6 * pr[0] + output_tokens / 1e6 * pr[1], 6) if pr else None


_RE_BAD_ESCAPE = re.compile(r'\\(?!["\\/bfnrtu])')


def _parse_json(raw: str) -> Any:
    try:
        return json.loads(raw, strict=False)
    except json.JSONDecodeError:
        return json.loads(_RE_BAD_ESCAPE.sub(r"\\\\", raw), strict=False)


def call(prompt: str, *, schema: dict, tool_name: str, model: str = DEFAULT_MODEL,
         timeout: int = 300, max_attempts: int = 5) -> tuple[dict, dict]:
    if provider_for(model) == "anthropic":
        return _call_anthropic(prompt, schema=schema, tool_name=tool_name, model=model,
                               timeout=timeout, max_attempts=max_attempts)
    return _call_openai(prompt, schema=schema, tool_name=tool_name, model=model,
                        timeout=timeout, max_attempts=max_attempts)


def _call_anthropic(prompt, *, schema, tool_name, model, timeout, max_attempts):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ModelError("ANTHROPIC_API_KEY not set")
    import anthropic

    ws = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=0,
                                 default_headers={"anthropic-workspace-id": ws} if ws else None)
    last = ""
    for attempt in range(max_attempts):
        try:
            resp = client.messages.create(
                model=model, max_tokens=8192,
                messages=[{"role": "user", "content": prompt}],
                tools=[{"name": tool_name, "description": "Record the result of reviewing this document.",
                        "input_schema": schema, "strict": True}],
                tool_choice={"type": "tool", "name": tool_name},
            )
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as exc:
            last = str(exc)[:200]
            time.sleep(10 * (attempt + 1))
            continue
        except anthropic.APIStatusError as exc:
            if exc.status_code == 529:      # overloaded
                last = str(exc)[:200]
                time.sleep(10 * (attempt + 1))
                continue
            raise ModelError(f"Anthropic call failed ({exc.status_code}): {str(exc)[:200]}") from exc
        if resp.stop_reason == "refusal":
            raise ModelError("model refused")
        if resp.stop_reason == "max_tokens":
            raise ModelError("incomplete response (max_tokens)")
        block = next((b for b in resp.content if b.type == "tool_use" and b.name == tool_name), None)
        if block is None:
            raise ModelError(f"no {tool_name} tool call (stop_reason={resp.stop_reason})")
        u = resp.usage
        inp = ((u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0)
               + (getattr(u, "cache_read_input_tokens", 0) or 0))
        usage = {"model": resp.model or model, "input_tokens": inp, "output_tokens": u.output_tokens or 0}
        usage["cost_usd"] = cost_usd(model, inp, usage["output_tokens"])
        parsed = block.input if isinstance(block.input, dict) else _parse_json(str(block.input))
        return parsed, usage
    raise ModelError(f"Anthropic call failed after {max_attempts} attempts: {last}")


def _call_openai(prompt, *, schema, tool_name, model, timeout, max_attempts):
    if not os.environ.get("OPENAI_API_KEY"):
        raise ModelError("OPENAI_API_KEY not set")
    try:
        import openai
    except ImportError as exc:  # optional dependency
        raise ModelError("the openai package is not installed") from exc
    client = openai.OpenAI(timeout=timeout, max_retries=0)
    last = ""
    for attempt in range(max_attempts):
        try:
            resp = client.chat.completions.create(
                model=model, temperature=0,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_schema",
                                 "json_schema": {"name": tool_name, "strict": True, "schema": schema}},
            )
        except (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError) as exc:
            if "insufficient_quota" in str(exc):
                raise ModelError(f"OpenAI account has no credits: {str(exc)[:200]}") from exc
            last = str(exc)[:200]
            time.sleep(10 * (attempt + 1))
            continue
        except openai.APIError as exc:
            raise ModelError(f"OpenAI call failed: {str(exc)[:200]}") from exc
        choice = resp.choices[0]
        if getattr(choice.message, "refusal", None):
            raise ModelError("model refused")
        if choice.finish_reason != "stop":
            raise ModelError(f"incomplete response (finish_reason={choice.finish_reason})")
        u = resp.usage
        usage = {"model": resp.model or model, "input_tokens": u.prompt_tokens if u else 0,
                 "output_tokens": u.completion_tokens if u else 0}
        usage["cost_usd"] = cost_usd(model, usage["input_tokens"], usage["output_tokens"])
        try:
            return _parse_json(choice.message.content or ""), usage
        except ValueError as exc:
            raise ModelError(f"unparseable model JSON: {exc}") from exc
    raise ModelError(f"OpenAI call failed after {max_attempts} attempts: {last}")
