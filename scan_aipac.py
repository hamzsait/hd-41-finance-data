"""Scan HD-41 donors for AIPAC affiliations via FEC schedule_a contributions.

Writes findings_aipac.json. Read-only against hd41_finance.db.
"""

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

DB_PATH = r"C:\Users\Hamza Sait\Electoral\HD-41\hd41_finance.db"
OUT_PATH = r"C:\Users\Hamza Sait\Electoral\HD-41\findings_aipac.json"

# Known AIPAC-affiliated FEC committees (curated)
KNOWN_AIPAC_COMMITTEES = {
    "C00797670": "AIPAC PAC",
    "C00799031": "United Democracy Project",
}

# Name patterns indicating AIPAC affiliation (case-insensitive, word boundaries)
NAME_PATTERNS = [
    (re.compile(r"\bAIPAC\b", re.IGNORECASE), "AIPAC PAC"),
    (re.compile(r"\bUnited Democracy Project\b", re.IGNORECASE), "United Democracy Project"),
]


def classify_committee(committee_id: str, committee_name: str | None):
    """Return (label, rule) if committee qualifies as AIPAC, else (None, None)."""
    if committee_id in KNOWN_AIPAC_COMMITTEES:
        return KNOWN_AIPAC_COMMITTEES[committee_id], "fec_committee_id_match"
    if committee_name:
        for pat, label in NAME_PATTERNS:
            if pat.search(committee_name):
                return label, "fec_committee_name_match"
    return None, None


def main():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # 1. Find ALL committees in fec_committee_cache that match AIPAC patterns by name,
    #    plus the known IDs. Build a working set of qualifying committee_ids.
    qualifying = {}  # committee_id -> (label, rule, committee_name)

    # Known IDs first (these may or may not be present in cache)
    for cid, label in KNOWN_AIPAC_COMMITTEES.items():
        row = cur.execute(
            "SELECT committee_name FROM fec_committee_cache WHERE committee_id = ?",
            (cid,),
        ).fetchone()
        cname = row["committee_name"] if row else label
        qualifying[cid] = (label, "fec_committee_id_match", cname)

    # Name scan across cache
    name_scan = cur.execute(
        """
        SELECT committee_id, committee_name
        FROM fec_committee_cache
        WHERE committee_name LIKE '%AIPAC%'
           OR committee_name LIKE '%United Democracy Project%'
        """
    ).fetchall()
    for r in name_scan:
        cid = r["committee_id"]
        cname = r["committee_name"] or ""
        label, rule = classify_committee(cid, cname)
        if not label:
            continue
        # Prefer fec_committee_id_match if already set
        if cid in qualifying and qualifying[cid][1] == "fec_committee_id_match":
            continue
        qualifying[cid] = (label, rule, cname)

    print(f"Qualifying AIPAC-related committees in cache: {len(qualifying)}")
    for cid, (label, rule, cname) in qualifying.items():
        print(f"  {cid} -> {label} ({rule}) name={cname!r}")

    if not qualifying:
        # No qualifying committees at all - still emit a valid empty findings file
        payload = build_payload([])
        write_output(payload)
        print_summary(payload)
        return

    # 2. Pull all fec_contributions_raw rows for these committee_ids
    placeholders = ",".join(["?"] * len(qualifying))
    rows = cur.execute(
        f"""
        SELECT id, donor_id, committee_id, contribution_amount, contribution_date,
               fec_sub_id, confirm_score
        FROM fec_contributions_raw
        WHERE committee_id IN ({placeholders})
          AND donor_id IS NOT NULL
        """,
        list(qualifying.keys()),
    ).fetchall()

    print(f"FEC contribution rows to AIPAC-affiliated committees: {len(rows)}")

    # Group by (donor_id, label)
    grouped = defaultdict(list)  # (donor_id, label) -> list of evidence dicts
    for r in rows:
        cid = r["committee_id"]
        label, rule, cname = qualifying[cid]
        amount = float(r["contribution_amount"] or 0.0)
        date = r["contribution_date"] or ""
        evidence = {
            "source": "FEC schedule_a",
            "source_url": f"https://www.fec.gov/data/receipts/?committee_id={cid}",
            "evidence_text": (
                f"Contributed ${amount:,.2f} to {label} on {date} "
                f"(FEC sub_id {r['fec_sub_id']})"
            ),
            "contribution_id": f"fec_contributions_raw.id={r['id']}",
            "committee_id": cid,
            "committee_name": cname,
            "amount": amount,
            "date": date,
            "rule": rule,
        }
        grouped[(r["donor_id"], label)].append(evidence)

    # 3. For each donor in findings, fetch donor identity + HD-41 candidate backing
    findings = []
    for (donor_id, label), evs in grouped.items():
        total_amount = sum(e["amount"] for e in evs)
        dates = [e["date"] for e in evs if e["date"]]
        first_seen = min(dates) if dates else None
        last_seen = max(dates) if dates else None

        # donor identity for HD-41 total
        ident = cur.execute(
            "SELECT canonical_name, total_donated FROM donor_identities WHERE donor_id = ?",
            (donor_id,),
        ).fetchone()
        hd41_total = float(ident["total_donated"]) if ident and ident["total_donated"] is not None else 0.0
        canonical_name = ident["canonical_name"] if ident else "(unknown)"

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
            for cr in cand_rows
            if cr["total"]
        ]
        cand_str = ", ".join(cand_parts) if cand_parts else "(no HD-41 contributions found)"

        n = len(evs)
        notes = (
            f"{n} contribution{'s' if n != 1 else ''} to {label}; "
            f"donor {canonical_name}; HD-41 backing: {cand_str}"
        )

        # Sensitive flag
        sensitive = (total_amount > 10000.0) or (hd41_total > 5000.0)

        finding = {
            "donor_id": donor_id,
            "label": label,
            "total_amount": round(total_amount, 2),
            "confidence": "high",
            "first_seen": first_seen,
            "last_seen": last_seen,
            "notes": notes,
            "sensitive": sensitive,
            "evidence": evs,
        }
        findings.append(finding)

    # Stable ordering
    findings.sort(key=lambda f: (f["label"], -f["total_amount"], f["donor_id"]))

    payload = build_payload(findings)
    write_output(payload)
    print_summary(payload)


def build_payload(findings):
    return {
        "category": "aipac",
        "rules": [
            {
                "name": "fec_committee_id_match",
                "description": "Donor gave to a known AIPAC-affiliated FEC committee",
            },
            {
                "name": "fec_committee_name_match",
                "description": "Donor gave to an FEC committee whose name matches AIPAC patterns",
            },
        ],
        "findings": findings,
    }


def write_output(payload):
    Path(OUT_PATH).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def print_summary(payload):
    findings = payload["findings"]
    n_findings = len(findings)
    n_evidence = sum(len(f["evidence"]) for f in findings)
    total_impl = sum(f["total_amount"] for f in findings)
    n_sensitive = sum(1 for f in findings if f["sensitive"])
    unique_donors = len({f["donor_id"] for f in findings})

    by_rule = defaultdict(int)
    for f in findings:
        for e in f["evidence"]:
            by_rule[e["rule"]] += 1

    print("---- SUMMARY ----")
    print(f"Findings (donor,label rows): {n_findings}")
    print(f"Unique donors implicated:    {unique_donors}")
    print(f"Evidence rows:               {n_evidence}")
    print(f"Total $ implicated:          ${total_impl:,.2f}")
    print(f"Sensitive flagged:           {n_sensitive}")
    print("Evidence by rule:")
    for rule, n in by_rule.items():
        print(f"  {rule}: {n}")
    print(f"Wrote: {OUT_PATH}")


if __name__ == "__main__":
    main()
