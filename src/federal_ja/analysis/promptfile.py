"""Loads prompts.yaml: the model name and the prompt text behind the
EXPERIMENTAL analysis/ tables. The YAML is the single source; prompts.py,
extract.py and llm.py read from here."""

from __future__ import annotations

from importlib.resources import files

import yaml

PROMPTS_YAML = files(__package__).joinpath("prompts.yaml")
CONFIG: dict = yaml.safe_load(PROMPTS_YAML.read_text(encoding="utf-8"))
