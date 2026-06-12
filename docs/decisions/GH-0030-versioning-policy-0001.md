# GH-0030 — Versioning policy: what `v0.x.y` means

**Date:** 2026-06-11
**Issue:** #30

## Context

openhouse releases as `v0.x.y`, but no written rule said what each digit means.
In practice every change so far bumped `x`; `y` had never moved. Meanwhile the
only versioning fact recorded anywhere was a *second* number — the
`schema_version` stamped into `parse-manifest.json` (`SCHEMA_VERSION = "0.2.0"`
in `openhouse/schemas.py`) — with no stated relationship to the release version.
The two sat at the same value (`0.2.0`) by coincidence, not by rule, and the
package metadata (`pyproject.toml`) had drifted to a stale `0.0.0`.

This note fixes the convention before that ambiguity ossifies. It covers pre-v1
(`0.x.y`) only; the `0.x → 1.0` cutover criteria are out of scope (#13, the v1
acceptance pass).

## Decision

### `0.x.y`, shifted left: `x` is "major", `y` is "minor + patch"

Pre-v1, the semver contract is shifted one place left:

- **`x` is our major.** Bump it on any **contract-breaking** change — the kind a
  consumer must react to. Concretely: a verb / flag / argument changes; the
  on-disk layout or stdout JSON shape changes; or the parsed schema changes so
  that `raw/` must be re-parsed (the CLAUDE.md "schema changes mean re-parse"
  rule — user-visible pre-v1).
- **`y` is our minor + patch.** Bump it on **backward-compatible** changes only:
  a bugfix that makes output match what SPEC.md already promised, crawl / backoff
  tuning, error-message wording, internal refactors, new tests, dependency
  bumps, docs.

There is no third digit pre-v1: minor and patch are collapsed into `y`.

### Litmus test

> Would someone who only reads stdout JSON, or who holds `raw/` / `parsed/` data
> on disk, have to *do* anything after upgrading?
>
> - **Yes** (re-parse, adjust a `jq` filter, change a flag they pass) → bump **`x`**.
> - **No** (it just works, only better) → bump **`y`**.

### The project version and `schema_version` are kept 1:1

There is effectively **one** version number. The `schema_version` written into
`parse-manifest.json` is **identical** to the project release version, and the
two are bumped together — never one without the other. A release tagged `v0.3.0`
stamps `schema_version: "0.3.0"`; a release tagged `v0.2.1` stamps `"0.2.1"`.

This collapses what was two drifting axes into a single source of truth. Pre-v1
it loses nothing: re-parse is cheap and offline by design (CLAUDE.md), so there
is no value in a separate, independently-incrementing schema axis to "protect"
consumers from re-parsing.

**Re-parse is keyed to the `x` component, not the whole string.** Because a
`y`-only release still bumps `schema_version` under the 1:1 rule, a consumer must
not treat *any* change in the string as "re-parse required." The trigger is
specifically the **`x` component changing** (`0.2.y → 0.3.0`), which by the
definition above is exactly when a contract / schema break happened. A `y`-only
bump (`0.2.0 → 0.2.1`) is backward-compatible: the stamped `schema_version`
changes, but existing `parsed/` data stays valid and no re-parse is owed.

### Enforcing the 1:1 rule in the tree

`SCHEMA_VERSION` in `openhouse/schemas.py` is the **canonical** version;
`pyproject.toml`'s `version` mirrors it. Before this change the two had drifted
(`pyproject` sat at a stale `0.0.0` while `SCHEMA_VERSION` was `0.2.0`), and the
"real" release version lived only in `omnibus/vX.Y.Z` branch names and PRs. This
change reconciles them:

- `pyproject.toml` is set to `0.2.0` to match `SCHEMA_VERSION`, with a comment
  pointing back to this rule.
- `tests/test_schemas.py::test_pyproject_version_matches_schema_version` asserts
  the equality, so a future bump that touches only one of the two fails CI. A
  version bump must move both.

Git tags (`vX.Y.Z` at release) remain a separate *process* choice, not enforced
by code here; the branch/PR naming already carries the release version in the
meantime.

## Scope

This note plus its newest-first line in `docs/decisions/README.md`; the
`pyproject.toml` version bump (`0.0.0 → 0.2.0`); and the equality guard in
`tests/test_schemas.py`. No schema change (`SCHEMA_VERSION` already read `0.2.0`)
— the bump only brings `pyproject` into line. SPEC.md is untouched: it is the
*data* contract; release-versioning policy is meta and belongs in the decision
log. The `0.x → 1.0` cutover stays out of scope (#13). Suite stays green.
