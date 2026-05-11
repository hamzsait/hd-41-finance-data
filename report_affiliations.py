"""Cross-candidate affiliations report.

Shows:
  - per-category roll-up (donors, $, sensitive)
  - per-candidate breakdown of donors with affiliations
  - cross-candidate donors (anyone giving to >1 HD-41 candidate)
  - cross-party donors (anyone giving to both a D and an R)
"""
import sqlite3
from collections import defaultdict
from pathlib import Path

DB = str(Path(__file__).parent / "hd41_finance.db")
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=" * 70)
print("PER-CATEGORY ROLL-UP")
print("=" * 70)
for r in cur.execute(
    """
    SELECT a.category,
           COUNT(DISTINCT a.donor_id) AS donors,
           COUNT(*)                   AS affiliations,
           ROUND(SUM(COALESCE(a.total_amount,0)),2) AS total_amt,
           SUM(a.sensitive)           AS sensitive_count,
           SUM(CASE WHEN a.confidence='high'   THEN 1 ELSE 0 END) AS high,
           SUM(CASE WHEN a.confidence='medium' THEN 1 ELSE 0 END) AS med,
           SUM(CASE WHEN a.confidence='low'    THEN 1 ELSE 0 END) AS low
    FROM donor_affiliations a
    GROUP BY a.category
    ORDER BY total_amt DESC
    """
):
    print(f"  {r['category']:<18}  donors={r['donors']:>3}  aff={r['affiliations']:>3}  "
          f"${r['total_amt']:>12,.2f}  sens={r['sensitive_count']:<2}  "
          f"H/M/L={r['high']}/{r['med']}/{r['low']}")

print()
print("=" * 70)
print("PER-CANDIDATE: donors with each category affiliation + $ local")
print("=" * 70)
print(f"  {'candidate':<10} {'party':<3} {'category':<18} {'aff_donors':>10} {'local $ from those donors':>26}")
for r in cur.execute(
    """
    SELECT cand.candidate_slug, cand.full_name, cand.party, a.category,
           COUNT(DISTINCT a.donor_id) AS aff_donors,
           ROUND(SUM(c.contribution_amount),2) AS local_total
    FROM contributions c
    JOIN candidates cand   ON cand.candidate_slug = c.candidate_slug
    JOIN donor_affiliations a ON a.donor_id = c.donor_id
    WHERE COALESCE(c.info_only_flag,'N') <> 'Y'
    GROUP BY cand.candidate_slug, a.category
    ORDER BY cand.party DESC, cand.candidate_slug, local_total DESC
    """
):
    print(f"  {r['candidate_slug']:<10} {r['party']:<3} {r['category']:<18} "
          f"{r['aff_donors']:>10} ${r['local_total']:>23,.2f}")

print()
print("=" * 70)
print("CROSS-CANDIDATE DONORS (gave to >1 HD-41 candidate)")
print("=" * 70)
rows = cur.execute(
    """
    SELECT di.donor_id, di.canonical_name,
           GROUP_CONCAT(DISTINCT c.candidate_slug) AS recipients,
           COUNT(DISTINCT c.candidate_slug)        AS n_recipients,
           ROUND(SUM(c.contribution_amount),2)     AS total
    FROM contributions c
    JOIN donor_identities di ON di.donor_id = c.donor_id
    WHERE COALESCE(c.info_only_flag,'N') <> 'Y'
    GROUP BY di.donor_id
    HAVING n_recipients > 1
    ORDER BY n_recipients DESC, total DESC
    """
).fetchall()
if not rows:
    print("  (none — no donor gave to more than one HD-41 candidate)")
else:
    for r in rows:
        print(f"  {r['canonical_name']:<35} n={r['n_recipients']} "
              f"recipients=[{r['recipients']}] total=${r['total']:,.2f}")

print()
print("=" * 70)
print("CROSS-PARTY DONORS (gave to both a D and an R)")
print("=" * 70)
rows = cur.execute(
    """
    SELECT di.donor_id, di.canonical_name,
           GROUP_CONCAT(DISTINCT cand.party)         AS parties,
           GROUP_CONCAT(DISTINCT cand.candidate_slug) AS recipients,
           ROUND(SUM(c.contribution_amount),2)        AS total
    FROM contributions c
    JOIN donor_identities di ON di.donor_id = c.donor_id
    JOIN candidates cand     ON cand.candidate_slug = c.candidate_slug
    WHERE COALESCE(c.info_only_flag,'N') <> 'Y'
    GROUP BY di.donor_id
    HAVING parties LIKE '%D%' AND parties LIKE '%R%'
    ORDER BY total DESC
    """
).fetchall()
if not rows:
    print("  (none — no donor gave to both a D and an R)")
else:
    for r in rows:
        print(f"  {r['canonical_name']:<35} parties=[{r['parties']}] "
              f"recipients=[{r['recipients']}] total=${r['total']:,.2f}")

print()
print("=" * 70)
print("SENSITIVE FLAGS — flagged for human review")
print("=" * 70)
for r in cur.execute(
    """
    SELECT a.category, a.label, a.total_amount, a.confidence,
           di.canonical_name,
           (SELECT GROUP_CONCAT(DISTINCT candidate_slug)
              FROM contributions c
             WHERE c.donor_id=a.donor_id
               AND COALESCE(c.info_only_flag,'N')<>'Y') AS recipients
    FROM donor_affiliations a
    JOIN donor_identities di ON di.donor_id=a.donor_id
    WHERE a.sensitive = 1
    ORDER BY a.category, a.total_amount DESC NULLS LAST
    """
):
    print(f"  [{r['category']:<12}] {r['canonical_name']:<28} "
          f"label={r['label'][:35]:<35} ${(r['total_amount'] or 0):>10,.2f}  "
          f"conf={r['confidence']:<6}  -> {r['recipients']}")

conn.close()
