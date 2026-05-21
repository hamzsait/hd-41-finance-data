"""Compute outside-vs-inside money for Haddad vs Salinas.

Two parallel definitions:
  1. By state    — in_state = contributor_street_state == 'TX'
  2. By district — in_district = zip5 ∈ IN_DISTRICT_ZIPS, OR
                                  (zip missing AND city ∈ IN_DISTRICT_CITIES);
                                  out-of-state is automatically out-of-district.

Output: compare_inside_outside.json — one payload consumed by
compare_inside_outside.html on the static site.

Methodology notes (documented in the JSON + on the page):
  * Money = SUM(contribution_amount) on non-superseded rows
    (info_only_flag != 'Y'). Includes both monetary (Schedule A1) and
    in-kind (Schedule A2) contributions, since both represent value
    moving to the campaign.
  * State="TX" / district zips are case-insensitive; city matching is
    on lowercased, trimmed city strings (catches the "EDIONBURG" typo
    we observed in real data).
  * Out-of-district intentionally has TWO flavors: "out of state" AND
    "out of district but in state". Top-donor evidence tables surface
    both for each candidate.

HD-41 zip set — conservative inclusion of zips whose primary city sits
inside HD-41's footprint per the Texas Legislative Council Plan H2316
(2021 redistricting). Boundary zips that primarily belong to HD-36 / 37
/ 38 (Weslaco, Harlingen, Brownsville, South Padre, Rio Grande City)
are intentionally excluded.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB   = str(ROOT / "hd41_finance.db")
OUT  = ROOT / "compare_inside_outside.json"

# HD-41 zip5 set. Source: Texas Legislative Council Plan H2316 (2021
# redistricting), HD-41 footprint in Hidalgo County. Conservative — we
# exclude boundary zips whose primary jurisdiction is in HD-36/37/38.
IN_DISTRICT_ZIPS: set[str] = {
    "78501", "78502", "78503", "78504", "78505",  # McAllen
    "78572", "78573", "78574",                      # Mission / Palmhurst / Palmview
    "78577",                                         # Pharr
    "78539", "78540", "78541", "78542",            # Edinburg
    "78537",                                         # Donna
    "78557",                                         # Hidalgo
    "78576",                                         # Penitas
    "78589",                                         # San Juan
    "78516",                                         # Alamo
    "78560",                                         # La Joya
    "78595",                                         # Sullivan City
}

# City fallback (case-insensitive, lowercased, trimmed). Used only when
# zip is missing/null on a TX-state row. The "edionburg" entry catches a
# real typo we observed in the live data; harmless if no row matches.
IN_DISTRICT_CITIES: set[str] = {
    "mcallen", "mission", "pharr", "edinburg", "edionburg",
    "donna", "hidalgo", "penitas", "san juan", "alamo",
    "la joya", "sullivan city", "palmhurst", "palmview",
}

CANDIDATES = [
    ("haddad",  "Victor 'Seby' Haddad",   "D"),
    ("salinas", "Julio Mauricio Salinas", "D"),
]


def classify_row(row: dict) -> tuple[str, str]:
    """Return (state_bucket, district_bucket) for one contribution row.

    state_bucket    in {'in', 'out', 'unknown'}
    district_bucket in {'in', 'out_state', 'out_district_in_state', 'unknown'}

    Note: by_district returned to the JSON consumer collapses
    out_state + out_district_in_state into 'out' (with a sub-count of
    each on the side for the evidence section).
    """
    state = (row.get("contributor_street_state") or "").strip().upper()
    zip5  = (row.get("contributor_street_zip") or "")[:5].strip()
    city  = (row.get("contributor_street_city") or "").strip().lower()

    # state bucket
    if not state:
        state_b = "unknown"
    elif state == "TX":
        state_b = "in"
    else:
        state_b = "out"

    # district bucket — only TX rows are eligible for "in-district"
    if state_b == "unknown":
        dist_b = "unknown"
    elif state_b == "out":
        dist_b = "out_state"
    else:  # in TX
        if zip5 in IN_DISTRICT_ZIPS:
            dist_b = "in"
        elif (not zip5) and (city in IN_DISTRICT_CITIES):
            dist_b = "in"
        else:
            dist_b = "out_district_in_state"

    return state_b, dist_b


def _display_donor(row: dict) -> str:
    if (row.get("contributor_persent_type") or "").upper() == "INDIVIDUAL":
        last  = (row.get("contributor_name_last")  or "").strip()
        first = (row.get("contributor_name_first") or "").strip()
        return f"{last}, {first}".strip(", ").strip() or "(unnamed individual)"
    return f"[ORG] {row.get('contributor_name_org') or '(unnamed entity)'}"


def _pct(part: float, total: float) -> float:
    return round((part / total) * 100.0, 2) if total > 0 else 0.0


def aggregate_for_candidate(conn: sqlite3.Connection, slug: str) -> dict:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT contribution_info_id, contribution_amount, contribution_dt,
               contributor_persent_type, contributor_name_org,
               contributor_name_last, contributor_name_first,
               contributor_street_city, contributor_street_state,
               contributor_street_zip
        FROM contributions
        WHERE candidate_slug=? AND COALESCE(info_only_flag,'N')<>'Y'
        """,
        (slug,),
    )
    rows = [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]

    total_amt = 0.0
    by_state    = {"in": 0.0, "out": 0.0, "unknown": 0.0}
    by_state_n  = {"in": 0,   "out": 0,   "unknown": 0}
    by_district = {"in": 0.0, "out_state": 0.0, "out_district_in_state": 0.0, "unknown": 0.0}
    by_district_n = {"in": 0, "out_state": 0, "out_district_in_state": 0, "unknown": 0}

    out_of_state_rows: list[tuple[float, dict]] = []
    out_of_district_in_state_rows: list[tuple[float, dict]] = []

    for r in rows:
        amt = float(r.get("contribution_amount") or 0)
        total_amt += amt
        sb, db = classify_row(r)
        by_state[sb]    += amt
        by_state_n[sb]  += 1
        by_district[db]   += amt
        by_district_n[db] += 1
        if sb == "out":
            out_of_state_rows.append((amt, r))
        if db == "out_district_in_state":
            out_of_district_in_state_rows.append((amt, r))

    out_of_state_rows.sort(key=lambda x: -x[0])
    out_of_district_in_state_rows.sort(key=lambda x: -x[0])

    def top10(pairs):
        return [
            {
                "name":   _display_donor(r),
                "city":   r.get("contributor_street_city") or "",
                "state":  r.get("contributor_street_state") or "",
                "zip":    (r.get("contributor_street_zip") or "")[:5],
                "amount": round(amt, 2),
                "date":   r.get("contribution_dt") or "",
            }
            for amt, r in pairs[:10]
        ]

    # Collapse district buckets for the JSON consumer's "out" view, but
    # also surface the breakdown so the page can label them differently.
    dist_in      = by_district["in"]
    dist_out_st  = by_district["out_state"]
    dist_out_in  = by_district["out_district_in_state"]
    dist_unknown = by_district["unknown"]
    dist_out_total = dist_out_st + dist_out_in

    return {
        "total":     round(total_amt, 2),
        "row_count": len(rows),
        "by_state": {
            "in":      {"amount": round(by_state["in"],    2), "count": by_state_n["in"],
                        "pct":    _pct(by_state["in"],    total_amt)},
            "out":     {"amount": round(by_state["out"],   2), "count": by_state_n["out"],
                        "pct":    _pct(by_state["out"],   total_amt)},
            "unknown": {"amount": round(by_state["unknown"],2),"count": by_state_n["unknown"],
                        "pct":    _pct(by_state["unknown"],total_amt)},
        },
        "by_district": {
            "in":      {"amount": round(dist_in,      2), "count": by_district_n["in"],
                        "pct":    _pct(dist_in,      total_amt)},
            "out":     {"amount": round(dist_out_total, 2),
                        "count":  by_district_n["out_state"] + by_district_n["out_district_in_state"],
                        "pct":    _pct(dist_out_total, total_amt),
                        # finer split:
                        "out_of_state":           {"amount": round(dist_out_st, 2),
                                                    "count":  by_district_n["out_state"]},
                        "out_of_district_in_state":{"amount": round(dist_out_in, 2),
                                                     "count":  by_district_n["out_district_in_state"]}},
            "unknown": {"amount": round(dist_unknown, 2), "count": by_district_n["unknown"],
                        "pct":    _pct(dist_unknown, total_amt)},
        },
        "top_out_of_state":              top10(out_of_state_rows),
        "top_out_of_district_in_state":  top10(out_of_district_in_state_rows),
    }


def main() -> int:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        payload = {
            "meta": {
                "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "methodology": {
                    "in_state":   "contributor_street_state == 'TX' (case-insensitive)",
                    "in_district": (
                        "TX donor AND (zip5 in HD-41 zip set OR "
                        "(zip empty AND normalized city in HD-41 city set)). "
                        "Out-of-state is automatically out-of-district."
                    ),
                    "money": (
                        "SUM(contribution_amount) over non-superseded rows "
                        "(info_only_flag != 'Y'). Both monetary (Schedule A1) "
                        "and in-kind (A2) included."
                    ),
                    "unknown_state": (
                        "contributor_street_state is null/empty — counted "
                        "separately so it doesn't bias the in/out split."
                    ),
                    "boundary_zips_excluded": (
                        "78596 Weslaco (HD-36), 78550 Harlingen (HD-37), "
                        "78520/78521/78526 Brownsville (HD-37/38), 78597 South Padre "
                        "(HD-38), 78582 Rio Grande City (HD-31)."
                    ),
                    "in_district_zips":   sorted(IN_DISTRICT_ZIPS),
                    "in_district_cities": sorted(IN_DISTRICT_CITIES),
                },
            },
            "candidates": [
                {
                    "slug": slug, "name": name, "party": party,
                    **aggregate_for_candidate(conn, slug),
                }
                for slug, name, party in CANDIDATES
            ],
        }
    finally:
        conn.close()

    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {OUT.name}  ({OUT.stat().st_size:,} bytes)")
    # Headline summary so the operator can sanity-check the numbers.
    print()
    print("=== headline percentages ===")
    for c in payload["candidates"]:
        print(f"  {c['slug']:<8}  total=${c['total']:>10,.0f}")
        bs, bd = c["by_state"], c["by_district"]
        print(f"    by_state    : in TX  ${bs['in']['amount']:>9,.0f} ({bs['in']['pct']:5.1f}%) | "
              f"out  ${bs['out']['amount']:>9,.0f} ({bs['out']['pct']:5.1f}%) | "
              f"unknown ${bs['unknown']['amount']:>7,.0f} ({bs['unknown']['pct']:.1f}%)")
        print(f"    by_district : in 41  ${bd['in']['amount']:>9,.0f} ({bd['in']['pct']:5.1f}%) | "
              f"out  ${bd['out']['amount']:>9,.0f} ({bd['out']['pct']:5.1f}%) | "
              f"unknown ${bd['unknown']['amount']:>7,.0f} ({bd['unknown']['pct']:.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
