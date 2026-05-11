# HD-41 — Texas House District 41 (Hidalgo County)

Project root for campaign-finance research on the 2026 race for Texas House District 41. ETL not started yet — this file is the planning brief.

## District background

- **Geography**: South Texas / Rio Grande Valley. Parts of Hidalgo County including McAllen, Mission, Pharr, Edinburg. ([Texas Tribune directory](https://www.texastribune.org/directory/districts/tx-house/41/))
- **Recent voting history**:
  - 2024 general: Robert "Bobby" Guerra (D) defeated John Guerra (R). At the top of the ticket, Trump carried HD-41 by ~1.6 pts — part of the broader RGV rightward shift — but down-ballot Dems still won. ([Ballotpedia](https://ballotpedia.org/Texas_House_of_Representatives_District_41), [Texas Tribune 2026-02-19](https://www.texastribune.org/2026/02/19/texas-house-district-41-election-2026-rio-grande-valley/))
  - 2022 general: Bobby Guerra (D) defeated John Guerra (R).
  - Demographics: majority Hispanic/Latino. Specific census numbers not pulled — flag if needed.
- **Why the seat is open**: Bobby Guerra (D-Mission) announced in 2025 he won't seek reelection after 13 years in the seat. He has endorsed Seby Haddad in the Dem primary. ([Texas Border Business](https://texasborderbusiness.com/high-stakes-battle-emerges-for-texas-house-district-41-after-guerra-steps-aside/))

## 2026 race

- **Primary**: March 3, 2026 — neither party hit 50% on either side, both went to runoffs.
- **Runoff**: **May 26, 2026** (17 days from today, 2026-05-09). This is the operative date for pulling current finance data.
- **General**: November 3, 2026. Trump-won district + open seat → flagged as a top GOP pickup target. ([Texas Tribune](https://www.texastribune.org/2026/02/19/texas-house-district-41-election-2026-rio-grande-valley/))

### Runoff candidates (4)

These are very likely the "four candidates" referenced in the kickoff prompt — confirm in open questions below.

**Democratic runoff**
1. **Victor "Seby" Haddad** — McAllen City Commissioner, District 5 (sitting); banker. Endorsed by retiring Rep. Bobby Guerra and several RGV state legislators. Identifies as "center, moderate Democrat." Voted in GOP primaries 2014–2022, switched to Dem primary in 2024 — opponents call this carpetbagging. ([Texas Border Business](https://texasborderbusiness.com/city-of-mcallen-commissioner-victor-seby-haddad-to-run-for-state-representative-district-41/), [Texas Tribune](https://www.texastribune.org/2026/02/19/texas-house-district-41-election-2026-rio-grande-valley/))
2. **Julio Salinas** — 26 y/o; Texas legislative staffer; co-chair, Texas Dem Party Hispanic Caucus; former leg. director for state Rep. Christina Morales. Endorsed by state Reps. Christina Morales and Lulu Flores. Platform: child-care tax credit, $15K teacher raise, drug-price caps. ([Texas Tribune](https://www.texastribune.org/2026/02/19/texas-house-district-41-election-2026-rio-grande-valley/))

**Republican runoff**
3. **Sergio J. Sánchez** — Former Hidalgo County felony prosecutor. Has previously voted in Dem primaries (used against him by GOP rivals). Stricter-abortion platform; high NRA rating. ([Texas Tribune](https://www.texastribune.org/2026/02/19/texas-house-district-41-election-2026-rio-grande-valley/))
4. **Gary Groves** — Hidalgo County GOP precinct chair / "Trump Train" rally organizer. Full-MAGA platform. Campaign signs vandalized in Feb 2026. ([Texas Tribune](https://www.texastribune.org/2026/02/19/texas-house-district-41-election-2026-rio-grande-valley/))

### Eliminated in primary (not in runoff)

- **Eric Holguín** (D) — Texas policy director, UnidosUS. Two-time prior candidate (HD-32 2020, TX-27 2018). ([Texas Tribune](https://www.texastribune.org/2026/02/19/texas-house-district-41-election-2026-rio-grande-valley/))
- **Sarah Sagredo-Hammond** (R) — HVAC company owner. Had reportedly never voted before running; accused of being secretly Dem. ([Texas Tribune](https://www.texastribune.org/2026/02/19/texas-house-district-41-election-2026-rio-grande-valley/))

## Data source landscape — TEC

State legislative races are regulated by the **Texas Ethics Commission**, not FEC. (FEC is federal-only.)

- **Bulk download (preferred)**: `https://prd.tecprd.ethicsefile.com/public/cf/public/TEC_CF_CSV.zip` — single zip, ~1 GB compressed / ~8.3 GB unzipped, **all electronically-filed reports back to 2000-07-01**, no API key, no rate limit. Confirmed working (the Austin project pulls from this URL). ([CSV layout](https://www.ethics.state.tx.us/data/search/cf/CFS-ReadMe.txt))
- **Schedules included** (per CFS-ReadMe.txt):
  - Schedules A/C — itemized **contributions** → `contribs_01..99.csv` shards (the 99-way split is by record-id range, not by filer)
  - Schedules F/G/H/I — **expenditures** → `expend_##.csv`
  - Cover sheets (totals) → `cover.csv`
  - `filers.csv` → master index of every filer (committees + candidates) with their `filerIdent` ID — this is the lookup table for finding our candidates
  - `spacs.csv`, `pledges.csv`, `loans.csv`, `debts.csv`, `final.csv`, etc.
- **Reporting cadence for state legislative candidates**:
  - Semi-annual (Jan 15, Jul 15)
  - **30-day pre-election** — for the May 26 runoff that's ~Apr 26
  - **8-day pre-election** — ~May 18
  - Daily reports for $1K+ contribs in the 8-day window
- **Refresh cadence for the bulk zip**: TEC regenerates regularly (no documented SLA, but in practice frequent). The zip has a single `last-modified` header — diff against existing copy to detect a refresh.
- **Quirks** (from existing Austin work):
  - Multi-shard contribs file — must concat or loop
  - `correction=X` rows mark superseded amendments; including them double-counts ~35%. **Filter on `infoOnlyFlag != 'Y'`** (or however the existing scraper handles it). [memory: feedback_data_quality.md]
  - First/last name fields can be reversed for some filers — name-matching needs fuzzy logic. [memory: feedback_data_quality.md]
  - Filer name is unstructured — match by `filerIdent` after one-time lookup, not by name on every join.

## Reuse from Austin / SA

The Austin project already has the full TEC pipeline built out. Files to reuse, not rewrite:

- `..\austin-finance-data\tec_data\TEC_CF_CSV.zip` — bulk dump dated **2026-04-10 17:15**. **Stale for runoff purposes**: it pre-dates the ~Apr 26 30-day pre-runoff reports and the ~May 18 8-day reports. We will need a fresh download.
- `..\austin-finance-data\texas_finance_scraper.py` — download + unzip + ingest into a SQLite table `texas_contributions_raw`. Parameterized via `TEC_SHARD` env var.
- `..\austin-finance-data\tec_full_ingest.py` — orchestrator that loops all 99 shards, commits between, logs to `tec_ingest_log.txt`.
- `..\austin-finance-data\tec_partisan_aggregate.py` — partisan-lean aggregation logic (may or may not be useful here).
- `..\austin-finance-data\tec_data\filers.csv` — 8.8 MB filer index. Search this for our 4 candidates' `filerIdent` once we know which committees they've registered.

**Are HD-41 candidates in the existing dump?** Likely partially:
- Haddad has been a McAllen commissioner since 2019 — his city COH filings probably exist locally but state-level HD-41 committee was registered after his Oct 2025 announcement.
- Salinas, Sánchez, Groves — all first-time candidates for state office. Their HD-41 committees were registered in late 2025 / early 2026, so any reports they've filed (semi-annual Jan 15 2026, 30-day pre-primary ~Feb 1 2026, 8-day pre-primary ~Feb 22 2026) **should** be in the Apr 10 dump. The runoff-specific 30-day and 8-day reports will not be.

**Recommended posture**: pull a fresh `TEC_CF_CSV.zip` into `HD-41/tec_data/` (don't reuse Austin's stale copy in place — keep projects independent), then point the existing scraper at it. Same code, fresh data, separate DB.

## Open questions for the user

1. **"All four candidates" — which four?** Best guess: the 4 runoff candidates (Haddad, Salinas, Sánchez, Groves). Confirm — or if you meant the 6 primary candidates including Holguín (D) and Sagredo-Hammond (R), say so.
2. **Time window**: through the **May 26 runoff** only, or all the way to **Nov 3 general**? Affects when we re-pull the bulk zip and whether we keep ingesting after May 26.
3. **Scope**: just **contributions in (Schedule A)**, or also **expenditures out (F/G/H/I)** and **loans/pledges**? Austin precedent has been contribs-only for the partisan-lean work.
4. **Cross-cycle**: include candidates' **prior-cycle** filings (Haddad's McAllen city COH reports, Salinas's staffer-era filings if any) for backstory, or strictly the HD-41 race?
5. **Pro-Israel / industry-affiliation overlay** like Austin & SA had (AIPAC/ADL/oil-gas/real-estate scoring)? That's a much bigger lift than just totals; flag now or skip.
6. **Cross-reference with the FEC partisan-lean scoring** from `~/.claude/projects/.../memory/project_fec_api.md`? Federal donors who also gave to HD-41 candidates might be interesting; not free to compute.

## Current state (2026-05-09)

Phase-1 raw ETL complete. Schedule A contributions for the **five candidates** (4 runoff + 1 primary loser, Holguin) are ingested into `hd41_finance.db`. No enrichment, no FEC cross-reference, no affiliation overlays — just clean raw data.

### Filer IDs (verified)

| Candidate | filerIdent | filerType | filing_start_date | Notes |
|---|---|---|---|---|
| Victor "Seby" Haddad (D) | `00090127` | COH | 2026-01-15 | new for HD-41 |
| Julio Mauricio Salinas (D) | `00090159` | COH | 2026-01-16 | new for HD-41 |
| Sergio Sanchez (R) | `00089992` | COH | 2026-01-15 | new for HD-41 |
| Gary Groves (R) | `00090204` | COH | 2026-01-13 | new for HD-41 |
| Eric Holguín (D) | `00083896` | COH | 2026-01-15 | **reused** from his 2020 HD-32 run; ingest filtered to `filerSeekOfficeDistrict='41'` |

Out-of-scope filers (kept as documentation, not ingested):
- Sergio Sanchez — separate `SCC` filer `00069847` (party-chair committee, McAllen)
- Eric Holguín — same filer `00083896` had **5 HD-32 reports** in 2020 (~$268K) plus 2 wrap-up reports (COHFR 2021, COHUC 2022). Excluded by `district_filter='41'` on cover-sheet `filerSeekOfficeDistrict`.

Note on the Holguín spelling: TEC stores the last name with the accented í (`Holguín`). My initial ASCII substring search missed him; widening to handle the accent revealed `00083896`.

### Per-candidate totals (active rows only — `info_only_flag != 'Y'`)

| Candidate | Reports active / superseded | Contrib rows | Active total | Cover declared | Δ | Date range |
|---|---|---|---|---|---|---|
| Haddad | 3 / 1 | 171 | $230,732.16 | $230,732.16 | $0 ✓ | 2025-10-05 → 2026-02-20 |
| Salinas | 3 / 1 | 354 | $134,170.48 | $134,170.48 | $0 ✓ | 2025-10-05 → 2026-02-21 |
| Holguín | 2 / 0 | 127 | $26,582.79 | $26,582.79 | $0 ✓ | 2025-10-01 → 2026-01-22 |
| Groves | 3 / 1 | 33 | $20,448.70 | $20,448.70 | $0 ✓ | 2025-10-24 → 2026-02-18 |
| Sanchez | 3 / 0 | 11 | $5,725.00 | $5,725.00 | $0 ✓ | 2025-10-22 → 2026-02-21 |

All five reconcile to TEC's declared cover-sheet totals to the penny.

**Holguín filing pattern is incomplete**: only 2 reports for the HD-41 cycle (Jul-Dec 2025 semi-annual + 30-day pre-primary). **No 8-day pre-primary report on file** — the other four candidates all filed one. Likely explanation: his campaign was effectively over by the 8-day deadline (Feb 22) since he finished 3rd in the Dem primary on March 3 and has no need to file forward-looking runoff reports. Last contribution date is 2026-01-22, the end of his 30-day-pre-primary period.

### Reports filed

The four runoff candidates each filed three reports for the primary cycle:
1. Jul-Dec 2025 semi-annual (filed ~Jan 13-16 2026)
2. 30-day pre-primary, period Jan 1-22 (filed ~Feb 1-9 2026)
3. 8-day pre-primary, period Jan 23-Feb 21 (filed ~Feb 22-23 2026)

Holguín filed only 1 + 2 — no 8-day pre-primary, consistent with his campaign winding down before that deadline.

Where a `CORCOH` correction was filed, the original is marked `infoOnlyFlag='Y'` and stays in the data (audit trail). The corrected version replaces it. Salinas corrected his semi-annual; Haddad and Groves corrected their 30-day pre-primary.

**Runoff reports not yet filed** — the next deadline is the 30-day pre-runoff (~Apr 26 — already passed but bulk dump may not yet contain it; today's pull is dated 2026-05-09 04:11). Re-pull the bulk zip after May 18 to capture 30-day + 8-day pre-runoff reports.

### Data files

- `tec_data/TEC_CF_CSV.zip` — fresh bulk download, dated **2026-05-09 04:11** (1.02 GB)
- `tec_data/cover.csv`, `filers.csv`, `CFS-ReadMe.txt` — extracted for direct query
- `hd41_finance.db` — SQLite, 434 KB, 4 tables (see schema below)

### Scripts

- `find_filers.py` — searches `filers.csv` for the four candidates, prints plausible matches with district/office/zip context
- `sanity_check.py` — exploratory script that walks every shard, computes per-candidate totals, top donors, and reconciles against cover.csv. Output saved to `sanity_check_output.txt`
- `pull_hd41_contributions.py` — production extractor. CLI: `--reset` (drop+rebuild), `--report` (print summary only). Idempotent: re-running against a refreshed bulk zip is safe.

### Schema (`hd41_finance.db`)

```
candidates         (candidate_slug PK, full_name, party, filer_ident, filing_start_date, notes)
candidate_filers   (PK candidate_slug+filer_ident, filer_type_cd, filer_name, role)
reports            (report_info_ident PK, candidate_slug, filer_ident, form_type_cd,
                    report_type_cd, period_start_dt, period_end_dt, filed_dt, received_dt,
                    election_dt, election_type_cd, total_contrib_amount,
                    unitemized_contrib_amount, info_only_flag, no_activity_flag)
contributions      (contribution_info_id PK, report_info_ident FK, candidate_slug FK, filer_ident,
                    form_type_cd, sched_form_type_cd, received_dt, info_only_flag,
                    contribution_dt, contribution_amount, contribution_descr, itemize_flag,
                    travel_flag,
                    contributor_persent_type, contributor_name_org/last/first/suffix/prefix,
                    contributor_street_city/state/zip/county/country,
                    contributor_employer, contributor_occupation, contributor_job_title,
                    contributor_pac_fein, contributor_oos_pac_flag, contributor_law_firm_name,
                    canonical_name, canonical_zip5, content_hash,
                    source_csv, ingested_at)
```

Primary key choice: `contributionInfoId` (TEC's stable per-row ID). Re-ingest is idempotent. `content_hash` is stored as a secondary index for cross-source dedup later, **not** as the primary key — it overcollapses real same-day same-amount paired donations (e.g. Mr. + Mrs. at same household). Verified collapse: 4 rows for Salinas ($800), 1 row for Sanchez ($125). Acceptable for downstream cross-reference, dangerous as a within-source PK.

### Reconciliation notes

- TEC `cover.totalContribAmount` is the filer's declared total contribs for the period — this includes unitemized small donations not in `contribs_##.csv`. For our 4 filers, `unitemizedContribAmount = 0` on every report, so cover total == contribs sum exactly. (Tiny RGV-scale donor pools; nobody's hitting the unitemization break.)
- `reportTypeCd` is empty on every cover row in this dataset. Use `formTypeCd` (`COH` vs `CORCOH`) to distinguish original vs corrected.
- `schedFormTypeCd` mix: A1 = monetary (vast majority), A2 = non-monetary / in-kind (49 rows total across all 4 candidates, mostly Salinas). Both are Schedule A and both are included.

### Data flags / candidates worth a second look

- **Sanchez** — just **$5,725 across 11 itemized rows**. Boilerplate small-dollar donor list. Charles Banker ($1,000, Houston) is the only out-of-RGV donor of note. This is genuinely tiny for a state-house primary that pushed to a runoff. `unitemizedContribAmount=0` everywhere, so it's not unitemized cash; either he's self-funding via Schedule E loans (out of scope) or running near-empty. Worth confirming via news coverage before drawing patterns.
- **Groves** — $20,449 / 33 donors is small but more typical for an early-stage RGV challenger. Boggus ($5,000, Harlingen) is the only donor over $3K.
- **Holguín** — $26,583 / 127 rows is mid-pack and his donor list is the most diverse of the five (small-dollar tilt, lots of out-of-state activist donors consistent with a UnidosUS-network candidate). Filing pattern incomplete (no 8-day pre-primary).

### What's NOT done (next phases)

- Loans (Schedule E), expenditures (Schedules F/G/H/I), pledges (Schedule B) — out of scope per current spec
- Affiliation overlay (AIPAC/ADL/oil-gas/real-estate scoring like Austin/SA) — explicitly deferred
- FEC cross-reference for federal-donor partisan-lean — explicitly deferred
- Runoff-period reports (30-day pre-runoff ~Apr 26, 8-day pre-runoff ~May 18) — not yet in bulk dump dated 2026-05-09; re-pull after May 18
- Frontend / profile pages
