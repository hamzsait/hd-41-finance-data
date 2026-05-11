"""
scan_oil_gas.py — HD-41 oil & gas donor affiliation scan.

Outputs findings_oil_gas.json conforming to the affiliations_integrate.py schema.

Three matching rules (per spec):
  1. fec_committee_name_match — donors who gave to FEC oil & gas committees
  2. employer_match — keyword match in TEC contributor_employer or FEC fec_employer
  3. occupation_match — keyword match in contributor_occupation or fec_occupation

Confidence:
  high   — FEC committee match OR specific-company employer keyword
  medium — generic petroleum/oilfield/drilling keyword, or occupation match

Sensitive flag:
  total oil_gas $ for donor > $5,000  OR  donor backs a Dem candidate
  (Haddad / Salinas / Holguin) — flagged for human review.
"""
from __future__ import annotations
import json
import pathlib
import re
import sqlite3
from collections import defaultdict

DB_PATH = pathlib.Path(__file__).parent / "hd41_finance.db"
OUT_PATH = pathlib.Path(__file__).parent / "findings_oil_gas.json"

# -----------------------------------------------------------------------------
# Rule 1 — FEC committee name patterns. Each pattern is a compiled regex (i-case)
# applied to fec_committee_cache.committee_name.
# -----------------------------------------------------------------------------
FEC_PATTERNS = [
    r"Exxon",
    r"ExxonMobil",
    r"Chevron",
    r"\bBP\b.*(America|PAC)|(America|PAC).*\bBP\b",
    r"Conoco",
    r"ConocoPhillips",
    r"Phillips\s*66",
    r"Marathon\s*Petroleum",
    r"Marathon\s*Oil",
    r"Valero",
    r"Halliburton",
    r"Schlumberger",
    r"\bSLB\b",
    r"Baker\s*Hughes",
    r"\bOccidental\s*Petroleum\b",
    r"\bOXY\b",
    r"Pioneer\s*Natural\s*Resources",
    r"Anadarko",
    r"Devon\s*Energy",
    r"\bEOG\s*Resources\b",
    r"Apache\s*Corp",
    r"Hess\s*Corp",
    r"American\s*Petroleum\s*Institute",
    r"\bAPI\b.*PAC",
    r"Independent\s*Petroleum\s*Association",
    r"Texas\s*Oil\s*&\s*Gas\s*Association",
    r"TXOGA",
    r"Permian\s*Basin\s*Petroleum",
    r"Energy\s*Transfer\s*Partners",
    r"Kinder\s*Morgan",
    r"Williams\s*Companies",
    # Combined "industry word + PAC"
    r"\bOil\b.*\bPAC\b",
    r"\bPetroleum\b.*\bPAC\b",
    r"\bDrilling\b.*\bPAC\b",
    r"\bOilfield\b.*\bPAC\b",
    r"\bRefining\b.*\bPAC\b",
    r"\bUpstream\b.*\bPAC\b",
    r"\bMidstream\b.*\bPAC\b",
]
FEC_REGEX = [re.compile(p, re.IGNORECASE) for p in FEC_PATTERNS]

# -----------------------------------------------------------------------------
# Rule 2 — Employer keyword lists. Substring match (case-insensitive).
# -----------------------------------------------------------------------------
HIGH_CONF_EMPLOYER = [
    "exxon", "chevron", "conoco", "phillips 66", "marathon", "valero",
    "halliburton", "schlumberger", "slb", "baker hughes", "occidental",
    "oxy", "pioneer natural resources", "anadarko", "devon energy",
    "eog resources", "apache", "hess", "kinder morgan", "williams companies",
    "energy transfer", "plains all american", "magellan midstream",
    "enterprise products", "sunoco", "citgo", "koch industries", "pemex", "shell",
]
MED_CONF_EMPLOYER = [
    "oil & gas", "oil and gas", "petroleum", "drilling", "oilfield services",
    "upstream", "midstream", "downstream", "refining", "exploration & production",
    "wellhead", "fracking", "hydraulic fracturing", "energy services",
    "permian basin",
]

# -----------------------------------------------------------------------------
# Rule 3 — Occupation keywords
# -----------------------------------------------------------------------------
OCCUPATION_KEYWORDS = [
    "petroleum engineer", "geologist", "landman", "oilfield",
    "oil & gas", "petroleum",
]

DEM_CANDIDATES = {"haddad", "salinas", "holguin"}


def _norm(s: str | None) -> str:
    return (s or "").strip().lower()


def main() -> int:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # ---- Per-donor candidate-slug map (for sensitive flag) -------------------
    donor_candidates: dict[str, set[str]] = defaultdict(set)
    for r in cur.execute(
        "SELECT donor_id, candidate_slug FROM contributions "
        "WHERE info_only_flag != 'Y' AND donor_id IS NOT NULL"
    ):
        donor_candidates[r["donor_id"]].add(r["candidate_slug"])

    # ---- Findings accumulator: key=(donor_id, label) -------------------------
    #   value = dict with confidence, evidence[], notes
    findings_acc: dict[tuple[str, str], dict] = {}

    def _add_finding(donor_id: str, label: str, confidence: str,
                     evidence: dict, notes: str | None = None):
        key = (donor_id, label)
        if key not in findings_acc:
            findings_acc[key] = {
                "donor_id": donor_id,
                "label": label,
                "confidence": confidence,
                "evidence": [],
                "notes": notes,
                "amounts": [],
                "dates": [],
            }
        # Upgrade confidence if a stronger one arrives
        prev = findings_acc[key]["confidence"]
        if confidence == "high" and prev != "high":
            findings_acc[key]["confidence"] = "high"
        findings_acc[key]["evidence"].append(evidence)
        amt = evidence.get("amount")
        dt = evidence.get("date")
        if amt is not None:
            findings_acc[key]["amounts"].append(amt)
        if dt:
            findings_acc[key]["dates"].append(dt)

    # =========================================================================
    # Rule 1 — FEC committee name match
    # =========================================================================
    matched_committees: dict[str, str] = {}  # committee_id -> committee_name
    for row in cur.execute("SELECT committee_id, committee_name FROM fec_committee_cache"):
        name = row["committee_name"] or ""
        for rx in FEC_REGEX:
            if rx.search(name):
                matched_committees[row["committee_id"]] = name
                break

    if matched_committees:
        ph = ",".join("?" * len(matched_committees))
        sql = (
            f"SELECT donor_id, committee_id, contribution_amount, contribution_date, "
            f"fec_sub_id, fec_employer, fec_occupation "
            f"FROM fec_contributions_raw WHERE committee_id IN ({ph}) "
            f"AND donor_id IS NOT NULL"
        )
        for r in cur.execute(sql, list(matched_committees.keys())):
            cid = r["committee_id"]
            cname = matched_committees[cid]
            label = f"FEC: {cname}"
            ev = {
                "source": "FEC schedule_a",
                "source_url": f"https://www.fec.gov/data/committee/{cid}/",
                "evidence_text": (
                    f"Contributed ${r['contribution_amount']:.2f} to "
                    f"{cname} on {r['contribution_date']}"
                ),
                "contribution_id": f"fec_contributions_raw.fec_sub_id={r['fec_sub_id']}",
                "committee_id": cid,
                "committee_name": cname,
                "amount": r["contribution_amount"],
                "date": r["contribution_date"],
                "rule": "fec_committee_name_match",
            }
            _add_finding(
                r["donor_id"], label, "high", ev,
                notes=f"Donor contributed to FEC-registered oil & gas committee '{cname}'.",
            )

    # =========================================================================
    # Rule 2 — Employer match (TEC + FEC sources)
    # =========================================================================
    def _scan_employer(donor_id: str, employer: str | None, source: str,
                       amount: float | None, date: str | None,
                       contribution_id: str | None,
                       extra_evidence: dict | None = None):
        if not employer:
            return
        emp_l = employer.lower()
        # High-conf keywords first
        matched_kw: list[tuple[str, str]] = []  # (keyword, confidence)
        for kw in HIGH_CONF_EMPLOYER:
            if kw in emp_l:
                matched_kw.append((kw, "high"))
        # Medium — only count if not already covered by a high-conf hit
        # (still counted as separate label because spec says one per keyword)
        for kw in MED_CONF_EMPLOYER:
            if kw in emp_l:
                # Skip generic 'energy services' if 'energy' alone matched another industry?
                # spec: skip generic "energy" alone — we already excluded it
                matched_kw.append((kw, "medium"))
        if not matched_kw:
            return
        for kw, conf in matched_kw:
            label = f"employer: {kw}"
            ev = {
                "source": source,
                "source_url": None,
                "evidence_text": (
                    f"Employer '{employer}' matches oil & gas keyword '{kw}'"
                    + (f" (${amount:.2f}" if amount is not None else "")
                    + (f" on {date})" if date else (")" if amount is not None else ""))
                ),
                "contribution_id": contribution_id,
                "committee_id": None,
                "committee_name": None,
                "amount": amount,
                "date": date,
                "rule": f"employer_match: {kw}",
            }
            if extra_evidence:
                ev.update(extra_evidence)
            _add_finding(
                donor_id, label, conf, ev,
                notes=f"Donor's employer '{employer}' matches keyword '{kw}'.",
            )

    # ---- TEC contributions employer scan ------------------------------------
    for r in cur.execute(
        "SELECT donor_id, contributor_employer, contribution_amount, "
        "contribution_dt, contribution_info_id "
        "FROM contributions "
        "WHERE info_only_flag != 'Y' AND donor_id IS NOT NULL"
    ):
        _scan_employer(
            donor_id=r["donor_id"],
            employer=r["contributor_employer"],
            source="employer_match (TEC)",
            amount=r["contribution_amount"],
            date=r["contribution_dt"],
            contribution_id=f"contributions.contribution_info_id={r['contribution_info_id']}",
        )

    # ---- FEC contributions employer scan ------------------------------------
    for r in cur.execute(
        "SELECT donor_id, fec_employer, contribution_amount, contribution_date, "
        "fec_sub_id, committee_id "
        "FROM fec_contributions_raw WHERE donor_id IS NOT NULL"
    ):
        _scan_employer(
            donor_id=r["donor_id"],
            employer=r["fec_employer"],
            source="employer_match (FEC)",
            amount=r["contribution_amount"],
            date=r["contribution_date"],
            contribution_id=f"fec_contributions_raw.fec_sub_id={r['fec_sub_id']}",
            extra_evidence={"committee_id": r["committee_id"]},
        )

    # =========================================================================
    # Rule 3 — Occupation match (TEC + FEC)
    # =========================================================================
    def _scan_occupation(donor_id: str, occupation: str | None, source: str,
                         amount: float | None, date: str | None,
                         contribution_id: str | None):
        if not occupation:
            return
        occ_l = occupation.lower()
        for kw in OCCUPATION_KEYWORDS:
            if kw in occ_l:
                label = f"occupation: {kw}"
                ev = {
                    "source": source,
                    "source_url": None,
                    "evidence_text": (
                        f"Occupation '{occupation}' matches keyword '{kw}'"
                    ),
                    "contribution_id": contribution_id,
                    "committee_id": None,
                    "committee_name": None,
                    "amount": amount,
                    "date": date,
                    "rule": f"occupation_match: {kw}",
                }
                _add_finding(
                    donor_id, label, "medium", ev,
                    notes=f"Donor's occupation '{occupation}' matches keyword '{kw}'.",
                )

    for r in cur.execute(
        "SELECT donor_id, contributor_occupation, contribution_amount, "
        "contribution_dt, contribution_info_id "
        "FROM contributions "
        "WHERE info_only_flag != 'Y' AND donor_id IS NOT NULL"
    ):
        _scan_occupation(
            donor_id=r["donor_id"],
            occupation=r["contributor_occupation"],
            source="occupation_match (TEC)",
            amount=r["contribution_amount"],
            date=r["contribution_dt"],
            contribution_id=f"contributions.contribution_info_id={r['contribution_info_id']}",
        )

    for r in cur.execute(
        "SELECT donor_id, fec_occupation, contribution_amount, contribution_date, "
        "fec_sub_id FROM fec_contributions_raw WHERE donor_id IS NOT NULL"
    ):
        _scan_occupation(
            donor_id=r["donor_id"],
            occupation=r["fec_occupation"],
            source="occupation_match (FEC)",
            amount=r["contribution_amount"],
            date=r["contribution_date"],
            contribution_id=f"fec_contributions_raw.fec_sub_id={r['fec_sub_id']}",
        )

    # =========================================================================
    # Build findings list (one per (donor_id, label))
    # =========================================================================
    # Per-donor total oil_gas $ — used for sensitive flag. Sum amounts across
    # ALL findings for that donor (de-duped on contribution_id).
    donor_total_dollars: dict[str, float] = defaultdict(float)
    donor_seen_contribs: dict[str, set[str]] = defaultdict(set)
    for (donor_id, label), data in findings_acc.items():
        for ev in data["evidence"]:
            cid = ev.get("contribution_id") or f"_{id(ev)}"
            if cid in donor_seen_contribs[donor_id]:
                continue
            donor_seen_contribs[donor_id].add(cid)
            amt = ev.get("amount")
            if isinstance(amt, (int, float)):
                donor_total_dollars[donor_id] += amt

    findings = []
    for (donor_id, label), data in findings_acc.items():
        amounts = [a for a in data["amounts"] if isinstance(a, (int, float))]
        dates = sorted(d for d in data["dates"] if d)
        total_amount = sum(amounts) if amounts else None
        first_seen = dates[0] if dates else None
        last_seen = dates[-1] if dates else None

        # Sensitive logic
        sensitive = False
        if donor_total_dollars[donor_id] > 5000:
            sensitive = True
        cands = donor_candidates.get(donor_id, set())
        if cands & DEM_CANDIDATES:
            sensitive = True

        findings.append({
            "donor_id": donor_id,
            "label": label,
            "total_amount": total_amount,
            "confidence": data["confidence"],
            "first_seen": first_seen,
            "last_seen": last_seen,
            "notes": data["notes"],
            "sensitive": sensitive,
            "evidence": data["evidence"],
        })

    payload = {
        "category": "oil_gas",
        "rules": [
            {
                "name": "fec_committee_name_match",
                "description": (
                    "Donors who gave to any FEC committee whose name matches a "
                    "curated list of oil & gas industry patterns "
                    "(Exxon, Chevron, Halliburton, Valero, API PAC, TXOGA, etc)."
                ),
            },
            {
                "name": "employer_match",
                "description": (
                    "Substring match on contributor_employer (TEC) or fec_employer (FEC) "
                    "against high-confidence company names (exxon, chevron, halliburton, "
                    "...) or medium-confidence generic keywords (petroleum, drilling, "
                    "oilfield, refining, ...). Generic 'energy' alone is excluded."
                ),
            },
            {
                "name": "occupation_match",
                "description": (
                    "Substring match on contributor_occupation (TEC) or fec_occupation "
                    "(FEC) against oil & gas roles "
                    "(petroleum engineer, geologist, landman, oilfield)."
                ),
            },
        ],
        "findings": findings,
    }

    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # =========================================================================
    # Summary
    # =========================================================================
    total_findings = len(findings)
    total_evidence = sum(len(f["evidence"]) for f in findings)
    total_dollars = sum(donor_total_dollars.values())
    n_sensitive = sum(1 for f in findings if f["sensitive"])
    unique_donors = len({f["donor_id"] for f in findings})

    # candidate breakdown
    cand_breakdown: dict[str, int] = defaultdict(int)
    cand_dollars: dict[str, float] = defaultdict(float)
    for f in findings:
        for c in donor_candidates.get(f["donor_id"], set()):
            cand_breakdown[c] += 1
        # attribute donor $ once per candidate via per-donor total
    for did, tot in donor_total_dollars.items():
        for c in donor_candidates.get(did, set()):
            cand_dollars[c] += tot

    # rule breakdown
    rule_breakdown: dict[str, int] = defaultdict(int)
    for f in findings:
        for ev in f["evidence"]:
            rule_breakdown[ev["rule"].split(":")[0]] += 1

    print("=" * 64)
    print("HD-41 oil_gas scan — summary")
    print("=" * 64)
    print(f"Findings:              {total_findings}")
    print(f"Unique donors flagged: {unique_donors}")
    print(f"Evidence rows:         {total_evidence}")
    print(f"$ implicated (oil_gas dollars across flagged donors): "
          f"${total_dollars:,.2f}")
    print(f"Sensitive findings:    {n_sensitive}")
    print()
    print("Rule breakdown (evidence rows):")
    for k, v in sorted(rule_breakdown.items(), key=lambda x: -x[1]):
        print(f"  {k:<30s} {v}")
    print()
    print("Candidate breakdown (findings count / $ from flagged donors):")
    for c in sorted(cand_breakdown.keys()):
        print(f"  {c:<10s}  {cand_breakdown[c]:>4d} findings  "
              f"${cand_dollars[c]:>10,.2f}")
    print()
    # Top donors
    print("Top 10 donors by oil_gas $ flagged:")
    top = sorted(donor_total_dollars.items(), key=lambda x: -x[1])[:10]
    for did, tot in top:
        nrow = cur.execute(
            "SELECT canonical_name, canonical_employer FROM donor_identities "
            "WHERE donor_id=?", (did,)
        ).fetchone()
        nm = nrow["canonical_name"] if nrow else "?"
        emp = nrow["canonical_employer"] if nrow else ""
        cands = ",".join(sorted(donor_candidates.get(did, set())))
        print(f"  ${tot:>10,.2f}  {nm[:35]:<35s}  emp={emp or '-'!s:<25}  ({cands})")

    print()
    print(f"Wrote {OUT_PATH}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
