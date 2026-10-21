"""The public archive of SENT digests: ``analysis/digest_archive/YYYY-MM-DD.json``
in the Hugging Face dataset, one file per email (2026-10-21).

The web archive and RSS feed (site.py, https://woah-jas.abigailhaddad.com) are
built only from these files, so the site shows exactly what went out.

* Written by run.py right after the state file, in the same post-send step,
  and only after Buttondown accepted the email with ``about_to_send``. A dry
  run, a draft, a failed send or a skipped run writes nothing here.
* The name is the digest's Eastern-time date. A second email on the same day
  gets ``-2`` (then ``-3``...). An existing file is never overwritten: if a
  file for that day already holds the same Buttondown email id, the entry is
  already archived and nothing is written (a rerun is a no-op).
* Atomic: one ``upload_file`` is one dataset commit, made with
  ``parent_commit`` = the commit the name was chosen at, so two writers
  racing for the same name cannot both win (the loser re-lists and retries).
* Under ``analysis/``, which the collector's ``publish --delete-stale``
  never deletes (``KEEP_PREFIXES`` in collect/hf_sync.py).

What an entry holds (all public; it is what the email said): subject, date,
sent_at, the Buttondown email id and URL, the dataset commit, the Markdown
body exactly as sent, the picks as display data (render.pick_view) and the
"All new notices" rows (render.notice_rows). If the archive write fails, run.py
has also written the entry to ``archive_entry.json`` under --out (in the
workflow's artifact); ``python -m federal_ja.analysis digest-archive-add`` uploads
it with the same rules.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from ..analysis.data import HF_DATASET

ARCHIVE_DIR = "analysis/digest_archive"
ENTRY_VERSION = 1
NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:-(\d+))?\.json$")


def make_entry(*, subject: str, body: str, date_et: str, sent_at: str, email_id: str | None,
               email_url: str | None, dataset_revision: str | None, digest_version: str | None,
               notices_reviewed: int, window: dict, picks: list[dict], notices: list[dict]) -> dict:
    return {"version": ENTRY_VERSION, "date": date_et, "subject": subject, "sent_at": sent_at,
            "buttondown_email_id": email_id, "buttondown_url": email_url,
            "dataset_revision": dataset_revision, "digest_version": digest_version,
            "notices_reviewed": notices_reviewed, "window": window, "picks": picks, "notices": notices,
            "body_markdown": body}


def choose_name(existing: dict[str, str | None], date: str, email_id: str | None) -> str | None:
    """The file name for a new entry, or None if this email is already archived.

    ``existing``: {file name: its buttondown_email_id} for the files already
    there (only that day's matter)."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date or ""):
        raise ValueError(f"bad archive date {date!r}")
    same_day = {n: e for n, e in existing.items() if (m := NAME_RE.match(n)) and m.group(1) == date}
    if email_id and email_id in same_day.values():
        return None
    for k in range(1, 1000):
        name = f"{date}.json" if k == 1 else f"{date}-{k}.json"
        if name not in existing:
            return name
    raise RuntimeError(f"more than 999 archive entries for {date}")


def sort_key(name: str) -> tuple[str, int]:
    m = NAME_RE.match(name)
    return (m.group(1), int(m.group(2) or 1)) if m else ("", 0)


def _split_names(names) -> tuple[list[str], list[str]]:
    good = sorted((n for n in names if NAME_RE.match(n)), key=sort_key)
    bad = sorted(n for n in names if not NAME_RE.match(n))
    if bad:
        # never a silent drop: say what was not read
        print(f"archive: ignoring {len(bad)} file(s) not named YYYY-MM-DD[-N].json: {bad}", file=sys.stderr)
    return good, bad


class LocalArchive:
    """A local directory instead (tests, previews, and the site builder's --archive-dir)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _existing(self) -> dict[str, str | None]:
        if not self.path.is_dir():
            return {}
        return {p.name: json.loads(p.read_text()).get("buttondown_email_id")
                for p in self.path.glob("*.json") if NAME_RE.match(p.name)}

    def save(self, entry: dict) -> str | None:
        name = choose_name(self._existing(), entry["date"], entry.get("buttondown_email_id"))
        if name is None:
            return None
        self.path.mkdir(parents=True, exist_ok=True)
        tmp = self.path / f".{name}.tmp"
        tmp.write_text(json.dumps(entry, indent=1, ensure_ascii=False))
        tmp.replace(self.path / name)          # atomic on one filesystem
        return str(self.path / name)

    def load_all(self) -> list[tuple[str, dict]]:
        if not self.path.is_dir():
            return []
        good, _ = _split_names(p.name for p in self.path.glob("*.json"))
        return [(n, json.loads((self.path / n).read_text())) for n in good]


class HFArchive:
    """The dataset's analysis/digest_archive/ (writing needs HF_TOKEN)."""

    def __init__(self, repo_id: str = HF_DATASET):
        self.repo_id = repo_id

    def _head(self, api) -> str:
        return api.dataset_info(self.repo_id, revision="main").sha

    def _names(self, api, sha: str) -> list[str]:
        from huggingface_hub.errors import EntryNotFoundError
        try:
            items = api.list_repo_tree(self.repo_id, path_in_repo=ARCHIVE_DIR, repo_type="dataset", revision=sha)
            return [i.path.rsplit("/", 1)[-1] for i in items if i.path.endswith(".json")]
        except EntryNotFoundError:          # no archive yet
            return []

    def _read(self, name: str, sha: str) -> dict:
        from huggingface_hub import hf_hub_download
        p = hf_hub_download(self.repo_id, f"{ARCHIVE_DIR}/{name}", repo_type="dataset", revision=sha)
        return json.loads(Path(p).read_text())

    def save(self, entry: dict, attempts: int = 5) -> str | None:
        from huggingface_hub import HfApi
        from huggingface_hub.errors import HfHubHTTPError
        api = HfApi()
        for i in range(attempts):
            sha = self._head(api)
            names = self._names(api, sha)
            existing = {n: None for n in names}
            for n in names:      # only the same day's files are read (for their email id)
                if NAME_RE.match(n) and n.startswith(entry["date"]):
                    existing[n] = self._read(n, sha).get("buttondown_email_id")
            name = choose_name(existing, entry["date"], entry.get("buttondown_email_id"))
            if name is None:
                return None
            try:
                info = api.upload_file(path_or_fileobj=json.dumps(entry, indent=1, ensure_ascii=False).encode(),
                                       path_in_repo=f"{ARCHIVE_DIR}/{name}", repo_id=self.repo_id,
                                       repo_type="dataset", parent_commit=sha,
                                       commit_message=f"digest archive: {name} {entry.get('subject', '')[:80]}")
                return str(getattr(info, "commit_url", info))
            except HfHubHTTPError:
                # parent_commit is stale when another job committed in between
                # (the collector commits often): re-list and retry. Anything
                # else (auth, network) raises.
                if i + 1 >= attempts or self._head(api) == sha:
                    raise
        raise RuntimeError("unreachable")

    def load_all(self) -> list[tuple[str, dict]]:
        from huggingface_hub import HfApi
        api = HfApi()
        sha = self._head(api)
        good, _ = _split_names(self._names(api, sha))
        return [(n, self._read(n, sha)) for n in good]
