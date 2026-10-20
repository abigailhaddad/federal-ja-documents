import datetime as dt

from federal_ja.collect import pipeline
from federal_ja.collect.sources import CollectResult, CollectStats
from federal_ja.collect.store import Store, file_rel_path, safe_filename


def notice(nid, fp="f1", posted="2025-03-01 00:00:00", src="active.csv"):
    return {"notice_id": nid, "title": "t", "posted_date": posted, "type": "Justification",
            "row_fingerprint": fp, "source_file": src, "source_files": [src]}


def result(*recs, exclusions=()):
    return CollectResult(notices={r["notice_id"]: r for r in recs}, exclusions=list(exclusions),
                         stats=CollectStats(start_date=dt.date(2025, 1, 20)))


def test_merge_new_changed_unchanged(tmp_path):
    s = Store(str(tmp_path))
    d = pipeline.merge_notices(s, result(notice("a"), notice("b")), now="T0")
    assert sorted(d["new"]) == ["a", "b"] and d["changed"] == []
    assert s.notices["a"]["resources_status"] == "pending"
    s.notices["a"]["resources_status"] = "ok"
    s.notices["b"]["resources_status"] = "ok"
    d = pipeline.merge_notices(s, result(notice("a"), notice("b", fp="f2", posted="2025-04-01 00:00:00",
                                                             src="FY2025.csv"), notice("c")), now="T1")
    assert d == {"new": ["c"], "changed": ["b"]}
    assert s.notices["a"]["resources_status"] == "ok"          # unchanged keeps state
    assert s.notices["b"]["resources_status"] == "pending"     # edited -> recheck
    assert s.notices["b"]["first_seen_at"] == "T0"
    assert s.notices["b"]["source_files"] == ["active.csv", "FY2025.csv"]


def test_merge_ignores_older_row(tmp_path):
    s = Store(str(tmp_path))
    pipeline.merge_notices(s, result(notice("a", posted="2025-05-01 00:00:00")))
    s.notices["a"]["resources_status"] = "ok"
    d = pipeline.merge_notices(s, result(notice("a", fp="old", posted="2025-04-01 00:00:00", src="FY.csv")))
    assert d["changed"] == []
    assert s.notices["a"]["row_fingerprint"] == "f1"
    assert s.notices["a"]["source_files"] == ["active.csv", "FY.csv"]


def test_merge_exclusions_dedup(tmp_path):
    s = Store(str(tmp_path))
    e = {"notice_id": "z", "source_file": "a.csv", "reason": "unparseable_posted_date",
         "posted_date": "x", "type": "Justification", "title": ""}
    pipeline.merge_notices(s, result(exclusions=[e]))
    pipeline.merge_notices(s, result(exclusions=[e]))
    assert len(s.exclusions) == 1


def att(rid, **kw):
    base = {"resource_id": rid, "attachment_id": "a" + rid, "resource_type": "file",
            "filename": f"{rid}.pdf", "link_url": "", "size": 10, "access_level": "public",
            "deleted_flag": False}
    base.update(kw)
    return base


def test_initial_download_status():
    assert pipeline.initial_download_status(att("1")) == "pending"
    assert pipeline.initial_download_status(att("1", resource_type="link")) == "link"
    assert pipeline.initial_download_status(att("1", deleted_flag=True)) == "deleted"
    assert pipeline.initial_download_status(att("1", access_level="private")) == "not_public"
    assert pipeline.initial_download_status(att("1", effective_deleted_date="2026-03-13T15:11:56Z")) == "deleted"


def test_apply_listing_tracks_changes(tmp_path):
    s = Store(str(tmp_path))
    pipeline.apply_resource_listing(s, "n", [att("1"), att("2", resource_type="link")])
    s.attachments[("n", "1")]["download_status"] = "ok"
    s.attachments[("n", "1")]["sha256"] = "abc"
    # second listing: res 1 now deleted on SAM, res 2 gone, res 3 new
    pipeline.apply_resource_listing(s, "n", [att("1", deleted_flag=True), att("3")])
    a1 = s.attachments[("n", "1")]
    assert a1["download_status"] == "ok" and a1["sha256"] == "abc" and a1["deleted_flag"] is True
    assert s.attachments[("n", "2")]["in_latest_listing"] is False
    assert s.attachments[("n", "3")]["download_status"] == "pending"


def test_private_then_public_becomes_pending(tmp_path):
    s = Store(str(tmp_path))
    pipeline.apply_resource_listing(s, "n", [att("1", access_level="private")])
    assert s.attachments[("n", "1")]["download_status"] == "not_public"
    pipeline.apply_resource_listing(s, "n", [att("1")])
    assert s.attachments[("n", "1")]["download_status"] == "pending"


class FakeSam:
    def __init__(self, listings, fail=()):
        self.listings, self.fail = listings, set(fail)

    def list_resources(self, nid):
        from federal_ja.collect.sam_api import SamError
        if nid in self.fail:
            raise SamError("resources HTTP 500", 500)
        return self.listings.get(nid, [])


def test_fetch_resources_statuses(tmp_path):
    s = Store(str(tmp_path))
    pipeline.merge_notices(s, result(notice("a"), notice("b"), notice("c")))
    out = pipeline.fetch_resources(s, FakeSam({"a": [att("1")]}, fail={"c"}), workers=2)
    assert out == {"ok": 1, "no_attachments": 1, "error": 1}
    assert s.notices["a"]["resources_status"] == "ok" and s.notices["a"]["attachment_count"] == 1
    assert s.notices["b"]["resources_status"] == "no_attachments"
    assert s.notices["c"]["resources_status"].startswith("error")
    # errors are retried only on request
    out = pipeline.fetch_resources(s, FakeSam({"c": [att("9")]}))
    assert out == {"ok": 0, "no_attachments": 0, "error": 0}
    out = pipeline.fetch_resources(s, FakeSam({"c": [att("9")]}), retry_errors=True)
    assert out["ok"] == 1


def test_store_roundtrip(tmp_path):
    s = Store(str(tmp_path))
    pipeline.merge_notices(s, result(notice("a")))
    pipeline.apply_resource_listing(s, "a", [att("1"), att("2", resource_type="link", link_url="https://x")])
    s.attachments[("a", "1")].update(download_status="ok", page_count=3, needs_transcription=True,
                                     transcription_cost_usd=0.0012, text="héllo",
                                     textless_pages=[2, 3])
    s.save()
    t = Store(str(tmp_path)).load()
    assert t.notices["a"]["source_files"] == ["active.csv"]
    a = t.attachments[("a", "1")]
    assert a["page_count"] == 3 and a["needs_transcription"] is True and a["text"] == "héllo"
    assert abs(a["transcription_cost_usd"] - 0.0012) < 1e-9
    assert a["textless_pages"] == [2, 3] and a["transcribed_pages"] is None
    assert t.attachments[("a", "2")]["link_url"] == "https://x"


def test_safe_filename():
    assert safe_filename("J&A Redacted (final).pdf") == "J_A_Redacted_final_.pdf"
    assert safe_filename("../../etc/passwd") == "etc_passwd"
    assert safe_filename("résumé.PDF") == "resume.PDF"
    assert safe_filename("") == "file"
    long = "a" * 300 + ".pdf"
    out = safe_filename(long)
    assert len(out) == 120 and out.endswith(".pdf")
    assert file_rel_path("n1", "res1", "x y.pdf") == "files/n1/res1_x_y.pdf"


class FakeTranscriber:
    def __init__(self, budget_after=None):
        self.n = 0
        self.budget_after = budget_after
        self.calls = []

    def transcribe_pdf(self, path, page_count, pages=None):
        from federal_ja.collect.transcribe import BudgetExceeded, TranscriptionResult
        self.n += 1
        self.calls.append((page_count, pages))
        if self.budget_after is not None and self.n > self.budget_after:
            raise BudgetExceeded("cap")
        return TranscriptionResult(text="", model="m", prompt_version="v1", pages=len(pages),
                                   input_tokens=100, output_tokens=10, cost_usd=0.001,
                                   page_texts={p: "[REDACTED]" for p in pages})


def _pdf_row(s, tmp_path, rid, data, fname):
    from federal_ja.collect import extract as ex
    (tmp_path / "files").mkdir(exist_ok=True)
    (tmp_path / "files" / fname).write_bytes(data)
    a = {"notice_id": "n", "resource_id": rid, "download_status": "ok", "sha256": "h" + rid,
         "file_path": f"files/{fname}", "filename": fname}
    s.attachments[("n", rid)] = a
    pipeline.apply_extraction(a, ex.extract_file(str(tmp_path / "files" / fname), fname), s)
    return a


def test_page_rows_and_doc_text(tmp_path):
    from tests.test_extract import LONG, text_pdf
    s = Store(str(tmp_path))
    a = _pdf_row(s, tmp_path, "1", text_pdf([LONG, "short header"]), "m.pdf")
    rows = s.pages[("n", "1")]
    assert [r["page_number"] for r in rows] == [1, 2]
    assert rows[0]["text_source"] == "native" and not rows[0]["needs_transcription"]
    assert rows[1]["needs_transcription"] and rows[1]["text_layer_chars"] == len("short header")
    assert rows[0]["sha256"] == "h1"
    assert a["text_source"] == "pdf_text" and a["needs_transcription"] and a["textless_pages"] == [2]
    s.save()
    t = Store(str(tmp_path)).load()
    assert len(t.pages[("n", "1")]) == 2 and t.pages[("n", "1")][1]["needs_transcription"] is True


def test_transcribe_pending_merges_and_stops_on_budget(tmp_path):
    from tests.test_extract import LONG, blank_pdf, text_pdf
    s = Store(str(tmp_path))
    m = _pdf_row(s, tmp_path, "1", text_pdf([LONG, "x"]), "mixed.pdf")
    sc = _pdf_row(s, tmp_path, "2", blank_pdf(2), "scan.pdf")
    _pdf_row(s, tmp_path, "3", blank_pdf(1), "scan2.pdf")
    _pdf_row(s, tmp_path, "4", text_pdf([LONG, LONG]), "typed.pdf")
    assert len(pipeline.transcription_candidates(s)) == 3
    ft = FakeTranscriber(budget_after=2)
    out = pipeline.transcribe_pending(s, ft)
    assert out["done"] == 2 and out["stopped_on_budget"] is True and out["pages"] == 3
    assert ft.calls[0] == (2, [2]) and ft.calls[1] == (2, [1, 2])
    assert m["text_source"] == "pdf_text+transcription" and m["transcribed_pages"] == [2]
    assert "Justification for other" in m["text"] and "--- page 2 ---\n[REDACTED]" in m["text"]
    p2 = s.pages[("n", "1")][1]
    assert p2["text_source"] == "transcription" and p2["text_layer_text"] == "x" and p2["transcription_model"] == "m"
    assert s.pages[("n", "1")][0]["text_source"] == "native"
    assert sc["text_source"] == "transcription"
    assert len(pipeline.transcription_candidates(s)) == 1
    # re-extraction keeps the transcription of pages that still need one
    from federal_ja.collect import extract as ex
    pipeline.apply_extraction(m, ex.extract_file(str(tmp_path / "files/mixed.pdf"), "mixed.pdf"), s)
    assert s.pages[("n", "1")][1]["text"] == "[REDACTED]" and m["text_source"] == "pdf_text+transcription"


def test_transcribe_fetches_missing_file_and_deletes_it(tmp_path):
    from tests.test_extract import blank_pdf
    s = Store(str(tmp_path))
    a = _pdf_row(s, tmp_path, "1", blank_pdf(1), "scan.pdf")
    src = tmp_path / "files" / "scan.pdf"
    data = src.read_bytes()
    src.unlink()
    fetched = []

    def fetch(row):
        p = tmp_path / "fetched.pdf"
        p.write_bytes(data)
        fetched.append(row["resource_id"])
        return str(p)
    out = pipeline.transcribe_pending(s, FakeTranscriber(), fetch=fetch)
    assert out["done"] == 1 and fetched == ["1"] and not (tmp_path / "fetched.pdf").exists()
    assert a["text_source"] == "transcription"


def test_pages_from_stored_text(tmp_path):
    s = Store(str(tmp_path))
    s.attachments[("n", "1")] = {"notice_id": "n", "resource_id": "1", "download_status": "ok",
                                 "file_kind": "pdf", "page_count": 3, "sha256": "h",
                                 "text": "--- page 1 ---\n" + "a" * 400 + "\n\n--- page 2 ---\nhdr\n\n--- page 3 ---\n" + "b" * 250,
                                 "text_source": "pdf_text"}
    s.attachments[("n", "2")] = {"notice_id": "n", "resource_id": "2", "download_status": "ok",
                                 "file_kind": "pdf", "page_count": 2, "text": "", "text_source": "none"}
    out = pipeline.pages_from_stored_text(s)
    assert out == {"pdfs": 2, "pages": 5, "no_text_pdfs": 1, "marker_mismatch": 0}
    a = s.attachments[("n", "1")]
    assert a["textless_pages"] == [2, 3]   # 250 chars < 300 threshold
    assert [r["text_layer_chars"] for r in s.pages[("n", "1")]] == [400, 3, 250]
    assert s.attachments[("n", "2")]["textless_pages"] == [1, 2]


def test_extraction_never_overwrites_transcription():
    from federal_ja.collect.extract import ExtractResult
    a = {"text": "T", "text_source": "transcription"}
    pipeline.apply_extraction(a, ExtractResult(file_kind="docx", needs_transcription=True))
    assert a["text"] == "T" and a["text_source"] == "transcription"


def test_batches_keep_notices_together():
    from federal_ja.collect.cli import _batches
    rows = [{"notice_id": n, "resource_id": r, "size": 10} for n, r in
            [("a", "1"), ("a", "2"), ("a", "3"), ("b", "1"), ("c", "1"), ("c", "2")]]
    out = list(_batches(rows, max_files=2, max_bytes=10**9, size_key="size"))
    assert [[(r["notice_id"], r["resource_id"]) for r in b] for b in out] == [
        [("a", "1"), ("a", "2"), ("a", "3")], [("b", "1"), ("c", "1"), ("c", "2")]]
    assert sum(len(b) for b in out) == len(rows)


def test_upload_verify_delete(tmp_path):
    from types import SimpleNamespace
    from federal_ja.collect import hf_sync
    s = Store(str(tmp_path))
    for rid, body in (("1", b"aaa"), ("2", b"bbbb")):
        rel = f"files/n/{rid}_x.pdf"
        (tmp_path / "files" / "n").mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(body)
        import hashlib
        s.attachments[("n", rid)] = {"notice_id": "n", "resource_id": rid, "file_path": rel,
                                     "downloaded_size": len(body), "sha256": hashlib.sha256(body).hexdigest(),
                                     "download_status": "ok"}

    class Api:
        def __init__(self, bad=False):
            self.bad = bad
        def create_commit(self, *a, **k):
            return SimpleNamespace(oid="c1")
        def get_paths_info(self, repo, paths, repo_type):
            out = []
            for p in paths:
                r = next(a for a in s.attachments.values() if a["file_path"] == p)
                size = r["downloaded_size"] + (1 if self.bad else 0)
                out.append(SimpleNamespace(path=p, size=size, lfs=SimpleNamespace(sha256=r["sha256"])))
            return out

    rows = list(s.attachments.values())
    import pytest
    with pytest.raises(hf_sync.UploadVerifyError):
        hf_sync.upload_verify_delete(s, rows, "T", api=Api(bad=True))
    assert (tmp_path / "files/n/1_x.pdf").exists()            # nothing deleted on a failed verify
    res = hf_sync.upload_verify_delete(s, rows, "T", api=Api(), keep_local={"files/n/2_x.pdf"})
    assert res["uploaded"] == 2 and res["commit"] == "c1"
    assert not (tmp_path / "files/n/1_x.pdf").exists() and (tmp_path / "files/n/2_x.pdf").exists()
    assert s.attachments[("n", "1")]["uploaded_at"] == "T"
    assert len((tmp_path / "data" / "upload_manifest.jsonl").read_text().splitlines()) == 2


def test_store_survives_lone_surrogates(tmp_path):
    from federal_ja.collect.extract import clean_text
    assert clean_text("a\udfc1b") == "a�b"
    s = Store(str(tmp_path))
    s.attachments[("n", "1")] = {"notice_id": "n", "resource_id": "1", "text": "x\udfc1y"}
    s.save()
    assert Store(str(tmp_path)).load().attachments[("n", "1")]["text"] == "x�y"


def test_transcribe_pending_parallel(tmp_path):
    from tests.test_extract import blank_pdf
    s = Store(str(tmp_path))
    for i in range(12):
        _pdf_row(s, tmp_path, str(i), blank_pdf(2), f"s{i}.pdf")
    out = pipeline.transcribe_pending(s, FakeTranscriber(), workers=4)
    assert out["done"] == 12 and out["pages"] == 24 and out["errors"] == 0
    assert all(a["text_source"] == "transcription" for a in s.attachments.values())
    assert pipeline.transcription_candidates(s) == []


def test_transcribe_pending_parallel_budget_stop(tmp_path):
    from tests.test_extract import blank_pdf
    import threading
    s = Store(str(tmp_path))
    for i in range(30):
        _pdf_row(s, tmp_path, str(i), blank_pdf(1), f"s{i}.pdf")

    class T(FakeTranscriber):
        lock = threading.Lock()
        def transcribe_pdf(self, *a, **k):
            with self.lock:
                return super().transcribe_pdf(*a, **k)
    out = pipeline.transcribe_pending(s, T(budget_after=5), workers=3)
    assert out["stopped_on_budget"] and out["done"] == 5
    assert len(pipeline.transcription_candidates(s)) == 25


def test_cli_parses_every_command(tmp_path, capsys):
    import pytest
    from federal_ja.collect import cli
    for cmd in ("scan", "resources", "download", "stream", "transcribe", "report", "publish",
                "daily", "pages", "reextract", "reextract_remote", "links", "recheck_pages", "pull"):
        with pytest.raises(SystemExit) as e:
            cli.main(["--workdir", str(tmp_path), cmd, "--help"])
        assert e.value.code == 0


def test_missing_strings_stay_missing(tmp_path):
    s = Store(str(tmp_path))
    s.attachments[("n", "1")] = {"notice_id": "n", "resource_id": "1", "download_error": None}
    s.attachments[("n", "2")] = {"notice_id": "n", "resource_id": "2", "download_error": "boom"}
    s.save()
    import pandas as pd
    d = pd.read_parquet(tmp_path / "data" / "attachments.parquet")
    assert d.download_error.isna().sum() == 1 and (d.download_error == "nan").sum() == 0
    t = Store(str(tmp_path)).load()
    assert t.attachments[("n", "1")]["download_error"] is None


def test_reset_multipage_transcriptions(tmp_path):
    from tests.test_extract import LONG, blank_pdf, text_pdf
    s = Store(str(tmp_path))
    _pdf_row(s, tmp_path, "1", text_pdf([LONG, "thin", "x"]), "m.pdf")
    _pdf_row(s, tmp_path, "2", blank_pdf(1), "one.pdf")
    pipeline.transcribe_pending(s, FakeTranscriber())
    out = pipeline.reset_multipage_transcriptions(s)
    assert out == {"docs": 1, "pages": 2}
    rows = s.pages[("n", "1")]
    assert rows[1]["text"] == "thin" and rows[1]["text_source"] == "native"
    assert rows[2]["text"] == "x" and rows[2]["transcription_model"] is None
    assert s.pages[("n", "2")][0]["text_source"] == "transcription"   # single page kept
    assert [a["resource_id"] for a in pipeline.transcription_candidates(s)] == ["1"]


def test_default_is_one_page_per_request():
    from federal_ja.collect import config
    assert config.TRANSCRIBE_PAGES_PER_CHUNK == 1


def test_declined_files_are_not_retried_by_default(tmp_path):
    from tests.test_extract import blank_pdf
    s = Store(str(tmp_path))
    a = _pdf_row(s, tmp_path, "1", blank_pdf(1), "a.pdf")
    b = _pdf_row(s, tmp_path, "2", blank_pdf(1), "b.pdf")
    a["transcription_error"] = "refusal"
    b["transcription_error"] = "APIStatusError: Output blocked by content filtering policy"
    _pdf_row(s, tmp_path, "3", blank_pdf(1), "c.pdf")["transcription_error"] = "RemoteProtocolError: x"
    assert [x["resource_id"] for x in pipeline.transcription_candidates(s)] == ["3"]
    assert len(pipeline.transcription_candidates(s, retry_declined=True)) == 3


def test_store_fills_collection_reason_for_older_tables(tmp_path):
    # Tables written before collection_reason existed: derived on load from
    # the row's own type and title, and written back on save.
    import pandas as pd
    s = Store(str(tmp_path))
    s.notices["a"] = notice("a")
    s.exclusions.append({"notice_id": "z", "source_file": "a.csv", "reason": "unparseable_posted_date",
                         "posted_date": "x", "type": "Justification", "title": ""})
    s.save()
    for name in ("notices", "exclusions"):
        p = tmp_path / "data" / f"{name}.parquet"
        df = pd.read_parquet(p)
        df.drop(columns=["collection_reason"]).to_parquet(p, index=False)
    s2 = Store(str(tmp_path)).load()
    assert s2.notices["a"]["collection_reason"] == "justification_type"
    assert s2.exclusions[0]["collection_reason"] == "justification_type"
    s2.save()
    assert pd.read_parquet(tmp_path / "data" / "notices.parquet").collection_reason.tolist() == ["justification_type"]
