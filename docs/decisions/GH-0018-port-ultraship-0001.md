# GH-0018 — Port `/ultraship` (unattended omnibus assembly) from bartleby

**Date:** 2026-06-11
**Issue:** #18

## Context

openhouse had `/ship` (including `onto #<omnibus>` and omnibus-promotion modes)
but no `/ultraship`. ultraship was built in the sibling **bartleby** repo and
never ported here. It takes a single omnibus issue and assembles the whole
bundle unattended — a director interview up front, a stage-manager fanning
sub-issues out to player subagents, a serialized merge train of "Part of #N"
sub-PRs, a bounded critic loop, a director grade, and a risk-ranked report.

The substrate it needs already existed in openhouse: the omnibus-tracking regime
(the `omnibus-checklist` block + native sub-issue links + the `### Sub-issues`
prose section, all maintained by `/ship onto`), the `guard-main-write.sh` hook,
and the `simplify-refactor` agent.

## Decision

Port three artifacts, adapting to openhouse rather than copying verbatim:

- **`.claude/skills/ultraship/SKILL.md`** — the bartleby skill prose, with the
  entire "Never touch the live corpus namespace" section dropped (it is
  bartleby-specific: `BARTLEBY_HOME` sandbox, `serve`/`ingest`/`project upgrade`,
  the `bartleby-home-sandbox.sh` hook). It is replaced by openhouse's live-data
  discipline: `pull` is the only network step; `parse`/`read` are offline and
  deterministic; a player verifies against checked-in fixtures, never the live
  Clerk site; a step that seems to need a live fetch is a park-with-a-question.
  Worktree paths are rewritten to `../openhouse-issue-<N>-<slug>`.
- **`scripts/ultraship.py`** — the deterministic, agent-free manifest
  validator / wave planner. Pure stdlib, repo-agnostic; only example paths in
  its docstring were changed to openhouse ones.
- **`tests/test_ultraship.py`** — its unit tests (29), loaded by path via
  `importlib`.

A minimal `pyproject.toml` (openhouse's first) was added so `uv run pytest` can
discover the test — the repo had no Python tooling yet. It declares only `pytest`
as a dev dependency plus `testpaths = ["tests"]`; it makes no claim about the
shape of the eventual `openhouse` package.

## The authority boundary (the rule that bends)

ultraship's **stage-manager merges sub-issue → omnibus autonomously** — the only
place the repo's human-merge rule bends. This is safe because the omnibus branch
is recoverable (the stage-manager never force-pushes it) and every landing is a
reviewable "Part of #N" sub-PR. **A human still merges omnibus → `main`**,
unchanged, via `/ship #<omnibus>` promotion mode. The `guard-main-write.sh` hook
protects `main` only and is untouched.

## Scope

Additive over `/ship onto`; `/ship` itself is unchanged. No corpus-sandbox hook
(not applicable here). Out of scope: relaxing human-merge-to-`main`, and
auto-cutting releases.
