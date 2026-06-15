# Contributing to openhouse

openhouse pulls U.S. House financial-disclosure filings — annual Financial
Disclosure statements and STOCK Act Periodic Transaction Reports — from the
Office of the Clerk into normalized JSON, through three offline-after-`pull`
verbs: `pull` (network) → `parse` (offline) → `read` (offline), plus an
`inspect` accuracy-review tool. No database; one data directory.

This guide is **standalone** — you should be able to get productive without
reading anything else. Two companions hold the authoritative detail:

- [`SPEC.md`](./SPEC.md) — the contract: commands, data model, on-disk layout,
  and **verified facts about the Clerk's data source** (URL routing, XML edge
  cases, FilingType codes). Read it before changing anything structural; update
  it when behavior diverges.
- [`docs/decisions/`](./docs/decisions/) — one additive note per decision, the
  rationale trail behind why the code looks the way it does.

The [`README.md`](./README.md) is the user-facing tour (install, the four
verbs, flag reference). The project is **pre-v1**: the parsed schema is not yet
frozen, so a version bump can mean a re-parse rather than a migration (see
[Schema changes](#schema-changes-mean-re-parse-not-migrate) below).

## Dev setup & tests

Python 3.12+, dependencies managed with [`uv`](https://docs.astral.sh/uv/) —
**not** pip or a hand-rolled venv. Run code with `uv run python`.

```sh
uv run pytest        # the whole suite
```

The suite runs entirely on **checked-in fixtures** under
[`tests/fixtures/`](./tests/fixtures/), so it is offline and deterministic — no
network, no wall-clock dependence. A passing `uv run pytest` is the gate for
every commit.

Live-data probes during development hit the real Clerk site. Keep them **rare,
polite, and out of the test suite**: probe the live site once, by hand, then
turn what you learn into a fixture under `tests/fixtures/` — never a
network-touching test. Tests must keep running with the socket closed.

## Workflow & branching

Issue work flows through the vendored `/ship` skill, which drives the whole
issue → tested PR loop: sync the base branch, create a sibling worktree, work
in logical commits behind per-commit quality gates, reconcile against the base,
and open a PR. Invoke it as `/ship #<N>`. Omnibus bundles (several sub-issues
landed onto an integration branch before reaching `main` as one unit) are
handled by the companion `/ultraship` skill. Both live under
`.claude/skills/` and read their repo-specific values from
`.claude/ship.toml` / `.claude/ultraship.toml`.

The rails the skills enforce — follow them by hand too if you ship manually:

- **Never commit or push on `main`.** The
  [`guard-main-write.sh`](./.claude/hooks/guard-main-write.sh) hook enforces
  this; a block means you're on the wrong branch.
- **Issue work happens in a sibling worktree** — `../openhouse-issue-<N>-<slug>`,
  never nested inside the repo, never `git checkout -b` on `main`. The worktree
  is based on the freshly-fetched `origin/main`.
- **Pre-commit gates, every commit, in order:** `uv run pytest` (must pass) →
  the `simplify-refactor` quality pass over the touched files → apply what's
  worth taking → re-run `uv run pytest` → commit.
- **PRs carry a `Closes #<N>` line**, and **a human merges** — never merge your
  own PR. (Under an omnibus, the sub-PR says *"Part of #<omnibus>"* with no
  `Closes`; the sub-issues close when the omnibus → `main` PR merges.)

## Data-model & legal rules

These are load-bearing invariants, not style preferences. Changing the code in
ways that violate them is a bug even when the tests pass.

### Schema changes mean re-parse, not migrate

Pre-v1 there is **no** backwards compatibility. When the parsed schema changes,
bump the `SCHEMA_VERSION` recorded in `parse-manifest.json` (the integer
generation in [`openhouse/schemas.py`](./openhouse/schemas.py)), delete the old
code, and re-run `openhouse parse` from `raw/`. That re-parse is cheap and
offline by design — keep it that way; don't write migration shims.

### Every query must be sound or complete — declare which

A query with no false positives bounds the truth from below ("as few as" — at
least these); one with no false negatives bounds it from above ("as many as" —
at most these). A query that yields both in unknown amounts bounds nothing and
is useless. State the guarantee, and state it relative to the parsed set plus
the manifest's count of what didn't parse — complete over the known, with an
explicit residual for the unknown. **When you can't have both, prefer
completeness:** a missed trade is worse than a spurious hit a human can discard.

### Never silently drop a filing

An unknown filing type, an unreadable PDF, or a failed extraction must become a
record with an explicit `pdf_class` / `parse_status` **plus** a manifest entry —
never a gap. Preserve raw values (the FilingType letter, `raw_text` fallbacks)
alongside anything normalized, so nothing is lost to a parsing guess.

### `pull` is the only network step

`parse` and `read` (and `inspect`) must work offline and deterministically. No
wall-clock in core logic — capture one timestamp at command entry and thread it
into the manifests.

### Polite crawling is load-bearing

The crawl defaults — sequential requests, 2.5 s spacing, a descriptive
User-Agent with an operator contact, backoff — are deliberate, not incidental.
The Clerk's site has 403'd naive clients and there is no bulk PDF download to
fall back on, so don't strip them to go faster. `pull` **requires** a
`--contact "Name <email>"` (or `OPENHOUSE_CONTACT`) for exactly this reason.

### Legal restriction is a design constraint

Clerk financial-disclosure data may **not** be used for commercial purposes
(news/media dissemination excepted), for solicitation, or to establish credit
ratings. This is statutory — [5 U.S.C.
§ 13107](https://www.law.cornell.edu/uscode/text/5/13107) (Ethics in Government
Act as amended), not a request. openhouse is a research and transparency tool;
the README and `--help` say so, and you must **never add a feature that points
the other way**.

## Output contract

Machine output is the contract: **JSON to stdout** (`jq`-composable,
agent-consumable), **prose and progress to stderr**, and a **non-zero exit on
error**. The `--table` rendering is garnish for humans, never the thing another
tool parses. A query also fails loudly when the data dir is empty or unparsed —
"nothing here to query" is an error, never a silent "no matches."

## Decision-log convention

When a change embeds a non-obvious decision, record it as its **own additive
file**:

- Add `docs/decisions/GH-<issue:0000>-<slug>-<index>.md` — the issue number
  four-zero-padded (e.g. `GH-0152`), a kebab slug, and an index that climbs past
  `0001` only when one issue yields more than one decision.
- Add a **newest-first line** to the index,
  [`docs/decisions/README.md`](./docs/decisions/README.md).

The log is **additive only**: existing decisions are never edited or pruned. A
decision that overrides an older one says *"supersedes GH-NNNN"* in its own
file. The decision goes in `docs/decisions/`, never folded into `SPEC.md`'s
current-state text.

## When unsure, stop and ask

Most choices in this repo trace to `SPEC.md` or a prior decision. If something
is ambiguous or uncovered, ask rather than wing it — and prefer the **smallest
fix that fits**: no new abstractions, flags, or layers unless they're asked for.
