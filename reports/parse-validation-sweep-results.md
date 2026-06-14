# Parse-validation sweep — results log

A running, hand-maintained record of what each
[parse-validation sweep](../docs/parse-validation-sweep.md) found. The per-run
artifacts live under a git-ignored `reports/run-<YYYYMMDDHHMM>/` directory
(regenerable); this file is the durable, tracked summary. **Append a new
`run-<YYYYMMDDHHMM>` section after every run** — newest first. Keep each entry
brief: scope, verdict roll-up, and the distinct *root-cause* issues (not
per-example), with novel / known-open / regression status and any referenced GH
issue.

---

## run-202606130927 (2026-06-13)

First sweep — pre-nomenclature, flat-file artifacts
(`inspect-sample-2026-06-13.*`, `parse-validation-full-2026-06-13.*`). Run-id
back-derived from the session's earliest transcript (09:27). Calibration batch
(10 filings) then a fuller pass (62 filings, 2020–2024). E-filed PTRs came through clean; e-filed annual FDs (schedule
extraction) were systematically buggy; scanned filings correctly deferred
(`body: null`, no OCR pre-v1).

**Scope & verdicts**
- Calibration: 10 filings (2020–2021). 2 clean / 3 out-of-scope-only / 5 with
  in-scope deviations.
- Full: 62 filings (2020–2024); ~35 candidate reports + 44 bodyless filings
  deliberately skipped. 23 match / 12 out-of-scope-only / 27 with in-scope
  deviations. E-filed PTRs: 25 filings, 1 low-sev finding. E-filed FDs: 26
  filings, 25 with deviations.

**Distinct in-scope root causes**
- **Empty trailing schedule absorbs the Asset Class Details appendix** →
  fabricated rows on Sched I/J [HIGH, novel; ~9 filings; related #17].
- **Schedule A amount-range high/low swapped** → inverted dollar buckets [HIGH,
  **regression of #70** + novel; ~6 filings].
- **Schedule A row merge / subholding (`=>`) collapse / dropped assets** [HIGH,
  **regression of #70** + novel; ~9 filings].
- **Schedule A value/income/owner columns bleed into asset name** [MED/HIGH,
  novel; ~6 filings; related #70].
- **Schedule A wrapped 2-line dollar cell loses bucket high bound** [HIGH,
  novel].
- **Candidate Schedule A 3-income-column shift when Current-Year = 'None'**
  [HIGH, novel; related #70].
- **Schedule C earned-income Source|Type column boundary mis-split** [MED,
  novel; ~3 filings; related #12].
- **Schedule D wrapped multi-line cell → `amount_range` null** [HIGH, novel;
  ~10 filings; related #70/#12].
- **Structured schedules E–J mis-parsed on real layouts** [MED, novel; related
  #17].
- **Asset-name text-layer casing preserved verbatim** [LOW, by design; 1 filing].

**Regressions flagged** — 5 filings show wrapped-`[TYPE]` row merges / amount
swaps after #70 / PR #72 claimed "0 Schedule A collapses": Pascrell 2021
(10046898), Suozzi 2021 (10048000), Sarbanes 2022 (10052884), Harris 2022
(10054295), Pou 2024 (10068928).

Root causes consolidated into 8 filable issue drafts (`consolidated-issues-2026-06-13.md`).
