"""Join J&A notices to USAspending prime-contract records by award number.

Source: the public Hugging Face dataset `abigailhaddad/usaspending-bulk-awards`,
a Parquet mirror of USAspending's Award Data Archive. Only its
`serve/contracts/{FY}.parquet` layer is read: one file per fiscal year with
every transaction once. (Its `contracts/fiscal_year=*/agency=*` tree also has
an `agency=All` partition that repeats every transaction, so it is not used.)

The files are read remotely with DuckDB, selecting only the columns below, so
nothing is downloaded whole.

Matching (all on a normalised key: uppercase, everything but A-Z and 0-9
removed):

1. The notice's `award_number` is classified first. Placeholders (TBD, N/A,
   "See J&A", too short to be a PIID) are `no_piid`; a field that lists
   several award numbers is `multiple_award_numbers`. Neither is matched.
2. The key is looked up in `award_id_piid` (awards and IDVs). Candidates are
   distinct `contract_award_unique_key` values.
3. If nothing is found and the key ends in a modification number
   (`P` + 5 digits, e.g. `SPRRA224D0011_P00003`), it is retried without it.
4. One candidate: matched. Several: those with the notice's awarding agency
   are kept; if exactly one remains it is matched, otherwise the notice is
   `ambiguous` and no award is picked.

Amounts are USAspending obligations (money obligated so far), summed over
every transaction of the award in the fiscal-year files read. They are not
the J&A's estimated value.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import time
from collections import Counter, defaultdict

log = logging.getLogger(__name__)

HF_DATASET = "abigailhaddad/usaspending-bulk-awards"
SERVE_URL = "hf://datasets/" + HF_DATASET + "/serve/contracts/{fy}.parquet"
FIRST_FY = 2007  # first fiscal year in the source archive

# Notice cgac -> USAspending toptier awarding_agency_code. SAM gives the
# military departments their own CGAC (Navy 017, Army 021, Air Force 057);
# USAspending reports all of them under Defense (097).
CGAC_TO_TOPTIER = {"017": "097", "021": "097", "057": "097"}

PLACEHOLDERS = {"TBD", "NA", "NONE", "PENDING", "VARIOUS", "SEEJANDA", "SEEJA",
                "SEEAMENDEDJANDA", "NOTAPPLICABLE", "UNKNOWN", "TBA"}
MIN_KEY_LEN = 6
# Uniform PIID (FAR 4.1603): 6-char office code, 2-digit FY, then the
# instrument type; R/Q/B there are solicitations, not awards.
SOLICITATION = re.compile(r"^[A-Z0-9]{6}\d{2}[RQB][A-Z0-9]{4,8}$")
MOD_SUFFIX = re.compile(r"^(.{10,}?)P\d{5}$")
# A field listing several award numbers: two or more PIID-like tokens, or one
# PIID followed by short serial suffixes ("N3220521D4105_4106").
_TOKEN_SPLIT = re.compile(r"(?:\s*(?:[_,;/&-]|\bAND\b|\)\s*\()\s*)+", re.I)
_PIID_LIKE = re.compile(r"^(?=.*\d)(?=.*[A-Z])[A-Z0-9-]{10,}$")

COLUMNS = [
    "usaspending_matched", "usaspending_match_method", "usaspending_match_note",
    "usaspending_candidates", "usaspending_award_key", "usaspending_permalink",
    "usaspending_award_or_idv", "usaspending_award_type", "usaspending_piid",
    "usaspending_parent_award_piid",
    "usaspending_recipient_name", "usaspending_recipient_uei",
    "usaspending_recipient_parent_name", "usaspending_awarding_agency",
    "usaspending_awarding_sub_agency", "usaspending_awarding_office",
    "usaspending_same_agency",
    "usaspending_total_obligated", "usaspending_transactions",
    "usaspending_current_total_value", "usaspending_potential_total_value",
    "usaspending_first_action_date", "usaspending_latest_action_date",
    "usaspending_extent_competed", "usaspending_other_than_full_and_open",
    "usaspending_idv_orders", "usaspending_idv_orders_obligated",
    "usaspending_as_of",
]

FLOAT_COLS = {"usaspending_total_obligated", "usaspending_current_total_value",
              "usaspending_potential_total_value", "usaspending_idv_orders_obligated"}
INT_COLS = {"usaspending_candidates", "usaspending_transactions", "usaspending_idv_orders"}
BOOL_COLS = {"usaspending_matched", "usaspending_same_agency"}

_TX_COLS = """contract_award_unique_key, award_id_piid, parent_award_id_piid,
    award_or_idv_flag, award_type, idv_type, awarding_agency_code, awarding_agency_name,
    awarding_sub_agency_name, awarding_office_name, recipient_name, recipient_uei,
    recipient_parent_name, federal_action_obligation, total_dollars_obligated,
    current_total_value_of_award, potential_total_value_of_award, action_date,
    modification_number, extent_competed, other_than_full_and_open_competition,
    usaspending_permalink, last_modified_date"""


def normalize(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper())


def _is_series_tail(piid: str, t: str) -> bool:
    tail = piid[-len(t):]
    return (len(t) <= 5 and len(t) < len(piid) and t > tail
            and re.sub(r"\d", "9", tail) == re.sub(r"\d", "9", t) and any(ch.isdigit() for ch in t))


def classify(award_number) -> tuple[str, str | None]:
    """('ok', key) | ('no_piid', None) | ('multiple_award_numbers', None)."""
    raw = str(award_number or "").strip()
    # "47QRCA25DU287(SAIC)", "191N6025P0278(PR15371045)": keep the PIID outside
    # the parentheses when there is one; "(SPE4A125G0011)" keeps its inside.
    outside = re.sub(r"\([^)]*\)", " ", raw).strip()
    if outside != raw and _PIID_LIKE.match(normalize(outside)):
        raw = outside
    key = normalize(raw)
    if (not key or key in PLACEHOLDERS or len(key) < MIN_KEY_LEN or not re.search(r"\d", key)
            or "TBD" in key or re.search(r"XX", key)):  # "SPE1C1-25-D-TBD", "FA8688-26-C-XXXX"
        return "no_piid", None
    tokens = [t.strip("() ") for t in _TOKEN_SPLIT.split(raw.upper().strip("() ")) if t.strip("() ")]
    if len(tokens) >= 2:
        # purchase-request numbers ("PR15881334") and TBD placeholders are not award numbers
        piidish = [t for t in tokens if _PIID_LIKE.match(t)
                   and not re.match(r"PR\d", t) and "TBD" not in t]
        rest = [t for t in tokens[1:] if not re.fullmatch(r"P\d{5}", t)]  # modification numbers
        if len(piidish) >= 2:
            return "multiple_award_numbers", None
        if len(piidish) == 1 and all(t == piidish[0] or re.fullmatch(r"PR\d{6,}", t) for t in tokens):
            return "ok", normalize(piidish[0])  # "19KU2026P0301-PR15881334"
        # "N3220521D4105_4106", "47PK0121D0001-D0061": a PIID, then the tails of
        # further PIIDs in the same series (same shape as the PIID's own tail).
        if (piidish and tokens[0] == piidish[0] and rest
                and all(_is_series_tail(tokens[0], t) for t in rest)):
            return "multiple_award_numbers", None
    return "ok", key


def toptier(cgac) -> str | None:
    c = (str(cgac).strip() if cgac is not None else "") or None
    return CGAC_TO_TOPTIER.get(c, c) if c else None


_AGENCY_STOP = {"department", "dept", "of", "the", "and", "us", "u", "s", "united", "states"}


def agency_words(name) -> frozenset:
    """'LABOR, DEPARTMENT OF' and 'Department of Labor' -> {'labor'}."""
    words = re.sub(r"[^a-z0-9 ]", " ", str(name or "").lower().replace("-", "")).split()
    return frozenset(w for w in words if w not in _AGENCY_STOP)


def same_agency(notice: dict, agency_code, agency_name) -> bool | None:
    """Is USAspending's awarding agency the notice's agency? Compared by code
    (military departments count as Defense) or, since SAM and USAspending
    code some agencies differently (Labor 016 vs 1601), by name. None when
    USAspending gives no agency."""
    if (agency_code in (None, "", "999")) and not agency_name:
        return None
    if agency_code and toptier(notice.get("cgac")) == agency_code:
        return True
    a, b = agency_words(notice.get("agency")), agency_words(agency_name)
    return bool(a) and a == b


def _connect(temp_dir: str | None):
    import duckdb

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    if temp_dir:
        os.makedirs(temp_dir, exist_ok=True)
        con.execute(f"SET temp_directory='{temp_dir}'")
    con.execute("SET max_temp_directory_size='2GB'")
    # One year's query makes ~1,000 range requests (row groups x columns), and
    # Hugging Face answers bursts with HTTP 429. Back off: 5 s doubling, 8 tries.
    con.execute("SET http_retries=8; SET http_retry_wait_ms=5000; SET http_retry_backoff=2; "
                "SET http_timeout=120")
    if os.environ.get("HF_TOKEN"):
        con.execute(f"CREATE SECRET hf (TYPE huggingface, TOKEN '{os.environ['HF_TOKEN']}')")
    return con


def fiscal_years(today: dt.date | None = None) -> list[int]:
    today = today or dt.date.today()
    last = today.year + 1 if today.month >= 10 else today.year
    return list(range(FIRST_FY, last + 1))


def _scan_year(con, url: str) -> None:
    """Temp table f: the year's transactions whose PIID or parent PIID is a key."""
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE f AS
        SELECT {_TX_COLS}, parent_award_agency_id,
               regexp_replace(upper(award_id_piid), '[^A-Z0-9]', '', 'g') AS k_piid,
               regexp_replace(upper(parent_award_id_piid), '[^A-Z0-9]', '', 'g') AS k_parent
        FROM read_parquet('{url}')
        WHERE regexp_replace(upper(award_id_piid), '[^A-Z0-9]', '', 'g') IN (SELECT k FROM keys)
           OR regexp_replace(upper(parent_award_id_piid), '[^A-Z0-9]', '', 'g') IN (SELECT k FROM keys)
    """)


def fetch(keys: set[str], years: list[int], temp_dir: str | None = None, con=None):
    """Return (transactions DataFrame, idv-orders DataFrame, per-year info).

    transactions: every transaction whose normalised award_id_piid is in keys.
    orders: per order whose normalised parent PIID is in keys, per year file:
    parent key, parent agency id, the order's award key, summed obligations
    (aggregate with idv_orders()).
    """
    import pandas as pd

    con = con or _connect(temp_dir)
    con.execute("CREATE OR REPLACE TEMP TABLE keys (k VARCHAR PRIMARY KEY)")
    con.executemany("INSERT INTO keys VALUES (?)", [(k,) for k in sorted(keys)])
    tx, orders, info = [], [], []
    for fy in years:
        url = SERVE_URL.format(fy=fy)
        t0 = dt.datetime.now()
        for attempt in range(4):
            try:
                _scan_year(con, url)
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 3:  # a missing year is fatal: amounts would be incomplete
                    raise RuntimeError(f"USAspending FY{fy}: {e}") from e
                log.warning("USAspending FY%s attempt %d failed (%s); waiting", fy, attempt + 1, e)
                time.sleep(120 * (attempt + 1))
        t = con.execute("SELECT * EXCLUDE (k_parent, parent_award_agency_id) FROM f "
                        "WHERE k_piid IN (SELECT k FROM keys)").df()
        t["fiscal_year_file"] = fy
        # per order (not per IDV) so an order active in several years counts once
        o = con.execute("""SELECT k_parent, parent_award_agency_id, contract_award_unique_key,
                                  sum(federal_action_obligation) AS obligated
                           FROM f WHERE k_parent IN (SELECT k FROM keys)
                           GROUP BY ALL""").df()
        # file-level max from the Parquet statistics (no data pages read)
        last_mod = con.execute(f"""SELECT max(stats_max_value) FROM parquet_metadata('{url}')
                                   WHERE path_in_schema = 'last_modified_date'""").fetchone()[0]
        info.append({"fiscal_year": fy, "transactions_matched": len(t),
                     "max_last_modified_date": str(last_mod) if last_mod else None,
                     "seconds": round((dt.datetime.now() - t0).total_seconds(), 1)})
        log.info("USAspending FY%s: %d transactions on matched keys (%.0fs)", fy, len(t),
                 info[-1]["seconds"])
        tx.append(t)
        orders.append(o)
    con.execute("DROP TABLE IF EXISTS f")
    txdf = pd.concat(tx, ignore_index=True) if tx else pd.DataFrame()
    odf = pd.concat(orders, ignore_index=True) if orders else pd.DataFrame()
    return txdf, odf, info


def idv_orders(odf):
    """Per-order rows from fetch -> per (parent PIID key, parent agency id):
    distinct orders (direct child awards; not calls under a BPA that was
    itself set up under the IDV) and their summed obligations."""
    import pandas as pd

    if odf is None or not len(odf):
        return pd.DataFrame(columns=["k_parent", "parent_award_agency_id", "n_orders", "obligated"])
    # awards only: BPAs set up under a schedule contract are IDVs with that
    # parent too, but they are not orders (and obligate nothing themselves)
    odf = odf[odf["contract_award_unique_key"].str.startswith("CONT_AWD_")]
    return odf.groupby(["k_parent", "parent_award_agency_id"], as_index=False).agg(
        n_orders=("contract_award_unique_key", "nunique"), obligated=("obligated", "sum"))


def _awards(txdf) -> dict[str, dict]:
    """One summary per contract_award_unique_key."""
    out = {}
    if txdf is None or not len(txdf):
        return out
    df = txdf.sort_values(["contract_award_unique_key", "action_date", "modification_number",
                           "fiscal_year_file"], na_position="first")
    for key, g in df.groupby("contract_award_unique_key", sort=False):
        last = g.iloc[-1]
        # a later file is a later snapshot of award-level values
        newest = g.sort_values(["fiscal_year_file", "action_date"]).iloc[-1]
        out[key] = {
            "key": key, "k_piid": last["k_piid"], "piid": last["award_id_piid"],
            "parent_piid": last["parent_award_id_piid"],
            "award_or_idv": last["award_or_idv_flag"],
            "award_type": last["award_type"] if last["award_or_idv_flag"] == "AWARD" else last["idv_type"],
            "agency_code": last["awarding_agency_code"], "agency": last["awarding_agency_name"],
            "sub_agency": last["awarding_sub_agency_name"], "office": last["awarding_office_name"],
            "recipient_name": last["recipient_name"], "recipient_uei": last["recipient_uei"],
            "recipient_parent_name": last["recipient_parent_name"],
            "obligated": float(g["federal_action_obligation"].fillna(0).sum()),
            "transactions": int(len(g)),
            "usa_total_dollars_obligated": newest["total_dollars_obligated"],
            "current_total_value": newest["current_total_value_of_award"],
            "potential_total_value": newest["potential_total_value_of_award"],
            "first_action_date": str(g["action_date"].min())[:10] if g["action_date"].notna().any() else None,
            "latest_action_date": str(g["action_date"].max())[:10] if g["action_date"].notna().any() else None,
            "extent_competed": last["extent_competed"],
            "other_than_full_and_open": last["other_than_full_and_open_competition"],
            "permalink": last["usaspending_permalink"],
        }
    return out


def _idv_agency_id(award_key: str) -> str | None:
    # CONT_IDV_<piid>_<agency id>
    parts = award_key.split("_")
    return parts[3] if len(parts) >= 4 and parts[1] == "IDV" else None


def match_notices(notices: list[dict], txdf, odf, as_of: str | None) -> dict[str, dict]:
    """notice_id -> the usaspending_* columns."""
    awards = _awards(txdf)
    by_key = defaultdict(list)
    for a in awards.values():
        by_key[a["k_piid"]].append(a)
    orders = {}
    if odf is not None and len(odf):
        for r in odf.itertuples(index=False):
            orders[(r.k_parent, r.parent_award_agency_id)] = (int(r.n_orders), float(r.obligated))
    out = {}
    for n in notices:
        row = dict.fromkeys(COLUMNS)
        row["usaspending_matched"] = False
        row["usaspending_as_of"] = as_of
        status, key = classify(n.get("award_number"))
        if status != "ok":
            row["usaspending_match_method"] = status
            row["usaspending_candidates"] = 0
            out[n["notice_id"]] = row
            continue
        method, cands = "exact", by_key.get(key, [])
        if not cands:
            m = MOD_SUFFIX.match(key)
            if m and by_key.get(m.group(1)):
                method, cands = "without_modification_number", by_key[m.group(1)]
        row["usaspending_candidates"] = len(cands)
        if not cands:
            row["usaspending_match_method"] = "piid_not_found"
            if SOLICITATION.match(key):
                row["usaspending_match_note"] = ("looks like a solicitation number "
                                                 "(R, Q or B in the 9th character), not an award")
            out[n["notice_id"]] = row
            continue
        chosen = None
        if len(cands) == 1:
            chosen = cands[0]
        else:
            same = [c for c in cands if same_agency(n, c["agency_code"], c["agency"])]
            if len(same) == 1:
                chosen, method = same[0], method + "+agency"
        if chosen is None:
            row["usaspending_match_method"] = "ambiguous"
            row["usaspending_match_note"] = "; ".join(sorted(c["key"] for c in cands))
            out[n["notice_id"]] = row
            continue
        c = chosen
        row.update({
            "usaspending_matched": True, "usaspending_match_method": method,
            "usaspending_award_key": c["key"], "usaspending_permalink": c["permalink"],
            "usaspending_award_or_idv": c["award_or_idv"], "usaspending_award_type": c["award_type"],
            "usaspending_piid": c["piid"], "usaspending_parent_award_piid": c["parent_piid"],
            "usaspending_recipient_name": c["recipient_name"],
            "usaspending_recipient_uei": c["recipient_uei"],
            "usaspending_recipient_parent_name": c["recipient_parent_name"],
            "usaspending_awarding_agency": c["agency"], "usaspending_awarding_sub_agency": c["sub_agency"],
            "usaspending_awarding_office": c["office"],
            "usaspending_same_agency": same_agency(n, c["agency_code"], c["agency"]),
            "usaspending_total_obligated": round(c["obligated"], 2),
            "usaspending_transactions": c["transactions"],
            "usaspending_current_total_value": c["current_total_value"],
            "usaspending_potential_total_value": c["potential_total_value"],
            "usaspending_first_action_date": c["first_action_date"],
            "usaspending_latest_action_date": c["latest_action_date"],
            "usaspending_extent_competed": c["extent_competed"],
            "usaspending_other_than_full_and_open": c["other_than_full_and_open"],
        })
        if row["usaspending_same_agency"] is False:
            row["usaspending_match_note"] = "awarding agency differs from the notice's agency"
        if c["award_or_idv"] == "IDV":
            n_o, obl = orders.get((c["k_piid"], _idv_agency_id(c["key"])), (0, 0.0))
            row["usaspending_idv_orders"] = n_o
            row["usaspending_idv_orders_obligated"] = round(obl, 2)
        out[n["notice_id"]] = row
    return out


def join(notices: list[dict], years: list[int] | None = None, temp_dir: str | None = None,
         con=None) -> tuple[dict[str, dict], dict]:
    """Run the whole join. Returns (notice_id -> columns, summary)."""
    keys = set()
    for n in notices:
        status, key = classify(n.get("award_number"))
        if status == "ok":
            keys.add(key)
            m = MOD_SUFFIX.match(key)
            if m:
                keys.add(m.group(1))
    years = years or fiscal_years()
    txdf, odf, info = fetch(keys, years, temp_dir=temp_dir, con=con)
    dates = [i["max_last_modified_date"] for i in info if i["max_last_modified_date"]]
    as_of = max(dates) if dates else None
    cols = match_notices(notices, txdf, idv_orders(odf), as_of)
    methods = Counter(c["usaspending_match_method"] for c in cols.values())
    matched = sum(1 for c in cols.values() if c["usaspending_matched"])
    summary = {"as_of": as_of, "fiscal_years": info, "notices": len(cols), "matched": matched,
               "match_rate": round(matched / len(cols), 4) if cols else None,
               "by_method": dict(methods), "keys_searched": len(keys),
               "transactions_read": int(len(txdf))}
    return cols, summary
