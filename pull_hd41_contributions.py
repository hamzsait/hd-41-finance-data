"""HD-41 — extract Schedule A contributions from the TEC bulk dump.

Scope:
    Texas House District 41 (Hidalgo County) 2026 race. Five candidates:
        - Victor "Seby" Haddad (D)         filerIdent 00090127  [runoff]
        - Julio Mauricio Salinas (D)       filerIdent 00090159  [runoff]
        - Sergio Sanchez (R)               filerIdent 00089992  [runoff]
        - Gary Groves (R)                  filerIdent 00090204  [runoff]
        - Eric Holguín (D)                 filerIdent 00083896  [eliminated in primary]

    Phase: raw ETL only. No enrichment, no FEC cross-ref, no affiliations.
    Four filers are COH accounts newly registered for HD-41 in late 2025.
    Holguin's filer 00083896 was originally registered for his 2020 HD-32
    run and reused for HD-41 (TEC allows updating ctaSeekOfficeCd in place).
    For Holguin we filter to HD-41 cycle reports only via district_filter='41';
    his 5 HD-32 reports + 2 wrap-up reports are excluded.

    Sergio Sanchez also has a prior SCC party-chair filer (00069847) — out of
    scope, excluded entirely.

Inputs (from TEC bulk zip already downloaded into ./tec_data/TEC_CF_CSV.zip):
    cover.csv          — one row per filed report (header / cover sheet)
    contribs_##.csv    — 99 shards of Schedule A/C contribution rows

Output:
    hd41_finance.db    — SQLite, four tables: candidates, candidate_filers,
                         reports, contributions.

Idempotency:
    Schema is created with IF NOT EXISTS. Row writes use INSERT OR REPLACE
    on the natural keys:
        candidates.candidate_slug      (we own the slug)
        candidate_filers.PK            (slug, filer_ident)
        reports.report_info_ident      (TEC's report PK)
        contributions.contribution_info_id  (TEC's row PK)
    Re-running against a refreshed bulk zip is safe.

CLI:
    python pull_hd41_contributions.py            -- full ingest
    python pull_hd41_contributions.py --report   -- print summary, no write
    python pull_hd41_contributions.py --reset    -- drop + recreate tables, then ingest

Quirks (verified during sanity-check 2026-05-09):
    - reportTypeCd is blank in cover.csv for every HD-41 report. Use formTypeCd
      to distinguish original (COH) vs corrected (CORCOH) reports instead.
    - When a filer files a CORCOH, every row in the original report ends up with
      infoOnlyFlag='Y' and the CORCOH report adds new rows with new
      contributionInfoIds. Filter info_only_flag != 'Y' for analysis.
    - Salinas's Jul-Dec 2025 semi-annual was corrected (101031344 -> 101033938),
      doubling the row count for that period. Active sum still reconciles.
    - schedFormTypeCd in (A1, A2): A1 = monetary individual contribs,
      A2 = non-monetary / in-kind. Both are Schedule A.
    - 5/350 active rows for Salinas + Sanchez collide on content_hash. These are
      real same-donor / same-date / same-amount pairs (typically Mr. + Mrs.
      donations from the same household), NOT amendment artifacts. So
      contributionInfoId is the safer primary key; content_hash is stored as a
      secondary index for later cross-source dedup.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ROOT = Path(r"C:\Users\Hamza Sait\Electoral\HD-41")
TEC_DIR = ROOT / "tec_data"
ZIP_PATH = TEC_DIR / "TEC_CF_CSV.zip"
DB_PATH = ROOT / "hd41_finance.db"

# Each tuple: (candidate_slug, full_name, party, filer_ident, district_filter).
# Single filer per candidate — verified via find_filers.py against filers.csv.
# district_filter:
#   None  — take all reports for this filer (used when the filer is brand-new).
#   '41'  — only take reports where filerSeekOfficeDistrict='41' (used when
#           the filer was reused from a prior cycle, e.g. Holguin's 2020 HD-32
#           filer reused for 2026 HD-41).
CANDIDATES: list[tuple[str, str, str, str, str | None]] = [
    ("haddad",  "Victor 'Seby' Haddad",   "D", "00090127", None),
    ("salinas", "Julio Mauricio Salinas", "D", "00090159", None),
    ("sanchez", "Sergio Sanchez",         "R", "00089992", None),
    ("groves",  "Gary Groves",            "R", "00090204", None),
    ("holguin", "Eric Holguín",           "D", "00083896", "41"),
]

FILER_TO_SLUG   = {fid: slug for slug, _, _, fid, _ in CANDIDATES}
SLUG_TO_NAME    = {slug: name for slug, name, _, _, _ in CANDIDATES}
SLUG_TO_DISTFLT = {slug: dflt for slug, _, _, _, dflt in CANDIDATES}

# csv module's default field-size limit chokes on TEC's wide cover-sheet rows.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS candidates (
    candidate_slug      TEXT PRIMARY KEY,
    full_name           TEXT NOT NULL,
    party               TEXT NOT NULL,
    filer_ident         TEXT NOT NULL,
    filing_start_date   TEXT,
    notes               TEXT,
    ingested_at         TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS candidate_filers (
    candidate_slug      TEXT NOT NULL,
    filer_ident         TEXT NOT NULL,
    filer_type_cd       TEXT,
    filer_name          TEXT,
    role                TEXT,
    PRIMARY KEY (candidate_slug, filer_ident),
    FOREIGN KEY (candidate_slug) REFERENCES candidates(candidate_slug)
);

CREATE TABLE IF NOT EXISTS reports (
    report_info_ident      TEXT PRIMARY KEY,
    candidate_slug         TEXT NOT NULL,
    filer_ident            TEXT NOT NULL,
    form_type_cd           TEXT,
    report_type_cd         TEXT,
    period_start_dt        TEXT,
    period_end_dt          TEXT,
    filed_dt               TEXT,
    received_dt            TEXT,
    election_dt            TEXT,
    election_type_cd       TEXT,
    total_contrib_amount   REAL,
    unitemized_contrib_amount REAL,
    info_only_flag         TEXT,
    no_activity_flag       TEXT,
    ingested_at            TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (candidate_slug) REFERENCES candidates(candidate_slug)
);
CREATE INDEX IF NOT EXISTS idx_reports_cand ON reports(candidate_slug);
CREATE INDEX IF NOT EXISTS idx_reports_filer ON reports(filer_ident);

CREATE TABLE IF NOT EXISTS contributions (
    contribution_info_id        TEXT PRIMARY KEY,
    report_info_ident           TEXT NOT NULL,
    candidate_slug              TEXT NOT NULL,
    filer_ident                 TEXT NOT NULL,
    -- report-row metadata
    form_type_cd                TEXT,
    sched_form_type_cd          TEXT,
    received_dt                 TEXT,
    info_only_flag              TEXT,
    -- contribution
    contribution_dt             TEXT,
    contribution_amount         REAL,
    contribution_descr          TEXT,
    itemize_flag                TEXT,
    travel_flag                 TEXT,
    -- contributor
    contributor_persent_type    TEXT,
    contributor_name_org        TEXT,
    contributor_name_last       TEXT,
    contributor_name_first      TEXT,
    contributor_name_suffix     TEXT,
    contributor_name_prefix     TEXT,
    contributor_street_city     TEXT,
    contributor_street_state    TEXT,
    contributor_street_zip      TEXT,
    contributor_street_county   TEXT,
    contributor_street_country  TEXT,
    contributor_employer        TEXT,
    contributor_occupation      TEXT,
    contributor_job_title       TEXT,
    contributor_pac_fein        TEXT,
    contributor_oos_pac_flag    TEXT,
    contributor_law_firm_name   TEXT,
    -- canonical / dedup
    canonical_name              TEXT,
    canonical_zip5              TEXT,
    content_hash                TEXT,
    -- provenance
    source_csv                  TEXT,
    ingested_at                 TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (report_info_ident) REFERENCES reports(report_info_ident),
    FOREIGN KEY (candidate_slug)    REFERENCES candidates(candidate_slug)
);
CREATE INDEX IF NOT EXISTS idx_contrib_cand        ON contributions(candidate_slug);
CREATE INDEX IF NOT EXISTS idx_contrib_filer       ON contributions(filer_ident);
CREATE INDEX IF NOT EXISTS idx_contrib_report      ON contributions(report_info_ident);
CREATE INDEX IF NOT EXISTS idx_contrib_canonical   ON contributions(canonical_name, canonical_zip5);
CREATE INDEX IF NOT EXISTS idx_contrib_hash        ON contributions(content_hash);
CREATE INDEX IF NOT EXISTS idx_contrib_active      ON contributions(info_only_flag);
CREATE INDEX IF NOT EXISTS idx_contrib_date        ON contributions(contribution_dt);
"""


def open_db(reset: bool = False) -> sqlite3.Connection:
    if reset and DB_PATH.exists():
        DB_PATH.unlink()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_SQL)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(s: str | None) -> float | None:
    if s is None or s == "":
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _zip5(s: str | None) -> str | None:
    if not s:
        return None
    digits = "".join(ch for ch in s if ch.isdigit())
    if not digits:
        return None
    return digits[:5]


def _canonical_name(row: dict) -> str | None:
    """For individuals: 'Last, First'. For entities: org name. Lowercased &
    stripped. Used as a join key for cross-cycle / cross-source matching."""
    persent = (row.get("contributorPersentTypeCd") or "").upper()
    if persent == "INDIVIDUAL":
        last = (row.get("contributorNameLast") or "").strip().lower()
        first = (row.get("contributorNameFirst") or "").strip().lower()
        if not last:
            return None
        return f"{last}, {first}".strip(", ").strip()
    if persent == "ENTITY":
        org = (row.get("contributorNameOrganization") or "").strip().lower()
        return org or None
    return None


def _content_hash(row: dict) -> str:
    """SA-style stable natural key. Stored as a SECONDARY dedup index — not the
    primary key, because it overcollapses real same-day same-amount paired
    donations from one household. Use for cross-source dedup later."""
    parts = [
        row.get("filerIdent") or "",
        (row.get("contributorPersentTypeCd") or "").lower(),
        (row.get("contributorNameLast") or "").lower().strip(),
        (row.get("contributorNameFirst") or "").lower().strip(),
        (row.get("contributorNameOrganization") or "").lower().strip(),
        (row.get("contributorStreetCity") or "").lower().strip(),
        (row.get("contributorStreetStateCd") or "").lower().strip(),
        (row.get("contributorStreetPostalCode") or "").strip(),
        row.get("contributionDt") or "",
        row.get("contributionAmount") or "",
        (row.get("schedFormTypeCd") or "").lower(),
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Step 1 — candidates + candidate_filers
# ---------------------------------------------------------------------------

def upsert_candidates(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    for slug, name, party, fid, dflt in CANDIDATES:
        notes = (
            f"district_filter='{dflt}' (filer reused from prior cycle)"
            if dflt else None
        )
        cur.execute(
            """
            INSERT INTO candidates (candidate_slug, full_name, party, filer_ident, filing_start_date, notes)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(candidate_slug) DO UPDATE SET
                full_name = excluded.full_name,
                party = excluded.party,
                filer_ident = excluded.filer_ident,
                notes = excluded.notes
            """,
            (slug, name, party, fid, None, notes),
        )
        cur.execute(
            """
            INSERT OR REPLACE INTO candidate_filers
                (candidate_slug, filer_ident, filer_type_cd, filer_name, role)
            VALUES (?,?,?,?,?)
            """,
            (slug, fid, "COH", name, "hd41_coh"),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Step 2 — reports (cover.csv)
# ---------------------------------------------------------------------------

def ingest_reports(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Returns (cover_rows_scanned, reports_for_our_filers, reports_kept_after_district_filter)."""
    cur = conn.cursor()
    scanned = 0
    matched = 0
    inserted = 0
    cover_path = TEC_DIR / "cover.csv"
    if not cover_path.exists():
        # extract on demand
        with zipfile.ZipFile(ZIP_PATH) as zf:
            zf.extract("cover.csv", TEC_DIR)
    with open(cover_path, encoding="utf-8", errors="replace", newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            scanned += 1
            fid = row.get("filerIdent")
            slug = FILER_TO_SLUG.get(fid)
            if not slug:
                continue
            matched += 1
            # district filter: drop reports from other cycles when the filer
            # was reused (e.g. Holguin's 2020 HD-32 reports on the same filer)
            dflt = SLUG_TO_DISTFLT.get(slug)
            if dflt is not None:
                if (row.get("filerSeekOfficeDistrict") or "") != dflt:
                    continue
            cur.execute(
                """
                INSERT OR REPLACE INTO reports (
                    report_info_ident, candidate_slug, filer_ident,
                    form_type_cd, report_type_cd,
                    period_start_dt, period_end_dt, filed_dt, received_dt,
                    election_dt, election_type_cd,
                    total_contrib_amount, unitemized_contrib_amount,
                    info_only_flag, no_activity_flag
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row.get("reportInfoIdent"),
                    slug,
                    fid,
                    row.get("formTypeCd"),
                    row.get("reportTypeCd"),
                    row.get("periodStartDt"),
                    row.get("periodEndDt"),
                    row.get("filedDt"),
                    row.get("receivedDt"),
                    row.get("electionDt"),
                    row.get("electionTypeCd"),
                    _safe_float(row.get("totalContribAmount")),
                    _safe_float(row.get("unitemizedContribAmount")),
                    row.get("infoOnlyFlag"),
                    row.get("noActivityFlag"),
                ),
            )
            inserted += 1
    conn.commit()

    # Patch each candidate's filing_start_date as the earliest receivedDt across
    # their non-superseded reports. A small UI quality-of-life thing.
    cur.execute(
        """
        UPDATE candidates
           SET filing_start_date = (
               SELECT MIN(received_dt) FROM reports
                WHERE reports.candidate_slug = candidates.candidate_slug
                  AND COALESCE(reports.info_only_flag, 'N') <> 'Y'
           )
        """
    )
    conn.commit()
    return scanned, matched, inserted


# ---------------------------------------------------------------------------
# Step 3 — contributions (contribs_##.csv shards)
# ---------------------------------------------------------------------------

CONTRIB_INSERT = """
INSERT OR REPLACE INTO contributions (
    contribution_info_id, report_info_ident, candidate_slug, filer_ident,
    form_type_cd, sched_form_type_cd, received_dt, info_only_flag,
    contribution_dt, contribution_amount, contribution_descr,
    itemize_flag, travel_flag,
    contributor_persent_type, contributor_name_org,
    contributor_name_last, contributor_name_first, contributor_name_suffix,
    contributor_name_prefix,
    contributor_street_city, contributor_street_state, contributor_street_zip,
    contributor_street_county, contributor_street_country,
    contributor_employer, contributor_occupation, contributor_job_title,
    contributor_pac_fein, contributor_oos_pac_flag, contributor_law_firm_name,
    canonical_name, canonical_zip5, content_hash,
    source_csv
) VALUES (
    ?,?,?,?, ?,?,?,?, ?,?,?, ?,?, ?,?, ?,?,?,?,
    ?,?,?, ?,?, ?,?,?, ?,?,?,
    ?,?,?, ?
)
"""


def ingest_contributions(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Returns (rows_scanned, rows_for_our_filers, rows_kept_after_report_filter)."""
    cur = conn.cursor()
    total_scanned = 0
    total_matched = 0
    total_inserted = 0
    BATCH = 1000

    # Build the set of reportInfoIdents that survived the district filter.
    # Contributions whose report didn't pass are skipped (this is how we drop
    # Holguin's HD-32-cycle contribs while keeping his HD-41-cycle contribs).
    allowed_reports: set[str] = {
        r[0] for r in cur.execute("SELECT report_info_ident FROM reports")
    }
    print(f"  district filter: {len(allowed_reports)} report(s) eligible for contrib ingest")

    with zipfile.ZipFile(ZIP_PATH) as zf:
        members = sorted(
            m for m in zf.namelist()
            if m.startswith("contribs_") and m.endswith(".csv")
        )
        for member in members:
            with zf.open(member) as raw:
                text = (line.decode("utf-8", errors="replace") for line in raw)
                rdr = csv.DictReader(text)
                shard_hits = 0
                shard_scanned = 0
                batch: list[tuple] = []
                for row in rdr:
                    shard_scanned += 1
                    fid = row.get("filerIdent")
                    slug = FILER_TO_SLUG.get(fid)
                    if not slug:
                        continue
                    total_matched += 1
                    # filter: only keep contributions whose report survived
                    # the cycle/district filter applied during ingest_reports
                    if (row.get("reportInfoIdent") or "") not in allowed_reports:
                        continue
                    shard_hits += 1
                    batch.append((
                        row.get("contributionInfoId"),
                        row.get("reportInfoIdent"),
                        slug,
                        fid,
                        row.get("formTypeCd"),
                        row.get("schedFormTypeCd"),
                        row.get("receivedDt"),
                        row.get("infoOnlyFlag"),
                        row.get("contributionDt"),
                        _safe_float(row.get("contributionAmount")),
                        row.get("contributionDescr"),
                        row.get("itemizeFlag"),
                        row.get("travelFlag"),
                        row.get("contributorPersentTypeCd"),
                        row.get("contributorNameOrganization"),
                        row.get("contributorNameLast"),
                        row.get("contributorNameFirst"),
                        row.get("contributorNameSuffixCd"),
                        row.get("contributorNamePrefixCd"),
                        row.get("contributorStreetCity"),
                        row.get("contributorStreetStateCd"),
                        row.get("contributorStreetPostalCode"),
                        row.get("contributorStreetCountyCd"),
                        row.get("contributorStreetCountryCd"),
                        row.get("contributorEmployer"),
                        row.get("contributorOccupation"),
                        row.get("contributorJobTitle"),
                        row.get("contributorPacFein"),
                        row.get("contributorOosPacFlag"),
                        row.get("contributorLawFirmName"),
                        _canonical_name(row),
                        _zip5(row.get("contributorStreetPostalCode")),
                        _content_hash(row),
                        member,
                    ))
                    if len(batch) >= BATCH:
                        cur.executemany(CONTRIB_INSERT, batch)
                        total_inserted += len(batch)
                        batch.clear()
                if batch:
                    cur.executemany(CONTRIB_INSERT, batch)
                    total_inserted += len(batch)
                total_scanned += shard_scanned
                if shard_hits:
                    print(f"  {member}: {shard_hits} hits / {shard_scanned} rows")
            conn.commit()
    return total_scanned, total_matched, total_inserted


# ---------------------------------------------------------------------------
# Step 4 — report
# ---------------------------------------------------------------------------

def emit_report(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    print("\n=== candidates ===")
    for r in cur.execute(
        """
        SELECT candidate_slug, full_name, party, filer_ident, filing_start_date
          FROM candidates ORDER BY candidate_slug
        """
    ):
        print(f"  {r[0]:<8} {r[2]:<2}  {r[1]:<28}  filer={r[3]}  first_filed={r[4] or '-'}")

    print("\n=== reports per candidate ===")
    for r in cur.execute(
        """
        SELECT c.candidate_slug,
               SUM(CASE WHEN COALESCE(r.info_only_flag,'N')<>'Y' THEN 1 ELSE 0 END) AS active,
               SUM(CASE WHEN r.info_only_flag='Y' THEN 1 ELSE 0 END)                AS superseded,
               COUNT(*)                                                              AS total
          FROM candidates c
          LEFT JOIN reports r ON r.candidate_slug = c.candidate_slug
         GROUP BY c.candidate_slug
         ORDER BY c.candidate_slug
        """
    ):
        print(f"  {r[0]:<8}  active={r[1]}  superseded={r[2]}  total={r[3]}")

    print("\n=== contributions per candidate (active rows only) ===")
    for r in cur.execute(
        """
        SELECT c.candidate_slug, c.full_name,
               COUNT(*)                       AS rows_active,
               COUNT(DISTINCT cn.content_hash) AS rows_dedup,
               ROUND(SUM(cn.contribution_amount),2) AS amount,
               MIN(cn.contribution_dt) AS earliest,
               MAX(cn.contribution_dt) AS latest
          FROM candidates c
          JOIN contributions cn ON cn.candidate_slug = c.candidate_slug
         WHERE COALESCE(cn.info_only_flag,'N') <> 'Y'
         GROUP BY c.candidate_slug
         ORDER BY amount DESC
        """
    ):
        print(
            f"  {r[0]:<8}  rows_active={r[2]:<4}  rows_dedup={r[3]:<4}  "
            f"${r[4]:>14,.2f}   {r[5]} -> {r[6]}"
        )

    print("\n=== reconciliation: cover.totalContribAmount vs sum of contribs (active reports + active rows) ===")
    for r in cur.execute(
        """
        WITH cov AS (
            SELECT candidate_slug,
                   ROUND(SUM(total_contrib_amount),2) AS cover_active
              FROM reports
             WHERE COALESCE(info_only_flag,'N') <> 'Y'
             GROUP BY candidate_slug
        ),
        con AS (
            SELECT candidate_slug,
                   ROUND(SUM(contribution_amount),2) AS contrib_active
              FROM contributions
             WHERE COALESCE(info_only_flag,'N') <> 'Y'
             GROUP BY candidate_slug
        )
        SELECT c.candidate_slug, c.full_name,
               cov.cover_active, con.contrib_active,
               ROUND(COALESCE(cov.cover_active,0) - COALESCE(con.contrib_active,0),2) AS diff
          FROM candidates c
          LEFT JOIN cov ON cov.candidate_slug = c.candidate_slug
          LEFT JOIN con ON con.candidate_slug = c.candidate_slug
         ORDER BY c.candidate_slug
        """
    ):
        ok = "OK" if r[4] == 0 else "MISMATCH"
        print(
            f"  {r[0]:<8}  cover=${r[2] or 0:>13,.2f}  "
            f"contribs=${r[3] or 0:>13,.2f}  diff=${r[4] or 0:>10,.2f}  [{ok}]"
        )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report",  action="store_true", help="print summary, do not ingest")
    ap.add_argument("--reset",   action="store_true", help="drop + recreate DB before ingest")
    args = ap.parse_args()

    if args.report:
        if not DB_PATH.exists():
            print(f"[!] {DB_PATH} does not exist yet; run without --report first.", file=sys.stderr)
            return 1
        conn = sqlite3.connect(DB_PATH)
        emit_report(conn)
        conn.close()
        return 0

    if not ZIP_PATH.exists():
        print(f"[!] missing {ZIP_PATH}. Download TEC_CF_CSV.zip first.", file=sys.stderr)
        return 2

    t0 = time.time()
    conn = open_db(reset=args.reset)

    print("[1/3] upserting candidates + filers...")
    upsert_candidates(conn)

    print("[2/3] ingesting cover.csv (reports)...")
    rs_scanned, rs_matched, rs_in = ingest_reports(conn)
    print(f"      scanned {rs_scanned:,} cover rows; matched {rs_matched:,} for our filers; "
          f"kept {rs_in:,} after district/cycle filter")

    print("[3/3] ingesting contribs_##.csv (Schedule A contributions)...")
    cs_scanned, cs_matched, cs_in = ingest_contributions(conn)
    print(f"      scanned {cs_scanned:,} contrib rows; matched {cs_matched:,} for our filers; "
          f"kept {cs_in:,} after report filter")

    emit_report(conn)
    conn.close()
    print(f"\n[done in {time.time()-t0:.1f}s]  -> {DB_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
