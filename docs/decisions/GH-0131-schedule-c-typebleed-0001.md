# GH-0131 — Schedule C Spouse/SPOUSE/Pension-prefixed Type stops bleeding into Source

**Date:** 2026-06-14
**Issue:** #131 (part of omnibus #136 / v0.8.0)

## Context

Schedule C (earned income) rows are one physical line: `Source | Type | Amount`.
The Type column is multi-word on the live form and folds the owner column in
front of it (`Spouse Pension`, `Member Retirement Plan`). Closed #101 fixed this
by matching the Type from a small closed vocabulary (`_FD_C_TYPE_PHRASES`),
optionally prefixed by an owner token, anchored to the line's tail
(`_FD_C_TYPE_RE`) — falling back to a single-token split for an unknown Type so
the row still divides.

#101 was incomplete. Two prefixed-Type shapes still bled their leading token into
`source` and truncated `income_type` — a silently-plausible corruption (the row
parses, but with a spurious token in `source` and a clipped `income_type`):

- **All-caps owner token (`SPOUSE`).** The owner alternation listed only
  `Spouse`; the regex is not `IGNORECASE` (and must not be — that would let a
  Type phrase match anywhere in the source). So `Acme Industries SPOUSE Salary`
  fell through to the last-token split: `source="Acme Industries SPOUSE"`,
  `income_type="Salary"`.
- **A Type that begins with `Pension` and runs two words (`Pension Plan`).**
  `Pension Plan` was not a vocabulary phrase; only `Pension` was. So
  `Teachers Retirement System Pension Plan` split on the last space:
  `source="... Pension"`, `income_type="Plan"`.

Verified on the issue's cited filings 10068086 (5 rows) and 10057260.

## Decision

Smallest fix that fits, riding #101's existing mechanism — no schema change, no
new abstraction:

In `openhouse/pdf.py`:

- Added `"Pension Plan"` to `_FD_C_TYPE_PHRASES`, placed **before** `"Pension"`
  so the longest-first / greedy-correct invariant from #101 holds (a `Pension
  Plan` Type matches whole, never clipped to `Pension`).
- Added `SPOUSE` to the owner-token alternation in `_FD_C_TYPE_RE`
  (`Member|Spouse|SPOUSE|SP|DC|JT`). Both casings render on the live form; an
  explicit all-caps variant is the targeted fix that avoids `re.IGNORECASE`
  (which would over-match phrases buried in the source name).
- Updated the vocabulary comment to record `Pension Plan`'s provenance
  (GH-0131's 10068086 / 10057260) and its greedy-ordering note, keeping the
  closed set self-documenting.

An unknown Type still falls back to the single-token split and `raw_text` still
carries the whole row verbatim — never dropped. This change is confined to the
Schedule C split and does not touch #128's Schedule-A wrapped-`[TYPE]`/`⇒`
anchoring or `schedule_incomplete` residual, nor #130's empty-trailing-schedule
appendix termination.

## Verification

- New value-asserting test in `tests/test_fd_extraction.py`
  (`test_schedule_c_spouse_pension_prefixed_type_does_not_bleed_into_source`)
  reproduces the named filings' row shapes synthetically (the #101/#97
  convention — the filings are not checked into `tests/fixtures/`) and asserts
  the CORRECT `source` AND `income_type` for each row:
  - 10068086 (5 rows): `Acme Industries` → `SPOUSE Salary`;
    `Teachers Retirement System` → `Pension Plan`;
    plus the three #101 shapes kept green in the same call
    (`State of Mississippi` → `Member Retirement Plan`,
    `AXA Equitable Annuity` → `Spouse Annuity Plan`,
    `Consulting LLC` → `Professional Services`).
  - 10057260: `Northern Trust Company` → `SPOUSE Pension`.
- The closed-#101 tests
  (`test_schedule_c_multiword_type_does_not_bleed_into_source`,
  `test_schedule_c_unknown_multiword_type_degrades_safely`) still pass unchanged.
- Full suite: 439 passed.
