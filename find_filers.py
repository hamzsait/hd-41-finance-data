"""Search TEC filers.csv for HD-41 runoff candidates.

Goal: identify each candidate's TEC filerIdent for the HD-41 race.
Print only the plausible matches (TX-located, STATEREP/HD-41, or otherwise relevant).
"""
import csv
import sys
from pathlib import Path

ROOT = Path(r"C:\Users\Hamza Sait\Electoral\HD-41")
FILERS = ROOT / "tec_data" / "filers.csv"

TARGETS = [
    ("haddad",  "Victor 'Seby' Haddad (D)"),
    ("salinas", "Julio Salinas (D)"),
    ("sanchez", "Sergio Sanchez (R)"),
    ("groves",  "Gary Groves (R)"),
    ("holguin", "Eric Holguin (D)"),
    # Holguin variants — accented n in HTML, but TEC bulk strips accents in
    # most cases. Lowercased substring match on last name catches both.
]

# Heads-up: csv module default field-size limit can choke on TEC's 4000-byte cover-sheet rows.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

hits = {k: [] for k, _ in TARGETS}

with open(FILERS, encoding="utf-8", errors="replace", newline="") as f:
    rdr = csv.DictReader(f)
    for row in rdr:
        last  = (row.get("filerNameLast") or "").lower()
        first = (row.get("filerNameFirst") or "").lower()
        org   = (row.get("filerNameOrganization") or "").lower()
        full  = f"{last}|{first}|{org}"
        for key, _ in TARGETS:
            if key in full:
                hits[key].append({
                    "fid":          row.get("filerIdent"),
                    "type":         row.get("filerTypeCd"),
                    "name":         row.get("filerName"),
                    "last":         row.get("filerNameLast"),
                    "first":        row.get("filerNameFirst"),
                    "org":          row.get("filerNameOrganization"),
                    "seek_office":  row.get("ctaSeekOfficeCd"),
                    "seek_dist":    row.get("ctaSeekOfficeDistrict"),
                    "seek_descr":   row.get("ctaSeekOfficeDescr"),
                    "hold_office":  row.get("filerHoldOfficeCd"),
                    "hold_dist":    row.get("filerHoldOfficeDistrict"),
                    "hold_descr":   row.get("filerHoldOfficeDescr"),
                    "city":         row.get("filerStreetCity"),
                    "state":        row.get("filerStreetStateCd"),
                    "zip":          row.get("filerStreetPostalCode"),
                    "eff_start":    row.get("filerEffStartDt"),
                    "eff_stop":     row.get("filerEffStopDt"),
                    "status":       row.get("filerFilerpersStatusCd"),
                })

for key, label in TARGETS:
    rows = hits[key]
    print(f"\n=== {label}: {len(rows)} raw last-name/org matches ===")

    # Filter: STATEREP filer OR HD-41 filer OR (TX + RGV zip starts 785xx/786xx)
    rgv_zip_prefixes = ("785", "786", "787", "780", "788", "78")
    plausible = []
    for r in rows:
        is_staterep = r["seek_office"] == "STATEREP" or r["hold_office"] == "STATEREP"
        is_dist41   = r["seek_dist"] == "41" or r["hold_dist"] == "41"
        is_tx_rgv   = r["state"] == "TX" and (r["zip"] or "").startswith(("785", "786"))
        if is_staterep or is_dist41 or is_tx_rgv:
            plausible.append(r)

    if not plausible:
        # fallback: just show TX-state hits
        plausible = [r for r in rows if r["state"] == "TX"]

    for r in plausible[:30]:  # cap output
        print(
            f"  fid={r['fid']:<10} type={r['type'] or '':<6} "
            f"name={r['name'][:55] if r['name'] else '':<55} "
            f"seek={r['seek_office'] or '':<10}/{r['seek_dist'] or '':<3} "
            f"hold={r['hold_office'] or '':<10}/{r['hold_dist'] or '':<3} "
            f"city={r['city'] or '':<14} zip={r['zip'] or '':<5} "
            f"eff_start={r['eff_start'] or ''}"
        )
