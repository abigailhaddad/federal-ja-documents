"""``python -m federal_ja.analysis spreadsheet --out FILE.xlsx`` -- a reader-friendly workbook.

EXPERIMENTAL (it includes the AI-generated columns). Everything is read from the
published Hugging Face dataset, so no local files are needed:

* ``analysis/extractions.parquet``, ``analysis/theme_status.parquet`` and
  ``analysis/theme_tags.parquet`` (the AI tables, a static snapshot), and
* ``data/notices.parquet``, ``data/attachments.parquet`` and ``data/pages.parquet``
  read at the SAME dataset commit the AI tables were built from, so every row
  joins. Notices posted after that snapshot are not in the workbook until the
  AI tables are rebuilt.

Rows: only notices with a downloaded document that the extraction read as an
actual J&A (``is_ja == "yes"``). Everything left out is counted, with reasons,
on the About sheet. Sorted newest posting first.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import pandas as pd

HF_BASE = "https://huggingface.co/datasets/abigailhaddad/federal-ja-documents/resolve/"
PROJECT_URL = "https://github.com/abigailhaddad/federal-ja-documents"
DATASET_URL = "https://huggingface.co/datasets/abigailhaddad/federal-ja-documents"

THEMES = {
    "sources_rejected": "Theme: other firms responded but were rejected",
    "agency_delay_cause": "Theme: agency's own delay caused it",
    "workforce_cause": "Theme: shutdown / staff loss / DOGE / reorg caused it",
}
NOT_REVIEWED = {"no_text": "not reviewed (no readable text)", "too_long": "not reviewed (too long)"}


# --------------------------------------------------------------------------- loading

def load_tables(rev: str | None = None) -> dict[str, pd.DataFrame]:
    """Read the AI tables at ``rev`` (default: main), then the data tables at the
    dataset commit the AI tables record."""
    import duckdb

    c = duckdb.connect()
    head = HF_BASE + (rev or "main") + "/"
    t = {name: c.sql(f"select * from '{head}analysis/{name}.parquet'").df()
         for name in ("extractions", "theme_status", "theme_tags")}
    revs = set(t["extractions"].dataset_revision.dropna()) | set(t["theme_status"].dataset_revision.dropna())
    if len(revs) != 1:
        raise ValueError(f"AI tables were built from more than one dataset commit: {sorted(revs)}")
    data_rev = revs.pop()
    base = HF_BASE + data_rev + "/"
    t["notices"] = c.sql(f"select * from '{base}data/notices.parquet'").df()
    t["attachments"] = c.sql(
        f"""select notice_id, attachment_order, file_path, download_status, link_url
            from '{base}data/attachments.parquet' order by notice_id, attachment_order""").df()
    t["pages"] = c.sql(
        f"""select notice_id, count(*) as pages,
                   count(*) filter (where text_source = 'transcription') as transcribed
            from '{base}data/pages.parquet' group by 1""").df()
    t["_data_rev"] = data_rev
    return t


# --------------------------------------------------------------------------- shaping

def _ts(v):
    """'2026-02-24 09:06:24' -> datetime; anything unparseable -> None."""
    if v is None or (isinstance(v, float) and pd.isna(v)) or v == "":
        return None
    try:
        return pd.Timestamp(v).to_pydatetime()
    except (ValueError, TypeError):
        return None


def _day(v):
    d = _ts(v)
    return d.date() if d else None


def _s(v):
    """Empty / NaN -> None, else the value unchanged."""
    if v is None or (isinstance(v, float) and pd.isna(v)) or (isinstance(v, str) and not v.strip()):
        return None
    return v


def _yn(v):
    return {True: "yes", False: "no", "True": "yes", "False": "no"}.get(v)


def _true(v) -> bool:
    return v is True or v == "True" or (not isinstance(v, str) and v is not None and not pd.isna(v) and bool(v))


def _label(v):
    v = _s(v)
    return v.replace("_", " ") if isinstance(v, str) else v


def _doc_links(att: pd.DataFrame, hf_root: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for r in att.itertuples():
        if r.download_status == "ok" and _s(r.file_path):
            out.setdefault(r.notice_id, []).append(hf_root + r.file_path)
    return out


def _pages_label(v) -> str | None:
    pages = json.loads(v) if isinstance(v, str) else (list(v) if v is not None else [])
    parts = [f"p{p['page_number']}" + (" (transcribed)" if p.get("text_source") == "transcription" else "")
             for p in pages if isinstance(p, dict) and p.get("page_number") is not None]
    return "; ".join(parts) or None


def build_frames(t: dict) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """(rows, tag rows, stats). Pure: no network."""
    n, att, pg = t["notices"], t["attachments"], t["pages"]
    ext = t["extractions"].set_index("notice_id")
    st = t["theme_status"].set_index("notice_id")
    hf_root = HF_BASE + t["_data_rev"] + "/"
    links = _doc_links(att, hf_root)
    pages = pg.set_index("notice_id")

    left_out: Counter = Counter()
    rows = []
    for r in n.itertuples():
        if r.notice_id not in links:
            left_out["no downloadable document"] += 1
            continue
        e = ext.loc[r.notice_id] if r.notice_id in ext.index else None
        is_ja = _s(e["is_ja"]) if e is not None else None
        if is_ja != "yes":
            left_out[{"no": "not a J&A (mostly award notices)",
                      "unclear": "unclear (blank templates, unreadable placeholders, garbled scans)"}
                     .get(is_ja, "no readable text")] += 1
            continue
        s = st.loc[r.notice_id] if r.notice_id in st.index else None
        theme = {}
        for cat, lab in THEMES.items():
            if s is None:
                theme[lab] = None
            elif _s(s["not_reviewed_reason"]):
                theme[lab] = NOT_REVIEWED.get(s["not_reviewed_reason"], "not reviewed")
            else:
                theme[lab] = "TAGGED" if s[f"{cat}_status"] == "tagged" else "not tagged"
        dl = links[r.notice_id]
        p = pages.loc[r.notice_id] if r.notice_id in pages.index else None
        idv = r.usaspending_award_or_idv == "IDV"
        problems = "; ".join(x for x, f in [("document cut short", _true(e["truncated"])),
                                            ("garbled text layer", _true(e["garbled_text_layer"]))] if f)
        rows.append({
            "Posted": _ts(r.posted_date),
            "Agency": _s(r.agency), "Sub-agency": _s(r.sub_tier),
            "Title": _s(r.title),
            "Vendor named in J&A (AI)": _s(e["vendor"]),
            "Estimated value $ stated in J&A (AI)": float(e["estimated_value_usd"]) if _s(e["estimated_value_usd"]) is not None else None,
            "Obligated $ so far (USAspending)": None if idv else _s(r.usaspending_total_obligated),
            **theme,
            "Summary (AI-written)": _s(e["summary_model_written"]),
            "SAM.gov notice": _s(r.sam_link),
            "Document": dl[0],
            "Authority in plain words (AI)": _s(e["authority_plain"]),
            "Authority cited (AI)": _s(e["authority_cited"]),
            "Document kind (AI)": _label(e["doc_kind"]),
            "What the value covers (AI)": _label(e["value_basis"]),
            "Value redacted (AI)": _yn(e["value_redacted"]),
            "Value quote (AI)": _s(e["value_quote"]),
            "Other vendors named (AI)": _s(e["additional_vendors"]),
            "Covers several contracts (AI)": _yn(e["covers_multiple_actions"]),
            "Period of performance (AI)": _s(e["period_of_performance"]),
            "Signature date (AI)": _day(e["signature_date"]),
            "Market research (AI-written)": _s(e["market_research_summary_model_written"]),
            "Other firms named (AI)": _s(e["other_firms_named"]),
            "Redactions detected (AI)": _s(e["redaction_detected"]),
            "Values removed by checks (AI)": _s(e["values_dropped_by_checks"]),
            "Notes (AI-written)": _s(e["notes_model_written"]),
            "Text problems": problems or None,
            "Office": _s(r.office),
            # Justification, or Special Notice for J&As found by their title
            # (collection_reason in the dataset); older snapshots lack the column.
            "SAM.gov notice type": _s(getattr(r, "type", None)),
            "Award number": _s(r.award_number),
            "Other documents": "\n".join(dl[1:]) or None,
            "Pages": int(p["pages"]) if p is not None else None,
            "Pages machine-transcribed": int(p["transcribed"]) if p is not None else None,
            "USAspending matched": "yes" if r.usaspending_matched else "no",
            "USAspending match note": _s(r.usaspending_match_note) or _s(r.usaspending_match_method),
            "Vendor (USAspending)": _s(r.usaspending_recipient_name),
            "Vendor UEI": _s(r.usaspending_recipient_uei),
            "Award or contract vehicle": _s(r.usaspending_award_or_idv),
            "Orders obligated $ under vehicle, all agencies": _s(r.usaspending_idv_orders_obligated),
            "Potential total value $ (USAspending)": _s(r.usaspending_potential_total_value),
            "Extent competed": _s(r.usaspending_extent_competed),
            "First action": _day(r.usaspending_first_action_date),
            "Latest action": _day(r.usaspending_latest_action_date),
            "USAspending page": _s(r.usaspending_permalink),
            "Notice ID": r.notice_id,
        })
    df = pd.DataFrame(rows).sort_values("Posted", ascending=False, kind="stable").reset_index(drop=True)

    kept = set(df["Notice ID"])
    tg = t["theme_tags"]
    tg = tg[tg.notice_id.isin(kept)]
    tags = pd.DataFrame({
        "Posted": tg.posted_date.map(_ts),
        "Theme": tg.category.map(THEMES).str.replace("Theme: ", "", regex=False),
        "Agency": tg.agency, "Title": tg.title,
        "Quote": tg.quote, "Second quote": tg.quote2.map(_s),
        "AI explanation": tg.explanation_model_written,
        "Quote page(s)": tg.quote_pages.map(_pages_label),
        "SAM.gov notice": tg.sam_link,
        "Document": tg.notice_id.map(lambda k: (links.get(k) or [None])[0]),
        "Notice ID": tg.notice_id,
    }).sort_values(["Posted", "Theme"], ascending=[False, True], kind="stable").reset_index(drop=True)

    stats = {
        "rows": len(df), "notices_total": len(n), "left_out": dict(left_out),
        "first": df.Posted.min(), "last": df.Posted.max(),
        "matched": int((df["USAspending matched"] == "yes").sum()),
        "us_as_of": str(n.usaspending_as_of.dropna().max())[:10] if "usaspending_as_of" in n else "",
        "value": int(df["Estimated value $ stated in J&A (AI)"].notna().sum()),
        "value_redacted": int((df["Value redacted (AI)"] == "yes").sum()),
        "themes": {lab: int((df[lab] == "TAGGED").sum()) for lab in THEMES.values()},
        "tags": len(tags), "tag_docs": tags["Notice ID"].nunique(),
        "data_rev": t["_data_rev"],
        "ai_run": str(t["extractions"].run_at.dropna().max())[:10],
    }
    assert len(df) + sum(left_out.values()) == len(n), "rows + left out must equal notices"
    assert df["Notice ID"].is_unique
    return df, tags, stats


def about_rows(s: dict, built: str) -> list[list[str]]:
    lo = s["left_out"]
    themes = "; ".join(f"{k.replace('Theme: ', '')} ({v:,} J&As)" for k, v in s["themes"].items())
    return [
        ["Federal sole-source justifications (J&As): documents, vendors, dollars, and AI-read details"],
        [""],
        ["What this is", f"{s['rows']:,} Justification & Approval documents posted on SAM.gov between {s['first']:%Y-%m-%d} and {s['last']:%Y-%m-%d}, newest first. A J&A is the document an agency writes to explain why it is buying without full and open competition."],
        ["Left out", f"{sum(lo.values()):,} more notices in the dataset: " + "; ".join(f"{v:,} {k}" for k, v in sorted(lo.items(), key=lambda x: -x[1])) + "."],
        ["Full text", f"Every document and its text (including transcriptions of scanned pages) is on Hugging Face: {DATASET_URL} . Search the 'pages' table there to find phrases."],
        [""],
        ["COLUMNS", ""],
        ["First columns", "Posted, agency, title, vendor, the two dollar figures, the three themes and an AI summary, then links to the SAM.gov notice and the document. Detail columns follow."],
        ["SAM.gov columns", "Agency, office, title, posting time, notice type (Justification, or Special Notice for J&As posted under that type and found by their title), award number, and links to the notice and to our copy of each document."],
        ["USAspending columns", f"Matched by award number to USAspending.gov data current through {s['us_as_of']}. {s['matched']:,} of {s['rows']:,} matched. 'Obligated $ so far' is money actually committed on that award, not the J&A's estimate; it is blank when the award number is a contract vehicle (GSA schedule, IDIQ), which has no money of its own; 'Orders obligated' then covers every agency's orders under that vehicle. DoD reports about 90 days late, so recent J&As match less often."],
        ["AI columns (marked AI)", f"Read out of every J&A by Claude Haiku 5.5. Every fact (authority, value, vendor, dates, period) had to come with a quote that was then found in the document; facts whose quote was not found were removed ('Values removed by checks'). Columns marked 'AI-written' are the model's own words, not quotes. A dollar figure that is only an approval ceiling ('not exceeding $15 million') is not reported as the value. {s['value']:,} J&As have a stated value; in {s['value_redacted']:,} the value is redacted."],
        ["How accurate the AI columns are", "A check of 32 randomly chosen J&As against the documents found 1 wrong value (a unit price reported as the total) and no wrong vendors, dates or authorities; 19 hand-picked hard cases were also checked. That is a sample: with 0 errors in 32, the true error rate could still be up to about 10%. The answer key was written by separate Claude instances, with disagreements settled against the document text. 'Redactions detected: not detected' does NOT mean nothing was redacted. Check the document before quoting or relying on any AI column."],
        ["Themes", f"Claude Haiku 5.5 read every J&A with readable text and flagged passages for three themes: {themes}. The {s['tags']:,} quotes (in {s['tag_docs']:,} J&As) are on the 'Theme tags' sheet with page numbers. In testing on 400 random J&As, roughly 7 to 9 in 10 tags were right, and the tool misses cases (it found about a third of the agency-delay cases and about 70% of the other two). A tag is a lead to read, not a finding; 'not tagged' does not mean the theme is absent."],
        [""],
        ["CAVEATS", ""],
        ["Redactions", "Some agencies' redactions failed (black boxes drawn over text that is still in the file). Text, quotes and AI readings can include words under those boxes. Check the page image before using a quote."],
        ["Not every J&A", "Agencies post J&As inconsistently, and some (intelligence agencies, the legislative branch) post few or none. Absence here is not evidence a contract was competed."],
        ["Snapshot", f"AI tables run {s['ai_run']} on dataset commit {s['data_rev'][:12]}; J&As posted after that are not included until the AI tables are rebuilt."],
        ["Built", f"{built} with `python -m federal_ja.analysis spreadsheet`. Code: {PROJECT_URL} . Data: {DATASET_URL} (documents and tables CC0; code Apache-2.0)."],
    ]


# --------------------------------------------------------------------------- writing

WIDE = {"Title": 45, "Summary (AI-written)": 70, "Market research (AI-written)": 60, "Notes (AI-written)": 50,
        "Value quote (AI)": 50, "Agency": 30, "Sub-agency": 30, "Office": 30, "Vendor named in J&A (AI)": 30,
        "Vendor (USAspending)": 30, "Other firms named (AI)": 35, "Values removed by checks (AI)": 35,
        "Period of performance (AI)": 30, "Authority in plain words (AI)": 30, "USAspending match note": 30,
        "Quote": 70, "Second quote": 60, "AI explanation": 70, "Other documents": 30,
        "Posted": 17, "Theme": 32}
LINKS = {"SAM.gov notice", "Document", "USAspending page"}


def write_workbook(df: pd.DataFrame, tags: pd.DataFrame, about: list[list[str]], out: Path) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(out, engine="openpyxl") as w:
        pd.DataFrame(about).to_excel(w, sheet_name="About", index=False, header=False)
        df.to_excel(w, sheet_name="J&As", index=False)
        tags.to_excel(w, sheet_name="Theme tags", index=False)
        wb = w.book
        ab = wb["About"]
        ab.column_dimensions["A"].width = 30
        ab.column_dimensions["B"].width = 120
        for row in ab.iter_rows():
            for c in row:
                c.alignment = Alignment(wrap_text=True, vertical="top")
            if row[0].value:
                row[0].font = Font(bold=True, size=14 if row[0].row == 1 else 11)
        for ws, freeze in ((wb["J&As"], "C2"), (wb["Theme tags"], "C2")):
            ws.freeze_panes = freeze
            ws.auto_filter.ref = ws.dimensions
            ws.row_dimensions[1].height = 48
            for c in ws[1]:
                name = c.value or ""
                col = c.column_letter
                ws.column_dimensions[col].width = WIDE.get(name, 16)
                colour = ("6B3FA0" if "(AI" in name else "2E7D5B" if name.startswith("Theme") else "1F3A5F")
                c.font = Font(bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", fgColor=colour)
                c.alignment = Alignment(wrap_text=True, vertical="top")
                is_money = "$" in name
                for cell in ws[col][1:]:
                    v = cell.value
                    if v is None:
                        continue
                    if name in LINKS and isinstance(v, str) and v.startswith("http"):
                        cell.hyperlink = v
                        cell.font = Font(color="0563C1", underline="single")
                    elif is_money and isinstance(v, (int, float)):
                        cell.number_format = '"$"#,##0'
                    elif isinstance(v, datetime):
                        cell.number_format = "yyyy-mm-dd hh:mm"
                    elif isinstance(v, date):
                        cell.number_format = "yyyy-mm-dd"
                    if name == "Theme" or (name.startswith("Theme:") and v == "TAGGED"):
                        cell.font = Font(bold=True)
        wb.active = 0


def main(out: Path, revision: str | None = None) -> dict:
    t = load_tables(revision)
    df, tags, stats = build_frames(t)
    write_workbook(df, tags, about_rows(stats, datetime.now().strftime("%Y-%m-%d")), out)
    stats = {k: (v.isoformat() if isinstance(v, (datetime, date)) else v) for k, v in stats.items()}
    return stats
