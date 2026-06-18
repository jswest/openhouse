# GH-0166 — one generalized FD column/row reconstruction, not per-symptom patches

**Date:** 2026-06-18
**Issue:** #166 (parse column-fix omnibus), core wave — sub-issues
#160/#162/#163/#164/#165 + the #76 extension

## Context

Five of the six sub-issues in #166 were **regressions of bugs already "fixed"
once** (#99/#100, #101/#131, #146/#103, #134/#102, #150/#103). The repeated
reopening traced to a shared root cause in `openhouse/pdf.py`: pdfplumber emits a
PDF row's words **left-to-right, line by line**, so when a row wraps, every
later-column fragment — a value/income high bound, a stacked income-type second
line, a wrapped Source name, a wrapped itinerary, a displaced `date_incurred`
year — lands at the **de-wrapped row tail**, after the asset/source-name
continuation, *out of its column slot*. Each prior fix patched the one schedule
where a particular tail-displacement surfaced; the next filing wrapped a
different column and the symptom returned.

## Decision

Fix the shared mechanism once where the column semantics are the same, and add
schedule-specific logic only where they genuinely differ.

- **Amount columns (`_fd_amount_entries`, used by Schedule A and B).** Replaced
  the greedy-bucket + single inversion-retry reader with a **minimal-split
  resolver**: treat every `$lo -` opener as one column, then search for the
  smallest set of textually-adjacent buckets to *split* (releasing their `$hi`
  into the ordered tail pool of wrapped highs) such that every column pairs to a
  high with no inverted range and no leftover dangling. A clean/one-wrap row needs
  no split; a multi-wrap row (Pou `10068928`: value **and** preceding both wrap,
  gluing an earlier high in front of a later low) recovers exactly when a
  consistent column-ordered assignment exists; otherwise it degrades to `None`
  (slot held, `raw_text` intact — never a fabricated bucket). The search is
  bounded (≤ one split per amount column). It also now recognizes a single
  **exact-dollar** column value (`$4,425.09`) as an `{exact, label}` point, the
  same point-not-bucket shape PTR rows use (#49).

- **Schedule A field untangling (`_schedule_a_amounts`).** Completes a **wrapped
  income-type second line** (`Capital Gains,` + tail `Dividends`) from the row
  tail and strips it from the asset; recognizes a value `None`/`Undetermined`
  whose `]`/`⇒` anchor wrapped off the line, when it precedes an income-type word
  *and* every amount opener (so an income-current `None` on the candidate form is
  not mistaken for the value). The open-ended `Over $X` value stays `None` with
  its slot held (the **#76** extension to FD Schedule A `value_of_asset`).

- **Per-schedule column boundaries** (semantics genuinely differ):
  - **C** (#162): expanded the Type vocabulary to the attested multi-word values
    (`Pension Distribution`, `Speech fee`, `Speech/panel fee`, the `1099-*`
    tax-form labels), longest-first, so the source|type boundary no longer bleeds.
  - **D** (#165): truncate the type slice at a `C :`/`C\x00+:` COMMENTS marker
    (Schedule D has no comment field) and recover a `Various dates in` date's year
    that wrapped into the amount column, re-attaching it to `date_incurred`.
  - **F** (#163): anchor rows only on a Date-column date that is **not** mid-prose
    (reject a leading date followed by `.`/`,`), killing the phantom split on an
    `MM/DD/YYYY` embedded in Terms text; best-effort `parties`/`terms` split on a
    closed Terms-opening keyword set.
  - **H** (#164): rebuild the multi-line `source` and the `location` itinerary by
    using the `Days` integer as the de-wrapped boundary and a closed
    `City, Region` / ` - ` leg signal to separate the wrapped Source-name
    continuation from the resumed itinerary.
  - **I** (#164 sibling): use the `Date` column as a second clean anchor so the
    activity and date split off and the source is the clean org name.

## Consequences

- `SCHEMA_VERSION` 10 → 11 (values change → re-parse, not migrate). No model
  *structure* changed (Schedule F/I already carried the now-populated
  `parties`/`terms`/`activity`/`date` fields), so `schemas.fingerprint` is
  unaffected; `FEC_SCHEMA_VERSION` untouched.
- A 120-FD before/after sweep showed **zero schedule row-count changes** and no
  new `extract_failed`: the fix corrects field *content* without destabilizing
  row segmentation — the property the per-symptom patches lacked.
- The genuinely geometry-ambiguous cases (a bare-city itinerary with no
  `City, Region` token; the old single-`Dates` dash-range H layout that carries no
  `Days` marker) are left as `raw_text`-complete with the structured field `None`,
  rather than guessed — soundness over a fabricated split (CLAUDE.md).
