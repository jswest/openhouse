# GH-0166 — Schedule C: split the candidate form's two amount columns

**Date:** 2026-06-18
**Issue:** #161 (Schedule C two amount columns), within the #166 parse
column-fix omnibus — Wave 2 (additive schema), distinct from the Wave-1
source|type boundary fix (#162).

## Context

The member annual Schedule C (earned income) has a single **Amount** column. The
Candidate/New-Filer form variant has **two**: "Amount Current Year to Filing" and
"Amount Preceding Year". The parser captured both trailing money/`N/A` tokens but
space-joined them into one `amount` string (e.g. `"$258,468.14 $406,169.85"`,
`"$400.00 N/A"`). A consumer could not tell current from preceding, and `amount`
was no longer a single machine-parseable value — a silent column merge.

## Decision

Emit the two columns as **separate fields**, mirroring how Schedule A models its
current/preceding income pair.

- **Naming: keep `amount`, add `amount_preceding`** — *not* the issue's suggested
  `amount_current`/`amount_preceding`. Schedule A already established the
  codebase convention: a primary column field (`income_amount`) plus a
  candidate-only `income_preceding` that is `None` on the member form. Schedule C
  follows the same shape: `amount` is the current/only column (the single column
  on the member form, the first column on the candidate form), `amount_preceding`
  is the candidate-only second column. Renaming `amount` → `amount_current` would
  needlessly churn the member form's existing, working field; mirroring Schedule A
  is both the smaller change and the consistent one.

- **Types stay strings.** Schedule C amounts are kept as verbatim `$N` / `N/A`
  strings (not `AmountRange`), as `amount` already was; `amount_preceding`
  matches. `raw_text` carries the whole row regardless (never silently drop a
  value).

- **Form distinction is structural, not a flag.** The regex captures one token
  (member form) or two (candidate form) at the row tail; one token → `amount`
  set, `amount_preceding` `None`; two → both set. No separate "is candidate form"
  detection is needed — the column count is the signal, exactly as the
  trailing-token count already drove the pre-#161 join.

## Scope

Touches only the amount split. The Wave-1 source|type boundary fix (#162) in the
same `_parse_schedule_c` is left untouched and its regression tests stay green.

## Schema

Adds a field to `ScheduleCItem` — a structure change — but it lands in the **same
schema generation 11** that Wave 1 already bumped to, so `SCHEMA_VERSION` is
**not** re-incremented. The change does move `openhouse/schemas.fingerprint` (the
release drift guard auto-discovers all models); the committed fingerprint was
regenerated in the same change (`release.py --write-fingerprint`).
