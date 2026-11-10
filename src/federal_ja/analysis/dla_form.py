"""Rule-based reading of the Defense Logistics Agency J&A form "AVN Form S53".
No model call.

The form has, on page 2, a table of approved sources (CAGE | COMPANY NAME)
and a column of tick boxes: "AMC 1." to "AMC 5." (acquisition method code)
and "AMSC A." to "AMSC Z." (its suffix), which say why the part isn't
competed. A later page has item 12, "A listing of sources, if any,
indicating an interest in writing in the acquisition", another CAGE table.
About 1,300 notices in the dataset use it (2026-11-10).

Nine in ten of these forms are scans, so the text is a machine
transcription in which a ticked box is "[X]", "[✓]", "☑" or "[checked]".
Checked against the page images on 2026-11-10 (36 forms, random with a fixed
seed plus chosen edge cases): of the 32 with tick marks in the text, the AMC
matched in 31, the AMSC in 32, the approved table in 31 and the interested
table in 30. The failures set the guards in ``read``:

* The transcription sometimes invents a tick. Of six forms whose text showed
  more than one AMC ticked, three had only one on the page. So a code is
  given only when exactly one AMC is ticked and the page uses one tick style
  (the invented ticks came with mixed styles).
* 77 forms have an embedded text layer with no tick marks and no table
  contents at all. "No rows" there means unreadable, not none, so an empty
  table is never reported as "none listed": only an explicit "None"/"N/A" is.
* CAGE codes never contain the letters I or O; a code that does is a misread
  digit, and the table it is in is not given.

Each part of the result is None when its guard fails. A None is "not read",
never "nothing there".
"""

from __future__ import annotations

import re

FORM_NAME = "AVN Form S53"

_TICK = re.compile(r"^(?:\[\s*[xX✓✔√]\s*\]|☑|☒|\[checked\])$")
_EMPTY = re.compile(r"^(?:\[\s*\]|☐|\[unchecked\])$")
_LINE = re.compile(r"^\s*(?P<mk>.{0,14}?)\s*(?P<kind>AMSC|AMC)\s+(?P<code>[1-5]|[A-Z])\.?(?=\s|$)")
_OTHER = re.compile(r"^\s*(?P<mk>.{0,14}?)\s*Other:\s*_*", re.I)
_HEADER = re.compile(r"CAGE\b[^\n]*COMPANY NAME")
_CELL_CAGE = re.compile(r"^\(?([0-9A-Za-z]{5})\)?$")
CAGE = re.compile(r"^[0-9A-HJ-NP-Z]{5}$")           # no I, no O
_NONE = re.compile(r"^(none|n/?a|no sources?|none received|none identified)\b", re.I)
_BLANK = re.compile(r"^\[blank[^\]]*\]$", re.I)
N_AMC_LINES, N_AMSC_LINES = 5, 17


def _mark(mk: str) -> str:
    mk = mk.strip()
    if _TICK.match(mk):
        return "tick"
    if _EMPTY.match(mk):
        return "empty"
    return "none" if mk == "" else "odd"


def _style(mk: str) -> str:
    """The tick's spelling with case and inner spaces removed, so "[X]" and
    "[ x ]" are one style and "[X]" and "[✓]" are two."""
    return re.sub(r"\s+", "", mk).upper()


def _table(block: str) -> tuple[list[tuple[str, str]], list[str], bool]:
    """(rows, problems, an explicit none was written) from the pipe rows of a
    CAGE | COMPANY NAME table (some forms have a third column between)."""
    rows, problems, none_seen = [], [], False
    for line in block.split("\n"):
        if _HEADER.search(line):
            continue
        if "|" not in line:
            if line.strip() and _NONE.match(line.strip()):
                none_seen = True
            continue
        cells = ["" if _BLANK.match(x.strip()) else x.strip() for x in line.strip().strip("|").split("|")]
        filled = [x for x in cells if x]
        if not filled:
            continue
        m = _CELL_CAGE.match(cells[0]) if cells[0] else None
        if m and len(filled) >= 2:
            cage = m.group(1)
            rows.append((cage, filled[-1]))
            if not CAGE.match(cage):
                problems.append(f"not a CAGE code: {cage}")
        elif all(_NONE.match(x) for x in filled):
            none_seen = True
        else:
            problems.append(f"irregular row: {line.strip()[:60]}")
    return rows, problems, none_seen


def read(text: str) -> dict | None:
    """The form's fields from the text of one document (pages joined with
    newlines), or None when the text is not this form. Keys:

    amc, amsc      the ticked code and suffix letters, or None (guard failed)
    other_ticked   the "Other:" box, or None
    approved       [(cage, name)] or None
    interested     [(cage, name)], [] for an explicit "none", or None
    why_not        {part: reason} for every part that is None
    """
    if FORM_NAME not in text:
        return None
    out: dict = {"amc": None, "amsc": None, "other_ticked": None, "approved": None, "interested": None,
                 "why_not": {}}
    start = text.find("The use of authority")
    if start < 0:
        out["why_not"]["codes"] = out["why_not"]["approved"] = "no checkbox section in the text"
    else:
        ends = [x for x in (text.find("NOTE: A technical", start), text.find("8. Efforts", start)) if x > 0]
        section = text[start:min(ends)] if ends else text[start:start + 9000]
        amc, amsc, styles, odd, seen, other = [], [], set(), False, set(), None
        for line in section.split("\n"):
            m = _LINE.match(line)
            if m and "Other" not in m["mk"]:
                kind = "AMC" if m["code"].isdigit() else "AMSC"     # some transcriptions print "AMC A."
                if (kind, m["code"]) in seen:
                    continue
                seen.add((kind, m["code"]))
                mark = _mark(m["mk"])
                if mark == "tick":
                    styles.add(_style(m["mk"]))
                    (amc if kind == "AMC" else amsc).append(m["code"])
                elif mark in ("none", "odd"):
                    odd = True
                continue
            m = _OTHER.match(line)
            if m and other is None:
                other = _mark(m["mk"])
                if other == "tick":
                    styles.add(_style(m["mk"]))
        n_amc = sum(1 for k in seen if k[0] == "AMC")
        n_amsc = sum(1 for k in seen if k[0] == "AMSC")
        why = None
        if not styles and odd:
            why = "no tick marks in the text"
        elif (n_amc, n_amsc) != (N_AMC_LINES, N_AMSC_LINES):
            why = f"expected {N_AMC_LINES} AMC and {N_AMSC_LINES} AMSC lines, found {n_amc} and {n_amsc}"
        elif odd:
            why = "a box with a missing or unreadable mark"
        elif len(amc) != 1:
            why = "no AMC ticked" if not amc else "more than one AMC ticked"
        elif len(styles) > 1:
            why = "more than one tick style on the page"
        if why:
            out["why_not"]["codes"] = why
        else:
            out.update(amc=amc[0], amsc=sorted(amsc), other_ticked=(other == "tick"))
        before = text[:start]
        headers = list(_HEADER.finditer(before))
        if not headers:
            out["why_not"]["approved"] = "table header not found"
        else:
            rows, problems, _ = _table(before[headers[-1].start():])
            if problems:
                out["why_not"]["approved"] = "; ".join(problems[:3])
            elif not rows:
                out["why_not"]["approved"] = "no rows read"       # blank, or lost: can't tell
            else:
                out["approved"] = rows
    m = re.search(r"\n\s*12\.\s*A listing of sources", text)
    if not m:
        out["why_not"]["interested"] = "item 12 not found"
    else:
        block = text[m.end():]
        end = re.search(r"\n\s*13\.\s", block)
        rows, problems, none_seen = _table(block[:end.start()] if end else block[:1500])
        if problems:
            out["why_not"]["interested"] = "; ".join(problems[:3])
        elif rows:
            out["interested"] = rows
        elif none_seen:
            out["interested"] = []
        else:
            out["why_not"]["interested"] = "table blank, with no 'none'"
    return out
