"""
scan_adl.py
Scan HD-41 donors for Anti-Defamation League (ADL) affiliations.

Writes findings_adl.json conforming to the affiliations_integrate.py schema.

Rules:
  1. fec_committee_id_match / fec_committee_name_match
       fec_committee_cache.committee_name matches:
         - \bAnti.?Defamation League\b
         - \bADL Action\b
         - \bADL\b (word-boundaried)
       Then any donor with fec_contributions_raw rows to that committee.
       Confidence: high.
  2. fec_employer_match
       fec_contributions_raw.fec_employer matches \bANTI.?DEFAMATION LEAGUE\b
       or \bADL\b. Confidence: medium.
  3. local_employer_match
       contributions.contributor_employer matches above patterns
       (info_only_flag != 'Y'). Confidence: medium.

Donors employed by ADL itself get sensitive=true.
"""

from __future__ import annotations
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

DB_PATH = r"C:\Users\Hamza Sait\Electoral\HD-41\hd41_finance.db"
OUT_PATH = r"C:\Users\Hamza Sait\Electoral\HD-41\findings_adl.json"

# Word-boundaried patterns. ADL itself: require word boundaries to avoid
# false positives like "PADLOCK" or "SADDLE".
RE_FULL_NAME = re.compile(r"\bAnti.?Defamation League\b", re.IGNORECASE)
RE_ADL_ACTION = re.compile(r"\bADL Action\b", re.IGNORECASE)
RE_ADL_BARE = re.compile(r"\bADL\b")  # case-sensitive: avoid "padl..."-style false hits
RE_EMP_FULL = re.compile(r"\bANTI.?DEFAMATION LEAGUE\b", re.IGNORECASE)
RE_EMP_ADL = re.compile(r"\bADL\b")


def matches_committee(name: str) -> bool:
    if not name:
        return False
    return bool(
        RE_FULL_NAME.search(name)
        or RE_ADL_ACTION.search(name)
        or RE_ADL_BARE.search(name)
    )


def matches_employer(emp: str) -> bool:
    if not emp:
        return False
    return bool(RE_EMP_FULL.search(emp) or RE_EMP_ADL.search(emp))


def fec_url_for_committee(cid: str) -> str:
    return f"https://www.fec.gov/data/committee/{cid}/" if cid else ""


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # findings[donor_id][label] -> dict
    findings: dict[str, dict[str, dict]] = defaultdict(dict)

    # --- Rule 1: FEC committee match ---
    cur.execute(
        "SELECT committee_id, committee_name FROM fec_committee_cache"
    )
    adl_committees = [
        (row["committee_id"], row["committee_name"])
        for row in cur.fetchall()
        if matches_committee(row["committee_name"] or "")
    ]

    for cid, cname in adl_committees:
        cur.execute(
            """
            SELECT id, donor_id, contribution_amount, contribution_date,
                   fec_contributor_name, fec_employer, fec_occupation, fec_sub_id
              FROM fec_contributions_raw
             WHERE committee_id = ?
               AND donor_id IS NOT NULL
            """,
            (cid,),
        )
        for row in cur.fetchall():
            did = row["donor_id"]
            label = cname
            entry = findings[did].setdefault(
                label,
                {
                    "donor_id": did,
                    "label": label,
                    "total_amount": 0.0,
                    "confidence": "high",
                    "first_seen": None,
                    "last_seen": None,
                    "notes": f"Contributed to ADL-affiliated FEC committee {cid}.",
                    "sensitive": False,
                    "evidence": [],
                },
            )
            amt = float(row["contribution_amount"] or 0.0)
            entry["total_amount"] += amt
            dt = row["contribution_date"]
            if dt:
                if entry["first_seen"] is None or dt < entry["first_seen"]:
                    entry["first_seen"] = dt
                if entry["last_seen"] is None or dt > entry["last_seen"]:
                    entry["last_seen"] = dt
            entry["evidence"].append(
                {
                    "source": "FEC schedule_a",
                    "source_url": fec_url_for_committee(cid),
                    "evidence_text": (
                        f"Contributed ${amt:,.2f} to {cname} on {dt}"
                    ),
                    "contribution_id": f"fec_contributions_raw.id={row['id']}",
                    "committee_id": cid,
                    "committee_name": cname,
                    "amount": amt,
                    "date": dt,
                    "rule": "fec_committee_id_match",
                }
            )

    # --- Rule 2: FEC employer match ---
    cur.execute(
        """
        SELECT id, donor_id, contribution_amount, contribution_date,
               fec_contributor_name, fec_employer, fec_occupation,
               committee_id
          FROM fec_contributions_raw
         WHERE donor_id IS NOT NULL
           AND fec_employer IS NOT NULL
           AND fec_employer != ''
        """
    )
    emp_donor_label: dict[tuple[str, str], str] = {}
    for row in cur.fetchall():
        emp = row["fec_employer"]
        if not matches_employer(emp):
            continue
        did = row["donor_id"]
        label = f"Employer: {emp.strip().title()}"
        entry = findings[did].setdefault(
            label,
            {
                "donor_id": did,
                "label": label,
                "total_amount": None,  # employer match is not an amount-to-ADL signal
                "confidence": "medium",
                "first_seen": None,
                "last_seen": None,
                "notes": "FEC filings list employer as ADL/Anti-Defamation League.",
                "sensitive": True,  # works for ADL itself — worth a flag
                "evidence": [],
            },
        )
        dt = row["contribution_date"]
        if dt:
            if entry["first_seen"] is None or dt < entry["first_seen"]:
                entry["first_seen"] = dt
            if entry["last_seen"] is None or dt > entry["last_seen"]:
                entry["last_seen"] = dt
        entry["evidence"].append(
            {
                "source": "FEC schedule_a",
                "source_url": fec_url_for_committee(row["committee_id"] or ""),
                "evidence_text": (
                    f"FEC employer listed as '{emp}' on contribution dated {dt}"
                ),
                "contribution_id": f"fec_contributions_raw.id={row['id']}",
                "committee_id": row["committee_id"],
                "committee_name": None,
                "amount": float(row["contribution_amount"] or 0.0),
                "date": dt,
                "rule": "fec_employer_match",
            }
        )
        emp_donor_label[(did, label)] = label

    # --- Rule 3: Local employer match ---
    cur.execute(
        """
        SELECT contribution_info_id, donor_id, contributor_employer,
               contribution_amount, contribution_dt, candidate_slug
          FROM contributions
         WHERE donor_id IS NOT NULL
           AND contributor_employer IS NOT NULL
           AND contributor_employer != ''
           AND (info_only_flag IS NULL OR info_only_flag != 'Y')
        """
    )
    for row in cur.fetchall():
        emp = row["contributor_employer"]
        if not matches_employer(emp):
            continue
        did = row["donor_id"]
        label = f"Employer: {emp.strip().title()}"
        entry = findings[did].setdefault(
            label,
            {
                "donor_id": did,
                "label": label,
                "total_amount": None,
                "confidence": "medium",
                "first_seen": None,
                "last_seen": None,
                "notes": "HD-41 contribution filings list employer as ADL/Anti-Defamation League.",
                "sensitive": True,
                "evidence": [],
            },
        )
        dt = row["contribution_dt"]
        if dt:
            if entry["first_seen"] is None or dt < entry["first_seen"]:
                entry["first_seen"] = dt
            if entry["last_seen"] is None or dt > entry["last_seen"]:
                entry["last_seen"] = dt
        entry["evidence"].append(
            {
                "source": "employer_match",
                "source_url": "",
                "evidence_text": (
                    f"HD-41 contribution lists employer as '{emp}' "
                    f"on {dt} (candidate {row['candidate_slug']})"
                ),
                "contribution_id": f"contributions.contribution_info_id={row['contribution_info_id']}",
                "committee_id": None,
                "committee_name": None,
                "amount": float(row["contribution_amount"] or 0.0),
                "date": dt,
                "rule": "local_employer_match",
            }
        )

    # Flatten
    out_findings = []
    for did, by_label in findings.items():
        for label, entry in by_label.items():
            out_findings.append(entry)

    out = {
        "category": "adl",
        "rules": [
            {
                "name": "fec_committee_id_match",
                "description": (
                    "Donor contributed to a FEC committee whose name "
                    "matches \\bAnti.?Defamation League\\b, \\bADL Action\\b, "
                    "or \\bADL\\b (word-boundaried). Confidence: high."
                ),
            },
            {
                "name": "fec_employer_match",
                "description": (
                    "Donor's FEC employer field matches "
                    "\\bANTI.?DEFAMATION LEAGUE\\b or \\bADL\\b. "
                    "Confidence: medium. Sensitive: true (employed by ADL)."
                ),
            },
            {
                "name": "local_employer_match",
                "description": (
                    "Donor's HD-41 contribution employer field matches "
                    "ADL patterns. Confidence: medium. Sensitive: true."
                ),
            },
        ],
        "findings": out_findings,
    }

    Path(OUT_PATH).write_text(json.dumps(out, indent=2), encoding="utf-8")

    findings_count = len(out_findings)
    evidence_count = sum(len(f["evidence"]) for f in out_findings)
    dollars = sum((f["total_amount"] or 0.0) for f in out_findings)
    sensitive_count = sum(1 for f in out_findings if f.get("sensitive"))
    print(
        f"findings={findings_count} evidence={evidence_count} "
        f"$implicated={dollars:,.2f} sensitive={sensitive_count}"
    )
    print(f"adl_committees_in_cache={len(adl_committees)}")

    conn.close()


if __name__ == "__main__":
    main()
