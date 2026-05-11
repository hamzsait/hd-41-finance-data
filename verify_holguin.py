import sqlite3
conn = sqlite3.connect(r"C:\Users\Hamza Sait\Electoral\HD-41\hd41_finance.db")
cur = conn.cursor()

print("=== candidates table ===")
for r in cur.execute(
    "SELECT candidate_slug, full_name, party, filer_ident, filing_start_date, notes "
    "FROM candidates ORDER BY candidate_slug"
):
    print(f"  {r[0]:<8}  {r[2]}  {r[1]:<25}  filer={r[3]}  first_filed={r[4] or '-'}  notes={r[5] or '-'}")

print()
print("=== contributions per candidate (active rows only) ===")
for r in cur.execute(
    """
    SELECT candidate_slug, COUNT(*) AS n, ROUND(SUM(contribution_amount),2) AS total
    FROM contributions
    WHERE COALESCE(info_only_flag,'N') <> 'Y'
    GROUP BY candidate_slug
    ORDER BY total DESC
    """
):
    print(f"  {r[0]:<8}  n={r[1]:<4}  total=${r[2]:>14,.2f}")

print()
print("=== reports per candidate (HD-41 cycle only — district filter applied) ===")
for r in cur.execute(
    """
    SELECT candidate_slug,
           COUNT(*) AS reports,
           SUM(CASE WHEN COALESCE(info_only_flag,'N')<>'Y' THEN 1 ELSE 0 END) AS active,
           ROUND(SUM(CASE WHEN COALESCE(info_only_flag,'N')<>'Y' THEN total_contrib_amount ELSE 0 END),2) AS cover_active
    FROM reports
    GROUP BY candidate_slug
    ORDER BY cover_active DESC
    """
):
    print(f"  {r[0]:<8}  total_reports={r[1]}  active={r[2]}  cover_declared=${r[3]:>14,.2f}")

print()
print("=== Holguin reports in detail (proves district filter worked) ===")
for r in cur.execute(
    """
    SELECT report_info_ident, form_type_cd, period_start_dt, period_end_dt,
           filed_dt, election_dt, info_only_flag, total_contrib_amount
    FROM reports
    WHERE candidate_slug='holguin'
    ORDER BY filed_dt
    """
):
    print(f"  rep={r[0]}  form={r[1]}  period={r[2]}..{r[3]}  filed={r[4]}  "
          f"election={r[5]}  info_only={r[6]}  total=${r[7]:,.2f}")
