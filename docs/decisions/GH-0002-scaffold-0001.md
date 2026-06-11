# GH-0002 — Scaffold: package, CLI skeleton, year-range parser, schemas

**Date:** 2026-06-11
**Issue:** #2

## Context

openhouse had only the `/ultraship` tooling and a minimal `pyproject.toml` that
declared nothing about the eventual `openhouse` package — no package, no CLI, no
models. M1 (SPEC §12) is to lay down the SPEC §9 layout: the `openhouse/`
package, a CLI dispatching three stub subcommands, the shared year-range parser,
and the pydantic filing-metadata schemas built against the *verified* SPEC §2.1
edge cases from day one.

This is scaffold only. No network code, no PDF code, no `pull`/`parse`/`read`/
`index`/`pdf` implementations — those are later sub-issues. The subcommands are
stubs that print "not implemented" to stderr and exit non-zero.

## Decision

### Testable year-range parser (no wall-clock)

`parse_year_range(arg, current_year)` lives in `cli.py` as shared infrastructure.
It takes `current_year` as an explicit parameter rather than reading the clock,
so it is deterministic and unit-testable without `datetime.now()` (SPEC §9: no
wall-clock in core logic). `main()` is the single place that reads the clock —
`datetime.now().year`, injected once at entry. The parser accepts `YYYY` or
`YYYY-YYYY`, validates the inclusive bound `[2008, current_year]`, rejects
reversed ranges and non-4-digit tokens, and **warns** (stderr, not an error)
that PTRs are absent before 2012 (SPEC §2.1). `pull`/`parse` validate the range
up front so a bad argument fails fast (exit 2) even though their bodies are
stubs.

### FilingType single-source-of-truth dict that preserves unknown codes

`schemas.py` holds `FILING_TYPE_LABELS`, seeded from SPEC §2.3's 12 verified 2024
codes (C, X, P, O, A, D, W, H, T, B, G, E). `FilingTypeInfo.from_code()` maps a
raw letter via the dict; an **unmapped** letter maps to the `"unknown"` sentinel
label but still carries the raw `code`, producing a valid record. This honors
the working agreement that an unknown filing type is never silently dropped.
Cross-year code enumeration (SPEC §10) folds into M2 and only ever edits this one
dict.

### Metadata-schema nullability choices

`FilingMetadata` (SPEC §6.1) uses the JSON example's field names. Every
nullability traces to a verified 2024 observation:

- `state_district: Optional[StateDistrict]` — empty `StateDst` (type `W`) → None.
- `filing_date: Optional[date]` — empty `FilingDate` (type `W`) → None.
- `filer.prefix` / `filer.suffix` default to None (usually empty).
- `StateDistrict.state` is a free 2-letter string — **not** validated against the
  50 states, so `DC00`/`PR00`/territories pass; `district` is an int with `0` =
  at-large/n.a.
- `doc_id` is `str` — opaque, since 4-digit IDs coexist with 7- and 8-digit.
- `year` is the coverage year and is never cross-validated against `filing_date`
  (a 2024 report can carry a 2025 filing date).

`filer_id` is intentionally *absent* here — its derivation (SPEC §6.2) lives in
`index.py` in a later milestone. Only the filing-metadata record is modeled; PTR
and FD body schemas (SPEC §6.3) come in M5/M6.

### requires-python kept at >=3.11

SPEC §9 and the issue say "3.12+", but the gate (`uv run pytest`) runs on the
local CPython 3.11.13 toolchain. To keep the gate green without forcing a
toolchain bump, `requires-python` stays `>=3.11` and no 3.12-only syntax is used.
Revisit when a 3.12 toolchain is the baseline.

A `[build-system]` (hatchling) was added so `uv` packages the project: without
it, `uv sync` skips installing the `openhouse` package and the
`[project.scripts]` entry point, and both the imports under test and
`uv run openhouse` would fail. `pydantic` is a runtime dependency; `pytest`
stays in the dev group; `testpaths = ["tests"]` is unchanged.

## Scope

Scaffold only. No network/PDF code, no `pull`/`parse`/`read`/`index`/`pdf`
implementations (stubs only). Schemas cover filing metadata, not bodies. The
existing `tests/test_ultraship.py` is untouched and still passes (55 tests total).
