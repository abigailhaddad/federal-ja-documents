import pytest

from federal_ja.collect import links as lk
from federal_ja.collect import pipeline
from federal_ja.collect.store import Store

PIEE_PAGE = """<form id="form" name="form" method="post" action="/sol/xhtml/unauth/search/viewPublicNotice.xhtml">
<tbody>
  <tr>
    <td>JA_UESC_25_CPEN_REDACTED</td>
    <td></td>

    <td><a href="#" onclick="return jsf.util.chain(this, event,'viewAttachment();', 'return myfaces.oam.submitForm(\\'form\\',\\'j_id_v_1_47:0:j_id_v_1_4a\\',null,[[\\'attachmentIndex\\',\\'0\\']]);');" id="x">JA_UESC</a></td>
  </tr>
  <tr>
    <td>Second &amp; Final</td>
    <td>http://x</td>
    <td><a href="#" onclick="return jsf.util.chain(this, event,'viewAttachment();', 'return myfaces.oam.submitForm(\\'form\\',\\'j_id_v_1_47:1:j_id_v_1_4a\\',null,[[\\'attachmentIndex\\',\\'1\\']]);');" id="y">2</a></td>
  </tr>
</tbody>
<a onclick="return myfaces.oam.submitForm('form','downloadAllAttachments');">all</a>
<input type="hidden" name="javax.faces.ViewState" id="j_id__v_0:javax.faces.ViewState:1" value="abc&#43;123==" autocomplete="off" />
</form>"""


def test_link_kind():
    assert lk.link_kind("https://www.neco.navy.mil/synopsis_file/N00104_JA_Redacted.pdf") == "neco"
    assert lk.link_kind("https://www.neco.navy.mil/") is None
    assert lk.link_kind("https://piee.eb.mil/sol/xhtml/unauth/search/oppMgmtLink.xhtml"
                        "?noticeId=N6247326F0173&noticeType=JustificationAndApproval") == "piee_ja"
    assert lk.link_kind("https://piee.eb.mil/sol/xhtml/unauth/search/oppMgmtLink.xhtml"
                        "?noticeId=X&noticeType=Presolicitation") is None
    # vendor-guide boilerplate and other hosts are never fetched
    assert lk.link_kind("https://www.acq.osd.mil/asda/dpc/ce/cap/docs/piee/PIEE_Solicitation_Module_Vendor_Access_Instructions.pdf") is None
    assert lk.link_kind("https://www.va.gov/") is None
    assert lk.link_kind("") is None
    assert lk.source_host("https://WWW.Neco.navy.mil/a") == "www.neco.navy.mil"


def test_parse_piee_page():
    vs, rows = lk.parse_piee_page(PIEE_PAGE)
    assert vs == "abc+123=="
    assert rows == [("JA_UESC_25_CPEN_REDACTED", "j_id_v_1_47:0:j_id_v_1_4a", "0"),
                    ("Second & Final", "j_id_v_1_47:1:j_id_v_1_4a", "1")]
    assert lk.parse_piee_page("<html></html>") == (None, [])


def test_pinned_intermediate():
    lk.check_intermediate()          # shipped PEM matches the pinned fingerprint
    bundle = open(lk.ca_bundle(), "rb").read()
    assert bundle.count(b"BEGIN CERTIFICATE") > 100  # certifi roots are still there


def test_pinned_intermediate_rejects_other_cert(tmp_path):
    import certifi
    pem = open(certifi.where(), "rb").read()
    first = pem[: pem.index(b"-----END CERTIFICATE-----") + 25]
    p = tmp_path / "x.pem"
    p.write_bytes(first[first.index(b"-----BEGIN"):])
    with pytest.raises(lk.LinkFetchError):
        lk.check_intermediate(str(p))


def test_filename_and_document_checks():
    assert lk.filename_from_disposition('attachment; filename="JA 1.pdf"') == "JA 1.pdf"
    assert lk.filename_from_disposition("attachment; filename*=UTF-8''J%26A.pdf") == "J&A.pdf"
    assert lk.filename_from_disposition("") == ""
    assert lk.looks_like_document(b"%PDF-1.7 ...")
    assert not lk.looks_like_document(b"<!DOCTYPE html><html>")


class FakeResp:
    def __init__(self, status, content=b"", text="", headers=None):
        self.status_code, self.content, self.text, self.headers = status, content, text, headers or {}


class FakeSession:
    def __init__(self, get, posts):
        self._get, self._posts, self.posted = get, list(posts), []

    def get(self, url, **kw):
        return self._get

    def post(self, url, data=None, **kw):
        self.posted.append(data)
        return self._posts.pop(0)


def test_fetch_piee_posts_each_attachment():
    s = FakeSession(FakeResp(200, text=PIEE_PAGE),
                    [FakeResp(200, b"%PDF-1 a", headers={"Content-Disposition": 'attachment; filename="a.pdf"'}),
                     FakeResp(200, b"%PDF-1 b")])
    docs = lk.fetch_piee("https://piee.eb.mil/x", session=s, pause=0)
    assert [d.name for d in docs] == ["a.pdf", "Second & Final"]
    assert s.posted[0]["javax.faces.ViewState"] == "abc+123==" and s.posted[1]["attachmentIndex"] == "1"
    assert s.posted[0]["form:_idcl"] == "j_id_v_1_47:0:j_id_v_1_4a"


def test_fetch_piee_rejects_html_response():
    s = FakeSession(FakeResp(200, text=PIEE_PAGE), [FakeResp(200, b"<html>error</html>")])
    with pytest.raises(lk.LinkFetchError):
        lk.fetch_piee("https://piee.eb.mil/x", session=s, pause=0)


def test_fetch_links_rows(tmp_path):
    from tests.test_extract import LONG, text_pdf
    s = Store(str(tmp_path))
    base = {"notice_id": "n", "resource_type": "link", "download_status": "link", "attachment_order": 1}
    s.attachments[("n", "neco")] = dict(base, resource_id="neco",
                                        link_url="https://www.neco.navy.mil/synopsis_file/a.pdf")
    s.attachments[("n", "piee")] = dict(base, resource_id="piee",
                                        link_url="https://piee.eb.mil/sol/xhtml/unauth/search/oppMgmtLink.xhtml?noticeId=A&noticeType=JustificationAndApproval")
    s.attachments[("n", "bad")] = dict(base, resource_id="bad",
                                       link_url="https://www.neco.navy.mil/synopsis_file/missing.pdf")
    s.attachments[("n", "va")] = dict(base, resource_id="va", link_url="https://www.va.gov/")
    pdf = text_pdf([LONG])

    def fetcher(url):
        if "missing" in url:
            raise lk.LinkFetchError("neco HTTP 404")
        if "neco" in url:
            return [lk.FetchedDoc("a.pdf", pdf, "application/pdf", url)]
        return [lk.FetchedDoc("one.pdf", pdf, "", url), lk.FetchedDoc("two.pdf", pdf, "", url)]

    out = pipeline.fetch_links(s, fetcher=fetcher, pause=0)
    assert out == {"ok": 3, "link_error": 1}
    a = s.attachments[("n", "neco")]
    assert a["download_status"] == "ok" and a["source_host"] == "www.neco.navy.mil"
    assert a["text_source"] == "pdf_text" and a["file_path"] == "files/n/neco_a.pdf"
    assert s.attachments[("n", "piee-2")]["parent_resource_id"] == "piee"
    assert s.attachments[("n", "piee-2")]["file_path"] == "files/n/piee-2_two.pdf"
    assert s.attachments[("n", "bad")]["download_status"] == "link_error"
    assert s.attachments[("n", "va")]["download_status"] == "link"   # never fetched
    assert ("n", "piee") in s.pages and ("n", "piee-2") in s.pages
