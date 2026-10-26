"""Text of XFA PDF forms (2026-10-26).

Some agencies' J&A forms (many Air Force and Army ones) are XFA forms: the
PDF's only page is a placeholder that says "Please wait... If this message
is not eventually replaced by the proper contents of the document, your PDF
viewer may not be able to display this type of document", and the form
itself lives in XML inside the file. pypdf's text layer, and a picture of
the page, both give only the placeholder.

``form_text`` reads the XML instead: it walks the form's ``template`` (the
form's own wording: title, section headings, field captions) in order and
puts each field's filled-in value from ``datasets`` after its caption, so
the text reads in the order the printed form does. Values it could not
place follow under "Other form entries:". On the 112 such attachments in
the dataset on 2026-10-26 it gave at least 300 characters for 111; the
other is an "RFIs Answered" page with 230.

The result is stored as one page with ``text_source`` "xfa_form" (the form
has no fixed pages), and the attachment's ``text_source`` is "pdf_xfa".
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

PLACEHOLDER = re.compile(r"Please\s+wait\.*\s*If\s+this\s+message\s+is\s+not\s+eventually\s+replaced", re.I)


def is_placeholder(text: str) -> bool:
    return bool(PLACEHOLDER.search((text or "")[:600]))


def _local(tag) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def xfa_parts(reader) -> dict[str, bytes]:
    """{part name: XML bytes} from a pypdf PdfReader; {} for a non-XFA PDF."""
    acro = reader.trailer["/Root"].get("/AcroForm")
    xfa = acro.get_object().get("/XFA") if acro is not None else None
    if xfa is None:
        return {}
    xfa = xfa.get_object()
    if hasattr(xfa, "get_data"):
        return {"whole": xfa.get_data()}
    return {str(xfa[i]): xfa[i + 1].get_object().get_data() for i in range(0, len(xfa) - 1, 2)}


def _text(el) -> str:
    """All text under an element, one line per <p>."""
    out: list[str] = []

    def walk(e):
        t = _local(e.tag)
        if t in ("p", "br", "li") and out and not out[-1].endswith("\n"):
            out.append("\n")
        if e.text:
            out.append(e.text)
        for c in e:
            walk(c)
            if c.tail:
                out.append(c.tail)
        if t == "p":
            out.append("\n")
    walk(el)
    s = re.sub(r"[ \t ]+", " ", "".join(out))
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def _named(node, name):
    return [c for c in node if _local(c.tag) == name] if node is not None else []


def _draw_text(draw) -> str:
    for v in draw.iter():
        if _local(v.tag) == "value":
            t = _text(v)
            if t:
                return t
    return ""


def _caption(field) -> str:
    return next((_text(c) for c in field if _local(c.tag) == "caption"), "")


def render(template: bytes, datasets: bytes) -> str:
    """The form's wording with the filled-in values, in template order.
    Data binds by name: a named subform descends into the data node of the
    same name (every instance, for repeated subforms), a field takes the
    text of its same-named data node."""
    troot, droot = ET.fromstring(template), ET.fromstring(datasets)
    lines: list[str] = []

    def walk(tnode, dnode):
        for c in tnode:
            tag, name = _local(c.tag), c.get("name")
            if tag == "draw":
                t = _draw_text(c)
                if t:
                    lines.append(t)
            elif tag == "subform" and name:
                for d in _named(dnode, name) or [None]:
                    walk(c, d)
            elif tag in ("subform", "subformSet", "area", "exclGroup"):
                walk(c, dnode)
            elif tag == "field":
                cap = _caption(c)
                vals = _named(dnode, name) if name else []
                val = _text(vals[0]) if vals else ""
                if val and cap:
                    sep = " " if cap.endswith(":") else ": "
                    lines.append(f"{cap}{sep}{val}" if len(val) < 200 else f"{cap}\n{val}")
                elif val:
                    lines.append(val)

    form = next((c for c in troot if _local(c.tag) == "subform"), troot)
    data = next((e for e in droot.iter() if _local(e.tag) == "data"), droot)
    top = _named(data, form.get("name")) if form.get("name") else []
    walk(form, top[0] if top else data)
    out: list[str] = []
    for ln in lines:                       # the same text in consecutive draws
        if not out or out[-1] != ln:
            out.append(ln)
    # Values the walk did not place (fields bound by an explicit data
    # reference, table rows, ...) follow in data order, so no filled-in text
    # is lost. On the 112 forms of 2026-10-26 the walk alone placed 93% of
    # the values of 20+ characters.
    placed = _squash("\n".join(out))
    extra = []
    for v in _data_values(data):
        sv = _squash(v)
        if re.search(r"[A-Za-z]{2}", v) and sv not in placed:
            extra.append(v)
            placed += " " + sv
    if extra:
        out += ["Other form entries:"] + extra
    return "\n".join(out)


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _data_values(node) -> list[str]:
    """Every filled-in value under a data node, in document order: a rich
    text <body> as one value, otherwise each leaf element's text."""
    vals: list[str] = []

    def walk(e):
        if _local(e.tag) == "body":
            t = _text(e)
            if t:
                vals.append(t)
            return
        if len(e) == 0:
            if e.text and e.text.strip():
                vals.append(_text(e))
            return
        for c in e:
            walk(c)
    walk(node)
    return vals


def form_text(reader) -> str:
    """The form's text, or "" when the PDF has no XFA template and data."""
    parts = xfa_parts(reader)
    if "template" not in parts or "datasets" not in parts:
        return ""
    return render(parts["template"], parts["datasets"])
