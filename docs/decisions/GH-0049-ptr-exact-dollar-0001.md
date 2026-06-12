# GH-0049 — PTR exact-dollar amount: a sound point, not a fake range

**Date:** 2026-06-12
**Issue:** #49 (part of #47)

## Context

After GH-0046 fixed the PTR amount-column **wrap**, the remaining PTR
`extract_failed` rows (~97 in 2020, ~29 in 2021, per GH-0046's measurement) are a
**third amount format**: the amount column carries a single **exact dollar value**
(e.g. `$894.97`) in place of the usual `$LOW - $HIGH` bucket. GH-0046 left this
out of scope and called it out explicitly as a separate, schema-touching follow-up
(its Consequences section): the `AmountRange` schema (`{low, high, label}`) cannot
represent a single exact value, so these rows correctly surfaced as
`extract_failed` — loud in the unparsed manifest, never a silent gap.

This issue represents the exact value **soundly** so those rows extract, without
fabricating a range.

## Decision

One schema change, riding the existing `SCHEMA_VERSION = 5` (no re-bump — #16/#17
already moved it to 5; this converges on 5). Touches `schemas.py`, `pdf.py`,
`read.py` + tests.

### `AmountRange` gains an `exact` point, mutually exclusive with the bucket

`AmountRange` now holds **one of two shapes**:

- **Range** (the common case): `low`/`high` integer bounds, `exact = None`.
- **Exact value** (this issue): `exact` (a `float` — exact figures carry cents),
  `low`/`high` = `None`.

`$894.97` is **not** coerced into a `{low: 894.97, high: 894.97}` masquerading as a
real range — a point is not a bucket, and a consumer must be able to tell them
apart on the wire. A `model_validator` enforces that exactly one shape is set
(`exact` xor `low`+`high`); a row that is genuinely neither is rejected at the
model, and upstream the parser simply never builds one (it `extract_failed`s
instead). `label` stays the verbatim form string (`"$1,001 - $15,000"` or
`"$894.97"`).

**Serialization keeps the range JSON byte-identical to pre-#49.** A
`model_serializer` emits only the shape that applies — a range omits `exact`, an
exact value omits `low`/`high` — so the millions of existing range rows do **not**
gain an `"exact": null` field (no schema-churn diff, no output bloat), while an
exact row carries a single `exact`. The two shapes stay visibly distinct.

We deliberately did **not** add `bound_low`/`bound_high` helper properties: the
only comparison consumer is `read`, which works on raw dicts (below), so a
model-level accessor would be dead speculative surface area (CLAUDE.md: smallest
fix that fits).

### Parser recognizes the exact-dollar form (`pdf.py`)

A new `_PTR_EXACT_ROW_RE` mirrors `_PTR_ROW_RE`'s row signature (owner / asset /
type / date pair / glyph) but expects the amount column to be **one bare
`$N[,NNN][.NN]` money token with no dash and no leading word**. The leading `$`,
the trailing-glyph anchor, and the forbidden dash are what keep it **sound**: it
will not match a one-sided `Over $1,000,000` (a word sits before the `$`, after
the date pair) nor a half-range — both must stay `extract_failed`. Whole-dollar
exact values (`$500`) are accepted too; cents are not required.

In `extract_ptr_transactions`, a line is tried against the range regex first, then
the exact regex. An exact match builds its `AmountRange(exact=…)` immediately (the
value is complete on the header line — there is no amount-column wrap to recover)
and skips the wrap-recovery block. A shared `_is_ptr_header` helper (range **or**
exact) is now the row boundary throughout, so an exact row both starts a new row
and ends the previous one, exactly like a range row. GH-0046's wrap handling,
small-caps case-insensitivity, and the status-block completeness guard are
untouched.

### `read` treats an exact value as the closed point `[X, X]` (`read.py`)

`_amount_low` returns `exact` when present, else `low`. So `--min-amount X`
correctly **includes** an exact `$894.97` when `X ≤ 894.97` and **excludes** it
when `X > 894.97` — sound over the point, no fabricated half-open range. The
`trades` table already renders `amount_range.label`, so an exact value displays
its verbatim `"$894.97"` with no change.

### Genuinely-unparseable rows still fail loudly

A row whose amount is neither a `$LOW - $HIGH` bucket nor a bare exact `$N` (e.g.
`Over $1,000,000`) matches neither regex, is not emitted, and the status-block
guard surfaces the PDF as `extract_failed` — never a fabricated range, never a
silent empty (CLAUDE.md: prefer completeness, never silently drop).

## Tests (all offline)

- `tests/test_schemas.py`: a bucket serializes without `exact` (byte-identical to
  pre-#49); an exact value serializes as `{exact, label}` with `low`/`high`
  omitted; the validator rejects `exact` mixed with bounds, and rejects neither
  shape.
- `tests/test_ptr_extraction.py`: a synthetic exact-dollar row (`$894.97`)
  extracts as an exact point (not a fake range); a range row and an exact row
  coexist in one body (each bounds the other; whole-dollar `$500` accepted); a
  one-sided `Over $1,000,000` still raises `PdfExtractError`.
- `tests/test_read.py`: `_amount_low` returns the exact value for an exact row and
  `low` for a range; `--min-amount` treats `$894.97` as its own point (clears 500,
  excluded by 1000).

Full suite: **291 passed**, including the GH-0043 fingerprint drift guard after
refreshing `openhouse/schemas.fingerprint` for the reshaped `AmountRange`.

## Consequences / residual left deliberately

- **Full-corpus re-measurement is left to the stage-manager.** This issue was
  implemented in an isolated worktree without `data/`, so the real 2020–2021
  before/after `extract_failed` counts could not be re-run here (GH-0046 measured
  ~97 / ~29 still-failing rows attributable to the exact-dollar format; how many
  of those now extract is a `data/`-bound measurement). Fixture coverage proves
  the format extracts soundly; the corpus number is a follow-up, not a gap.
- The exact regex assumes the amount column is a **single** money token after the
  date pair. A two-token amount that is neither a dash-range nor a lone exact
  value would still `extract_failed` loudly (correct — it is a fourth format, not
  this one), surfaced by the status-block count guard.
