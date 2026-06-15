# GH-0133 — Schedule H/J column-header (NUL rendering) no longer leaks as a phantom data row

**Date:** 2026-06-14
**Issue:** #133 (part of omnibus #136 / v0.8.0)

## Context

Schedules H (travel) and J (new-filer compensation) are raw_text-only schedules
by design (#17): their columns merge with no stable delimiter, so real rows
degrade to `raw_text` (H additionally splits off `source`/`dates`; J has no
parser at all and keeps the whole row verbatim). The per-page column-header line
that prints above the rows is form furniture, dropped in `_segment_schedules`
via `_FD_FURNITURE_RE` so the column parsers never see it.

`_FD_FURNITURE_RE` matched only the **intact-letter** rendering of those headers
(`Source Date` for H's `Source Dates Location Items`, `Source Description` for
J's `Source Description of Duties`). But per SPEC §2.2 the dominant 2021-onward
annual-FD rendering is **glyphs-lost (NUL)**: small-caps furniture words extract
as one `U+0000` per glyph. In that rendering the H/J header becomes
`S\x00+ D\x00+ L\x00+ I\x00+` / `S\x00+ D\x00+ of D\x00+` — the intact-letter
branches miss it, the header survives segmentation, and (carrying no date for H
to anchor on / no parser for J) it is salvaged into a phantom raw_text row with
all structured fields null. A fabrication: a record the filer never wrote.

This is the `Structured schedules E–J mis-parsed on real layouts` root cause from
the parse-validation sweep, observed on NUL-rendered filings 10054295 (H),
10059679 (H) and 10061936 (J).

## Decision

Smallest fix that fits — extend the existing furniture recognizer, no schema
change, no new abstraction:

In `openhouse/pdf.py`, add one branch to `_FD_FURNITURE_RE`:
`S\x00+\s+D\x00+`. Both H and J glyphless headers lead with the `Source` column
(`S\x00+`) followed by a `D`-initial second column (`Dates` / `Description`
→ `D\x00+`); anchoring on that pair matches both and nothing else:

- The `S\x00+ A:` schedule heading is consumed by `_FD_HEADING_RE` **before** the
  furniture check, so it cannot reach this branch.
- The `S\x00+ A \x00+ B` appendix title ("Schedules A and B Asset Class Details")
  has an `A`-initial second token, so `D\x00+` does not match it.
- NULs appear only in furniture (SPEC §2.2) and filer content is always a regular
  font that extracts intact, so a real H/J row never carries a leading NUL run
  and never trips this branch.

The intact-letter branches (`Source Date` / `Source Description`) are unchanged
and still cover the case-mangled rendering. The raw_text-degrade behavior for
real H/J rows is untouched — only the phantom **header** row is removed.

This change is confined to the furniture recognizer and does not revert or
weaken #128's Schedule-A anchoring/residual, #130's empty-trailing-schedule
appendix termination, or #131's Schedule C split.

## Verification

Two new value-asserting tests in `tests/test_fd_extraction.py` reproduce the
named filings' NUL-rendered header shapes synthetically (the #97/#101
convention — the filings are not checked into `tests/fixtures/`). Each asserts
**both** halves of the contract:

- `test_schedule_h_nul_glyph_header_not_emitted_as_row` (10054295 / 10059679):
  the NUL header is NOT a row (exactly one item — the real trip; no item carries
  a NUL/`Source` raw_text), AND the real H row still degrades as designed
  (`source`/`dates` split off, `location`/`items` null, full row in `raw_text`).
- `test_schedule_j_nul_glyph_header_not_emitted_and_row_degrades` (10061936):
  the NUL header is NOT a row, AND the real J row degrades to raw_text alone
  (every structured column null, `raw_text` carries the whole row verbatim).

Both tests were confirmed to FAIL before the fix (2 items including a phantom
`S D of D` header row) and PASS after. The existing intact-letter H banner test
(`test_schedule_h_banner_skipped_and_itinerary_coalesced`) and the #130
trailing-schedule tests stay green.

- Full suite: 441 passed.
