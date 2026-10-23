"""SAM.gov's award block for each notice: which contract, and which ORDER, a
J&A is for. Added 2026-10-23.

The CSV extracts carry `AwardNumber` (our `award_number`) but not the
second number SAM's notice form has, `award.deliveryOrderNumber`. For a J&A
that justifies one task or delivery order, agencies usually put the VEHICLE
in the award-number box (a GSA schedule, NASA SEWP, an agency IDIQ) and the
order in the order-number box. Without the order number, a reader joining
J&As to contracts by award number can only attach the J&A to every order
under that vehicle, including other agencies' orders. A downstream site did
exactly that, which is why these columns exist.

Source: the keyless endpoint the SAM.gov notice page itself reads,
``GET https://sam.gov/api/prod/opps/v2/opportunities/<notice_id>``. Its
``data2.award`` object looks like (checked 2026-10-23 on 14 records)::

    {"date": "2026-09-11", "number": "NNG15SC71B",
     "deliveryOrderNumber": "70CTD026FR0000064", "amount": "1536472.17000000",
     "awardee": {"name": "...", "ueiSAM": "R4WTAS3E7BA4", "location": {...}},
     "lineItemNumber": "", "justificationAuthority": {"authority": "brand",
                                                      "modificationNumber": ""}}

Every key can be missing; Special Notices usually have ``{}`` or null. The
values are what the posting office typed, unchecked: deliveryOrderNumber is
sometimes the award number again, a solicitation number, or a number with a
modification suffix ("36C10B20N10060037_P00020"). We copy them verbatim
(stripped of surrounding whitespace) and do not interpret them.

One request per notice, through the same polite SamClient as the attachment
lists (rate limit, retries, User-Agent). New and edited notices get
``sam_award_status = "pending"`` from ``pipeline.merge_notices`` and are
fetched by the daily job; notices collected before this module existed have
no status (null) and are fetched only by an explicit backfill
(``ja-collect sam-awards --backfill``), so the daily job's run time does not
jump by ~10k requests the first day.

Resumable and cached: every SAM answer is appended to
``<workdir>/cache/sam_award.jsonl`` (not published) as it arrives, and a
later backfill applies a settled cached answer to a notice that still has no
status instead of asking SAM again. ``ja-collect pull`` replaces ``data/`` with the published tables, so
without this cache a backfill interrupted before publishing would start
over.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable

from .sam_api import SamClient, SamError
from .store import Store

log = logging.getLogger(__name__)

# Notice columns this module fills, in table order (see store.NOTICE_COLUMNS).
COLUMNS = [
    "sam_award_status", "sam_award_checked_at", "sam_award_number",
    "sam_award_order_number", "sam_award_date", "sam_award_amount",
    "sam_awardee_name", "sam_awardee_uei", "sam_award_modification_number",
    "sam_justification_authority", "sam_organization_id",
]
FIELD_COLUMNS = [c for c in COLUMNS if c not in ("sam_award_status", "sam_award_checked_at")]

STATUS_PENDING = "pending"
STATUS_OK = "ok"                # record read, award block present
STATUS_NO_AWARD = "no_award"    # record read, no (or an empty) award block
STATUS_NOT_FOUND = "not_found"  # HTTP 404: SAM no longer serves the notice
# Answers that need no new request. not_found is settled too (SAM answers
# 404 for the same notices on the attachment-list endpoint, see the card);
# --retry-errors asks again for not_found and error rows.
SETTLED = {STATUS_OK, STATUS_NO_AWARD, STATUS_NOT_FOUND}

CACHE_REL = os.path.join("cache", "sam_award.jsonl")


def _s(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (dict, list)):
        return None
    v = str(v).strip()
    return v or None


def parse_award(record: dict | None) -> dict:
    """Notice column values from one v2 opportunity record."""
    rec = record or {}
    data2 = rec.get("data2") or {}
    award = data2.get("award")
    out = {c: None for c in FIELD_COLUMNS}
    out["sam_organization_id"] = _s(data2.get("organizationId"))
    if not isinstance(award, dict) or not any(v not in (None, "", {}, []) for v in award.values()):
        out["sam_award_status"] = STATUS_NO_AWARD
        return out
    awardee = award.get("awardee") if isinstance(award.get("awardee"), dict) else {}
    ja = award.get("justificationAuthority") if isinstance(award.get("justificationAuthority"), dict) else {}
    out.update(
        sam_award_status=STATUS_OK,
        sam_award_number=_s(award.get("number")),
        sam_award_order_number=_s(award.get("deliveryOrderNumber")),
        sam_award_date=_s(award.get("date")),
        sam_award_amount=_s(award.get("amount")),
        sam_awardee_name=_s(awardee.get("name")),
        sam_awardee_uei=_s(awardee.get("ueiSAM")),
        sam_award_modification_number=_s(ja.get("modificationNumber")),
        sam_justification_authority=_s(ja.get("authority")),
    )
    return out


def fetch_award(client: SamClient, notice_id: str) -> dict:
    """Column values for one notice; never raises for a per-notice failure
    (the failure is the status)."""
    try:
        return parse_award(client.get_notice(notice_id))
    except SamError as e:
        if e.status == 404:
            return {**{c: None for c in FIELD_COLUMNS}, "sam_award_status": STATUS_NOT_FOUND}
        return {**{c: None for c in FIELD_COLUMNS}, "sam_award_status": f"error: {e}"[:300]}
    except Exception as e:  # noqa: BLE001 - recorded, never silently dropped
        return {**{c: None for c in FIELD_COLUMNS},
                "sam_award_status": f"error: {type(e).__name__}: {e}"[:300]}


# --------------------------------------------------------------------------- cache

def load_cache(path: str) -> dict[str, dict]:
    """notice_id -> latest cached answer. A truncated last line (killed
    mid-write) is skipped; every complete line counts."""
    out: dict[str, dict] = {}
    if not os.path.exists(path):
        return out
    with open(path) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("notice_id"):
                out[r["notice_id"]] = r
    return out


def _append_cache(fh, notice_id: str, rec: dict) -> None:
    fh.write(json.dumps({"notice_id": notice_id, **rec}, sort_keys=True) + "\n")
    fh.flush()


# --------------------------------------------------------------------------- step

def select(store: Store, *, backfill: bool = False, retry_errors: bool = False,
           notice_ids: Iterable[str] | None = None) -> list[str]:
    """Notices to work on. Default: status 'pending' (new or edited notices).
    backfill: also notices with no status yet. retry_errors: also 'error...'
    and 'not_found'."""
    if notice_ids is not None:
        return [n for n in dict.fromkeys(notice_ids) if n in store.notices]
    out = []
    for nid, n in store.notices.items():
        st = n.get("sam_award_status")
        if st == STATUS_PENDING:
            out.append(nid)
        elif st is None and backfill:
            out.append(nid)
        elif retry_errors and st is not None and (st.startswith("error") or st == STATUS_NOT_FOUND):
            out.append(nid)
    return sorted(out)


def apply(store: Store, notice_id: str, rec: dict) -> None:
    n = store.notices[notice_id]
    for c in COLUMNS:
        n[c] = rec.get(c)


def fetch_sam_awards(store: Store, client: SamClient, *, backfill: bool = False,
                     retry_errors: bool = False, notice_ids: Iterable[str] | None = None,
                     limit: int | None = None, workers: int = 2,
                     checkpoint: Callable[[], None] | None = None,
                     checkpoint_every: int = 200, use_cache: bool = True,
                     now: Callable[[], str] | None = None) -> dict:
    """Fill the sam_award_* columns. Returns counts by status, plus how many
    answers came from the cache instead of SAM."""
    now = now or (lambda: dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat())
    todo = select(store, backfill=backfill, retry_errors=retry_errors, notice_ids=notice_ids)
    cache_path = store.path(CACHE_REL)
    cache = load_cache(cache_path) if use_cache else {}
    counts: dict[str, int] = {}
    from_cache = 0

    def count(status: str) -> None:
        k = "error" if status.startswith("error") else status
        counts[k] = counts.get(k, 0) + 1

    to_fetch = []
    for nid in todo:
        c = cache.get(nid)
        # A cached answer stands in for a request only for a notice the
        # tables have never had an answer for (status null: an interrupted
        # backfill whose tables were re-pulled). A 'pending' notice is new or
        # was edited on SAM, and an error/not_found is being retried, so
        # those always ask SAM again.
        if (c and store.notices[nid].get("sam_award_status") is None
                and c.get("sam_award_status") in SETTLED):
            apply(store, nid, c)
            count(c["sam_award_status"])
            from_cache += 1
        else:
            to_fetch.append(nid)
    if limit is not None:
        to_fetch = to_fetch[:limit]
    log.info("sam awards: %d selected, %d from cache, %d to fetch", len(todo), from_cache, len(to_fetch))

    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    done = 0
    with open(cache_path, "a") as fh, ThreadPoolExecutor(max(1, workers)) as pool:
        futs = {pool.submit(fetch_award, client, nid): nid for nid in to_fetch}
        for fut in as_completed(futs):
            nid = futs[fut]
            rec = dict(fut.result(), sam_award_checked_at=now())
            _append_cache(fh, nid, rec)
            apply(store, nid, rec)
            count(rec["sam_award_status"])
            done += 1
            if checkpoint and done % checkpoint_every == 0:
                log.info("sam awards: %d/%d", done, len(to_fetch))
                checkpoint()
    out = {"selected": len(todo), "from_cache": from_cache, "fetched": len(to_fetch),
           "by_status": counts}
    out["remaining_unfetched"] = len(todo) - from_cache - len(to_fetch)
    return out


def coverage(store: Store) -> dict[str, int]:
    """Notices by sam_award_status ('unfetched' for null), for the card and
    the run log."""
    out: dict[str, int] = {}
    for n in store.notices.values():
        st = n.get("sam_award_status")
        k = "unfetched" if st is None else ("error" if st.startswith("error") else st)
        out[k] = out.get(k, 0) + 1
    return dict(sorted(out.items()))
