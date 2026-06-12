# GH-0040 — `read` residual refinements: PTR base, `not_classified`, `--ticker` asymmetry, schema-drift warning, filing-year docs

**Date:** 2026-06-11
**Issue:** #40 (LOW items only; the MEDIUM PTR owner-column fix is deferred)

## Context

A review of `openhouse/read.py` surfaced five findings, one MEDIUM and four LOW.
The four LOW findings are all **residual-accounting** corrections — they tighten
the "complete over the known, explicit residual for the unknown" guarantee
(CLAUDE.md) that `read` must state on every query. They do not change which
records a query returns; they change what `read` *says* about the unknown
remainder, plus one safety warning and two doc sentences. The MEDIUM owner-column
fix (`SP `/`DC `/`JT `-prefixed asset names in `pdf.py`) is **out of scope** here:
no fixture exhibits it, and `pdf.py` is owned by another branch on this omnibus.

## Decision

### 1. `trades` residual base is e-filed type-`P`, not all e-filed; and counts `not_classified`

The `trades` residual said "complete over the N e-filed filings parsed," where
`N = by_pdf_class.efiled` from the manifest. But that count includes e-filed
**FDs**, which have no PTR transaction bodies — so it overstated the body-bearing
population a `trades` answer is actually complete over. A new helper
`_ptr_efiled_count` counts e-filed **type-`P`** filings from `filings.json` (the
per-record source of truth; the manifest roll-up has no e-filed-by-type
breakdown), and `cmd_trades` passes it to `_print_residual` via a new keyword-only
`parsed_override`. The other range commands (`filings`, `summary`) keep the
all-e-filed base, which is correct for them.

Separately, the residual omitted the manifest's `not_classified` bucket. After a
`--types ptr` partial parse, `not_classified` filings appear in **neither** the
parsed side **nor** the scanned/missing/error side — so the unknown was
under-reported. `_residual_counts` now tallies `not_classified` and folds it into
`unparsed`, and the residual line prints it explicitly:
`scanned S / missing M / error E / not_classified C`. This does not double-count:
`parse.py` reconciles `efiled + scanned + missing + not_classified == total`, and
`unparsed` sums only the non-efiled buckets.

### 2. `--ticker` null-ticker blind-spot asymmetry is now stated

The null-ticker blind-spot count (`_null_ticker_residual`) scopes by
`--member`/dates but **not** by `--owner`/`--type`/`--min-amount`. Against a
filtered query that count over-reports — which is the conservative direction (it
never hides a blind spot), but the asymmetry was silent. Rather than thread the
extra filters into the residual (more code, and they would narrow the very thing
whose blind spot we report), the smaller, clearer fix is to **state it**: the
residual line now appends "(Scoped by --member/dates only, ignoring
--owner/--type/--min-amount, so this is a conservative over-report against a
filtered query's population.)"

### 3. `read` warns once on `schema_version` drift

`read` queries the on-disk JSON shape directly and never checked the manifest's
`schema_version`; it would silently query a tree written by an older schema. A new
`_warn_schema_drift` helper emits **one** stderr warning per run when any in-range
manifest's `schema_version != SCHEMA_VERSION` (imported from `schemas.py`, the
current constant), then returns. Per "re-parse, not migrate" (CLAUDE.md) it only
warns and names the remedy (`openhouse parse`) — it does not migrate. It is
called from all three range commands (over `present`, so skipped years don't
trigger it) and from `cmd_filing` (over the single found year).

### 4. `trades <range>` = **filing** year (docs only)

The range selects **filing** years, but transactions routinely predate the filing
(a Dec-2020 trade lands in a 2021 filing). The `trades` range `--help` and a new
SPEC §5 Requirements bullet now say: "range = filing year; transactions may
predate it — widen the range when bounding by transaction date." `_add_range_arg`
gained an optional `help=` parameter so only the `trades` range arg carries the
note; the other three range args are unchanged.

## Consequences

- The `trades` residual now reads each year's `filings.json` once more (for
  `_ptr_efiled_count`). Negligible at this scale (a handful of per-year JSON
  files), and consolidating it would add coupling to `_print_residual`'s
  signature rather than remove it — left as-is deliberately.
- No new flags or query modes; the guarantees stated are unchanged in kind, only
  more accurate. Edits confined to `openhouse/read.py`, `SPEC.md`, and
  `tests/test_read.py` (seven new tests covering the PTR base, `not_classified`,
  the `--ticker` asymmetry statement, and the schema-drift warning).
