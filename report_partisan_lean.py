"""HD-41 — per-candidate weighted partisan lean.

Formula (per the user's spec):
    weighted_lean = (sum_D - sum_R) / (sum_D + sum_R)

where sum_D / sum_R are the FEC dem/rep totals across each candidate's matched
individual donors. Range [-1, +1]. Positive = Dem-leaning donors. Negative =
Rep-leaning donors.

Also reports:
  * total / matched / partisan-classified donor counts per candidate
  * D / R / Mixed donor counts (per-donor lean threshold 0.6 / 0.4)
  * legacy [0,1] dem-share weighted by local gift size (for the donut chart)
  * candidates with no matches at all (FEC ran but no donors had history)

Usage:
    python report_partisan_lean.py
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = str(Path(__file__).resolve().parent / "hd41_finance.db")


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    print("=== FEC enrichment overall ===")
    row = conn.execute(
        """
        SELECT
            COUNT(*)                                                         AS total_identities,
            SUM(CASE WHEN fec_matched=1               THEN 1 ELSE 0 END)     AS processed,
            SUM(CASE WHEN fec_total_donations > 0     THEN 1 ELSE 0 END)     AS has_history,
            SUM(CASE WHEN fec_partisan_lean IS NOT NULL THEN 1 ELSE 0 END)   AS classified,
            SUM(CASE WHEN fec_partisan_lean >= 0.6     THEN 1 ELSE 0 END)    AS dem_leaning,
            SUM(CASE WHEN fec_partisan_lean <= 0.4     THEN 1 ELSE 0 END)    AS rep_leaning,
            SUM(CASE WHEN fec_partisan_lean BETWEEN 0.4 AND 0.6 AND fec_partisan_lean IS NOT NULL THEN 1 ELSE 0 END) AS mixed
        FROM donor_identities
        """
    ).fetchone()
    print(f"  identities total            : {row['total_identities']}")
    print(f"  processed (fec_matched=1)   : {row['processed']}")
    print(f"  has FEC history             : {row['has_history']}")
    print(f"  partisan-classified         : {row['classified']}")
    print(f"    dem-leaning (>= 0.6)      : {row['dem_leaning']}")
    print(f"    rep-leaning (<= 0.4)      : {row['rep_leaning']}")
    print(f"    mixed (0.4 < lean < 0.6)  : {row['mixed']}")

    print()
    print("=== per-candidate partisan lean (active individual rows only) ===")
    rows = conn.execute(
        """
        WITH candidate_donors AS (
            SELECT c.candidate_slug, di.donor_id,
                   di.fec_matched,
                   di.fec_partisan_lean,
                   di.fec_total_dem,
                   di.fec_total_rep,
                   di.fec_total_other,
                   SUM(c.contribution_amount) AS local_total
            FROM contributions c
            JOIN donor_identities di ON di.donor_id = c.donor_id
            WHERE COALESCE(c.info_only_flag,'N') <> 'Y'
              AND c.contributor_persent_type = 'INDIVIDUAL'
            GROUP BY c.candidate_slug, di.donor_id
        )
        SELECT cand.candidate_slug,
               cand.full_name,
               cand.party,
               COUNT(DISTINCT cd.donor_id) AS donors_total,
               SUM(CASE WHEN cd.fec_matched=1                                         THEN 1 ELSE 0 END) AS donors_processed,
               SUM(CASE WHEN cd.fec_partisan_lean IS NOT NULL                         THEN 1 ELSE 0 END) AS donors_classified,
               SUM(CASE WHEN cd.fec_partisan_lean >= 0.6                              THEN 1 ELSE 0 END) AS donors_d,
               SUM(CASE WHEN cd.fec_partisan_lean <= 0.4                              THEN 1 ELSE 0 END) AS donors_r,
               SUM(CASE WHEN cd.fec_partisan_lean > 0.4 AND cd.fec_partisan_lean < 0.6 THEN 1 ELSE 0 END) AS donors_m,
               COALESCE(SUM(CASE WHEN cd.fec_partisan_lean IS NOT NULL THEN cd.fec_total_dem   ELSE 0 END),0) AS sum_d,
               COALESCE(SUM(CASE WHEN cd.fec_partisan_lean IS NOT NULL THEN cd.fec_total_rep   ELSE 0 END),0) AS sum_r,
               COALESCE(SUM(CASE WHEN cd.fec_partisan_lean IS NOT NULL THEN cd.fec_total_other ELSE 0 END),0) AS sum_o
        FROM candidates cand
        LEFT JOIN candidate_donors cd ON cd.candidate_slug = cand.candidate_slug
        GROUP BY cand.candidate_slug
        ORDER BY cand.party DESC, cand.candidate_slug
        """
    ).fetchall()

    print(f"  {'cand':<8}  {'pty':<3}  {'tot':>4}  {'proc':>4}  {'cls':>4}  "
          f"{'D':>3}  {'R':>3}  {'M':>3}  "
          f"{'sum_D':>10}  {'sum_R':>10}  {'sum_O':>10}  {'lean(-1..+1)':>14}")
    for r in rows:
        sum_d = float(r["sum_d"] or 0)
        sum_r = float(r["sum_r"] or 0)
        sum_o = float(r["sum_o"] or 0)
        lean_signed = (sum_d - sum_r) / (sum_d + sum_r) if (sum_d + sum_r) > 0 else None
        lean_str = f"{lean_signed:+.3f}" if lean_signed is not None else "—"
        print(
            f"  {r['candidate_slug']:<8}  {r['party']:<3}  "
            f"{r['donors_total']:>4}  {r['donors_processed']:>4}  {r['donors_classified']:>4}  "
            f"{r['donors_d']:>3}  {r['donors_r']:>3}  {r['donors_m']:>3}  "
            f"${sum_d:>9,.0f}  ${sum_r:>9,.0f}  ${sum_o:>9,.0f}  "
            f"{lean_str:>14}"
        )

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
