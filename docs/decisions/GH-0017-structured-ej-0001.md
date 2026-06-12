# GH-0017 — Structured columns for FD schedules E–J

**Date:** 2026-06-12
**Issue:** #17 (part of the v0.5.0 omnibus, #47)

## Context

#12 extracted the e-filed annual-FD body schedule by schedule (SPEC §6.3),
depth-ordering the work: **A–D fully structured**, **E–J shipped as
`raw_text`-only** line items (a single `RawLineItem`). That left the positions,
agreements, gifts, travel, honoraria, and new-filer-compensation schedules with
no queryable columns — a consumer asking "which gifts over $X" or "what board
seats" had only the verbatim row text. This sub-issue adds per-schedule
structured columns for E–J, on top of the `raw_text` every line item already
carries, so schema gaps keep losing nothing.

The evidence is the two committed e-filed FD fixtures
(`efiled_fd_10042852.pdf`, `efiled_fd_nulglyph_10049721.pdf`): both carry filled
Schedule E and F; G–I render `None disclosed.` (absent) and J is absent
entirely. The live form's column headers — verified on those fixtures —
(`Position | Name of Organization`, `Date | Parties | Terms`, `Source |
Description | Value`, `Source | Dates | Location | Items`, `Source | Activity |
Date | Amount`, `Source | Brief Description of Duties`) drive each split. No
network: extraction is offline against fixtures only.

## Decision

### One structured item model per schedule (E–J), `RawLineItem` retired

`RawLineItem` is replaced by `ScheduleEItem … ScheduleJItem`, mirroring the A–D
models: every column is `Optional` and **every item keeps a non-optional
`raw_text`**. The columns track the live form headers — E `position`/
`organization`; F `date`/`parties`/`terms`; G `source`/`description`/`value`;
H `source`/`dates`/`location`/`items`; I `source`/`activity`/`date`/`amount`;
J `source`/`description`. This is a parsed-schema reshape, so a re-parse (not a
migrate) is required — but `SCHEMA_VERSION` stays **5**: #16 already moved the
generation to 5 in this same omnibus, and #16/#17 converge on one schema-5
generation rather than each bumping. The committed `schemas.fingerprint` is
refreshed against the schema-5 models (the GH-0043 drift guard's
"bumped-on-purpose" signal).

### Split only on signals readable with confidence; degrade to `None`

The form's columns merge to a single space on extraction with no stable
delimiter, so each parser splits **only** on a signal it can read reliably and
leaves the rest `None` (CLAUDE.md "degrade to `None` rather than a wrong value";
`raw_text` still carries the row in full):

- **E (positions)** — split on a recognized leading position title (a closed
  list: President, Board Member, Trustee, …); the title is `position`, the
  remainder `organization`. An unrecognized opening leaves both `None`. Both
  fixtures' E rows split cleanly (`President` / `BLB Properties`, `Board Member`
  / `Housing Assistance Council`).
- **F (agreements)** — anchor each row on a leading `Month YYYY` (or
  `MM/[DD/]YYYY`) date and fold the heavily wrapping terms continuation lines
  into one `raw_text`; `date` is split off, `parties`/`terms` stay `None` (no
  reliable boundary between them).
- **G (gifts), I (honoraria)** — a trailing `$N[.dd]` figure is the
  `value`/`amount`; the pre-amount text becomes `source`; middle columns stay
  `None`. (Shared `_split_trailing_dollar` helper.)
- **H (travel), J (new-filer comp)** — no committed fixture has a filled row to
  anchor a reliable split, and their middle columns merge with no delimiter, so
  these keep the row whole in `raw_text` with all columns `None`. They route
  through the existing `_salvage_raw` fall-through (which already emits one
  raw_text-only item per row of the schedule's own model type) rather than
  carrying a hand-written parser that would be byte-identical to it.

### Prioritization is effort order, not scope

All six E–J are structured. The split *effort* leads with the denser, more
reliably columnar schedules (E/F filled in the fixtures; G/I have a clean
trailing-dollar anchor) and is conservative on the sparse ones (H/J), exactly as
the issue's "prioritize by fill rate, never drop `raw_text`" directs. A row that
doesn't bisect into columns is not a failure — it carries full verbatim
`raw_text`, the binding invariant.

### Column-header furniture for E–J dropped in segmentation

`_segment_schedules` already drops per-schedule column-header furniture so the
column parsers see only rows. The E/F headers (`Position name…`, `Date
Parties…`) were already covered; G–J header rows (`Source Description…`, `Source
Dates…`, `Source Activity…`, `Source Brief…`) are added to `_FD_FURNITURE_RE` so
a header line is never mistaken for a gift/travel/honoraria/comp item.

## Consequences

- e-filed annual FDs now carry structured columns for **all** schedules A–J;
  every E–J line item additionally retains its verbatim `raw_text`, so a column
  the parser can't read (an unrecognized E position title, F parties/terms, all
  of H/J) loses nothing.
- The structured E–J columns are best-effort over a delimiter-free, layout-
  variable form; the authoritative datum remains `raw_text`. H and J carry no
  populated-row fixture, so their columns are designed from the form headers and
  left `None` until a filled example justifies a split — a tracked follow-up, not
  a gap (the row is never dropped).
- Schema-5 reshape: re-parse from `raw/` (offline, cheap by design); no migration
  (pre-v1). `SCHEMA_VERSION` is unchanged (converged with #16 at 5); the
  `schemas.fingerprint` baseline is refreshed.
