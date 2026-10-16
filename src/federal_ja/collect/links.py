"""Fetch J&A documents that SAM lists as off-site LINK resources.

Only two hosts are fetched, both of which hold the J&A document itself:

* www.neco.navy.mil ``/synopsis_file/...`` PDFs (Navy/DLA). The server does
  not send its intermediate certificate, so normal TLS verification fails.
  We add that one intermediate (DigiCert Global G2 TLS RSA SHA256 2020 CA1,
  shipped in ``certs/`` and pinned by SHA-256 below) to the standard CA
  bundle. Verification is never turned off.
* piee.eb.mil ``oppMgmtLink.xhtml?...noticeType=JustificationAndApproval``
  public notice pages. No login is needed: the page lists attachments, and
  each one is returned by posting the page's JSF form back with the
  attachment's control id, ``attachmentIndex`` and the page ViewState.

Every other link (agency home pages, PIEE vendor guides on acq.osd.mil,
training sites, ...) is recorded but not fetched.
"""

from __future__ import annotations

import hashlib
import html as htmllib
import os
import re
import tempfile
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import requests

from . import config

CERT_DIR = os.path.join(os.path.dirname(__file__), "certs")
NECO_INTERMEDIATE = os.path.join(CERT_DIR, "DigiCertGlobalG2TLSRSASHA2562020CA1.pem")
# SHA-256 of the DER certificate. Checked against crt.sh, and its signature
# verifies against DigiCert Global Root G2 from the certifi bundle.
NECO_INTERMEDIATE_SHA256 = "c8025f9fc65fdfc95b3ca8cc7867b9a587b5277973957917463fc813d0b625a9"

PIEE_FORM_URL = "https://piee.eb.mil/sol/xhtml/unauth/search/viewPublicNotice.xhtml"
DOC_MAGIC = (b"%PDF", b"PK\x03\x04", b"\xd0\xcf\x11\xe0", b"\x89PNG", b"\xff\xd8\xff")


class LinkFetchError(Exception):
    pass


def source_host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower()


def link_kind(url: str) -> str | None:
    """'neco', 'piee_ja', or None (not fetched)."""
    u = urlparse(url or "")
    host = (u.hostname or "").lower()
    if host == "www.neco.navy.mil" and u.path.lower().startswith("/synopsis_file/"):
        return "neco"
    if host == "piee.eb.mil" and u.path.endswith("/oppMgmtLink.xhtml"):
        qs = parse_qs(u.query)
        if qs.get("noticeType", [""])[0] == "JustificationAndApproval":
            return "piee_ja"
    return None


def check_intermediate(path: str = NECO_INTERMEDIATE) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes

    cert = x509.load_pem_x509_certificate(open(path, "rb").read())
    fp = cert.fingerprint(hashes.SHA256()).hex()
    if fp != NECO_INTERMEDIATE_SHA256:
        raise LinkFetchError(f"bundled intermediate fingerprint {fp} != pinned value")


_bundle_path: str | None = None


def ca_bundle() -> str:
    """certifi's roots plus the pinned NECO intermediate, as a temp file."""
    global _bundle_path
    if _bundle_path and os.path.exists(_bundle_path):
        return _bundle_path
    import certifi

    check_intermediate()
    fd, p = tempfile.mkstemp(prefix="ja-ca-", suffix=".pem")
    with os.fdopen(fd, "wb") as f:
        f.write(open(certifi.where(), "rb").read())
        f.write(b"\n")
        f.write(open(NECO_INTERMEDIATE, "rb").read())
    _bundle_path = p
    return p


def looks_like_document(data: bytes) -> bool:
    return data[:8].startswith(DOC_MAGIC) or b"%PDF-" in data[:1024]


def filename_from_disposition(cd: str) -> str:
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd or "", re.I)
    if m:
        from urllib.parse import unquote
        return unquote(m.group(1)).strip('"')
    m = re.search(r'filename="?([^";]+)"?', cd or "", re.I)
    return m.group(1).strip() if m else ""


@dataclass
class FetchedDoc:
    name: str
    data: bytes
    content_type: str
    fetched_url: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": config.USER_AGENT})
    return s


def fetch_neco(url: str, session: requests.Session | None = None) -> list[FetchedDoc]:
    s = session or _session()
    r = s.get(url, timeout=(20, 120), verify=ca_bundle())
    if r.status_code != 200:
        raise LinkFetchError(f"neco HTTP {r.status_code}")
    if not looks_like_document(r.content):
        raise LinkFetchError(f"neco: not a document ({r.headers.get('Content-Type', '')})")
    name = filename_from_disposition(r.headers.get("Content-Disposition", "")) or \
        os.path.basename(urlparse(url).path)
    return [FetchedDoc(name, r.content, r.headers.get("Content-Type", ""), url)]


_VIEWSTATE = re.compile(r'name="javax\.faces\.ViewState"[^>]*value="([^"]*)"')
# <td>NAME</td> <td>...</td> <td><a ... submitForm(\'form\',\'CONTROL\',null,[[\'attachmentIndex\',\'N\']]) ...>
_ATTACH_ROW = re.compile(
    r"<td>([^<]*)</td>\s*<td>[^<]*</td>\s*<td><a [^>]*?viewAttachment\(\);'[^>]*?"
    r"submitForm\(\\'form\\',\\'([^\\]+)\\',null,\[\[\\'attachmentIndex\\',\\'(\d+)\\'\]\]",
    re.S)


def parse_piee_page(page: str) -> tuple[str | None, list[tuple[str, str, str]]]:
    """-> (ViewState, [(attachment name, control id, attachmentIndex), ...])."""
    vs = _VIEWSTATE.search(page)
    rows = [(htmllib.unescape(n).strip(), c, i) for n, c, i in _ATTACH_ROW.findall(page)]
    return (htmllib.unescape(vs.group(1)) if vs else None), rows


def fetch_piee(url: str, session: requests.Session | None = None,
               pause: float = 1.0) -> list[FetchedDoc]:
    s = session or _session()
    r = s.get(url, timeout=(20, 120))
    if r.status_code != 200:
        raise LinkFetchError(f"piee page HTTP {r.status_code}")
    vs, rows = parse_piee_page(r.text)
    if not rows:
        raise LinkFetchError("piee: no attachments listed on the notice page")
    if not vs:
        raise LinkFetchError("piee: no ViewState on the notice page")
    out = []
    for name, control, idx in rows:
        time.sleep(pause)
        resp = s.post(PIEE_FORM_URL, timeout=(20, 180), data={
            "form_SUBMIT": "1", "javax.faces.ViewState": vs, "form:_idcl": control,
            "attachmentIndex": idx, "currentTab": "homeTab"})
        if resp.status_code != 200 or not looks_like_document(resp.content):
            raise LinkFetchError(f"piee attachment {idx}: HTTP {resp.status_code}, "
                                 f"{resp.headers.get('Content-Type', '')}")
        fname = filename_from_disposition(resp.headers.get("Content-Disposition", "")) or name
        out.append(FetchedDoc(fname, resp.content, resp.headers.get("Content-Type", ""), url))
    return out


def fetch_link(url: str, session: requests.Session | None = None) -> list[FetchedDoc]:
    kind = link_kind(url)
    if kind == "neco":
        return fetch_neco(url, session)
    if kind == "piee_ja":
        return fetch_piee(url, session)
    raise LinkFetchError("not a fetchable link host")
