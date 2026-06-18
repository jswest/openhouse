# GH-0174 — Source namespace: `openhouse <source> <verb>` + on-disk clerk migration

**Date:** 2026-06-18
**Issue:** #174 (part of the FEC omnibus #167)

## Context

openhouse began life as three bare verbs (`pull` / `parse` / `read`, later joined
by `inspect` and the tool-level `ready`) over a single data directory, all
implicitly about one data source: the U.S. House Clerk's financial disclosures.
The FEC lane (#167) introduces a *second* source. Two sources cannot both own the
bare verbs, and they cannot share `raw/<year>/` + `parsed/<year>/` without
collision. This sub-issue restructures the grammar and the on-disk layout to make
room — a **clean break**, pre-v1, with **no deprecation shim** (CLAUDE.md: schema
changes mean re-parse, not migrate; pre-v1 has no backwards compatibility).

It deliberately does **not** implement any FEC behavior — that is later sub-issues
under #167. It only scaffolds the `fec` source so the grammar is symmetric.

## Decision

### Grammar: a source noun above the pipeline verbs

The CLI is now `openhouse <source> <verb>`. The **pull / parse / read / inspect**
pipeline is source-scoped under `clerk`; today's verbs moved beneath it with
behavior and flags 100% intact (`openhouse clerk pull 2024` behaves exactly as the
old `openhouse pull 2024` did). The tool-level **`ready`** (and the repo-local
`release` skill) stay **top-level** — they install/release the tool, not a source's
data, so scoping them under a source would be a category error. **`reference`**
(#184) is also top-level: it is a shared cross-source identity lookup over the CC0
congress-legislators cache, not a data source's pipeline verb.

- **Bare verbs are removed**, not aliased. `openhouse pull …` prints a clear
  stderr error pointing at `openhouse clerk pull …` and exits 2 — a migration
  signpost instead of argparse's opaque "invalid choice".
- **`fec` is scaffolded as stubs.** Its verbs (`pull`/`parse`/`read`/`donors`/`pac`)
  exist so `openhouse fec --help` lists them and the grammar is symmetric, but each
  exits non-zero with "not yet implemented — see #167". The FEC source is
  intercepted in `main()` before argparse so a stub verb can carry args
  (`fec pull 2024`) without the stub sub-parser having to model each flag.

### CLI seam

`clerk read` keeps the existing pre-argparse REMAINDER interception (its
`--data-dir`/`--table` before-or-after-subcommand handling defeats argparse
subparsing): `main()` slices on `["clerk", "read", …]` and hands the rest to
`read_mod.run`. `build_parser` gains a `clerk`/`fec`/`ready` source layer; the
existing verb parsers move verbatim under `clerk` via `_add_clerk_verbs`.

### On-disk symmetry

Each source owns a `<source>/` level under both `raw/` and `parsed/`:

```
raw/clerk/<year>/…      parsed/clerk/<year>/…
raw/fec/<cycle>/        parsed/fec/<cycle>/      # RESERVED for #167, not created here
```

The clerk pipeline's path construction moved from `raw/<year>` → `raw/clerk/<year>`
and `parsed/<year>` → `parsed/clerk/<year>` across `pull.py`, `parse.py`,
`read.py`, `inspect/server.py`, `index.py` (the `source_pdf` literal), and
`scripts/sweep_invariants.py`. The shared CC0 congress-legislators set stays at the
**un-scoped** `raw/reference/` — it is bulk reference data, not a source's
disclosures, so the migration does not touch it.

### Migration: a one-time offline `mv`, never an auto-move

The migration for an existing user relocates **bytes**, not a re-crawl and not a
forced re-parse:

```
mv ~/.openhouse/raw/<year>    ~/.openhouse/raw/clerk/<year>
mv ~/.openhouse/parsed/<year> ~/.openhouse/parsed/clerk/<year>
```

When openhouse detects a legacy unprefixed `raw/<YYYY>/` (a 4-digit-year dir
directly under `raw/`) it prints this `mv` **once to stderr as a nudge** and
exits the verb normally — it **never relocates data itself**. This mirrors the
existing `./data` shadow note (SPEC §6.4): same one-time-flag pattern, same
"inform, don't mutate" spirit. The nudge fires for every clerk verb (resolved once
in `main()` for pull/parse/inspect; in `read.run` for read).

### `source_pdf` and the SCHEMA_VERSION bump (9 → 10)

Each parsed record embeds `source_pdf` (e.g. `raw/clerk/2024/ptr/<DocID>.pdf`),
consumed by `inspect` to locate the PDF. The namespaced layout changes that stored
string, which changes `filings.json` **bytes** — so per the project's iron rule
("schema changes mean re-parse, not migrate") `SCHEMA_VERSION` bumps **9 → 10**.

This is the one place the `mv`-only migration is incomplete: a bare `mv` relocates
the parsed JSON but not the `raw/<year>/…` path baked into each record's
`source_pdf`. The bump is the resolution — `read`'s existing schema-drift warning
fires against a `mv`-only tree, telling the user to re-run `openhouse clerk parse`
(cheap, offline) to refresh `source_pdf` to the moved bytes. So: `mv` to relocate,
re-parse to refresh the embedded paths; the drift warning makes the second step
visible rather than a silent inspect breakage. (The model *structure* is unchanged,
so the release fingerprint guard is not tripped — only the schema generation moves.)

## Scope

Code: `cli.py` (source layer, bare-verb rejection, fec stubs, legacy-layout nudge),
`index.py`/`parse.py`/`pull.py`/`read.py`/`inspect/server.py` (path construction +
user-facing command strings), `scripts/sweep_invariants.py` (walks `parsed/clerk/`),
`schemas.py` (SCHEMA_VERSION 9→10). Docs: SPEC §3/§4/§5/§5.5/§6.4 (+ §6.1 example,
§11 acceptance), README (command examples + a "Migrating" section), the package-data
skill files `openhouse/skill/SKILL.md` + `reference.md`. Tests + fixtures swept to
the new grammar/paths; the `parsed/` fixture tree moved under `parsed/clerk/`. No
FEC behavior implemented (stubs only). `docs/decisions/README.md` untouched (the
director consolidates the index). 427 tests pass.
