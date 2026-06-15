# GH-0134 — Schedule D `Month DD, YYYY` Date incurred consumed whole, not leaked into Creditor

**Date:** 2026-06-14
**Issue:** #134 (part of omnibus #136 / v0.8.0)

## Context

Schedule D (liabilities) columns are `Owner | Creditor | Date incurred | Type |
Amount`. `_FD_DATE_RE` both anchors a row's item-start and extracts the date, so
the two always agree. It matched three date shapes — `Month YYYY`,
`MM/DD/YYYY`, `MM/YYYY` — but **not** `Month DD, YYYY`.

When a filer wrote the Date incurred as `Month DD, YYYY` (e.g. `April 15,
2019`), this was a silently-plausible corruption distinct from closed #102's
wrapped-amount loss:

- `_FD_DATE_RE`'s month branch is `Month\s+\d{4}`, which cannot match the
  `Month DD` head (`April 15` is not `Month` + 4 digits), and the slash branches
  don't apply. So the fuller-date regex failed entirely.
- The row still anchored via the bare-year fallback (`_FD_D_BARE_YEAR_RE`),
  which matched only the trailing `2019`. `date_m.start()` therefore pointed at
  the year, and the creditor slice `raw[start:date_m.start()]` ran up to it —
  carrying the `April 15,` fragment into `creditor` (`Navient April 15,`), with
  only `2019` surviving in `date_incurred`.

The amount was already correct (that is #102's territory, and stays so). Verified
against the issue's cited rows: 10063197 (D[1]) and 10057260 (D[5]).

## Decision

Smallest fix that fits — no schema change, no new abstraction, riding the single
existing regex:

In `openhouse/pdf.py`, extend `_FD_DATE_RE`'s month branch from
`(?:January…December)\s+\d{4}` to
`(?:January…December)\s+(?:\d{1,2},\s+)?\d{4}` — an optional `\d{1,2},`
day-comma group between month and year. With the group present the
`Month DD, YYYY` form is matched (and thus consumed) whole, comma included;
absent, the comma-less `Month YYYY` form still matches exactly as before. The
regex engine tries the optional group before skipping it, so the longer comma
form is preferred — same precedence the "longest-first" ordering of the branch
list already guarantees, and the month branch still precedes the slash forms.

Because the date is now consumed whole, the creditor slice ends *before* the
month (`creditor` = `Navient`, clean) and the date slice carries the full
`April 15, 2019` into `date_incurred`. No other code path uses `_FD_DATE_RE`
(grep-confirmed: only `_parse_schedule_d`'s `starts()` anchor and its date
extraction), so the blast radius is confined to Schedule D.

This change is purely additive to the date grammar: it does not touch #128's
Schedule-A anchoring/residual, #130's appendix termination, #131's Schedule C
split, #133's H/J header skip, or #102's wrapped-Type amount handling
(`_fd_d_amount`), all of which stay green.

## Verification

- New value-asserting tests in `tests/test_fd_extraction.py` reproduce the named
  rows synthetically (the #97/#101 convention — the filings are not checked into
  `tests/fixtures/`) and assert the CORRECT `creditor` AND `date_incurred`:
  - `test_schedule_d_month_day_year_date_consumed_whole`:
    - 10063197 D[1]: `Navient` → `April 15, 2019` (type `Student Loan`,
      amount `$15,001 - $50,000`).
    - 10057260 D[5]: `Wells Fargo` → `January 1, 2020` (type `Mortgage`,
      amount `$250,001 - $500,000`).
    - Control `Old National Bank` → `January 2016` confirms the comma-less form
      is unaffected.
  - `test_schedule_d_month_day_year_with_wrapped_amount`: the `Month DD, YYYY`
    date coexists with #102's wrapped-Type amount recovery — `BB&T Bank` →
    `April 15, 2019`, type rejoined to `Mortgage on Rental Property, Washington,
    DC`, amount recovered to `$100,001 - $250,000`.
- The closed-#102 tests (`test_schedule_d_wrapped_type_amount_not_lost`,
  `test_schedule_d_present_but_unparseable_amount_stays_visible`) and the #70
  bare-year tests still pass unchanged.
- Full suite: 443 passed.
