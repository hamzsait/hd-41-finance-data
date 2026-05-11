"""HD-41 affiliations schema (port of SA's affiliations_schema.py).

Creates donor_affiliations + donor_affiliation_evidence with the same shape
SA used, so generate_profile_data.py can read both projects with one query.

Idempotent — re-runnable. clear_category() helper wipes a single category so
the integrator can replace per-category findings cleanly.

Categories (canonical strings):
    aipac, adl, zionist_general,
    oil_gas, real_estate, mic,
    fec_partisan, tec_partisan
"""
from __future__ import annotations
import pathlib
import sqlite3
import sys

DB_PATH = pathlib.Path(__file__).parent / "hd41_finance.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS donor_affiliations (
    affiliation_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    donor_id         TEXT    NOT NULL,
    category         TEXT    NOT NULL,
    label            TEXT    NOT NULL,
    total_amount     REAL,
    confidence       TEXT,
    first_seen       TEXT,
    last_seen        TEXT,
    notes            TEXT,
    sensitive        INTEGER DEFAULT 0,
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(donor_id, category, label)
);
CREATE INDEX IF NOT EXISTS idx_aff_donor    ON donor_affiliations(donor_id);
CREATE INDEX IF NOT EXISTS idx_aff_category ON donor_affiliations(category);

CREATE TABLE IF NOT EXISTS donor_affiliation_evidence (
    evidence_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    affiliation_id    INTEGER NOT NULL,
    source            TEXT    NOT NULL,
    source_url        TEXT,
    evidence_text     TEXT,
    contribution_id   TEXT,
    committee_id      TEXT,
    committee_name    TEXT,
    amount            REAL,
    date              TEXT,
    raw_data          TEXT,
    rule              TEXT,
    FOREIGN KEY(affiliation_id) REFERENCES donor_affiliations(affiliation_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ev_aff       ON donor_affiliation_evidence(affiliation_id);
CREATE INDEX IF NOT EXISTS idx_ev_committee ON donor_affiliation_evidence(committee_id);
"""

VALID_CATEGORIES = {
    "aipac", "adl", "zionist_general",
    "oil_gas", "real_estate", "mic",
    "fec_partisan", "tec_partisan",
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def clear_category(conn: sqlite3.Connection, category: str) -> tuple[int, int]:
    if category not in VALID_CATEGORIES:
        raise ValueError(f"unknown category {category!r}; valid: {sorted(VALID_CATEGORIES)}")
    cur = conn.cursor()
    aff_ids = [r[0] for r in cur.execute(
        "SELECT affiliation_id FROM donor_affiliations WHERE category=?", (category,)
    ).fetchall()]
    if not aff_ids:
        return 0, 0
    ph = ",".join("?" * len(aff_ids))
    ev = cur.execute(
        f"DELETE FROM donor_affiliation_evidence WHERE affiliation_id IN ({ph})", aff_ids,
    ).rowcount
    aff = cur.execute(
        f"DELETE FROM donor_affiliations WHERE affiliation_id IN ({ph})", aff_ids,
    ).rowcount
    conn.commit()
    return aff, ev


def main() -> int:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        ensure_schema(conn)
        n_aff = conn.execute("SELECT COUNT(*) FROM donor_affiliations").fetchone()[0]
        n_ev  = conn.execute("SELECT COUNT(*) FROM donor_affiliation_evidence").fetchone()[0]
        print(f"[schema] donor_affiliations={n_aff} rows  donor_affiliation_evidence={n_ev} rows")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
