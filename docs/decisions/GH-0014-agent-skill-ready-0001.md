# GH-0014 — Agent skill as package data + `openhouse ready` stamping

**Date:** 2026-06-12
**Issue:** #14

## Context

SPEC §8 promised, post-v1, a Claude Code skill for openhouse following the
**bartleby pattern**: the skill prose ships as package data, and an `openhouse
ready` verb stamps a *blessed* copy into `~/.claude/skills/openhouse`. The CLI
(`pull`/`parse`/`read`) is already the entire interface, so the skill is thin —
it documents the three verbs, not a new code path. This sub-issue lands that
work as part of the v0.5.0 omnibus (#47).

## Decision

### Skill prose is package data; the repo is the only source of truth

Two Markdown files live inside the package: `openhouse/skill/SKILL.md` (how to
invoke the three commands, where the JSON lands, the §1 legal restriction) and
`openhouse/skill/reference.md` (record schemas, the FilingType table, query
recipes with the sound/complete guarantees). **No code lives in the skill
directory** beyond an `__init__.py` package marker — needed only so
`importlib.resources.files("openhouse.skill")` can resolve the prose. The files
are written to be accurate to the *actual* current CLI surface (the `read`
sub-parser), not an aspirational one.

`pyproject.toml` `force-include`s the two `.md` files into the wheel so the
packaged install always carries them, independent of hatchling's default
file-selection rules. Verified: the built wheel bundles both.

### `openhouse ready` — wipe-and-copy install + a hashed marker

New module `openhouse/ready.py`, wired as a fourth subcommand in `cli.py`
(localized to subparser + a single dispatch branch, so #50's later `cli.py`
changes merge cleanly). It is **offline** — no network, no year range — and obeys
the house contract: JSON to stdout, prose to stderr, non-zero exit on error.

- **Install** is wipe-and-copy: the destination is removed and recreated from the
  packaged files, so it is idempotent and never accumulates stale files.
- A hidden marker `~/.claude/skills/openhouse/.openhouse-skill.json` records the
  **producing package version** plus a **content hash** over the skill files. The
  hash is sorted-name `name\0bytes\0` so it depends only on content, never on
  iteration order; the marker file itself is excluded from the hash.
- `--check` classifies the install without writing: **up-to-date** (installed
  hash == packaged hash), **stale** (installed matches its marker but a different
  version is packaged now — re-run `ready`), **hand-edited** (files changed since
  install — marker hash no longer describes them), or **absent**. Exit 0 only for
  up-to-date.

We stamp a blessed copy rather than symlink a checkout so parallel agent sessions
run the last released skill, not whatever mid-refactor state a working tree is in
(SPEC §8 rationale).

### Not cribbed: the `skill_runner` / dispatch layer

bartleby's `skill_runner` dispatch is deliberately omitted — three CLI verbs
don't warrant an indirection layer (SPEC §8). The skill points the agent straight
at the CLI.

## Scope

New `openhouse/skill/{__init__.py,SKILL.md,reference.md}`; new
`openhouse/ready.py`; `openhouse/cli.py` (import + `ready` subparser + one
dispatch branch); `pyproject.toml` (`force-include` the skill `.md` files); new
`tests/test_ready.py` (hash/marker/status + run dispatch, all confined to
`tmp_path` — nothing touches the real `~/.claude`); this note. Suite green (258
tests). No v1 branding; no wall-clock in core logic.
