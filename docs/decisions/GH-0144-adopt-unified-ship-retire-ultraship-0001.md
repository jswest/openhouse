# GH-0144 — adopt the redesigned unified `/ship`, retire `/ultraship`

Supersedes GH-0018, GH-0020; partially supersedes GH-0063 (the `ultraship` half
of the vendoring decision — the `ship` half stands).

## Context

The drawer's `/ship` was rewritten from a sequential single-issue workflow into a
unified **director / player / critic** multi-agent skill: the director (the
session) plans and dispatches, players implement one issue each in their own
sibling worktrees, and correctness + simplicity critics review post-landing. It
also absorbs the omnibus role — a parent issue with native GitHub sub-issues is
fanned across players, assembled on an omnibus branch, and promoted — which is
exactly what the separate `/ultraship` skill existed to do. The redesigned ship
mentions `ultraship` zero times.

This is not a like-for-like refresh: the invocation surface and the config
contract both changed, so a bare re-press would leave `.claude/ship.toml` on the
old keys and `/ship` reading a config it no longer understands.

## Decision

Adopt the redesigned `/ship` in full and **retire `/ultraship`**.

Load-bearing choices:

- **Re-press `ship` fresh from the drawer.** Removed the stale pressed dir
  (older SKILL.md, old `.toml.example`, no `.stamp`) and `signet press ship` —
  installs the new `SKILL.md` + `ship.toml.example` and writes the `.stamp`
  (gitignored, machine-local).

- **Migrate `.claude/ship.toml` to the new contract** (the press never touches
  this file). Dropped `version_scheme`, `skip_tests_token`, `gate_agent`;
  renamed `docs_only_globs` → `auto_skip_globs` and `live_data_note` →
  `guardrails` (openhouse's live-data redline text preserved verbatim, now
  injected into every player prompt and the director's own work); added
  `player_tier = "cheap"` and `max_critic_passes = 3`. `architecture_doc` stays
  `""` — openhouse keeps no single current-state doc, so there's nothing for the
  pre-promotion sweep to read.

- **Retire `/ultraship` entirely.** The new ship covers omnibus natively, so the
  separate skill is redundant. Removed `.claude/skills/ultraship/` (SKILL.md,
  `ultraship.py`, stamp), `.claude/ultraship.toml`, and `tests/test_ultraship.py`
  (which loaded `ultraship.py` by path; nothing else imports it). Cleared the
  signet registry entry.

## Scope

`ship` (re-pressed) + `ultraship` (removed). **`release` still stays repo-local**
— unchanged from GH-0063, not yet generalized in the drawer.

Quality-gate note: openhouse's `CLAUDE.md` "Pre-commit gates" rule
(`uv run pytest` → `simplify-refactor` → re-run → commit) is left in place as
general repo hygiene for any commit, even though `/ship` now runs its quality
review as a post-landing critic rather than a per-commit gate.

Files: `.claude/skills/ship/` (re-pressed), `.claude/skills/ultraship/`
(removed), `.claude/ship.toml` (migrated), `.claude/ultraship.toml` (removed),
`tests/test_ultraship.py` (removed), `.gitignore` (drop the `ultraship.toml`
un-ignore), `CLAUDE.md` (vendoring note now ship-only).
