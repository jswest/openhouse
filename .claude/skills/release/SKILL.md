---
name: release
description: >-
  Cut an openhouse release the way this repo expects: enforce the on-main /
  clean / synced preconditions, dry-run the release script, PAUSE for explicit
  confirmation, then publish the tag + GitHub Release. Use whenever asked to cut,
  publish, or tag a release.
---

# release — cut a release with dry-run → confirm → publish

No argument. Run the steps in order. One hard stop is marked **PAUSE**: do not
pass it without the user's OK.

Cutting a release is a **deliberate, post-merge act on `main`** — never a
per-issue step, never something to fold into a `/ship` run. This skill only
*advises and orchestrates*; `.claude/skills/release/release.py` is the source of
truth for the version math. **Do not invent, pass, or guess a version** — the
script computes it from `SCHEMA_VERSION` and the last tag.

Repo specifics: the main checkout is `/Users/johnwest/Code/spot/openhouse`; run
everything from there. The release runs via `uv run python
.claude/skills/release/release.py`, which starts with `uv`, so the
`guard-main-write.sh` hook (it gates only segments beginning with `git commit` /
`git push`) never sees the script's internal `git tag` / `git push`. **Never
reach around the script with a bare `git tag` or `git push` on main** — those
would be blocked, and rightly. Always go through the script.

Versioning is automatic: `v0.<SCHEMA_VERSION>.<patch>` — the minor *is*
`SCHEMA_VERSION`, the integer parsed-schema generation (read from
`openhouse/schemas.py`); patch increments at the same schema and resets to 0 on a
schema bump; major is carried forward. The package version itself is derived from
the tag by `hatch-vcs`, so the script's only write is the tag.

## 1. Preconditions (enforce these — the script does not)
The script *warns* on a non-`main` branch and *refuses* a dirty tree (unless
`--allow-dirty`), and never checks sync with the remote, so this skill enforces
the guardrails up front. Refuse to proceed unless all hold:
- **On `main`.** `git rev-parse --abbrev-ref HEAD` is `main`. If not, stop — a
  release is cut from `main` after a merge.
- **Working tree clean.** `git status --porcelain` is empty.
- **Synced with the remote.** `git fetch origin`, then confirm HEAD is up to date
  with `origin/main` (`git rev-parse HEAD` == `git rev-parse origin/main`, i.e. no
  un-pulled or un-pushed commits). If behind, `git pull --ff-only origin main`
  first; if ahead, stop and report (a release should only contain merged work).
- Confirm `gh auth status` is good (the publish step calls `gh`).

## 2. Dry run
From the main checkout: `uv run python .claude/skills/release/release.py`
(default = dry run, no flags). Show the user:
- **Last tag**, **schema version** (with the `(schema bumped → re-parse)` marker
  if it moved), and the **computed next release** version.
- The **release-notes preview** the script printed.

## 3. Surface the guard — don't route around it
The dry run can stop early; relay the script's own message and **halt** rather
than working around it:
- **No-op guard** (`error: no commits since <tag>; nothing to release.`, exit 1):
  HEAD is already the latest release. Say so and stop — there is nothing to cut.

(openhouse has no schema-drift guard yet — unlike bartleby, there is no single
DDL constant to diff. Until that lands, the script trusts `SCHEMA_VERSION`; if
the parsed schema changed, make sure the bump is in `openhouse/schemas.py` before
releasing.)

## 4. PAUSE — confirm
Hard stop. Show the dry-run output (next version + notes) and **wait for the
user's explicit OK** before anything is tagged or pushed. Nothing below this line
runs without that confirmation.

## 5. Publish
On confirm, from the main checkout: `uv run python
.claude/skills/release/release.py --tag --push`. This creates the annotated tag,
pushes it, and publishes the GitHub Release. **Report the release URL** that
`gh release create` prints on success.
