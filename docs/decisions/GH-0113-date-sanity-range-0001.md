# GH-0113 — parse-time date sanity range; flag in place, preserve raw

**Date:** 2026-06-14
**Issue:** #113 (part of #115, openhouse v0.7.0)

## Context

Every disclosure-date parse was a bare
`datetime.strptime(raw, "%m/%d/%Y").date()` with no plausibility check, at three
sites: PTR `transaction_date` / `notification_date` (`pdf.py`), FD Schedule B
`transaction_date` (`pdf.py`), and the filing `filing_date` (`index.py`). A
transposed-digit year from PDF extraction (`3031`, `2220`, `2202` — all observed
in a 2020–2025 field-report session) parses exactly as readily as `2024`, so an
impossible date emitted as a **valid** `date` with `parse_status: ok`, no flag,
no residual. For a temporal-analysis tool ("when did the trade happen relative to
X") an undetectable garbage date is worse than a dropped one — it violates
*never silently drop / preserve raw, flag the anomaly* (CLAUDE.md).

## Decision

### Sanity range: `1990 ≤ year ≤ entry_year + 1`, entry year threaded down

A single helper `parse_disclosure_date(raw, *, max_year)` in `pdf.py` parses the
`M/D/YYYY` string and returns the `date` **only** when its year falls in
`MIN_DISCLOSURE_YEAR (1990) … max_year`; otherwise `None`. All three sites call
it, so the range is applied consistently.

The **upper bound is data, not wall-clock** (CLAUDE.md: no `date.today()` /
`datetime.now()` in core logic — one timestamp captured at command entry). The
existing `current_year` captured in `cli.main` is threaded as a new `entry_year`
argument: `cli.main → parse(entry_year=current_year) → parse_year → max_year =
entry_year + 1`, then passed to `build_filing_records(max_year=…)` and
`_classify_records(max_year=…) → extract_ptr_transactions(max_year=…)` /
`extract_fd_schedules(max_year=…)`. Schedule B alone among the FD parsers carries
a date, so `extract_fd_schedules` passes `max_year` to `_parse_schedule_b` only.
The `+ 1` tolerates a legitimately next-year notification/filing date.

A static `FALLBACK_MAX_YEAR = 2100` is the **default** on the leaf functions
(`extract_*`, `build_filing_records`, `_parse_filing_date`, `_classify_records`)
so direct API/test callers and the date-agnostic `pull.doc_ids_for_member` path
don't have to thread it. It is a fixed constant, never a wall-clock read; the
production `parse` path always passes the real `entry_year + 1` explicitly, so
the fallback never weakens production behavior. (Director's parked-question
option B — a static fallback — taken for the leaf boundary; the production thread
is the real entry-year bound.)

### Anomaly representation: flag in place, raw preserved, residual entry

A rejected date is **never** emitted as a valid `date`. Instead:

- the structured field is `None`, and
- the verbatim string is kept in a sibling `*_raw` field, set **only** on
  rejection (`None` on every sound row): `date_raw` / `notification_date_raw` on
  `PtrTransaction`, `transaction_date_raw` on `ScheduleBItem`. A set `*_raw` is
  the per-row anomaly flag.
- `PtrTransaction.transaction_date` / `notification_date` become `Optional` (they
  were non-nullable `date`).
- The filing is **not dropped**: `_classify_records` scans the produced body for
  any set `*_raw` and, if found, adds an `unparsed-manifest.json` entry with
  reason `date_out_of_range` while the body is still written and the filing stays
  `parse_status: ok`. A single bad date in a multi-transaction filing thus
  surfaces a residual without discarding the good rows (CLAUDE.md). The filing
  `filing_date` simply degrades to `None` (it was already `Optional`; the index
  has no per-row residual channel, and a null filing date is the existing
  "absent" representation).

### No schema-version bump

`SCHEMA_VERSION` is already **7** on this branch's base (GH-0114 owns the 6→7
bump for the bundle). The new `*_raw` date fields and the nullability change fold
into generation 7; the generation-7 docstring note in `schemas.py` is extended.
`openhouse/schemas.fingerprint` was refreshed to the new live model fingerprint
so the GH-0043 drift guard stays green (the guard test asserts committed ==
live).

## Tests (all offline, fixtures only)

- `test_ptr_extraction.py::test_transposed_year_transaction_date_is_flagged_not_accepted`:
  a synthetic PTR row with `04/30/3031` / `05/02/3031` extracts with both
  structured dates `None`, both `*_raw` preserved, and asset/ticker/type/amount
  intact (`max_year=2026`).
- `test_ptr_extraction.py::test_near_future_typo_year_is_flagged_but_in_range_year_kept`:
  a `2220` near-future typo on one row is rejected while a sound `2023` date on
  another row in the same body is kept — per-date, never a whole-filing drop.
- `test_ptr_extraction.py::test_out_of_range_date_records_residual_without_dropping_filing`:
  end-to-end through `_classify_records` — the body is written, the filing stays
  `ok`, and an `unparsed` entry with reason `date_out_of_range` accounts for it.
- `test_fd_extraction.py::test_schedule_b_transposed_year_date_is_flagged_not_accepted`:
  a Schedule B row with a `2202` Date column — structured date `None`,
  `transaction_date_raw` preserved, the rest of the row intact.
- `test_index.py::test_filing_date_sanity_range_rejects_impossible_year`:
  `_parse_filing_date` keeps an in-range date, returns `None` for a transposed
  year and a sub-1990 year, and `None` for empty.

Full suite: **407 passed**.

## Consequences

- A corpus re-parse from `raw/` recovers the bad dates as flagged anomalies; the
  re-parse is already forced by the bundle's generation-7 bump.
- Existing hand-authored parsed fixtures stay valid: the `*_raw` date fields are
  `Optional` (default `None`) and the `transaction_date` nullability only widens
  the type.
- `read` consumers that key on `transaction_date` now see `null` (not a bogus
  year-3031 date) on an anomalous row; the raw string is available beside it for
  a human to inspect.
