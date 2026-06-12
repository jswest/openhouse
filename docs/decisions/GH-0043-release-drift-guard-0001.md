# GH-0043 — Release drift guard: refuse to tag on schema-fingerprint drift

**Date:** 2026-06-12
**Issue:** #43
**Builds on:** [GH-0037](GH-0037-release-mechanism-0001.md)

## Context

GH-0037 stood up the release tool but **deferred the drift guard** explicitly:
"openhouse has no single DDL constant to AST-diff … until then the script trusts
`SCHEMA_VERSION`." That trust is the gap. The release version is
`v0.<SCHEMA_VERSION>.<patch>`, the minor *is* the parsed-schema generation, and a
re-parse is the only remedy for a schema change (CLAUDE.md: re-parse, not
migrate). So the one mistake the release flow must catch is a **silent** one: the
pydantic models in `openhouse/schemas.py` that shape `parsed/` output change
shape, but `SCHEMA_VERSION` is left untouched — which would ship reshaped
`parsed/` data under an unchanged compatibility signal, and stamp the wrong
generation into every `parse-manifest.json`.

This matters right now: the v0.5.0 omnibus reshapes `parsed/` (#16 bioguide join,
#17 structured E–J, #49 PTR exact-dollar) and must raise `SCHEMA_VERSION` to 5.
#43 lands first so that bump is **self-enforcing** for the rest of the bundle — a
changed schema with a static int refuses to tag.

## Decision

Add a pure fingerprint + drift check to `.claude/skills/release/release.py`; no
runtime/`parse`/`read` code changes. Mirrors bartleby's `check_drift` shape,
adapted to openhouse (a pydantic-model fingerprint, not a DDL AST-diff).

### Fingerprint is a pure function of the schema models

`fingerprint(models)` hashes the **normalized** `model_json_schema()` of every
pydantic model defined in `openhouse.schemas`, keyed by class name and SHA-256'd
over a canonical (`sort_keys`, no-whitespace) JSON dump. Models are
**auto-discovered** from the module (`__module__ == "openhouse.schemas"`,
excluding imported `BaseModel`) — there is no hand-maintained list, so a new
model or a new field is fingerprinted automatically. This is the smallest sound
scope: those classes *are* the `parsed/` shape (SPEC §6.1/§6.3).

**Normalization drops documentation, keeps structure.** `_strip_noise`
recursively removes `description` (docstrings and `Field(description=...)`) and
`title` (pydantic derives `title` from the field/class name — pure restatement of
the dict key) and sorts every dict. A reworded docstring, an added `Field`
description, or incidental key ordering therefore does **not** move the
fingerprint; a field added/removed/retyped or made (non-)optional **does**.
Keying by class name means a model *rename* is correctly drift (the rename
changes the emitted JSON key).

### Drift = fingerprint changed AND version not bumped

`check_drift(live, recorded, schema_version, last_tag)` returns an error string
(else `None`) iff **all** hold: there is a prior tag and recorded fingerprint;
`SCHEMA_VERSION` still equals that tag's minor (`schema_moved` is false); and the
live fingerprint differs from the recorded one. A version bump is the
acknowledgement — once `SCHEMA_VERSION` moves past the tag's minor, any
fingerprint change is *expected* and clean. No prior tag/fingerprint (the
baseline release) is never drift. Both functions are pure and unit-tested for the
objective's three cases (changed model + static version → drift; bump → clean;
non-schema edit → clean).

### "Last tag's fingerprint" = a committed file beside `schemas.py`

The drift check needs the fingerprint *as of the last tag*. The simplest sound
mechanism that the offline release flow can read without re-checking-out a tag is
a committed `openhouse/schemas.fingerprint` (a single hex line). It lives beside
`schemas.py` so a schema edit and its fingerprint refresh land in the **same
diff** — the file is the recorded baseline, and `git tag` snapshots it like any
other tracked file, so "what the last tag shipped" is just "the committed value".
`release.py --write-fingerprint` rewrites it; the drift message tells the operator
to run it alongside the `SCHEMA_VERSION` bump.

Rejected alternatives: writing the fingerprint *into* `parse-manifest.json`
(those are per-year data artifacts under `data/`, not a release-tracked constant,
and would couple the release tool to a parsed data dir); and re-checking-out the
last tag to recompute its fingerprint (needs git history spelunking and would
re-run the models at an old revision — more moving parts, no more soundness).

### Guard wiring

`main()` runs `check_drift` immediately after reading the schema int + last tag,
before computing the next version — so a dry run *also* surfaces drift, and any
drift exits non-zero with a clear stderr message (JSON/stderr contract,
CLAUDE.md) before a tag is ever created. `--write-fingerprint` short-circuits to
just refresh the file.

## Soundness

The guard is **sound, not complete**, and that is the right bias here: a false
*positive* (refusing a legitimate tag) is impossible unless the models genuinely
changed shape without a bump — exactly the state we want to block. A false
*negative* is bounded to changes the normalized JSON schema cannot see (a pure
docstring/`title` edit), which by construction do not change `parsed/` output and
so need no re-parse. So every refusal is a real un-bumped reshape, and every pass
is either an unchanged shape or an acknowledged (bumped) one.

## Tests (`tests/test_release.py`, all offline/pure)

- `fingerprint` is stable (same models → same hash; list order irrelevant),
  ignores docstring/`Field(description)` noise (same-named model, reworded prose →
  same hash), and moves on a real structural change (added field → different
  hash).
- `check_drift` for the three objective cases plus the baseline (no prior
  tag/fingerprint → clean).
- A guard test that the committed `openhouse/schemas.fingerprint` matches the live
  models — so a future schema edit that forgets to refresh the fingerprint fails
  CI loudly, not just at release time.

Full suite: **251 passed** (233 prior + 18 in `test_release.py`, 9 of them new).

## Consequences / residual

- The fingerprint covers **structure** (fields, types, optionality, required-ness,
  nested model refs, the model name). It deliberately does *not* cover validator
  bodies, `Field` constraints expressed only as `description`, or runtime
  serialization quirks — those don't surface in `model_json_schema()`. A schema
  change invisible to the JSON schema is the one drift case the guard can miss;
  it is also, by the same token, a change that does not alter `parsed/` JSON
  shape. Watch this if a future field gains a validator that *re-shapes* output
  without changing its declared type.
- One human step remains by design: on a real schema bump the operator bumps
  `SCHEMA_VERSION` **and** runs `--write-fingerprint` in the same commit. The
  committed-fingerprint guard test makes a forgotten refresh a test failure, so
  the omission is caught well before the release.
