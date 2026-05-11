"""HD-41 — donor identity resolution.

Adapted from san-antonio-finance-data/build_identities.py with one important
fix: SA's version assigns a fresh uuid4() per cluster on every run, which means
re-running invalidates every foreign key in `contributions.donor_id`. Here we
derive donor_id deterministically from the cluster's canonical_name + zip5, so
identical input data always yields the same IDs.

Pipeline:
  1. Pull individual contribution rows (active only) from `contributions`.
     ENTITY rows are skipped — they don't need fuzzy resolution.
  2. Normalize names (NFKD ASCII fold, lowercase, strip punctuation, expand
     nicknames), zip5, and employer/occupation.
  3. Block on (last, zip5) and (soundex(last), zip5).
  4. Score pairs (Last/First fuzz token-sort, zip exact, employer fuzz).
     Auto-merge ≥ 0.83. Send 0.65–0.83 to review_queue.
  5. Union-Find clustering.
  6. Compute donor_id = sha1(canonical_name + '|' + canonical_zip5)[:16] for
     each cluster. Stable across re-runs.
  7. Write donor_identities + review_queue. Stamp donor_id +
     match_confidence on contributions.

Idempotency:
  - donor_identities is replaced on each run (DROP+CREATE) but content is
     deterministic.
  - contributions.donor_id is updated in place; re-running yields the same IDs.

Usage:
    python build_hd41_identities.py
"""
from __future__ import annotations

import hashlib
import pathlib
import re
import sqlite3
import unicodedata
from collections import defaultdict

from rapidfuzz import fuzz
from jellyfish import soundex

DB = str(pathlib.Path(__file__).parent / "hd41_finance.db")

# ── Nickname table (subset of SA's; same canonical first names) ────────────
NICKNAMES = {
    "bill": "william", "billy": "william", "will": "william",
    "bob": "robert", "rob": "robert", "bobby": "robert",
    "jim": "james", "jimmy": "james", "jamie": "james",
    "tom": "thomas", "tommy": "thomas",
    "mike": "michael", "mick": "michael",
    "dick": "richard", "rick": "richard", "ricky": "richard",
    "dave": "david",
    "joe": "joseph", "joey": "joseph",
    "sue": "susan", "susie": "susan",
    "liz": "elizabeth", "beth": "elizabeth", "betty": "elizabeth",
    "kate": "katherine", "kathy": "katherine", "katie": "kathryn",
    "chris": "christopher",
    "dan": "daniel", "danny": "daniel",
    "pat": "patricia", "patty": "patricia", "trish": "patricia",
    "sam": "samuel",
    "ed": "edward", "eddie": "edward", "ted": "edward",
    "ben": "benjamin",
    "nick": "nicholas",
    "tony": "anthony",
    "andy": "andrew", "drew": "andrew",
    "alex": "alexander",
    "greg": "gregory",
    "ken": "kenneth",
    "steve": "steven",
    "matt": "matthew",
    "jeff": "jeffrey",
    "jerry": "gerald", "gerry": "gerald",
    "chuck": "charles", "charlie": "charles",
    "harry": "harold", "hal": "harold",
    "hank": "henry",
    "jack": "john", "johnny": "john", "jon": "john",
    "peggy": "margaret", "meg": "margaret", "maggie": "margaret",
    "frank": "francis",
    "fred": "frederick",
    "jake": "jacob",
    "lou": "louis", "louie": "louis",
    "ray": "raymond",
    "ron": "ronald", "ronnie": "ronald",
    "stu": "stuart",
    "tim": "timothy", "timmy": "timothy",
    "vince": "vincent",
    "phil": "philip",
    "max": "maximilian",
    "tami": "tamara", "tammy": "tamara",
    "teri": "teresa", "terri": "teresa",
    "gene": "eugene",
    "missy": "melissa",
    "lori": "laura",
    "dee": "diana",
    "bev": "beverly",
    "deb": "deborah", "debbie": "deborah",
    "don": "donald",
    "pam": "pamela",
    "mandy": "amanda",
}

NOISE_EMPLOYERS = {
    "retired", "self", "self employed", "self-employed", "selfemployed",
    "not employed", "not-employed", "na", "n/a", "none", "unknown",
    "homemaker", "student", "unemployed", "housewife", "various", "retire",
}


def to_ascii(s: str) -> str:
    try:
        return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    except Exception:
        return s


def normalize_first_last(last_raw: str | None, first_raw: str | None) -> tuple[str, str]:
    """TEC has separate last/first columns — much easier than SA's parsing."""
    last  = re.sub(r"[^a-z ]", "", to_ascii(last_raw  or "").lower()).strip()
    first = re.sub(r"[^a-z ]", "", to_ascii(first_raw or "").lower()).strip()
    # take only the first token of first name (drop middle initial / suffix)
    first = first.split()[0] if first else ""
    first = NICKNAMES.get(first, first)
    return last, first


def normalize_employer(emp: str | None) -> str:
    s = to_ascii(emp or "").lower().strip()
    s = re.sub(r"\b(inc|llc|corp|co|ltd|pc|lp|pllc|pa)\.?\b", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return "" if s in NOISE_EMPLOYERS else s


def normalize_occupation(occ: str | None) -> str:
    s = to_ascii(occ or "").lower().strip()
    s = re.sub(r"[^a-z ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


# ── Union-Find ─────────────────────────────────────────────────────────────
class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}
        self.rank: dict[int, int] = {}

    def find(self, x: int) -> int:
        if x not in self.parent:
            self.parent[x] = x
            self.rank[x] = 0
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, x: int, y: int) -> None:
        px, py = self.find(x), self.find(y)
        if px == py:
            return
        if self.rank[px] < self.rank[py]:
            px, py = py, px
        self.parent[py] = px
        if self.rank[px] == self.rank[py]:
            self.rank[px] += 1


# ── Scoring ────────────────────────────────────────────────────────────────
FLOOR = 0.78


def score_pair(a: dict, b: dict) -> float:
    last_score  = fuzz.token_sort_ratio(a["last"],  b["last"])  / 100.0
    first_score = fuzz.token_sort_ratio(a["first"], b["first"]) / 100.0

    if last_score < FLOOR or first_score < FLOOR:
        return round(min(0.50 * last_score + 0.50 * first_score, 0.69), 4)

    if a["zip5"] and b["zip5"]:
        zip_score = 1.0 if a["zip5"] == b["zip5"] else 0.0
    else:
        zip_score = 0.5

    if a["emp_occ"] and b["emp_occ"]:
        emp_score = fuzz.token_sort_ratio(a["emp_occ"], b["emp_occ"]) / 100.0
    else:
        emp_score = 0.5

    return round(0.30 * last_score + 0.30 * first_score + 0.30 * zip_score + 0.10 * emp_score, 4)


def derive_donor_id(canonical_name: str, canonical_zip: str) -> str:
    """Stable across runs. Hashes the cluster's representative name+zip."""
    payload = f"{(canonical_name or '').strip().lower()}|{(canonical_zip or '').strip()}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def most_common(lst: list[str]) -> str:
    if not lst:
        return ""
    return max(set(lst), key=lst.count)


def main() -> None:
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    # ── 1. Load active individual contributions ───────────────────────────
    print("Loading individual rows from contributions...")
    cur.execute(
        """
        SELECT contribution_info_id,
               contributor_name_last, contributor_name_first,
               canonical_zip5, contribution_amount, contribution_dt,
               candidate_slug, contributor_employer, contributor_occupation,
               contributor_name_org
        FROM contributions
        WHERE contributor_persent_type='INDIVIDUAL'
          AND COALESCE(info_only_flag,'N')<>'Y'
        """
    )
    raw = cur.fetchall()
    print(f"  {len(raw):,} individual rows")

    records: list[dict] = []
    for cid, last_raw, first_raw, zip5, amt, dt, recipient, emp_raw, occ_raw, _org in raw:
        last, first = normalize_first_last(last_raw, first_raw)
        if not last:
            continue
        emp = normalize_employer(emp_raw)
        occ = normalize_occupation(occ_raw)
        emp_occ = " ".join(sorted(set((emp + " " + occ).split())))
        records.append({
            "cid":   cid,                         # contribution_info_id
            "last":  last,
            "first": first,
            "zip5":  (zip5 or "")[:5],
            "emp_occ": emp_occ,
            "raw_last":  last_raw or "",
            "raw_first": first_raw or "",
            "raw_zip":   zip5 or "",
            "raw_emp":   emp_raw or "",
            "raw_occ":   occ_raw or "",
            "amount":    float(amt or 0),
            "date":      dt or "",
            "recipient": recipient or "",
        })
    print(f"  {len(records):,} normalized")

    # ── 2. Blocking ───────────────────────────────────────────────────────
    print("Blocking pairs...")
    seen_pairs: set[tuple[int, int]] = set()
    pairs: list[tuple[int, int]] = []

    def add_block(blocks: dict[tuple, list[int]], cap: int = 60) -> None:
        for members in blocks.values():
            if len(members) < 2 or len(members) > cap:
                continue
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    a, b = sorted((members[i], members[j]))
                    if (a, b) not in seen_pairs:
                        seen_pairs.add((a, b))
                        pairs.append((a, b))

    block_a: dict[tuple, list[int]] = defaultdict(list)
    for i, r in enumerate(records):
        if r["last"] and r["zip5"]:
            block_a[(r["last"], r["zip5"])].append(i)
    add_block(block_a)
    print(f"  block A (last+zip5):    {len(pairs):,} pairs")

    before = len(pairs)
    block_b: dict[tuple, list[int]] = defaultdict(list)
    for i, r in enumerate(records):
        if r["last"] and r["zip5"]:
            try:
                block_b[(soundex(r["last"]), r["zip5"])].append(i)
            except Exception:
                continue
    add_block(block_b)
    print(f"  block B (soundex+zip):  {len(pairs):,} pairs (+{len(pairs)-before:,})")

    # ── 3. Score + cluster ────────────────────────────────────────────────
    print("Scoring + clustering...")
    uf = UnionFind()
    review_rows: list[tuple] = []
    auto = review = 0

    for i, j in pairs:
        s = score_pair(records[i], records[j])
        if s >= 0.83:
            uf.union(i, j)
            auto += 1
        elif s >= 0.65:
            a, b = records[i], records[j]
            review_rows.append((
                f"{a['raw_last']}, {a['raw_first']}",
                f"{b['raw_last']}, {b['raw_first']}",
                a["zip5"], b["zip5"],
                a["emp_occ"], b["emp_occ"], s,
            ))
            review += 1
    print(f"  auto-merged: {auto:,}    review_queue: {review:,}")

    # ── 4. Aggregate clusters ─────────────────────────────────────────────
    print("Aggregating clusters...")
    clusters: dict[int, dict] = defaultdict(lambda: {
        "names": [], "zips": [], "employers": [],
        "total": 0.0, "recipients": set(),
        "first_seen": "99999999", "last_seen": "0",
        "rec_idxs": [],
    })
    for i, r in enumerate(records):
        root = uf.find(i)
        c = clusters[root]
        # Use a stable display form for the canonical_name vote
        c["names"].append(f"{r['raw_last']}, {r['raw_first']}".strip(", ").strip())
        if r["zip5"]:
            c["zips"].append(r["zip5"])
        if r["emp_occ"]:
            c["employers"].append(r["emp_occ"])
        c["total"] += r["amount"]
        c["recipients"].add(r["recipient"])
        if r["date"] and r["date"] < c["first_seen"]:
            c["first_seen"] = r["date"]
        if r["date"] and r["date"] > c["last_seen"]:
            c["last_seen"] = r["date"]
        c["rec_idxs"].append(i)

    # Stable donor_id per cluster
    rec_to_donor: dict[int, str] = {}
    identity_rows: list[tuple] = []
    for root, c in clusters.items():
        canonical_name = most_common(c["names"])
        canonical_zip  = most_common(c["zips"])
        canonical_emp  = most_common(c["employers"])
        donor_id = derive_donor_id(canonical_name, canonical_zip)
        first_seen = c["first_seen"] if c["first_seen"] != "99999999" else ""
        last_seen  = c["last_seen"]  if c["last_seen"]  != "0"        else ""
        identity_rows.append((
            donor_id,
            canonical_name,
            canonical_zip,
            canonical_emp,
            round(c["total"], 2),
            len(c["recipients"]),
            "|".join(sorted(c["recipients"])),
            len(c["rec_idxs"]),
            first_seen,
            last_seen,
        ))
        for idx in c["rec_idxs"]:
            rec_to_donor[idx] = donor_id

    # Hash collisions across distinct clusters would silently merge identities.
    seen_ids = {row[0] for row in identity_rows}
    if len(seen_ids) != len(identity_rows):
        # Tie-break by appending cluster member count + smallest cid; this
        # collision is theoretically possible (sha1 truncated to 16 hex)
        # but extremely unlikely on <1k clusters; bail loudly if it happens.
        raise SystemExit(
            f"donor_id collision: {len(identity_rows)} clusters → {len(seen_ids)} ids. "
            "Bump the donor_id length in derive_donor_id()."
        )

    # ── 5. Write donor_identities ─────────────────────────────────────────
    print("Writing donor_identities + review_queue...")
    cur.execute("DROP TABLE IF EXISTS donor_identities")
    cur.execute("""
        CREATE TABLE donor_identities (
            donor_id            TEXT PRIMARY KEY,
            canonical_name      TEXT,
            canonical_zip       TEXT,
            canonical_employer  TEXT,
            total_donated       REAL,
            campaign_count      INTEGER,
            campaigns           TEXT,
            record_count        INTEGER,
            first_seen          TEXT,
            last_seen           TEXT,
            -- FEC enrichment columns (filled by fec_enrich_hd41.py)
            fec_partisan_lean   REAL,
            fec_total_dem       REAL DEFAULT 0,
            fec_total_rep       REAL DEFAULT 0,
            fec_total_other     REAL DEFAULT 0,
            fec_total_donations INTEGER DEFAULT 0,
            fec_matched         INTEGER DEFAULT 0,
            fec_matched_at      TEXT
        )
    """)
    cur.executemany(
        "INSERT INTO donor_identities (donor_id, canonical_name, canonical_zip, "
        "canonical_employer, total_donated, campaign_count, campaigns, "
        "record_count, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?)",
        identity_rows,
    )

    # Review queue
    cur.execute("DROP TABLE IF EXISTS review_queue")
    cur.execute("""
        CREATE TABLE review_queue (
            donor_a   TEXT, donor_b   TEXT,
            zip_a     TEXT, zip_b     TEXT,
            emp_occ_a TEXT, emp_occ_b TEXT,
            score     REAL,
            resolved  INTEGER DEFAULT 0
        )
    """)
    cur.executemany(
        "INSERT INTO review_queue VALUES (?,?,?,?,?,?,?,0)",
        review_rows,
    )

    # ── 6. Stamp donor_id + match_confidence onto contributions ───────────
    for col_def in ["donor_id TEXT", "match_confidence TEXT"]:
        col = col_def.split()[0]
        try:
            cur.execute(f"ALTER TABLE contributions ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass  # column exists
    # Reset before re-stamping (idempotent)
    cur.execute("UPDATE contributions SET donor_id = NULL, match_confidence = NULL")

    updates: list[tuple] = []
    for idx, donor_id in rec_to_donor.items():
        cluster_size = len(clusters[uf.find(idx)]["rec_idxs"])
        confidence = "exact" if cluster_size == 1 else "high"
        updates.append((donor_id, confidence, records[idx]["cid"]))
    cur.executemany(
        "UPDATE contributions SET donor_id=?, match_confidence=? WHERE contribution_info_id=?",
        updates,
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_contrib_donor ON contributions(donor_id)")

    conn.commit()
    conn.close()

    # ── Summary ───────────────────────────────────────────────────────────
    n_clusters = len(clusters)
    merged = sum(1 for c in clusters.values() if len(c["rec_idxs"]) > 1)
    print()
    print("=== DONE ===")
    print(f"  Records processed:      {len(records):,}")
    print(f"  Unique donor_ids:       {n_clusters:,}")
    print(f"  Merged identities:      {merged:,}  (one person, multiple gifts)")
    print(f"  Singleton identities:   {n_clusters - merged:,}")
    print(f"  Review queue entries:   {review:,}  (manual-check pairs, score 0.65-0.83)")
    print(f"  Tables: donor_identities, review_queue")
    print(f"  contributions stamped with donor_id + match_confidence")


if __name__ == "__main__":
    main()
