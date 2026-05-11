"""Scan HD-41 donors for broader Israel-aligned PAC affiliations (excluding AIPAC + ADL).

Writes findings_zionist_general.json. Read-only against hd41_finance.db.

Pattern-matches `fec_committee_cache.committee_name` (case-insensitive). Known
Israel-aligned PAC patterns: NORPAC, JACPAC, Pro-Israel America, Democratic
Majority for Israel (DMFI), Republican Jewish Coalition (RJC), Jewish Coalition,
American Israel..., Christians United for Israel (CUFI), J Street, If Not Now,
plus the broad combination test "Israel" AND ("PAC"|"Committee"|"Action Fund").

AIPAC (incl. United Democracy Project) and ADL Action are explicitly excluded
since they live in their own categories.
"""

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

DB_PATH = r"C:\Users\Hamza Sait\Electoral\HD-41\hd41_finance.db"
OUT_PATH = r"C:\Users\Hamza Sait\Electoral\HD-41\findings_zionist_general.json"

# Per-pattern label assignments. The first pattern that matches a committee
# name wins. Order matters: more specific patterns go first.
NAME_PATTERNS = [
    (re.compile(r"\bNORPAC\b", re.IGNORECASE), "NORPAC"),
    (re.compile(r"\bJACPAC\b", re.IGNORECASE), "JACPAC"),
    (re.compile(r"\bPro.?Israel America\b", re.IGNORECASE), "Pro-Israel America"),
    (re.compile(r"\bDemocratic Majority for Israel\b", re.IGNORECASE), "Democratic Majority for Israel"),
    (re.compile(r"\bDMFI\b", re.IGNORECASE), "Democratic Majority for Israel"),
    (re.compile(r"\bRepublican Jewish Coalition\b", re.IGNORECASE), "Republican Jewish Coalition"),
    (re.compile(r"\bRJC\b", re.IGNORECASE), "Republican Jewish Coalition"),
    (re.compile(r"\bChristians United for Israel\b", re.IGNORECASE), "Christians United for Israel"),
    (re.compile(r"\bCUFI Action Fund\b", re.IGNORECASE), "Christians United for Israel"),
    (re.compile(r"\bJ Street\b", re.IGNORECASE), "J Street"),
    (re.compile(r"\bIf Not Now\b", re.IGNORECASE), "If Not Now"),
    (re.compile(r"\bAmerican Israel\b", re.IGNORECASE), "American Israel-aligned PAC"),
    (re.compile(r"\bJewish Coalition\b", re.IGNORECASE), "Jewish Coalition"),
]

# Broader combined keyword test on committee name: must contain Israel AND
# (PAC|Committee|Action Fund). Used as a catch-all when no per-pattern hit.
ISRAEL_RE = re.compile(r"\bIsrael\b", re.IGNORECASE)
PAC_RE = re.compile(r"\b(PAC|Committee|Action Fund)\b", re.IGNORECASE)

# Explicit exclusions — these live in other categories.
EXCLUDE_RES = [
    re.compile(r"\bAIPAC\b", re.IGNORECASE),
    re.compile(r"\bUnited Democracy Project\b", re.IGNORECASE),
    re.compile(r"\bAnti.?Defamation League\b", re.IGNORECASE),
    re.compile(r"\bADL Action\b", re.IGNORECASE),
    # Bare "ADL" in a PAC name — but be careful not to swallow unrelated acronyms.
    # Most committees with ADL in the name are ADL-related. Filter conservatively.
    re.compile(r"\bADL\b", re.IGNORECASE),
]

# Labels considered "left-wing pro-Israel" for cross-alignment sensitivity.
LEFT_LABELS = {"J Street"}
# Labels considered "right-wing pro-Israel" for cross-alignment sensitivity.
RIGHT_LABELS = {
    "Republican Jewish Coalition",
    "Christians United for Israel",
    "NORPAC",
}


def is_excluded(name: str) -> bool:
    return any(p.search(name) for p in EXCLUDE_RES)


def classify_committee(committee_name: str | None):
    """Return label if committee qualifies as zionist_general, else None."""
    if not committee_name:
        return None
    if is_excluded(committee_name):
        return None
    for pat, label in NAME_PATTERNS:
        if pat.search(committee_name):
            return label
    if ISRAEL_RE.search(committee_name) and PAC_RE.search(committee_name):
        return "Israel-aligned PAC (generic)"
    return None


def main():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # 1. Sweep fec_committee_cache for Israel-aligned committees.
    qualifying = {}  # committee_id -> (label, committee_name)
    all_committees = cur.execute(
        "SELECT committee_id, committee_name FROM fec_committee_cache"
    ).fetchall()
    for r in all_committees:
        cid = r["committee_id"]
        cname = r["committee_name"] or ""
        label = classify_committee(cname)
        if label:
            qualifying[cid] = (label, cname)

    print(f"Qualifying Israel-aligned committees in cache: {len(qualifying)}")
    for cid, (label, cname) in sorted(qualifying.items(), key=lambda x: x[1][0]):
        print(f"  {cid} -> {label!r} name={cname!r}")

    if not qualifying:
        payload = build_payload([])
        write_output(payload)
        print_summary(payload)
        return

    # 2. Pull all fec_contributions_raw rows for these committees.
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

    print(f"FEC contribution rows to qualifying committees: {len(rows)}")

    # 3. Group by (donor_id, label).
    grouped = defaultdict(list)  # (donor_id, label) -> list of evidence dicts
    donor_label_set = defaultdict(set)  # donor_id -> set of labels they gave to
    donor_total = defaultdict(float)  # donor_id -> total $ across all zionist_general giving
    for r in rows:
        cid = r["committee_id"]
        label, cname = qualifying[cid]
        amount = float(r["contribution_amount"] or 0.0)
        date = r["contribution_date"] or ""
        evidence = {
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
        grouped[(r["donor_id"], label)].append(evidence)
        donor_label_set[r["donor_id"]].add(label)
        donor_total[r["donor_id"]] += amount

    # 4. For each (donor, label) build a finding entry.
    findings = []
    for (donor_id, label), evs in grouped.items():
        total_amount = sum(e["amount"] for e in evs)
        dates = [e["date"] for e in evs if e["date"]]
        first_seen = min(dates) if dates else None
        last_seen = max(dates) if dates else None

        ident = cur.execute(
            "SELECT canonical_name, total_donated FROM donor_identities WHERE donor_id = ?",
            (donor_id,),
        ).fetchone()
        hd41_total = float(ident["total_donated"]) if ident and ident["total_donated"] is not None else 0.0
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
        cand_str = ", ".join(cand_parts) if cand_parts else "(no HD-41 contributions found)"

        # Cross-PAC observation: list other zionist_general labels this donor gave to.
        other_labels = sorted(donor_label_set[donor_id] - {label})
        cross_note = ""
        if other_labels:
            cross_note = f" Also gave to: {', '.join(other_labels)}."

        donor_combined = donor_total[donor_id]
        gave_left = bool(donor_label_set[donor_id] & LEFT_LABELS)
        gave_right = bool(donor_label_set[donor_id] & RIGHT_LABELS)
        cross_aligned = gave_left and gave_right

        # Sensitive flag per spec.
        sensitive = (
            donor_combined > 5000.0
            or cross_aligned
            or label == "If Not Now"
        )

        n = len(evs)
        notes = (
            f"{n} contribution{'s' if n != 1 else ''} to {label} "
            f"(${total_amount:,.2f}); donor {canonical_name}; "
            f"HD-41 backing: {cand_str}; "
            f"combined Israel-aligned giving across all PACs: ${donor_combined:,.2f}."
            + cross_note
            + (" Cross-aligned: gave to BOTH left- and right-wing pro-Israel PACs." if cross_aligned else "")
        )

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

    findings.sort(key=lambda f: (f["label"], -f["total_amount"], f["donor_id"]))

    payload = build_payload(findings)
    write_output(payload)
    print_summary(payload, qualifying)


def build_payload(findings):
    return {
        "category": "zionist_general",
        "rules": [
            {
                "name": "fec_committee_name_match",
                "description": (
                    "Donor gave to an FEC committee whose name matches a known "
                    "Israel-aligned PAC pattern (NORPAC, JACPAC, Pro-Israel "
                    "America, Democratic Majority for Israel / DMFI, Republican "
                    "Jewish Coalition / RJC, Jewish Coalition, American Israel..., "
                    "Christians United for Israel / CUFI, J Street, If Not Now), "
                    "or contains both 'Israel' and ('PAC'|'Committee'|'Action Fund'). "
                    "AIPAC (incl. United Democracy Project) and ADL Action are excluded."
                ),
            },
        ],
        "findings": findings,
    }


def write_output(payload):
    Path(OUT_PATH).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def print_summary(payload, qualifying=None):
    findings = payload["findings"]
    n_findings = len(findings)
    n_evidence = sum(len(f["evidence"]) for f in findings)
    total_impl = sum(f["total_amount"] for f in findings)
    n_sensitive = sum(1 for f in findings if f["sensitive"])
    unique_donors = len({f["donor_id"] for f in findings})

    by_label = defaultdict(lambda: {"donors": set(), "amount": 0.0, "evidence": 0})
    for f in findings:
        by_label[f["label"]]["donors"].add(f["donor_id"])
        by_label[f["label"]]["amount"] += f["total_amount"]
        by_label[f["label"]]["evidence"] += len(f["evidence"])

    print("---- SUMMARY ----")
    print(f"Committees searched (qualifying):  {len(qualifying) if qualifying else 0}")
    print(f"Findings (donor,label rows):       {n_findings}")
    print(f"Unique donors implicated:          {unique_donors}")
    print(f"Evidence rows:                     {n_evidence}")
    print(f"Total $ implicated:                ${total_impl:,.2f}")
    print(f"Sensitive flagged:                 {n_sensitive}")
    print("By label:")
    for label, agg in sorted(by_label.items()):
        print(
            f"  {label}: donors={len(agg['donors'])} "
            f"evidence={agg['evidence']} total=${agg['amount']:,.2f}"
        )
    print(f"Wrote: {OUT_PATH}")


if __name__ == "__main__":
    main()
