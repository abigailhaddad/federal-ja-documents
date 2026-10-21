"""The digest's memory of what it has already sent: ``analysis/digest_state.json``
in the Hugging Face dataset.

Why there: the workflows already hold an ``HF_TOKEN``; one ``upload_file`` is
one atomic dataset commit; nothing has to push to the GitHub repo; and the
collector's ``publish --delete-stale`` never deletes anything under
``analysis/`` (``KEEP_PREFIXES`` in collect/hf_sync.py). The file is public;
it holds only notice ids, timestamps and email subjects.

The window is a WATERMARK on ``first_seen_at``: a digest covers the notices
first seen after the previous sent digest's watermark, and after a
successful send the watermark becomes the latest ``first_seen_at`` among
the notices it looked at (picked or not, left out or not). So:

* a dry run, a failed run or a failed send changes nothing, and the next run
  covers the same notices again plus anything newer (nothing is skipped);
* a rerun after a successful send finds nothing new (nothing is re-sent);
* the collector stamps one ``first_seen_at`` per run and runs one at a time,
  so a notice published after the digest read the dataset always has a later
  ``first_seen_at`` than anything the digest saw.

The state is written only after Buttondown accepted the email. If that write
fails, the email carries the watermark in its metadata and the next run
recovers it from Buttondown before choosing its window (see run.py), so it is
not sent twice.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ..analysis.data import HF_DATASET

STATE_PATH = "analysis/digest_state.json"
MAX_RUNS_KEPT = 90


def empty_state() -> dict:
    return {"version": 1, "watermark_first_seen_at": None, "last_sent_at": None,
            "last_sent_date_et": None, "runs": []}


class HFState:
    """Reads and writes the state file in the dataset (writing needs HF_TOKEN)."""

    def load(self) -> dict:
        from huggingface_hub import hf_hub_download
        # Only "the file is not there" means a fresh state. Any other error
        # (network, auth) raises: guessing an empty state could re-send.
        from huggingface_hub.errors import RemoteEntryNotFoundError
        try:
            p = hf_hub_download(HF_DATASET, STATE_PATH, repo_type="dataset", revision="main",
                                force_download=True)
        except RemoteEntryNotFoundError:
            return empty_state()
        return json.loads(Path(p).read_text())

    def save(self, state: dict, message: str) -> str:
        from huggingface_hub import HfApi
        info = HfApi().upload_file(path_or_fileobj=json.dumps(state, indent=1).encode(), path_in_repo=STATE_PATH,
                                   repo_id=HF_DATASET, repo_type="dataset", commit_message=message)
        return str(getattr(info, "commit_url", info))


class LocalState:
    """A local JSON file instead (tests, or trying the send path by hand)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else empty_state()

    def save(self, state: dict, message: str) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, indent=1))
        return str(self.path)


def watermark(state: dict) -> datetime | None:
    v = state.get("watermark_first_seen_at")
    return datetime.fromisoformat(v) if v else None


def after_send(state: dict, *, new_watermark: datetime | None, sent_at: datetime, date_et: str,
               run: dict) -> dict:
    """The state after a successful send (a new dict; the input is unchanged)."""
    old = watermark(state)
    wm = max([w for w in (old, new_watermark) if w is not None], default=None)
    runs = (list(state.get("runs") or []) + [run])[-MAX_RUNS_KEPT:]
    return {**state, "version": 1,
            "watermark_first_seen_at": wm.astimezone(timezone.utc).isoformat() if wm else None,
            "last_sent_at": sent_at.astimezone(timezone.utc).isoformat(), "last_sent_date_et": date_et,
            "runs": runs}
