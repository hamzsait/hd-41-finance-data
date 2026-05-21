"""Extract the per-donor source list for a HD-41 candidate's web-research pass.

For each unique donor of the candidate (INDIVIDUAL via donor_id; ENTITY via
synthetic 'org:' + sha1(org_name)[:12]), emit:
  donor_id, kind, canonical_name (or org), employer, occupation,
  city, state, zip5, local_total ($ to this candidate).

Writes one JSON file per batch and one merged JSON of all donors. The batch
files are the input format for subagent prompts.

Usage:
    python scan_web_research.py --candidate haddad --batches 10
    python scan_web_research.py --candidate haddad --batches 10 --out-dir .
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import pathlib
import sqlite3
import sys

DB_PATH = pathlib.Path(r"C:\Users\Hamza Sait\Electoral\HD-41\hd41_finance.db")


def synth_org_id(org_name: str) -> str:
    h = hashlib.sha1(org_name.strip().lower().encode("utf-8")).hexdigest()[:12]
    return f"org:{h}"


def extract(conn: sqlite3.Connection, slug: str):
    cur = conn.cursor()

    # INDIVIDUAL donors — keyed on donor_id, joined to donor_identities for
    # canonical name. Pick the most-common employer/occupation/city/state/zip
    # from the contributions table since donor_identities only carries
    # canonical_employer and canonical_zip.
    indiv_rows = cur.execute(
        """
        SELECT c.donor_id,
               di.canonical_name AS canonical_name,
               COALESCE(MAX(c.contributor_employer),'')      AS employer,
               COALESCE(MAX(c.contributor_occupation),'')    AS occupation,
               COALESCE(MAX(c.contributor_street_city),'')   AS city,
               COALESCE(MAX(c.contributor_street_state),'')  AS state,
               COALESCE(MAX(SUBSTR(c.contributor_street_zip,1,5)),'') AS zip5,
               COUNT(*) AS gift_count,
               ROUND(SUM(c.contribution_amount), 2) AS local_total
        FROM contributions c
        LEFT JOIN donor_identities di ON di.donor_id = c.donor_id
        WHERE c.candidate_slug = ?
          AND COALESCE(c.info_only_flag,'N') <> 'Y'
          AND c.contributor_persent_type = 'INDIVIDUAL'
          AND c.donor_id IS NOT NULL
        GROUP BY c.donor_id
        ORDER BY local_total DESC
        """,
        (slug,),
    ).fetchall()

    entity_rows = cur.execute(
        """
        SELECT TRIM(contributor_name_org) AS org_name,
               COALESCE(MAX(contributor_street_city),'')   AS city,
               COALESCE(MAX(contributor_street_state),'')  AS state,
               COALESCE(MAX(SUBSTR(contributor_street_zip,1,5)),'') AS zip5,
               COUNT(*) AS gift_count,
               ROUND(SUM(contribution_amount), 2) AS local_total
        FROM contributions
        WHERE candidate_slug = ?
          AND COALESCE(info_only_flag,'N') <> 'Y'
          AND contributor_persent_type = 'ENTITY'
          AND TRIM(COALESCE(contributor_name_org,'')) <> ''
        GROUP BY LOWER(TRIM(contributor_name_org))
        ORDER BY local_total DESC
        """,
        (slug,),
    ).fetchall()

    donors = []
    for r in indiv_rows:
        donors.append({
            "donor_id":   r["donor_id"],
            "kind":       "individual",
            "name":       r["canonical_name"] or "",
            "employer":   r["employer"] or "",
            "occupation": r["occupation"] or "",
            "city":       r["city"] or "",
            "state":      r["state"] or "",
            "zip5":       r["zip5"] or "",
            "gift_count": r["gift_count"] or 0,
            "local_total": float(r["local_total"] or 0.0),
        })
    for r in entity_rows:
        org = r["org_name"]
        donors.append({
            "donor_id":   synth_org_id(org),
            "kind":       "entity",
            "name":       org,
            "employer":   "",
            "occupation": "",
            "city":       r["city"] or "",
            "state":      r["state"] or "",
            "zip5":       r["zip5"] or "",
            "gift_count": r["gift_count"] or 0,
            "local_total": float(r["local_total"] or 0.0),
        })
    return donors


def make_batches(donors, n_batches):
    n = len(donors)
    per = math.ceil(n / n_batches) if n_batches > 0 else n
    batches = []
    for i in range(n_batches):
        chunk = donors[i * per:(i + 1) * per]
        if not chunk:
            continue
        batches.append(chunk)
    return batches


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate", required=True, help="candidate_slug, e.g. haddad")
    ap.add_argument("--batches", type=int, default=10)
    ap.add_argument("--out-dir", default=".", help="dir for batch files")
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        donors = extract(conn, args.candidate)
    finally:
        conn.close()

    print(f"[scan] {args.candidate}: {len(donors)} unique donors "
          f"({sum(1 for d in donors if d['kind']=='individual')} individuals, "
          f"{sum(1 for d in donors if d['kind']=='entity')} entities)")

    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Single combined dump for reference / re-batching.
    full_path = out_dir / f"web_research_source_{args.candidate}.json"
    full_path.write_text(
        json.dumps({"candidate_slug": args.candidate, "donors": donors},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"[scan] wrote {full_path.name} ({full_path.stat().st_size:,} bytes)")

    batches = make_batches(donors, args.batches)
    for i, chunk in enumerate(batches, start=1):
        path = out_dir / f"web_research_batch_{args.candidate}_{i:02d}.json"
        path.write_text(
            json.dumps({
                "candidate_slug": args.candidate,
                "batch_id": i,
                "n_donors": len(chunk),
                "donors": chunk,
            }, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"[scan]   batch {i:02d}: {len(chunk)} donors -> {path.name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
