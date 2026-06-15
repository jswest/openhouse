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

## run-202606150610 (2026-06-15)

Third sweep, schema-8 corpus (years 2022–2025; 2020–2021 still schema-6, out of
scope). First run after v0.8.0 (#100/#128 + #130/#131/#133/#134). Tier-2 invariant
pass clean. Tier-1 scoped to the 10 richest **annual FDs** of the `0.01/seed-0`
draw (the high-signal tier); the 5 candidate-C novel batch was **skipped by
operator** — calibration already yielded more than enough confirmed findings. FP
rate ~0: Schiff verified against the live parser (on-disk record byte-identical to
current code output), so all findings are real, not stale-corpus artifacts.

**Scope & verdicts**
- Calibration: 10 annual FDs (2022–2025). 2 clean / 8 in-scope deviation / 0
  could_not_review.

**The headline: v0.8.0's column-content fixes did not generalize**
- **#100** (Schedule A row structure) — **HOLDS, robust.** No dropped assets /
  wrapped-`[TYPE]` merges across all 10, including large FDs (Waltz 59-row A,
  Williams 58) and prior regression cases (Sarbanes, Harris). Genuinely fixed.
- **#133** (Schedule H/J header-as-row) — **INCOMPLETE [worst].** Fails on every
  filing with real H/J data (Schiff/Harris/Waltz/Brownley H; Lopez J). Intact-letter
  header never matches suppression.
- **#131** (Schedule C Spouse bleed) — **INCOMPLETE.** Fails on open-vocabulary Type
  (Schiff "wage"/"distribution"; Waltz "Spouse salary").
- **#134** (Schedule D date leak) — **INCOMPLETE.** Fails on non-standard dates
  (Schiff "Various dates in 2022"; Waltz "Jan 2022").
- **#130** (appendix absorption) — **INCOMPLETE [narrow].** Fails only when the
  trailing schedule is populated (Schiff Schedule I); holds when empty/absent.
- **#132** (Schedule A two-income columns) — **CONFIRMED REAL**, relabeled from
  speculative. Moore A[9..12]: current-year "None" → income_type, preceding-year
  range → income_amount, income_preceding nulled. Trigger pinned.

**New root cause (not among the four)**
- **Schedule E/F structured-field splitter** — position/organization (and
  parties/terms) unsplit on a leading contiguous block of identical-position rows
  (Williams E[0..6]; also Harris, Sarbanes). #17 territory; 3 of 10 filings.

**Diagnosis & disposition**
- Opus 4.8 root-cause investigation traced all four to a shared two-layer structure
  in `openhouse/pdf.py` (Layer 1 segmentation `_segment_schedules`/`_FD_FURNITURE_RE`:
  #130, #133; Layer 2 trust-pattern-then-mis-split: #131, #134, #132). Posted to
  **#143** (new stub, may become an omnibus). Cross-cutting cause: prior fixes used
  synthetic fixtures; the failing real filings (10054507/10054295/10057260/10062886)
  were never in `tests/fixtures/`.
- Issues touched: **#143** created (stub + diagnosis); **#132** relabeled
  speculative→confirmed (title/body edited).

Per-filing detail in the git-ignored `reports/run-202606150610/`
(`calibration.jsonl` / `calibration.md`); diagnosis on #143.

## run-202606141629 (2026-06-14)

Second sweep, schema-v7 corpus (years 2022–2025; 2020–2021 are still v6 and out
of scope). Tier-2 invariant pass clean. Tier-1 calibration (10 richest annual
FDs) completed; the full sweep was **stopped early by operator decision after 10
of 78 efiled filings** (batches 1–2) to land #100 (PR #128) + the issues below,
re-parse at schema 8, and re-sweep. All 18 scanned/missing filings short-circuited
to `could_not_review`. **68 efiled filings (rest of PTRs, candidate `C` reports,
bodyless cover sheets) were not reviewed.**

**Scope & verdicts**
- Calibration: 10 filings (2022–2025 annual FDs). 1 clean / 9 in-scope deviations.
- Sweep (partial): 28 filings recorded — 10 efiled visually reviewed (5 clean / 5
  in-scope deviations) + 18 scanned/missing `could_not_review`. E-filed PTRs came
  through clean apart from one by-design text-layer artifact; e-filed annual FDs
  (schedule extraction) again carried in-scope deviations.
- Combined in-scope deviations (calibration + sweep): 14 filings.

**Distinct in-scope root causes** (each re-verified; corpus is post-#104/#115)
- **Empty trailing schedule absorbs the *Investment Vehicle Details* appendix →
  fabricated I/J rows** [HIGH, **novel**; 5 filings; sibling of closed #97 → **#130**].
- **Schedule C source|type: Spouse/SPOUSE/Pension-prefixed Type bleeds into Source**
  [MED, **incomplete fix of closed #101**; 2 filings → **#131**].
- **Schedule A income columns corrupted on two-income (candidate) forms** [MED–HIGH,
  **speculative / novel**, overlaps #100 — recheck post-#128 re-parse; 4 filings → **#132**].
- **Schedule H/J column-header emitted as a data row** [LOW, novel; 3 filings → **#133**].
- **Schedule D date_incurred "Month DD, YYYY" comma leaks into creditor** [LOW–MED,
  novel, distinct from closed #102; 2 filings → **#134**].
- **Schedule A wrapped-`[TYPE]` / `⇒` row merge → dropped assets** [HIGH, **known-open**;
  open #100 / PR #128; 8 filings — not re-filed].
- **Verbatim asset-string capital-glyph lowercasing** [LOW, **by design** — source
  text-layer artifact; filed #135 then closed; matches run-202606130927's prior
  by-design determination].

**Regressions / incomplete fixes flagged** — corpus is post-v0.6.3(#104)+v0.7.0(#115):
#101 (Schedule C) verified holding for "Professional Services" but **incomplete** for
Spouse/Pension-prefixed types (#131). #97 verified holding for *Asset Class Details*
but does not generalize to the *Investment Vehicle Details* appendix (#130).

**Orchestration note:** concurrent per-filing subagents rasterizing PDFs into a
shared temp dir clobbered each other in batch 1 (one agent saw another filing's
pages); fixed by giving each subagent a unique `render/<doc_id>/` dir. Worth folding
into the sweep method doc for future runs.

Issues filed this session (operator-authorized): #130, #131, #132 (speculative),
#133, #134; #135 filed then closed by-design. Per-filing detail in the git-ignored
`reports/run-202606141629/` (`sweep.jsonl` / `sweep.md` / `consolidated-issues.md`).

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
