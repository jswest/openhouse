# GH-0114 — `asset_type` normalized, verbatim tag preserved beside it

**Date:** 2026-06-14
**Issue:** #114 (part of #115, openhouse v0.7.0)

## Context

`asset_type` was emitted as the **verbatim** bracketed `[TYPE]` tag from the
Clerk's PDFs (`_asset_type_from_asset` / the FD `[TYPE]` regex did a `.strip()`
only). The Clerk renders that tag with **inconsistent casing** across the corpus —
`ST`, `sT`, `Cs`, `gS` all occur (pdfplumber's small-caps glyph artifact, plus
per-form drift). So every downstream consumer had to defensively `upper()` the
field to compare or group by it: `read.py`'s `_TICKERED_ASSET_TYPES` filter did
exactly that, and an external cross-corpus field-report session reported having to
do it everywhere.

The field carries `asset_type` on three models — `PtrTransaction`, `ScheduleAItem`,
`ScheduleBItem` — so any fix is a schema change and a forced re-parse.

## Decision

**This sub-issue owns the single `SCHEMA_VERSION` 6→7 bump for the v0.7.0 bundle.**
Other sub-issues that need schema space ride this one bump.

### Field shape: normalize `asset_type` in place, add `asset_type_raw`

Adopted the issue's **recommended** shape (over the rejected alternative):

- **`asset_type`** is now **normalized** — uppercased and trimmed — so the
  convenient, comparable value is the **default** field. `None` when the row
  carries no tag.
- **`asset_type_raw`** is a new sibling field carrying the **verbatim** tag,
  preserving the Clerk's casing (`sT` / `Cs` / `gS`). `None` exactly when
  `asset_type` is.

This mirrors the schema's existing "structured field + verbatim `raw_text`"
pattern (CLAUDE.md: *preserve raw values alongside anything normalized*), and the
raw tag is **never dropped**.

**Rejected alternative:** keep `asset_type` raw and add `asset_type_normalized`.
Rejected because it leaves the footgun (inconsistent casing) as the *default*
field — every consumer would still reach for the raw field by habit and re-learn
the casing trap. Making the clean value the default is the whole point.

### Where it's populated (`pdf.py`)

- `_asset_type_from_asset` is renamed `_asset_type_raw_from_asset` (it still
  returns the stripped-but-not-cased tag — now explicitly the *raw* value).
- A new `_normalize_asset_type(raw)` helper does the uppercase+trim, `None`→`None`.
- All three construction sites (PTR transactions, Schedule A items, Schedule B
  items) now compute the raw tag once and set `asset_type=_normalize_asset_type(raw)`
  and `asset_type_raw=raw` consistently.

### `read.py` reconciliation

`read.py`'s `_TICKERED_ASSET_TYPES` filter previously did
`(txn.get("asset_type") or "").upper()`. Since `asset_type` is now normalized at
**parse** time, the query-time `.upper()` is redundant and was removed (a comment
records why). Behavior is preserved: the parsed-fixture `asset_type` values are
already uppercase (`ST` / `CS`), so the filter still matches them.

### Schema bump + fingerprint

`SCHEMA_VERSION` **6→7**, with a "Generation 7" note appended to the
generation-history docstring in `schemas.py`. This forces a re-parse from `raw/`
(re-parse, not migrate — CLAUDE.md). `openhouse/schemas.fingerprint` was refreshed
via `release.py --write-fingerprint` so the GH-0043 drift guard stays green.

## Tests (all offline)

- `tests/test_ptr_extraction.py::test_asset_type_is_normalized_with_raw_preserved`:
  a synthetic PTR page with mixed-case tags (`[sT]`, `[Cs]`, `[gS]`) extracts
  `asset_type == ["ST", "CS", "GS"]` (normalized) while `asset_type_raw ==
  ["sT", "Cs", "gS"]` (verbatim) — asserting both fields on mixed-casing rows.

Full suite: **402 passed**, including the fingerprint drift guard after the refresh.

## Consequences

- A corpus re-parse from `raw/` is required (the bump forces it); offline and
  cheap by design.
- Hand-authored parsed fixtures under `tests/fixtures/parsed/` already carry
  uppercase `asset_type` and gain `asset_type_raw` only when re-parsed; the new
  field is `Optional` (defaults to `None`), so the existing fixtures stay valid
  and the read tests are unaffected.
