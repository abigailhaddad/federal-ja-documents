"""EXPERIMENTAL: tag possible issues in published J&A documents.

Everything in this package is experimental. It uses a language model
(Claude Haiku 5.5 by default) to tag passages in J&A documents that a person
may want to check: for example, a J&A that says agency delay led to the sole
source, or that other firms responded and were rejected. Every tag is a
POSSIBLE ISSUE FOR A PERSON TO CHECK against the document, never a finding
that anything was wrong, and every tag carries a verbatim quote that is
verified against the page text.

The package reads only the published Hugging Face dataset
(``data/notices.parquet``, ``data/attachments.parquet``,
``data/pages.parquet``), never collection internals. A document's text is its
pages in order, native text layer or machine transcription, and each tag
records whether its quote sits on a transcribed page.

Each category's measured precision and recall, with 95% intervals, and its
status ("ready", "screening" or not shipped) are in
``docs/analysis_validation.md``. Only categories with a "screening" or better
status can be run.

Usage::

    python -m federal_ja.analysis tag --category agency_delay_cause --limit 20 --out work/tags
"""
