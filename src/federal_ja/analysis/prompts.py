"""Theme prompts (loaded from prompts.yaml) and response schemas. EXPERIMENTAL.

The prompt text is FROZEN: byte-for-byte what was validated
(docs/analysis_validation.md). Any edit, even whitespace, changes the model's
behaviour, so it needs a new version in prompts.yaml and a new validation round.

Method: keyword patterns pick candidate passages (patterns.py), and the model
answers one yes/no question about those excerpts only, with a verbatim quote.
"""

from __future__ import annotations

from .promptfile import CONFIG

PROMPT_VERSION = "2026-10-16.frozen1"

METADATA_TOKEN = "<<METADATA>>"
EXCERPTS_TOKEN = "<<EXCERPTS>>"


CONFIRM_TEMPLATES: dict[str, str] = {}

# The three shipped themes: prompt text and versions live in prompts.yaml.
_THEMES = CONFIG["themes"]
for _c in ("workforce_cause", "agency_delay_cause", "sources_rejected"):
    CONFIRM_TEMPLATES[_c] = _THEMES[_c]["prompt"]
WORKFORCE_CONFIRM_VERSION = _THEMES["workforce_cause"]["version"]
assert _THEMES["agency_delay_cause"]["version"] == _THEMES["sources_rejected"]["version"] == PROMPT_VERSION


# Round-4 workforce_cause method (regex gate, then confirm). Written
# 2026-10-17 on design data only (round-1 dev/holdout documents and the
# round-3 keyword-enriched workforce set), then frozen before testing.

CONFIRM_SCHEMA: dict = {
    "type": "object",
    "properties": {"answer": {"type": "boolean"}, "quote": {"type": ["string", "null"]},
                   "quote2": {"type": ["string", "null"]}, "explanation": {"type": "string"}},
    "required": ["answer", "quote", "quote2", "explanation"],
    "additionalProperties": False,
}

CONFIRM_SCHEMA_N_FIRMS: dict = {
    "type": "object",
    "properties": {"answer": {"type": "boolean"}, "quote": {"type": ["string", "null"]},
                   "quote2": {"type": ["string", "null"]}, "n_firms": {"type": ["integer", "null"]},
                   "explanation": {"type": "string"}},
    "required": ["answer", "quote", "quote2", "n_firms", "explanation"],
    "additionalProperties": False,
}

# Tool names the frozen runs used (part of what the model sees).
CONFIRM_TOOL = {"agency_delay_cause": "ja_confirm", "workforce_cause": "ja_confirm_wf",
                "sources_rejected": "ja_confirm2"}


def metadata_line(meta: dict) -> str:
    return ("SAM.gov notice metadata (not part of the document; do not quote it): "
            f"title={meta.get('title')!r}; department={meta.get('agency')}; "
            f"posted_date={meta.get('posted_date')}\n")


def confirm_prompt(category: str, meta: dict, excerpts: list[str]) -> str:
    return (CONFIRM_TEMPLATES[category]
            .replace(METADATA_TOKEN, metadata_line(meta))
            .replace(EXCERPTS_TOKEN, "\n\n[...]\n\n".join(excerpts)))


def confirm_schema(category: str) -> dict:
    return CONFIRM_SCHEMA if category in ("agency_delay_cause", "workforce_cause") else CONFIRM_SCHEMA_N_FIRMS


def prompt_version(category: str) -> str:
    """Each category's prompt is versioned on its own, so changing one
    category's method does not re-review the others."""
    return WORKFORCE_CONFIRM_VERSION if category == "workforce_cause" else PROMPT_VERSION
