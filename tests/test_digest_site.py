"""The sent-digest archive (federal_ja.digest.archive) and the web archive +
RSS feed built from it (federal_ja.digest.site). No network: the archive is a
local directory, the reader, the model and Buttondown are the fakes in
test_digest.py."""

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import pytest

from federal_ja.digest import archive as ar, buttondown, render, site

from .test_digest import args, run as digest_run


def ok_send(subject, body, *, metadata, status):
    return {"id": "em_ok1", "status": status, "absolute_url": "https://buttondown.com/x/archive/y/"}


# ── the archive is written only after a successful send ────────────────────

def test_dry_run_writes_no_archive_entry(tmp_path):
    digest_run(tmp_path, args(tmp_path, "--dry-run"), send_fn=lambda *a, **k: pytest.fail("sent"))
    assert not (tmp_path / "digest_archive").exists()
    # the local preview copy is there, unsent
    e = json.loads((tmp_path / "out" / "archive_entry.json").read_text())
    assert e["sent_at"] is None and e["buttondown_email_id"] is None and len(e["picks"]) == 1


def test_failed_send_and_draft_write_no_archive_entry(tmp_path):
    def fail(*a, **k):
        raise buttondown.SendError("HTTP 400")
    with pytest.raises(buttondown.SendError):
        digest_run(tmp_path, args(tmp_path, "--send"), send_fn=fail)
    digest_run(tmp_path, args(tmp_path, "--send", "--buttondown-status", "draft"), send_fn=ok_send)
    assert not (tmp_path / "digest_archive").exists()


def test_successful_send_archives_the_email_once(tmp_path):
    assert digest_run(tmp_path, args(tmp_path, "--send"), send_fn=ok_send) == 0
    files = sorted((tmp_path / "digest_archive").glob("*.json"))
    assert [f.name for f in files] == ["2026-10-21.json"]
    e = json.loads(files[0].read_text())
    assert e["subject"].startswith("WOAH! J&As — Oct 21: VA loses")
    assert e["buttondown_email_id"] == "em_ok1" and e["sent_at"] == "2026-10-21T15:00:00+00:00"
    assert e["notices_reviewed"] == 1 and e["dataset_revision"] == "rev1"
    p = e["picks"][0]
    assert p["headline"].startswith("VA loses") and p["quotes"][0]["where"] == "JA_Redacted.pdf, p. 1"
    assert p["sam_link"] == "https://sam.gov/opp/n1/view" and p["documents"][0]["filename"] == "JA_Redacted.pdf"
    assert e["notices"][0]["notice_id"] == "n1" and e["notices"][0]["pick_rank"] == 1
    assert e["body_markdown"].startswith("## 1. VA loses")
    rep = json.loads((tmp_path / "out" / "picks.json").read_text())
    assert rep["sent"] is True and rep["archive_file"].endswith("2026-10-21.json")
    # the rerun finds nothing new and sends nothing: still one file
    digest_run(tmp_path, args(tmp_path, "--send"), send_fn=ok_send)
    assert len(list((tmp_path / "digest_archive").glob("*.json"))) == 1


def test_archive_failure_is_loud_but_the_state_is_saved(tmp_path):
    class Broken:
        def save(self, entry):
            raise OSError("hub down")
    from federal_ja.digest import run as r
    from .test_digest import FakeReader, NOW, fake_model, pick
    with pytest.raises(OSError):
        r.run(args(tmp_path, "--send"), archive=Broken(), reader_factory=FakeReader, revision_fn=lambda: "rev1",
              call_fn=fake_model([pick()]), send_fn=ok_send, recent_fn=lambda: [], now=NOW)
    assert json.loads((tmp_path / "state.json").read_text())["last_sent_date_et"] == "2026-10-21"
    rep = json.loads((tmp_path / "out" / "picks.json").read_text())
    assert "hub down" in rep["archive_error"]
    # the entry is kept for `digest-archive-add`
    assert json.loads((tmp_path / "out" / "archive_entry.json").read_text())["buttondown_email_id"] == "em_ok1"


def test_choose_name_suffixes_and_never_overwrites():
    assert ar.choose_name({}, "2026-10-21", "em_1") == "2026-10-21.json"
    have = {"2026-10-21.json": "em_1", "2026-10-20.json": "em_0"}
    assert ar.choose_name(have, "2026-10-21", "em_1") is None           # already archived
    assert ar.choose_name(have, "2026-10-21", "em_2") == "2026-10-21-2.json"
    assert ar.choose_name({**have, "2026-10-21-2.json": "em_2"}, "2026-10-21", "em_3") == "2026-10-21-3.json"
    assert ar.choose_name(have, "2026-10-22", "em_0") == "2026-10-22.json"  # another day's id doesn't count
    with pytest.raises(ValueError):
        ar.choose_name({}, "../x", "em_1")


def test_local_archive_second_email_same_day(tmp_path):
    a = ar.LocalArchive(tmp_path / "arch")
    assert a.save(entry("2026-10-21", "em_1")).endswith("2026-10-21.json")
    assert a.save(entry("2026-10-21", "em_1")) is None
    assert a.save(entry("2026-10-21", "em_2")).endswith("2026-10-21-2.json")
    (tmp_path / "arch" / "notes.json").write_text("{}")                   # not an entry: ignored, said so
    assert [n for n, _ in a.load_all()] == ["2026-10-21.json", "2026-10-21-2.json"]


# ── rendering ──────────────────────────────────────────────────────────────

EVIL = '<script>alert("x")</script> & "quotes" \'apos\' ]]> <b>bold</b>'


def entry(date="2026-10-21", email_id="em_1", *, evil=False, n_picks=1, sent_at=None):
    t = EVIL if evil else "ICE definitizes a letter contract"
    picks = [{"rank": i + 1, "notice_id": f"n{i}", "headline": t, "why": t, "org": t, "vendor": t, "value": "$1",
              "authority": t, "posted_date": "2026-10-19", "award_number": t, "title": t,
              "sam_link": "javascript:alert(1)" if evil else f"https://sam.gov/opp/n{i}/view",
              "quotes": [{"text": t, "where": t, "added_by_code": False}],
              "documents": [{"filename": t, "url": "https://example.org/a.pdf?x=1&y=\"2\""}], "more_documents": 0}
             for i in range(n_picks)]
    notices = [{"notice_id": "n0", "pick_rank": 1, "org": t, "title": t, "vendor": None, "value": None,
                "authority": None, "sam_link": "https://sam.gov/opp/n0/view", "gist": t}]
    return ar.make_entry(subject=f"WOAH! J&As — {date}: {t}", body="## 1. x\n", date_et=date,
                         sent_at=sent_at or f"{date}T17:07:14+00:00", email_id=email_id, email_url=None,
                         dataset_revision="abc", digest_version="v", notices_reviewed=3,
                         window={"from_label": "Oct 20", "to_label": "Oct 21", "left_out_old": 2},
                         picks=picks, notices=notices)


def test_issue_page_escapes_untrusted_text_and_drops_unsafe_links():
    h = site.issue_page("2026-10-21.json", entry(evil=True), None, None)
    assert "<script>" not in h and "<b>bold</b>" not in h
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;" in h and "&#x27;apos&#x27;" in h
    assert "javascript:" not in h                                     # the bad link became plain text
    assert 'href="https://example.org' not in h                      # a URL with a quote in it is dropped
    assert 'href="https://sam.gov/opp/n0/view"' in h
    assert 'href="../index.html"' in h and 'href="../feed.xml"' in h


def test_index_newest_first_with_pick_counts(tmp_path):
    items = [("2026-10-20.json", entry("2026-10-20", n_picks=2)), ("2026-10-21.json", entry("2026-10-21")),
             ("2026-10-21-2.json", entry("2026-10-21", "em_2", n_picks=0))]
    files = site.build(items, tmp_path / "dist")
    assert files == ["404.html", "about.html", "feed.xml", "index.html", "issues/2026-10-20.html",
                     "issues/2026-10-21-2.html", "issues/2026-10-21.html", "style.css"]
    idx = (tmp_path / "dist" / "index.html").read_text()
    order = [idx.index(f"issues/{s}.html") for s in ("2026-10-21-2", "2026-10-21", "2026-10-20")]
    assert order == sorted(order)
    assert "2 picks" in idx and "0 picks" in idx and "1 pick<" in idx
    zero = (tmp_path / "dist" / "issues" / "2026-10-21-2.html").read_text()
    assert "Nothing WOAH-worthy today (3 new notices reviewed)" in zero
    assert 'href="2026-10-21.html">← Older' in zero
    about = (tmp_path / "dist" / "about.html").read_text()
    assert "AI-picked and AI-written (Claude)" in about and "word for word" in about
    assert render.DATASET_URL in about and render.PROJECT_URL in about


def test_build_refuses_a_directory_not_named_dist(tmp_path):
    (tmp_path / "keep.txt").write_text("x")
    with pytest.raises(SystemExit):
        site.build([], tmp_path)
    assert (tmp_path / "keep.txt").exists()


def test_rss_feed_is_valid_rss2_with_the_last_30_issues():
    items = [(f"2026-{m:02d}-{d:02d}.json", entry(f"2026-{m:02d}-{d:02d}", f"em_{m}_{d}"))
             for m in (9, 10) for d in range(1, 21)]
    items.append(("2026-10-20-2.json", entry("2026-10-20", "em_x", evil=True, sent_at="2026-10-20T22:00:00+00:00")))
    xml = site.feed_xml(items)
    root = ET.fromstring(xml)                     # well-formed, even with "]]>" and markup in the text
    assert root.tag == "rss" and root.get("version") == "2.0"
    ch = root.find("channel")
    for tag in ("title", "link", "description", "lastBuildDate"):
        assert (ch.findtext(tag) or "").strip(), tag
    its = ch.findall("item")
    assert len(its) == site.FEED_ITEMS
    assert its[0].findtext("link") == f"{render.SITE_URL}/issues/2026-10-20-2.html"
    assert its[0].findtext("title") == f"WOAH! J&As — 2026-10-20: {EVIL}"   # XML-escaped, round-trips
    for it in its:
        assert it.findtext("guid") == it.findtext("link") and it.findtext("link").startswith("https://")
        assert parsedate_to_datetime(it.findtext("pubDate")).tzinfo is not None
    # the description is HTML, escaped as XML text; the untrusted text inside it is escaped again
    assert "<script>" not in its[0].findtext("description")
    assert "&lt;script&gt;" in its[0].findtext("description")
    assert parsedate_to_datetime(ch.findtext("lastBuildDate")) == datetime(2026, 10, 20, 22, 0, tzinfo=timezone.utc)
    assert its[-1].findtext("link").endswith("2026-09-12.html")     # 30 newest: Sep 12 .. Oct 20 + the -2


def test_email_footer_links_the_archive():
    assert f"Archive & RSS: [{render.SITE_URL}]({render.SITE_URL})" in render.footer(1, {})
