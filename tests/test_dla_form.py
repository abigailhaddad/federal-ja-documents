"""The rule-based reader for DLA's AVN Form S53 (analysis/dla_form.py). No model calls."""

from federal_ja.analysis import dla_form, extras
from federal_ja.analysis.text import Document, Page

AMSC = "ABCDHKLMNPQRSUVYZ"


def form(amc_ticks=("1",), amsc_ticks=("C",), tick="[X]", approved=None, item12=None, other=False, drop_marks=False):
    approved = approved if approved is not None else ["95270 | SKURKA AEROSPACE INC", "81982 | HYDRO-AIRE AEROSPACE CORP"]
    box = lambda on: "" if drop_marks else ((tick if on is True else on if isinstance(on, str) else "[ ]") + " ")
    lines = ["JUSTIFICATION FOR OTHER THAN FULL AND OPEN COMPETITION", "CAGE | COMPANY NAME", *approved, "",
             "The use of authority 10 U.S.C. 3204(a)(1) is based on:"]
    for n in "12345":
        on = dict(amc_ticks).get(n) if isinstance(amc_ticks, dict) else (n in amc_ticks)
        lines.append(f"{box(on)}AMC {n}. Acquire in some way.")
    lines += [f"{box(c in amsc_ticks)}AMSC {c}. Some reason." for c in AMSC]
    lines += [f"{box(other)}Other: ____", "NOTE: A technical certification is required for acquisitions over $15 million",
              "AVN Form S53, March 2026", "",
              "12. A listing of sources, if any, indicating an interest in writing in the acquisition are: (if none, state none)",
              "CAGE | COMPANY NAME", *(item12 if item12 is not None else ["3XMD9 | NEW PHOENIX AEROSPACE"]), "",
              "13. The actions, being taken to overcome barriers to competition are: none."]
    return "\n".join(lines)


def test_reads_the_ticked_codes_and_both_tables():
    r = dla_form.read(form())
    assert (r["amc"], r["amsc"], r["other_ticked"]) == ("1", ["C"], False)
    assert r["approved"] == [("95270", "SKURKA AEROSPACE INC"), ("81982", "HYDRO-AIRE AEROSPACE CORP")]
    assert r["interested"] == [("3XMD9", "NEW PHOENIX AEROSPACE")] and r["why_not"] == {}
    assert dla_form.read("Some other J&A with AMC 1. and CAGE | COMPANY NAME") is None


def test_no_code_when_more_than_one_amc_is_ticked_or_the_tick_styles_are_mixed():
    # 2026-11-10: of six forms whose text showed two AMC ticks, three had one on the page
    r = dla_form.read(form(amc_ticks=("1", "3")))
    assert r["amc"] is None and r["amsc"] is None and r["why_not"]["codes"] == "more than one AMC ticked"
    assert r["approved"] is not None                      # the tables don't depend on the ticks
    r = dla_form.read(form(amc_ticks={"3": "[✓]"}, amsc_ticks=("V",), tick="[X]"))
    assert r["amc"] is None and r["why_not"]["codes"] == "more than one tick style on the page"
    assert dla_form.read(form(amc_ticks=()))["why_not"]["codes"] == "no AMC ticked"


def test_a_text_layer_without_tick_marks_gives_nothing_not_none():
    r = dla_form.read(form(drop_marks=True, approved=[], item12=[]))
    assert (r["amc"], r["approved"], r["interested"]) == (None, None, None)
    assert r["why_not"] == {"codes": "no tick marks in the text", "approved": "no rows read",
                            "interested": "table blank, with no 'none'"}


def test_interested_none_is_only_an_explicit_none():
    assert dla_form.read(form(item12=["None | None"]))["interested"] == []
    assert dla_form.read(form(item12=["N/A"]))["interested"] == []
    assert dla_form.read(form(item12=["[blank] | [blank]"]))["interested"] is None


def test_a_cage_code_with_the_letter_o_or_i_stops_the_table():
    r = dla_form.read(form(item12=["3XMD9 | NEW PHOENIX AEROSPACE", "4KOV2 | IPT"]))
    assert r["interested"] is None and r["why_not"]["interested"] == "not a CAGE code: 4KOV2"


def test_other_tick_styles_and_three_column_tables():
    r = dla_form.read(form(tick="☑", approved=["CAGE | CUI | COMPANY NAME", "0B9R9 | | ACME INC"], other=True))
    assert r["amc"] == "1" and r["other_ticked"] is True and r["approved"] == [("0B9R9", "ACME INC")]


def test_the_models_lists_are_compared_with_the_forms_by_cage_code():
    d = Document("n1", {}, [Page("r1", "S53_Redacted.pdf", 1, "transcription", form())])
    aq = "95270 | SKURKA AEROSPACE INC 81982 | HYDRO-AIRE AEROSPACE CORP"

    def rec(facts):
        return extras.extras_document(d, call_fn=lambda p, **kw: ({"facts": facts}, {}))

    both = [{"kind": "approved_source", "value": "95270 | SKURKA AEROSPACE INC", "quote": aq},
            {"kind": "approved_source", "value": "81982 | HYDRO-AIRE AEROSPACE CORP", "quote": aq}]
    r = rec(both)
    assert r["rules"]["form"]["amc"] == "1"
    assert r["fields"]["sources_match_form"] == {"approved_sources": True, "interested_sources": False}
    assert rec(both[:1])["fields"]["sources_match_form"]["approved_sources"] is False     # the model missed one
    row = extras.flat_row(r, {})
    assert (row["form_amc"], row["form_amsc"], row["approved_sources_match_form"]) == ("1", "C", True)
    assert row["form_interested_sources"] == "3XMD9 | NEW PHOENIX AEROSPACE"
    # on the form, the lists shown are the form's own tables, whatever the model said
    assert row["approved_sources_confirmed"] == "95270 | SKURKA AEROSPACE INC; 81982 | HYDRO-AIRE AEROSPACE CORP"
    assert row["interested_sources_confirmed"] == "3XMD9 | NEW PHOENIX AEROSPACE"
    assert extras.flat_row(rec(both[:1]), {})["approved_sources_confirmed"] == row["approved_sources_confirmed"]
    assert extras.flat_row(rec([]), {})["approved_sources_confirmed"] == row["approved_sources_confirmed"]
    # no form: nothing to compare with
    plain = Document("n2", {}, [Page("r", "a.pdf", 1, "native", "A justification with no form in it at all.")])
    r = extras.extras_document(plain, call_fn=lambda p, **kw: ({"facts": []}, {}))
    assert r["fields"]["sources_match_form"] == {"approved_sources": None, "interested_sources": None}
    assert extras.flat_row(r, {})["dla_form"] is False
    # off the form: an "approved source" is not shown; an interested firm with a quote that says so is
    d2 = Document("n3", {}, [Page("r", "a.pdf", 1, "native",
                                  "Novartis is the sole FDA approved source of this product. One response was received from BHPE LLC.")])
    r = extras.extras_document(d2, call_fn=lambda p, **kw: ({"facts": [
        {"kind": "approved_source", "value": "Novartis", "quote": "Novartis is the sole FDA approved source of this product"},
        {"kind": "interested_source", "value": "BHPE LLC", "quote": "One response was received from BHPE LLC"}]}, {}))
    row = extras.flat_row(r, {})
    assert row["approved_sources"] == "Novartis" and row["approved_sources_confirmed"] is None
    assert row["interested_sources_confirmed"] == "BHPE LLC"


def test_on_the_form_a_none_table_does_not_hide_a_firm_the_form_says_responded():
    # 2026-11-10: item 12 said NONE while item 6 said "Triman Industries (CAGE
    # 0ZBE8) responded with interest"
    text = form(item12=["NONE | NONE"]) + "\nOn 03/05/2026, Triman Industries (CAGE 0ZBE8) responded with interest."
    d = Document("n9", {}, [Page("r1", "S53_Redacted.pdf", 1, "transcription", text)])
    rec = lambda facts: extras.extras_document(d, call_fn=lambda p, **kw: ({"facts": facts}, {}))
    row = extras.flat_row(rec([]), {})
    assert row["interested_sources_confirmed"] == "none"
    q = "Triman Industries (CAGE 0ZBE8) responded with interest"
    row = extras.flat_row(rec([{"kind": "interested_source", "value": "0ZBE8 | Triman Industries", "quote": q}]), {})
    assert row["interested_sources_confirmed"] == "0ZBE8 | Triman Industries"


def test_on_the_form_an_approved_cage_the_table_lacks_means_the_list_is_not_shown():
    # 2026-11-10: the table read 70236 where the form's text said "Tactair ... Cage:70326"
    text = form() + "\nThe approved source is Tactair Fluid Controls (Cage:70326)."
    d = Document("n10", {}, [Page("r1", "S53_Redacted.pdf", 1, "transcription", text)])
    r = extras.extras_document(d, call_fn=lambda p, **kw: ({"facts": [
        {"kind": "approved_source", "value": "70326 | Tactair Fluid Controls",
         "quote": "The approved source is Tactair Fluid Controls (Cage:70326)"}]}, {}))
    assert extras.flat_row(r, {})["approved_sources_confirmed"] is None
