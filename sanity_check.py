"""HD-41 sanity check — pre-extractor experiment.

For each of the 4 runoff candidates, walk every TEC bulk file we care about
(cover.csv for reports, contribs_##.csv for Schedule A donations) and print:
  - reports filed (type, period, filed date, totals declared by the filer)
  - contribution row counts and dollars (raw, filtered, deduped)
  - any superseded / amended quirks
  - top donors

Output goes to console + sanity_check_output.txt.
"""
import csv
import hashlib
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(r"C:\Users\Hamza Sait\Electoral\HD-41")
TEC_DIR = ROOT / "tec_data"
ZIP_PATH = TEC_DIR / "TEC_CF_CSV.zip"

# csv module's default field-size limit chokes on TEC's wide cover-sheet rows.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

CANDIDATES = {
    "00090127": ("haddad",  "Victor 'Seby' Haddad", "D"),
    "00090159": ("salinas", "Julio Mauricio Salinas", "D"),
    "00089992": ("sanchez", "Sergio Sanchez", "R"),
    "00090204": ("groves",  "Gary Groves", "R"),
}
FILER_IDS = set(CANDIDATES.keys())

OUTPUT = ROOT / "sanity_check_output.txt"
output_lines: list[str] = []

def log(msg: str = "") -> None:
    print(msg, flush=True)
    output_lines.append(msg)


def content_hash(row: dict) -> str:
    """SA-style stable natural key. Collapses original-vs-amended duplicates
    and re-ingestion duplicates."""
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
# 1. Reports (cover.csv) — see what reports each filer has on file
# ---------------------------------------------------------------------------
log("=" * 80)
log("STEP 1: Reports filed per candidate (cover.csv)")
log("=" * 80)

reports_by_filer: dict[str, list[dict]] = defaultdict(list)

with open(TEC_DIR / "cover.csv", encoding="utf-8", errors="replace", newline="") as f:
    rdr = csv.DictReader(f)
    for row in rdr:
        if row.get("filerIdent") in FILER_IDS:
            reports_by_filer[row["filerIdent"]].append(row)

for fid, (slug, name, party) in CANDIDATES.items():
    rows = reports_by_filer.get(fid, [])
    log(f"\n--- {name} ({party}) [{fid}] — {len(rows)} cover-sheet rows ---")
    if not rows:
        log("  (no reports)")
        continue

    # Sort by filed date
    rows.sort(key=lambda r: (r.get("filedDt") or "", r.get("reportInfoIdent") or ""))
    log(f"  {'reportInfoIdent':<13} {'filedDt':<10} {'periodStart':<10} {'periodEnd':<10} "
        f"{'reportTypeCd':<12} {'electionDt':<10} {'totalContrib':>14} {'infoOnly':<8} formType")
    for r in rows:
        log(
            f"  {(r.get('reportInfoIdent') or ''):<13} "
            f"{(r.get('filedDt') or ''):<10} "
            f"{(r.get('periodStartDt') or ''):<10} "
            f"{(r.get('periodEndDt') or ''):<10} "
            f"{(r.get('reportTypeCd') or '')[:12]:<12} "
            f"{(r.get('electionDt') or ''):<10} "
            f"{(r.get('totalContribAmount') or '0'):>14} "
            f"{(r.get('infoOnlyFlag') or 'N'):<8} "
            f"{r.get('formTypeCd') or ''}"
        )


# ---------------------------------------------------------------------------
# 2. Find which contribs_##.csv shards have rows for our filers
#    (open the zip and stream-read each shard — don't extract all 99)
# ---------------------------------------------------------------------------
log("\n" + "=" * 80)
log("STEP 2: Locating contribution shards with our filer IDs")
log("=" * 80)

# Stats per (filer, shard)
contrib_stats: dict[str, dict] = {
    fid: {
        "rows_total":          0,
        "rows_active":         0,   # infoOnlyFlag != 'Y'
        "rows_superseded":     0,   # infoOnlyFlag == 'Y'
        "amount_total":      0.0,
        "amount_active":     0.0,
        "amount_superseded": 0.0,
        "shards_seen":     set(),
        "hashes":          set(),   # for dedup count
        "active_hashes":   set(),
        "by_report":  defaultdict(lambda: {"n": 0, "amt": 0.0}),
        "schedules":  defaultdict(int),
        "form_types": defaultdict(int),
        "rows":       [],           # full row capture for top-donor analysis
    }
    for fid in FILER_IDS
}

with zipfile.ZipFile(ZIP_PATH) as zf:
    members = sorted(m for m in zf.namelist() if m.startswith("contribs_") and m.endswith(".csv"))
    log(f"  Scanning {len(members)} contribution shards...")
    for i, member in enumerate(members, 1):
        with zf.open(member) as raw:
            text = (line.decode("utf-8", errors="replace") for line in raw)
            rdr = csv.DictReader(text)
            shard_hits = 0
            for row in rdr:
                fid = row.get("filerIdent")
                if fid not in FILER_IDS:
                    continue
                shard_hits += 1
                stats = contrib_stats[fid]
                stats["shards_seen"].add(member)
                try:
                    amt = float(row.get("contributionAmount") or 0)
                except ValueError:
                    amt = 0.0
                stats["rows_total"]   += 1
                stats["amount_total"] += amt
                stats["schedules"][row.get("schedFormTypeCd") or ""] += 1
                stats["form_types"][row.get("formTypeCd") or ""] += 1
                rep_id = row.get("reportInfoIdent") or ""
                stats["by_report"][rep_id]["n"]   += 1
                stats["by_report"][rep_id]["amt"] += amt
                h = content_hash(row)
                stats["hashes"].add(h)
                if (row.get("infoOnlyFlag") or "N") != "Y":
                    stats["rows_active"]   += 1
                    stats["amount_active"] += amt
                    stats["active_hashes"].add(h)
                else:
                    stats["rows_superseded"]   += 1
                    stats["amount_superseded"] += amt
                stats["rows"].append(row)
        if shard_hits:
            log(f"   {member:<20} hits={shard_hits}")
        if i % 10 == 0:
            log(f"   ... scanned {i}/{len(members)}")

log("  done.")


# ---------------------------------------------------------------------------
# 3. Per-candidate summary
# ---------------------------------------------------------------------------
log("\n" + "=" * 80)
log("STEP 3: Per-candidate contribution summary")
log("=" * 80)

for fid, (slug, name, party) in CANDIDATES.items():
    s = contrib_stats[fid]
    log(f"\n--- {name} ({party}) [{fid}] ---")
    log(f"  shards seen           : {sorted(s['shards_seen'])}")
    log(f"  rows total            : {s['rows_total']:,}")
    log(f"  rows active (not supersded): {s['rows_active']:,}  ${s['amount_active']:,.2f}")
    log(f"  rows superseded(infoOnly=Y): {s['rows_superseded']:,}  ${s['amount_superseded']:,.2f}")
    log(f"  distinct content hashes (all rows)   : {len(s['hashes']):,}")
    log(f"  distinct content hashes (active only): {len(s['active_hashes']):,}")
    log(f"  schedFormTypeCd breakdown : {dict(s['schedules'])}")
    log(f"  formTypeCd breakdown      : {dict(s['form_types'])}")
    log(f"  rows per report:")
    for rid in sorted(s["by_report"], key=lambda k: -s["by_report"][k]["amt"]):
        d = s["by_report"][rid]
        log(f"    report={rid:<11} n={d['n']:<4} amt=${d['amt']:>14,.2f}")

    # Top 10 donors among active rows
    by_donor: dict[tuple, dict] = defaultdict(lambda: {"n": 0, "amt": 0.0, "name": ""})
    for r in s["rows"]:
        if (r.get("infoOnlyFlag") or "N") == "Y":
            continue
        if (r.get("contributorPersentTypeCd") or "").upper() == "INDIVIDUAL":
            key  = (
                (r.get("contributorNameLast") or "").strip().lower(),
                (r.get("contributorNameFirst") or "").strip().lower(),
                (r.get("contributorStreetPostalCode") or "")[:5],
            )
            disp = f"{r.get('contributorNameLast')}, {r.get('contributorNameFirst')} ({r.get('contributorStreetCity')}, {r.get('contributorStreetStateCd')})"
        else:
            key  = (r.get("contributorNameOrganization") or "").strip().lower()
            disp = f"[ORG] {r.get('contributorNameOrganization')}"
        try:
            amt = float(r.get("contributionAmount") or 0)
        except ValueError:
            amt = 0.0
        by_donor[key]["n"]    += 1
        by_donor[key]["amt"]  += amt
        by_donor[key]["name"]  = disp
    top = sorted(by_donor.values(), key=lambda v: -v["amt"])[:10]
    log(f"  top donors (active only):")
    for t in top:
        log(f"    ${t['amt']:>10,.2f}  n={t['n']:<3}  {t['name']}")


# ---------------------------------------------------------------------------
# 4. Reconciliation: contrib-row sums vs cover-sheet declared totals
# ---------------------------------------------------------------------------
log("\n" + "=" * 80)
log("STEP 4: Reconciliation — contrib rows vs cover sheet totals")
log("=" * 80)
log("  cover.totalContribAmount = filer's declared TOTAL contribs for the period.")
log("  This INCLUDES unitemized small donations not in contribs_##.csv,")
log("  so cover_total >= contribs_sum is expected. Diff = unitemized.")

for fid, (slug, name, party) in CANDIDATES.items():
    log(f"\n--- {name} ({party}) [{fid}] ---")
    reports = reports_by_filer.get(fid, [])
    # Build active reports only (skip cover rows where infoOnlyFlag=Y)
    active_reports = [r for r in reports if (r.get("infoOnlyFlag") or "N") != "Y"]
    log(f"  cover rows: {len(reports)} total, {len(active_reports)} active")
    s = contrib_stats[fid]
    cover_active_total = 0.0
    for r in active_reports:
        try:
            cover_active_total += float(r.get("totalContribAmount") or 0)
        except ValueError:
            pass
        try:
            unitem = float(r.get("unitemizedContribAmount") or 0)
        except ValueError:
            unitem = 0.0
        log(
            f"    rep={(r.get('reportInfoIdent') or ''):<11} "
            f"period {r.get('periodStartDt') or ''}..{r.get('periodEndDt') or ''} "
            f"reportType={(r.get('reportTypeCd') or '-'):<8} "
            f"formType={(r.get('formTypeCd') or '-'):<8} "
            f"declared_total=${(float(r.get('totalContribAmount') or 0)):>12,.2f} "
            f"unitemized=${unitem:>10,.2f}"
        )
    log(f"  Sum cover declared (active reports) = ${cover_active_total:,.2f}")
    log(f"  Sum contribs_##.csv (active rows)    = ${s['amount_active']:,.2f}")
    log(f"  Sum contribs_##.csv (deduped active) = sum-by-hash below")
    # dedup the active rows by content_hash (keep one per hash)
    seen = set()
    deduped_amt = 0.0
    deduped_n = 0
    for r in s["rows"]:
        if (r.get("infoOnlyFlag") or "N") == "Y":
            continue
        h = content_hash(r)
        if h in seen:
            continue
        seen.add(h)
        deduped_n += 1
        try:
            deduped_amt += float(r.get("contributionAmount") or 0)
        except ValueError:
            pass
    log(f"     deduped: n={deduped_n:,}  ${deduped_amt:,.2f}")


OUTPUT.write_text("\n".join(output_lines), encoding="utf-8")
log(f"\n[saved → {OUTPUT}]")
