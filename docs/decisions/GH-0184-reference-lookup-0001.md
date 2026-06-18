# GH-0184 — `reference` command: top-level legislator lookup by name or bioguide-id

**Date:** 2026-06-18
**Issue:** #184

## Context

`clerk read` exposes `--bioguide` and `--member` filters for querying parsed
filings, but there is no command for simply finding a legislator's bioguide ID
or confirming who a given ID refers to. A user trying to look up "González-Colón"
or "A000370" has to know the bioguide ahead of time or go to an external source.

The CC0 `congress-legislators` bulk files (`legislators-current.json` +
`legislators-historical.json`) are already on disk after any `clerk pull` under
`raw/reference/`. This is a pure read-only query over that cache — no network,
no schema change, no re-parse.

## Decision

### Top-level placement

`reference` is registered as a top-level subparser alongside `ready` — not under
`clerk` or `fec`. Rationale: it is a **shared cross-source identity lookup**, not
a data source's pipeline verb. Scoping it under `clerk` would be a category
error (the same bioguide identity is equally relevant to the FEC lane). This
**extends** the top-level exemption established for `ready`/`release` in
GH-0174 — kept additive, GH-0174 itself is unchanged.

### Matching: substring + diacritic folding

The needle is matched against:
- **bioguide id**: plain case-insensitive substring (no diacritic folding needed
  — bioguide IDs are ASCII).
- **name fields** (`first`, `last`, `official_full`): diacritic-insensitive via
  `_norm_name()` (already in `legislators.py`), then case-insensitive substring.
  So `"gonzalez"` matches `"González-Colón"`.

Search is over **current ∪ historical** — both files loaded, union deduplicated
naturally (no member appears in both files in the real data, but we read both
regardless).

### Output columns

`name`, `bioguide_id`, `chamber`, `state`. `name` = `official_full` when
present, else `"{first} {last}"`. `chamber` and `state` come from `terms[-1]`
(the most recent term): `chamber` = `type` ("rep"/"sen"), `state` = `state`.
Sorted by `name` asc, then `bioguide_id` asc — deterministic regardless of
file order.

### Complete-over-cache guarantee

The command is **complete** over the on-disk cache: every record whose bioguide
or name matches the needle is returned, none dropped. The residual (members
absent from the cache, e.g. sworn in after the last `clerk pull`) is noted
in a one-line stderr message alongside the count of records searched.

No matches → empty result (`[]` / header-only table), exit 0. Neither reference
file present → non-zero exit with a pointer to `clerk pull`.

### Implementation

New module `openhouse/reference.py`; pre-argparse intercept in `cli.main()` on
`raw_argv[:1] == ["reference"]`, mirroring the `clerk read` and `fec read`
pattern. Reuses `_norm_name()` and the shared `load_legislator_records()` loader
from `legislators.py`, and `_emit()` / `resolve_data_dir()` from `cli.py`.

## Scope

New: `openhouse/reference.py`, `tests/test_reference.py`. Amended:
`openhouse/cli.py` (import + `build_parser` stub + dispatch intercept),
`openhouse/legislators.py` (shared `load_legislator_records()` loader),
`README.md` (usage docs). No schema change; no network call; no re-parse.
