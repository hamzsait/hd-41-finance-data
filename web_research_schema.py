"""HD-41 web-research schema.

Creates donor_web_research + donor_web_research_evidence — a parallel pair to
donor_affiliations + donor_affiliation_evidence, but populated by per-donor
web search rather than by FEC committee-id pattern matching. The two systems
live side-by-side in the same DB.

Categories (canonical strings):
    medical, adl, aipac, dmfi, jstreet, real_estate, oil_gas,
    _searched_no_results

`_searched_no_results` is a sentinel row written for any donor that was
researched but produced no meaningful affiliations — so a re-run knows to
skip them.

Idempotent — re-runnable. clear_category() helper wipes a single category so
the integrator can replace per-category findings cleanly.
"""
from __future__ import annotations
import pathlib
import sqlite3
import sys

# The worktree-local DB is gitignored. The real DB lives in the main HD-41
# directory (one level above the .claude/worktrees/* path); referenced as
# absolute so this works from any worktree.
DB_PATH = pathlib.Path(r"C:\Users\Hamza Sait\Electoral\HD-41\hd41_finance.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS donor_web_research (
    research_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    donor_id      TEXT    NOT NULL,
    category      TEXT    NOT NULL,
    label         TEXT,
    total_amount  REAL,
    confidence    TEXT,
    sensitive     INTEGER DEFAULT 0,
    notes         TEXT,
    first_seen    TEXT,
    last_seen     TEXT,
    created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(donor_id, category, label)
);
CREATE INDEX IF NOT EXISTS idx_wr_donor    ON donor_web_research(donor_id);
CREATE INDEX IF NOT EXISTS idx_wr_category ON donor_web_research(category);

CREATE TABLE IF NOT EXISTS donor_web_research_evidence (
    evidence_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    research_id    INTEGER NOT NULL REFERENCES donor_web_research(research_id) ON DELETE CASCADE,
    source         TEXT,
    source_url     TEXT NOT NULL,
    evidence_text  TEXT,
    snippet        TEXT,
    search_query   TEXT,
    retrieved_at   TEXT,
    rule           TEXT
);
CREATE INDEX IF NOT EXISTS idx_wre_res ON donor_web_research_evidence(research_id);
"""

VALID_CATEGORIES = {
    "medical", "adl", "aipac", "dmfi", "jstreet",
    "real_estate", "oil_gas",
    "_searched_no_results",
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def clear_category(conn: sqlite3.Connection, category: str) -> tuple[int, int]:
    if category not in VALID_CATEGORIES:
        raise ValueError(f"unknown category {category!r}; valid: {sorted(VALID_CATEGORIES)}")
    cur = conn.cursor()
    res_ids = [r[0] for r in cur.execute(
        "SELECT research_id FROM donor_web_research WHERE category=?", (category,)
    ).fetchall()]
    if not res_ids:
        return 0, 0
    ph = ",".join("?" * len(res_ids))
    ev = cur.execute(
        f"DELETE FROM donor_web_research_evidence WHERE research_id IN ({ph})", res_ids,
    ).rowcount
    res = cur.execute(
        f"DELETE FROM donor_web_research WHERE research_id IN ({ph})", res_ids,
    ).rowcount
    conn.commit()
    return res, ev


def main() -> int:
    conn = sqlite3.connect(str(DB_PATH))
    try:
        ensure_schema(conn)
        n_res = conn.execute("SELECT COUNT(*) FROM donor_web_research").fetchone()[0]
        n_ev  = conn.execute("SELECT COUNT(*) FROM donor_web_research_evidence").fetchone()[0]
        print(f"[schema] donor_web_research={n_res} rows  donor_web_research_evidence={n_ev} rows")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
