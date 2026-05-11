"""HD-41 — generate per-candidate {slug}_data.json + {slug}_all_donations.json.

Adapted from san-antonio-finance-data/generate_profile_data.py:
  - reads HD-41's `contributions` / `candidates` / `donor_identities` tables
  - by_year → by_month (cycle is ~8 months; monthly resolution is more useful)
  - meta.status (runoff_d / runoff_r / primary_eliminated) for the index page
  - meta.party + meta.filer_ident
  - partisan_lean.weighted_lean_signed in [-1, +1] using the user's formula:
        (sum_D - sum_R) / (sum_D + sum_R)
    plus partisan_lean.weighted_lean (legacy [0, 1] form for the SA template)
  - Affiliations summary keeps only the FEC partisan section; other category
    bins (AIPAC/ADL/oil/RE/MIC) are emitted as empty arrays so the SA template
    null-checks don't error.

Usage:
    python generate_profile_data.py --slug haddad
    python generate_profile_data.py --all                 # all 5 candidates
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "hd41_finance.db"

# Static info about each candidate (sourced from CLAUDE.md / project memory).
# `office` is shown in the hero badge; `status` is the index-page chip.
CANDIDATE_META: dict[str, dict] = {
    "haddad":  {"party": "D", "status": "runoff_d",
                "subtitle": "Runoff (D) · McAllen City Commissioner · banker"},
    "salinas": {"party": "D", "status": "runoff_d",
                "subtitle": "Runoff (D) · Texas legislative staffer"},
    "sanchez": {"party": "R", "status": "runoff_r",
                "subtitle": "Runoff (R) · Former Hidalgo Co. felony prosecutor"},
    "groves":  {"party": "R", "status": "runoff_r",
                "subtitle": "Runoff (R) · Hidalgo County GOP precinct chair"},
    "holguin": {"party": "D", "status": "primary_eliminated",
                "subtitle": "Eliminated in primary · UnidosUS Texas policy director"},
}

OFFICE_LABEL = "Texas House District 41 · 2026 Race"


def yyyymm_from_dt(dt: str | None) -> str:
    """20251005 -> '2025-10'.  empty/None -> ''."""
    if not dt or len(dt) < 6:
        return ""
    return f"{dt[:4]}-{dt[4:6]}"


def parse_date_iso(dt: str | None) -> str:
    """20251005 -> '2025-10-05' for display."""
    if not dt or len(dt) != 8:
        return dt or ""
    return f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}"


def build_one(conn: sqlite3.Connection, slug: str, output_dir: Path) -> dict:
    cur = conn.cursor()
    cand = cur.execute(
        "SELECT candidate_slug, full_name, party, filer_ident, filing_start_date "
        "FROM candidates WHERE candidate_slug=?",
        (slug,),
    ).fetchone()
    if not cand:
        raise SystemExit(f"no candidates row for slug={slug!r}")

    candidate_name = cand["full_name"]
    party = cand["party"]
    static = CANDIDATE_META.get(slug, {"status": "unknown", "subtitle": ""})

    # ── Pull all active rows for this candidate (any contributor type) ─────
    rows = cur.execute(
        """
        SELECT contribution_info_id, contribution_dt, contribution_amount,
               contributor_persent_type, contributor_name_org,
               contributor_name_last, contributor_name_first,
               contributor_street_city, contributor_street_state,
               contributor_street_zip, contributor_employer,
               contributor_occupation, donor_id, sched_form_type_cd
        FROM contributions
        WHERE candidate_slug = ?
          AND COALESCE(info_only_flag,'N') <> 'Y'
        """,
        (slug,),
    ).fetchall()

    if not rows:
        print(f"[!] no rows for {slug!r}", file=sys.stderr)
        return {}

    print(f"[generate] {candidate_name} ({party}): {len(rows)} active rows")

    # ── Hero ──────────────────────────────────────────────────────────────
    total_raised = sum(float(r["contribution_amount"] or 0) for r in rows)
    # Unique donors — count distinct donor_ids for INDIVIDUALs, plus distinct
    # contributor_name_org values for ENTITY rows (entities don't get clustered).
    indiv_ids = {r["donor_id"] for r in rows
                 if r["contributor_persent_type"] == "INDIVIDUAL" and r["donor_id"]}
    entity_orgs = {(r["contributor_name_org"] or "").strip().lower() for r in rows
                   if r["contributor_persent_type"] == "ENTITY"
                   and (r["contributor_name_org"] or "").strip()}
    unique_donors = len(indiv_ids) + len(entity_orgs)

    hero = {
        "total_raised":        int(round(total_raised)),
        "unique_donors":       unique_donors,
        "total_contributions": len(rows),
        "employer_affiliated_pct": 0.0,
        "top_industry":        "Unknown",
    }

    # ── By month ──────────────────────────────────────────────────────────
    month_buckets: dict[str, list[float]] = {}
    for r in rows:
        m = yyyymm_from_dt(r["contribution_dt"])
        if not m:
            continue
        month_buckets.setdefault(m, []).append(float(r["contribution_amount"] or 0))
    by_month = [
        {"month": m, "count": len(vs), "total": int(round(sum(vs)))}
        for m, vs in sorted(month_buckets.items())
    ]
    # Also a `by_year` shim for the SA template (it iterates by_year).
    year_buckets: dict[str, list[float]] = {}
    for m, vs in month_buckets.items():
        year_buckets.setdefault(m[:4], []).extend(vs)
    by_year = [
        {"year": y, "count": len(vs), "total": int(round(sum(vs)))}
        for y, vs in sorted(year_buckets.items())
    ]

    # ── Top donors (per identity, INDIVIDUAL + ENTITY) ────────────────────
    # First the individuals via donor_identities (they have FEC fields)
    indiv_rows = cur.execute(
        """
        SELECT di.donor_id, di.canonical_name, di.canonical_zip,
               di.canonical_employer,
               di.fec_partisan_lean, di.fec_total_dem, di.fec_total_rep,
               di.fec_total_other, di.fec_total_donations, di.fec_matched,
               COUNT(c.contribution_info_id) AS gift_count,
               SUM(c.contribution_amount) AS local_total
        FROM donor_identities di
        JOIN contributions c ON c.donor_id = di.donor_id
        WHERE c.candidate_slug = ?
          AND COALESCE(c.info_only_flag,'N') <> 'Y'
          AND c.contributor_persent_type = 'INDIVIDUAL'
        GROUP BY di.donor_id
        ORDER BY local_total DESC
        """,
        (slug,),
    ).fetchall()

    # Entities don't go through identity resolution; aggregate them by org name.
    entity_rows = cur.execute(
        """
        SELECT contributor_name_org AS name,
               contributor_street_city AS city,
               contributor_street_state AS state,
               COUNT(*) AS gift_count,
               SUM(contribution_amount) AS local_total
        FROM contributions
        WHERE candidate_slug = ?
          AND COALESCE(info_only_flag,'N') <> 'Y'
          AND contributor_persent_type = 'ENTITY'
        GROUP BY LOWER(TRIM(contributor_name_org))
        ORDER BY local_total DESC
        """,
        (slug,),
    ).fetchall()

    top_donors_combined = []
    for d in indiv_rows:
        top_donors_combined.append({
            "name":     d["canonical_name"] or "",
            "employer": (d["canonical_employer"] or "").title() if d["canonical_employer"] else "",
            "industry": "Unknown",
            "tags":     "",
            "total":    int(round(d["local_total"] or 0)),
            "count":    d["gift_count"] or 0,
        })
    for e in entity_rows:
        top_donors_combined.append({
            "name":     f"[ORG] {e['name']}",
            "employer": "",
            "industry": "Unknown",
            "tags":     "",
            "total":    int(round(e["local_total"] or 0)),
            "count":    e["gift_count"] or 0,
        })
    top_donors_combined.sort(key=lambda d: -d["total"])
    top_donors = top_donors_combined[:10]

    # ── Partisan lean (FEC-only) ──────────────────────────────────────────
    matched = [d for d in indiv_rows
               if (d["fec_total_dem"] or 0) + (d["fec_total_rep"] or 0) > 0]
    partisan_lean = None
    if matched:
        buckets = [
            {"label": "Strong D", "min": 0.9,   "max": 1.01,  "donors": 0, "total": 0},
            {"label": "Lean D",   "min": 0.6,   "max": 0.9,   "donors": 0, "total": 0},
            {"label": "Mixed",    "min": 0.4,   "max": 0.6,   "donors": 0, "total": 0},
            {"label": "Lean R",   "min": 0.1,   "max": 0.4,   "donors": 0, "total": 0},
            {"label": "Strong R", "min": -0.01, "max": 0.1,   "donors": 0, "total": 0},
        ]
        donors_list = []
        sum_D = sum_R = 0.0
        weighted_lean_sum_local = 0.0
        weighted_amt_local = 0.0
        dem_donors = rep_donors = mixed_donors = 0

        for d in matched:
            dem = float(d["fec_total_dem"]   or 0)
            rep = float(d["fec_total_rep"]   or 0)
            other = float(d["fec_total_other"] or 0)
            local = float(d["local_total"]    or 0)

            if dem + rep <= 0:
                continue
            lean = dem / (dem + rep)

            for b in buckets:
                if b["min"] <= lean < b["max"]:
                    b["donors"] += 1
                    b["total"] += round(local, 2)
                    break

            if lean >= 0.6:
                dem_donors += 1
            elif lean <= 0.4:
                rep_donors += 1
            else:
                mixed_donors += 1

            sum_D += dem
            sum_R += rep
            if local > 0:
                weighted_lean_sum_local += lean * local
                weighted_amt_local       += local

            donors_list.append({
                "id":       d["donor_id"],
                "name":     d["canonical_name"],
                "lean":     round(lean, 3),
                "dem":      round(dem, 0),
                "rep":      round(rep, 0),
                "other":    round(other, 0),
                "fec_n":    d["fec_total_donations"] or 0,
                "tec_n":    0,
                "fec_dem":  round(dem, 0),
                "fec_rep":  round(rep, 0),
                "tec_dem":  0,
                "tec_rep":  0,
                "local":    round(local, 0),
            })

        donors_list.sort(key=lambda x: -(x["dem"] + x["rep"]))

        # Two flavors of weighted lean:
        # * weighted_lean         — [0, 1], dem-share, weighted by LOCAL gift size.
        #                           This is what the SA template/donut expects.
        # * weighted_lean_signed  — [-1, +1], (D-R)/(D+R) over each candidate's
        #                           matched donors' FEC totals. The user's
        #                           explicit ask for the headline number.
        weighted_lean        = round(weighted_lean_sum_local / weighted_amt_local, 3) if weighted_amt_local > 0 else None
        weighted_lean_signed = round((sum_D - sum_R) / (sum_D + sum_R), 3) if (sum_D + sum_R) > 0 else None

        partisan_lean = {
            "matched_donors":       len(donors_list),
            "total_donors":         unique_donors,
            "dem_donors":           dem_donors,
            "rep_donors":           rep_donors,
            "mixed_donors":         mixed_donors,
            "fec_only":             len(donors_list),
            "tec_only":             0,
            "both_sources":         0,
            "weighted_lean":        weighted_lean,           # [0,1] for SA template
            "weighted_lean_signed": weighted_lean_signed,    # [-1,+1] for the headline
            "fec_total_dem_sum":    round(sum_D, 2),
            "fec_total_rep_sum":    round(sum_R, 2),
            "buckets":              buckets,
            "donors":               donors_list,
            "donor_committees":     {},
        }
        print(f"[generate]   matched={len(donors_list)}  D={dem_donors}  R={rep_donors}  "
              f"M={mixed_donors}  weighted_signed={weighted_lean_signed}")

    # ── Cycles (single cycle for HD-41) ───────────────────────────────────
    cycles = [{
        "label":        "HD-41 cycle",
        "election_year": 2026,
        "year_range":   (
            f"{by_month[0]['month'][:4]}–present" if by_month else "?"
        ),
        "hero":            hero,
        "interest_groups": [],
        "notable_firms":   [],
        "top_donors":      top_donors,
    }]

    # ── Affiliations: only `fec_partisan` populated ───────────────────────
    affiliations_summary = {"categories": []}
    if partisan_lean:
        affiliations_summary["categories"].append({
            "category":         "fec_partisan",
            "category_label":   "Federal partisan giving (FEC)",
            "donor_count":      partisan_lean["matched_donors"],
            "total_amount":     partisan_lean["fec_total_dem_sum"] + partisan_lean["fec_total_rep_sum"],
            "confidence_breakdown": {"high": partisan_lean["matched_donors"], "medium": 0, "low": 0},
            "sensitive_count":  0,
            "top_donors":       [
                {
                    "donor_id":     d["id"],
                    "name":         d["name"],
                    "label":        "D" if d["lean"] >= 0.6 else ("R" if d["lean"] <= 0.4 else "Mixed"),
                    "total_amount": d["dem"] + d["rep"],
                    "confidence":   "high",
                }
                for d in partisan_lean["donors"][:10]
            ],
        })

    # ── All donations table ───────────────────────────────────────────────
    all_donations: list[list] = []
    for r in rows:
        if r["contributor_persent_type"] == "INDIVIDUAL":
            display = f"{r['contributor_name_last']}, {r['contributor_name_first']}".strip(", ").strip()
        else:
            display = f"[ORG] {r['contributor_name_org']}"
        city_state_zip = ", ".join(filter(None, [
            (r["contributor_street_city"] or "").strip(),
            (r["contributor_street_state"] or "").strip()
            + (f" {r['contributor_street_zip']}" if r["contributor_street_zip"] else ""),
        ]))
        all_donations.append([
            display,
            parse_date_iso(r["contribution_dt"]),
            round(float(r["contribution_amount"] or 0), 2),
            (r["contributor_employer"] or "").title(),
            "Unknown",
            city_state_zip,
        ])
    # Newest first
    all_donations.sort(key=lambda x: x[1], reverse=True)

    # ── Payload ───────────────────────────────────────────────────────────
    meta = {
        "candidate_name":  candidate_name,
        "candidate_slug":  slug,
        "party":           party,
        "status":          static["status"],
        "office":          OFFICE_LABEL,
        "subtitle":        static["subtitle"],
        "filer_ident":     cand["filer_ident"],
        "generated_at":    datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    payload = {
        "meta":                  meta,
        "hero":                  hero,
        "by_year":               by_year,
        "by_month":              by_month,
        "interest_groups":       [],
        "notable_firms":         [],
        "top_donors":            top_donors,
        "cycles":                cycles,
        "partisan_lean":         partisan_lean,
        "ip_spectrum":           None,
        "civic_affiliations":    None,
        "affiliations_summary":  affiliations_summary,
        "donor_affiliations":    {},
    }

    data_path = output_dir / f"{slug}_data.json"
    don_path  = output_dir / f"{slug}_all_donations.json"
    data_path.write_text(json.dumps(payload, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    don_path.write_text(json.dumps(all_donations, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    print(f"[generate]   wrote {data_path.name} ({data_path.stat().st_size:,} bytes)  "
          f"{don_path.name} ({don_path.stat().st_size:,} bytes, {len(all_donations):,} rows)")
    return payload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db",          default=str(DEFAULT_DB))
    ap.add_argument("--slug",        help="single candidate; omit to use --all")
    ap.add_argument("--all",         action="store_true", help="generate for every candidate")
    ap.add_argument("--output-dir",  default=str(ROOT))
    args = ap.parse_args()

    if not args.slug and not args.all:
        ap.error("either --slug or --all required")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    out_dir = Path(args.output_dir)

    if args.all:
        slugs = [r[0] for r in conn.execute("SELECT candidate_slug FROM candidates ORDER BY candidate_slug")]
    else:
        slugs = [args.slug]

    for slug in slugs:
        try:
            build_one(conn, slug, out_dir)
        except Exception as e:
            print(f"[!] {slug}: {e}", file=sys.stderr)

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
