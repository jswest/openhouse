# GH-0037 — Release mechanism: integer schema generation as the minor

**Date:** 2026-06-12
**Issue:** #37
**Supersedes:** [GH-0030](GH-0030-versioning-policy-0001.md)

## Context

`GH-0030` set a `v0.x.y` policy where the project version and a *string*
`SCHEMA_VERSION` ("0.2.0") were kept identical, `x` = any contract break, `y` =
backward-compatible. It had a latent imprecision: it declared "re-parse is keyed
to the `x` component changing," but also made `x` bump on a verb/flag/arg change —
so a `read` flag rename bumped `x` yet needed **no** re-parse. `x` changing was
therefore not a reliable re-parse signal.

This note replaces that model with one modeled on bartleby's `/release` (its
decisions GH-0100, GH-0361), **adapted** to openhouse, and adds the release
tooling GH-0030 deferred. It covers pre-v1 only; the `0.x → 1.0` cutover stays
with #13.

## Decision

### `SCHEMA_VERSION` is an integer, and it *is* the minor

`SCHEMA_VERSION` (`openhouse/schemas.py`) becomes an **int** — the parsed-schema
generation — and *is* the minor of the release version `v0.<SCHEMA_VERSION>.<patch>`.

- **MINOR == `SCHEMA_VERSION`.** It moves **iff** the parsed-data schema changes,
  which forces a re-parse. The same integer is stamped in every
  `parse-manifest.json`, so a user reading it off their `parsed/` knows exactly
  which generation they hold. The re-parse signal is now **exact** — the
  imprecision above is gone.
- **PATCH** increments for every release at the same schema; resets to 0 on a
  schema bump.
- **MAJOR** is a human call (the v1 cutover, #13) — never automated; carried
  forward.

Current value: `3` (was the string `"0.2.0"` → `"0.3.0"`; the PTR body added in
#9 is generation 3). The constant *is* the minor, so computing the next version
needs no diffing.

### Accepted tradeoff: CLI/stdout-only breaks become patch pre-v1

Because the minor tracks the parsed schema only, a CLI / flag / stdout-shape
break that does **not** change the parsed schema lands in a **patch** pre-v1. A
`jq`-shape change with no parsed-schema impact could therefore ship in a patch.
This is accepted, not overlooked: semver promises nothing about patch pre-1.0,
the change still appears in prose in the GH Release notes, and in openhouse
stdout is largely *derived from* the parsed schema, so standalone stdout-only
breaks are rare. Recorded so the choice is explicit.

### Version is tag-derived (`hatch-vcs`), not a literal

The package version derives from the latest git tag via `hatch-vcs` (hatchling's
native equivalent of bartleby's `setuptools-scm` — openhouse already builds with
hatchling). `pyproject` carries `dynamic = ["version"]`, `__version__` reads
`importlib.metadata`, and the release tool's **only write is `git tag`**. This
**replaces GH-0030's literal `pyproject.version` + the
`test_pyproject_version_matches_schema_version` guard** — there is no longer a
literal to keep in sync. Off a tag, builds report a dev version
(`0.<…>.devN+g<sha>`).

### The release tool + `/release` skill

`.claude/skills/release/release.py` (beside its `SKILL.md`, the GH-0020 pattern —
not a recreated `scripts/`) computes `v0.<SCHEMA_VERSION>.<patch>` from the int +
last tag, generates GH Release notes from `git log`, and prepends a **"re-parse
from `raw/`"** banner when the schema moved. `--tag` / `--push` tag and publish.
The `/release` skill wraps it: on-`main` / clean / synced preconditions →
dry-run → **PAUSE** → publish. Cutting a release is a deliberate post-merge act
on `main`, never folded into `/ship`.

**Adapted away from bartleby:** the `_UPGRADES` additive-vs-breaking messaging
(GH-0361) — openhouse has no in-place migration, so the only remedy for a schema
change is a re-parse, and the banner collapses to that single case — and the DDL
**drift guard**, because openhouse has no single DDL constant to AST-diff (it is
deferred to its own follow-up; until then the script trusts `SCHEMA_VERSION`).

**Deliberately not built:** a hand-maintained `CHANGELOG.md` (the auto-generated
GH Release notes + this decision log cover it) and PyPI publishing (git-tag
installs already give real version pins; the Clerk-data legal restriction points
away from broad distribution).

### Baseline tag

The baseline tag is **`v0.3.0`** (schema 3, patch 0), cut from `main` by running
the new script post-merge — its first dogfood use, mirroring bartleby's `v0.7.0`.

## Scope

`schemas.py` (`SCHEMA_VERSION` → int 3); `pyproject.toml` (`hatch-vcs`, dynamic
version); `openhouse/__init__.py` (`importlib.metadata`); removed
`test_pyproject_version_matches_schema_version`; new
`.claude/skills/release/{release.py,SKILL.md}` + `tests/test_release.py`; this
note + its README index line; a one-line SPEC §6.5 clarification that the
manifest's `schema_version` is the integer generation. The schema-drift guard is
a follow-up. `parse.py` is unchanged — it passes the constant through, so the
manifest now stamps the int. Suite stays green (190 tests).
