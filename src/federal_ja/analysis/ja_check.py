"""Does a Special Notice's document look like an actual J&A? EXPERIMENTAL.

Special Notices are collected when their title matches
``collect.sources.JA_TITLE_PATTERN`` ("sole source", "justification", ...).
Most such titles belong to a notice of intent to sole source, an
announcement made before award, not a justification. This rule keeps a
Special Notice for extraction only when one of its attachments is titled
as a justification. It makes no model call.

A notice counts when either:

* an attachment's **filename** names a justification (J&A, JOFOC, SSJ, LSJ,
  D&F, "Single Source Determination", "Restricting Competition", ...); or
* an attachment's **opening** (first 400 characters) does, and neither the
  filename nor the text before the match is a notice of intent; or
* an all-caps J&A **heading** appears anywhere ("JUSTIFICATION AND
  APPROVAL", "SINGLE SOURCE DETERMINATION", ...): a justification stapled
  behind a notice in one file.

Checked on 2026-10-26 (docs/field_extraction.md, "Special Notices"): on a
random sample of 20 kept and 20 dropped backlog notices read by hand, all
20 kept were justifications and 18 of 20 dropped were not; the rule was
then fixed for both misses. Known weakness: the opening test is
case-insensitive, so the word "justification" in passing in the first 400
characters keeps a notice. Requiring a capital letter was tried: on all
9,067 Special Notices it dropped 3 real justifications to remove 1 notice
of intent, so it was not adopted. The model's own ``is_ja`` label is not used:
it called about 40 plain notices of intent "yes".

Justification-type notices are not checked: the agency posted them as
justifications.
"""

from __future__ import annotations

import re

from .text import Document

SPECIAL_NOTICE_TYPE = "Special Notice"
OPENING_CHARS = 400

HEADING = re.compile(
    r"justification|\bj\s*(?:&|and|n|_|-)?\s*a\b|\bjofoc\b|\bjotfoc\b|\bjefo\b|\bssj\b|\blsj\b"
    r"|determination\s*(?:and|&)\s*findings|\bd\s*&\s*f\b"
    r"|(?:single|sole|limited)[\s-]*source\s+(?:determination|memo)|brand[\s-]*name\s+determination"
    r"|(?:restrict|limit)(?:ing|ed)?[\s_-]*competition"
    r"|only\s+one\s+source\s+(?:is\s+)?(?:reasonably\s+available|able)"
    r"|(?:for|of)\s+other\s+than\s+full\s+and\s+open\s+competition", re.I)
NOTICE_OF_INTENT = re.compile(
    r"notice\s+of\s+intent|intent\s+to\s+(?:award|sole|negotiate|solicit)|not\s+a\s+request\s+for", re.I)
CAPS_HEADING = re.compile(
    r"JUSTIFICATION (?:AND|&) APPROVAL|JUSTIFICATION FOR OTHER THAN|SOLE SOURCE JUSTIFICATION"
    r"|SINGLE SOURCE JUSTIFICATION|LIMITED SOURCES? JUSTIFICATION|DETERMINATION (?:AND|&) FINDINGS"
    r"|SINGLE SOURCE DETERMINATION|EXCEPTION TO FAIR OPPORTUNITY")


# Text that stands in for a document we could not read: an XFA PDF form
# ("Please wait... If this message is not eventually replaced ..."), or a
# file encrypted with Microsoft Information Protection.
UNREADABLE = re.compile(r"Please\s+wait\.*\s*If\s+this\s+message\s+is\s+not\s+eventually\s+replaced"
                        r"|Microsoft\s+Information\s+Protection", re.I)


def unreadable_ja_attachment(doc: Document) -> bool:
    """An attachment named as a justification whose text is empty or an
    unreadable-document placeholder: the model never saw the J&A, so its
    "not a J&A" answer is about the other attachments only."""
    for fn, pages in doc.attachments():
        text = "\n\n".join(p.text for p in pages)
        if HEADING.search((fn or "").replace("_", " ")) and (not text.strip() or UNREADABLE.search(text[:600])):
            return True
    return False


def ja_evidence(doc: Document) -> str | None:
    """'filename', 'opening' or 'heading' (the first test passed), or None
    when no attachment looks like a justification."""
    atts = [(fn or "", "\n\n".join(p.text for p in pages)) for fn, pages in doc.attachments()]
    for fn, _ in atts:
        # "SSJ_CO Signed.pdf": "_" is a word character, so \b would not see the break
        if HEADING.search(fn.replace("_", " ")):
            return "filename"
    for fn, text in atts:
        opening = re.sub(r"\s+", " ", text[:OPENING_CHARS])
        m = HEADING.search(opening)
        if m and not NOTICE_OF_INTENT.search(fn) and not NOTICE_OF_INTENT.search(opening[:m.start()]):
            return "opening"
    if any(CAPS_HEADING.search(text) for _, text in atts):
        return "heading"
    return None


def not_ja_special_notices(notices, docs: dict[str, Document]) -> set[str]:
    """Ids of Special Notices (``notices``: a DataFrame with notice_id and
    type) whose documents don't look like a J&A. That includes one with no
    readable text, unless an attachment's filename names a justification:
    with nothing to read, the notice can't be shown to be a J&A."""
    if "type" not in notices:
        return set()
    sn = set(notices.loc[notices["type"].fillna("").str.strip() == SPECIAL_NOTICE_TYPE, "notice_id"])
    return {nid for nid in sn if nid in docs and ja_evidence(docs[nid]) is None}
