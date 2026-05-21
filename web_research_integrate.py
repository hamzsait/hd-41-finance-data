"""
web_research_integrate.py
Single-writer integrator for per-donor web-research findings.

Each subagent batch writes a `web_findings_<candidate>_batch_<NN>.json` file
with the schema below. This script reads those files and idempotently writes
their content into donor_web_research + donor_web_research_evidence. Re-running
on the same input set is safe — categories are not wiped wholesale (because
batches partition the donor set; wiping would erase other batches' work);
instead each (donor_id, category, label) tuple is upserted by UNIQUE constraint.

Findings JSON schema:
{
  "candidate_slug": "haddad",
  "batch_id": 1,
  "findings": [
    {
      "donor_id": "...",           # may be a synthetic "org:..." for ENTITY rows
      "donor_label": "Name (employer, city)",  # for human reference; not written
      "category": "medical"|"adl"|"aipac"|"dmfi"|"jstreet"|"real_estate"|"oil_gas"|"_searched_no_results",
      "label": "Physician — McAllen Heart Clinic",
      "total_amount": 1500.0,      # local HD-41 $ from the donor's contributions; null OK
      "confidence": "high"|"medium"|"low",
      "first_seen": null,
      "last_seen": null,
      "sensitive": false,
      "notes": "Free text — explain the tie",
      "evidence": [
        {
          "source": "WebSearch"|"WebFetch"|"linkedin"|"corp_bio"|"news"|...,
          "source_url": "https://...",     # REQUIRED — no URL = no row
          "evidence_text": "Description of what was found",
          "snippet": "quoted text from the URL",
          "search_query": "the query used",
          "retrieved_at": "2026-05-21",
          "rule": "name+employer match"
        }
      ]
    }
  ]
}

Behaviour:
- Donor IDs are NOT required to live in donor_identities (ENTITY rows have
  synthetic IDs). The integrator accepts any donor_id string.
- Evidence rows with a null/empty source_url are silently dropped (hard rule).
- Sentinel category `_searched_no_results` is written with no evidence rows.

Usage:
    python web_research_integrate.py web_findings_haddad_batch_*.json
    python web_research_integrate.py --all  # auto-discover ./web_findings_*.json
"""
from __future__ import annotations
import argparse
import glob
import json
import pathlib
import sqlite3
import sys

from web_research_schema import ensure_schema, VALID_CATEGORIES, DB_PATH


def _coerce_amount(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def integrate(conn: sqlite3.Connection, findings_path: pathlib.Path) -> dict:
    with findings_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    findings = payload.get("findings", [])
    cur = conn.cursor()

    n_res = 0
    n_ev = 0
    n_skipped_cat = 0
    n_skipped_no_url = 0
    n_sensitive = 0

    for f in findings:
        donor_id = f.get("donor_id")
        category = f.get("category")
        if not donor_id or category not in VALID_CATEGORIES:
            n_skipped_cat += 1
            continue

        label = f.get("label") or category
        total = _coerce_amount(f.get("total_amount"))
        confidence = f.get("confidence") or ("none" if category == "_searched_no_results" else "medium")
        first_seen = f.get("first_seen") or None
        last_seen = f.get("last_seen") or None
        notes = f.get("notes") or None
        sensitive = 1 if f.get("sensitive") else 0
        if sensitive:
            n_sensitive += 1

        cur.execute(
            """
            INSERT INTO donor_web_research
                (donor_id, category, label, total_amount, confidence,
                 sensitive, notes, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(donor_id, category, label) DO UPDATE SET
                total_amount = excluded.total_amount,
                confidence   = excluded.confidence,
                sensitive    = excluded.sensitive,
                notes        = excluded.notes,
                first_seen   = excluded.first_seen,
                last_seen    = excluded.last_seen
            """,
            (donor_id, category, label, total, confidence,
             sensitive, notes, first_seen, last_seen),
        )
        research_id = cur.execute(
            "SELECT research_id FROM donor_web_research "
            "WHERE donor_id=? AND category=? AND label=?",
            (donor_id, category, label),
        ).fetchone()[0]

        # Replace evidence for this research_id wholesale (idempotent re-run)
        cur.execute(
            "DELETE FROM donor_web_research_evidence WHERE research_id=?",
            (research_id,),
        )

        n_res += 1

        for ev in (f.get("evidence") or []):
            src_url = ev.get("source_url")
            if not src_url:
                n_skipped_no_url += 1
                continue
            cur.execute(
                """
                INSERT INTO donor_web_research_evidence
                    (research_id, source, source_url, evidence_text, snippet,
                     search_query, retrieved_at, rule)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    research_id,
                    ev.get("source") or "web",
                    src_url,
                    ev.get("evidence_text"),
                    ev.get("snippet"),
                    ev.get("search_query"),
                    ev.get("retrieved_at"),
                    ev.get("rule"),
                ),
            )
            n_ev += 1

    conn.commit()
    return {
        "file": findings_path.name,
        "findings_in":         len(findings),
        "inserted_research":   n_res,
        "inserted_evidence":   n_ev,
        "skipped_bad_cat":     n_skipped_cat,
        "skipped_no_url":      n_skipped_no_url,
        "sensitive_flagged":   n_sensitive,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Integrate web-research findings into the DB.")
    p.add_argument("paths", nargs="*", help="findings JSON files (or omit + use --all)")
    p.add_argument("--all", action="store_true",
                   help="Auto-discover ./web_findings_*.json files")
    args = p.parse_args()

    here = pathlib.Path(__file__).parent
    paths: list[pathlib.Path] = []
    if args.all:
        paths.extend(sorted(here.glob("web_findings_*.json")))
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
        totals = {"inserted_research": 0, "inserted_evidence": 0,
                  "skipped_bad_cat": 0, "skipped_no_url": 0, "sensitive_flagged": 0}
        for path in paths:
            print(f"\n[integrate] {path.name}")
            try:
                result = integrate(conn, path)
            except Exception as e:
                print(f"  ERROR: {e}", file=sys.stderr)
                continue
            for k, v in result.items():
                print(f"  {k}: {v}")
            for k in totals:
                totals[k] += result.get(k, 0)
        print(f"\n[total] {totals}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
