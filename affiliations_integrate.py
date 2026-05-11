"""
affiliations_integrate.py
Single-writer integrator for affiliation findings.

Each subagent writes a findings_<category>.json file with the schema below.
This script reads those files and idempotently writes their content into
donor_affiliations + donor_affiliation_evidence. Running it twice on the
same input is safe (the category is wiped + replaced, not appended).

Findings JSON schema:
{
  "category": "<one of VALID_CATEGORIES>",
  "rules": [                              # optional — documents the matching rules
    {"name": "...", "description": "..."}
  ],
  "findings": [
    {
      "donor_id": "uuid",
      "label": "human-readable affiliation label",
      "total_amount": 1234.0,              # null if N/A
      "confidence": "high"|"medium"|"low",
      "first_seen": "YYYY-MM-DD"|null,
      "last_seen":  "YYYY-MM-DD"|null,
      "notes": "optional explanation",
      "sensitive": false,                  # true → flagged for human review
      "evidence": [
        {
          "source": "FEC schedule_a"|"TEC bulk"|"employer_match"|"civic_affiliations"|...,
          "source_url": "https://...",
          "evidence_text": "Contributed $500 to AIPAC PAC on 2024-06-15",
          "contribution_id": "fec_contributions_raw.id=12345",
          "committee_id": "C00797670",
          "committee_name": "AIPAC PAC",
          "amount": 500.0,
          "date": "2024-06-15",
          "raw_data": "{...optional JSON blob...}",
          "rule": "fec_committee_id_match"
        },
        ...
      ]
    },
    ...
  ]
}

Usage:
    python affiliations_integrate.py findings_aipac.json
    python affiliations_integrate.py findings_*.json     # multiple at once
    python affiliations_integrate.py --all               # auto-discover findings_*.json

Behaviour:
    For each input file, the named category is wiped from the DB and the
    new findings are inserted. Donors not present in donor_identities are
    silently dropped with a warning.
"""

from __future__ import annotations
import argparse
import glob
import json
import pathlib
import sqlite3
import sys

from affiliations_schema import ensure_schema, clear_category, VALID_CATEGORIES, DB_PATH


def _coerce_amount(v) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def integrate(conn: sqlite3.Connection, findings_path: pathlib.Path) -> dict:
    """Load one findings JSON and write it to the DB. Returns counters."""
    with findings_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    category = payload.get("category")
    if category not in VALID_CATEGORIES:
        raise ValueError(
            f"{findings_path.name}: bad category {category!r}; "
            f"must be one of {sorted(VALID_CATEGORIES)}"
        )

    findings = payload.get("findings", [])
    cur = conn.cursor()

    # Build a lookup of valid donor_ids so we can warn on stale ones
    valid_donor_ids = {
        row[0] for row in cur.execute("SELECT donor_id FROM donor_identities").fetchall()
    }

    cleared_aff, cleared_ev = clear_category(conn, category)

    n_aff = 0
    n_ev = 0
    n_skipped = 0
    n_sensitive = 0
    skipped_examples = []

    for f in findings:
        donor_id = f.get("donor_id")
        if not donor_id or donor_id not in valid_donor_ids:
            n_skipped += 1
            if len(skipped_examples) < 3:
                skipped_examples.append(donor_id)
            continue

        label = f.get("label") or category
        total = _coerce_amount(f.get("total_amount"))
        confidence = f.get("confidence") or "medium"
        first_seen = f.get("first_seen") or None
        last_seen = f.get("last_seen") or None
        notes = f.get("notes") or None
        sensitive = 1 if f.get("sensitive") else 0
        if sensitive:
            n_sensitive += 1

        cur.execute(
            """
            INSERT INTO donor_affiliations
                (donor_id, category, label, total_amount, confidence,
                 first_seen, last_seen, notes, sensitive)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(donor_id, category, label) DO UPDATE SET
                total_amount = excluded.total_amount,
                confidence   = excluded.confidence,
                first_seen   = excluded.first_seen,
                last_seen    = excluded.last_seen,
                notes        = excluded.notes,
                sensitive    = excluded.sensitive
            """,
            (donor_id, category, label, total, confidence,
             first_seen, last_seen, notes, sensitive),
        )
        affiliation_id = cur.execute(
            "SELECT affiliation_id FROM donor_affiliations "
            "WHERE donor_id=? AND category=? AND label=?",
            (donor_id, category, label),
        ).fetchone()[0]
        n_aff += 1

        for ev in (f.get("evidence") or []):
            cur.execute(
                """
                INSERT INTO donor_affiliation_evidence
                    (affiliation_id, source, source_url, evidence_text,
                     contribution_id, committee_id, committee_name,
                     amount, date, raw_data, rule)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    affiliation_id,
                    ev.get("source") or "unknown",
                    ev.get("source_url"),
                    ev.get("evidence_text"),
                    ev.get("contribution_id"),
                    ev.get("committee_id"),
                    ev.get("committee_name"),
                    _coerce_amount(ev.get("amount")),
                    ev.get("date"),
                    ev.get("raw_data"),
                    ev.get("rule"),
                ),
            )
            n_ev += 1

    conn.commit()
    return {
        "category":        category,
        "findings_in":     len(findings),
        "cleared_aff":     cleared_aff,
        "cleared_ev":      cleared_ev,
        "inserted_aff":    n_aff,
        "inserted_ev":     n_ev,
        "skipped_unknown": n_skipped,
        "skipped_examples": skipped_examples,
        "sensitive_flagged": n_sensitive,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Integrate affiliation findings JSON into the DB.")
    p.add_argument("paths", nargs="*", help="findings JSON files (or omit + use --all)")
    p.add_argument("--all", action="store_true",
                   help="Auto-discover ./findings_*.json files")
    args = p.parse_args()

    here = pathlib.Path(__file__).parent
    paths: list[pathlib.Path] = []
    if args.all:
        paths.extend(sorted(here.glob("findings_*.json")))
    for x in args.paths:
        paths.extend(pathlib.Path(p) for p in glob.glob(x) or [x])

    paths = [pathlib.Path(p) for p in paths]
    paths = [p for p in paths if p.exists()]
    if not paths:
        print("No findings files matched.", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA foreign_keys = ON")
    ensure_schema(conn)
    try:
        for path in paths:
            print(f"\n[integrate] {path.name}")
            try:
                result = integrate(conn, path)
            except Exception as e:
                print(f"  ERROR: {e}", file=sys.stderr)
                continue
            for k, v in result.items():
                print(f"  {k}: {v}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
