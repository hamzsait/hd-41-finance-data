"""HD-41 — FEC federal-donation enrichment.

Adapted from san-antonio-finance-data/fec_enrich.py:
  - reads hd41_finance.db
  - drops the resolve_from_fec_employers pass (we have no employer_identities
    table here; that's a later phase)
  - same dual-key rotation, rate limiter, idempotent resume

Schema additions on first run:
  donor_identities          — adds fec_partisan_lean, fec_total_dem/rep/other,
                              fec_total_donations, fec_matched, fec_matched_at
                              (already created by build_hd41_identities.py;
                              this is belt-and-suspenders)
  fec_committee_cache       — committee_id → classification (Dem/Rep/Other)
  fec_contributions_raw     — every confirmed FEC schedule_a row, joined by
                              donor_id (idempotent via unique (donor_id, sub_id))

Idempotency:
  Donors with fec_matched=1 are skipped on resume. Use --reset to re-process all.

Usage:
    python fec_enrich_hd41.py                # process all unmatched donors
    python fec_enrich_hd41.py --dry-run      # print, no writes
    python fec_enrich_hd41.py --limit 50     # process top N by total_donated
    python fec_enrich_hd41.py --reset        # clear fec_matched flags first
"""
from __future__ import annotations

import argparse
import io
import os
import pathlib
import re
import sqlite3
import sys
import time
import unicodedata
from collections import deque
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from rapidfuzz import fuzz

# Force UTF-8 output for emoji/accented names; Windows defaults to cp1252.
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
except (ValueError, AttributeError):
    pass

# ── Config ─────────────────────────────────────────────────────────────────
ROOT = pathlib.Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")
DB = str(ROOT / "hd41_finance.db")
FEC_API_KEYS = [k for k in (os.getenv("FEC_API_KEY_1"), os.getenv("FEC_API_KEY_2")) if k]
if not FEC_API_KEYS:
    raise SystemExit("No FEC API keys in env. Set FEC_API_KEY_1 (and optionally FEC_API_KEY_2) in .env")
FEC_BASE = "https://api.open.fec.gov/v1"
TOP_N = 2000  # default upper bound (we only have 541 donors)
MATCH_THRESHOLD = 75       # composite confirm-score floor (0–100)
LARGE_SET_THRESHOLD = 300  # if >N candidates, narrow by zip5

NICKNAMES = {
    "bill": "william", "billy": "william", "will": "william",
    "bob": "robert", "rob": "robert", "bobby": "robert",
    "jim": "james", "jimmy": "james", "jamie": "james",
    "tom": "thomas", "tommy": "thomas",
    "mike": "michael", "mick": "michael",
    "dick": "richard", "rick": "richard",
    "dave": "david",
    "joe": "joseph", "joey": "joseph",
    "sue": "susan", "susie": "susan",
    "liz": "elizabeth", "beth": "elizabeth", "betty": "elizabeth",
    "kate": "katherine", "kathy": "katherine",
    "chris": "christopher",
    "dan": "daniel", "danny": "daniel",
    "sam": "samuel",
    "ed": "edward", "ted": "edward",
    "ben": "benjamin",
    "nick": "nicholas",
    "tony": "anthony",
    "andy": "andrew",
    "alex": "alexander",
    "greg": "gregory",
    "ken": "kenneth",
    "steve": "steven",
    "matt": "matthew",
    "jeff": "jeffrey",
    "jerry": "gerald",
    "chuck": "charles", "charlie": "charles",
    "hank": "henry",
    "jack": "john", "jon": "john", "johnny": "john",
    "peggy": "margaret", "meg": "margaret",
    "frank": "francis",
    "fred": "frederick",
    "jake": "jacob",
    "ron": "ronald",
    "tim": "timothy",
    "phil": "philip",
    "don": "donald",
    "pam": "pamela",
    "deb": "deborah", "debbie": "deborah",
    "gene": "eugene",
    "drew": "andrew",
}

SUFFIX_STRIP = re.compile(r"\b(jr|sr|ii|iii|iv|dr|mr|mrs|ms|prof|rev|hon)\.?\b", re.IGNORECASE)
DEM_PATTERNS = re.compile(
    r"\b(democrat|democratic|dccc|dscc|dlcc|actblue|emily.?s list|"
    r"planned parenthood|moveon|sierra club|afscme|seiu|nea|afl.cio|"
    r"progressive|nrdc action|lgbtq|biden|obama|clinton|pelosi|"
    r"majority pac|house majority|senate majority)\b", re.IGNORECASE)
REP_PATTERNS = re.compile(
    r"\b(republican|gop|rnc|nrcc|nrsc|rslc|trump|maga|heritage action|"
    r"club for growth|tea party|freedom works|susan b anthony|nra|"
    r"nfib|associated builders|american energy alliance|winred|"
    r"mitt romney|mcconnell|mccarthy)\b", re.IGNORECASE)


# ── Rate limiter ───────────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self, max_calls: int = 1600, window_seconds: int = 600):
        self.max_calls = max_calls
        self.window = window_seconds
        self.timestamps: deque[float] = deque()

    def wait(self) -> None:
        now = time.time()
        while self.timestamps and now - self.timestamps[0] > self.window:
            self.timestamps.popleft()
        if len(self.timestamps) >= self.max_calls:
            sleep_for = self.window - (now - self.timestamps[0]) + 0.5
            print(f"  [rate limit] sleeping {sleep_for:.1f}s ...", flush=True)
            time.sleep(sleep_for)
        self.timestamps.append(time.time())


# ── DB setup ───────────────────────────────────────────────────────────────
def setup_db(conn: sqlite3.Connection) -> None:
    cols = [
        "fec_partisan_lean REAL",
        "fec_total_dem REAL DEFAULT 0",
        "fec_total_rep REAL DEFAULT 0",
        "fec_total_other REAL DEFAULT 0",
        "fec_total_donations INTEGER DEFAULT 0",
        "fec_matched INTEGER DEFAULT 0",
        "fec_matched_at TEXT",
    ]
    for col_def in cols:
        try:
            conn.execute(f"ALTER TABLE donor_identities ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fec_committee_cache (
            committee_id    TEXT PRIMARY KEY,
            party_code      TEXT,
            committee_type  TEXT,
            committee_name  TEXT,
            classification  TEXT NOT NULL,
            fetched_at      TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fec_contributions_raw (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            donor_id                TEXT NOT NULL,
            committee_id            TEXT NOT NULL,
            contribution_amount     REAL,
            contribution_date       TEXT,
            fec_contributor_name    TEXT,
            fec_contributor_city    TEXT,
            fec_contributor_zip     TEXT,
            fec_employer            TEXT,
            fec_occupation          TEXT,
            fec_sub_id              TEXT,
            confirm_score           REAL,
            UNIQUE(donor_id, fec_sub_id)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fec_raw_donor     ON fec_contributions_raw(donor_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fec_raw_committee ON fec_contributions_raw(committee_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fec_raw_sub       ON fec_contributions_raw(fec_sub_id)")
    conn.commit()
    print("DB schema ready.")


# ── Name normalisation ─────────────────────────────────────────────────────
def to_ascii(s: str) -> str:
    try:
        return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    except Exception:
        return s


def _parse_name(raw: str | None) -> tuple[str, str]:
    """Returns (last, first) from any "Last, First" or "First Last" form."""
    s = to_ascii(raw or "").lower()
    s = SUFFIX_STRIP.sub("", s)
    s = re.sub(r"[^a-z ,'-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if "," in s:
        parts = s.split(",", 1)
        last = parts[0].strip()
        first = parts[1].strip().split()[0] if parts[1].strip() else ""
    else:
        toks = s.split()
        last = toks[-1] if toks else ""
        first = toks[0] if len(toks) > 1 else ""
    first = NICKNAMES.get(first, first)
    return last, first


def normalize_name_for_fec(canonical_name: str) -> tuple[str, str, str]:
    last, first = _parse_name(canonical_name)
    query = f"{last.upper()}, {first.upper()}" if first else last.upper()
    return query, last, first


def parse_fec_name(fec_name: str | None) -> tuple[str, str]:
    return _parse_name(fec_name)


# ── FEC API calls (with key rotation) ──────────────────────────────────────
_key_index = 0
_key_cooldown: dict[str, float] = {}


def _active_key() -> str:
    global _key_index
    now = time.time()
    for _ in range(len(FEC_API_KEYS)):
        key = FEC_API_KEYS[_key_index % len(FEC_API_KEYS)]
        if now >= _key_cooldown.get(key, 0):
            return key
        _key_index += 1
    soonest = min(FEC_API_KEYS, key=lambda k: _key_cooldown.get(k, 0))
    wait = _key_cooldown[soonest] - now
    print(f"  [all keys cooling] waiting {wait:.0f}s ...", flush=True)
    time.sleep(wait + 1)
    return soonest


def api_get(session: requests.Session, url: str, params: dict, rate_limiter: RateLimiter, max_retries: int = 8):
    global _key_index
    for attempt in range(max_retries):
        rate_limiter.wait()
        key = _active_key()
        params["api_key"] = key
        try:
            resp = session.get(url, params=params, timeout=60)
        except requests.exceptions.RequestException:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 60))
            _key_cooldown[key] = time.time() + retry_after + 5
            _key_index += 1
            print(f"  [429 key#{FEC_API_KEYS.index(key)+1}] cooldown {retry_after+5}s", flush=True)
            continue
        if resp.status_code in (500, 503):
            time.sleep(min(2 ** attempt, 64))
            continue
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
    raise RuntimeError(f"max retries exceeded for {url}")


def query_fec_schedule_a(session, name_query: str, local_zip: str, rate_limiter: RateLimiter):
    base_params = {
        "api_key":           _active_key(),
        "contributor_name":  name_query,
        "contributor_state": "TX",
        "per_page":          100,
        "sort":              "-contribution_receipt_date",
    }
    data = api_get(session, f"{FEC_BASE}/schedules/schedule_a/", base_params, rate_limiter)
    if data is None:
        return []

    total_count = data.get("pagination", {}).get("count", 0)
    if total_count > LARGE_SET_THRESHOLD and local_zip:
        base_params["contributor_zip"] = local_zip[:5]
        data = api_get(session, f"{FEC_BASE}/schedules/schedule_a/", base_params, rate_limiter)
        if data is None:
            return []

    all_results = list(data.get("results", []))
    last_indexes = data.get("pagination", {}).get("last_indexes", {})
    while last_indexes and len(data.get("results", [])) == 100:
        page_params = dict(base_params)
        page_params["last_index"] = last_indexes.get("last_index")
        page_params["last_contribution_receipt_date"] = last_indexes.get("last_contribution_receipt_date")
        data = api_get(session, f"{FEC_BASE}/schedules/schedule_a/", page_params, rate_limiter)
        if data is None:
            break
        all_results.extend(data.get("results", []))
        last_indexes = data.get("pagination", {}).get("last_indexes", {})
    return all_results


# ── Identity confirmation ──────────────────────────────────────────────────
def confirm_match(local_last: str, local_first: str, local_zip: str, fec_row: dict) -> tuple[bool, float]:
    fec_last, fec_first = parse_fec_name(fec_row.get("contributor_name", ""))
    fec_zip = (fec_row.get("contributor_zip") or "")[:5]
    last_score  = fuzz.token_sort_ratio(local_last,  fec_last)  if local_last  else 0
    first_score = fuzz.token_sort_ratio(local_first, fec_first) if local_first else 0
    zip_score = 100 if (local_zip and fec_zip and local_zip[:5] == fec_zip) else 0
    if local_first:
        composite = (0.45 * last_score + 0.30 * first_score
                     + 0.15 * 100  # state=TX is enforced by query
                     + 0.10 * zip_score)
    else:
        composite = 0.70 * last_score + 0.30 * zip_score
    return composite >= MATCH_THRESHOLD, composite


# ── Committee classification ───────────────────────────────────────────────
class CommitteeCache:
    def __init__(self, conn: sqlite3.Connection, session: requests.Session, rl: RateLimiter):
        self.conn = conn
        self.session = session
        self.rl = rl
        self._mem: dict[str, str] = {}
        for cid, cls in conn.execute("SELECT committee_id, classification FROM fec_committee_cache"):
            self._mem[cid] = cls

    def get(self, committee_id: str) -> str:
        if committee_id in self._mem:
            return self._mem[committee_id]
        cls = self._fetch(committee_id)
        self._mem[committee_id] = cls
        return cls

    def _fetch(self, committee_id: str) -> str:
        url = f"{FEC_BASE}/committee/{committee_id}/"
        data = api_get(self.session, url, {"api_key": _active_key()}, self.rl)
        if data is None:
            classification, party_code, ctype, name = "Other", "", "", committee_id
        else:
            results = data.get("results", []) or []
            r = results[0] if results else {}
            party_code = (r.get("party") or "").upper()
            ctype = (r.get("committee_type") or "").upper()
            name = r.get("name", committee_id)
            if party_code in ("DEM", "D"):
                classification = "Dem"
            elif party_code in ("REP", "R"):
                classification = "Rep"
            elif DEM_PATTERNS.search(name):
                classification = "Dem"
            elif REP_PATTERNS.search(name):
                classification = "Rep"
            else:
                classification = "Other"
        self.conn.execute(
            """
            INSERT OR REPLACE INTO fec_committee_cache
            (committee_id, party_code, committee_type, committee_name, classification, fetched_at)
            VALUES (?,?,?,?,?,?)
            """,
            (committee_id, party_code, ctype, name, classification,
             datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()
        return classification


# ── Main pipeline ──────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run",   action="store_true")
    ap.add_argument("--limit",     type=int, default=TOP_N)
    ap.add_argument("--reset",     action="store_true", help="re-process already-matched donors")
    ap.add_argument("--candidate", help=(
        "restrict to donor_ids that gave to this candidate_slug "
        "(active rows only). Useful for finishing one candidate's coverage "
        "without re-processing the whole donor pool."
    ))
    args = ap.parse_args()

    conn = sqlite3.connect(DB, timeout=120)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    setup_db(conn)

    if args.reset:
        conn.execute("UPDATE donor_identities SET fec_matched = 0")
        conn.commit()
        print("Reset all fec_matched flags.")

    cur = conn.cursor()
    if args.candidate:
        # Restrict to donors who gave to this candidate (active rows only).
        cur.execute(
            """
            SELECT di.donor_id, di.canonical_name, di.canonical_zip
            FROM donor_identities di
            WHERE (di.fec_matched = 0 OR di.fec_matched IS NULL)
              AND di.donor_id IN (
                SELECT DISTINCT c.donor_id
                FROM contributions c
                WHERE c.candidate_slug = ?
                  AND COALESCE(c.info_only_flag,'N') <> 'Y'
                  AND c.contributor_persent_type = 'INDIVIDUAL'
                  AND c.donor_id IS NOT NULL
              )
            ORDER BY di.total_donated DESC
            LIMIT ?
            """,
            (args.candidate, args.limit),
        )
    else:
        cur.execute(
            """
            SELECT donor_id, canonical_name, canonical_zip
            FROM donor_identities
            WHERE fec_matched = 0 OR fec_matched IS NULL
            ORDER BY total_donated DESC
            LIMIT ?
            """,
            (args.limit,),
        )
    donors = [dict(r) for r in cur.fetchall()]
    scope_note = f"candidate={args.candidate!r}" if args.candidate else "all candidates"
    print(f"Processing {len(donors)} donors  ({scope_note}, dry_run={args.dry_run})")

    session = requests.Session()
    session.headers.update({"User-Agent": "HD-41 Finance Research / contact@example.com"})
    rate_limiter = RateLimiter(max_calls=1600, window_seconds=600)
    committee_cache = CommitteeCache(conn, session, rate_limiter)

    stats = {"matched": 0, "no_history": 0, "ambiguous": 0, "api_errors": 0}

    for i, d in enumerate(donors, 1):
        donor_id = d["donor_id"]
        cname    = d["canonical_name"] or ""
        local_zip = (d["canonical_zip"] or "")[:5]

        fec_query, local_last, local_first = normalize_name_for_fec(cname)
        if not local_last:
            if not args.dry_run:
                conn.execute(
                    "UPDATE donor_identities SET fec_matched=1, fec_matched_at=? WHERE donor_id=?",
                    (datetime.now(timezone.utc).isoformat(), donor_id),
                )
                conn.commit()
            continue

        if i == 1 or i % 25 == 0 or i == len(donors):
            print(f"  [{i}/{len(donors)}] {cname}  zip={local_zip}", flush=True)

        try:
            raw_rows = query_fec_schedule_a(session, fec_query, local_zip, rate_limiter)
        except Exception as e:
            print(f"  [ERROR] {cname}: {e}", flush=True)
            stats["api_errors"] += 1
            continue

        if not raw_rows:
            stats["no_history"] += 1
            if not args.dry_run:
                conn.execute(
                    """
                    UPDATE donor_identities
                    SET fec_matched=1, fec_total_donations=0, fec_matched_at=?
                    WHERE donor_id=?
                    """,
                    (datetime.now(timezone.utc).isoformat(), donor_id),
                )
                conn.commit()
            continue

        confirmed: list[tuple[dict, float]] = []
        for r in raw_rows:
            ok, score = confirm_match(local_last, local_first, local_zip, r)
            if ok:
                confirmed.append((r, score))

        if not confirmed:
            stats["no_history"] += 1
            if not args.dry_run:
                conn.execute(
                    """
                    UPDATE donor_identities
                    SET fec_matched=1, fec_total_donations=0, fec_matched_at=?
                    WHERE donor_id=?
                    """,
                    (datetime.now(timezone.utc).isoformat(), donor_id),
                )
                conn.commit()
            continue

        dem_total = rep_total = other_total = 0.0
        raw_inserts: list[tuple] = []
        for r, score in confirmed:
            committee_id = r.get("committee_id", "")
            amount = r.get("contribution_receipt_amount") or 0.0
            if amount <= 0:
                continue
            cls = committee_cache.get(committee_id) if committee_id else "Other"
            if cls == "Dem":
                dem_total += amount
            elif cls == "Rep":
                rep_total += amount
            else:
                other_total += amount
            raw_inserts.append((
                donor_id, committee_id, amount,
                r.get("contribution_receipt_date"),
                r.get("contributor_name"),
                r.get("contributor_city"),
                r.get("contributor_zip"),
                (r.get("contributor_employer") or "").strip() or None,
                (r.get("contributor_occupation") or "").strip() or None,
                r.get("sub_id"),
                score,
            ))

        n = len(raw_inserts)
        lean = (dem_total / (dem_total + rep_total)) if (dem_total + rep_total) > 0 else None

        if args.dry_run:
            lean_s = f"{lean:.2f}" if lean is not None else "—"
            print(f"  {cname}: D=${dem_total:,.0f}  R=${rep_total:,.0f}  O=${other_total:,.0f}  lean={lean_s}", flush=True)
        else:
            conn.executemany(
                """
                INSERT OR REPLACE INTO fec_contributions_raw
                (donor_id, committee_id, contribution_amount, contribution_date,
                 fec_contributor_name, fec_contributor_city, fec_contributor_zip,
                 fec_employer, fec_occupation, fec_sub_id, confirm_score)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                raw_inserts,
            )
            conn.execute(
                """
                UPDATE donor_identities
                SET fec_partisan_lean=?, fec_total_dem=?, fec_total_rep=?,
                    fec_total_other=?, fec_total_donations=?,
                    fec_matched=1, fec_matched_at=?
                WHERE donor_id=?
                """,
                (lean, dem_total, rep_total, other_total, n,
                 datetime.now(timezone.utc).isoformat(), donor_id),
            )
            conn.commit()
        stats["matched"] += 1

    print()
    print(f"Done.  matched={stats['matched']}  no_history={stats['no_history']}  errors={stats['api_errors']}")

    if not args.dry_run:
        cur.execute(
            """
            SELECT
              COUNT(*) FILTER (WHERE fec_matched=1)                  AS processed,
              COUNT(*) FILTER (WHERE fec_partisan_lean IS NOT NULL)  AS has_lean,
              COUNT(*) FILTER (WHERE fec_partisan_lean >= 0.6)       AS dem_leaning,
              COUNT(*) FILTER (WHERE fec_partisan_lean <= 0.4)       AS rep_leaning,
              COUNT(*) FILTER (WHERE fec_partisan_lean BETWEEN 0.4 AND 0.6) AS mixed
            FROM donor_identities
            """
        )
        row = cur.fetchone()
        print()
        print("DB summary:")
        print(f"  Processed:           {row[0]:,}")
        print(f"  Has FEC lean:        {row[1]:,}")
        print(f"  Dem-leaning (>=.6):  {row[2]:,}")
        print(f"  Rep-leaning (<=.4):  {row[3]:,}")
        print(f"  Mixed (.4–.6):       {row[4]:,}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
