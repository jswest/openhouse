# GH-0168 — FEC lane scaffold: schemas, year→cycle mapping, data layout, legal/provenance

**Date:** 2026-06-18
**Issue:** #168 (part of the FEC omnibus #167; builds on the source namespace #174)

## Context

#174 cut the `openhouse <source> <verb>` grammar and reserved
`raw/fec/<cycle>/` + `parsed/fec/<cycle>/`, leaving the `fec` verbs as bare stubs
that exit "not yet implemented" the instant they are reached. This sub-issue lays
the **contract** the FEC lane (Path 1 — connected-SSF PAC money, #167) builds on:
record schemas, the year→cycle convention, the cycle-keyed data layout, and the
legal/provenance footing. It implements **no** acquisition, normalization, or
query logic — those are later #167 sub-issues. The `fec` verbs stay stubs, but
the year→cycle **grammar is made real and testable** (the stub still exits after
parsing + expanding the year).

## Decision

### Schemas — a second lane, independently versioned

Three FEC models in `openhouse/schemas.py` (SPEC §13): `FecCommittee`
(committee id, name, `connected_organization_name`, `organization_type` +
verbatim `organization_type_raw`, `committee_type`, `affiliation`),
`FecPacContribution` (recipient/contributor committee ids, `amount`, `date`,
`line` `F3-11C`, and the `image_number` + `transaction_id` **double-entry key**),
and `FecMemberCandidateLink` (`bioguide_id`, `candidate_id`, `committee_id` — the
offline CC0 `id.fec[]` join, never fuzzy). A new `organization_type` code table
`FEC_ORG_TYPE_LABELS` (`C/T/L/M/V/W`) mirrors the FilingType pattern: raw code
preserved beside the normalized label, so an unmapped/blank type is never an
error.

`FEC_SCHEMA_VERSION = 1` is **independent of** `SCHEMA_VERSION` (which stays
**10**, untouched), exactly as `inspect`'s `LABELS_SCHEMA_VERSION` is independent:
a reshape in one lane must not force a re-parse of the other. A `provenance` field
(`"fec"` vs `"clerk"`, defaulting to `"fec"` on every FEC record) keeps the two
sources' differing legal footings distinguishable downstream.

### Fingerprint guard — refreshed deliberately (covers all models)

The release schema-drift guard (`.claude/skills/release/release.py`,
`schemas.fingerprint`, GH-0043) **auto-discovers every pydantic model in
`openhouse.schemas`** via `inspect.getmembers` filtered on
`__module__ == "openhouse.schemas"` — it is module-scoped, **not** FD-only. So
adding the FEC models necessarily moves the live fingerprint. Per the issue's
instruction for the "covers all models" case, the fingerprint was **regenerated
deliberately** (`release.py --write-fingerprint`) as part of this change. This is
*not* drift in the guard's sense: the guard fires only when models reshape at a
*static* `SCHEMA_VERSION` *between tags* — here the structure changed within a
pre-tag working tree, and the committed fingerprint simply tracks live (the test
`test_committed_fingerprint_matches_live_models` enforces that). No
`SCHEMA_VERSION` bump is warranted: the clerk parsed shape is unchanged, and the
FEC lane has its own version int.

### `date` field aliasing

`FecPacContribution.date` is *named* `date`. Under
`from __future__ import annotations`, the `= None` default would shadow the
`date` type in the class namespace when pydantic resolves the deferred string
annotation, yielding a None-only field. Fixed by annotating with a module-level
alias `Date = datetime.date` the field cannot rebind. The on-wire field stays
`date`.

### Year → cycle: mirror existing infra, no new vocabulary

`openhouse fec <verb>` reuses the existing `parse_year_range` parser (same
`<year>` / `<year>-<year>`, same `[2008, current_year]` bounds). Two small pure
helpers — `year_to_cycle` (odd → next even, even → itself) and
`expand_years_to_cycles` (sorted, de-duped enclosing cycles) — do the expansion.
There is **no `--cycle` flag**. When expansion changes the requested set the CLI
emits a one-line **stderr** note (the §5 `trades` filing-year-note pattern). The
year-scoped verbs (`pull`/`parse`) parse + validate + expand **before** the stub
exits "not yet implemented", so the grammar is exercised by tests today; the
query verbs (`read`/`donors`/`pac`) carry no year and short-circuit straight to
the stub.

### Data layout — cycle-keyed

`cli.fec_raw_dir(data_dir, cycle)` → `<data>/raw/fec/<cycle>/` and
`cli.fec_parsed_dir` → `<data>/parsed/fec/<cycle>/`, consistent with how the
clerk lane builds `raw/clerk/<year>/` inline, and with the same
`--data-dir`/`OPENHOUSE_DATA_DIR`/`~/.openhouse` precedence. `fec pull 2023` and
`fec pull 2024` both resolve to `.../fec/2024/`.

### Legal/provenance stub

A short note in `--help` (the `fec` source description) and README states FEC's
**public-domain** status + the **52 U.S.C. §30111** "sale or use" bar (no
soliciting / commercial use), mirroring the existing Clerk legal note. This is a
stub; full wording lands in #173.

## Scope

Code: `openhouse/schemas.py` (3 FEC models, `FEC_SCHEMA_VERSION`,
`FEC_ORG_TYPE_LABELS`, `PROVENANCE_FEC`/`PROVENANCE_CLERK`, `Date` alias),
`openhouse/cli.py` (`year_to_cycle` / `expand_years_to_cycles`,
`fec_raw_dir` / `fec_parsed_dir`, `_FEC_LEGAL_NOTICE`, the year→cycle routing in
the FEC interception, fec source description). Data: regenerated
`openhouse/schemas.fingerprint`. Docs: SPEC §13 (new), README use-restriction
FEC paragraph. Tests: FEC schema round-trips + the version-independence /
org-type-table assertions in `tests/test_schemas.py`; year→cycle expansion (odd,
even, range) + stderr note + cycle-keyed path helpers + arg-validation in
`tests/test_cli.py`. No FEC behavior implemented (stubs only); no network.
`docs/decisions/README.md` untouched (the director consolidates the index).
444 tests pass.
