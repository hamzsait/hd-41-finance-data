"""Scan HD-41 donors for real-estate-industry affiliations.

Three rules:
  1. fec_committee_name_match  - donor gave to a FEC committee whose name matches
     real-estate-industry patterns (NAR, MORPAC, TREPAC, NMHC, NAHB, etc.)
  2. employer_match            - contributor_employer / fec_employer contains a
     real-estate keyword (high or medium confidence depending on specificity)
  3. occupation_match          - contributor_occupation / fec_occupation contains
     a real-estate keyword

Writes findings_real_estate.json. Read-only against hd41_finance.db.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

DB_PATH = r"C:\Users\Hamza Sait\Electoral\HD-41\hd41_finance.db"
OUT_PATH = r"C:\Users\Hamza Sait\Electoral\HD-41\findings_real_estate.json"

# ---------------------------------------------------------------------------
# Rule 1: FEC committee-name patterns
# ---------------------------------------------------------------------------
# (regex, canonical_label)
COMMITTEE_PATTERNS = [
    (re.compile(r"national association of realtors", re.I), "National Association of Realtors PAC"),
    (re.compile(r"\bREALTORS?\s+PAC\b", re.I), "REALTORS PAC"),
    (re.compile(r"texas association of realtors", re.I), "Texas Association of Realtors PAC (TREPAC/FedPAC)"),
    (re.compile(r"texas\s+realtors", re.I), "Texas REALTORS"),
    (re.compile(r"\bTREPAC\b", re.I), "TREPAC (Texas REALTORS PAC)"),
    (re.compile(r"\bREALPAC\b", re.I), "REALPAC"),
    (re.compile(r"mortgage bankers", re.I), "Mortgage Bankers Association PAC"),
    (re.compile(r"\bMORPAC\b", re.I), "MORPAC"),
    (re.compile(r"multifamily housing", re.I), "Multifamily Housing PAC"),
    (re.compile(r"\bNMHC\b", re.I), "NMHC PAC"),
    (re.compile(r"national multi[\s-]?housing council", re.I), "National Multi Housing Council"),
    (re.compile(r"\bBUILDPAC\b", re.I), "BUILDPAC"),
    (re.compile(r"national association of home ?builders", re.I), "National Association of Home Builders PAC"),
    (re.compile(r"\bNAHB\b", re.I), "NAHB"),
    (re.compile(r"\bGHBA\b", re.I), "Greater Houston Builders Association PAC"),
    (re.compile(r"\bHOME PAC\b", re.I), "HOME PAC (Greater Houston Builders)"),
    (re.compile(r"texas apartment association", re.I), "Texas Apartment Association PAC"),
    (re.compile(r"commercial real estate finance council", re.I), "Commercial Real Estate Finance Council PAC"),
    (re.compile(r"\bCREFC\b", re.I), "CREFC PAC"),
    (re.compile(r"real estate roundtable", re.I), "Real Estate Roundtable"),
]

# Generic catch-all: real-estate-industry word + PAC/Action Fund
GENERIC_REPAC_RE = re.compile(
    r"(real ?estate|realtors?|realty|apartment|home ?builders?).{0,40}(pac|action fund)",
    re.I,
)


def classify_committee(committee_name: str | None):
    """Return canonical label if committee name matches a real-estate pattern, else None."""
    if not committee_name:
        return None
    for pat, label in COMMITTEE_PATTERNS:
        if pat.search(committee_name):
            return label
    if GENERIC_REPAC_RE.search(committee_name):
        # Use the actual committee name as label so we don't lose specificity
        return committee_name.strip()
    return None


# ---------------------------------------------------------------------------
# Rule 2: employer keyword matching
# ---------------------------------------------------------------------------
# High-confidence employer substrings (unambiguous real-estate companies/terms)
HIGH_EMPLOYER_KEYWORDS = [
    "realtor",
    "realty",
    "real estate",
    "properties",
    "property management",
    "brokerage",
    "apartment",
    "multifamily",
    "land development",
    "homebuilder",
    "home builder",
    "title company",
    "title insurance",
    "mortgage",
    "appraisal",
    "subdivision",
    "lennar",
    "kb home",
    "d.r. horton",
    "dr horton",
    "pulte",
    "toll brothers",
    "weston",
    "mcanally",
]

# Specific-company "homes" tokens (avoid matching "Homes Depot" or "homes for sale" generically)
# We require " homes" as a word ending or " homes," etc.
HOMES_RE = re.compile(r"\b[\w&.'\- ]+ homes\b", re.I)

# Medium-confidence employer keywords - require a second signal to use
MEDIUM_EMPLOYER_KEYWORDS = [
    "development",
    "construction",
    "builder",
    "investments",
    "holdings",
]


def employer_match(value: str | None):
    """Return list of (keyword, confidence) hits for an employer string."""
    if not value:
        return []
    v = value.lower()
    hits = []
    for kw in HIGH_EMPLOYER_KEYWORDS:
        if kw in v:
            hits.append((kw, "high"))
    if HOMES_RE.search(value):
        # avoid double-counting if another high keyword already covers it
        if not any(kw in v for kw in ["homebuilder", "home builder"]):
            hits.append(("homes (company)", "high"))
    # medium: only when there's no high hit AND we see 2+ medium tokens
    if not hits:
        med = [kw for kw in MEDIUM_EMPLOYER_KEYWORDS if kw in v]
        if len(med) >= 2:
            hits.append((" + ".join(med), "medium"))
    return hits


# ---------------------------------------------------------------------------
# Rule 3: occupation keyword matching
# ---------------------------------------------------------------------------
OCCUPATION_KEYWORDS_HIGH = [
    "realtor",
    "real estate broker",
    "real estate agent",
    "real estate developer",
    "property manager",
    "property management",
    "mortgage broker",
    "appraiser",
    "title agent",
    "homebuilder",
    "home builder",
    "real estate",  # bare 'real estate' as occupation is strong
]

# Medium-confidence (alone too generic)
OCCUPATION_KEYWORDS_MEDIUM = [
    "developer",
    "general contractor",
    "broker",
]


def occupation_match(value: str | None):
    """Return list of (keyword, confidence) hits for an occupation string."""
    if not value:
        return []
    v = value.lower()
    hits = []
    for kw in OCCUPATION_KEYWORDS_HIGH:
        if kw in v:
            hits.append((kw, "high"))
    if not hits:
        for kw in OCCUPATION_KEYWORDS_MEDIUM:
            if kw in v:
                # 'broker' alone is meaningless w/o real-estate context; STOCKBROKER -> skip
                if kw == "broker" and "stockbroker" in v:
                    continue
                if kw == "developer" and any(
                    bad in v for bad in ["software developer", "web developer", "app developer"]
                ):
                    continue
                hits.append((kw, "medium"))
    return hits


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # donor_id -> label -> {evidence:[], total_amount, dates, sources}
    findings_acc: dict[tuple[str, str], dict] = {}

    def add(donor_id, label, confidence, evidence, amount, date, source_tag):
        key = (donor_id, label)
        if key not in findings_acc:
            findings_acc[key] = {
                "donor_id": donor_id,
                "label": label,
                "confidence": confidence,
                "evidence": [],
                "amount": 0.0,
                "dates": [],
                "sources": set(),
            }
        slot = findings_acc[key]
        # upgrade confidence if a stronger one appears
        rank = {"low": 0, "medium": 1, "high": 2}
        if rank[confidence] > rank[slot["confidence"]]:
            slot["confidence"] = confidence
        slot["evidence"].append(evidence)
        slot["amount"] += amount or 0.0
        if date:
            slot["dates"].append(date)
        slot["sources"].add(source_tag)

    # ------- Rule 1: FEC committee name match -------
    qualifying_committees: dict[str, str] = {}  # committee_id -> label
    for r in cur.execute("SELECT committee_id, committee_name FROM fec_committee_cache").fetchall():
        label = classify_committee(r["committee_name"])
        if label:
            qualifying_committees[r["committee_id"]] = label

    print(f"Qualifying real-estate FEC committees in cache: {len(qualifying_committees)}")
    for cid, lbl in qualifying_committees.items():
        print(f"  {cid} -> {lbl}")

    if qualifying_committees:
        placeholders = ",".join(["?"] * len(qualifying_committees))
        rows = cur.execute(
            f"""
            SELECT id, donor_id, committee_id, contribution_amount, contribution_date,
                   fec_sub_id
            FROM fec_contributions_raw
            WHERE committee_id IN ({placeholders})
              AND donor_id IS NOT NULL
            """,
            list(qualifying_committees.keys()),
        ).fetchall()
        print(f"FEC contribution rows to real-estate committees: {len(rows)}")

        for r in rows:
            cid = r["committee_id"]
            label = qualifying_committees[cid]
            cname = cur.execute(
                "SELECT committee_name FROM fec_committee_cache WHERE committee_id = ?",
                (cid,),
            ).fetchone()["committee_name"]
            amount = float(r["contribution_amount"] or 0.0)
            date = r["contribution_date"] or ""
            ev = {
                "source": "FEC schedule_a",
                "source_url": f"https://www.fec.gov/data/receipts/?committee_id={cid}",
                "evidence_text": (
                    f"Contributed ${amount:,.2f} to {cname} on {date} "
                    f"(FEC sub_id {r['fec_sub_id']})"
                ),
                "contribution_id": f"fec_contributions_raw.id={r['id']}",
                "committee_id": cid,
                "committee_name": cname,
                "amount": amount,
                "date": date,
                "rule": "fec_committee_name_match",
            }
            add(r["donor_id"], label, "high", ev, amount, date, "fec_committee")

    # ------- Rule 2 + 3: TEC contributions employer/occupation -------
    tec_rows = cur.execute(
        """
        SELECT contribution_info_id, donor_id, contributor_employer, contributor_occupation,
               contribution_amount, contribution_dt, candidate_slug
        FROM contributions
        WHERE donor_id IS NOT NULL
          AND (info_only_flag IS NULL OR info_only_flag != 'Y')
        """
    ).fetchall()
    print(f"TEC contribution rows scanned: {len(tec_rows)}")

    for r in tec_rows:
        donor_id = r["donor_id"]
        emp = r["contributor_employer"]
        occ = r["contributor_occupation"]
        amount = float(r["contribution_amount"] or 0.0)
        date = r["contribution_dt"] or ""
        cid = r["contribution_info_id"]
        slug = r["candidate_slug"]

        for kw, conf in employer_match(emp):
            label = f"Employer: {kw}"
            ev = {
                "source": "employer_match",
                "source_url": None,
                "evidence_text": (
                    f"TEC contribution: ${amount:,.2f} to {slug} on {date}; "
                    f"employer={emp!r}; matched keyword={kw!r}"
                ),
                "contribution_id": f"contributions.contribution_info_id={cid}",
                "committee_id": None,
                "committee_name": None,
                "amount": amount,
                "date": date,
                "rule": f"employer_match: {kw}",
            }
            add(donor_id, label, conf, ev, amount, date, "tec_employer")

        for kw, conf in occupation_match(occ):
            label = f"Occupation: {kw}"
            ev = {
                "source": "occupation_match",
                "source_url": None,
                "evidence_text": (
                    f"TEC contribution: ${amount:,.2f} to {slug} on {date}; "
                    f"occupation={occ!r}; matched keyword={kw!r}"
                ),
                "contribution_id": f"contributions.contribution_info_id={cid}",
                "committee_id": None,
                "committee_name": None,
                "amount": amount,
                "date": date,
                "rule": f"occupation_match: {kw}",
            }
            add(donor_id, label, conf, ev, amount, date, "tec_occupation")

    # ------- Rule 2 + 3: FEC contributions employer/occupation -------
    fec_rows = cur.execute(
        """
        SELECT id, donor_id, committee_id, fec_employer, fec_occupation,
               contribution_amount, contribution_date, fec_sub_id
        FROM fec_contributions_raw
        WHERE donor_id IS NOT NULL
        """
    ).fetchall()
    print(f"FEC contribution rows scanned for emp/occ: {len(fec_rows)}")

    for r in fec_rows:
        donor_id = r["donor_id"]
        emp = r["fec_employer"]
        occ = r["fec_occupation"]
        amount = float(r["contribution_amount"] or 0.0)
        date = r["contribution_date"] or ""
        rid = r["id"]
        cid = r["committee_id"] or ""

        for kw, conf in employer_match(emp):
            label = f"Employer: {kw}"
            ev = {
                "source": "employer_match",
                "source_url": f"https://www.fec.gov/data/receipts/?committee_id={cid}" if cid else None,
                "evidence_text": (
                    f"FEC contribution: ${amount:,.2f} on {date}; "
                    f"employer={emp!r}; matched keyword={kw!r}"
                ),
                "contribution_id": f"fec_contributions_raw.id={rid}",
                "committee_id": cid or None,
                "committee_name": None,
                "amount": amount,
                "date": date,
                "rule": f"employer_match: {kw}",
            }
            add(donor_id, label, conf, ev, amount, date, "fec_employer")

        for kw, conf in occupation_match(occ):
            label = f"Occupation: {kw}"
            ev = {
                "source": "occupation_match",
                "source_url": f"https://www.fec.gov/data/receipts/?committee_id={cid}" if cid else None,
                "evidence_text": (
                    f"FEC contribution: ${amount:,.2f} on {date}; "
                    f"occupation={occ!r}; matched keyword={kw!r}"
                ),
                "contribution_id": f"fec_contributions_raw.id={rid}",
                "committee_id": cid or None,
                "committee_name": None,
                "amount": amount,
                "date": date,
                "rule": f"occupation_match: {kw}",
            }
            add(donor_id, label, conf, ev, amount, date, "fec_occupation")

    # ------- Build per-donor totals for the sensitive flag -------
    donor_totals: dict[str, float] = defaultdict(float)
    donor_sources: dict[str, set] = defaultdict(set)
    for (donor_id, _label), slot in findings_acc.items():
        donor_totals[donor_id] += slot["amount"]
        donor_sources[donor_id].update(slot["sources"])

    # Validate donors exist in donor_identities; drop with a warning otherwise
    valid_donors = {
        r[0] for r in cur.execute("SELECT donor_id FROM donor_identities").fetchall()
    }

    findings = []
    for (donor_id, label), slot in findings_acc.items():
        if donor_id not in valid_donors:
            print(f"WARN: donor_id {donor_id} not in donor_identities; skipping label={label}")
            continue

        # Identity + HD-41 backing for richer notes
        ident = cur.execute(
            "SELECT canonical_name, total_donated FROM donor_identities WHERE donor_id = ?",
            (donor_id,),
        ).fetchone()
        canonical_name = ident["canonical_name"] if ident else "(unknown)"

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
            for cr in cand_rows
            if cr["total"]
        ]
        cand_str = ", ".join(cand_parts) if cand_parts else "(no HD-41 contributions)"

        dates = [d for d in slot["dates"] if d]
        first_seen = min(dates) if dates else None
        last_seen = max(dates) if dates else None

        n = len(slot["evidence"])
        total = round(slot["amount"], 2)

        # Sensitive: donor's combined real-estate $ across all labels > $5K
        # OR donor has 2+ distinct source types (employer + FEC committee, etc.)
        sensitive = (donor_totals[donor_id] > 5000.0) or (len(donor_sources[donor_id]) >= 2)

        notes = (
            f"{n} evidence row{'s' if n != 1 else ''} for label {label!r}; "
            f"donor {canonical_name}; HD-41 backing: {cand_str}; "
            f"donor's combined real-estate-tagged $ = ${donor_totals[donor_id]:,.2f} "
            f"across {len(donor_sources[donor_id])} source type(s)"
        )

        findings.append(
            {
                "donor_id": donor_id,
                "label": label,
                "total_amount": total,
                "confidence": slot["confidence"],
                "first_seen": first_seen,
                "last_seen": last_seen,
                "notes": notes,
                "sensitive": sensitive,
                "evidence": slot["evidence"],
            }
        )

    findings.sort(key=lambda f: (f["label"], -f["total_amount"], f["donor_id"]))

    payload = {
        "category": "real_estate",
        "rules": [
            {
                "name": "fec_committee_name_match",
                "description": (
                    "Donor gave to a FEC committee whose name matches "
                    "real-estate-industry patterns (NAR, MORPAC, NMHC, NAHB, TREPAC, "
                    "Texas REALTORS, Texas Apartment Assn, CREFC, Real Estate Roundtable, "
                    "or generic real-estate/realty/apartment/homebuilder + PAC/Action Fund)."
                ),
            },
            {
                "name": "employer_match",
                "description": (
                    "contributor_employer / fec_employer contains a real-estate keyword. "
                    "High confidence: realtor, realty, real estate, properties, property "
                    "management, brokerage, apartment, multifamily, land development, "
                    "homebuilder, title company, title insurance, mortgage, appraisal, "
                    "subdivision, named builders (Lennar, KB Home, D.R. Horton, Pulte, "
                    "Toll Brothers, Weston, McAnally), or '<X> Homes' company names. "
                    "Medium confidence: 2+ generic tokens (development/construction/"
                    "builder/investments/holdings) together."
                ),
            },
            {
                "name": "occupation_match",
                "description": (
                    "contributor_occupation / fec_occupation contains a real-estate "
                    "keyword. High: realtor, real estate (broker/agent/developer), "
                    "property manager/management, mortgage broker, appraiser, title "
                    "agent, homebuilder. Medium: developer, general contractor, broker "
                    "(STOCKBROKER and software/web/app developer excluded)."
                ),
            },
        ],
        "findings": findings,
    }

    Path(OUT_PATH).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    # ---- summary ----
    n_findings = len(findings)
    n_evidence = sum(len(f["evidence"]) for f in findings)
    total_impl = sum(f["total_amount"] for f in findings)
    n_sensitive = sum(1 for f in findings if f["sensitive"])
    unique_donors = len({f["donor_id"] for f in findings})

    by_rule = defaultdict(int)
    by_label = defaultdict(float)
    by_conf = defaultdict(int)
    for f in findings:
        by_conf[f["confidence"]] += 1
        by_label[f["label"]] += f["total_amount"]
        for e in f["evidence"]:
            # bucket by rule prefix (strip ': <keyword>')
            rule = e["rule"].split(":", 1)[0]
            by_rule[rule] += 1

    print()
    print("---- SUMMARY ----")
    print(f"Findings (donor,label rows): {n_findings}")
    print(f"Unique donors implicated:    {unique_donors}")
    print(f"Evidence rows:               {n_evidence}")
    print(f"Total $ in evidence:         ${total_impl:,.2f}")
    print(f"Sensitive flagged:           {n_sensitive}")
    print(f"Confidence distribution:     {dict(by_conf)}")
    print("Evidence by rule:")
    for rule, n in sorted(by_rule.items(), key=lambda x: -x[1]):
        print(f"  {rule}: {n}")
    print("Top labels by total $:")
    for label, amt in sorted(by_label.items(), key=lambda x: -x[1])[:10]:
        print(f"  ${amt:,.2f}  {label}")

    # Top donors
    donor_summary = defaultdict(float)
    donor_name = {}
    for f in findings:
        donor_summary[f["donor_id"]] += f["total_amount"]
    for d in donor_summary:
        row = cur.execute(
            "SELECT canonical_name FROM donor_identities WHERE donor_id = ?", (d,)
        ).fetchone()
        donor_name[d] = row["canonical_name"] if row else "(unknown)"
    print("Top 10 donors by tagged $:")
    for d, amt in sorted(donor_summary.items(), key=lambda x: -x[1])[:10]:
        print(f"  ${amt:,.2f}  {donor_name[d]}  ({d})")

    # Candidate distribution (HD-41 dollars from these donors)
    cand_dist = defaultdict(float)
    donor_ids = list({f["donor_id"] for f in findings})
    if donor_ids:
        placeholders = ",".join(["?"] * len(donor_ids))
        for row in cur.execute(
            f"""
            SELECT candidate_slug, SUM(contribution_amount) AS total
            FROM contributions
            WHERE donor_id IN ({placeholders})
              AND (info_only_flag IS NULL OR info_only_flag != 'Y')
            GROUP BY candidate_slug
            """,
            donor_ids,
        ).fetchall():
            cand_dist[row[0]] += row[1] or 0.0
    print("HD-41 candidate $ from these donors:")
    for slug, amt in sorted(cand_dist.items(), key=lambda x: -x[1]):
        print(f"  ${amt:,.2f}  {slug}")

    print(f"Wrote: {OUT_PATH}")


if __name__ == "__main__":
    main()
