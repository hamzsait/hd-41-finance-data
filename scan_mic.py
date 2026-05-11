"""Scan HD-41 donors for Military Industrial Complex (MIC) / defense industry affiliations.

Three rules:
  1. fec_committee_name_match (high)   - donor gave to a defense-contractor PAC
  2. employer_match           (high)   - donor's employer matches a defense contractor
  3. occupation_match         (medium) - donor's occupation matches a defense role

Writes findings_mic.json. Read-only against hd41_finance.db.
"""

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

DB_PATH = r"C:\Users\Hamza Sait\Electoral\HD-41\hd41_finance.db"
OUT_PATH = r"C:\Users\Hamza Sait\Electoral\HD-41\findings_mic.json"

# ---- Rule 1: FEC committee-name patterns -----------------------------------
# (regex, canonical label). Order matters: first match wins.
COMMITTEE_NAME_PATTERNS = [
    (re.compile(r"Lockheed\s*Martin", re.I), "Lockheed Martin"),
    (re.compile(r"\bLockheed\b", re.I), "Lockheed Martin"),
    (re.compile(r"\bRaytheon\b", re.I), "Raytheon"),
    (re.compile(r"\bRTX\b", re.I), "RTX (Raytheon)"),
    (re.compile(r"\bBoeing\b", re.I), "Boeing"),
    (re.compile(r"General\s*Dynamics", re.I), "General Dynamics"),
    (re.compile(r"\bGD\b\s*PAC", re.I), "General Dynamics"),
    (re.compile(r"Northrop\s*Grumman", re.I), "Northrop Grumman"),
    (re.compile(r"\bNorthrop\b", re.I), "Northrop Grumman"),
    (re.compile(r"\bNGC\b", re.I), "Northrop Grumman"),
    (re.compile(r"\bL3Harris\b", re.I), "L3Harris"),
    (re.compile(r"\bL[\.\-]?3[\.\-]?Harris\b", re.I), "L3Harris"),
    (re.compile(r"\bBAE Systems\b", re.I), "BAE Systems"),
    (re.compile(r"Huntington\s*Ingalls", re.I), "Huntington Ingalls"),
    (re.compile(r"Booz\s*Allen(\s*Hamilton)?", re.I), "Booz Allen Hamilton"),
    (re.compile(r"\bHoneywell\b", re.I), "Honeywell"),
    (re.compile(r"\bPalantir\b", re.I), "Palantir"),
    (re.compile(r"\bAnduril\b", re.I), "Anduril"),
    (re.compile(r"\bSAIC\b", re.I), "SAIC"),
    (re.compile(r"Science Applications International", re.I), "SAIC"),
    (re.compile(r"\bLeidos\b", re.I), "Leidos"),
    (re.compile(r"\bCACI\b", re.I), "CACI"),
    (re.compile(r"\bManTech\b", re.I), "ManTech"),
    (re.compile(r"Aerojet\s*Rocketdyne", re.I), "Aerojet Rocketdyne"),
    (re.compile(r"\bKBR\b", re.I), "KBR"),
    (re.compile(r"\bTextron\b", re.I), "Textron"),
    (re.compile(r"Defense Industrial Initiatives", re.I), "Defense Industrial Initiatives"),
    (re.compile(r"Aerospace Industries Association", re.I), "Aerospace Industries Association"),
]

# SQL LIKE patterns to pre-filter fec_committee_cache (broader; regex re-checks).
COMMITTEE_LIKE_TERMS = [
    "lockheed", "raytheon", "rtx", "boeing", "general dynamics", "gd pac",
    "northrop", "grumman", "ngc", "l3harris", "l-3 harris", "l.3.harris",
    "bae systems", "huntington ingalls", "booz allen", "honeywell",
    "palantir", "anduril", "saic", "science applications",
    "leidos", "caci", "mantech", "aerojet", "kbr", "textron",
    "defense industrial initiatives", "aerospace industries association",
]

# ---- Rule 2: employer substring matches -------------------------------------
# (substring, canonical label)
EMPLOYER_KEYWORDS = [
    ("lockheed", "Lockheed Martin"),
    ("raytheon", "Raytheon"),
    ("boeing", "Boeing"),
    ("general dynamics", "General Dynamics"),
    ("northrop", "Northrop Grumman"),
    ("grumman", "Northrop Grumman"),
    ("l3harris", "L3Harris"),
    ("bae systems", "BAE Systems"),
    ("huntington ingalls", "Huntington Ingalls"),
    ("booz allen", "Booz Allen Hamilton"),
    ("honeywell", "Honeywell"),
    ("palantir", "Palantir"),
    ("anduril", "Anduril"),
    ("saic", "SAIC"),
    ("leidos", "Leidos"),
    ("caci", "CACI"),
    ("mantech", "ManTech"),
    ("aerojet rocketdyne", "Aerojet Rocketdyne"),
    ("textron", "Textron"),
    ("kbr", "KBR"),
    ("sandia national", "Sandia National Laboratories"),
    ("los alamos national", "Los Alamos National Laboratory"),
    ("lawrence livermore", "Lawrence Livermore National Laboratory"),
    ("applied signal", "Applied Signal"),
    ("rolls-royce defense", "Rolls-Royce Defense"),
    ("rolls royce defense", "Rolls-Royce Defense"),
    ("draper laboratory", "Draper Laboratory"),
    ("mitre corp", "MITRE Corporation"),
    ("scientific research", "Scientific Research (defense)"),
    ("defense contractor", "Defense Contractor (generic)"),
]

# ---- Rule 3: occupation substring matches (medium) --------------------------
OCCUPATION_KEYWORDS = [
    ("defense contractor", "Defense Contractor (occupation)"),
    ("aerospace engineer", "Aerospace Engineer"),
    ("weapons systems", "Weapons Systems"),
    ("military contractor", "Military Contractor"),
    ("intelligence analyst", "Intelligence Analyst"),
]


def classify_committee_name(cname: str):
    if not cname:
        return None
    for pat, label in COMMITTEE_NAME_PATTERNS:
        if pat.search(cname):
            return label
    return None


def find_employer_match(text: str):
    if not text:
        return None, None
    t = text.lower()
    for kw, label in EMPLOYER_KEYWORDS:
        if kw in t:
            return label, kw
    return None, None


def find_occupation_match(text: str):
    if not text:
        return None, None
    t = text.lower()
    for kw, label in OCCUPATION_KEYWORDS:
        if kw in t:
            return label, kw
    return None, None


def main():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # (donor_id, label) -> {evidence: [...], confidence: 'high'|'medium'}
    grouped = defaultdict(lambda: {"evidence": [], "confidence_rank": 0})

    # Confidence ranks: high=2, medium=1. Take max across evidence.
    HIGH = 2
    MED = 1

    # ---------------------------------------------------------------- Rule 1
    # Find qualifying committees in fec_committee_cache.
    like_clauses = " OR ".join(["LOWER(committee_name) LIKE ?"] * len(COMMITTEE_LIKE_TERMS))
    like_params = [f"%{t}%" for t in COMMITTEE_LIKE_TERMS]
    cache_rows = cur.execute(
        f"SELECT committee_id, committee_name FROM fec_committee_cache WHERE {like_clauses}",
        like_params,
    ).fetchall()

    qualifying = {}  # committee_id -> (label, committee_name)
    for r in cache_rows:
        cname = r["committee_name"] or ""
        label = classify_committee_name(cname)
        if label:
            qualifying[r["committee_id"]] = (label, cname)

    print(f"Qualifying MIC-related committees in cache: {len(qualifying)}")
    for cid, (label, cname) in list(qualifying.items())[:20]:
        print(f"  {cid} -> {label}  ({cname!r})")
    if len(qualifying) > 20:
        print(f"  ...+{len(qualifying)-20} more")

    if qualifying:
        placeholders = ",".join(["?"] * len(qualifying))
        rows = cur.execute(
            f"""
            SELECT id, donor_id, committee_id, contribution_amount, contribution_date,
                   fec_sub_id
            FROM fec_contributions_raw
            WHERE committee_id IN ({placeholders})
              AND donor_id IS NOT NULL
            """,
            list(qualifying.keys()),
        ).fetchall()
        print(f"FEC contribution rows to MIC committees: {len(rows)}")
        for r in rows:
            cid = r["committee_id"]
            label, cname = qualifying[cid]
            amount = float(r["contribution_amount"] or 0.0)
            date = r["contribution_date"] or ""
            ev = {
                "source": "FEC schedule_a",
                "source_url": f"https://www.fec.gov/data/receipts/?committee_id={cid}",
                "evidence_text": (
                    f"Contributed ${amount:,.2f} to {cname or label} on {date} "
                    f"(FEC sub_id {r['fec_sub_id']})"
                ),
                "contribution_id": f"fec_contributions_raw.id={r['id']}",
                "committee_id": cid,
                "committee_name": cname,
                "amount": amount,
                "date": date,
                "rule": "fec_committee_name_match",
            }
            key = (r["donor_id"], label)
            grouped[key]["evidence"].append(ev)
            grouped[key]["confidence_rank"] = max(grouped[key]["confidence_rank"], HIGH)

    # ---------------------------------------------------------------- Rule 2 (employer)
    # Scan contributions (TEC).
    emp_like = " OR ".join(["LOWER(contributor_employer) LIKE ?"] * len(EMPLOYER_KEYWORDS))
    emp_params = [f"%{kw}%" for kw, _ in EMPLOYER_KEYWORDS]
    tec_rows = cur.execute(
        f"""
        SELECT contribution_info_id, donor_id, candidate_slug, contribution_amount,
               contribution_dt, contributor_employer, contributor_occupation,
               contributor_name_first, contributor_name_last, contributor_name_org
        FROM contributions
        WHERE (info_only_flag IS NULL OR info_only_flag != 'Y')
          AND donor_id IS NOT NULL
          AND contributor_employer IS NOT NULL
          AND ({emp_like})
        """,
        emp_params,
    ).fetchall()
    print(f"TEC contribution rows w/ employer keyword: {len(tec_rows)}")
    for r in tec_rows:
        emp = r["contributor_employer"] or ""
        label, kw = find_employer_match(emp)
        if not label:
            continue
        amount = float(r["contribution_amount"] or 0.0)
        date = r["contribution_dt"] or ""
        name = (
            r["contributor_name_org"]
            or " ".join(x for x in [r["contributor_name_first"], r["contributor_name_last"]] if x)
            or "(unknown)"
        )
        ev = {
            "source": "employer_match",
            "source_url": None,
            "evidence_text": (
                f"{name} listed employer '{emp}' on TEC contribution of ${amount:,.2f} "
                f"to {r['candidate_slug']} on {date}"
            ),
            "contribution_id": f"contributions.contribution_info_id={r['contribution_info_id']}",
            "committee_id": None,
            "committee_name": None,
            "amount": amount,
            "date": date,
            "rule": f"employer_match: {kw}",
        }
        key = (r["donor_id"], label)
        grouped[key]["evidence"].append(ev)
        grouped[key]["confidence_rank"] = max(grouped[key]["confidence_rank"], HIGH)

    # Scan fec_contributions_raw employer field.
    emp_like_fec = " OR ".join(["LOWER(fec_employer) LIKE ?"] * len(EMPLOYER_KEYWORDS))
    fec_rows = cur.execute(
        f"""
        SELECT id, donor_id, committee_id, contribution_amount, contribution_date,
               fec_employer, fec_occupation, fec_contributor_name, fec_sub_id
        FROM fec_contributions_raw
        WHERE donor_id IS NOT NULL
          AND fec_employer IS NOT NULL
          AND ({emp_like_fec})
        """,
        emp_params,
    ).fetchall()
    print(f"FEC contribution rows w/ employer keyword: {len(fec_rows)}")
    for r in fec_rows:
        emp = r["fec_employer"] or ""
        label, kw = find_employer_match(emp)
        if not label:
            continue
        amount = float(r["contribution_amount"] or 0.0)
        date = r["contribution_date"] or ""
        ev = {
            "source": "employer_match",
            "source_url": (
                f"https://www.fec.gov/data/receipts/?committee_id={r['committee_id']}"
                if r["committee_id"] else None
            ),
            "evidence_text": (
                f"{r['fec_contributor_name'] or '(unknown)'} listed employer '{emp}' on FEC "
                f"contribution of ${amount:,.2f} on {date} (sub_id {r['fec_sub_id']})"
            ),
            "contribution_id": f"fec_contributions_raw.id={r['id']}",
            "committee_id": r["committee_id"],
            "committee_name": None,
            "amount": amount,
            "date": date,
            "rule": f"employer_match: {kw}",
        }
        key = (r["donor_id"], label)
        grouped[key]["evidence"].append(ev)
        grouped[key]["confidence_rank"] = max(grouped[key]["confidence_rank"], HIGH)

    # ---------------------------------------------------------------- Rule 3 (occupation)
    occ_like = " OR ".join(["LOWER(contributor_occupation) LIKE ?"] * len(OCCUPATION_KEYWORDS))
    occ_params = [f"%{kw}%" for kw, _ in OCCUPATION_KEYWORDS]
    tec_occ = cur.execute(
        f"""
        SELECT contribution_info_id, donor_id, candidate_slug, contribution_amount,
               contribution_dt, contributor_employer, contributor_occupation,
               contributor_name_first, contributor_name_last, contributor_name_org
        FROM contributions
        WHERE (info_only_flag IS NULL OR info_only_flag != 'Y')
          AND donor_id IS NOT NULL
          AND contributor_occupation IS NOT NULL
          AND ({occ_like})
        """,
        occ_params,
    ).fetchall()
    print(f"TEC contribution rows w/ occupation keyword: {len(tec_occ)}")
    for r in tec_occ:
        occ = r["contributor_occupation"] or ""
        label, kw = find_occupation_match(occ)
        if not label:
            continue
        amount = float(r["contribution_amount"] or 0.0)
        date = r["contribution_dt"] or ""
        name = (
            r["contributor_name_org"]
            or " ".join(x for x in [r["contributor_name_first"], r["contributor_name_last"]] if x)
            or "(unknown)"
        )
        ev = {
            "source": "occupation_match",
            "source_url": None,
            "evidence_text": (
                f"{name} listed occupation '{occ}' on TEC contribution of ${amount:,.2f} "
                f"to {r['candidate_slug']} on {date}"
            ),
            "contribution_id": f"contributions.contribution_info_id={r['contribution_info_id']}",
            "committee_id": None,
            "committee_name": None,
            "amount": amount,
            "date": date,
            "rule": f"occupation_match: {kw}",
        }
        key = (r["donor_id"], label)
        grouped[key]["evidence"].append(ev)
        # medium unless something else already promoted it
        if grouped[key]["confidence_rank"] < MED:
            grouped[key]["confidence_rank"] = MED

    occ_like_fec = " OR ".join(["LOWER(fec_occupation) LIKE ?"] * len(OCCUPATION_KEYWORDS))
    fec_occ = cur.execute(
        f"""
        SELECT id, donor_id, committee_id, contribution_amount, contribution_date,
               fec_employer, fec_occupation, fec_contributor_name, fec_sub_id
        FROM fec_contributions_raw
        WHERE donor_id IS NOT NULL
          AND fec_occupation IS NOT NULL
          AND ({occ_like_fec})
        """,
        occ_params,
    ).fetchall()
    print(f"FEC contribution rows w/ occupation keyword: {len(fec_occ)}")
    for r in fec_occ:
        occ = r["fec_occupation"] or ""
        label, kw = find_occupation_match(occ)
        if not label:
            continue
        amount = float(r["contribution_amount"] or 0.0)
        date = r["contribution_date"] or ""
        ev = {
            "source": "occupation_match",
            "source_url": (
                f"https://www.fec.gov/data/receipts/?committee_id={r['committee_id']}"
                if r["committee_id"] else None
            ),
            "evidence_text": (
                f"{r['fec_contributor_name'] or '(unknown)'} listed occupation '{occ}' on FEC "
                f"contribution of ${amount:,.2f} on {date} (sub_id {r['fec_sub_id']})"
            ),
            "contribution_id": f"fec_contributions_raw.id={r['id']}",
            "committee_id": r["committee_id"],
            "committee_name": None,
            "amount": amount,
            "date": date,
            "rule": f"occupation_match: {kw}",
        }
        key = (r["donor_id"], label)
        grouped[key]["evidence"].append(ev)
        if grouped[key]["confidence_rank"] < MED:
            grouped[key]["confidence_rank"] = MED

    # ------------------------------------------------------------ Build findings
    findings = []
    for (donor_id, label), bucket in grouped.items():
        evs = bucket["evidence"]
        confidence = "high" if bucket["confidence_rank"] >= HIGH else "medium"

        total_amount = round(sum(e["amount"] for e in evs), 2)
        dates = [e["date"] for e in evs if e["date"]]
        first_seen = min(dates) if dates else None
        last_seen = max(dates) if dates else None

        ident = cur.execute(
            "SELECT canonical_name, total_donated FROM donor_identities WHERE donor_id = ?",
            (donor_id,),
        ).fetchone()
        canonical_name = ident["canonical_name"] if ident else "(unknown)"
        hd41_total = float(ident["total_donated"]) if ident and ident["total_donated"] is not None else 0.0

        # HD-41 candidate backing
        cand_rows = cur.execute(
            """
            SELECT candidate_slug, SUM(contribution_amount) AS total
            FROM contributions
            WHERE donor_id = ?
              AND (info_only_flag IS NULL OR info_only_flag != 'Y')
            GROUP BY candidate_slug
            ORDER BY total DESC
            """,
            (donor_id,),
        ).fetchall()
        cand_parts = [
            f"{(cr['candidate_slug'] or '').title()} (${cr['total']:,.2f})"
            for cr in cand_rows if cr["total"]
        ]
        cand_str = ", ".join(cand_parts) if cand_parts else "(no HD-41 contributions found)"

        # Dem-candidate flag (haddad, salinas, holguin are Dems per CLAUDE.md)
        DEM_SLUGS = {"haddad", "salinas", "holguin"}
        backs_dem = any((cr["candidate_slug"] or "").lower() in DEM_SLUGS for cr in cand_rows)

        # Sensitive: > $2K MIC total OR backs a Dem candidate with MIC ties
        sensitive = (total_amount > 2000.0) or backs_dem

        n = len(evs)
        notes = (
            f"{n} MIC evidence row{'s' if n != 1 else ''} ({label}); "
            f"donor {canonical_name}; HD-41 backing: {cand_str}"
        )
        if backs_dem and confidence == "high":
            notes += " [REVIEW: Dem candidate + MIC ties]"

        findings.append({
            "donor_id": donor_id,
            "label": label,
            "total_amount": total_amount,
            "confidence": confidence,
            "first_seen": first_seen,
            "last_seen": last_seen,
            "notes": notes,
            "sensitive": sensitive,
            "evidence": evs,
        })

    findings.sort(key=lambda f: (f["label"], -f["total_amount"], f["donor_id"]))

    payload = {
        "category": "mic",
        "rules": [
            {
                "name": "fec_committee_name_match",
                "description": "Donor gave to an FEC committee whose name matches a defense contractor (high confidence)",
            },
            {
                "name": "employer_match",
                "description": "Donor's reported employer (TEC or FEC) contains a defense contractor name (high confidence)",
            },
            {
                "name": "occupation_match",
                "description": "Donor's reported occupation matches a defense industry role (medium confidence)",
            },
        ],
        "findings": findings,
    }

    Path(OUT_PATH).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # ------------------------------------------------------------ Summary
    by_rule = defaultdict(int)
    for f in findings:
        for e in f["evidence"]:
            # Bucket employer_match/occupation_match by prefix (drop ': keyword')
            r = e["rule"].split(":", 1)[0]
            by_rule[r] += 1

    cand_dist = defaultdict(int)
    for f in findings:
        # Backing candidate inferred from notes is fine, but better: re-query
        pass

    # Candidate distribution by donors (re-query)
    cand_donors = defaultdict(set)
    for f in findings:
        rows = cur.execute(
            """SELECT DISTINCT candidate_slug FROM contributions
               WHERE donor_id = ? AND (info_only_flag IS NULL OR info_only_flag != 'Y')""",
            (f["donor_id"],),
        ).fetchall()
        for rr in rows:
            cand_donors[(rr["candidate_slug"] or "").lower()].add(f["donor_id"])

    print("---- SUMMARY ----")
    print(f"Findings (donor,label rows): {len(findings)}")
    print(f"Unique donors implicated:    {len({f['donor_id'] for f in findings})}")
    print(f"Evidence rows:               {sum(len(f['evidence']) for f in findings)}")
    print(f"Total $ implicated:          ${sum(f['total_amount'] for f in findings):,.2f}")
    print(f"Sensitive flagged:           {sum(1 for f in findings if f['sensitive'])}")
    print(f"High-confidence findings:    {sum(1 for f in findings if f['confidence']=='high')}")
    print(f"Medium-confidence findings:  {sum(1 for f in findings if f['confidence']=='medium')}")
    print("Evidence by rule:")
    for rule, n in sorted(by_rule.items()):
        print(f"  {rule}: {n}")
    print("HD-41 candidate distribution (donors w/ MIC ties also gave to):")
    for cand, donors in sorted(cand_donors.items()):
        print(f"  {cand or '(none)'}: {len(donors)} donor(s)")
    print(f"Wrote: {OUT_PATH}")


if __name__ == "__main__":
    main()
