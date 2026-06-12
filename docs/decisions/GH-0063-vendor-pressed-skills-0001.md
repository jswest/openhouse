# GH-0063 — vendor `ship`/`ultraship` as pressed copies, config-driven

## Context

`.claude/skills/{ship,ultraship}/` were hand-authored here, but the same skills
live in other repos too, so hand-copying drifts. We want one source of truth in a
local skill drawer and a byte-identical, checked-in copy in this repo's git —
pressed from the drawer, never hand-edited here. Everything openhouse-specific
(checkout path, `uv run pytest`, the `omnibus/vX.Y.Z` scheme, the worktree naming,
the live-data redline) was baked into the SKILL.md prose, which is exactly what
makes a generalized copy impossible.

## Decision

Adopt the drawer's generalized `ship` and `ultraship` as **vendored copies**: the
SKILL.md (and `ultraship.py`) are pressed verbatim into
`.claude/skills/{ship,ultraship}/`, and every repo-specific value moves into
`.claude/ship.toml` / `.claude/ultraship.toml`, which the press never touches and
the skills read at runtime.

Load-bearing choices:

- **Consumer-only; the press tool is not a dependency.** openhouse never invokes
  the drawer/press tool — it only consumes the `.stamp` the press leaves behind.
  The generalized SKILL.md self-checks against that stamp (byte-compare vs the
  drawer named in it; **skip silently if the drawer is absent**, so clones / CI /
  teammates are never penalized), so no separate drift hook is needed here.

- **`.stamp` is gitignored, the `.toml` are tracked.** The stamp is machine-local
  provenance (drawer path + per-file hashes); a clone with no drawer self-skips.
  The two `.toml` are the reviewable, per-repo contract, so they're un-ignored
  out of the `.claude/*` blanket. `ship.toml.example` rides along verbatim from
  the drawer (kept, so the drift check stays green).

- **Behavior changes ride along verbatim.** The generalized `ultraship` invokes
  system `python3` (its core is stdlib, deps-free) rather than `uv run python`,
  and relaxes the hard `omnibus/vX.Y.Z` title requirement to a slug fallback. Both
  are accepted as part of vendoring the drawer copy, not re-litigated here.

## Scope

`ship` + `ultraship` only. **`release` stays repo-local** — it isn't generalized
in the drawer yet, and its `release.py` still reads `SCHEMA_VERSION` from
`openhouse/schemas.py` and encodes the `v0.<SCHEMA_VERSION>.<patch>` scheme; it
will be config-driven and vendored in a follow-up. The drawer's `SPEC.md` is
denylisted by the press (dev-only, not vendored).

Files: `.claude/skills/{ship,ultraship}/` (pressed), `.claude/ship.toml`,
`.claude/ultraship.toml`, `.gitignore` (un-ignore the `.toml`, ignore
`.claude/skills/*/.stamp`), `CLAUDE.md` (the vendored-from-drawer note).
